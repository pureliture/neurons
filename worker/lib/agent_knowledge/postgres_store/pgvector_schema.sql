-- ==============================================================================
-- PostgreSQL DDL Schema for LBrain Memory Authority (Milestone 3)
-- Target Engine: PostgreSQL 17+ with pgvector >= 0.8.0
-- ==============================================================================

-- 0. Required Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector"; -- pgvector >= 0.8.0 required

-- ==============================================================================
-- 1. Table: memory_cards (Authoritative Knowledge & Decision Cards)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS memory_cards (
    memory_id VARCHAR(64) PRIMARY KEY,
    project VARCHAR(64) NOT NULL,
    card_type VARCHAR(32) NOT NULL, -- 'decision', 'preference', 'task', 'evidence', 'drift', 'status'
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    typed_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    
    -- State Machine (Orthogonal separation)
    lifecycle_state VARCHAR(32) NOT NULL DEFAULT 'candidate',
    authorization_status VARCHAR(32) NOT NULL DEFAULT 'disabled',
    currentness VARCHAR(32) NOT NULL DEFAULT 'current',
    confidence NUMERIC(3,2) NOT NULL DEFAULT 1.0,
    
    -- Temporal Validity Window
    valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_to TIMESTAMPTZ,
    
    -- Gemini Embedding 2 full output; halfvec permits dimensions above vector's 2,000 limit.
    embedding_model VARCHAR(64) NOT NULL DEFAULT 'gemini-embedding-2',
    embedding_revision INT NOT NULL DEFAULT 1,
    embedding_state VARCHAR(32) NOT NULL DEFAULT 'pending', -- 'pending', 'ready', 'stale', 'failed'
    embedding halfvec(3072),
    
    -- Integrity Hash & Source Lineage
    content_hash VARCHAR(71) NOT NULL, -- sha256:[0-9a-f]{64}
    source_ref JSONB NOT NULL DEFAULT '[]'::jsonb,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Freshness & Temporal Metadata (FR-AGE-040~045)
    freshness_state VARCHAR(32) NOT NULL DEFAULT 'unchecked',
    freshness_checked_at TIMESTAMPTZ,
    freshness_next_check_after TIMESTAMPTZ,
    source_hash VARCHAR(71),
    observed_at TIMESTAMPTZ,

    -- Constraints
    CONSTRAINT chk_memory_cards_temporal CHECK (valid_to IS NULL OR valid_to >= valid_from),
    CONSTRAINT chk_memory_cards_confidence CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CONSTRAINT chk_memory_cards_lifecycle CHECK (lifecycle_state IN (
        'candidate', 'suggested_accept', 'human_accepted', 'human_rejected',
        'auto_accepted', 'needs_review', 'accepted', 'rejected'
    )),
    CONSTRAINT chk_memory_cards_auth CHECK (authorization_status IN ('active', 'disabled')),
    CONSTRAINT chk_memory_cards_currentness CHECK (currentness IN ('current', 'superseded', 'stale', 'conflicted', 'unknown', 'retired')),
    CONSTRAINT chk_memory_cards_emb_state CHECK (embedding_state IN ('pending', 'ready', 'stale', 'failed'))
);

-- Steward envelope is separate from the legacy memory_cards lane. Additive and
-- repeatable on databases created before steward promotion.
ALTER TABLE memory_cards ADD COLUMN IF NOT EXISTS steward_envelope JSONB;
CREATE INDEX IF NOT EXISTS idx_steward_review_queue
    ON memory_cards(project, created_at DESC, memory_id)
    WHERE steward_envelope IS NOT NULL AND lifecycle_state IN ('candidate', 'needs_review', 'suggested_accept');

-- Freshness and temporal metadata columns (FR-AGE-040~045)
-- Repeatable and additive for existing databases created before freshness schema.
ALTER TABLE memory_cards ADD COLUMN IF NOT EXISTS freshness_state VARCHAR(32) NOT NULL DEFAULT 'unchecked';
ALTER TABLE memory_cards ADD COLUMN IF NOT EXISTS freshness_checked_at TIMESTAMPTZ;
ALTER TABLE memory_cards ADD COLUMN IF NOT EXISTS freshness_next_check_after TIMESTAMPTZ;
ALTER TABLE memory_cards ADD COLUMN IF NOT EXISTS source_hash VARCHAR(71);
ALTER TABLE memory_cards ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;

-- Ensure currentness check constraint permits 'retired' state on pre-existing tables.
ALTER TABLE memory_cards DROP CONSTRAINT IF EXISTS chk_memory_cards_currentness;
ALTER TABLE memory_cards ADD CONSTRAINT chk_memory_cards_currentness
    CHECK (currentness IN ('current', 'superseded', 'stale', 'conflicted', 'unknown', 'retired'));

CREATE TABLE IF NOT EXISTS steward_card_decisions (
    decision_id TEXT PRIMARY KEY,
    memory_id VARCHAR(64) NOT NULL REFERENCES memory_cards(memory_id),
    content_hash VARCHAR(71) NOT NULL,
    action VARCHAR(32) NOT NULL CHECK (action IN ('approve', 'auto_accept', 'reject', 'supersede', 'stale', 'correct_fact')),
    actor TEXT NOT NULL,
    target_memory_id VARCHAR(64),
    result_card JSONB,
    result_target JSONB,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE steward_card_decisions ADD COLUMN IF NOT EXISTS result_card JSONB;
ALTER TABLE steward_card_decisions ADD COLUMN IF NOT EXISTS result_target JSONB;
ALTER TABLE steward_card_decisions DROP CONSTRAINT IF EXISTS steward_card_decisions_action_check;
ALTER TABLE steward_card_decisions ADD CONSTRAINT steward_card_decisions_action_check
    CHECK (action IN ('approve', 'auto_accept', 'reject', 'supersede', 'stale', 'correct_fact'));
CREATE INDEX IF NOT EXISTS idx_steward_card_decisions_memory_id ON steward_card_decisions(memory_id);

CREATE TABLE IF NOT EXISTS steward_correction_denials (
    audit_event_id TEXT PRIMARY KEY,
    request_hash VARCHAR(71) NOT NULL,
    reason_code VARCHAR(80) NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 서명된 인간 검토 묶음의 불변 결과. 기존 단건 audit/outbox 의미는 바꾸지 않는다.
-- 전체 안전 응답(result)은 action/actor/reason 등 service의 검증된 메타데이터도 보존한다.
-- IF NOT EXISTS로 기존 데이터 손실 없이 반복 적용할 수 있는 추가 테이블이다.
CREATE TABLE IF NOT EXISTS steward_bundle_decisions (
    decision_id TEXT PRIMARY KEY,
    request_fingerprint TEXT NOT NULL,
    project TEXT NOT NULL,
    brain_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    member_ids JSONB NOT NULL CHECK (jsonb_typeof(member_ids) = 'array'),
    result JSONB NOT NULL CHECK (jsonb_typeof(result) = 'object'),
    decided_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_steward_bundle_decisions_scope
    ON steward_bundle_decisions(project, brain_id, scope, decided_at, decision_id);

-- Indexes for memory_cards
CREATE INDEX IF NOT EXISTS idx_memory_cards_active 
    ON memory_cards(project, authorization_status, currentness);

CREATE INDEX IF NOT EXISTS idx_memory_cards_temporal 
    ON memory_cards(project, valid_from, valid_to);

CREATE INDEX IF NOT EXISTS idx_memory_cards_content_hash 
    ON memory_cards(content_hash);

CREATE INDEX IF NOT EXISTS idx_memory_cards_card_type 
    ON memory_cards(card_type);

CREATE INDEX IF NOT EXISTS idx_memory_cards_lifecycle 
    ON memory_cards(lifecycle_state);

CREATE INDEX IF NOT EXISTS idx_memory_cards_typed_payload 
    ON memory_cards USING gin(typed_payload);

CREATE INDEX IF NOT EXISTS idx_memory_cards_source_ref 
    ON memory_cards USING gin(source_ref);

-- HNSW Cosine Distance Index (halfvec_cosine_ops, m=16, ef_construction=64)
CREATE INDEX IF NOT EXISTS idx_memory_cards_embedding 
    ON memory_cards 
    USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_memory_cards_freshness 
    ON memory_cards(project, freshness_state, freshness_next_check_after);


-- ==============================================================================
-- 2. Table: session_memory_chunks (Historical Conversation Semantic Archive)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS session_memory_chunks (
    chunk_id VARCHAR(64) PRIMARY KEY,
    session_id_hash VARCHAR(71) NOT NULL,
    project VARCHAR(64) NOT NULL,
    provider VARCHAR(32) NOT NULL DEFAULT 'unspecified',
    chunk_index INT NOT NULL DEFAULT 0,
    content_markdown TEXT NOT NULL,
    token_count INT NOT NULL DEFAULT 0,
    content_hash VARCHAR(71) NOT NULL,
    embedding_model VARCHAR(64) NOT NULL DEFAULT 'gemini-embedding-2',
    embedding_state VARCHAR(32) NOT NULL DEFAULT 'pending',
    embedding_revision INT NOT NULL DEFAULT 1,
    embedding halfvec(3072),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Observed (source) time of the session this chunk came from. Nullable:
    -- legacy/backfilled rows may not carry it. It must never default to NOW(),
    -- because that would masquerade ingestion time as observation time (age bug).
    observed_at TIMESTAMPTZ,
    CONSTRAINT chk_session_chunks_emb_state CHECK (embedding_state IN ('pending', 'ready', 'stale', 'failed')),
    CONSTRAINT chk_session_chunks_emb_revision CHECK (embedding_revision >= 1)
);

-- Normalize legacy nullable values before enforcing the authority contract.
UPDATE session_memory_chunks SET token_count = 0 WHERE token_count IS NULL;
ALTER TABLE session_memory_chunks
    ALTER COLUMN token_count SET DEFAULT 0,
    ALTER COLUMN token_count SET NOT NULL;

-- Additive upgrade for installations created by the pre-v2.3 schema. The
-- fallback hash is only an upgrade marker; new writes always provide the
-- content hash calculated by the ingest path.
ALTER TABLE session_memory_chunks
    ADD COLUMN IF NOT EXISTS content_hash VARCHAR(71) DEFAULT 'sha256:legacy';
ALTER TABLE session_memory_chunks
    ADD COLUMN IF NOT EXISTS embedding_state VARCHAR(32) NOT NULL DEFAULT 'pending';
ALTER TABLE session_memory_chunks
    ADD COLUMN IF NOT EXISTS embedding_revision INT NOT NULL DEFAULT 1;
ALTER TABLE session_memory_chunks
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Additive upgrade: preserve source observation time. Nullable on purpose; a
-- missing value means "unknown age", never "now".
ALTER TABLE session_memory_chunks
    ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;

-- Indexes for session_memory_chunks
CREATE INDEX IF NOT EXISTS idx_session_chunks_lookup 
    ON session_memory_chunks(project, session_id_hash);

CREATE INDEX IF NOT EXISTS idx_session_chunks_embedding 
    ON session_memory_chunks 
    USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);


-- ==============================================================================
-- 3. Table: memory_edges (Many-to-Many Relational Lineage & Provenance DAG)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS memory_edges (
    edge_id BIGSERIAL PRIMARY KEY,
    src_id VARCHAR(64) NOT NULL REFERENCES memory_cards(memory_id) ON DELETE RESTRICT,
    rel_type VARCHAR(32) NOT NULL, -- 'supersedes', 'derived_from', 'contradicts', 'supports'
    dst_id VARCHAR(64) NOT NULL REFERENCES memory_cards(memory_id) ON DELETE RESTRICT,
    provenance_hash VARCHAR(71) NOT NULL, -- sha256:[0-9a-f]{64}
    confidence NUMERIC(3,2) NOT NULL DEFAULT 1.0,
    properties JSONB NOT NULL DEFAULT '{}'::jsonb,
    valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_to TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Constraints
    CONSTRAINT chk_memory_edges_rel_type CHECK (rel_type IN ('supersedes', 'derived_from', 'contradicts', 'supports')),
    CONSTRAINT chk_memory_edges_confidence CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CONSTRAINT chk_memory_edges_temporal CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

-- Indexes for memory_edges
CREATE INDEX IF NOT EXISTS idx_memory_edges_traversal 
    ON memory_edges(src_id, rel_type, dst_id);

CREATE INDEX IF NOT EXISTS idx_memory_edges_reverse 
    ON memory_edges(dst_id, rel_type);

CREATE INDEX IF NOT EXISTS idx_memory_edges_temporal 
    ON memory_edges(src_id, valid_from, valid_to);

CREATE INDEX IF NOT EXISTS idx_memory_edges_properties 
    ON memory_edges USING gin(properties);


-- ==============================================================================
-- 4. Table: embedding_outbox (Transactional Outbox, Worker Leases & CAS Queue)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS embedding_outbox (
    outbox_id BIGSERIAL PRIMARY KEY,
    target_type VARCHAR(32) NOT NULL, -- 'memory_card', 'session_chunk'
    target_id VARCHAR(64) NOT NULL,
    content_hash VARCHAR(71) NOT NULL, -- sha256:[0-9a-f]{64}
    payload_text TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'queued', -- 'queued', 'processing', 'completed', 'cas_skipped', 'failed', 'dead_letter'
    claimed_at TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    worker_id VARCHAR(64),
    retry_count INT NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Constraints
    CONSTRAINT chk_embedding_outbox_status CHECK (status IN ('queued', 'processing', 'completed', 'cas_skipped', 'failed', 'dead_letter')),
    CONSTRAINT chk_embedding_outbox_retry CHECK (retry_count >= 0)
);

-- Fast polling partial index for active/retryable worker tasks
CREATE INDEX IF NOT EXISTS idx_embedding_outbox_queue 
    ON embedding_outbox(status, created_at) 
    WHERE status IN ('queued', 'failed');

-- Deduplication partial unique index preventing duplicate active enqueues
CREATE UNIQUE INDEX IF NOT EXISTS idx_embedding_outbox_dedup 
    ON embedding_outbox(target_type, target_id, content_hash) 
    WHERE status IN ('queued', 'processing');

-- 재시도 대기 중인 failed 행도 active job이다. 이전 index보다 엄격한
-- 제약을 먼저 검증하며, 기존 중복 데이터가 있으면 schema 적용을 거부한다.
CREATE UNIQUE INDEX IF NOT EXISTS idx_embedding_outbox_retry_dedup
    ON embedding_outbox(target_type, target_id, content_hash)
    WHERE status IN ('queued', 'processing', 'failed');

-- Graphiti/Neo4j is a derived projection. This outbox is the target durable
-- hand-off for the future PG-authority write path; the current legacy graph
-- trigger still uses its ledger-backed projection cursor until that cutover
-- milestone is enabled.
CREATE TABLE IF NOT EXISTS graph_projection_outbox (
    projection_id BIGSERIAL PRIMARY KEY,
    source_type VARCHAR(32) NOT NULL,
    source_id VARCHAR(128) NOT NULL,
    source_revision VARCHAR(128) NOT NULL,
    content_hash VARCHAR(71) NOT NULL,
    episode_payload JSONB NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'queued',
    claimed_at TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    worker_id VARCHAR(64),
    retry_count INT NOT NULL DEFAULT 0,
    last_error TEXT,
    lease_generation BIGINT NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_graph_projection_outbox_status CHECK (
        status IN ('queued', 'processing', 'completed', 'failed', 'dead_letter')
    ),
    CONSTRAINT chk_graph_projection_outbox_retry CHECK (retry_count >= 0),
    CONSTRAINT uq_graph_projection_outbox_source_revision
        UNIQUE (source_type, source_id, source_revision)
);

CREATE INDEX IF NOT EXISTS idx_graph_projection_outbox_queue
    ON graph_projection_outbox(status, created_at)
    WHERE status IN ('queued', 'failed');

ALTER TABLE graph_projection_outbox ADD COLUMN IF NOT EXISTS lease_generation
    BIGINT NOT NULL DEFAULT 0 CHECK (lease_generation >= 0);

-- 기존 memory를 대상으로 하는 재검토 예약. 등록 자체는 authority를 바꾸지 않는다.
CREATE TABLE IF NOT EXISTS lbrain_recheck_tasks (
    task_id VARCHAR(64) PRIMARY KEY,
    project VARCHAR(64) NOT NULL,
    memory_id VARCHAR(64) NOT NULL REFERENCES memory_cards(memory_id),
    request_hash VARCHAR(71) NOT NULL,
    request JSONB NOT NULL,
    state VARCHAR(16) NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued','waiting','leased','resolved','blocked')),
    next_check_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_token VARCHAR(32),
    lease_until TIMESTAMPTZ,
    worker_id VARCHAR(128),
    generation BIGINT NOT NULL DEFAULT 0 CHECK (generation>=0),
    attempt_count BIGINT NOT NULL DEFAULT 0 CHECK (attempt_count>=0),
    last_input_hash VARCHAR(71),
    last_check_at TIMESTAMPTZ,
    result_ref VARCHAR(180) NOT NULL DEFAULT '',
    reason_code VARCHAR(64) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK ((state='leased') = (lease_token IS NOT NULL AND lease_until IS NOT NULL AND worker_id IS NOT NULL)),
    CHECK (state<>'resolved' OR result_ref<>'')
);
CREATE INDEX IF NOT EXISTS idx_lbrain_recheck_due
    ON lbrain_recheck_tasks(project,next_check_at) WHERE state IN ('queued','waiting','blocked','leased');
CREATE UNIQUE INDEX IF NOT EXISTS idx_lbrain_recheck_candidate_route
    ON lbrain_recheck_tasks(project,(request->>'candidate_route_key'))
    WHERE request ? 'candidate_route_key';

CREATE TABLE IF NOT EXISTS lbrain_recheck_attempts (
    task_id VARCHAR(64) NOT NULL REFERENCES lbrain_recheck_tasks(task_id),
    generation BIGINT NOT NULL,
    worker_id VARCHAR(128) NOT NULL,
    state VARCHAR(16) NOT NULL DEFAULT 'running'
        CHECK (state IN ('running','waiting','resolved','blocked','lease_expired')),
    input_hash VARCHAR(71),
    result_ref VARCHAR(180) NOT NULL DEFAULT '',
    reason_code VARCHAR(64) NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    PRIMARY KEY(task_id,generation)
);
CREATE TABLE IF NOT EXISTS lbrain_recheck_judgments (
    task_id VARCHAR(64) NOT NULL REFERENCES lbrain_recheck_tasks(task_id),
    input_hash VARCHAR(71) NOT NULL,
    judgment JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(task_id,input_hash)
);
CREATE TABLE IF NOT EXISTS lbrain_recheck_heartbeats (
    project VARCHAR(64) NOT NULL,
    worker_id VARCHAR(128) NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(project,worker_id)
);

-- 관측·해석·현재 사실을 분리한다. source 본문은 기존 immutable source store에 둔다.
CREATE TABLE IF NOT EXISTS lbrain_fact_scopes (
    fact_key VARCHAR(64) PRIMARY KEY,
    project VARCHAR(64) NOT NULL,
    scope JSONB NOT NULL,
    memory_id VARCHAR(64) NOT NULL REFERENCES memory_cards(memory_id),
    field_path VARCHAR(128) NOT NULL,
    value JSONB NOT NULL,
    value_hash VARCHAR(71) NOT NULL,
    generation BIGINT NOT NULL DEFAULT 0,
    head_assertion_id VARCHAR(64),
    state VARCHAR(24) NOT NULL DEFAULT 'current' CHECK (state IN ('current','pending','disputed')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_lbrain_facts_project ON lbrain_fact_scopes(project, fact_key);
CREATE TABLE IF NOT EXISTS lbrain_fact_members (
    memory_id VARCHAR(64) NOT NULL REFERENCES memory_cards(memory_id),
    field_path VARCHAR(128) NOT NULL,
    fact_key VARCHAR(64) NOT NULL REFERENCES lbrain_fact_scopes(fact_key),
    render_template TEXT NOT NULL,
    PRIMARY KEY(memory_id,field_path)
);
CREATE INDEX IF NOT EXISTS idx_lbrain_fact_members_key ON lbrain_fact_members(fact_key);
CREATE TABLE IF NOT EXISTS lbrain_fact_observations (
    version_id VARCHAR(71) PRIMARY KEY,
    observation_id VARCHAR(71) NOT NULL,
    project VARCHAR(64) NOT NULL,
    source_id VARCHAR(71) NOT NULL,
    source_revision VARCHAR(180) NOT NULL,
    interpretation_version VARCHAR(128) NOT NULL,
    observation JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_lbrain_fact_observation_origin
    ON lbrain_fact_observations(project,observation_id);
CREATE TABLE IF NOT EXISTS lbrain_fact_assertions (
    assertion_id VARCHAR(64) PRIMARY KEY,
    fact_key VARCHAR(64) NOT NULL REFERENCES lbrain_fact_scopes(fact_key),
    observation_version VARCHAR(71) NOT NULL REFERENCES lbrain_fact_observations(version_id),
    value JSONB NOT NULL,
    value_hash VARCHAR(71) NOT NULL,
    state VARCHAR(24) NOT NULL CHECK (state IN ('support','history','pending','revoked')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(fact_key,observation_version,value_hash)
);
CREATE TABLE IF NOT EXISTS lbrain_fact_resolutions (
    resolution_id VARCHAR(64) PRIMARY KEY,
    fact_key VARCHAR(64) NOT NULL REFERENCES lbrain_fact_scopes(fact_key),
    request_hash VARCHAR(71) NOT NULL,
    terminal BOOLEAN NOT NULL DEFAULT FALSE,
    result JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS lbrain_fact_resolution_events (
    event_id VARCHAR(64) PRIMARY KEY,
    resolution_id VARCHAR(64) NOT NULL REFERENCES lbrain_fact_resolutions(resolution_id),
    assertion_id VARCHAR(64) NOT NULL REFERENCES lbrain_fact_assertions(assertion_id),
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS lbrain_fact_project_versions (
    project VARCHAR(64) PRIMARY KEY,
    generation BIGINT NOT NULL DEFAULT 0
);

-- 공통 교정만 managed card의 내용/상태를 바꾼다. 과거 resolution ID의 재사용은
-- 허용하지 않으며 card, intent, resolution은 같은 transaction에서 commit한다.
CREATE TABLE IF NOT EXISTS lbrain_fact_mutation_intents (
    resolution_id VARCHAR(64) PRIMARY KEY REFERENCES lbrain_fact_resolutions(resolution_id)
        DEFERRABLE INITIALLY DEFERRED,
    fact_key VARCHAR(64) NOT NULL REFERENCES lbrain_fact_scopes(fact_key),
    transaction_id TEXT NOT NULL,
    target_memory_id VARCHAR(64) NOT NULL REFERENCES memory_cards(memory_id),
    candidate_memory_id VARCHAR(64) NOT NULL REFERENCES memory_cards(memory_id)
        DEFERRABLE INITIALLY DEFERRED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- 오래된 Repeatable Read snapshot도 신규 관리 대상을 누락하지 못하게 한다.
-- scope 등록이 epoch를 바꾸면 과거 snapshot의 writer는 serialization 실패한다.
CREATE TABLE IF NOT EXISTS lbrain_fact_guard_epoch (
    singleton BOOLEAN PRIMARY KEY CHECK (singleton),
    generation BIGINT NOT NULL DEFAULT 0
);
INSERT INTO lbrain_fact_guard_epoch(singleton) VALUES (TRUE) ON CONFLICT DO NOTHING;

-- queue 대기 중에는 아직 fact head가 없을 수 있다. 첫 요청 수락 시 서버가
-- 확정한 정확한 대상 범위를 등록하여 그 사이의 일반 승인도 막는다.
CREATE TABLE IF NOT EXISTS lbrain_fact_admission_rules (
    project VARCHAR(64) NOT NULL,
    policy_id VARCHAR(128) NOT NULL,
    policy_version VARCHAR(71) NOT NULL,
    fact_key VARCHAR(64) NOT NULL,
    target_memory_id VARCHAR(64) NOT NULL REFERENCES memory_cards(memory_id),
    target_title TEXT NOT NULL,
    card_type VARCHAR(32) NOT NULL,
    field_path VARCHAR(128) NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY(project,policy_id),
    UNIQUE(project,target_title,card_type,field_path)
);

CREATE OR REPLACE FUNCTION lbrain_fact_scope_guard_epoch() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE lbrain_fact_guard_epoch SET generation=generation+1 WHERE singleton;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS lbrain_fact_scope_guard_epoch ON lbrain_fact_scopes;
CREATE TRIGGER lbrain_fact_scope_guard_epoch AFTER INSERT ON lbrain_fact_scopes
    FOR EACH ROW EXECUTE FUNCTION lbrain_fact_scope_guard_epoch();
DROP TRIGGER IF EXISTS lbrain_fact_rule_guard_epoch ON lbrain_fact_admission_rules;
CREATE TRIGGER lbrain_fact_rule_guard_epoch AFTER INSERT OR UPDATE OR DELETE ON lbrain_fact_admission_rules
    FOR EACH ROW EXECUTE FUNCTION lbrain_fact_scope_guard_epoch();

CREATE OR REPLACE FUNCTION lbrain_guard_managed_card() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    managed BOOLEAN;
    admitted BOOLEAN;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF EXISTS (SELECT 1 FROM lbrain_fact_members WHERE memory_id=OLD.memory_id)
           OR EXISTS (SELECT 1 FROM lbrain_fact_scopes WHERE memory_id=OLD.memory_id)
           OR EXISTS (SELECT 1 FROM lbrain_fact_admission_rules WHERE target_memory_id=OLD.memory_id) THEN
            RAISE EXCEPTION 'managed_fact_history_retention_required' USING ERRCODE='23514';
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        -- Embedding lease/CAS는 사실의 내용이나 수명 상태를 바꾸지 않는다.
        IF (to_jsonb(NEW) - ARRAY['embedding','embedding_model','embedding_state','embedding_revision','updated_at'])
           = (to_jsonb(OLD) - ARRAY['embedding','embedding_model','embedding_state','embedding_revision','updated_at']) THEN
            RETURN NEW;
        END IF;
    END IF;
    PERFORM generation FROM lbrain_fact_guard_epoch WHERE singleton FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'managed_fact_guard_unavailable' USING ERRCODE='23514';
    END IF;
    SELECT EXISTS (SELECT 1 FROM lbrain_fact_members WHERE memory_id=NEW.memory_id)
        OR EXISTS (SELECT 1 FROM lbrain_fact_scopes WHERE memory_id=NEW.memory_id)
        OR EXISTS (SELECT 1 FROM lbrain_fact_admission_rules WHERE target_memory_id=NEW.memory_id)
        INTO managed;
    IF NEW.authorization_status='active' AND NEW.currentness='current' THEN
        managed := managed OR EXISTS (
            SELECT 1 FROM lbrain_fact_members m JOIN memory_cards c ON c.memory_id=m.memory_id
            WHERE c.project=NEW.project AND c.card_type=NEW.card_type AND c.title=NEW.title)
            OR EXISTS (SELECT 1 FROM lbrain_fact_admission_rules r WHERE r.project=NEW.project
                AND r.card_type=NEW.card_type AND r.target_title=NEW.title);
    END IF;
    IF managed THEN
        SELECT EXISTS (SELECT 1 FROM lbrain_fact_mutation_intents i
            WHERE i.resolution_id=current_setting('neurons.fact_resolution_id',TRUE)
              AND i.transaction_id=pg_current_xact_id()::text
              AND NEW.memory_id IN (i.target_memory_id,i.candidate_memory_id)) INTO admitted;
        IF NOT admitted THEN
            RAISE EXCEPTION 'managed_fact_resolution_required' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS lbrain_guard_managed_card ON memory_cards;
CREATE TRIGGER lbrain_guard_managed_card BEFORE INSERT OR UPDATE OR DELETE ON memory_cards
    FOR EACH ROW EXECUTE FUNCTION lbrain_guard_managed_card();

CREATE OR REPLACE FUNCTION lbrain_check_fact_mutation_result() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM lbrain_fact_resolutions r WHERE r.resolution_id=NEW.resolution_id
        AND r.fact_key=NEW.fact_key AND r.terminal
        AND r.result->>'action'='replace_current'
        AND r.result->>'authority_write_performed'='true'
        AND r.result->>'successor_memory_id'=NEW.candidate_memory_id
        AND r.result->'current'->>'memory_id'=NEW.candidate_memory_id) THEN
        RAISE EXCEPTION 'managed_fact_resolution_result_required' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS lbrain_check_fact_mutation_result ON lbrain_fact_mutation_intents;
CREATE CONSTRAINT TRIGGER lbrain_check_fact_mutation_result
    AFTER INSERT OR UPDATE ON lbrain_fact_mutation_intents DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION lbrain_check_fact_mutation_result();

CREATE OR REPLACE FUNCTION lbrain_preserve_fact_mutation_intent() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'managed_fact_history_retention_required' USING ERRCODE='23514';
END;
$$;
DROP TRIGGER IF EXISTS lbrain_preserve_fact_mutation_intent ON lbrain_fact_mutation_intents;
CREATE TRIGGER lbrain_preserve_fact_mutation_intent BEFORE UPDATE OR DELETE ON lbrain_fact_mutation_intents
    FOR EACH ROW EXECUTE FUNCTION lbrain_preserve_fact_mutation_intent();

-- 명시적으로 허용된 source adapter의 redacted 근거만 보존한다.
CREATE TABLE IF NOT EXISTS lbrain_fact_source_receipts (
    source_ref_id VARCHAR(180) PRIMARY KEY,
    content_hash VARCHAR(71) NOT NULL,
    redacted_content TEXT NOT NULL,
    proof JSONB NOT NULL,
    state VARCHAR(16) NOT NULL DEFAULT 'active' CHECK (state IN ('active','revoked')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS lbrain_fact_batches (
    batch_id VARCHAR(64) PRIMARY KEY,
    project VARCHAR(64) NOT NULL,
    request_hash VARCHAR(71) NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- graph/embedding 반영 전에도 관련된 현재 fact만 indexed lexical 후보로 회수한다.
CREATE INDEX IF NOT EXISTS idx_memory_cards_fact_text ON memory_cards USING GIN
    (to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(summary,'')));

-- source 본문은 CouchDB에 둔다. 같은 표현/벡터를 재사용해도 원천 version과 관측 참조는 보존한다.
CREATE TABLE IF NOT EXISTS session_memory_chunk_sources (
    chunk_id VARCHAR(64) NOT NULL REFERENCES session_memory_chunks(chunk_id),
    source_hash VARCHAR(71) NOT NULL,
    source_content_hash VARCHAR(71) NOT NULL,
    representation_content_hash VARCHAR(71) NOT NULL,
    observation_refs JSONB NOT NULL,
    provenance_hash VARCHAR(71) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(chunk_id,source_hash)
);
