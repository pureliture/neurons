# Cutover Goal Evidence

## Gate 0 — Preflight Revalidation

- status: done
- evidence:
  - git worktree is `codex/neurons-k3s-migration-spec`; source checkout `main` is clean.
  - live namespace `neurons` exists.
  - public contract ConfigMap `neurons-k3s-public-contract` exists.
  - public contract reports `primary-runtime=local-ubuntu-k3s`.
  - public contract reports `max-canary-window-hours=24`.
  - public contract reports `workqueue-isolation-gate=shadow-stream-or-separate-durable-required`.
  - target namespace has 0 services, 0 deployments, 0 statefulsets, 0 pods, and 0 secrets.
  - `K3sMigrationContractTest` passes.
  - public artifact scan found no target namespace typo, removed legacy platform name, Secret object,
    raw dataset/document id key, or private path pattern.

## Gate 1 — Private Ops Overlay Readiness

- status: in-progress
- evidence:
  - local private ops repository skeleton exists at the expected project location.
  - private overlay readiness manifest passes Kubernetes server dry-run.
  - live `neurons-ops-readiness` ConfigMap exists in namespace `neurons`.
  - readiness status is `incomplete`.
  - current compose service and env key shapes were inspected without printing env values.
  - k3s reports `local-path` as an available storage class.
  - Kubernetes NetworkPolicy API accepts server dry-run for the `neurons` namespace.
  - private overlay manifests now define image provenance, secret reference, storage,
    Tailscale/network, NetworkPolicy, WorkQueue isolation, and backup/restore plan shapes.
  - full private overlay server dry-run passes against the live k3s API.
  - private runbooks define backup/restore rehearsal shape, hard-mutation approval record shape,
    image provenance requirements, and network boundary evidence requirements.
  - Tailscale service is active and self reports online with tailnet addresses present.
  - Tailscale advertised subnet route count is currently zero.
  - current compose profile inventory contains API, worker, NATS, CouchDB, Postgres ledger,
    Neo4j, Vertex wrapper, tools, graph trigger, bulk semantic trigger, MCP, and optional Qdrant.
  - live compose currently has five running repo-owned services.
  - live runtime truth spans multiple compose projects: stateful brain stores/workers, ingress API,
    ingress worker, MCP HTTP, session-memory worker, and Qdrant live surface.
  - env/config inspection was limited to key names and source volume names; no env values were
    printed or recorded.
  - live Qdrant source surface is observed, so Gate 2 must rehearse it or record an explicit
    non-migration decision.
  - private Gate 1 to Gate 2 approval bundle is drafted for image provenance, Secret/config
    binding, backup/restore rehearsal, and network boundary operations.
  - private Gate 2 read-only preflight script, approval-gated rehearsal skeleton, and evidence
    template are present; read-only preflight passed.
  - private Gate 3 WorkQueue read-only preflight script and initial WorkQueue evidence are present.
  - source NATS has one stream and one consumer, with the observed live durable recorded privately.
  - session-memory worker is currently exited and needs a Gate 2 migration/non-migration decision.
  - private Gate 1 image provenance and network boundary read-only preflight scripts are present.
  - private Secret/Config key map draft is present without raw values.
  - Gate 1 image provenance read-only preflight passed; all mapped source images are present, with
    Qdrant still mutable until pinned or excluded.
  - Gate 1 network boundary read-only preflight passed; NetworkPolicy API is present, Tailscale is
    online, and route count remains zero.
  - private node placement plan is present and uses a neutral canary node label for the local
    registry pull path.
  - private ops readiness validator passes.
  - private Gate 4 workload canary preview overlay is present, separated from live readiness
    overlay, and uses `replicas: 0` plus worker-disabled health-only mode.
  - Gate 4 workload canary preview overlay passes Kubernetes server dry-run.
  - private readiness validator checks preview overlay safety.
  - private Gate 5 read/write canary guarded command pack and evidence template are present.
  - private Gate 6 cutover/postcheck and compose retire guarded command packs and evidence
    template are present.
  - Gate 5/6 guarded command packs exit without action when approval env is absent.
  - private gate status matrix, operator approval packet, and resume next-actions handoff are
    present.
  - user approval is recorded as gate-ordered Approval A-F; execution remains blocked by missing
    prior-gate evidence where applicable.
  - Gate 2 Postgres ledger restore rehearsal passed with compose primary unchanged.
  - Gate 2 NATS queue state restore rehearsal passed with compose primary unchanged.
  - Gate 2 worker/runtime volume restore rehearsal passed with compose primary unchanged.
  - Gate 2 CouchDB source store restore rehearsal passed with compose primary unchanged.
  - Gate 2 Neo4j graph store restore rehearsal passed with compose primary unchanged.
  - Gate 2 Qdrant live surface restore rehearsal passed with compose primary unchanged.
  - Gate 1 image import command pack is approval-gated and exits before archive/import when
    elevated operator permission is unavailable.
  - Gate 3 NetworkPolicy live enforcement proof passed with temporary proof pods/policy and
    cleanup returned target namespace resource counts to zero.
  - Gate 1 Secret/Config binding applied with redacted object/key-count postcheck; target
    namespace has no workload, StatefulSet, or NetworkPolicy after the apply.
  - Gate 1 node placement decision recorded for initial health-only canary without tolerations;
    broader stateful rollout still requires node remediation before promotion.
  - Gate 1 Kubernetes operator RBAC allowlist evidence passes for target namespace operations and
    node list checks.
  - Gate 3 Tailscale route command pack exists and requires an explicit narrow route scope before
    any route advertisement.
  - Gate 1 local registry push/pull proof passed for first-party canary images; preview overlay now
    references the proven registry tags in private ops.
  - Gate 4 workload canary preview overlay server dry-run passes with local registry images and a
    neutral node label selector.
  - Gate 4 workload canary live overlay server dry-run passes with NATS canary and proven local
    registry images.
  - Gate 4 apply/delete/postcheck command packs exist and are approval-gated.
  - Gate 4 apply command fails closed before live apply while Gate 3 route approval or access
    evidence is missing; target namespace workload resources remain zero.
  - Gate 4 postcheck is prepared to verify deployment readiness, worker queue isolation, live
    durable absence, and NATS/API/MCP health through a cleaned-up smoke pod.
  - Tailscale candidate route scope was derived from observed k3s pod/service CIDR surfaces.
  - Tailscale route advertisement is applied for the derived route scope, but active route count is
    still zero pending tailnet admin approval.
  - Gate 3 Tailscale route postcheck fails closed while active route approval or access evidence
    is missing.
  - Gate 3 Tailscale route wait command pack is present for read-only polling after route approval
    submission.
  - Gate 3 Tailscale route wait command blocks without access evidence and times out read-only against
    the remote runtime while route approval is pending.
  - Gate 3 access evidence validator accepts a personal-tailnet-wide filled evidence sample and
    rejects public-route evidence before Gate 4 canary apply.
  - Gate 4 live canary apply was reverified to fail closed at Gate 3 prereq after ACL validator
    integration; target namespace workload resources remained zero.
  - Gate 5 read/write canary command pack is ready in plan mode and starts no canary write.
  - Gate 5 execute mode requires Gate 4 workload canary postcheck to pass before a public-safe
    synthetic write.
  - Gate 6 cutover and compose retire command packs are ready in plan mode and start no cutover or
    compose stop.
  - Gate 6 postcheck/retire verify modes require Gate 5, promotion switch, Gate 6, and
    rollback/deferral evidence before recording success.
- missing:
  - finalized workload references to the applied Secret/Config objects
  - PVC sizes and restore targets from actual backup artifacts
  - Tailscale route admin approval and access policy record; no active subnet route is recorded yet
  - WorkQueue live-consumer isolation proof before any worker consumes the live queue
  - rollback and compose retire approval records
  - Gate 4 live apply execution after Gates 1-3 pass
  - Gate 5 read/write canary execution evidence after Gate 4 is ready
  - Gate 6 cutover/postcheck/rollback/compose retire execution evidence after Gates 1-5 pass
- next stop condition:
  - do not create Kubernetes Secrets, PVCs, StatefulSets, Deployments, NetworkPolicy on running
    workloads, or queue consumers until the missing private overlay inputs are supplied and
    approved.
