# Live Namespace and Public Contract Canary Evidence

## Scope

- Target namespace: `neurons`
- Applied artifact: `deploy/k3s/public-contract/base`
- Operation class: live namespace creation and public contract canary apply
- Workload canary: not included in this artifact set

## Applied Objects

- `Namespace/neurons`
- `ConfigMap/neurons-k3s-public-contract`

Kubernetes also materialized the standard namespace root CA ConfigMap. No application workload
object was applied.

## Verified Contract Values

- `primary-runtime`: `local-ubuntu-k3s`
- `max-canary-window-hours`: `24`
- `workqueue-isolation-gate`: `shadow-stream-or-separate-durable-required`
- namespace public contract label: `true`

## Postcheck

- Services in `neurons`: `0`
- Deployments in `neurons`: `0`
- StatefulSets in `neurons`: `0`
- Pods in `neurons`: `0`
- Secrets in `neurons`: `0`
- Public contract ConfigMap exists.

## Safety State

- No workload was deployed.
- No secret was created or changed.
- No compose service was stopped or changed.
- No stateful service was created.
- No queue consumer was started.
- No Neo4j, CouchDB, Qdrant, Postgres, NATS, or removed legacy external-memory state was mutated.

## Remaining Gates

- Workload canary apply remains blocked until private ops overlay supplies image references,
  secret references, NetworkPolicy expectations, WorkQueue isolation mode, backup/restore evidence,
  and explicit live apply approval.
- compose retire remains blocked.
