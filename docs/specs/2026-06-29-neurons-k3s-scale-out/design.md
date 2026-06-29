# Neurons k3s Scale-Out Capability Design Spec

## Overview

origin/main의 단일 노드 k3s lift-and-shift PoC 위에 **수평 scale-out capability**를 도입한다. 핵심은
(1) 14개 워크로드를 scale-out 카테고리로 분류하고, (2) `infra_baseline.py` manifest generator에
HPA/PDB/StatefulSet/podAntiAffinity 생성 경로와 `replica_policy` 파라미터를 추가하며, (3) 실제 운영
정수값(replica 수·노드스펙·HPA target·CNI)은 전부 private `neurons-ops` overlay로 위임하고, (4) 기존
public/private 경계와 단일 노드 cutover 안전 게이트를 깨지 않는 것이다. 본 작업은 capability(코드·계약·
테스트·문서)까지이며 live multi-node 가동은 별도 승인 게이트 뒤다.

> 이 design은 아키텍처 리뷰 게이트(opus)의 6개 blocking fix를 반영해 정정한 버전이다. 특히
> ingress-worker는 NATS WorkQueue retention + 현행 takeover 모델 때문에 `competing-consumer`가 아니라
> `serialized-worker / single`로 분류한다(아래 C1·Error Handling 참조).

## Requirements Reference

- Phase 1 source: `requirements.md`
- 상위 컨텍스트: `docs/specs/2026-06-27-neurons-k3s-migration/`
- 핵심 FR: 워크로드 분류(FR1), `_deployment_resource` 파라미터화(FR2), scale-out 매니페스트 생성(FR3),
  worker 선행조건 명문화(FR4), private overlay 계약 확장(FR5), key-scoped 정수 가드 + 테스트 분담
  (FR6/FR6b), 회귀 0(FR7), staged 파일 분류(FR8).

## Architecture

```
[public neurons repo]
  deploy/k3s/public-contract/
    workload-inventory.yaml      ← scaleCategory / replicaPolicy 필드 (14 entry, 정수 금지)
    ops-overlay-contract.yaml    ← agentNodeJoin/cniSelection/replicaCounts/hpaTargets/nodeSpecs (private)
    base/config-contract.yaml    ← replica-policy: ops-defined 정책 키
    base/kustomization.yaml      ← 신규 scale-out resource 등록(있을 경우)
  worker/lib/agent_knowledge/llm_brain_core/
    infra_baseline.py            ← _deployment_resource(replica_policy=...) + scale-out 함수군
                                   + reject_capacity_integers() (신규, key-scoped)
  src/test/java/.../K3sMigrationContractTest.java   ← static YAML 가드 (no-integer-replica/category)
  worker/tests/test_infra_baseline.py               ← 생성 매니페스트 public-safe/구조 테스트
  worker/tests/test_separation_manifest.py          ← 신규 파일 분류 fail-closed (staged 필요)
  deploy/k3s/README.md                              ← scale-out 섹션 (forbidden-substring 어서션 대상)
  docs/specs/2026-06-29-neurons-k3s-scale-out/      ← requirements/design + redacted runbook
  deploy/separation/separation-manifest.json        ← 신규 파일 분류

[private neurons-ops repo] (본 작업 범위 밖, 계약으로만 참조)
  실제 replicas 정수, 노드 spec/label/taint, PVC 크기, storageClass,
  HPA target utilization, CNI 선택, K3S_TOKEN, Tailscale route/ACL
```

경계 원칙: **public은 정책 라벨·스키마·생성기·테스트만, 정수 운영값은 0개**.

## Data Flow

```
워크로드 분류 (scaleCategory, 14 entry)
        │
        ├── horizontally-scalable → Deployment(replica_policy=ops-defined → replicas 생략 + annotation)
        │                            + HPA(skeleton) + PDB(maxUnavailable) + podAntiAffinity(preferred)
        │     (mcp-http: host networking 제거 전까지 replicaPolicy=single)
        │
        ├── serialized-worker → Deployment(replicas:1 fixed) + PDB(minAvailable:1)
        │     (ingress-worker / graph-trigger / bulk-semantic-trigger / session-memory-worker)
        │
        ├── singleton-stateful → StatefulSet(volumeClaimTemplates schema) + headless Service
        │                         + PDB(minAvailable:1)
        │
        └── not-a-target → 매니페스트 미생성 (llm-brain-tools, retired-java-ingress-worker)
                                  │
                          reject_capacity_integers (key-scoped 정수 누출 차단)
                                  │
                          private overlay(kustomize patch)가 실제 정수 주입 → kubectl apply (게이트 뒤)
```

기존 canary 경로(`k3s_poc_canary_manifest_bundle`)는 **변경 없이** `replicas: 1`을 유지한다. scale-out
매니페스트는 **별도 함수**로 분리해 canary guard(stateful 거부)를 침범하지 않는다.

## Component Details

### C1. workload-inventory.yaml (분류 필드 추가)

- 입력: 기존 14개 `workloads:` entry. 출력: 각 entry에 `scaleCategory` + `replicaPolicy` 추가.
- 의존성: `K3sMigrationContractTest.workloadInventoryCoversComposeOwnedServicesAndExclusions`
  (scalar 필드 추가에는 깨지지 않음 — indent-tracking 파서가 list 키만 수집).
- 분류 매핑(아키텍처 리뷰 반영):

  | id | scaleCategory | replicaPolicy | 비고 |
  | --- | --- | --- | --- |
  | ingress-api | horizontally-scalable | ops-defined | 인프로세스 IdempotencyStore는 NATS dedup이 보완 |
  | vertex-wrapper | horizontally-scalable | ops-defined | stateless HTTP proxy |
  | mcp-http | horizontally-scalable | single | host networking 제거 + `--allow-kubernetes-pod-ip` 전까지 single |
  | ingress-worker | serialized-worker | single | WorkQueue 단일 consumer + takeover 모델 |
  | graph-trigger | serialized-worker | single | `fcntl.flock` graph-project.lock |
  | bulk-semantic-trigger | serialized-worker | single | 동일 lock 직렬화(no concurrent Neo4j write) |
  | session-memory-worker | serialized-worker | single | 파일 기반 approval/watermark 직렬화 |
  | ingress-queue(nats) | singleton-stateful | singleton | 단일 노드 JetStream |
  | couchdb-source-store | singleton-stateful | singleton | 단일 NODENAME |
  | ledger-postgres | singleton-stateful | singleton | 단일 primary write |
  | neo4j-graph-store | singleton-stateful | singleton | Community Edition, 클러스터 불가 |
  | searchable-mirror(qdrant) | singleton-stateful | singleton | profile-gated 단일 |
  | llm-brain-tools | not-a-target | singleton | operator exec 전용 |
  | retired-java-ingress-worker | not-a-target | singleton | 은퇴 profile, k3s 비대상 |

- 제약: 정수 replica 값 금지. `replicaPolicy`는 문자열 정책 라벨만.

### C2. infra_baseline.py (manifest generator 확장)

- `_deployment_resource(*, name, namespace, image, container_port, replica_policy="canary")`:
  `"canary"`이면 기존대로 `replicas: 1`. `"ops-defined"`이면 `replicas` 키를 **생략**하고
  `metadata.annotations["neurons.scale/replica-policy"] = "ops-defined"` 부여(overlay strategic-merge
  patch가 정수 주입). 기존 canary 호출부는 기본값으로 동작 불변.
- 신규 생성 함수(정수 미포함):
  - `_hpa_resource(name, namespace)` — autoscaling/v2, `scaleTargetRef`만 채우고 `minReplicas`/
    `maxReplicas`/`metrics[].resource.target`은 annotation 마커(`neurons.scale/hpa: ops-defined`)로 두고
    정수 미포함. 실제 값은 overlay patch.
  - `_pdb_resource(name, namespace, mode)` — `mode="minAvailable"`(stateful/serialized) 또는
    `"maxUnavailable"`(stateless). 단자리 `1`만 허용(정책 불변식).
  - `_statefulset_resource(name, namespace, image, container_port)` — `serviceName`(headless),
    `replicas: 1`, `volumeClaimTemplates` 스키마(storageClassName/size는 overlay 마커).
  - `_headless_service_resource(name, namespace)` — `clusterIP: None`.
  - `_pod_anti_affinity(name)` — `preferredDuringSchedulingIgnoredDuringExecution`,
    `topologyKey: kubernetes.io/hostname`(single-node Pending 방지 위해 required 아님).
  - `scale_out_manifest_bundle(*, workloads, namespace, access_policy)` — 각 워크로드 scaleCategory에
    따라 위 리소스를 조합. `not-a-target` 제외, singleton-stateful은 StatefulSet single-writer로만 생성.
- `reject_capacity_integers(resource: dict)` — **신규, key-scoped 가드**. `ensure_public_safe`(문자열
  path/secret 전용, `public_safe_util.py`)는 **오버로드하지 않는다**(qdrant_mirror 등 공유 호출부의
  포트 정수 false-positive 위험). 새 함수는 `replicas`/`minReplicas`/`maxReplicas`/`averageUtilization`/
  `minAvailable`/`maxUnavailable` 키의 값이 두 자리 이상 정수면 거부한다. `containerPort`(8080·6333·
  7687·8765 등)는 검사 대상 키가 아니므로 통과. `scale_out_manifest_bundle`이 반환 직전 모든 리소스에
  `reject_capacity_integers`와 기존 `ensure_public_safe`를 함께 적용.

### C3. ops-overlay-contract.yaml (private 계약 확장)

`requiredPrivateInputs`에 추가(모두 `publicSafe: false`):
- `agentNodeJoin` — K3S_URL/K3S_TOKEN, node hostname 유일성, K3S_NODE_NAME.
- `cniSelection` — flannel 유지 vs `--flannel-backend=none` + Calico/Cilium(NetworkPolicy 집행 필요 시).
- `replicaCounts` — 워크로드별 실제 replicas/HPA min·max.
- `hpaTargets` — target CPU/메모리/커스텀 메트릭 값.
- `nodeSpecs` — 노드 label/taint, resource requests/limits.

### C4. base/config-contract.yaml + base/kustomization.yaml

- `config-contract.yaml`: 정책 키 `replica-policy: ops-defined` 추가(ConfigMap, 정수 없음).
- `kustomization.yaml`: scale-out resource를 tracked YAML로 둘 경우 `resources`에 등록. (생성기 출력은
  in-memory이므로, 별도 tracked manifest를 두지 않으면 kustomization 변경 불필요 — M2에서 확정.)

### C5. 문서 + separation-manifest 분류

- `docs/specs/2026-06-29-neurons-k3s-scale-out/requirements.md`, `design.md` — `docs/**` catch-all로
  public 분류(자동).
- redacted `scale-out-runbook.md` — 노드 용량 언급 시 `sanitize-then-public / replace-text`로
  `separation-manifest.json`에 **명시 분류**(catch-all은 coverage만 만족시키고 disposition 정확성은
  강제 못 함 — 수동 판단).
- `deploy/k3s/README.md` scale-out 섹션 — `readPublicK3sArtifacts()` forbidden-substring 어서션 대상.
  용량 숫자·private path·`kind: Secret` prose 금지.
- 신규 worker/src/test 파일 — `worker/**`/`src/**` catch-all로 자동 분류. **동일 커밋에서 staged**
  상태여야 `test_separation_manifest`(git ls-files 기반)가 검출한다.

## Error Handling

- 정수 replica 누출: (Python) `reject_capacity_integers`가 key-scoped로 차단 + (Java)
  `doesNotMatch("replicas:\\s*[0-9]{2,}")`가 static YAML에서 차단. canary `replicas: 1`은 단자리라
  false-positive 없음. 포트 정수는 검사 키가 아니라 통과.
- 신규 파일 미분류: `test_every_tracked_file_is_classified`가 fail-closed로 차단(staged 후 검출).
  단 이 테스트는 *coverage*만 검증하고 disposition *correctness*는 검증하지 못하므로, runbook의
  sanitize 분류는 수동 판단으로 보장한다.
- stateful 다중 replica 오용: `scale_out_manifest_bundle`이 singleton-stateful을 StatefulSet
  single-writer로만 생성. Deployment replicas>1 경로 자체를 제공하지 않음.
- worker 다중 replica 조기 활성화: ingress-worker `replicaPolicy: single` + 계약 주석으로 차단. 코드가
  WorkQueue fan-out/공유 store 없이 다중 replica를 권하지 않음.
- canary 회귀: `scale_out_manifest_bundle`은 canary의 stateful `ValueError` guard(`infra_baseline.py`)와
  분리. 기존 `test_k3s_poc_canary_plan_rejects_stateful_or_production_migration`이 canary 보호 게이트로
  계속 작동(신규 테스트 불필요, 인용만).
- flannel NetworkPolicy 미집행: 생성 NetworkPolicy에 `# flannel default does not enforce this policy`
  주석 + ops-overlay-contract `cniSelection`으로 명시.

## Testing Strategy

- Java(`K3sMigrationContractTest`): **static YAML/문서 파일 집합만** 가드(생성기 출력은 미열람).
  신규 — (a) `workload-inventory.yaml` 등 public 산출물 `doesNotMatch("replicas:\\s*[0-9]{2,}")`,
  (b) 14개 워크로드 `scaleCategory` 존재. 기존 5개 테스트(README/base forbidden-substring 포함) green
  유지 — README scale-out 섹션 편집 시 `kind: Secret`/`/Users/`/`/home/`/용량 숫자 prose 금지.
- Python(`test_infra_baseline`): **생성 매니페스트** 검증(생성기 출력은 Python에만 존재). 신규 —
  (a) `_deployment_resource(replica_policy="ops-defined")`가 `replicas` 키 생략 + annotation 부여,
  (b) `scale_out_manifest_bundle` 산출물에 capacity-key 두 자리 정수 부재(`reject_capacity_integers`),
  (c) HPA/PDB/StatefulSet/headless Service/affinity 구조, (d) affinity가 `preferred`(required 아님),
  (e) `reject_capacity_integers`가 `replicas: 12`는 거부하고 `containerPort: 8080`은 통과. 기존 canary
  테스트(replicas:1, production_migration_implied=False, stateful 거부) green 유지.
- Python(`test_separation_manifest`): 신규 파일 staged 후 3개 테스트 green.
- 통합 검증: `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`, `cd worker && uv run pytest -q`,
  `cd worker && uv run neuron-knowledge --show-boundary`.

## TDD Strategy

code-changing work(`infra_baseline.py`, contract/단위 test)는 red→green→refactor를 기본 실행 전략으로
한다. 먼저 실패하는 테스트를 쓰고(red), 최소 구현으로 통과시킨 뒤(green), 중복/명확성을 정리한다
(refactor). YAML/문서 변경도 Java/Python contract 테스트가 게이트로 작동하므로 동일 흐름에 포함한다.

## Milestones

agentic-execution이 act→observe→adjust 루프로 소비하는 검증 단위. 순서는 의존성 순. 각 마일스톤은
변경 파일을 staged한 상태로 게이트를 평가한다.

- **M1: 워크로드 분류 + static YAML 누출 차단 게이트** — `workload-inventory.yaml` 14개에
  `scaleCategory`/`replicaPolicy` 추가, `config-contract.yaml`에 `replica-policy` 키, `K3sMigrationContractTest`에
  no-integer-replica(static YAML) + 14-category-coverage 어서션 추가.
  - done: 신규 Java 어서션 red→green, 기존 5개 + 신규 green. `gradle test` 통과.
  - evidence: 테스트 출력(redacted), inventory diff.
- **M2: manifest generator scale-out 경로 + 정수 가드** — `_deployment_resource` `replica_policy`,
  `_hpa_resource`/`_pdb_resource`/`_statefulset_resource`/`_headless_service_resource`/`_pod_anti_affinity`/
  `scale_out_manifest_bundle` 추가, `reject_capacity_integers` 신규(key-scoped), `test_infra_baseline`
  신규 테스트. kustomization 변경 필요 여부 확정.
  - done: 신규 Python 테스트 red→green, 기존 canary 테스트(replicas:1·stateful 거부) green.
    `uv run pytest -q` 통과.
  - evidence: pytest 출력(redacted), 생성 매니페스트 샘플(capacity-key 정수 미포함, 포트 정수 통과 확인).
- **M3: private 계약 + 선행조건 명문화** — `ops-overlay-contract.yaml`에 agentNodeJoin/cniSelection/
  replicaCounts/hpaTargets/nodeSpecs 추가, `README.md` scale-out 섹션(용량 숫자 prose 금지), inventory에
  ingress-worker/mcp-http 선행조건 주석.
  - done: contract 테스트 green(README forbidden-substring 포함), README/inventory 일관성.
  - evidence: 계약 diff.
- **M4: 문서 + 파일 분류 + 전체 게이트** — scale-out runbook(redacted) 작성, 신규 파일을 staged 상태로
  `separation-manifest.json`에 분류(runbook은 필요 시 `sanitize-then-public`), 전체 검증 스위트 green.
  - done: `gradle test` + `uv run pytest -q` + `--show-boundary` 통과, `test_separation_manifest` green,
    leak-scan 패턴 위반 0.
  - evidence: 통합 테스트 결과(redacted), manifest 분류 diff.

## Open Questions

- `ensure_public_safe`(`public_safe_util.py`)는 문자열 path/secret만 검사하고 정수를 보지 않음(리뷰 확인).
  따라서 정수 가드는 그 함수가 아니라 별도 `reject_capacity_integers`로 신설한다 — 확정.
- HPA를 public skeleton(annotation 마커)으로 둘지, 아예 overlay-only로 두고 public엔 PDB/StatefulSet/
  affinity만 둘지 — M2에서 `reject_capacity_integers` 통과를 기준으로 최종 결정(기본안: annotation 마커).
- 생성 매니페스트를 tracked YAML로 직렬화해 Java 테스트가 읽게 할지, Python 테스트에만 맡길지 — **해소**:
  Python 전담을 유지하되 `load_scale_out_workloads(inventory)`가 inventory(SoT)를 읽어 분류를 검증하고
  `scale_out_manifest_bundle`이 그 결과를 소비한다. 실제 inventory를 caller로 쓰는 round-trip 테스트
  (`test_inventory_classification_round_trips_to_a_clean_scale_out_bundle`)가 YAML↔코드 drift를
  fail-closed로 차단한다. tracked YAML 직렬화/kustomization 등록은 불필요.
