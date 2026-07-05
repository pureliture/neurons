# Spec Drift Matrix - Architecture Debt Single-Issue Campaign

This matrix is a source-level drift aid for GitHub issue #40. It is not live runtime proof.

Status vocabulary:

- `done`: source artifact records completed local/test evidence and no current #40 follow-up action is implied.
- `partial`: some implementation or evidence exists, but live/runtime/readiness/cleanup scope remains open.
- `stale`: source may describe historical assumptions and must be re-read before implementation.
- `superseded`: a newer source or #40 tracker now owns the decision path.
- `open`: approved or active source with work still ahead.

## Priority Review Set

| Source | Matrix status | #40 relevance | Source-level evidence | Next action |
| --- | --- | --- | --- | --- |
| `specs/architecture-debt-single-issue` | open | single tracker campaign | Approved requirements/design define current What/How SoT; M0-M23 local implementation slices are recorded. | Next action is final local verification, #40 read-back/update, commit, and physical PR split/review workflow. |
| `specs/llm-brain-core-v1` | partial | `llm_brain_core` flat package, runtime truth | Internal implementation matrix records M0-M9 as done, but #40 still tracks package/interface structure and runtime-truth wording. | Use as context before package restructuring; do not treat as fully complete for #40. |
| `specs/context-authority-roadmap` | partial | runtime verification scope, k3s public contract | Milestones record many done checks, but runtime evidence gaps and k3s current-runtime caveats remain explicit. | Reuse vocabulary for `runtime_evidence_unverified` and current-runtime gap reporting. |
| `docs/specs/2026-07-05-lbrain-knowledge-object-substrate` | partial | runtime truth and support claims | Design separates `authority_lane` from `verification_state`; runtime proof remains typed evidence only. | Keep support-claim vocabulary aligned with M1. |
| `docs/specs/2026-07-05-lbrain-knowledge-object-substrate-production-validation` | partial | production/runtime claim gap | Validation report states local/contractual validation is not production-runtime verification. | Cite when #40 body needs runtime claim wording. |
| `docs/specs/2026-06-25-model-connectors-boundary` | partial | model connector residual debt | Boundary source exists, but #40 now narrows remaining debt to parsing normalization and reranker `logprobs` policy. | Reclassify as residual adapter/capability debt, not total non-separation. |
| `docs/specs/2026-06-27-neurons-k3s-migration` | partial | deploy/k3s public contract | Milestones include canary/rollback records but mark current production runtime proof as not durable; #40 now has static public contract guards. | Keep live apply/current-runtime proof separate and approval-gated. |
| `docs/specs/2026-06-29-neurons-k3s-scale-out` | partial | deploy/k3s scale-out preconditions | Scale-out runbook/specs describe dry-run/apply/postcheck sequencing and rollout gates; #40 static tests now cover public scale-out preconditions. | Live scale-out or private ops values still require separate approval and evidence. |
| `docs/specs/2026-06-29-ragflow-retirement-cleanup` | partial | CouchDB/session-memory dead code cleanup | Retired bridge cleanup source exists but #40 still flags dead CLI/helper surfaces. | Run deletion tests before removing compatibility or archive-only tools. |
| `specs/couchdb-transcript-migration` | partial | CouchDB/session-memory migration surface | Milestones show logic done while live event stream and real retired bridge comparison are human-gated. | Separate active runtime from archive/test-only tooling. |
| `specs/recall-cutover` | stale | live recall/delete safety | Source marks RC milestones done, but it includes high-risk cutover/delete semantics and needs fresh verification before reuse. | Do not use for live deletion without fresh SoT and approval. |
| `scripts/postcheck.sh` and `scripts/runtime-verify.py` | partial | runtime verification scope | M1 now labels postcheck as `api_shape_only` and runtime verify as `api_queue_smoke`, not full E2E. | Keep full E2E claims separate from API/NATS smoke. |

## Coverage Inventory

| Source | Matrix status | Drift note |
| --- | --- | --- |
| `docs/specs/2026-06-17-ci-quality-signal` | done | CI quality signal source is not a current #40 blocker. |
| `docs/specs/2026-06-21-qdrant-docling-searchable-mirror` | partial | Local/searchable mirror gates are done, but production failover remains `NO-GO`. |
| `docs/specs/2026-06-24-qdrant-mirror-cutover` | open | Cutover requires evidence and approval; do not infer active runtime migration. |
| `docs/specs/2026-06-25-model-connectors-boundary` | partial | Current #40 wording should say residual parsing/logprobs debt. |
| `docs/specs/2026-06-26-architecture-modernization-campaign` | superseded | #40 now owns the current single-issue architecture debt campaign. |
| `docs/specs/2026-06-27-neurons-k3s-migration` | partial | Public contract static guards now exist; durable current-runtime proof remains separate. |
| `docs/specs/2026-06-29-arch-quickwins` | partial | Some quick wins are done, but #40 still tracks compose/model/dead-code follow-ups. |
| `docs/specs/2026-06-29-neurons-k3s-scale-out` | partial | Public scale-out preconditions are statically guarded; live/private scale-out proof remains outside #40 local implementation. |
| `docs/specs/2026-06-29-public-private-separation` | partial | Boundary source remains current guardrail, but later history-rewrite/human-gate work is not fully closed. |
| `docs/specs/2026-06-29-ragflow-retirement-cleanup` | partial | Cleanup remains active around archive-only/test-only tooling. |
| `docs/specs/2026-06-30-ingress-api-profile-startup` | done | Startup/profile source is not a current #40 high-risk item. |
| `docs/specs/2026-06-30-mcp-http-allowed-hosts` | done | Host authorization source is outside this campaign unless runtime claim wording drifts. |
| `docs/specs/2026-07-02-repository-extraction-m2` | partial | Read-only runtime divergence wording is useful; activation proof remains separate. |
| `docs/specs/2026-07-05-couchdb-session-memory-pagination` | open | Current CouchDB/session-memory work may affect dead-code cleanup classification. |
| `docs/specs/2026-07-05-graph-trigger-postgres-ledger` | open | Live env change/cutover is approval-gated and outside M1/M2. |
| `docs/specs/2026-07-05-lbrain-knowledge-object-substrate` | partial | Source is current for authority/verification vocabulary. |
| `docs/specs/2026-07-05-lbrain-knowledge-object-substrate-production-validation` | partial | Production validation explicitly remains not runtime-verified. |
| `specs/architecture-debt-single-issue` | open | Current SoT for this campaign. |
| `specs/brain-steward-hardening` | done | Restricted commit/stale proposal milestones are recorded done; adjacent but not current #40 first track. |
| `specs/context-authority-roadmap` | partial | Current source for context authority and runtime evidence gaps. |
| `specs/couchdb-live-pipeline` | stale | Source marks live-pipeline milestones done, but historical live-pipeline language needs fresh evidence before reuse. |
| `specs/couchdb-transcript-migration` | partial | Logic/evidence exists, live comparison remains human-gated. |
| `specs/hermes-brain-steward` | done | Milestones are recorded done; adjacent, not a current #40 first-track item. |
| `specs/hermes-chunk-overlap` | done | Milestones are recorded done; adjacent, not a current #40 first-track item. |
| `specs/llm-brain-bulk-semantic-lane` | partial | Milestones are recorded done, but live enablement remains an operating decision. |
| `specs/llm-brain-core-v1` | partial | Internal implementation matrix is done; #40 package restructuring remains open. |
| `specs/recall-cutover` | stale | Source marks RC milestones done; high-risk live cutover/delete still requires fresh approval before action. |
| `specs/steward-commit-atomicity` | done | Atomic commit source appears completed and not a current #40 blocker. |

## Use In Later Milestones

- Before changing code for a #40 item, check whether its source is `partial`, `stale`, `superseded`, or `open`.
- `stale` and `superseded` sources cannot override `requirements.md`, approved `design.md`, or #40 current body.
- `partial` sources can guide implementation only within their tested/local evidence scope.
- Runtime claims require fresh target-specific evidence. This matrix cannot promote a claim to runtime-verified.
