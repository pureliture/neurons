# LBrain Architecture Rationalization: Requirements & Problem Statement (v2.2)

- **Status**: Formally Approved for Implementation (Clean PASS)
- **Date**: 2026-09-01
- **Target Repository**: `neurons` (Server/Brain Authority)
- **Review Status**: Round 1 & Round 2 Peer Reviews Fully Addressed (`review.md` 참조)

---

## 1. Executive Summary (Precision Reframed)

`LBrain` (LLM-Brain) 시스템의 아키텍처 개편 목표는 "수만 건 규모의 벡터 검색 성능 최적화"가 아니라, **"운영 표면 축소(Operational Surface Minimization)", "동일 엔진 트랜잭션 일관성(ACID/Dual-Write Elimination)", "직렬화 비대화 해소(Slim Payload)"**이다.

Live 런타임 실측 결과, 현재 도메인 규모는 권위 카드 8개, 세션 아티팩트 69개 수준이며, `brain_context_resolve` 호출 1회당 **68.5KB(약 17,000 토큰)**에 달하는 극심한 직렬화 오버헤드가 발생하고 있다.

본 RFC는 다음 세 가지 핵심 방향으로 시스템을 정격화(Rationalize)한다:
1. **Hot-Path 스토리지 수렴 (PostgreSQL 17+ / `pgvector >= 0.8.0`)**: 메타데이터 필터, 트랜잭셔널 아웃박스, 다대다 관계 간선, 벡터 유사도 검색을 단일 PostgreSQL 엔진 내에서 처리하여 Qdrant 이중 쓰기 불일치 및 별도 클러스터 운영 오버헤드를 제거한다.
2. **Graphiti / Neo4j 2-Track 전략 (Hot-path 차단 & Cold-path 단방향 파생)**: 
   - 실시간 에이전트 질의 경로(Hot-path)에서는 고비용 LLM 엔티티 추출을 차단하여 지연(300초 타임아웃)과 비용을 0으로 만든다.
   - 복합 관계 추론 및 온톨로지 워크벤치를 위해 기성 Neo4j/Graphiti 스택을 PostgreSQL로부터 비동기 단방향(1-way)으로 파생되는 Cold-Path 읽기 전용 인덱스로 유지한다 (직접 구현 회피).
   - PostgreSQL 내부에도 다대다 간선 테이블(`memory_edges`)을 두어 RDBMS 레벨에서도 완벽한 DAG 및 시간축(`valid_from/to`)을 지원한다.
3. **MCP 2-Tier 분리 & 티어드 슬림 직렬화 (Tiered Slim Serializer)**:
   - 에이전트 도구를 2개(`brain.resolve(mode=list|context|query)`, `memory_candidate_create`)로 통합.
   - 1KB 하드캡 대신 **티어드 모델(Slim 기본 ~1.2KB + `with_evidence` 상세 증거 opt-in)**을 도입하여 토큰을 95% 절감하면서도 결정론적 SHA-256 증거 체인을 보존한다.

---

## 2. Current State & Live Evidence (As-Is 실측)

| 측정 항목 | Live 실측 결과 | 아키텍처적 의미 |
|---|---|---|
| `brain_context_resolve` 페이로드 | **68.5 KB** (full) / **65.3 KB** (compact) | 빈 lanes 7개, route_spec, 3중 중복 객체로 인한 직렬화 비대화 |
| 권위 지식/세션 규모 | 권위 카드 8개, 세션 아티팩트 69개 | "대규모 벡터 검색 성능"이 아닌 "운영 단순화 & 일관성"이 핵심 |
| Graphiti / Neo4j 상태 | `status: degraded`, `edge_provenance_unresolved` | 실시간 Hot-path에서 실패 중이나 시간축/다중 엣지 모델은 유효 |
| MCP 노출 도구 수 | raw 정의 34개 (실제 active allowlist 12개) | 읽기 도구 중복 난립 및 관리자 승인 도구 노출로 라우팅 혼선 |

---

## 3. Core Requirements by Domain

### 3.1. Domain 1: PostgreSQL & pgvector (Vector Store Consolidation)
- **R1.1 (운영 수렴)**: Qdrant 전용 클러스터 대신 기존 PostgreSQL 인스턴스의 `pgvector >= 0.8.0` 확장을 활용하여 백업, PITR, 트랜잭션 관리를 단일 엔진으로 수렴한다.
- **R1.2 (임베딩 일관성 & CAS Write-Back)**: 카드 변경과 임베딩 생성 작업을 Transactional Outbox (`embedding_outbox`) 패턴으로 묶고, 워커가 임베딩을 반영할 때 반드시 큐 생성 시점의 `content_hash`와 일치할 때만 업데이트하는 **CAS (Compare-And-Swap)** 가드를 적용하여 스텔(Stale) 임베딩 덮어쓰기 레이스를 차단한다.
- **R1.3 (Outbox Lease & Idempotency)**: `embedding_outbox`에 중복 인큐 방지 유니크 인덱스를 두고, 다중 워커의 안전한 처리를 위해 `claimed_at`, `lease_until`, `worker_id`, `retry_count` 기반의 리스(Lease) 메커니즘을 적용한다.
- **R1.4 (Dual-Read Shadow Gate)**: Qdrant를 즉시 삭제하지 않고, `Phase 2.5`에서 Qdrant와 pgvector 간 Recall@5 >= 0.95 및 P95 Latency <= 20ms를 벤치마크 검증한 후 컷오버한다.

### 3.2. Domain 2: Graphiti & Neo4j Strategy (Hot/Cold Separation)
- **R2.1 (Hot-path 완전 격리)**: 실시간 대화 수집 및 MCP 응답 루프에서 Graphiti의 실시간 LLM 엔티티 추출을 차단하여 레이턴시(300초 타임아웃)와 API 비용을 0으로 만든다.
- **R2.2 (Cold-path 단방향 파생 워크벤치)**: 에이전트 메모리의 복합 관계, 다단계 인과관계, temporal fact 구간 탐색을 위해 기성 Neo4j/Graphiti 파이프라인을 PostgreSQL의 변경을 비동기로 수신하는 단방향(Eventual-consistent) 파생 워크벤치로 유지한다.
- **R2.3 (RDBMS 다대다 DAG 및 순환 방지)**: PostgreSQL에 `memory_edges` 테이블을 두어 다중 대체, 근거 분기, `valid_from/to` 구간을 보존하며, 재귀 조회 시 깊이 제한(`depth < 5`) 및 순환 방지(`CYCLE`) 가드를 적용한다.

### 3.3. Domain 3: MCP 2-Tier & Tiered Slim Serializer
- **R3.1 (Agent Public Surface 2개화)**:
  - `brain.resolve(query, mode="list"|"context"|"query", project, response_mode)`: 단일 통합 읽기 도구.
  - `memory_candidate_create`: 제안 전용 쓰기 도구 (Proposal-only, rate-limited, project-scoped, ledger write 직접 불가).
- **R3.2 (Tiered Slim Payload & Hard Limit)**:
  - `response_mode="slim"` (기본): 결정, 선호도, 현재 태스크, 활성 가드레일을 ~1.2KB(Soft Token Budget: 250~500 토큰, Hard Max: 3KB)로 압축 제공하며 초과 시 deterministic truncation 및 `has_more: true` 반환.
  - `response_mode="with_evidence"` (선택): 증거 해시 체인(`evidence_hashes`), 다단계 엣지(`edges`), 상세 페이로드 포함.
- **R3.3 (Admin Control Plane 물리 격리)**: `memory_candidate_approve`, `memory_supersede_commit`, 감사 프로브 등은 별도의 `agent_memory_admin` 서비스 키(`lbrain_admin`) 및 독립 엔드포인트/프로세스로 물리적 격리한다.

---

## 4. Acceptance Criteria (수용 기준)

### AC1. Storage & Verification
- [ ] PostgreSQL 단일 DB에서 메타데이터 필터링 + 벡터 유사도 검색이 트랜잭션 격리 하에 수행되어야 한다.
- [ ] `SET LOCAL hnsw.iterative_scan = 'relaxed_order'`가 `pgvector >= 0.8.0` 인스턴스에서 에러 없이 실행되어야 한다.
- [ ] Outbox 워커가 비동기 임베딩 반영 시 `content_hash` 불일치 건에 대해 업데이트를 건너뛰는(No-op) CAS 가드가 동작해야 한다.
- [ ] 고정 벤치마크 Fixture Corpus에 대해 `Recall@5 >= 0.95` 및 `P95 Latency <= 20ms`가 입증되어야 한다.

### AC2. Graph Integrity & DAG Support
- [ ] 1개 카드가 여러 카드를 대체하거나(수렴), 여러 근거에 의해 폐기되는(분기) 다대다 DAG가 `memory_edges`에 기록될 수 있어야 한다.
- [ ] `as_of` 및 `date_from/date_to` 기반의 Temporal Recall 쿼리가 순환 루프 없이 안전하게 수행되어야 한다.

### AC3. MCP Surface & Security
- [ ] 에이전트 노출 MCP 툴은 정확히 2개(`brain.resolve`, `memory_candidate_create`)여야 한다.
- [ ] `memory_candidate_create`는 오직 `candidate/disabled` 상태로만 레코드를 생성하며, 즉시 승인 쓰기를 시도할 경우 Fail-closed 거부되어야 한다.
- [ ] 기본 `slim` 모드 응답 크기는 2KB 이하(P95 기준)여야 하며, `current_task`의 3중 중복 객체 및 빈 lanes가 완전히 제거되어야 한다.
- [ ] `agent_memory_admin`은 전역 managed allowlist의 별도 항목으로 관리되며 에이전트 프로파일에 복제 노출되지 않아야 한다.
