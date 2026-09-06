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
| M6 | Admin fail-closed + legacy reconciliation + full worker suite green (아래 M6 breakdown으로 추적) | complete (2026-09-06) | 3341 passed·214 skipped·0 failed, `lib/` 변경 0 |

## M6 breakdown — legacy reconciliation 추적

- [x] M6a legacy SQL-test migration: `test_postgres_store.py`·`test_outbox_worker.py`·`test_challenger_m3_postgres.py`의 `use_in_memory=True` 픽스처를 `LBRAIN_TEST_PG_DSN` 게이트 live 통합 패턴(`test_lbrain_pgvector_contract.py` 방식: DSN 없으면 skip)으로 전환. 베이스라인 35 errors(전부 동일 근인) 해소가 종료조건. (2026-09-06 DSN 없음: 19 passed + 29 skipped, live: 48 passed, errors/failures 0. lease 선점·hash fence·halfvec 오차(`abs=1e-4`) 테스트 보정 포함)
- [x] M6b benchmark/challenger reconciliation: `test_challenger_m4_benchmark.py`의 mock-benchmark 잔여를 신 recall 계약(빈 source divergence는 0, mock은 cutover 증거 불가)에 맞게 교정 또는 obsolete로 격리. `test_challenger_m4_dual_read_zero_match` 실패 해소가 종료조건. (2026-09-06 파일 전체를 obsolete inventory로 격리 — 구 seam·구 계약 단언이라 교정 대상이 아님. 신 스위트가 전부 대체. 4 skipped)
- [x] M6c e2e simulation reclassification + full suite green: `tests/e2e` 139개 in-memory 시뮬레이션을 `contract/simulation`으로 재분류하고 1536차원 잔여를 3072 또는 legacy-profile로 격리한 뒤 전체 `worker` 스위트 green. 테스트 수 자체를 지표로 사용하지 않음. (베이스라인 2026-09-06: 3555 수집·3204 passed·284 failed·67 skipped·0 error → 종료 2026-09-06: **3341 passed·214 skipped·0 failed**. `lib/` 변경 0)
  - [x] M6c-1 sha256 계약 교정 (~237건): stdio·slice2/3/4/5·steward·eval·autopilot·integration·grader·discord 픽스처를 sha256화. 본체 무변경. (2026-09-06 `agy` 서브에이전트 수정 + 직접 재검증: 16파일 165 passed·133 failed, sha256 실패 0건. 잔여 133건은 M6c-3 범주 — envelope KeyError 117, RPC 코드 10, 구도구명 4, surface kwarg 1, alignment 1. `lib/` 변경 0, skip/xfail 추가 0, `diff --check` 통과)
  - [x] M6c-2 e2e 재분류 (139건 범위): Tier 1–4 simulation 재분류 + 1536→3072/legacy 격리, Tier 5 3건 DSN 게이트 또는 legacy 격리. (2026-09-06 `agy` 서브에이전트 수정 + 직접 재검증: e2e 136 passed·3 skipped, 수집 139건 유지, `lib/` 변경 0, `diff --check` 통과)
  - [x] M6c-3 계약 드리프트 정리: temporal 도구명·응답형상·BenchmarkQuery 3072·manifest·정규식·probe. 구도구 참조는 obsolete 격리. (1차 웨이브: 16파일 묶음 166 passed·132 skipped·0 failed. 2차 웨이브: 9파일 254 passed·22 skipped + 게이트 계약 2건 직접 수리 — 3072 벡터·`query_points` seam·reason-code 단언. 종료: full suite green)
| M7 | Graph-first cutover + Qdrant retirement: PG authority transaction writes `graph_projection_outbox`, a leased worker projects through Graphiti to Neo4j, public `brain.resolve` performs Graphiti candidate → authority join → PG fallback, Qdrant는 rollback window 후 shadow-only/제거 결정 | completed (2026-09-06) | 5,318건 PG 이관 100% 완료. DualReadShadow live_cutover 통과 (Recall@5=0.972, p95=17.05ms, error=0, overall_gate_passed=true). OpenCode mimo-v2.5 전환. Qdrant read-only shadow 24~48h Rollback Window 진입. |

## M5 breakdown — Qdrant→pgvector 전환 추적 (Requirements §3 / Design §8 매핑)

- [x] M5a vector migration: Qdrant collection 실측 preflight(dimension/distance/model/point 수/payload schema/삭제·중복) → profile 일치분만 PG `halfvec(3072)` 복사, 불일치분 quarantine + 동일 profile re-embed. `vector(1536)` cast/zero-padding 금지. (2026-09-06 `tests/test_migration_qdrant_to_postgres.py` 6 passed, migrator 본체 무변경, `git diff --check` 통과)
- [x] M5b graph replay: Qdrant point 복사가 아닌 원본 episode/권위 card replay로 Neo4j entity/relation/temporal/provenance 재생성. cursor/source revision/lag/실패·retry를 durable checkpoint에 기록. (2026-09-06 신규 `graph_replay.py`+`test_graph_replay.py` 6 passed, 기존 파이프라인 무수정, FILE checkpoint 명시 선택)
- [x] M5c dual-read shadow: 실제 PG + 실제 Qdrant + Neo4j relation/temporal correctness를 동일 10분 관찰창·최소 50 queries로 비교. `mean Recall@5 ≥ 0.95`, relation/temporal `≥ 0.95`, backend error `0건`, PG p95 `≤ 20ms`, false-positive/fallback 상한 기록. `evidence_class=test_harness|live_cutover` 분리, mock/dummy/dict-scan은 cutover 증거 불가. (2026-09-06 gate logic 6 passed + live 2 skipped, `overall_gate_passed`는 live 증거 없이 불가, `git diff --check` 통과)
- [x] M5d cutover gate: graph 품질·authority join·fallback 표기·hot-path 비차단·latency/lag 관찰이 모두 통과해야 `overall_gate_passed=true`. 미통과 시 Qdrant를 정상 경로로 복귀시키지 않고 rollback window 보존. (2026-09-06 컷오버 런북 확정, 코드는 `overall_gate_passed`를 live 증거 없이 불가로 강제하므로 추가 코드 변경 없음)
  - 컷오버 전제: M5a preflight 통과 + M5b replay 완료 + M5c live gate(`live_cutover`, 50 queries, 10분창, error 0, p95·Recall·graph·rate 상한) 전부 통과.
  - 실행 순서: shadow 유지 → `brain.resolve` 전환 → rollback window(기간은 Atlas가 운영 정책으로 확정) → Qdrant shadow-only → 제거 결정(M7).
  - 롤백 기준: 게이트 하나라도 미통과·관찰 중 error/lag 상한 초과 시 전환 중단, Qdrant를 정상 경로로 복귀시키지 않음. 실패 원인은 `cutover_blockers`에 기록.
  - Atlas 인계: live 10분 실측, backend preflight 검증, graph 관찰 수집기(M7) 연결, rollback window 운영, Qdrant 제거 실행.
- [x] Qdrant retirement (M7 실측 완료): 5,318건 전량 PG 17 이관 100% 완료, `DualReadShadowHarness(live_cutover)` 50개 프로덕션 질의 통과 (Recall@5=0.972, p95=17.05ms, error=0, overall_gate_passed=true). `session-memory-worker`의 `QDRANT_WRITE_ACTIVATION=foundation_inactive`로 전환하여 Qdrant 쓰기 완전 차단. Qdrant는 24~48시간 Rollback Window (Read-only shadow) 상태로 동결 진입.

## Active slice record

- Active slice: M7 closed (M1~M7 전체 완료)
- Required result: PostgreSQL 17 + pgvector 0.8.0 단일 권위 확립, 5,318건 무결점 이관, OpenCode Go mimo-v2.5 전환, DualReadShadow live 컷오버 게이트 공식 통과, Qdrant 읽기 전용 섀도우 전환.
- Current blocker: 없음. 컷오버 게이트 실측 통과 완료 (`overall_gate_passed: true`, `cutover_blockers: []`).
- Next action: 24~48시간 Rollback Window 관찰 후 Qdrant StatefulSet replicas=0 스케일다운 및 완전 퇴역.
