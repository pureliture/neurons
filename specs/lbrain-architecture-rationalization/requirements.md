# LBrain Architecture Rationalization: Requirements & Problem Statement (v2)

- **Status**: Revised Draft / Approved for Design
- **Date**: 2026-09-01
- **Target Repository**: `neurons` (Server/Brain Authority)
- **Review Status**: Peer Review Addressed (Revise Before Accept ➡️ Fully Addressed)

---

## 1. Executive Summary (Reframed)

`LBrain` (LLM-Brain) 시스템의 아키텍처 개편 목표는 "수만 건 규모의 벡터 검색 성능 최적화"가 아니라, **"운영 표면 축소(Operational Surface Minimization)", "동일 엔진 트랜잭션 일관성(ACID/Dual-Write Elimination)", "직렬화 비대화 해소(Slim Payload)"**이다.

Live 런타임 실측 결과, 현재 도메인 규모는 권위 카드 8개, 세션 아티팩트 69개 수준이며, `brain_context_resolve` 호출 1회당 **68.5KB(약 17,000 토큰)**에 달하는 극심한 직렬화 오버헤드가 발생하고 있다.

본 RFC는 다음 세 가지 핵심 방향으로 시스템을 정격화(Rationalize)한다:
1. **Qdrant ➡️ PostgreSQL (`pgvector`) 수렴 (F/O)**: 메타데이터 필터와 벡터 검색을 단일 PostgreSQL 트랜잭션 내에서 원자적으로 처리하고, 엔진 운영/백업/모니터링을 1개로 통합한다.
2. **Graphiti / Neo4j 2-Track 전략 (Hot-path 분리 & Cold-path 보존)**: 
   - 실시간 에이전트 질의 경로(Hot-path)에서는 고비용 LLM 엔티티 추출을 차단하여 지연/비용을 없앤다.
   - 복합 관계 추론 및 다대다 DAG, 시간축(Temporal fact `valid_from/to`) 보존을 위해 기존 Neo4j/Graphiti 스택을 Out-of-band Workbench/Projection(Cold-path)으로 유지한다 (직접 구현 회피).
   - PostgreSQL 내부에도 다대다 간선 테이블(`memory_edges`)을 두어 RDBMS 레벨에서도 기본 DAG를 지원한다.
3. **MCP 2-Tier 분리 & 티어드 슬림 직렬화 (Tiered Slim Serializer)**:
   - 에이전트 도구를 2개(`brain.resolve(mode=list|context|query)`, `memory_candidate_create`)로 통합.
   - 1KB 하드캡 대신 **티어드 모델(Slim 기본 + `with_evidence` opt-in)**을 도입하여 필수 결정/증거 해시 손실 없이 페이로드를 90% 이상 절감한다.

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
- **R1.1 (운영 수렴)**: Qdrant 전용 클러스터 대신 기존 PostgreSQL 인스턴스의 `pgvector` 확장을 활용하여 백업, PITR, 트랜잭션 관리를 단일 엔진으로 수렴한다.
- **R1.2 (다차원/다모델 지원)**: 임베딩 차원을 `vector(1536)`으로 하드코딩하지 않고, `embedding_model`, `embedding_dimension`, `embedding_revision` 메타데이터를 관리하여 모델 전환 시 무중단 마이그레이션을 보장한다.
- **R1.3 (Transactional Outbox & State)**: 카드 변경과 임베딩 생성 작업을 Transactional Outbox 패턴으로 묶고, `embedding_state` (`pending` | `ready` | `stale` | `failed`)와 `content_hash`가 일치하는 카드만 검색에 노출한다.
- **R1.4 (Dual-Read Shadow Gate)**: Qdrant를 즉시 삭제하지 않고, `Phase 2.5`에서 Qdrant와 pgvector 간 Recall@k 및 P95/P99 지연시간을 벤치마크 검증한 후 컷오버한다.

### 3.2. Domain 2: Graphiti & Neo4j Strategy (Hot/Cold Separation)
- **R2.1 (Hot-path 완전 격리)**: 실시간 대화 수집 및 MCP 응답 루프에서 Graphiti의 실시간 LLM 엔티티 추출을 차단하여 레이턴시(300초 타임아웃)와 API 비용을 0으로 만든다.
- **R2.2 (Cold-path Workbench 보존)**: 에이전트 메모리의 복합 관계, 다단계 인과관계, temporal fact 구간 탐색을 위해 기성 Neo4j/Graphiti 파이프라인을 비동기/배치 워크벤치로 유지한다 (직접 RDBMS에 복잡한 그래프 엔진을 구현하는 운영 부담 회피).
- **R2.3 (RDBMS 다대다 DAG 지원)**: 단일 포인터(`supersedes`) 한계를 극복하기 위해 PostgreSQL에 `memory_edges` 테이블을 두어 다중 대체, 근거 분기, `valid_from/to` 구간을 보존한다.

### 3.3. Domain 3: MCP 2-Tier & Tiered Slim Serializer
- **R3.1 (Agent Public Surface 2개화)**:
  - `brain.resolve(query, mode="list"|"context"|"query", project, response_mode)`: 단일 통합 읽기 도구.
  - `memory_candidate_create`: 제안 전용 쓰기 도구 (Proposal-only, rate-limited, project-scoped, ledger write 직접 불가).
- **R3.2 (Tiered Slim Payload)**:
  - `response_mode="slim"` (기본): 결정, 선호도, 현재 태스크, 활성 가드레일을 1~2KB(Soft Token Budget: 250~500 토큰)로 압축 제공.
  - `response_mode="with_evidence"` (선택): 증거 해시 체인(`evidence_hashes`), 다단계 엣지(`edges`), 상세 페이로드 포함.
- **R3.3 (Admin Control Plane 격리)**: `memory_candidate_approve`, `memory_supersede_commit`, 감사 프로브 등은 별도의 `agent_memory_admin` 서비스 키 및 엔드포인트로 물리적 격리한다.

---

## 4. Acceptance Criteria (수용 기준)

### AC1. Storage & Verification
- [ ] PostgreSQL 단일 DB에서 메타데이터 필터링 + 벡터 유사도 검색이 트랜잭션 격리 하에 수행되어야 한다.
- [ ] 고정 벤치마크 Fixture Corpus에 대해 `Recall@5 >= 0.95` 및 `P95 Latency <= 20ms`가 입증되어야 한다.

### AC2. Graph Integrity & DAG Support
- [ ] 1개 카드가 여러 카드를 대체하거나(수렴), 여러 근거에 의해 폐기되는(분기) 다대다 DAG가 `memory_edges`에 기록될 수 있어야 한다.
- [ ] `as_of` 및 `date_from/date_to` 기반의 Temporal Recall 쿼리가 정상 동작해야 한다.

### AC3. MCP Surface & Security
- [ ] 에이전트 노출 MCP 툴은 정확히 2개(`brain.resolve`, `memory_candidate_create`)여야 한다.
- [ ] `memory_candidate_create`는 오직 `candidate/disabled` 상태로만 레코드를 생성하며, 즉시 승인 쓰기를 시도할 경우 Fail-closed 거부되어야 한다.
- [ ] 기본 `slim` 모드 응답 크기는 2KB 이하(P95 기준)여야 하며, `current_task`의 3중 중복 객체 및 빈 lanes가 완전히 제거되어야 한다.

---

## 5. Review Decisions Summary

| 검토 항목 | 최종 판정 | 확정된 설계 방향 |
|---|---|---|
| **pgvector vs Qdrant** | **조건부 채택 (F/O 진행)** | 성능 우위가 아닌 **운영 1개화 & 트랜잭션 일관성**으로 프레이밍. Dual-Read 섀도우 검증 후 컷오버. |
| **Neo4j / Graphiti** | **2-Track 보존 (Hot/Cold 분리)** | 완전 삭제 취소. **실시간 Hot-path는 차단**하고, **Out-of-band 워크벤치는 기성 스택 유지** (직접 구현 회피). |
| **MCP 도구 & 직렬화** | **전면 채택 & 티어드 보강** | 읽기 도구 단일화(`brain.resolve`), **티어드 슬림 모델(Slim + with_evidence)**로 증거 보존. |
