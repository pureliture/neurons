# Neurons k3s Scale-Out Capability Requirements

## 승인 대상

- Source of truth: `requirements.md`
- Preview companion: `requirements.html` (필요 시 생성)
- 상위 컨텍스트: `docs/specs/2026-06-27-neurons-k3s-migration/`(단일 노드 이전 계약). 본 문서는
  그 위에 얹는 **scale-out capability** 계약이며, 단일 노드 cutover를 대체하지 않는다.

## 배경

origin/main 기준 현재 k3s 작업은 **단일 노드 lift-and-shift PoC**다. 수평 scale-out은
코드·계약·운영 정책 세 레이어 모두에서 차단되어 있다.

- `infra_baseline.py`의 `_deployment_resource()`가 모든 canary Deployment를 `replicas: 1`로
  하드코딩한다.
- `workload-inventory.yaml`이 ingress-worker를 `single-consumer worker lane`으로 계약화한다.
- canary plan이 stateful workload를 코드 레벨 `ValueError`로 거부하고 Deployment 외 kind를 불허한다.
- scale-out 객체(HPA, PDB, StatefulSet, topologySpreadConstraints)는 하나도 없다.

본 작업의 목표는 이 capability를 **public contract + manifest generator + 테스트**에 도입하되,
실제 다중 replica 운영값은 private `neurons-ops` overlay로 위임하고, public/private 경계와
기존 안전 게이트를 깨지 않는 것이다.

## 질문-답변 흐름 (self-grilled)

### Q1: scale-out의 현실적 범위는? 전체(stateful 클러스터링 포함)인가, 일부인가?

stateless 워크로드 수평확장 + worker lane competing-consumer 경로 + manifest/계약/분류/테스트에
capability 도입까지를 in-scope로 한다. stateful 스토어(CouchDB/Postgres/Neo4j/Qdrant/NATS)의
실제 클러스터링·복제는 별도 이니셔티브로 분리(non-goal)한다. 근거: stateful 클러스터링은 각
스토어마다 별도 토폴로지·백업·복원 리허설이 필요한 대형 작업이고, YAGNI상 capability 도입과
분리해야 한다.

### Q2: 워크로드는 scale-out 관점에서 어떻게 나뉘는가?

`workload-inventory.yaml`의 `workloads:` 리스트는 **14개 entry**다(`retired-java-ingress-worker`
포함). 14개 모두에 `scaleCategory`를 부여한다. 4개 카테고리로 확정한다.

- `horizontally-scalable` (Deployment + HPA 대상): **ingress-api, vertex-wrapper, mcp-http**.
  단 mcp-http는 `network_mode: host` 제거 + `--allow-kubernetes-pod-ip` 경로 전환(코드 이미 존재)이
  선행조건이며, host networking은 Deployment replicas>1과 포트 충돌하므로 그 전까지 `replicaPolicy:
  single`로 고정한다. ingress-api, vertex-wrapper는 `replicaPolicy: ops-defined`.
- `serialized-worker` (동시성 보호로 단일 Pod 고정, `replicaPolicy: single`): **ingress-worker,
  graph-trigger, bulk-semantic-trigger, session-memory-worker**. graph/bulk/session은 `fcntl.flock`
  또는 파일 기반 직렬화, ingress-worker는 WorkQueue 단일 consumer 계약(Q3)으로 단일 고정.
- `singleton-stateful` (StatefulSet single-writer, scale-out 비대상, `replicaPolicy: singleton`):
  **ingress-queue(nats), couchdb-source-store, ledger-postgres, neo4j-graph-store,
  searchable-mirror(qdrant)**.
- `not-a-target` (`replicaPolicy: singleton`): **llm-brain-tools**(operator exec 전용 `sleep infinity`),
  **retired-java-ingress-worker**(은퇴 profile, k3s 비대상).

### Q3: ingress-worker `single-consumer` 계약을 깨야 하는가?

깨지 않는다. 그리고 이번 작업에서 다중 replica worker를 활성화하지 않는다. 근거(아키텍처 리뷰로 정정):

- live stream `RAG_INGRESS_QUEUE`는 **WorkQueue retention**이고, 현재 worker 코드(`shadow_worker.py`)는
  live durable 진입 시 `delete_consumer` 후 단일 consumer를 재생성하는 **takeover 모델**이다. 다중 client
  fan-out 모델로 구현되어 있지 않다.
- 코드 주석/계약(`compose.yaml`, `workload-inventory.yaml`, `README.md`)은 명시적으로 "이 durable의
  consumer는 하나"이며 다른 consumer와 동시 실행을 금지한다. compose worker와 k3s worker가 동일 live
  durable을 동시에 물면 메시지를 나눠가져 canary 안전성이 깨진다(abort 조건).
- 따라서 ingress-worker를 `competing-consumer`로 라벨하지 않는다. 활성화할 수 없는 capability 라벨은
  오해를 부르므로 `serialized-worker / single`로 둔다.

worker 다중 replica는 다음이 **모두** 완료된 별도 후속 작업의 대상이며, 이번 design의 명시적 non-goal이다.

1. WorkQueue retention → limits/interest retention 또는 consumer fan-out 모델로 stream/consumer 재설계.
2. pod-local SQLite `IngestStateStore`(`INGEST_STATE_DB_PATH`)를 공유 store(Postgres 권장) 또는
   ReadWriteMany PVC로 이전.
3. consumer `ackWait`(P99×2 이상)·`maxAckPending`(N×fetch_batch×4 이상) 튜닝.

이번 작업은 위 선행조건을 계약 문서에 명문화만 하고, ingress-worker `replicaPolicy`는 `single`을 유지한다.

### Q4: stateful 워크로드는 어떻게 다루는가?

`singleton-stateful`로 분류하고 StatefulSet + headless Service + `volumeClaimTemplates`(Pod당 독립
PVC) 형태의 single-writer로 매니페스트 스키마만 도입한다. 실제 replicas는 1로 고정하며, 다중 replica
클러스터링은 비목표다. canary plan의 stateful 거부 guard는 유지하고, scale-out manifest 생성은 별도
경로로 분리해 canary 안전성을 침범하지 않는다.

### Q5: HPA/PDB/affinity를 manifest generator에 넣는가? 정수 노출은?

넣는다. 단 public 산출물에는 **정책 문자열/스키마만** 담고 정수 카운트·target utilization은 절대
포함하지 않는다. HPA `minReplicas/maxReplicas`, PDB `minAvailable/maxUnavailable`의 실제 값,
HPA target utilization, 노드 selector/taint는 private overlay 소유다. canary 기본 경로는 기존대로
`replicas: 1`을 유지하고, scale-out 매니페스트(HPA/PDB/StatefulSet/podAntiAffinity)는 별도 생성
함수로 분리한다.

### Q6: 기존 게이트(canary/24h/backup-restore/single-node cutover)와의 관계는?

scale-out은 단일 노드 cutover(Gates 0~6) 완료 **이후**의 후속 capability다. 본 작업은 capability
도입(코드/계약/테스트/문서)이고, 실제 multi-node 가동·HPA 활성화·다중 replica live apply는 private
overlay와 명시적 승인 게이트 뒤에 둔다. 기존 단일 노드 이전 계약과 안전 게이트는 그대로 보존한다.

### Q7: public/private 경계에서 무엇이 어디로 가는가?

- public neurons: workload `scaleCategory`/`replicaPolicy` 정책 라벨, HPA/PDB/StatefulSet/affinity
  매니페스트 스키마, infra_baseline 생성 함수, contract/단위 테스트, redacted runbook.
- private neurons-ops: 실제 replica 정수, 노드 spec/label/taint, PVC 크기, storageClass, HPA target
  값, CNI 선택(`--flannel-backend=none` + Calico/Cilium 여부), agent node join token(`K3S_TOKEN`),
  Tailscale route/ACL.

### Q8: TDD 전략은?

code-changing work(`infra_baseline.py`, contract test, 단위 test)는 red→green→refactor를 기본으로
한다. 계약/문서 변경은 `K3sMigrationContractTest`와 `test_separation_manifest`의 fail-closed 게이트로
검증한다.

## 기능 요구사항

- FR1: `workload-inventory.yaml`의 `workloads:` **14개 entry 각각**에 `scaleCategory`(enum:
  horizontally-scalable, serialized-worker, singleton-stateful, not-a-target)와 `replicaPolicy`(string:
  ops-defined, single, singleton) 필드를 추가한다. 정수 replica 값은 넣지 않는다.
- FR2: `infra_baseline.py`의 `_deployment_resource()`에 `replica_policy` 파라미터를 도입한다. canary
  기본값은 `replicas: 1`을 유지하고, policy가 `ops-defined`일 때 `replicas` 필드를 생략하고 annotation
  정책 마커를 부여한다(overlay가 strategic-merge patch로 실제 정수 주입). 기존 canary 호출부는 기본값으로
  동작 불변.
- FR3: scale-out 매니페스트 생성 함수를 추가한다 — HPA(autoscaling/v2 skeleton, 정수 target 비포함),
  PDB(`minAvailable`/`maxUnavailable` 스키마), StatefulSet(headless Service + volumeClaimTemplates
  스키마), podAntiAffinity(`topologyKey: kubernetes.io/hostname`, preferred 기본). 산출물은
  manifest-bundle seam의 **key-scoped 정수 가드**를 통과해야 한다(FR6).
- FR4: ingress-worker 다중 replica 선행조건(WorkQueue retention/consumer fan-out 모델 전환, 공유 state
  store, ackWait/maxAckPending 튜닝)을 계약 문서와 inventory 주석에 명문화한다. ingress-worker는
  `serialized-worker / replicaPolicy: single`로 둔다.
- FR5: `ops-overlay-contract.yaml`의 `requiredPrivateInputs`에 `agentNodeJoin`, `cniSelection`,
  `replicaCounts`, `hpaTargets`, `nodeSpecs`를 추가한다(모두 `publicSafe: false`).
- FR6: 정수 용량값 누출을 **key-scoped**로 차단한다. `ensure_public_safe`(문자열 path/secret 전용)는
  오버로드하지 않고, manifest-bundle seam에 별도 함수 `reject_capacity_integers(...)`를 추가해
  `replicas`/`minReplicas`/`maxReplicas`/`averageUtilization`/`minAvailable`/`maxUnavailable` 키의
  두 자리 이상 정수만 거부한다(`containerPort` 8080·6333 등 합법 정수 false-positive 방지).
- FR6b: contract 테스트 분담 — **Python `test_infra_baseline`**가 생성 매니페스트(HPA/PDB/StatefulSet/
  Deployment)의 정수 누출·구조·public-safe를 검증한다(생성기 출력은 Python에만 존재). **Java
  `K3sMigrationContractTest`**는 static YAML(`workload-inventory.yaml` 등 고정 파일 집합)만 가드 —
  `scaleCategory` 커버리지, `doesNotMatch("replicas:\\s*[0-9]{2,}")`. README/base 파일도 기존
  forbidden-substring 어서션 대상이므로 용량 숫자 prose를 넣지 않는다.
- FR7: 기존 5개 Java contract 테스트와 Python `test_infra_baseline`/`test_separation_manifest`를
  green으로 유지한다.
- FR8: scale-out design 문서와 redacted runbook을 추가하고, 추가되는 모든 파일을 **동일 커밋에서 staged**
  상태로 `separation-manifest.json`에 분류한다(`test_separation_manifest`는 `git ls-files` 기반이라
  staged 전엔 미검출). runbook이 노드 용량을 언급하면 `docs/**` public catch-all 대신 명시
  `sanitize-then-public / replace-text` 규칙을 둔다 — 이는 fail-closed 테스트가 잡지 못하는 수동 판단이다.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| Public/private 경계 | public 산출물에 정수 replica·노드스펙·HPA target·secret 부재. 기존 forbidden-public-data 규칙 준수 |
| 안전 게이트 보존 | 단일 노드 cutover Gates 0~6, 24h safety window, backup/restore 리허설, canary durable isolation 불변 유지 |
| Live mutation | 실제 multi-node 가동·HPA 활성화·다중 replica live apply는 private overlay + 명시 승인 뒤. 본 작업은 코드/계약/문서까지만 |
| 테스트 | code-changing은 TDD-first. 기존 테스트 회귀 0 |
| CNI 집행 | flannel 기본은 NetworkPolicy 미집행 — 주석/계약에 명시, CNI 선택은 private overlay |
| 검증 명령 | `JAVA_HOME=... gradle test`, `cd worker && uv run pytest -q` 통과 |

## 사용자 시나리오

- S1: 운영자가 단일 노드 cutover를 완료한 뒤, ingress-api/vertex-wrapper/mcp-http를 private overlay에서
  `replicas: N`으로 올리고 HPA를 활성화해 부하에 따라 수평확장한다. public contract는 이를 정책
  라벨/스키마로만 표현하고 실제 값은 overlay에 둔다.
- S2: 운영자가 ingress-worker 처리량을 늘리려 할 때, 계약 문서가 선행조건(WorkQueue retention/fan-out
  모델 전환 + 공유 state store + ack 튜닝)을 명시해, 그 전에는 `replicaPolicy: single`로 다중 replica를
  막는다.
- S3: 기여자가 실수로 `workload-inventory.yaml`에 `replicas: 12`를 넣으면 새 contract 테스트가
  CI에서 실패해 production 용량 정보 노출을 차단한다.
- S4: stateful 스토어는 StatefulSet single-writer로 유지되어 Deployment replicas>1로 인한 PVC mount
  경합·split-brain이 발생하지 않는다.

## 미결정 항목

- worker SQLite→Postgres 마이그레이션의 구체 시점(별도 후속 작업으로 분리하되 우선순위 미정).
- stateful 스토어 클러스터링 이니셔티브의 착수 시점(완전 별도 spec).
- HA control plane(server 노드 3개 embedded etcd) 도입 여부 — scale-out과 독립한 별도 요구사항.

## Non-Goals

- stateful 스토어(CouchDB/Postgres/Neo4j/Qdrant/NATS-JetStream) 실제 클러스터링/복제 구현.
- ingress-worker 다중 replica 활성화 및 그 선행조건 구현 — WorkQueue retention/consumer fan-out 모델
  전환, SQLite→Postgres state store 마이그레이션, ack 튜닝(전부 별도 후속 작업).
- multi-node 클러스터 실제 가동, HA control plane(3-server etcd), 실제 CNI 교체.
- live HPA 활성화, 다중 replica live apply, 실제 노드 join 실행.
- OCI/원격 클러스터 확장(상위 k3s-migration 문서에서 이미 non-goal).
