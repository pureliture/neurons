# LBrain Architecture Rationalization: Requirements & Problem Statement (v2.5)

- **Status**: Decision-aligned target architecture; multi-agent review remediation (v2.5)
- **Date**: 2026-09-03
- **Target Repository**: `neurons` (Server/Brain Authority)
- **Worktree**: `/Users/ddalkak/Projects/neurons/.worktrees/lbrain-architecture-rationalization-spec`
- **Review & Audit History**: `specs/lbrain-architecture-rationalization/review.md` 참조

---

## 1. 최종 결정 요약

이번 결정은 모든 것을 PostgreSQL 하나로 합치는 결정이 아니다. **관계형 권위 저장소와 그래프 우선 조회를 분리하되, 임베딩 벡터의 운영 경로만 Qdrant에서 PostgreSQL로 수렴**하는 결정이다.

| 책임 | 정본/주요 경로 | 역할 |
|---|---|---|
| 승인·권한·currentness·content hash·명시적 근거 | PostgreSQL | 변경과 권위의 정본(SoT) |
| 엔티티·관계·시간·다단계 경로 | Graphiti → Neo4j | Graph-first 조회와 temporal graph |
| 카드·세션 청크의 의미 벡터 | PostgreSQL `halfvec(3072)` | 그래프 미투영 데이터와 graph 장애 시 명시적 fallback |
| 기존 벡터 색인 | Qdrant | 이관 및 dual-read shadow 기간에만 사용, 공개 조회 경로에서는 제거 |
| 코드 구조 분석 | 사용하지 않음 | AST/Graphify는 제품 런타임 범위에서 제외 |

중요한 원칙은 **조회 우선순위와 데이터 권위는 다른 축**이라는 점이다. Neo4j가 먼저 후보를 찾더라도 승인 여부, currentness, 프로젝트 범위, content hash는 PostgreSQL을 다시 확인한다. 반대로 PostgreSQL은 권위 저장소이지만 모든 일반 조회의 첫 번째 검색 엔진은 아니다.

현재 문서와 기본값은 이 목표 아키텍처에 맞춘다. M3에서 project-scoped `brain.resolve`의 Graph-first 라우터를 구현하고 실제 로컬 PostgreSQL·Graphiti adapter 변환으로 검증했다. 다만 Neo4j live 검증, graph projection writer/consumer와 실데이터 cutover는 별도의 실행 마일스톤이며, 로컬 검증이 운영 완료를 뜻하지 않는다.

---

## 2. 핵심 요구사항

### 2.1. R1 — PostgreSQL 권위 저장소와 임베딩 계약

- **R1.1 (권위 경계)**: PostgreSQL은 `lifecycle_state`, `authorization_status`, `currentness`, `content_hash`, 승인 기록, 프로젝트 범위, `memory_edges`의 명시적 관계를 보유한다. Neo4j/Graphiti는 이 값을 임의로 승인하거나 변경하는 두 번째 권위가 될 수 없다.
- **R1.2 (고정 임베딩 profile)**: 일반 LBrain 메모리 벡터의 정식 profile은 `lbrain-memory-gemini-embedding-2-v1`이다.
  - model: `gemini-embedding-2`
  - dimension: `3072`
  - distance: cosine
  - PostgreSQL type: `halfvec(3072)`
  - HNSW operator class: `halfvec_cosine_ops`
- **R1.3 (실제 SQL·Fail-Closed)**: `PgVectorStore`의 CRUD와 fallback 검색은 실제 `psycopg` SQL을 사용한다. 연결 실패나 schema 불일치를 Python dict, 임시 메모리 저장소, 빈 결과로 숨기지 않고 호출자에게 예외와 명시적 degraded 상태를 전달한다.
- **R1.4 (Dual CAS)**: `memory_cards`와 `session_memory_chunks`의 임베딩 write-back은 outbox 생성 당시 `content_hash`와 현재 행의 hash가 일치할 때만 반영한다. 불일치는 성공으로 위장하지 않는 CAS no-op으로 기록한다.
- **R1.5 (Outbox 안전성)**: `embedding_outbox`는 active job 중복을 막는 unique index, `FOR UPDATE SKIP LOCKED`, `claimed_at`, `lease_until`, `worker_id`, `retry_count`, dead-letter를 사용한다. 임베딩 생성 책임자는 Embedding Worker 하나로 고정한다.
- **R1.6 (pgai 범위)**: `pgai`는 첫 cutover의 임베딩 owner나 숨은 비동기 실행기가 아니다. 필요하면 별도 ADR과 성능·재현성 검증 뒤 도입하며, 첫 cutover에서는 Embedding Worker의 명시적 호출만 허용한다.
- **R1.7 (Temporal read 의미 명확화)**: `as_of`가 없으면 `currentness=current`만 반환한다. 명시적 `as_of`에서는 해당 시점의 validity를 만족하는 `current` 또는 `superseded` 기록을 허용한다. 두 경우 모두 **현재** `authorization_status=active`와 accepted lifecycle을 요구한다. `stale`, `conflicted`, `unknown`, `disabled`는 공개 과거 조회에서도 제외한다. 과거 권한을 재구성하거나 현재의 권한 회수를 우회하는 기능이 아니다.

### 2.2. R2 — Graphiti와 Neo4j 유지 및 Graph-first 조회

- **R2.1 (구성 유지)**: Graphiti와 Neo4j를 퇴역시키지 않는다. Graphiti는 비정형 episode에서 엔티티, 관계, temporal fact, provenance를 추출·검색하는 애플리케이션 계층이고, Neo4j는 그 결과를 저장·탐색하는 그래프 DB이다.
- **R2.2 (Graph-first query)**: `brain.resolve(mode="query")`의 목표 실행 순서는 Graphiti adapter → Neo4j 검색 → PostgreSQL 권위 join → slim serializer이다. `PgVectorStore.hybrid_search()`는 그래프가 아직 투영되지 않았거나 graph plane이 명시적으로 degraded일 때만 fallback으로 사용한다.
- **R2.3 (Hot/Cold 분리)**: MCP hot path는 Graphiti의 LLM entity extraction을 기다리지 않는다. episode 수신과 권위 write는 빠르게 끝내고, Graphiti extraction과 Neo4j projection은 비동기 cold worker가 수행한다. 이미 투영된 Neo4j graph read는 hot path에서 허용한다.
- **R2.4 (단방향 파생)**: write flow는 `PostgreSQL transaction → graph_projection_outbox → Graph Projection Worker → Graphiti → Neo4j`이다. PostgreSQL commit과 Neo4j write 사이에는 분산 ACID를 주장하지 않고 projection lag를 관찰한다.
- **R2.5 (두 종류의 관계)**: PostgreSQL `memory_edges`는 승인된 명시적 관계와 법적·운영적 lineage를 보존한다. Graphiti/Neo4j의 inferred entity/relationship graph는 재생성 가능한 파생 색인이다. 양쪽의 edge를 조용히 합치지 말고 provenance와 source revision을 구분한다.
- **R2.6 (장애 의미와 공개 상태 계약)**: 공개 응답의 `graph_status`는 `available`, `degraded`, `unavailable`, `projection_lag` 중 하나로 고정한다. 내부 예외 문자열(`error`)을 외부 상태로 그대로 노출하지 않는다. `retrieval_path`는 `graph_neo4j`, `pgvector_fallback`, `none` 중 하나이고, `authority_join_status`는 `verified`, `mismatch`, `unavailable` 중 하나이다. 지연은 `projection_lag_ms`(0 이상의 정수 또는 `null`)로만 표현한다. PG fallback을 선택하면 `fallback_used=true`를 반환하며 Qdrant로 자동 우회하지 않는다.
- **R2.7 (버전 고정)**: Graphiti는 부동 범위(`>=`)만으로 최신화를 주장하지 않는다. 호환성 검증을 통과한 정확한 `graphiti-core` lock version과 Neo4j image/major version을 함께 고정하고, 업그레이드 때 Graphiti schema/index/retrieval 회귀를 검증한다.

### 2.3. R3 — MCP 2-Tier와 Slim Payload

- **R3.1 (Agent public surface)**: Agent 공개 도구는 두 개로 고정한다.
  - `brain.resolve(query, mode="list"|"context"|"query", project, response_mode, as_of)` — 단일 읽기 라우터
  - `memory_candidate_create` — project-scoped proposal-only 쓰기
- **R3.2 (검색 경로 공개)**: `brain.resolve(mode="query")`는 결과 metadata에 R2.6의 `retrieval_path`, `graph_status`, `authority_join_status`, `fallback_used`, `projection_lag_ms`를 포함한다. 호출자가 그래프 결과를 PG 결과로 오해할 수 없어야 한다.
- **R3.3 (Slim 기본값)**: `response_mode="slim"`은 기본값이며 결정, 선호, 현재 task, 활성 guardrail과 최소 provenance만 반환한다. soft budget은 약 250~500 tokens, 목표 크기는 약 1.2KB, hard max는 3KB이다. 초과하면 deterministic truncation, stable pagination token, `has_more=true`를 반환한다.
- **R3.4 (Evidence 선택 모드)**: `response_mode="with_evidence"`에서만 evidence hash chain, edge 요약, authority join 세부사항을 확장한다. 원문 transcript나 private path를 Agent payload에 넣지 않는다.
- **R3.5 (Admin 격리)**: approve/reject/supersede/stale commit, 감사 probe, corpus 관리 작업은 `agent_memory_admin`의 별도 인증·endpoint·process에서만 수행한다. Agent key는 승인 권한을 가질 수 없다.

### 2.4. R4 — 운영·배포 범위

- **R4.1 (Thin client)**: Client PC는 Neo4j, PostgreSQL, Graphiti를 필수 설치하지 않는다. 기본 배포는 중앙 brain endpoint를 사용하고, 개발용 local graph profile은 선택 사항으로만 둔다.
- **R4.2 (재구축 가능성)**: Neo4j graph는 PostgreSQL 권위 데이터와 원본 episode reference로 재투영할 수 있어야 한다. projection cursor, source revision, 실패·재시도 상태를 durable하게 남긴다.
- **R4.3 (범위 제외)**: AST/Graphify는 제품 메모리 검색·projection·정합성 경로에 사용하지 않는다. 코드 구조 graph가 필요해지는 경우 별도 요구사항과 별도 저장·수명주기 결정을 만든다.

---

## 3. Qdrant 이관 및 cutover 요구사항

1. **Preflight**: 각 Qdrant collection의 실제 `dimension`, distance, model, point 수, payload schema, 삭제·중복 상태를 운영 read-only probe로 확인한다. 문서의 `3072` 기본값만으로 기존 데이터 차원을 추정하지 않는다.
2. **Vector migration**: source vector가 `gemini-embedding-2 / 3072 / cosine`과 일치하면 PostgreSQL `halfvec(3072)` fallback 색인으로 복사할 수 있다. 차원이 다르면 zero-padding이나 임의 projection으로 섞지 말고 quarantine 후 동일 profile로 re-embed한다.
3. **Graph migration**: Qdrant point를 PG로 복사하는 것만으로 Graphiti의 entity, relation, temporal validity, provenance가 복원되지 않는다. 원본 episode 또는 권위 card를 replay하여 Graphiti가 Neo4j graph를 다시 만든다.
4. **Dual-read shadow**: 실제 PostgreSQL과 실제 Qdrant를 비교하되, vector Recall@5뿐 아니라 Neo4j relation/temporal fact correctness, PG authority false-positive rate, p50/p95 latency, projection lag, fallback rate를 함께 기록한다. cutover 판정은 실제 양쪽 backend, 최소 50개 query fixture, 동일한 10분 관찰 창을 사용한다. `mean Recall@5 >= 0.95`, relation/temporal correctness `>= 0.95`, backend error `0건`, PG p95 `<= 20ms`를 기본 gate로 삼고, false-positive와 fallback rate는 측정값과 승인된 상한을 함께 기록한다. 실행 결과에는 `evidence_class`를 `test_harness` 또는 `live_cutover`로 명시하며, `live_cutover`는 실제 backend preflight를 통과해야 한다. Mock client, dummy vector, Python dict scan은 cutover 증거로 인정하지 않는다.
5. **Cutover**: Graph-first query의 품질·지연·권위 join·장애 표기가 모두 통과한 뒤에만 공개 read path를 전환한다. 전환 전까지 Qdrant는 rollback/shadow용으로 보존하되, 정상 Agent 조회의 기본 경로로 남겨두지 않는다.

---

## 4. Acceptance Criteria

### AC1. 데이터·임베딩 계약

- [ ] 신규 schema가 `gemini-embedding-2`, `halfvec(3072)`, `halfvec_cosine_ops`를 사용한다.
- [ ] 카드와 세션 청크의 vector/query/worker/migration dimension이 하나의 shared constant와 profile을 따른다.
- [ ] `pgvector/pgvector:pg17` 또는 동등하게 검증된 image에서 `pgvector >= 0.8.0`, extension 설치/활성화, HNSW, `hnsw.iterative_scan` 설정을 실제 PostgreSQL로 검증한다.
- [ ] 기존 Qdrant collection의 실제 차원·distance·model preflight 결과와 quarantine/re-embed 결과가 남는다.

### AC2. 정합성과 graph read

- [ ] PgVectorStore의 CRUD·fallback search는 실제 `psycopg` SQL이며 DB 실패를 in-memory fallback으로 숨기지 않는다.
- [ ] 양쪽 CAS write-back과 outbox lease/idempotency가 실제 DB concurrency test에서 확인된다. worker_id와 활성 lease를 잃은 작업은 write-back하지 않으며, hash 불일치는 `cas_skipped` terminal state로 관찰된다.
- [ ] `brain.resolve(mode="query")`가 Graphiti/Neo4j를 먼저 조회하고 PostgreSQL authority join을 수행한다.
- [ ] graph 장애나 미투영 상태에서만 PG fallback을 사용하고, `retrieval_path`와 `fallback_used`를 명시한다.
- [ ] Graphiti LLM extraction은 async/bulk cold lane에서만 실행되며 Neo4j graph read는 hot path에서 가능하다.

### AC3. MCP와 운영

- [ ] Agent public tool은 `brain.resolve`와 `memory_candidate_create`만 노출되고 admin mutation은 별도 control plane에 있다.
- [ ] 기본 slim 응답이 목표 1.2KB에 가깝고 hard max 3KB를 넘지 않으며 pagination이 deterministic하다.
- [ ] graph projection replay로 Neo4j를 재구축할 수 있고 PG graph outbox의 projection lag/dead-letter를 관찰할 수 있다. 현재 SQLite ledger-backed trigger는 이 기준을 충족하는 최종 경로가 아니다.
- [ ] AST/Graphify는 product runtime dependency가 아니다.
- [ ] 전체 worker 테스트는 동작 회귀를 검증하되, 테스트 개수 자체를 품질 지표로 사용하지 않는다. obsolete mock/in-memory 테스트는 별도 inventory에서 제거·축소하며 실패를 숨기기 위해 skip/xfail을 추가하지 않는다.

### AC4. 현재 구현과의 경계

- [ ] 문서·기본값 정합화만으로 운영 완료를 주장하지 않는다. 공개 `brain.resolve`의 로컬 Graph-first 라우터 검증과 달리, legacy `KnowledgeSearchService.brain_query`, PG graph outbox writer/consumer, live PostgreSQL/Neo4j benchmark는 후속 milestone의 별도 증거가 필요하다.
