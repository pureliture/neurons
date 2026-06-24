# 04. dual-write / shadow-read / recall parity / rollback gate와 cutover ladder

이 문서는 "RAGFlow를 **언제 안전하게 끌 수 있는가**"를 단계별 gate로 고정한다. 각
gate는 통과 증거(evidence)를 산출하고, 그 증거가 다음 단계 진입의 전제가 된다.
어떤 단계도 이 문서가 직접 실행하지 않는다 — 모두 별도 승인·실행 절차를 거친다.

## 0. 재사용하는 기존 gate 골격

| 골격 | schema / symbol | 역할 |
| --- | --- | --- |
| searchable-mirror gate | `build_searchable_mirror_gate_report`, evidence schema `agent_knowledge_searchable_mirror_gate_evidence.v1` | dual_write / read_compare / apple_silicon_local / ubuntu_host / operator_approval 5섹션 검증, 항상 `production_authority=NO-GO` |
| shadow worker | `shadow_worker.py` (`RAG_INGRESS_SHADOW` 격리 스트림, `SHADOW_DELIVER`, `deliver=False` 관찰) | live queue 미접속 dual-write 선례 |
| state/recall 은퇴 chain | `retirement_readiness.py`(M9), `state_shadow_readiness.py`(M6), `product_surface_switch_plan.py` | ledger/recall-routing 은퇴 + approval/rollback manifest 패턴 |

> 현재 searchable-mirror gate와 M9 chain은 **cross-reference가 없다**. 본 ladder는
> 둘을 연결한다(Stage 6).

## 1. evidence 섹션 정의 (gate가 요구하는 것)

`agent_knowledge_searchable_mirror_gate_evidence.v1`는 이미 다음을 강제한다:

- `dual_write`: `evidence_digest`(sha256), `total_count>0`, `target_profiles[]`.
- `read_compare`: `evidence_digest`, `total_count`, `matched_count`,
  `mismatch_count`. **`mismatch_count==0` 그리고 `total_count==matched_count`**.
- `apple_silicon_local`: `smoke_digest`(sha256).
- `ubuntu_host`: `dry_run_digest`(sha256).
- `operator_approval`: `approved==true`, `approval_digest`(sha256).
- `collected_at`.

### 1.1 recall parity 지표 (보강 필요)

현재 read_compare는 **정확 일치(exact match)** 만 본다. semantic mirror 교체에는
정확 일치만으론 부족하다. 다음을 추가 정의한다(evidence 생성 harness가 채울 값):

- **recall@k overlap**: 동일 query 집합에 대해 RAGFlow `retrieve` top-k와 Qdrant
  `query_mirror_candidates` top-k의 content_hash 교집합 비율. gate 통과 기준 예:
  대표 query cohort에서 recall@10 ≥ 목표치(예 0.95) 이상, 회귀 없음.
- **authority-join 일치**: 두 경로 hit를 ledger join 후 최종 노출 카드 집합이
  동일(=`mismatch_count==0`은 이 join 후 비교에 적용).
- **golden 회귀 gate**: `neurons.golden.draft.json` 기반 autopilot grade가 mirror
  교체 전후로 `authority-model`/`recall-transport` lane에서 회귀 0.

이 지표 산출 harness는 아직 없다(현재 gap). harness가 read_compare evidence를
생성하기 전엔 read cutover 불가.

## 2. cutover ladder (RAGFlow OFF까지)

각 Stage는 **이전 Stage의 evidence가 green일 때만** 진입한다. RAGFlow는 Stage 6
이전까지 항상 ON이며 authority/fallback으로 남는다.

### Stage 0 — code-only readiness (현재, 이 작업으로 충족)

- Qdrant adapter + delete seam + 재사용 fake + contract test 존재, worker test green.
- live wiring/routing/env 변경 없음. RAGFlow 100% ON.
- exit 증거: `uv run pytest -q` green, 본 문서 01–04.

### Stage 1 — embedding/schema 확정 (no live write)

- embedding: 기존 OpenAI-compatible embedder(`LLM_BRAIN_EMBEDDING_*`, dim 1024,
  graphiti_adapter와 동일 endpoint) 재사용 + 동일 reranker. vector size/distance
  고정(새 모델 결정 아님).
- payload top-level 승격 필드 + payload index 버전 확정(문서 02 §3).
- ledger `qdrant_collections` 레지스트리 migration(additive) 설계.
- Qdrant hit ledger-join 구현(authority 규약 충족).
- exit 증거: collection schema 문서 + 단위 test(실모델 차원/필터/ledger-join).

### Stage 2 — dual-write shadow (RAGFlow가 record-of-authority)

- `shadow_worker` 패턴으로 Qdrant를 **두 번째 sink**로 추가(격리 스트림, RAGFlow
  delivery 무변경). `INGRESS_DELIVERY_BACKEND`에 qdrant 분기 추가 가능하나
  **live recall은 여전히 RAGFlow에서만** 제공.
- dual_write evidence(`total_count`, `target_profiles`) 수집.
- exit 증거: `dual_write` 섹션 green, RAGFlow recall 무변경 확인.

### Stage 3 — shadow-read parity (관찰만)

- read_compare harness로 동일 query에 대해 RAGFlow vs Qdrant 결과를 ledger-join
  후 비교. `mismatch_count==0` + recall@k 목표 + golden 회귀 0를 **soak window**
  동안 연속 green으로 누적(`state_shadow_readiness`의 `consecutive_green_runs`/
  `soak_window_start` 패턴 재사용).
- exit 증거: `read_compare`/`apple_silicon_local`/`ubuntu_host` green,
  soak window 충족.

### Stage 4 — read cutover (mirror lane만, RAGFlow ON 유지)

- recall의 **mirror/vector lane**만 Qdrant로 전환: brain.query archive/evidence
  lane, knowledge.search ranking, supersede vector stage, transcript retrieval
  source. ledger/CouchDB authority join은 유지. RAGFlow는 **fallback**으로 ON.
- `product_surface_switch_plan` 패턴으로 recall 표면 switch plan + rollback
  manifest + approval packet 생성(redacted argv, operator-bound).
- exit 증거: operator approval, switch 후 evidence 재생성, recall regression 0.

### Stage 5 — write cutover & no-fallback 소비자 이전

- RAGFlow write(session-memory sync, ingress delivery, card projection)를 Qdrant/
  CouchDB로 전환. Qdrant write path + delete chokepoint(문서 03 §4.4) 연결.
- **문서 01 §C의 no-fallback read 소비자 전부 이전/은퇴**: autopilot mining,
  `ragflow_read_sot` build, GC runners 3종, backfill seed, native-memory reconcile,
  status reconciler(자연 해소), brain archive lane.
- exit 증거: 각 소비자별 source 전환 test + GC dry-run/coverage/backup/rollback
  evidence(AGENTS.md GC 규약 분리 보고).

### Stage 6 — RAGFlow disable (최종, 되돌릴 수 있게)

- Stage 2–5가 soak 동안 green이고, 모든 no-fallback 소비자가 이전 완료, recall
  regression gate 통과, backup/rollback evidence 확보, operator approval 존재할 때만.
- searchable-mirror gate evidence(`...searchable_mirror_gate_evidence.v1`)를 M9
  closure chain(`build_m9_closure_bundle`/`build_m9_closure_approval_record`)에
  **cross-reference로 추가**(현재 미연결). closure approval record의 apply_sequence에
  "ragflow_search_mirror_disable" 단계를 추가하고, unredacted operator binding +
  rollback bytes 보존을 요구.
- disable은 즉시 hard delete가 아니라 **retention/stability window** 동안 RAGFlow
  데이터 보존 후 별도 GC 승인으로 삭제(되돌릴 수 있는 순서).

## 3. rollback gate

| 시점 | rollback 방법 |
| --- | --- |
| Stage 2–3 | Qdrant sink/관찰 비활성화. RAGFlow가 계속 authority라 사용자 영향 0. |
| Stage 4 | recall mirror lane을 RAGFlow로 즉시 환원(switch plan rollback manifest의 bytes/digest로 config 복원). |
| Stage 5 | write를 RAGFlow로 환원. Qdrant는 mirror라 데이터 재-ingest로 복구(본문은 ledger/CouchDB 보유). |
| Stage 6 | RAGFlow는 disable만 한 상태(데이터 보존)이므로 re-enable로 복귀. hard delete는 retention window 이후 별도 승인. |

rollback 불가 지점은 **RAGFlow 데이터의 hard delete**뿐이다 → 이는 disable·soak·
승인·backup 이후로만 분리한다(GC 규약).

## 4. "RAGFlow를 끌 수 있는가" 판정 체크리스트 (Stage 6 진입 gate)

- [ ] dual_write evidence green (Stage 2)
- [ ] read_compare: mismatch 0 + recall@k 목표 + golden 회귀 0, soak window 충족 (Stage 3)
- [ ] apple_silicon_local + ubuntu_host smoke green
- [ ] read mirror lane Qdrant 전환 + recall regression 0 (Stage 4)
- [ ] write 전환 + Qdrant delete chokepoint 연결 (Stage 5)
- [ ] no-fallback 소비자(01 §C) 전부 이전/은퇴
- [ ] backup/rollback bytes 보존, retention/stability window 정의
- [ ] operator approval(unredacted binding) + M9 closure chain cross-reference
- [ ] disable은 hard delete 아님(되돌릴 수 있는 순서)

위 9개가 모두 충족되기 전에는 RAGFlow searchable mirror를 끄지 않는다.
