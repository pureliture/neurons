# LBrain Architecture Rationalization: Design & Technical Specification (v2.3)

- **Status**: Hardened After Implementation Audit (v2.3)
- **Date**: 2026-09-02
- **Target Repository**: `neurons` (Server/Brain Authority)
- **Worktree**: `/Users/ddalkak/Projects/neurons/.worktrees/lbrain-architecture-rationalization-spec`
- **Review & Audit History**: `specs/lbrain-architecture-rationalization/review.md` 참조

---

## 1. Target System Architecture

```mermaid
flowchart TB
    subgraph ClientPlane ["1. Client Sensor Plane (dendrite)"]
        Hook["Provider Hooks (Claude / Codex / Antigravity / Hermes)"]
        Spool["Durable Local Spool / Outbox"]
        Drain["transcript-drain (Bounded Shipper)"]
        Hook --> Spool --> Drain
    end

    subgraph ServerPlane ["2. Unified Brain Authority Plane (neurons)"]
        Ingress["Ingress API / NATS JetStream"]
        Worker["Python Worker (Ingress & Recall)"]
        
        subgraph PostgresStorage ["🐘 Core Authority Engine: PostgreSQL 17+ (pgvector >= 0.8.0)"]
            Relational["Ledger Entities (36 Core)
- status & authorization_status"]
            Edges["DAG & Temporal Graph
- memory_edges (다대다 DAG, valid_from/to)"]
            PgVector["pgvector (HNSW Index)
- Session Chunks & Card Embeddings
- iterative_scan = 'relaxed_order'"]
            Outbox["Transactional Outbox
- embedding_outbox (Lease & Dual CAS Update)"]
            JsonbStore["JSONB Documents
- Raw Transcript & Evidence Bundles"]
        end

        subgraph GraphWorkbench ["🕸️ 2-Track: Neo4j / Graphiti (Cold-Path 1-Way Derived Projection)"]
            Neo4j["Neo4j Graph Store"]
            Graphiti["Graphiti Extraction Pipeline (Batch / On-Demand)"]
            Graphiti <--> Neo4j
        end

        Ingress --> Worker
        Worker <== "psycopg Real SQL Driver (No In-Memory Mocking)" ==> PostgresStorage
        PostgresStorage -. "1-Way Async Projection (Eventual)" .-> GraphWorkbench
    end

    subgraph MCPSurface ["3. Rationalized MCP Interface (2-Tier)"]
        AgentMCP["agent_memory (Agent Public Surface - 2 Tools)
1. brain.resolve (mode=list | context | query)
   ==> Calls PgVectorStore.hybrid_search()
2. memory_candidate_create (Proposal-only)
* Tiered Serializer: slim (~1.2KB) | with_evidence"]
        
        AdminMCP["agent_memory_admin (Admin Surface - Auth Gated)
- approve / reject / supersede_commit / stale_commit
- audit probes / corpus management
- key: lbrain_admin (독립 프로세스/엔드포인트)"]
    end

    Worker <--> AgentMCP
    Worker <--> AdminMCP
    AgentMCP <==> CodingAgents["AI Coding Agents (Antigravity / Hermes / Claude)"]
    AdminMCP <==> Operator["Human Operator / CI/CD Pipeline"]
```

---

## 2. PostgreSQL Storage Layer Specification (Real SQL Only)

### 2.1. Extension Configuration
```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector"; -- pgvector >= 0.8.0 required
```

### 2.2. Schema DDL: Memory Cards, Edges, Chunks & Outbox

```sql
-- 1. 정형 지식 / 결정 카드 테이블
CREATE TABLE memory_cards (
    memory_id VARCHAR(64) PRIMARY KEY,
    project VARCHAR(64) NOT NULL,
    card_type VARCHAR(32) NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    typed_payload JSONB NOT NULL,
    lifecycle_state VARCHAR(32) NOT NULL DEFAULT 'candidate',
    authorization_status VARCHAR(32) NOT NULL DEFAULT 'disabled',
    currentness VARCHAR(32) NOT NULL DEFAULT 'current',
    confidence NUMERIC(3,2) NOT NULL DEFAULT 1.0,
    valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_to TIMESTAMPTZ,
    embedding_model VARCHAR(64) NOT NULL DEFAULT 'text-embedding-3-small',
    embedding_revision INT NOT NULL DEFAULT 1,
    embedding_state VARCHAR(32) NOT NULL DEFAULT 'pending',
    embedding vector(1536),
    content_hash VARCHAR(71) NOT NULL,
    source_ref JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_memory_cards_active ON memory_cards(project, authorization_status, currentness);
CREATE INDEX idx_memory_cards_temporal ON memory_cards(project, valid_from, valid_to);
CREATE INDEX idx_memory_cards_embedding ON memory_cards USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);


-- 2. 다대다 DAG 및 관계 간선 테이블
CREATE TABLE memory_edges (
    edge_id BIGSERIAL PRIMARY KEY,
    src_id VARCHAR(64) NOT NULL REFERENCES memory_cards(memory_id) ON DELETE RESTRICT,
    rel_type VARCHAR(32) NOT NULL,
    dst_id VARCHAR(64) NOT NULL REFERENCES memory_cards(memory_id) ON DELETE RESTRICT,
    provenance_hash VARCHAR(71) NOT NULL,
    confidence NUMERIC(3,2) NOT NULL DEFAULT 1.0,
    valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_to TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_memory_edges_traversal ON memory_edges(src_id, rel_type, dst_id);
CREATE INDEX idx_memory_edges_reverse ON memory_edges(dst_id, rel_type);


-- 3. 세션 대화 청크 테이블 (Dual CAS 지원을 위해 content_hash 추가)
CREATE TABLE session_memory_chunks (
    chunk_id VARCHAR(64) PRIMARY KEY,
    session_id_hash VARCHAR(71) NOT NULL,
    project VARCHAR(64) NOT NULL,
    provider VARCHAR(32) NOT NULL,
    chunk_index INT NOT NULL,
    content_markdown TEXT NOT NULL,
    content_hash VARCHAR(71) NOT NULL,
    embedding_model VARCHAR(64) NOT NULL DEFAULT 'text-embedding-3-small',
    embedding_state VARCHAR(32) NOT NULL DEFAULT 'pending',
    embedding_revision INT NOT NULL DEFAULT 1,
    embedding vector(1536),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_session_chunks_lookup ON session_memory_chunks(project, session_id_hash);
CREATE INDEX idx_session_chunks_embedding ON session_memory_chunks USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);


-- 4. Transactional Outbox 테이블 (임베딩 비동기 안전 생성 및 동시성 락)
CREATE TABLE embedding_outbox (
    outbox_id BIGSERIAL PRIMARY KEY,
    target_type VARCHAR(32) NOT NULL, -- 'memory_card', 'session_chunk'
    target_id VARCHAR(64) NOT NULL,
    content_hash VARCHAR(71) NOT NULL,
    payload_text TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'queued',
    claimed_at TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    worker_id VARCHAR(64),
    retry_count INT NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_embedding_outbox_queue ON embedding_outbox(status, created_at) WHERE status IN ('queued', 'failed');
CREATE UNIQUE INDEX idx_embedding_outbox_dedup ON embedding_outbox(target_type, target_id, content_hash) WHERE status IN ('queued', 'processing');
```

---

## 3. Real SQL Execution & Dual CAS Pattern

### 3.1. PgVectorStore Python Interface (No In-Memory Dicts)
`PgVectorStore`는 `self.cards = {}` 같은 인메모리 딕셔너리를 일체 포함하지 않으며, 모든 메서드가 `psycopg` 커넥션을 통해 실제 쿼리를 실행한다. 연결 실패 시 조용히 메모리로 폴백하지 않고 **`ConnectionError`를 발생(Fail-Closed)**시킨다.

### 3.2. Worker Lease Claim & Dual CAS Write-Back

#### ① Worker Lease Claim
```sql
UPDATE embedding_outbox
   SET status = 'processing',
       worker_id = :worker_id,
       claimed_at = NOW(),
       lease_until = NOW() + INTERVAL '30 seconds',
       updated_at = NOW()
 WHERE outbox_id IN (
     SELECT outbox_id
       FROM embedding_outbox
      WHERE (status = 'queued' OR (status = 'failed' AND retry_count < 5))
        AND (lease_until IS NULL OR lease_until < NOW())
      ORDER BY created_at ASC
      LIMIT 10
      FOR UPDATE SKIP LOCKED
 )
 RETURNING outbox_id, target_type, target_id, content_hash, payload_text;
```

#### ② Dual CAS Write-Back (Cards & Chunks)
```sql
-- 1) memory_cards CAS
UPDATE memory_cards
   SET embedding = :vector,
       embedding_state = 'ready',
       embedding_revision = embedding_revision + 1,
       updated_at = NOW()
 WHERE memory_id = :target_id
   AND content_hash = :enqueued_content_hash;

-- 2) session_memory_chunks CAS (누락 방지)
UPDATE session_memory_chunks
   SET embedding = :vector,
       embedding_state = 'ready',
       embedding_revision = embedding_revision + 1,
       updated_at = NOW()
 WHERE chunk_id = :target_id
   AND content_hash = :enqueued_content_hash;

-- 3) Outbox 완료 처리
UPDATE embedding_outbox
   SET status = 'completed', updated_at = NOW()
 WHERE outbox_id = :outbox_id;
```

---

## 4. MCP Interface & Real Search Wiring

### 4.1. `brain.resolve` Execution Flow
```mermaid
sequenceDiagram
    autonumber
    actor Agent as Coding Agent
    participant MCP as mcp_jsonrpc.py
    participant Serializer as slim_serializer.py
    participant PGStore as pgvector_store.py (psycopg)
    participant PG as PostgreSQL 17+

    Agent->>MCP: brain.resolve(query="OCI app plane", mode="query")
    MCP->>PGStore: hybrid_search(project="neurons", query_vector, text_query)
    PGStore->>PG: SET LOCAL hnsw.iterative_scan = 'relaxed_order';
    PGStore->>PG: SELECT ... FROM memory_cards WHERE active AND current ORDER BY vector <=> :q
    PG-->>PGStore: Rows (card_id, payload, hash, similarity)
    PGStore-->>MCP: List[SearchResult]
    MCP->>Serializer: serialize_slim(results) or serialize_with_evidence(results)
    Serializer-->>MCP: Slim Payload (<2.0 KB, no empty lanes)
    MCP-->>Agent: JSON-RPC Result
```

---

## 5. Zero Regression Strategy for Existing Tests

- **레거시 픽스처 호환**: 기존 테스트(`test_artifact_preference_evaluator.py`, `test_ledger.py` 등)에서 사용하는 `content_hash=""` 또는 `sha256:x` 같은 비표준 해시를 수용할 수 있도록 `validate_content_hash(hash_str, strict=False)` 옵션을 제공한다.
- **수용 기준**: `cd worker && uv run pytest -q` 실행 시 기존 3,400여 개 테스트가 1건의 실패도 없이 100% 통과해야 한다.
