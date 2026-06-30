# Ingress API Profile Startup Requirements

## 승인 대상

- Source of truth: `requirements.md`
- Preview companion: `requirements.html`

## 질문-답변 흐름

### Q: 어떤 실패를 고쳐야 하는가?

`ingress-api`는 Kubernetes 운영 설정처럼 `SPRING_PROFILES_ACTIVE=api` 단독으로 실행될 때 Spring context가 떠야 한다. 현재 실패는 `StatusService`가 `RagTargetAdapter`를 필수 bean으로 요구하지만, 실제 구현체가 `retired-index-bridge` profile에만 묶여 있어서 `api` 단독 profile에서 bean resolution이 실패하는 것이다.

### Q: target adapter가 없는 API는 성공 상태인가, 장애 상태인가?

앱 프로세스는 떠야 하지만 target 상태는 성공으로 위장하면 안 된다. `/status`는 target pressure를 `CLOSED`, reason을 `not_configured`, `externalStatus`를 `not_configured`로 표현한다.

### Q: fallback adapter bean을 둘 것인가, API status 의존성을 optional로 둘 것인가?

선택: unavailable target fallback `RagTargetAdapter` bean.

이유: `StatusService`의 Spring constructor 계약은 유지하면서 `api` profile 단독 startup 실패를 bean graph 수준에서 막을 수 있다. 단, fallback은 `api & !worker & !retired-index-bridge` profile에만 묶어 `worker`가 noop delivery adapter를 주입받지 않게 한다.

### Q: `retired-index-bridge` profile과 worker profile은 어떻게 보호하는가?

`api,retired-index-bridge`에서는 fallback 자체가 비활성화되고 실제 `RetiredIndexBridgeTargetAdapter`가 기존 동작을 유지한다. `api,worker` 또는 `worker` profile에는 fallback adapter를 추가하지 않는다.

### Q: 이번 작업에서 운영 rollout까지 수행하는가?

아니다. live canary/production rollout은 외부에 보이는 운영 변경이므로 이 source change의 범위에서는 실행하지 않는다. 이 작업은 코드와 테스트로 runtime failure를 막고, rollout은 별도 승인된 운영 절차에서 canary first로 진행한다.

## 기능 요구사항

- `api` profile 단독 상태에서 fallback `RagTargetAdapter`가 등록되어 `StatusService` startup을 막지 않아야 한다.
- adapter가 없을 때 `/status`는 target pressure `CLOSED`, reason `not_configured`, `externalStatus` `not_configured`를 반환해야 한다.
- `/healthz`는 target adapter 구성 여부와 무관하게 `{"status":"ok","component":"ingress-api"}`를 반환해야 한다.
- `api,retired-index-bridge`처럼 실제 adapter가 존재하는 profile 조합에서는 실제 adapter가 `StatusService`에 주입되어야 한다.
- `worker` profile에는 fallback adapter를 추가하지 않으며, delivery 성공을 위장하지 않는다.
- NATS는 기존 runtime prerequisite로 남는다. 이번 변경은 target adapter 미설정 상태만 degraded로 바꾸며, queue 연결/프로비저닝 정책은 바꾸지 않는다.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| Safety | adapter 미설정 상태는 fail-closed/degraded로 표시한다. |
| Compatibility | public endpoint shape와 기존 `retired-index-bridge` tests를 깨지 않는다. |
| Isolation | 모든 변경은 전용 branch/worktree 안에서 수행한다. |
| Verification | Java tests와 boot jar build를 통과해야 한다. |
| Operations | production-facing rollout은 이 변경의 자동 실행 범위에서 제외한다. |
| Runtime prerequisite | NATS 연결과 기존 provision-on-startup 정책은 현행 동작을 유지한다. |

## 사용자 시나리오

- 운영자는 `SPRING_PROFILES_ACTIVE=api`만 설정한 ingress-api pod가 Spring startup 단계에서 죽지 않는 것을 기대한다.
- 운영자는 target adapter가 아직 켜지지 않은 API pod에서 `/healthz`로 process health를 확인하고, `/status`로 target degraded 상태를 확인한다.
- 운영자는 `retired-index-bridge` profile을 추가했을 때 실제 target adapter 상태가 `/status`에 반영되는 것을 기대한다.

## 미결정 항목

- 없음. 이 요구사항은 사용자의 사전 승인에 따라 Phase 2 설계로 즉시 진행한다.
