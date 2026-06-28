# Neurons k3s Migration Requirements

## 승인 대상

- Source of truth: `requirements.md`
- Preview companion: `requirements.html`
- 현재 단계: Phase 1 Requirements Discovery
- 승인 전 제한: `design.md` 작성, k3s manifest 작성, Helm chart 작성, deploy script 작성,
  live runtime 변경, secret 변경, k3s apply, compose 중단은 하지 않는다.

## 배경

- `neurons`는 OpenClaw/LLM-brain의 server/brain repo다.
- 현재 실제 runtime 기준은 Ubuntu compose 쪽이다.
- k3s는 Ubuntu runtime host에 설치돼 있지만, 마지막 확인 기준으로 `neurons`
  namespace/deployment target은 없었다.
- k3s 이관은 아직 시작되지 않았다.
- 이번 flow는 `grill-to-spec`로 요구사항을 먼저 확정하고, `requirements.md`
  승인 후에만 `design.md`를 작성한다.
- `design.md` 승인 후에만 `agentic-execution`으로 구현을 시작한다.

## 질문-답변 흐름

### Q1: k3s primary 위치는 어디로 잡을까?

**local Ubuntu primary**로 확정한다.

- 현재 compose runtime과 가장 가까운 Ubuntu host를 k3s primary로 본다.
- OCI는 이번 결정에서 primary가 아니다.
- OCI를 staging, canary, DR 후보로 포함할지는 별도 결정으로 남긴다.

### Q2: 이관 범위는 어디까지로 잡을까?

**`neurons` compose 전체를 k3s로 이전**으로 확정한다.

- 여기서 "compose 전체"는 `neurons` repo가 소유한 compose surface를 뜻한다.
- API/MCP, worker/runtime services, CouchDB, Postgres ledger, Neo4j, Qdrant, 관련
  repo-owned worker compose surface를 이관 후보에 포함한다.
- 이미 제거된 removed legacy external-memory surface는 이번 k3s migration 대상이 아니다.
- 남아 있는 legacy external-memory source/env 이름은 migration target이 아니라
  cleanup/compatibility debt로 취급한다.
- 제거된 external-memory platform을 외부 active dependency처럼 재도입하지 않는다.

### Q3: 네트워크 경계는 어떻게 잡을까?

**Tailscale subnet router**로 확정한다.

- local Ubuntu primary의 k3s/network 운영을 tailnet에서 다룰 수 있게 한다.
- 이 migration은 1인 개인 tailnet을 전제로 하며, 승인된 개인 기기 전체가 k3s
  pod/service route에 접근할 수 있는 `personal-tailnet-wide` 모델을 허용한다.
- Tailscale admin route 승인은 k3s pod/service route scope로 제한한다.
- public internet 노출은 금지한다.
- raw DB나 stateful service 직접 접근은 기본 운영 경로가 아니지만, 개인 tailnet-wide
  접근의 잔여 위험은 owner risk acceptance evidence로 기록한다.
- k3s API 자체를 tailnet only로 강제할지는 Phase 2 design에서 운영 복잡도와 함께 검토하되,
  Phase 1 요구사항에서는 subnet router를 기본 네트워크 경계로 둔다.

### Q4: cutover 방식은 어떻게 잡을까?

**compose 유지 + k3s canary**로 확정한다.

- Ubuntu compose runtime은 k3s canary 검증 중에도 유지한다.
- k3s는 처음부터 primary traffic 전체를 받지 않고 canary target으로 검증한다.
- ingress worker canary는 compose live worker와 같은 WorkQueue durable을 공유하지 않는다.
- canary worker는 shadow stream, 별도 durable, 또는 worker 비활성 health-only 검증 중
  하나로만 실행한다.
- live durable 단일 consumer 계약을 깨는 k3s/compose 동시 실행은 abort 조건이다.
- compose 중단이나 primary 전환은 별도 승인 gate 이후에만 가능하다.
- canary는 redacted health, API/MCP behavior, worker behavior, stateful dependency
  readiness, rollback 가능성을 확인해야 한다.

### Q5: rollback 기준은 어떻게 잡을까?

**backup/restore 검증 후 전환**으로 확정한다.

- stateful stack까지 이관 범위에 포함하므로, primary 전환 전 backup/restore proof가
  필요하다.
- backup은 실제 restore rehearsal로 검증되어야 한다.
- restore rehearsal은 raw transcript, raw dataset/document id, secret-like 값을 출력하지
  않아야 한다.
- backup/restore gate를 통과하기 전에는 compose 중단이나 k3s primary 전환을 하지 않는다.
- rollback 기준은 compose 즉시 복귀 가능성과 state restore 가능성을 함께 확인해야 한다.

### Q6: stateful data migration 전략은 어떻게 잡을까?

**backup/restore rehearsal 먼저**로 확정한다.

- CouchDB, Postgres ledger, Neo4j, Qdrant는 primary 전환 전에 backup과 restore
  rehearsal을 먼저 통과해야 한다.
- rehearsal은 k3s primary 전환의 선행 조건이며, 성공 evidence 없이 done 처리하지 않는다.
- restore 검증은 redacted count, health, schema/index readiness, representative query
  behavior 중심으로 한다.
- raw transcript body, raw dataset_id, raw document_id, secret-like 값은 출력하지 않는다.
- fresh state + selective backfill은 기본 전략이 아니라 예외/복구 옵션으로만 둔다.

### Q7: secret/config 관리는 어디를 기준으로 할까?

**`neurons-ops` private repo 기준**으로 확정한다.

- 실제 env 파일, Tailscale route/access evidence, production overlay, backup/restore runbook,
  host-specific wiring은 private ops repo에서 관리한다.
- public `neurons` repo에는 safe template, non-secret defaults, validation contract,
  documentation만 둔다.
- raw secret, private hostname/path, raw dataset_id, raw document_id는 public repo와
  preview artifact에 기록하지 않는다.
- local Ubuntu host env는 현재 runtime truth를 확인하는 입력으로 사용할 수 있지만,
  k3s migration target config의 장기 source of truth는 private ops repo로 둔다.

### Q8: compose와 k3s 공존 기간은 어떻게 잡을까?

**짧은 공존**으로 확정한다.

- compose는 migration safety net으로만 유지한다.
- 장기 dual-run은 목표가 아니다.
- safety window는 canary/cutover postcheck 기준 최대 24h를 기본 상한으로 둔다.
- 24h 안에 promotion 또는 rollback 결론이 나지 않으면 abort하고 compose primary를 유지한다.
- retry는 private ops approval record에 기록된 제한 안에서만 허용한다.
- k3s canary, backup/restore rehearsal, postcheck gate 통과 후 cutover를 승인한다.
- cutover 승인 후에는 compose를 오래 공존시키지 않고 retire를 목표로 한다.
- compose 중단은 별도 승인 gate 이후에만 실행한다.

### Q9: OCI 역할은 이번 scope에 포함할까?

**OCI는 이번 requirements에서 제외**로 확정한다.

- 이번 요구사항은 local Ubuntu primary k3s migration에 집중한다.
- OCI staging, canary, DR은 이번 scope에 포함하지 않는다.
- 향후 OCI 확장은 별도 requirements/design flow로 다룬다.

### Q10: cutover success criteria는 어디까지 확인할까?

**read/write canary까지 포함**으로 확정한다.

- cutover 후보가 되려면 health, API/MCP behavior, worker behavior, stateful dependency
  readiness, backup/restore rehearsal, rollback 가능성, read/write canary를 통과해야 한다.
- write canary는 실제 state mutation이므로 별도 승인 gate 이후에만 실행한다.
- canary payload는 public-safe synthetic data만 사용한다.
- raw transcript body, raw dataset_id, raw document_id, secret-like 값은 출력하지 않는다.
- removed legacy external-memory surface 재도입은 이번 canary scope에 포함하지 않는다.
- canary 결과는 redacted evidence로 남기며, evidence 없이 done 처리하지 않는다.

## 기능 요구사항

- primary runtime 위치 결정을 명시한다.
- 포함 workload: `neurons` repo-owned compose surface 전체.
- 제외 workload: removed legacy external-memory surface와 `dendrite` Mac host-native thin client.
- stateful data migration 전략은 backup/restore rehearsal 우선으로 명시한다.
- Tailscale subnet router 기반 접근 모델을 명시한다.
- secret/config 관리는 `neurons-ops` private repo 기준으로 명시한다.
- backup/restore/rollback gate를 명시한다.
- compose와 k3s 공존 기간은 짧은 safety window로 명시한다.
- cutover success criteria와 canary promotion criteria는 read/write canary 포함으로 명시한다.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| Safety | live runtime 변경, secret 변경, k3s apply, compose 중단은 명시 승인 전 금지한다. |
| Privacy | raw host, private path, token, cookie, bearer string, API key, raw transcript body, raw dataset_id, raw document_id를 출력하지 않는다. |
| Runtime authority | k3s 이관 전 현재 runtime truth는 Ubuntu compose 기준으로 확인한다. |
| Stateful caution | Neo4j, CouchDB, Qdrant, Postgres state는 보수적으로 다룬다. |
| Network boundary | Tailscale subnet router는 k3s pod/service route scope와 owner-accepted personal tailnet-wide access evidence를 가져야 하며, public internet 노출은 금지한다. kube-apiserver 접근 주체와 service egress는 별도 gate로 확인한다. |
| Cutover safety | compose 유지 + k3s canary를 기본 전환 방식으로 삼는다. |
| WorkQueue safety | k3s canary는 live WorkQueue durable을 compose worker와 공유하지 않는다. |
| Rollback gate | primary 전환 전 backup/restore proof와 compose 복귀 가능성을 검증한다. |
| Stateful migration | backup/restore rehearsal이 k3s primary 전환의 선행 조건이다. |
| Config boundary | public `neurons` repo는 safe template/contract만, private ops repo는 실제 secret/config를 소유한다. |
| Coexistence | compose와 k3s의 장기 dual-run은 목표가 아니며, 기본 최대 24h safety window만 둔다. |
| Canary gate | write canary는 별도 승인 gate 이후 public-safe synthetic data로만 실행한다. |
| Validation | k3s live apply 전에는 dry-run, backup/rollback, postcheck 기준이 필요하다. |
| Tooling | Python 실행과 test는 가능한 경우 `uv`를 우선한다. |
| Documentation freshness | Kubernetes, Tailscale, k3s 최신 사용법은 공식 문서 기준으로 확인한다. |

## 사용자 시나리오

- 운영자는 현재 Ubuntu compose runtime을 유지한 채, k3s 이관 요구사항과 승인 gate를
  문서로 먼저 확정한다.
- 개발자는 `requirements.md` 승인 전에는 설계와 구현을 시작하지 않는다.
- 리뷰어는 이번 작업이 architecture-modernization branch의 `1cb04e8`과 별개이며,
  k3s 배포 구현이 아직 시작되지 않았음을 문서에서 확인한다.

## Non-Goals

- architecture-modernization M1/M2 구현
- Repository extraction
- Graphiti/Neo4j product roadmap 변경
- OCI staging/canary/DR 구현 또는 설계
- removed legacy external-memory surface 재도입
- `design.md` 승인 전 k3s manifest, Helm chart, deploy script 작성
- 명시 승인 전 live runtime 변경, secret 변경, k3s apply, compose 중단

## 미결정 항목

- 없음. `requirements.md` 승인 후 Phase 2에서 접근안을 비교한다.
