# LBrain Architecture Rationalization: Requirements & Problem Statement (v2.3)

- **Status**: Hardened After Implementation Audit (v2.3)
- **Date**: 2026-09-02
- **Target Repository**: `neurons` (Server/Brain Authority)
- **Worktree**: `/Users/ddalkak/Projects/neurons/.worktrees/lbrain-architecture-rationalization-spec`
- **Review & Audit History**: `specs/lbrain-architecture-rationalization/review.md` 참조

---

## 1. Executive Summary (Precision Reframed & Hardened)

`LBrain` (LLM-Brain) 시스템의 아키텍처 개편 목표는 "수만 건 규모의 벡터 검색 성능 최적화"가 아니라, **"운영 표면 축소(Operational Surface Minimization)", "동일 엔진 트랜잭션 일관성(ACID/Dual-Write Elimination)", "직렬화 비대화 해소(Slim Payload)"**이다.

Live 런타임 실측 결과, 현재 도메인 규모는 권위 카드 8개, 세션 아티팩트 69개 수준이며, `brain_context_resolve` 호출 1회당 **68.5KB(약 17,000 토큰)**에 달하는 극심한 직렬화 오버헤드가 발생하고 있다.

**[v2.3 핵심 반성 및 보강 조치]**:
커밋 `165e56f`에 대한 사후 감사 결과, `PgVectorStore`가 실제 SQL이 아닌 Python 딕셔너리(`self.cards = {}`)로 시뮬레이션되었고, DB 연결 실패 시 조용히 메모리 모드로 빠지는 치명적인 Fail-silent 결함이 확인되었다. 또한 섀도우 벤치마크 역시 `MockQdrantClient` + 더미 벡터 기반의 하니스 단위 검증에 불과했다.
본 v2.3 명세는 이러한 **"인메모리 모의(Mocking) 및 허위 컷오버 증거"를 원천 금지**하고, **실제 `psycopg` 기반 SQL 실행, Fail-Closed 원칙, 실제 백엔드 벤치마크, 기존 275개 테스트 무회귀(Zero Regression)**를 엄격히 강제한다.

---

## 2. Core Requirements by Domain

### 2.1. Domain 1: PostgreSQL & pgvector (Real SQL & Fail-Closed)
- **R1.1 (운영 수렴)**: Qdrant 전용 클러스터 대신 기존 PostgreSQL 인스턴스의 `pgvector >= 0.8.0` 확장을 활용하여 백업, PITR, 트랜잭션 관리를 단일 엔진으로 수렴한다.
- **R1.2 (No In-Memory Mocking & Fail-Closed)**: `PgVectorStore` 내부에서 Python 딕셔너리로 DB 동작을 흉내 내는 시뮬레이션을 원천 금지하며, 실제 `psycopg` SQL 쿼리(`INSERT`, `SELECT`, `UPDATE`)를 실행해야 한다. DB 연결 실패 시 조용히 메모리 모드로 전환(Fail-silent)하지 않고 **즉시 예외를 발생(Fail-Closed)**시켜야 한다.
- **R1.3 (Dual CAS Write-Back Protection)**: 
  - `memory_cards` 및 `session_memory_chunks` 양쪽 모두에 대해, 비동기 워커가 임베딩을 반영할 때 큐 생성 시점의 `content_hash`와 일치할 때만 업데이트하는 **CAS (Compare-And-Swap)** 가드를 적용하여 스텔(Stale) 덮어쓰기 레이스를 차단한다.
- **R1.4 (Outbox Lease & Idempotency)**: `embedding_outbox`에 중복 인큐 방지 유니크 인덱스를 두고, 다중 워커의 안전한 처리를 위해 `claimed_at`, `lease_until`, `worker_id`, `retry_count` 기반의 리스(Lease) 메커니즘을 적용한다.
- **R1.5 (Real Backend Dual-Read Shadow Gate)**: 컷오버 증거는 모의 클라이언트(Mock)나 더미 벡터가 아닌, 실제 PostgreSQL 인스턴스(포트 15432)와 실제 Qdrant를 대상으로 한 벤치마크에서 `Recall@5 >= 0.95` 및 `P95 Latency <= 20ms`를 달성해야만 유효하다.

### 2.2. Domain 2: Graphiti & Neo4j Strategy (Hot/Cold Separation)
- **R2.1 (Hot-path 완전 격리)**: 실시간 대화 수집 및 MCP 응답 루프에서 Graphiti의 실시간 LLM 엔티티 추출을 차단하여 레이턴시(300초 타임아웃)와 API 비용을 0으로 만든다.
- **R2.2 (Cold-path 단방향 파생 워크벤치)**: 에이전트 메모리의 복합 관계, 다단계 인과관계, temporal fact 구간 탐색을 위해 기성 Neo4j/Graphiti 파이프라인을 PostgreSQL의 변경을 비동기로 수신하는 단방향(Eventual-consistent) 파생 워크벤치로 유지한다.
- **R2.3 (RDBMS 다대다 DAG 및 순환 방지)**: PostgreSQL에 `memory_edges` 테이블을 두어 다중 대체, 근거 분기, `valid_from/to` 구간을 보존하며, 재귀 조회 시 깊이 제한(`depth < 5`) 및 순환 방지(`CYCLE`) 가드를 적용한다.

### 2.3. Domain 3: MCP 2-Tier & Real Search Engine Wiring
- **R3.1 (Agent Public Surface 2개화)**:
  - `brain.resolve (query, mode="list"|"context"|"query", project, response_mode, as_of)`: 단일 통합 읽기 도구.
  - `memory_candidate_create`: 제안 전용 쓰기 도구 (Proposal-only, rate-limited, project-scoped, ledger write 직접 불가).
- **R3.2 (Actual Semantic Search Wiring)**: `brain.resolve(mode=query)`는 기존 ledger의 문자열 `in` 검색이 아니라, 반드시 `PgVectorStore.hybrid_search()`를 직접 호출하여 실제 pgvector 시맨틱/하이브리드 검색을 수행해야 한다.
- **R3.3 (Tiered Slim Payload & Hard Limit)**:
  - `response_mode="slim"` (기본): 결정, 선호도, 현재 태스크, 활성 가드레일을 ~1.2KB(Soft Token Budget: 250~500 토큰, Hard Max: 3KB)로 압축 제공하며 초과 시 deterministic truncation 및 `has_more: true` 반환.
  - `response_mode="with_evidence"` (선택): 증거 해시 체인(`evidence_hashes`), 다단계 엣지(`edges`), 상세 페이로드 포함.
- **R3.4 (Zero Regression & Hash Compatibility)**: 엄격한 SHA-256 검증기 도입으로 인해 기존 275개 테스트가 깨지지 않도록, 레거시 테스트 픽스처를 수용하는 하위 호환 처리로 전체 worker 테스트 무회귀(Zero Regression)를 보장한다.
- **R3.5 (Admin Control Plane 물리 격리)**: `memory_candidate_approve`, `memory_supersede_commit`, 감사 프로브 등은 별도의 `agent_memory_admin` 서비스 키(`lbrain_admin`) 및 독립 엔드포인트/프로세스로 물리적 격리한다.

---

## 3. Acceptance Criteria (수용 기준)

### AC1. Real Database & Concurrency
- [ ] `PgVectorStore`의 모든 CRUD 및 검색이 실제 `psycopg` SQL 경로를 거쳐야 하며, 내부 dict 시뮬레이션이 없어야 한다.
- [ ] DB 연결 실패 시 in-memory fallback 없이 즉시 Fail-Closed 예외를 던져야 한다.
- [ ] `memory_cards` 및 `session_memory_chunks` 양쪽 모두 CAS update가 동작하여 해시 불일치 시 No-op 되어야 한다.
- [ ] `SET LOCAL hnsw.iterative_scan = 'relaxed_order';`가 `pgvector >= 0.8.0` 인스턴스에서 정상 실행되어야 한다.

### AC2. Real Dual-Read Benchmark
- [ ] 실제 PostgreSQL(포트 15432) 인스턴스에서 DDL이 정상 실행되고 테이블이 생성되어야 한다.
- [ ] 실제 DB 기반 하이브리드 검색에서 `Recall@5 >= 0.95` 및 `P95 Latency <= 20ms`가 실측되어야 한다.

### AC3. MCP Interface & Zero Regression
- [ ] `brain.resolve(mode=query)`가 `PgVectorStore`를 직접 호출해야 한다.
- [ ] 기본 `slim` 모드 응답 크기는 2.0 KB 이하(Hard Max: 3.0 KB)여야 한다.
- [ ] 기존 worker 테스트 전체(`cd worker && uv run pytest -q`)가 단 1건의 실패도 없이 100% Pass 되어야 한다.
