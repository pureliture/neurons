# k3s Dry-Run Evidence

## Scope

- Target artifact: `deploy/k3s/public-contract/base`
- Runtime: local Ubuntu k3s control plane
- Operation class: client dry-run and server dry-run only
- Live mutation: none

## Evidence

- Remote Kubernetes client was available and reported k3s `v1.36.2+k3s1`.
- Target namespace did not exist before dry-run.
- Client dry-run accepted the public `Namespace` and `ConfigMap` contract artifacts.
- Server dry-run accepted the target `Namespace` artifact.
- Server dry-run rejected the exact target `ConfigMap` because the target namespace does not exist.
  This is expected while live namespace creation is not approved.
- Server dry-run accepted the same `ConfigMap` schema against an existing non-target namespace for
  API validation only.
- Postcheck confirmed the target namespace was still absent.
- Postcheck confirmed the schema-validation `ConfigMap` was not created.

## Safety State

- No `kubectl apply` without dry-run was executed.
- No live namespace was created.
- No live configmap was created.
- No secret was created or changed.
- No compose service was stopped or changed.
- No Neo4j, CouchDB, Qdrant, Postgres, NATS, or removed legacy external-memory state was mutated.

## Remaining Gates

- Server dry-run of namespaced target objects requires the target namespace to exist or a later
  approval to create it.
- Live apply remains blocked.
- Secret/config mutation remains blocked.
- k3s canary live apply remains blocked.
- compose retire remains blocked.
