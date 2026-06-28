# Single-Goal k3s Cutover Control

이 문서는 `neurons` k3s 전체 전환을 하나의 single agentic-execution goal로 실행하기 위한
control artifact다. 목표는 goal을 여러 작은 승인 흐름으로 흩뜨리지 않고, 하나의 실행 루프 안에서
gate별 evidence를 쌓아 cutover 또는 rollback 결론까지 도달하는 것이다.

## Goal Objective

`neurons`를 local Ubuntu compose primary에서 local Ubuntu k3s primary로 전환한다. 전환은
public `neurons` contract와 private `neurons-ops` overlay를 결합해 진행하며, backup/restore
rehearsal, WorkQueue isolation, NetworkPolicy, read/write canary, rollback proof, postcheck,
compose retire까지 같은 goal 안에서 완료한다.

## Already Done

- k3s namespace `neurons` exists.
- Public contract ConfigMap canary is applied.
- No workload, pod, service, statefulset, or secret has been created by this public contract.

## Gate 0 — Preflight Revalidation

Done evidence:

- current git worktree and branch are verified
- `requirements.md`, `design.md`, public contract, and this control file are current
- live namespace and public contract ConfigMap are present
- no unexpected workload/pod/secret exists in the target namespace

Abort condition:

- any unexpected workload or secret exists before the private overlay is introduced

## Gate 1 — Private Ops Overlay Readiness

Required `neurons-ops` inputs:

- immutable image references or build provenance
- env/config bindings
- secret references, not raw secret values
- persistent storage plan
- Tailscale route and access policy
- kube-apiserver operator allowlist
- NetworkPolicy expectations
- WorkQueue isolation mode
- backup/restore rehearsal commands and evidence path
- rollback and compose retire approval record shape

Stop and ask if any real secret value, private path dump, or raw environment dump is required to
continue. Do not mark complete with missing private overlay inputs.

## Gate 2 — Backup/Restore Rehearsal

Required stores:

- CouchDB
- Postgres ledger
- Neo4j
- Qdrant when its source profile is enabled
- NATS and worker/runtime state where migration is required

Done evidence:

- restore rehearsal passes against an isolated target
- redacted counts and readiness checks are recorded
- rollback viability is recorded

Abort condition:

- restore cannot be proven
- evidence leaks private data

## Gate 3 — WorkQueue Isolation and Network Boundary

Required controls:

- WorkQueue isolation uses shadow stream, separate durable, or worker-disabled health-only mode
- compose worker and k3s worker never share the same live durable
- kube-apiserver access has an operator allowlist
- NetworkPolicy expectations exist before promotion
- personal tailnet-wide route access is explicitly accepted by the owner
- direct stateful service access is not the normal operator path

Stop and ask if the only available worker canary path would share the compose live durable.

## Gate 4 — Workload Canary Apply

Allowed only after Gates 0-3 pass.

Done evidence:

- server dry-run passes
- live apply is approved
- workloads become ready
- no secret values are printed
- worker behavior is isolated from live durable consumption
- health/API/MCP/stateful readiness checks pass

Abort condition:

- canary exceeds the 24h safety window without promotion or rollback decision
- canary fails readiness or stateful dependency checks

## Gate 5 — Read/Write Canary

Allowed only after explicit approval.

Done evidence:

- public-safe synthetic data is used
- write path is bounded and reversible
- readback succeeds
- redacted evidence is captured

Abort condition:

- real private transcript/source data would be required
- read/write canary cannot be isolated from production state risk

## Gate 6 — Cutover, Postcheck, Rollback, Compose Retire

Done evidence:

- cutover approval is recorded
- k3s becomes primary
- postcheck passes
- rollback path remains available through the safety window
- compose retire approval is recorded
- compose is retired only after postcheck and approval

Abort condition:

- rollback path cannot be proven
- postcheck fails
- safety window expires without decision

## Completion Rule

do not mark complete until every Gate 0-6 item has direct evidence. Passing tests, green manifests,
or a successful public contract canary alone is not enough. The goal completes only when k3s is the
verified primary runtime and compose retire has either completed with approval or has an explicit
approved deferral recorded inside the same goal.

## Regression Rule

If reality contradicts `requirements.md` or `design.md`, stop and ask. Do not silently change the
source of truth inside the cutover goal.
