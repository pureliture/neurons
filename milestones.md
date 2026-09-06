# LBrain Architecture Rationalization Implementation Milestones

- Requirements: [requirements.md](specs/lbrain-architecture-rationalization/requirements.md) v2.5
- Design: [design.md](specs/lbrain-architecture-rationalization/design.md) v2.5
- Audit history: [review.md](specs/lbrain-architecture-rationalization/review.md)
- Selected contract: `agentic-execution`
- Target branch: `codex/lbrain-architecture-rationalization-spec`
- Target commit at start: `a117a786efd24355242f3288e5b7e9313e57a16e`

## Execution boundary

- Implement code and repository-level tests only.
- Do not deploy, mutate a live database, change Docker/systemd/GitOps state, or perform production MCP writes.
- Keep the existing public contract. The full worker regression suite is a separate
  gate because legacy fixtures still assume in-memory storage and 1536 dimensions;
  do not report it green until that inventory is reconciled.
- Keep exactly one implementation slice active at a time.

## Milestones

| Milestone | Observable vertical slice | Status | Evidence |
|---|---|---|---|
| M1 | `memory_candidate_create` persists an authoritative candidate through real `psycopg` SQL and atomically creates its embedding outbox row; the same data can be read back | complete | Disposable PostgreSQL 17 + `pgvector 0.8.6`; `LBRAIN_TEST_PG_DSN=local`; `uv run pytest -q tests/test_lbrain_pgvector_contract.py -rs` → 14 passed, 1 warning (2026-09-05) |
| M2 | 실제 SQL lease와 dual-CAS로 카드·청크 임베딩을 갱신하고 stale 작업을 차단 | complete | 임시 PostgreSQL 17.11 / pgvector 0.8.6에서 contract 20 passed; system_architecture_manager 재검토 PASS |
| M3 | Graphiti 후보 → PG 권위 join → 명시적 PG fallback → bounded Slim 공개 조회·HTTP 접근 경계 | complete | 실제 PG·adapter 변환·공개 HTTP·Unicode 107건 페이지 검증 포함 158 passed, 1 기존 live Neo4j opt-in skip; Sol 독립 재검토 PASS |
| M4 | 실제 SQL의 명시적 edge·temporal traversal·bounded evidence 응답에서 순환 탐색과 권한 우회를 방지 | complete | 170 passed, 1 live Neo4j opt-in skip; Sol 독립 리뷰 PASS_WITH_GAPS. 동일 응답의 decision·edge·cursor·3072바이트 보완 검증 5 passed (2026-09-06). live Neo4j는 별도 gate |
| M5 | Qdrant→PG 전환 패키지: (a) Qdrant preflight + `halfvec(3072)` vector migration/quarantine/re-embed, (b) episode/card→Graphiti→Neo4j graph replay, (c) dual-read shadow live gate, (d) cutover gate (mock 증거로 통과 금지) | complete | M5a 6 passed, M5b 6 passed, M5c gate logic 6 passed + live 2 skipped, M5d 런북 확정. live 실측·Qdrant 제거는 Atlas 소유 |
| M6 | Admin Control Plane boundary is fail-closed, legacy compatibility is reconciled, and the full worker suite is green | pending | pending |
| M7 | Graph-first cutover + Qdrant retirement: PG authority transaction writes `graph_projection_outbox`, a leased worker projects through Graphiti to Neo4j, public `brain.resolve` performs Graphiti candidate → authority join → PG fallback, Qdrant는 rollback window 후 shadow-only/제거 결정 | blocked pending implementation | documented in requirements/design/review. Qdrant 제거는 live gate 통과 + Atlas 실행으로만 가능 |

## M5 breakdown — Qdrant→pgvector 전환 추적 (Requirements §3 / Design §8 매핑)

- [x] M5a vector migration: Qdrant collection 실측 preflight(dimension/distance/model/point 수/payload schema/삭제·중복) → profile 일치분만 PG `halfvec(3072)` 복사, 불일치분 quarantine + 동일 profile re-embed. `vector(1536)` cast/zero-padding 금지. (2026-09-06 `tests/test_migration_qdrant_to_postgres.py` 6 passed, migrator 본체 무변경, `git diff --check` 통과)
- [x] M5b graph replay: Qdrant point 복사가 아닌 원본 episode/권위 card replay로 Neo4j entity/relation/temporal/provenance 재생성. cursor/source revision/lag/실패·retry를 durable checkpoint에 기록. (2026-09-06 신규 `graph_replay.py`+`test_graph_replay.py` 6 passed, 기존 파이프라인 무수정, FILE checkpoint 명시 선택)
- [x] M5c dual-read shadow: 실제 PG + 실제 Qdrant + Neo4j relation/temporal correctness를 동일 10분 관찰창·최소 50 queries로 비교. `mean Recall@5 ≥ 0.95`, relation/temporal `≥ 0.95`, backend error `0건`, PG p95 `≤ 20ms`, false-positive/fallback 상한 기록. `evidence_class=test_harness|live_cutover` 분리, mock/dummy/dict-scan은 cutover 증거 불가. (2026-09-06 gate logic 6 passed + live 2 skipped, `overall_gate_passed`는 live 증거 없이 불가, `git diff --check` 통과)
- [x] M5d cutover gate: graph 품질·authority join·fallback 표기·hot-path 비차단·latency/lag 관찰이 모두 통과해야 `overall_gate_passed=true`. 미통과 시 Qdrant를 정상 경로로 복귀시키지 않고 rollback window 보존. (2026-09-06 컷오버 런북 확정, 코드는 `overall_gate_passed`를 live 증거 없이 불가로 강제하므로 추가 코드 변경 없음)
  - 컷오버 전제: M5a preflight 통과 + M5b replay 완료 + M5c live gate(`live_cutover`, 50 queries, 10분창, error 0, p95·Recall·graph·rate 상한) 전부 통과.
  - 실행 순서: shadow 유지 → `brain.resolve` 전환 → rollback window(기간은 Atlas가 운영 정책으로 확정) → Qdrant shadow-only → 제거 결정(M7).
  - 롤백 기준: 게이트 하나라도 미통과·관찰 중 error/lag 상한 초과 시 전환 중단, Qdrant를 정상 경로로 복귀시키지 않음. 실패 원인은 `cutover_blockers`에 기록.
  - Atlas 인계: live 10분 실측, backend preflight 검증, graph 관찰 수집기(M7) 연결, rollback window 운영, Qdrant 제거 실행.
- [ ] Qdrant retirement (M7에 귀속): public `brain.resolve`에서 Qdrant 호출 `0건` 확인 후 rollback window 유지 → shadow-only → 제거. 제거 실행은 Atlas 소유 (운영 DB/배포 변경).

## Active slice record

- Active slice: M6 (M1/M2/M3/M4/M5 closed)
- Required result: 이관의 profile/권위 검증과 비식별 실패 보고, 동일 조건의 shadow 비교를 보장하고 불충분한 증거로 cutover를 통과시키지 않는다.
- Current blocker: 로컬 구현 차단 없음. 지정 system_architecture_manager 고정 GPT-5.5 사용량 제한으로 역할 지침을 읽는 Sol 검토자로 대체했다. PostgreSQL/Neo4j 운영 검증과 배포는 별도 권한 범위다.
- Amendment decisions: M2 설계 보정 — accepted 카드에는 권위 필드를 건드리지 않는 embedding-only CAS만 허용한다. 따라서 검색 가능한 accepted 카드의 최초 임베딩도 막지 않으면서, worker가 lifecycle·authorization·currentness·content hash를 덮어쓰지 않는 경계를 명시한다.
- M3 보정: GraphFact inferred hash와 원본 카드 hash를 분리하고 authority_sources를 PG join에 사용한다. 정상 graph 후보는 미투영 시에도 보존한다. 실제 MCP tool-result의 중복·escape·cursor까지 3KB에 포함하며, 목록은 SQL keyset으로 100건 이후도 읽는다. Pydantic 입력 검증과 MCP/HTTP 공통 오류 표시를 재사용한다.
- M4 보정: explicit as_of에서만 과거 유효한 superseded 기록을 허용하되 현재 권한은 유지한다(Sol 요구 보존형 정정 검토). edge INSERT의 SQL 인자 오류와 실제 근거가 모두 잘리는 응답을 재현·수정했다. 근거는 PG의 같은 statement snapshot(root hash + edges), Pydantic 경로/hash 검증, 한 번의 batch 조회, compact edge/hash 표현을 재사용한다. 신규 production queue/store는 없다.
- Next action: M6(Admin fail-closed + legacy reconciliation + full suite green) 진입 전 범위 확정 필요. M6는 규모가 크므로 sub-slice 분해 후 진행. M7 graph projection writer/consumer 구현도 별도 슬라이스. live 실측·Qdrant 제거는 Atlas 소유.
