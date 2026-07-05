# Architecture Debt Single-Issue Campaign Requirements

## 승인 대상

- Source of truth: `requirements.md`
- Preview companion: 생성하지 않음. Markdown만으로 검토 가능한 1차 요구사항 초안입니다.
- Approval status: Approved by user on 2026-07-05. Phase 2 `design.md` 작성으로 진행합니다.

## 배경

GitHub issue #40은 2026-06-28 `origin/main` `f222ea7` 기준 architecture deepening review에서 시작했으나, 2026-07-05 follow-up 분석으로 현재 부채 상태가 재분류되었습니다. 사용자는 #40을 여러 issue로 쪼개지 않고 단일 통합 추적 이슈로 유지하기로 결정했습니다.

입력 자료:

- `/tmp/architecture-review-followup-20260705191500.html`
- `/Users/ddalkak/Projects/neurons/.agents/orchestrator_followup/handoff.md`
- GitHub issue #40 updated body

## 질문-답변 흐름

### Q: 이 작업은 여러 GitHub issue로 쪼갤 것인가?

아니요. #40 하나를 단일 architecture debt tracker로 사용합니다. 구현 PR은 작게 나눌 수 있지만, backlog authority는 #40입니다.

### Q: 기존 2026-06-28 리뷰는 폐기할 것인가?

아니요. 과거 리뷰는 baseline으로 유지하되, 현재 추적 기준은 2026-07-05 follow-up 보정 상태입니다. 오래된 표현은 최신 runtime terminology로 재분류합니다.

### Q: 이번 요구사항은 구현까지 포함하는가?

아니요. 이 문서는 Phase 1 요구사항 source입니다. 구현 방법, module move, schema shape, CLI flag, manifest layout은 `requirements.md` 승인 후 `design.md`에서 접근안과 trade-off를 비교합니다.

### Q: 사용자가 계속 확인하지 않아도 장기 작업으로 끝까지 가야 하는가?

예. 사용자는 세부 작업 방식보다 승인된 SoT를 기준으로 장시간 반복 실행해 목표에 도달하는 운영 모델을 원합니다. 구현 단계는 `requirements.md`와 승인된 `design.md`를 drift guard로 삼아, act -> observe -> adjust 루프를 반복해야 합니다.

## 목표

- #40을 단일 architecture debt campaign의 source tracker로 유지합니다.
- follow-up 보고서가 지적한 누락 영역을 구현 가능한 요구사항 backlog로 정리합니다.
- 기존 후보의 resolved, active, partial, stale 상태를 명확히 구분합니다.
- 구현 전 design 단계에서 검증 가능한 성공 기준과 안전 경계를 제공합니다.
- 사용자 확인 없이도 장기 실행 가능한 goal-oriented execution contract를 정의합니다.
- 실행 중 설계 drift가 생기지 않도록 SoT 파일과 회귀 조건을 고정합니다.

## 기능 요구사항

### FR1. 단일 이슈 추적

- #40은 architecture debt campaign의 유일한 GitHub issue로 남아야 합니다.
- 새 GitHub issue 생성은 기본 금지입니다.
- 구현 PR은 여러 개일 수 있으나, 각 PR은 #40의 어느 requirement를 다루는지 명시해야 합니다.

### FR2. 현재 상태 재분류 유지

다음 상태 분류를 유지해야 합니다.

| 상태 | 항목 |
| --- | --- |
| Resolved | compatibility shim 삭제, `.env.example` guard 일부 |
| Active | Ledger god-class, TargetProfile contract drift, RetiredIndexBridge adapter placement, `llm_brain_core` flat package |
| Partial | compose env anchor |
| Reworded/Stale | old `RagFlowTargetAdapter`, old `RAGFLOW_*` framing |
| Follow-up debt | `worker/eval`, `deploy/k3s`, `scripts`, `specs`, model connector residual debt, CouchDB/session-memory migration dead code |

### FR3. `worker/eval` readiness debt 정리

- eval lane은 product readiness gate와 dev-only harness를 구분해야 합니다.
- open blocker는 code/test-backed checklist로 표현되어야 합니다.
- 최소 blocker set은 live mining provider, scheduler install, `machine_origin`, candidates persistence, runtime tripwire, golden content completion을 포함해야 합니다.
- `worker/eval` 결과가 runtime readiness를 과장하지 않도록 출력/문서 표현을 제한해야 합니다.

### FR4. `deploy/k3s` public contract hardening

- public repo가 소유하는 k3s contract와 private `neurons-ops` 책임을 더 명확히 분리해야 합니다.
- public-safe workload skeleton 또는 검증 가능한 contract artifact가 필요합니다.
- scale-out precondition, NetworkPolicy/CNI caveat, workqueue isolation, backup/restore rehearsal gate가 testable해야 합니다.
- live k3s apply, compose stop, firewall/systemd/Docker host mutation은 별도 승인 전 금지입니다.

### FR5. runtime verification scope 분리

- `postcheck`, API/NATS smoke, full E2E business verification을 서로 다른 verification level로 구분해야 합니다.
- API shape만 확인한 결과를 full runtime verified처럼 표현하면 안 됩니다.
- full E2E verification은 persistence, mirror/index, graph/projection, recall/read path 중 어떤 범위까지 검증했는지 명시해야 합니다.
- live external system 검증은 approval, bounded timeout, redaction, postcheck boundary를 가져야 합니다.

### FR6. `specs` drift 관리

- `specs/*/requirements.md`, `design.md`, `implementation-matrix.md`의 주요 claims는 current code/runtime 상태와 대조 가능해야 합니다.
- spec 상태는 done, partial, stale, superseded, open으로 분류되어야 합니다.
- 구현 세션은 관련 spec drift를 먼저 확인한 뒤 시작해야 합니다.

### FR7. model connector residual debt 정리

- model connector debt는 "완전 미분리"가 아니라 residual parsing/logprobs debt로 추적해야 합니다.
- provider capability가 `logprobs`를 지원하지 않을 때 실패 방식이 명확해야 합니다.
- Graphiti structured response normalization은 connector 내부 ad-hoc patch인지, 명시적 adapter contract인지 design에서 결정해야 합니다.

### FR8. CouchDB/session-memory migration dead code 정리

- active runtime surface와 archive-only or test-only tooling을 구분해야 합니다.
- legacy CLI, migration helper, shadow cutover helper는 deletion test 대상이 되어야 합니다.
- 삭제 전 test import path, CLI compatibility, docs references 영향을 확인해야 합니다.

### FR9. 기존 active backlog 유지

다음 기존 부채는 follow-up 후보와 함께 계속 추적해야 합니다.

- Ledger god-class / mixin 다중 상속
- Java/Python TargetProfile contract drift
- compose env anchor partial state
- RetiredIndexBridge adapter placement
- `llm_brain_core` flat package

### FR10. 장기 실행 goal contract

- 승인된 `design.md`는 하나의 long-running implementation goal로 취급되어야 합니다.
- 실행 agent는 사용자의 매 단계 확인 없이 목표 달성까지 반복 작업할 수 있어야 합니다.
- 실행 루프는 act -> observe -> adjust -> repeat 구조를 따라야 합니다.
- 각 반복은 현재 SoT requirement/design과 #40 backlog 상태를 대조해야 합니다.
- 단순 난이도, 테스트 실패, 긴 실행 시간만으로 중단하면 안 됩니다.
- 목표 달성, SoT 변경 필요, 승인 필요한 live mutation, 또는 반복된 외부 blocker가 있을 때만 멈춰야 합니다.

### FR11. 설계 drift 방지

- `requirements.md`는 What의 SoT입니다.
- 승인된 `design.md`는 How의 SoT입니다.
- 구현 중 새로운 사실이 design과 충돌하면 agent가 임의로 설계를 바꾸면 안 됩니다.
- SoT 변경이 필요하면 구현 루프를 멈추고 grill-to-spec 상류로 회귀해야 합니다.
- 진행 상태는 design의 milestone/evidence checklist에 기록되어야 합니다.
- 완료 선언은 SoT checklist와 verification evidence가 맞을 때만 허용됩니다.

### FR12. MCP single internal definition

- MCP tool schema, dispatch owner, and handler callable은 하나의 내부 contract definition에서 유도되어야 합니다.
- public `list_tools()` output은 schema-compatible해야 하며 handler callable 또는 dispatch-only metadata를 노출하면 안 됩니다.
- restricted steward write handlers와 read/proposal handlers는 내부 registry에서 분리되어야 합니다.
- MCP 구현은 local contract tests로 검증해야 하며, live MCP proposal write 또는 runtime mutation을 수행하면 안 됩니다.

### FR13. Ledger area-object extraction

- Ledger god-class 완화는 public `Ledger` API와 durable-state semantics를 유지하면서 진행해야 합니다.
- 첫 extraction은 memory-promotion side effect처럼 이미 seam이 존재하거나 최소 seam으로 둘 수 있는 area에 한정해야 합니다.
- mixin 다중 상속 전체 제거는 이번 단계의 필수 완료 조건이 아닙니다.
- area-object 경계는 boundary tests로 보호해야 하며 GC/live data mutation을 수행하면 안 됩니다.

### FR14. TargetProfile shared schema artifact

- Java `TargetProfileRegistry`, `application.yml`, Python dataset resolver, compose env, and `.env.example` coverage는 public-safe shared TargetProfile artifact와 일치해야 합니다.
- shared artifact는 logical `targetProfile`, backend kind, dataset role, and expected retired bridge dataset env key만 담아야 하며 physical dataset id나 secret을 담으면 안 됩니다.
- profile 추가/삭제/역할 변경은 Java/Python/compose 한쪽만 바뀌는 drift로 남으면 안 됩니다.

### FR15. RetiredIndexBridge adapter placement guard

- retired external index bridge Java adapter implementation은 `adapter.ext.retired_index_bridge` package boundary 안에 격리되어야 합니다.
- `target.port`는 backend-neutral port만 소유하고 retired bridge implementation detail을 의존하면 안 됩니다.
- historical `targetAdapter`/old placement wording은 current implementation source를 override하면 안 됩니다.

### FR16. compose SnakeYAML hardening

- compose env anchor 검증은 문자열 포함 검사만이 아니라 YAML merge 결과를 파싱해 서비스별 resolved env를 확인해야 합니다.
- Java ingress services and Python ingress worker must resolve the same retired bridge common env keys through one shared source.
- live queue/delivery opt-in env는 shared retired bridge anchor로 이동하면 안 됩니다.

### FR17. k3s public contract hardening

- `deploy/k3s` public contract는 workload inventory, config contract, ops overlay contract, and public/private boundary를 static tests로 검증해야 합니다.
- tests must cover scale-out preconditions, NetworkPolicy/CNI caveat, workqueue isolation, backup/restore rehearsal gate, and absence of live apply.
- live k3s apply, host mutation, secret loading, and private ops value changes remain approval-gated and out of scope.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| Safety | read-only analysis와 live mutation을 명확히 분리 |
| Public/private boundary | secret, raw private path, raw transcript body, raw dataset/document id 금지 |
| Approval gate | live write/delete/disable/deploy/k3s mutation은 별도 승인 필요 |
| Compatibility | public CLI/API compatibility는 승인 없이 깨지 않음 |
| Verification | 각 implementation track은 test or evidence gate를 가져야 함 |
| TDD | code-changing work는 red -> green -> refactor 또는 동등한 TDD-first 흐름 |
| Issue management | GitHub issue는 #40 하나만 사용 |
| Runtime truth | local checkout만으로 live runtime success를 주장하지 않음 |
| Long-running autonomy | 승인된 SoT 안에서는 사용자 재확인 없이 목표 달성까지 반복 실행 |
| Drift control | SoT 변경 필요 시 구현을 멈추고 requirements/design 단계로 회귀 |

## 사용자 시나리오

- 운영자는 #40 하나만 열어 현재 architecture debt backlog와 상태를 확인한다.
- 구현자는 #40의 항목을 작은 PR로 처리하되 새 issue를 만들지 않는다.
- reviewer는 PR이 어떤 requirement와 evidence gate를 닫는지 확인한다.
- runtime verifier는 API-only smoke와 full E2E verification을 혼동하지 않는다.
- spec maintainer는 오래된 spec claim을 stale/superseded로 표시하고 current code와 충돌을 줄인다.
- 사용자는 goal을 한 번 지정한 뒤 중간 확인 없이도 agent가 SoT를 기준으로 반복 실행해 목표까지 가기를 기대한다.
- 구현 agent는 실패할 때 즉흥적으로 scope를 바꾸지 않고, SoT와 충돌하면 상류 승인으로 되돌린다.

## 우선순위 요구

요구사항 초안의 기본 우선순위는 다음과 같습니다.

1. `worker/eval` readiness debt와 `specs` drift를 먼저 정리합니다.
2. runtime verification scope를 바로잡아 이후 PR evidence가 과장되지 않게 합니다.
3. MCP schema / dispatch owner / handler callable single internal definition을 먼저 닫습니다.
4. MCP 검증 통과 후 Ledger area-object extraction을 진행합니다.
5. TargetProfile shared schema artifact를 닫습니다.
6. RetiredIndexBridge adapter placement guard를 닫습니다.
7. compose SnakeYAML hardening을 닫습니다.
8. `deploy/k3s` public contract hardening을 닫습니다.

## 범위 밖

- 새 GitHub issue 생성
- `requirements.md` 승인 전 `design.md` 작성
- live RetiredIndexBridge write/delete/disable
- live Docker/k3s/systemd/firewall mutation
- public repo에 private ops value 또는 raw identifier 추가
- production deployment claim

## 성공 기준

- #40 본문과 requirements source가 같은 backlog 상태를 설명합니다.
- 각 active debt가 resolved, active, partial, stale, follow-up debt 중 하나로 분류됩니다.
- 구현 대상 항목마다 최소 하나의 verification expectation이 있습니다.
- API-only verification과 full E2E verification의 표현이 분리됩니다.
- `requirements.md 승인` 후 design phase에서 2-3개 접근안을 비교할 수 있을 만큼 요구사항이 닫혀 있습니다.
- 승인된 `design.md`를 장기 실행 goal로 넘길 수 있습니다.
- 실행 루프가 SoT drift 없이 진행·중단·완료를 판단하는 조건을 가집니다.
- MCP schema/owner/handler가 하나의 내부 contract source에서 유도되고 public schema compatibility가 유지됩니다.
- Ledger area-object extraction이 public API와 durable-state semantics를 유지한 채 boundary tests로 보호됩니다.
- TargetProfile shared artifact가 Java/Python/compose/env-example drift를 잡습니다.
- RetiredIndexBridge adapter placement가 architecture guard로 보호됩니다.
- compose env anchor 검증이 YAML merge 결과 기준으로 동작합니다.
- k3s public contract가 live apply 없이 static tests로 검증됩니다.

## 닫힌 결정

### D1. 첫 구현 campaign의 운영 방식

첫 implementation campaign은 사용자가 매 단계 확인하지 않아도 되는 장기 실행 goal을 전제로 설계합니다. 작업 범위와 milestone 분해는 `design.md`에서 정하되, 모든 실행은 승인된 SoT를 기준으로 drift 없이 반복되어야 합니다.

결정:

- #40은 단일 tracker로 유지합니다.
- `requirements.md`와 승인된 `design.md`를 SoT로 둡니다.
- 구현은 long-running goal로 실행할 수 있어야 합니다.
- agent는 act -> observe -> adjust loop를 목표 달성까지 반복합니다.
- SoT 변경 필요, live mutation approval 필요, 반복된 외부 blocker가 생기면 멈춥니다.

## 미결정 항목

없음. 사용자가 승인했습니다.

## 승인 안내

이 문서는 승인된 Phase 1 source입니다. 이후 변경이 필요하면 Phase 2 또는 구현 중 임의 수정하지 않고 grill-to-spec Phase 1로 회귀합니다.
