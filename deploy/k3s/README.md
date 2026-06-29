# Neurons k3s Public Contract

이 디렉터리는 `neurons` public repo가 소유하는 k3s migration contract다. 실제
production overlay, secret/config, Tailscale route/ACL, backup evidence, approval record는
private `neurons-ops` repo가 소유한다.

## Scope

- Target: local Ubuntu primary k3s
- Migration shape: `neurons` repo-owned compose surface 전체
- Cutover shape: compose 유지 + k3s canary, 짧은 safety window 후 compose retire
- Stateful strategy: backup/restore rehearsal first
- Network boundary: Tailscale subnet router with limited routes and access policy

## Public Repo Owns

- Safe workload inventory
- Public/private overlay contract
- Non-secret validation expectations
- Redacted runbooks
- Tests that fail closed when the boundary is weakened

## Private Ops Repo Owns

- Real env files and secret material
- Host-specific k3s overlay values
- Tailscale routes and ACL/grants
- Backup/restore execution records
- Live apply, cutover, rollback, and compose retire approval records

## Hard Gates

- No live apply before dry-run evidence and explicit approval
- No secret mutation before explicit approval
- No compose stop before cutover approval
- No primary promotion before backup/restore rehearsal evidence
- No k3s worker canary may share the live WorkQueue durable with the compose worker
- No safety window may exceed 24h without aborting back to compose primary
- No promotion without kube-apiserver access allowlist and NetworkPolicy expectations
- No public artifact may contain real hostnames, private paths, secret values, raw transcript
  bodies, or raw dataset/document identifiers

## Scale-Out Capability

수평 scale-out은 단일 노드 cutover 완료 이후의 후속 단계다. public contract는
`scaleCategory`/`replicaPolicy` 정책 라벨과 HPA/PDB/StatefulSet/anti-affinity skeleton만
소유하고, 실제 replica 수·HPA target·노드 spec·PVC 크기는 private `neurons-ops` overlay가 채운다.
public 산출물에는 어떤 capacity count도 넣지 않는다.

- `horizontally-scalable` — ingress-api, vertex-wrapper, mcp-http. Deployment + HPA skeleton.
  mcp-http는 host networking 제거와 pod-ip bind 전환 전까지 단일 replica로 고정한다.
- `serialized-worker` — ingress-worker, graph-trigger, bulk-semantic-trigger, session-memory-worker.
  단일 Pod 고정. ingress-worker 다중 replica는 WorkQueue retention/consumer 모델 전환, 공유 state
  store, ack 튜닝이 모두 끝난 별도 후속 작업의 대상이다.
- `singleton-stateful` — nats, couchdb, ledger postgres, neo4j, qdrant. StatefulSet single-writer.
  실제 클러스터링은 별도 이니셔티브로 분리한다.
- `not-a-target` — llm-brain-tools, retired java ingress worker.

flannel 기본 backend는 NetworkPolicy를 집행하지 않는다. 집행이 필요하면 CNI 선택을 private overlay의
`cniSelection`에서 결정한다. agent node join token과 노드 spec도 private overlay가 소유한다.

## Current Artifacts

- `public-contract/workload-inventory.yaml`
- `public-contract/ops-overlay-contract.yaml`
- `public-contract/base/kustomization.yaml`
- `public-contract/base/namespace.yaml`
- `public-contract/base/config-contract.yaml`
- `docs/specs/2026-06-27-neurons-k3s-migration/backup-restore-rehearsal.md`
- `docs/specs/2026-06-27-neurons-k3s-migration/canary-cutover-runbook.md`
- `docs/specs/2026-06-27-neurons-k3s-migration/single-goal-cutover-control.md`
