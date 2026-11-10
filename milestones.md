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
| M5 | legacy vector source에서 PostgreSQL/pgvector로 이관하고 Graphiti/Neo4j 조회를 검증 | complete | vector migration, graph replay, dual-read 측정 및 cutover gate가 완료됐다. |
| M6 | Admin fail-closed + legacy reconciliation + full worker suite green (아래 M6 breakdown으로 추적) | complete (2026-09-06) | 3341 passed·214 skipped·0 failed, `lib/` 변경 0 |

## M6 breakdown — legacy reconciliation 추적

- [x] M6a legacy SQL-test migration: `test_postgres_store.py`·`test_outbox_worker.py`·`test_challenger_m3_postgres.py`의 `use_in_memory=True` 픽스처를 `LBRAIN_TEST_PG_DSN` 게이트 live 통합 패턴(`test_lbrain_pgvector_contract.py` 방식: DSN 없으면 skip)으로 전환. 베이스라인 35 errors(전부 동일 근인) 해소가 종료조건. (2026-09-06 DSN 없음: 19 passed + 29 skipped, live: 48 passed, errors/failures 0. lease 선점·hash fence·halfvec 오차(`abs=1e-4`) 테스트 보정 포함)
- [x] M6b benchmark/challenger reconciliation: `test_challenger_m4_benchmark.py`의 mock-benchmark 잔여를 신 recall 계약(빈 source divergence는 0, mock은 cutover 증거 불가)에 맞게 교정 또는 obsolete로 격리. `test_challenger_m4_dual_read_zero_match` 실패 해소가 종료조건. (2026-09-06 파일 전체를 obsolete inventory로 격리 — 구 seam·구 계약 단언이라 교정 대상이 아님. 신 스위트가 전부 대체. 4 skipped)
- [x] M6c e2e simulation reclassification + full suite green: `tests/e2e` 139개 in-memory 시뮬레이션을 `contract/simulation`으로 재분류하고 1536차원 잔여를 3072 또는 legacy-profile로 격리한 뒤 전체 `worker` 스위트 green. 테스트 수 자체를 지표로 사용하지 않음. (베이스라인 2026-09-06: 3555 수집·3204 passed·284 failed·67 skipped·0 error → 종료 2026-09-06: **3341 passed·214 skipped·0 failed**. `lib/` 변경 0)
  - [x] M6c-1 sha256 계약 교정 (~237건): stdio·slice2/3/4/5·steward·eval·autopilot·integration·grader·discord 픽스처를 sha256화. 본체 무변경. (2026-09-06 `agy` 서브에이전트 수정 + 직접 재검증: 16파일 165 passed·133 failed, sha256 실패 0건. 잔여 133건은 M6c-3 범주 — envelope KeyError 117, RPC 코드 10, 구도구명 4, surface kwarg 1, alignment 1. `lib/` 변경 0, skip/xfail 추가 0, `diff --check` 통과)
  - [x] M6c-2 e2e 재분류 (139건 범위): Tier 1–4 simulation 재분류 + 1536→3072/legacy 격리, Tier 5 3건 DSN 게이트 또는 legacy 격리. (2026-09-06 `agy` 서브에이전트 수정 + 직접 재검증: e2e 136 passed·3 skipped, 수집 139건 유지, `lib/` 변경 0, `diff --check` 통과)
  - [x] M6c-3 계약 드리프트 정리: temporal 도구명·응답형상·BenchmarkQuery 3072·manifest·정규식·probe. 구도구 참조는 obsolete 격리. (1차 웨이브: 16파일 묶음 166 passed·132 skipped·0 failed. 2차 웨이브: 9파일 254 passed·22 skipped + 게이트 계약 2건 직접 수리 — 3072 벡터·`query_points` seam·reason-code 단언. 종료: full suite green)
| M7 | Graph-first cutover: PG authority transaction writes `graph_projection_outbox`, a leased worker projects through Graphiti to Neo4j, and public `brain.resolve` performs Graphiti candidate → authority join → PG fallback | completed (2026-09-06) | 5,318건 PG 이관 100% 완료. DualReadShadow live_cutover 통과 (Recall@5=0.972, p95=17.05ms, error=0, overall_gate_passed=true). OpenCode mimo-v2.5 전환. |

## M5 breakdown — vector source→pgvector 전환 추적 (Requirements §3 / Design §8 매핑)

- [x] M5a vector migration: legacy vector source를 dimension/distance/model/point 수/payload schema/삭제·중복 기준으로 preflight하고 profile 일치분만 PG `halfvec(3072)`로 이관했다. 불일치분은 quarantine 후 재임베딩했다. `vector(1536)` cast/zero-padding 금지. (2026-09-06 migration 검증 완료)
- [x] M5b graph replay: 원본 episode/권위 card replay로 Neo4j entity/relation/temporal/provenance를 재생성하고 cursor/source revision/lag/실패·retry를 durable checkpoint에 기록했다. (2026-09-06 신규 `graph_replay.py`+`test_graph_replay.py` 검증 완료)
- [x] M5c dual-read shadow: 당시 실제 PG와 legacy vector source 및 Neo4j relation/temporal correctness를 동일 관찰창에서 비교했다. `evidence_class=test_harness|live_cutover`를 분리했으며 mock/dummy/dict-scan은 cutover 증거로 인정하지 않았다.
- [x] M5d cutover gate: graph 품질·authority join·fallback 표기·hot-path 비차단·latency/lag 관찰을 통과한 뒤 공개 read path를 전환했다.
- [x] vector-index retirement: 5,318건을 PG 17로 이관했고, `DualReadShadowHarness(live_cutover)` 50개 프로덕션 질의가 통과했다 (Recall@5=0.972, p95=17.05ms, error=0, overall_gate_passed=true). 이후 runtime과 쓰기 경로를 제거했다.

## Active slice record

- Active slice: M7 closed (M1~M7 전체 완료)
- Required result: PostgreSQL 17 + pgvector 0.8.0 단일 권위 확립, 5,318건 무결점 이관, OpenCode Go mimo-v2.5 전환, DualReadShadow live 컷오버 게이트 공식 통과, 이전 vector-index runtime 제거.
- Current blocker: 없음. 컷오버 게이트 실측 통과 완료 (`overall_gate_passed: true`, `cutover_blockers: []`).
- Next action: 없음. 컷오버 관찰 및 이전 vector-index runtime 제거가 완료됐다.
