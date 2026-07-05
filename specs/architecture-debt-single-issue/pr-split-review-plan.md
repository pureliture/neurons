# PR Split and Review Plan

Status: prepared through local implementation milestone M23.

Tracker: GitHub issue #40 remains the only issue tracker.

## Current Stop Line

M0-M23 reached a safe implementation stop line. Do not add another architecture slice before final local verification, #40 read-back/update, commit, and PR split/review preparation.

Verification collected for the current branch:

- full worker suite: passed
- optional MCP HTTP targeted tests: passed
- focused MCP registry/stdio tests: passed
- focused Ledger boundary/core tests: passed
- focused TargetProfile shared artifact tests: passed
- focused RetiredIndexBridge adapter placement architecture tests: passed
- focused compose SnakeYAML merge tests: passed
- focused k3s public contract static tests: passed
- `worker/eval/ledger_area_boundaries.py`: passed
- full root Gradle test: passed
- `git diff --check`: passed
- read-only architecture review: MCP first, Ledger memory-promotion area object second, no live mutation
- read-only architecture review after M20-M23: TargetProfile artifact, adapter placement, compose SnakeYAML, and k3s public contract are valid public/static next candidates
- code_simplifier review: MCP runtime contract extraction clarified without behavior change

## Recommended PR Split

### PR 1. SoT, evidence vocabulary, and tracker baseline

Scope:

- `specs/architecture-debt-single-issue/requirements.md`
- `specs/architecture-debt-single-issue/design.md`
- `specs/architecture-debt-single-issue/milestones.md`
- `specs/architecture-debt-single-issue/spec-drift-matrix.md`
- `worker/lib/agent_knowledge/session_memory/eval_readiness.py`
- eval/runtime evidence wording updates

Review focus:

- no product-readiness or full-E2E overclaim
- #40 remains tracker-only; local specs remain SoT
- runtime evidence labels stay conservative

### PR 2. Retired CouchDB/session-memory metadata and model connector normalization

Scope:

- `specs/architecture-debt-single-issue/couchdb-dead-code-audit.md`
- `specs/architecture-debt-single-issue/model-connectors-residual-debt.md`
- `worker/lib/agent_knowledge/cli.py`
- `worker/lib/agent_knowledge/model_connectors/structured_response.py`
- `worker/lib/agent_knowledge/model_connectors/openai_compatible.py`
- `worker/lib/agent_knowledge/llm_brain_core/graphiti_adapter.py`

Review focus:

- CouchDB/session-memory paths are classified, not deleted
- Graphiti compatibility wrappers remain public-safe
- reranker logprob emulation stays isolated behind connector tests

### PR 3. TargetProfile, RetiredIndexBridge adapter placement, and compose contract guard

Scope:

- `specs/architecture-debt-single-issue/targetprofile-contract-drift.md`
- `specs/architecture-debt-single-issue/compose-env-anchor-cleanup.md`
- `specs/architecture-debt-single-issue/targetprofile-shared-schema-artifact.md`
- `specs/architecture-debt-single-issue/retired-index-bridge-adapter-placement.md`
- `specs/architecture-debt-single-issue/compose-snakeyaml-hardening.md`
- `docs/contracts/target-profiles.yaml`
- `docs/contracts/ingress-contract.md`
- `compose.yaml`
- Java RetiredIndexBridge target adapter/tests
- Java architecture/compose/TargetProfile tests
- `worker/lib/agent_knowledge/rag_ingress/shadow_worker.py`
- `worker/tests/test_shadow_worker.py`

Review focus:

- `index-session-memory` must not fall back to `index-session-summary`
- Java/Python profile env names stay aligned
- `docs/contracts/target-profiles.yaml` remains logical and public-safe; it must not contain physical dataset ids or secrets
- `target.port` remains backend-neutral and does not depend on `adapter.ext.retired_index_bridge`
- compose anchor does not move live runtime responsibility into public repo
- SnakeYAML test proves resolved env merge behavior, not just raw string placement

### PR 4. `llm_brain_core.objects` package-depth first pass

Scope:

- `specs/architecture-debt-single-issue/llm-brain-core-package-depth-first-pass.md`
- `specs/architecture-debt-single-issue/llm-brain-core-objects-subpackage-first-pass.md`
- `worker/eval/llm_brain_core_package_depth.py`
- `worker/lib/agent_knowledge/llm_brain_core/objects/`
- root object-module compatibility shims
- object-subpackage tests

Review focus:

- root compatibility imports remain intact
- repo-internal object-substrate imports move to `llm_brain_core.objects`
- package-depth lint does not overfit private implementation names

### PR 5. MCP registries and single runtime contract

Scope:

- `specs/architecture-debt-single-issue/mcp-tools-coupling-audit.md`
- `specs/architecture-debt-single-issue/mcp-typed-registry-first-pass.md`
- `specs/architecture-debt-single-issue/mcp-handler-registry-first-pass.md`
- `specs/architecture-debt-single-issue/mcp-steward-restricted-handler-first-pass.md`
- `specs/architecture-debt-single-issue/mcp-steward-read-proposal-handler-first-pass.md`
- `specs/architecture-debt-single-issue/mcp-single-internal-definition.md`
- `worker/lib/agent_knowledge/mcp_tools.py`
- `worker/lib/agent_knowledge/mcp_jsonrpc.py`
- MCP registry tests

Review focus:

- public tool schema does not expose handler callables or dispatch-only metadata
- `ToolRuntimeContract` derives public schema, dispatch owner, and handler callable from one internal runtime contract
- restricted write handlers remain disjoint from read/proposal handlers
- cache invalidation happens only for successful restricted steward writes

### PR 6. Ledger boundary guard and memory-promotion area object

Scope:

- `specs/architecture-debt-single-issue/ledger-god-class-first-pass.md`
- `specs/architecture-debt-single-issue/ledger-area-object-extraction.md`
- `worker/eval/ledger_area_boundaries.py`
- `worker/lib/agent_knowledge/ledger.py`
- `worker/lib/agent_knowledge/ledger_memory_promotion_area.py`
- `worker/lib/agent_knowledge/ledger_memory_promotion_mixin.py`
- Ledger boundary/core tests

Review focus:

- `MemoryPromotionArea` is the first private area object only
- public `Ledger` dirty-memory APIs remain compatibility delegators
- durable-state semantics and transaction behavior stay unchanged
- broad Ledger multiple-inheritance removal remains deferred

### PR 7. k3s public contract static hardening

Scope:

- `specs/architecture-debt-single-issue/k3s-public-contract-hardening.md`
- `worker/tests/test_k3s_public_contract.py`
- existing `deploy/k3s/public-contract/**` files as tested contract sources

Review focus:

- tests are static and do not imply live apply or cluster verification
- public workload inventory keeps scale-out preconditions without private capacity values
- NetworkPolicy/CNI caveat and backup/restore rehearsal gates remain explicit
- public repo does not gain private ops values, hostnames, raw transcript bodies, raw dataset ids, or raw document ids

## Review Order

1. PR 1 first, because evidence language is the review baseline for every later PR.
2. PR 3 early if reviewers want Java/Python/compose contract drift isolated before Python-heavy changes.
3. PR 5 before PR 6, because MCP is lower blast radius and already reviewed as the safer first candidate.
4. PR 6 after PR 5, because Ledger is durable-state authority.
5. PR 7 can be reviewed independently after PR 1 because it is static public contract coverage only.

## Suggested Verification Pack

For final branch review, keep this pack as the branch-level proof:

- `cd worker && uv run pytest -q`
- `cd worker && uv run --extra mcp-http pytest -q tests/test_neuron_mcp_http.py`
- `cd worker && uv run python eval/ledger_area_boundaries.py`
- `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
- `git diff --check`

## Next Candidate After Review

No further in-scope local implementation candidate is queued inside #40 after M23.

Next work is physical PR split/opening/review preparation from this branch. Any live runtime, k3s, Docker/Compose stop/start, RetiredIndexBridge write/delete/disable, host mutation, private ops value, or full E2E verification claim remains a separate approval-gated workflow.

## Non-Goals For Review Prep

- Do not open more GitHub issues.
- Do not perform live write/delete/disable/deploy/k3s/Docker/systemd/firewall mutation.
- Do not delete retained CouchDB/session-memory compatibility surfaces.
- Do not claim full E2E business verification from local/API-only evidence.
