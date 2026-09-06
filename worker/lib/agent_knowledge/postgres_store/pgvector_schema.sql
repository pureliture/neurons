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

    -- Constraints
    CONSTRAINT chk_memory_cards_temporal CHECK (valid_to IS NULL OR valid_to >= valid_from),
    CONSTRAINT chk_memory_cards_confidence CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CONSTRAINT chk_memory_cards_lifecycle CHECK (lifecycle_state IN (
        'candidate', 'suggested_accept', 'human_accepted', 'human_rejected',
        'auto_accepted', 'needs_review', 'accepted', 'rejected'
    )),
    CONSTRAINT chk_memory_cards_auth CHECK (authorization_status IN ('active', 'disabled')),
    CONSTRAINT chk_memory_cards_currentness CHECK (currentness IN ('current', 'superseded', 'stale', 'conflicted', 'unknown')),
    CONSTRAINT chk_memory_cards_emb_state CHECK (embedding_state IN ('pending', 'ready', 'stale', 'failed'))
);

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
