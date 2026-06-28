# Neurons k3s Scale-Out Runbook (Redacted)

이 runbook은 public-safe redacted 절차다. 실제 replica 수·HPA target·노드 spec·storage 크기·
Tailscale route는 private `neurons-ops` overlay가 소유하며 여기에 적지 않는다. public 산출물에는
어떤 capacity count도 넣지 않는다.

## 선행조건

- 단일 노드 cutover(Gates 0~6)가 완료되고 k3s가 primary다.
- backup/restore rehearsal evidence가 존재한다.
- private overlay에 `replicaCounts`/`hpaTargets`/`nodeSpecs`/`cniSelection`/`agentNodeJoin`이
  채워져 있다.

## 절차 (per workload, dry-run first)

1. `workload-inventory.yaml`의 `scaleCategory`/`replicaPolicy`를 확인한다.
2. `horizontally-scalable` workload만 HPA/다중 replica 대상이다. `serialized-worker`와
   `singleton-stateful`은 단일 인스턴스를 유지한다. `mcp-http`는 host networking 제거 전까지 단일
   replica다.
3. private overlay가 public skeleton(Deployment/HPA/PDB/StatefulSet)에 실제 count를
   strategic-merge patch로 주입한다.
4. agent node를 join한다(server URL/token은 private overlay 소유).
5. NetworkPolicy 집행이 필요하면 CNI를 결정한다(flannel 기본 backend는 정책을 집행하지 않는다).
6. client dry-run → server dry-run(explicit approval) → apply → rollout postcheck.
7. 실패 시 replicas를 0으로 내리고 compose primary로 rollback한다.

## 차단 조건 (abort)

- ingress-worker를 WorkQueue/shared-store 선행조건 없이 다중 replica로 올리는 것.
- singleton-stateful workload를 Deployment 다중 replica로 올리는 것(PVC 경합/split-brain).
- public 산출물에 실제 capacity count를 적는 것.
- safety window 초과(상위 k3s-migration gate 준수).
