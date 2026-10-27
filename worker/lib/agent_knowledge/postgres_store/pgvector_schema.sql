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
CREATE INDEX IF NOT EXISTS idx_lbrain_fact_current_support ON lbrain_fact_assertions(fact_key,value_hash,assertion_id)
    WHERE state IN ('support','pending');
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
-- 다른 source의 보류 근거가 채택된 값과 일치해 함께 사용되면, 그 source의
-- 원래 보류 기록보다 나중 결정이 오래 남더라도 근거를 먼저 지우지 못한다.
CREATE TABLE IF NOT EXISTS lbrain_fact_corroborations (
    resolution_id VARCHAR(64) NOT NULL REFERENCES lbrain_fact_resolutions(resolution_id)
        DEFERRABLE INITIALLY DEFERRED,
    assertion_id VARCHAR(64) NOT NULL REFERENCES lbrain_fact_assertions(assertion_id),
    PRIMARY KEY(resolution_id,assertion_id)
);
CREATE INDEX IF NOT EXISTS idx_lbrain_fact_corroboration_assertion ON lbrain_fact_corroborations(assertion_id);
CREATE OR REPLACE FUNCTION lbrain_preserve_fact_corroboration() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='UPDATE' OR EXISTS (SELECT 1 FROM lbrain_fact_resolutions WHERE resolution_id=OLD.resolution_id) THEN
        RAISE EXCEPTION 'fact_corroboration_retention_required' USING ERRCODE='23514';
    END IF;
    RETURN OLD;
END;
$$;
DROP TRIGGER IF EXISTS lbrain_preserve_fact_corroboration ON lbrain_fact_corroborations;
CREATE TRIGGER lbrain_preserve_fact_corroboration BEFORE UPDATE OR DELETE ON lbrain_fact_corroborations
    FOR EACH ROW EXECUTE FUNCTION lbrain_preserve_fact_corroboration();
INSERT INTO lbrain_fact_corroborations(resolution_id,assertion_id)
    SELECT r.resolution_id,item.value FROM lbrain_fact_resolutions r
    CROSS JOIN LATERAL jsonb_array_elements_text(COALESCE(r.result->'corroborating_assertion_ids','[]'::jsonb)) item
    ON CONFLICT DO NOTHING;

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
CREATE TABLE IF NOT EXISTS lbrain_fact_admission_targets (
    project VARCHAR(64) NOT NULL,
    target_title TEXT NOT NULL,
    card_type VARCHAR(32) NOT NULL,
    field_path VARCHAR(128) NOT NULL,
    fact_key VARCHAR(64) NOT NULL,
    PRIMARY KEY(project,target_title,card_type,field_path),
    UNIQUE(project,target_title,card_type,field_path,fact_key)
);
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
    PRIMARY KEY(project,policy_id)
);
-- 같은 필드는 하나의 fact에만 속하지만 그 fact를 검증하는 원천 정책은
-- 여러 개일 수 있다. 기존 단일 원천 UNIQUE를 소유권 FK로 교체한다.
INSERT INTO lbrain_fact_admission_targets(project,target_title,card_type,field_path,fact_key)
    SELECT DISTINCT project,target_title,card_type,field_path,fact_key FROM lbrain_fact_admission_rules
    ON CONFLICT DO NOTHING;
DO $$
DECLARE old_constraint TEXT;
BEGIN
    FOR old_constraint IN SELECT c.conname FROM pg_constraint c
        WHERE c.conrelid='lbrain_fact_admission_rules'::regclass AND c.contype='u'
            AND (SELECT array_agg(a.attname::text ORDER BY k.ordinality)
                FROM unnest(c.conkey) WITH ORDINALITY k(attnum,ordinality)
                JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum)
                = ARRAY['project','target_title','card_type','field_path']::text[]
    LOOP
        EXECUTE format('ALTER TABLE lbrain_fact_admission_rules DROP CONSTRAINT %I',old_constraint);
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='lbrain_fact_admission_rules'::regclass
        AND conname='lbrain_fact_admission_target_scope') THEN
        ALTER TABLE lbrain_fact_admission_rules ADD CONSTRAINT lbrain_fact_admission_target_scope
            FOREIGN KEY(project,target_title,card_type,field_path,fact_key)
            REFERENCES lbrain_fact_admission_targets(project,target_title,card_type,field_path,fact_key);
    END IF;
END;
$$;
CREATE OR REPLACE FUNCTION lbrain_admit_fact_target_scope() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE admitted_fact VARCHAR(64);
BEGIN
    -- 동일 대상의 동시 등록도 PK 충돌과 행 잠금으로 하나의 소유권에 합류한다.
    INSERT INTO lbrain_fact_admission_targets(project,target_title,card_type,field_path,fact_key)
        VALUES (NEW.project,NEW.target_title,NEW.card_type,NEW.field_path,NEW.fact_key)
        ON CONFLICT(project,target_title,card_type,field_path) DO UPDATE
            SET fact_key=lbrain_fact_admission_targets.fact_key
        RETURNING fact_key INTO admitted_fact;
    IF admitted_fact<>NEW.fact_key THEN
        -- 기존 INSERT ... ON CONFLICT DO NOTHING과 같은 거부 계약이다.
        -- coordinator는 등록되지 않은 정책을 fact_policy_changed로 보고한다.
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS lbrain_admit_fact_target_scope ON lbrain_fact_admission_rules;
CREATE TRIGGER lbrain_admit_fact_target_scope BEFORE INSERT OR UPDATE OF project,target_title,card_type,field_path,fact_key
    ON lbrain_fact_admission_rules FOR EACH ROW EXECUTE FUNCTION lbrain_admit_fact_target_scope();

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
ALTER TABLE lbrain_fact_source_receipts ADD COLUMN IF NOT EXISTS owner_project VARCHAR(64);
-- 관측 INSERT→source row 잠금과 같은 순서로 migration을 fence한다.
LOCK TABLE lbrain_fact_observations IN SHARE ROW EXCLUSIVE MODE;
LOCK TABLE lbrain_fact_source_receipts IN SHARE ROW EXCLUSIVE MODE;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM lbrain_fact_source_receipts WHERE content_hash IS DISTINCT FROM
        'sha256:' || encode(sha256(convert_to(redacted_content,'UTF8')),'hex')) THEN
        RAISE EXCEPTION 'fact_source_content_hash_mismatch' USING ERRCODE='23514';
    END IF;
END $$;

-- 교정 이력이 참조하는 근거를 실제 FK로 보존한다. 관측과 연결은 함께
-- 삭제할 수 있지만 assertion/이력이 남아 있으면 관측 FK가 삭제를 막는다.
CREATE TABLE IF NOT EXISTS lbrain_fact_observation_sources (
    observation_version VARCHAR(71) PRIMARY KEY REFERENCES lbrain_fact_observations(version_id)
        DEFERRABLE INITIALLY DEFERRED,
    source_ref_id VARCHAR(180) NOT NULL REFERENCES lbrain_fact_source_receipts(source_ref_id)
);
CREATE INDEX IF NOT EXISTS idx_lbrain_observation_sources_ref
    ON lbrain_fact_observation_sources(source_ref_id,observation_version);

CREATE OR REPLACE FUNCTION lbrain_check_observation_source() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE observed lbrain_fact_observations%ROWTYPE;
        receipt lbrain_fact_source_receipts%ROWTYPE;
BEGIN
    IF TG_OP='UPDATE' THEN
        RAISE EXCEPTION 'fact_observation_source_immutable' USING ERRCODE='23514';
    END IF;
    SELECT * INTO observed FROM lbrain_fact_observations WHERE version_id=NEW.observation_version FOR KEY SHARE;
    SELECT * INTO receipt FROM lbrain_fact_source_receipts WHERE source_ref_id=NEW.source_ref_id FOR UPDATE;
    IF observed.version_id IS NULL OR receipt.source_ref_id IS NULL THEN
        RAISE EXCEPTION 'fact_observation_source_required' USING ERRCODE='23514';
    END IF;
    IF receipt.owner_project IS NULL THEN
        UPDATE lbrain_fact_source_receipts SET owner_project=observed.project
            WHERE source_ref_id=NEW.source_ref_id RETURNING * INTO receipt;
    END IF;
    IF receipt.owner_project<>observed.project THEN
        RAISE EXCEPTION 'fact_source_project_conflict' USING ERRCODE='23514';
    END IF;
    IF observed.observation#>>'{proof,source_ref_id}' IS DISTINCT FROM NEW.source_ref_id
       OR observed.observation->>'content_hash' IS DISTINCT FROM receipt.content_hash
       OR observed.observation->>'project' IS DISTINCT FROM observed.project
       OR observed.observation->>'source_id' IS DISTINCT FROM observed.source_id
       OR observed.observation->>'source_revision' IS DISTINCT FROM observed.source_revision
       OR observed.observation->>'interpretation_version' IS DISTINCT FROM observed.interpretation_version THEN
        RAISE EXCEPTION 'fact_observation_source_mismatch' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS lbrain_check_observation_source ON lbrain_fact_observation_sources;
CREATE TRIGGER lbrain_check_observation_source BEFORE INSERT OR UPDATE ON lbrain_fact_observation_sources
    FOR EACH ROW EXECUTE FUNCTION lbrain_check_observation_source();

CREATE OR REPLACE FUNCTION lbrain_link_observation_source() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='UPDATE' THEN
        RAISE EXCEPTION 'fact_observation_immutable' USING ERRCODE='23514';
    END IF;
    IF NEW.observation#>>'{proof,source_ref_id}' IS NULL THEN
        RAISE EXCEPTION 'fact_observation_source_required' USING ERRCODE='23514';
    END IF;
    INSERT INTO lbrain_fact_observation_sources(observation_version,source_ref_id)
        VALUES (NEW.version_id,NEW.observation#>>'{proof,source_ref_id}');
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS lbrain_link_observation_source ON lbrain_fact_observations;
CREATE TRIGGER lbrain_link_observation_source AFTER INSERT ON lbrain_fact_observations
    FOR EACH ROW EXECUTE FUNCTION lbrain_link_observation_source();
DROP TRIGGER IF EXISTS lbrain_preserve_observation ON lbrain_fact_observations;
CREATE TRIGGER lbrain_preserve_observation BEFORE UPDATE ON lbrain_fact_observations
    FOR EACH ROW EXECUTE FUNCTION lbrain_link_observation_source();

-- 옛 관측에 근거가 없으면 migration 전체를 중단한다. dangling history를
-- 임의 삭제하거나 nullable FK로 통과시키지 않는다.
INSERT INTO lbrain_fact_observation_sources(observation_version,source_ref_id)
    SELECT version_id,observation#>>'{proof,source_ref_id}' FROM lbrain_fact_observations
    ON CONFLICT(observation_version) DO NOTHING;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='lbrain_fact_observations'::regclass
        AND conname='lbrain_observation_has_source') THEN
        ALTER TABLE lbrain_fact_observations ADD CONSTRAINT lbrain_observation_has_source
            FOREIGN KEY(version_id) REFERENCES lbrain_fact_observation_sources(observation_version)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION lbrain_preserve_fact_source() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='UPDATE' AND ((to_jsonb(NEW)-ARRAY['state','owner_project'])
            IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['state','owner_project'])
        OR (OLD.state='revoked' AND NEW.state<>'revoked')
        OR (OLD.owner_project IS NOT NULL AND NEW.owner_project IS DISTINCT FROM OLD.owner_project)) THEN
        RAISE EXCEPTION 'fact_source_immutable' USING ERRCODE='23514';
    END IF;
    IF NEW.content_hash IS DISTINCT FROM
        'sha256:' || encode(sha256(convert_to(NEW.redacted_content,'UTF8')),'hex') THEN
        RAISE EXCEPTION 'fact_source_content_hash_mismatch' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS lbrain_preserve_fact_source ON lbrain_fact_source_receipts;
CREATE TRIGGER lbrain_preserve_fact_source BEFORE INSERT OR UPDATE ON lbrain_fact_source_receipts
    FOR EACH ROW EXECUTE FUNCTION lbrain_preserve_fact_source();

CREATE TABLE IF NOT EXISTS lbrain_fact_source_events (
    event_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_ref_id VARCHAR(180) NOT NULL UNIQUE REFERENCES lbrain_fact_source_receipts(source_ref_id),
    project VARCHAR(64) NOT NULL,
    last_fact_key VARCHAR(64) NOT NULL DEFAULT '',
    last_policy_id VARCHAR(128) NOT NULL DEFAULT '',
    expansion_done BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS lbrain_fact_source_commands (
    command_id VARCHAR(71) PRIMARY KEY,
    request_hash VARCHAR(71) NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS lbrain_fact_source_targets (
    event_id UUID NOT NULL REFERENCES lbrain_fact_source_events(event_id),
    fact_key VARCHAR(64) NOT NULL REFERENCES lbrain_fact_scopes(fact_key),
    policy_id VARCHAR(128) NOT NULL DEFAULT '',
    task_id VARCHAR(64) REFERENCES lbrain_recheck_tasks(task_id),
    state VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','blocked','queued','expanded','covered')),
    reason_code VARCHAR(64) NOT NULL DEFAULT '',
    last_policy_id VARCHAR(128) NOT NULL DEFAULT '',
    next_check_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempt_count BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY(event_id,fact_key,policy_id)
);
CREATE INDEX IF NOT EXISTS idx_lbrain_fact_source_targets_due
    ON lbrain_fact_source_targets(next_check_at) WHERE state IN ('pending','blocked');

CREATE OR REPLACE FUNCTION lbrain_plan_new_rule_source_targets() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO lbrain_fact_source_targets(event_id,fact_key,policy_id)
        SELECT DISTINCT e.event_id,NEW.fact_key,NEW.policy_id
        FROM lbrain_fact_source_events e JOIN lbrain_fact_observation_sources s ON s.source_ref_id=e.source_ref_id
        JOIN lbrain_fact_assertions a ON a.observation_version=s.observation_version
        WHERE e.project=NEW.project AND a.fact_key=NEW.fact_key ON CONFLICT DO NOTHING;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS lbrain_plan_new_rule_source_targets ON lbrain_fact_admission_rules;
CREATE TRIGGER lbrain_plan_new_rule_source_targets AFTER INSERT ON lbrain_fact_admission_rules
    FOR EACH ROW EXECUTE FUNCTION lbrain_plan_new_rule_source_targets();

CREATE OR REPLACE FUNCTION lbrain_record_source_revocation() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.owner_project IS NULL THEN
        RETURN NEW; -- 참조가 없던 receipt의 tombstone은 향후 authority에 사용될 수 없다.
    END IF;
    INSERT INTO lbrain_fact_source_events(source_ref_id,project)
        VALUES (NEW.source_ref_id,NEW.owner_project) ON CONFLICT DO NOTHING;
    -- worker보다 먼저 새 조회와 cursor/cache에 철회가 보이도록 generation도
    -- 같은 transaction에서 올린다. callback/모델 호출은 trigger에 넣지 않는다.
    INSERT INTO lbrain_fact_project_versions(project,generation)
        VALUES (NEW.owner_project,1)
        ON CONFLICT(project) DO UPDATE SET generation=lbrain_fact_project_versions.generation+1;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS lbrain_record_source_revocation ON lbrain_fact_source_receipts;
CREATE TRIGGER lbrain_record_source_revocation AFTER UPDATE OF state,owner_project ON lbrain_fact_source_receipts
    FOR EACH ROW WHEN (NEW.state='revoked' AND (OLD.state<>'revoked'
        OR (OLD.owner_project IS NULL AND NEW.owner_project IS NOT NULL)))
    EXECUTE FUNCTION lbrain_record_source_revocation();

WITH added AS (
    INSERT INTO lbrain_fact_source_events(source_ref_id,project)
        SELECT source_ref_id,owner_project FROM lbrain_fact_source_receipts
        WHERE state='revoked' AND owner_project IS NOT NULL ON CONFLICT DO NOTHING RETURNING project
)
INSERT INTO lbrain_fact_project_versions(project,generation)
    SELECT DISTINCT project,1 FROM added ON CONFLICT(project)
    DO UPDATE SET generation=lbrain_fact_project_versions.generation+1;

ALTER TABLE lbrain_fact_scopes DROP CONSTRAINT IF EXISTS lbrain_fact_scopes_state_check;
ALTER TABLE lbrain_fact_scopes ADD CONSTRAINT lbrain_fact_scopes_state_check
    CHECK (state IN ('current','pending','disputed','invalidated'));

-- 원래 관측 시각과 서버의 현재성 재검증 시각을 분리한다. 기존 고정본의
-- 관측/성공 결과를 갱신하여 검증 기한을 늘리지 않는다.
CREATE TABLE IF NOT EXISTS lbrain_fact_verifications (
    resolution_id VARCHAR(64) PRIMARY KEY REFERENCES lbrain_fact_resolutions(resolution_id)
        DEFERRABLE INITIALLY DEFERRED,
    fact_key VARCHAR(64) NOT NULL REFERENCES lbrain_fact_scopes(fact_key),
    assertion_id VARCHAR(64) NOT NULL REFERENCES lbrain_fact_assertions(assertion_id),
    policy_id VARCHAR(128) NOT NULL,
    policy_version VARCHAR(71),
    max_age_seconds INTEGER NOT NULL CHECK (max_age_seconds BETWEEN 1 AND 86400),
    checked_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_lbrain_fact_verified_assertion
    ON lbrain_fact_verifications(assertion_id,checked_at DESC);
CREATE OR REPLACE FUNCTION lbrain_preserve_fact_verification() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP<>'INSERT' THEN
        RAISE EXCEPTION 'fact_verification_immutable' USING ERRCODE='23514';
    END IF;
    NEW.checked_at := clock_timestamp();
    NEW.expires_at := NEW.checked_at + NEW.max_age_seconds*interval '1 second';
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS lbrain_preserve_fact_verification ON lbrain_fact_verifications;
CREATE TRIGGER lbrain_preserve_fact_verification BEFORE INSERT OR UPDATE OR DELETE ON lbrain_fact_verifications
    FOR EACH ROW EXECUTE FUNCTION lbrain_preserve_fact_verification();
CREATE OR REPLACE FUNCTION lbrain_check_fact_verification() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM lbrain_fact_resolutions r
        JOIN lbrain_fact_assertions a ON a.assertion_id=NEW.assertion_id
        WHERE r.resolution_id=NEW.resolution_id AND r.fact_key=NEW.fact_key AND a.fact_key=NEW.fact_key
            AND r.terminal AND r.result->>'action' IN ('replace_current','attach_evidence')
            AND r.result->>'assertion_id'=NEW.assertion_id) THEN
        RAISE EXCEPTION 'fact_verification_resolution_mismatch' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS lbrain_check_fact_verification ON lbrain_fact_verifications;
CREATE CONSTRAINT TRIGGER lbrain_check_fact_verification AFTER INSERT ON lbrain_fact_verifications
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lbrain_check_fact_verification();
CREATE TABLE IF NOT EXISTS lbrain_fact_check_schedules (
    fact_key VARCHAR(64) NOT NULL REFERENCES lbrain_fact_scopes(fact_key) DEFERRABLE INITIALLY DEFERRED,
    project VARCHAR(64) NOT NULL,
    policy_id VARCHAR(128) NOT NULL,
    verified_resolution_id VARCHAR(64) REFERENCES lbrain_fact_verifications(resolution_id),
    next_check_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    state VARCHAR(16) NOT NULL DEFAULT 'scheduled' CHECK(state IN ('scheduled','queued','blocked')),
    generation BIGINT NOT NULL DEFAULT 1 CHECK(generation>=1),
    task_id VARCHAR(64) REFERENCES lbrain_recheck_tasks(task_id),
    reason_code VARCHAR(64) NOT NULL DEFAULT '',
    PRIMARY KEY(fact_key,policy_id)
);
-- 첫 동시 입력은 상대 정책의 확인 예약을 먼저 만들고 같은 transaction에서
-- 공통 head를 생성할 수 있다. commit 시에는 여전히 실제 head가 필수다.
DO $$
DECLARE scope_constraint TEXT;
BEGIN
    FOR scope_constraint IN SELECT conname FROM pg_constraint
        WHERE conrelid='lbrain_fact_check_schedules'::regclass
            AND confrelid='lbrain_fact_scopes'::regclass AND contype='f'
    LOOP
        EXECUTE format('ALTER TABLE lbrain_fact_check_schedules ALTER CONSTRAINT %I DEFERRABLE INITIALLY DEFERRED',scope_constraint);
    END LOOP;
END;
$$;
CREATE INDEX IF NOT EXISTS idx_lbrain_fact_checks_due
    ON lbrain_fact_check_schedules(project,next_check_at) WHERE state IN ('scheduled','blocked');
CREATE TABLE IF NOT EXISTS lbrain_fact_batches (
    batch_id VARCHAR(64) PRIMARY KEY,
    project VARCHAR(64) NOT NULL,
    request_hash VARCHAR(71) NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS lbrain_fact_target_decisions (
    decision_id VARCHAR(64) PRIMARY KEY,
    task_id VARCHAR(64) NOT NULL UNIQUE REFERENCES lbrain_recheck_tasks(task_id),
    request_hash VARCHAR(71) NOT NULL,
    input_hash VARCHAR(71) NOT NULL,
    resolution_id VARCHAR(64) REFERENCES lbrain_fact_resolutions(resolution_id),
    snapshot JSONB NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(task_id,input_hash) REFERENCES lbrain_recheck_judgments(task_id,input_hash)
);
CREATE OR REPLACE FUNCTION lbrain_preserve_target_decision() RETURNS TRIGGER
LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION 'target_decision_retention_required' USING ERRCODE='23514';
END; $$;
DROP TRIGGER IF EXISTS lbrain_preserve_target_decision ON lbrain_fact_target_decisions;
CREATE TRIGGER lbrain_preserve_target_decision BEFORE UPDATE OR DELETE ON lbrain_fact_target_decisions
    FOR EACH ROW EXECUTE FUNCTION lbrain_preserve_target_decision();
CREATE OR REPLACE FUNCTION lbrain_check_target_decision() RETURNS TRIGGER
LANGUAGE plpgsql AS $$ BEGIN
    IF NOT EXISTS(SELECT 1 FROM lbrain_recheck_tasks t JOIN lbrain_recheck_judgments j ON j.task_id=t.task_id
        WHERE t.task_id=NEW.task_id AND t.request_hash=NEW.request_hash AND j.input_hash=NEW.input_hash
            AND j.judgment->'target_inventory'=NEW.snapshot
            AND NEW.result->>'project'=t.project
            AND NEW.result->>'profile_hash'=NEW.snapshot->>'profile_hash'
            AND (NEW.resolution_id IS NOT NULL OR (NEW.result->>'reason_code'='target_not_found'
                AND j.judgment#>>'{target_selection,reason_code}'='target_not_found')))
        OR (NEW.resolution_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM lbrain_fact_resolutions r
            JOIN lbrain_recheck_tasks t ON t.task_id=NEW.task_id
            WHERE r.resolution_id=NEW.resolution_id AND r.fact_key=t.request->>'fact_key' AND r.terminal)) THEN
        RAISE EXCEPTION 'target_decision_binding_mismatch' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END; $$;
DROP TRIGGER IF EXISTS lbrain_check_target_decision ON lbrain_fact_target_decisions;
CREATE CONSTRAINT TRIGGER lbrain_check_target_decision AFTER INSERT ON lbrain_fact_target_decisions
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lbrain_check_target_decision();
CREATE OR REPLACE FUNCTION lbrain_preserve_target_judgment() RETURNS TRIGGER
LANGUAGE plpgsql AS $$ BEGIN
    IF EXISTS(SELECT 1 FROM lbrain_fact_target_decisions WHERE task_id=OLD.task_id AND input_hash=OLD.input_hash) THEN
        RAISE EXCEPTION 'target_judgment_retention_required' USING ERRCODE='23514';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END; $$;
DROP TRIGGER IF EXISTS lbrain_preserve_target_judgment ON lbrain_recheck_judgments;
CREATE TRIGGER lbrain_preserve_target_judgment BEFORE UPDATE OR DELETE ON lbrain_recheck_judgments
    FOR EACH ROW EXECUTE FUNCTION lbrain_preserve_target_judgment();
-- graph/embedding 반영 전에도 관련된 현재 fact만 indexed lexical 후보로 회수한다.
-- 서버의 독립 source 관측을 먼저 보존하고 교정/지연 native 수집이 공유한다.
CREATE TABLE IF NOT EXISTS lbrain_fact_source_reads (
    observation_ref VARCHAR(64) PRIMARY KEY,
    project VARCHAR(64) NOT NULL,
    request_key VARCHAR(180) NOT NULL,
    request_hash VARCHAR(71) NOT NULL,
    policy_id VARCHAR(128) NOT NULL,
    policy_version VARCHAR(71) NOT NULL,
    fact_key VARCHAR(64) NOT NULL,
    target_memory_id VARCHAR(64) NOT NULL REFERENCES memory_cards(memory_id),
    observation_version VARCHAR(71) NOT NULL REFERENCES lbrain_fact_observations(version_id),
    result JSONB NOT NULL,
    result_hash VARCHAR(71) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(project,request_key)
);
CREATE OR REPLACE FUNCTION lbrain_preserve_source_read() RETURNS TRIGGER
LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION 'source_observation_read_immutable' USING ERRCODE='23514';
END; $$;
DROP TRIGGER IF EXISTS lbrain_preserve_source_read ON lbrain_fact_source_reads;
CREATE TRIGGER lbrain_preserve_source_read BEFORE UPDATE ON lbrain_fact_source_reads
    FOR EACH ROW EXECUTE FUNCTION lbrain_preserve_source_read();
CREATE TABLE IF NOT EXISTS lbrain_fact_source_read_tasks (
    task_id VARCHAR(64) PRIMARY KEY REFERENCES lbrain_recheck_tasks(task_id) DEFERRABLE INITIALLY DEFERRED,
    observation_ref VARCHAR(64) NOT NULL REFERENCES lbrain_fact_source_reads(observation_ref)
);
CREATE OR REPLACE FUNCTION lbrain_check_source_read_binding() RETURNS TRIGGER
LANGUAGE plpgsql AS $$ BEGIN
    IF NOT EXISTS(SELECT 1 FROM lbrain_fact_observations o
        JOIN memory_cards c ON c.memory_id=NEW.target_memory_id AND c.project=NEW.project
        JOIN lbrain_fact_admission_rules r ON r.project=NEW.project AND r.policy_id=NEW.policy_id
            AND r.fact_key=NEW.fact_key AND r.policy_version=NEW.policy_version
        WHERE o.version_id=NEW.observation_version AND o.project=NEW.project
            AND o.observation#>>'{proof,policy_version}'=NEW.policy_version
            AND NEW.result->>'observation_ref'=NEW.observation_ref
            AND NEW.result->>'project'=NEW.project AND NEW.result->>'fact_key'=NEW.fact_key
            AND NEW.result->>'policy_id'=NEW.policy_id
            AND NEW.result->>'observation_id'=o.observation_id
            AND NEW.result->>'observation_version'=NEW.observation_version
            AND NEW.result->'canonical_write_performed'='false'::jsonb
            AND NEW.result#>>'{evidence,revision}'=o.source_revision
            AND NEW.result#>>'{evidence,content_hash}'=o.observation->>'content_hash'
            AND NEW.result#>>'{evidence,observation_ref}'=NEW.observation_ref) THEN
        RAISE EXCEPTION 'source_read_binding_mismatch' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END; $$;
DROP TRIGGER IF EXISTS lbrain_check_source_read_binding ON lbrain_fact_source_reads;
CREATE TRIGGER lbrain_check_source_read_binding BEFORE INSERT ON lbrain_fact_source_reads
    FOR EACH ROW EXECUTE FUNCTION lbrain_check_source_read_binding();
CREATE OR REPLACE FUNCTION lbrain_check_source_read_task() RETURNS TRIGGER
LANGUAGE plpgsql AS $$ BEGIN
    IF TG_OP='UPDATE' OR NOT EXISTS(SELECT 1 FROM lbrain_fact_source_reads r
        JOIN lbrain_recheck_tasks t ON t.task_id=NEW.task_id AND t.project=r.project
        WHERE r.observation_ref=NEW.observation_ref AND t.request->>'fact_key'=r.fact_key
            AND t.request->>'policy_version'=r.policy_version
            AND t.request#>'{change,evidence}'=r.result->'evidence') THEN
        RAISE EXCEPTION 'source_read_task_binding_mismatch' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END; $$;
DROP TRIGGER IF EXISTS lbrain_check_source_read_task ON lbrain_fact_source_read_tasks;
CREATE TRIGGER lbrain_check_source_read_task BEFORE INSERT OR UPDATE ON lbrain_fact_source_read_tasks
    FOR EACH ROW EXECUTE FUNCTION lbrain_check_source_read_task();
CREATE TABLE IF NOT EXISTS lbrain_fact_native_read_links (
    task_id VARCHAR(64) PRIMARY KEY REFERENCES lbrain_recheck_tasks(task_id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(task_id) REFERENCES lbrain_fact_source_read_tasks(task_id),
    observation_ref VARCHAR(64) NOT NULL REFERENCES lbrain_fact_source_reads(observation_ref),
    native_observation_id VARCHAR(71) NOT NULL,
    native_receipt_hash VARCHAR(71) NOT NULL,
    native_source_ref_id VARCHAR(64) NOT NULL REFERENCES lbrain_fact_source_receipts(source_ref_id),
    native_proof JSONB NOT NULL,
    resolution_id VARCHAR(64) NOT NULL REFERENCES lbrain_fact_resolutions(resolution_id) DEFERRABLE INITIALLY DEFERRED,
    join_only BOOLEAN NOT NULL,
    authority_write_performed BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE OR REPLACE FUNCTION lbrain_check_native_read_binding() RETURNS TRIGGER
LANGUAGE plpgsql AS $$ BEGIN
    IF NEW.join_only AND NEW.authority_write_performed OR NOT EXISTS(
        SELECT 1 FROM lbrain_fact_source_read_tasks b
        JOIN lbrain_fact_source_reads r ON r.observation_ref=b.observation_ref
        JOIN lbrain_recheck_tasks t ON t.task_id=b.task_id AND t.project=r.project
        JOIN lbrain_fact_resolutions x ON x.resolution_id=NEW.resolution_id AND x.fact_key=r.fact_key AND x.terminal
        JOIN lbrain_fact_source_receipts s ON s.source_ref_id=NEW.native_source_ref_id AND s.owner_project=r.project
        WHERE b.task_id=NEW.task_id AND b.observation_ref=NEW.observation_ref
            AND t.request#>>'{native_source_read,observation_id}'=NEW.native_observation_id
            AND t.request#>>'{native_source_read,receipt_hash}'=NEW.native_receipt_hash
            AND s.proof->>'receipt_hash'=NEW.native_receipt_hash
            AND s.proof->>'adapter'='native_source_read_reference.v1') THEN
        RAISE EXCEPTION 'native_read_binding_mismatch' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END; $$;
DROP TRIGGER IF EXISTS lbrain_check_native_read_binding ON lbrain_fact_native_read_links;
CREATE CONSTRAINT TRIGGER lbrain_check_native_read_binding AFTER INSERT ON lbrain_fact_native_read_links
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION lbrain_check_native_read_binding();
CREATE OR REPLACE FUNCTION lbrain_preserve_native_read_link() RETURNS TRIGGER
LANGUAGE plpgsql AS $$ BEGIN
    IF TG_OP='UPDATE' OR EXISTS(SELECT 1 FROM lbrain_fact_resolutions WHERE resolution_id=OLD.resolution_id) THEN
        RAISE EXCEPTION 'native_read_link_retention_required' USING ERRCODE='23514';
    END IF;
    RETURN OLD;
END; $$;
DROP TRIGGER IF EXISTS lbrain_preserve_native_read_link ON lbrain_fact_native_read_links;
CREATE TRIGGER lbrain_preserve_native_read_link BEFORE UPDATE OR DELETE ON lbrain_fact_native_read_links
    FOR EACH ROW EXECUTE FUNCTION lbrain_preserve_native_read_link();
CREATE UNIQUE INDEX IF NOT EXISTS idx_lbrain_native_read_route
    ON lbrain_recheck_tasks(project,(request->>'native_route_key')) WHERE request ? 'native_route_key';
CREATE TABLE IF NOT EXISTS lbrain_native_source_cursors (
    project VARCHAR(64) NOT NULL,
    source_key VARCHAR(71) NOT NULL,
    sequence TEXT NOT NULL DEFAULT '0',
    generation BIGINT NOT NULL DEFAULT 0,
    checked_at TIMESTAMPTZ,
    attempted_at TIMESTAMPTZ,
    reason_code VARCHAR(64) NOT NULL DEFAULT '',
    PRIMARY KEY(project,source_key)
);
ALTER TABLE lbrain_native_source_cursors ADD COLUMN IF NOT EXISTS attempted_at TIMESTAMPTZ;
ALTER TABLE lbrain_native_source_cursors ADD COLUMN IF NOT EXISTS reason_code VARCHAR(64) NOT NULL DEFAULT '';
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
