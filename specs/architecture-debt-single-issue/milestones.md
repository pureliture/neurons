# Milestones - Architecture Debt Single-Issue Campaign

Goal: Execute the approved `design.md` for GitHub issue #40 as one long-running implementation goal.

SoT:

- What: `specs/architecture-debt-single-issue/requirements.md`
- How: `specs/architecture-debt-single-issue/design.md`
- Tracker: GitHub issue #40 only

## M0. SoT and Tracker Baseline

- status: done
- orchestration:
  - mode: normal
  - risk_complexity_signal: shared tracker plus long-running execution contract
  - delegation_decision: single-executor
  - delegation_reason: docs-only baseline can be verified from local SoT, LBrain read-only authority, and GitHub read-back
  - role_boundaries: single executor
  - expected_evidence_class: docs-only
  - tdd_status: not applicable
  - sot_change: false
  - residual_risk: #40 title remains old, but body is the active tracker and source artifacts are authoritative
- evidence:
  - local worktree is `codex/architecture-debt-single-issue-requirements`
  - source checkout `main` is clean
  - LBrain accepted/current authority confirms live mutation requires separate approval gates
  - GitHub issue #40 is open and body contains the single-issue architecture debt tracker

## M1. R1 Verification Vocabulary and Eval Baseline

- status: done
- orchestration:
  - mode: normal
  - risk_complexity_signal: worker eval/readiness wording plus verification support claims affect future completion evidence
  - delegation_decision: parallel-investigate
  - delegation_reason: main executor can implement the first focused guard while review roles independently check architecture and simplification risks
  - role_boundaries: main executor owns implementation; codebase_architecture_manager reviews module/interface impact; code_simplifier reviews recently modified code for behavior-preserving simplification
  - expected_evidence_class: code-change
  - tdd_status: failing-first characterization/contract tests passed
  - sot_change: false
  - residual_risk: full runtime behavior remains unproven unless target-specific runtime evidence is produced later
- evidence:
  - `worker/tests/test_eval_readiness.py` guards `worker_eval` as `dev_only_harness`, not a product readiness gate or runtime-verified lane
  - eval CLI stdout exposes readiness classification, blocker codes, and verification level taxonomy
  - Discord eval notifier wording is eval-specific and does not expose product readiness or runtime verification claims
  - `scripts/runtime-verify.py` labels runtime evidence as `api_queue_smoke` with `fullE2EVerified=false`
  - `scripts/postcheck.sh --offline` labels postcheck evidence as `api_shape_only` with `verified=false` and `fullE2EVerified=false`
  - focused worker tests passed for eval readiness, eval loop, eval notifier, and golden grader
  - runtime verifier contract tests passed
  - codebase_architecture_manager reviewed M1 as GO with `eval_readiness` as the local vocabulary module
  - code_simplifier applied behavior-preserving cleanup to duplicate constants and repeated runtime JSON lookup

## M2. Spec Drift Matrix

- status: done
- orchestration:
  - mode: normal
  - risk_complexity_signal: specs can overstate runtime truth and guide later code changes
  - delegation_decision: parallel-investigate
  - delegation_reason: spec inventory and drift classification can be investigated in parallel while main executor builds the matrix artifact
  - role_boundaries: main executor owns matrix artifact; delegated test/support worker may inspect coverage gaps only
  - expected_evidence_class: docs-only
  - tdd_status: not applicable; substitute evidence is static coverage and source-reference check
  - sot_change: false
  - residual_risk: classifications are source-level and do not prove live runtime state
- evidence:
  - `specs/architecture-debt-single-issue/spec-drift-matrix.md` created as source-level drift matrix
  - matrix covers every immediate child directory under `specs/` and `docs/specs/`
  - static coverage check reported no missing spec directories
  - matrix includes `done`, `partial`, `stale`, `superseded`, and `open` status vocabulary
  - matrix explicitly states it is not live runtime proof and cannot promote runtime-verified claims
  - Spark explorer cross-check reviewed major spec directories and highlighted high runtime-truth risk entries

## M3. #40 Alignment and Continuation Gate

- status: done
- orchestration:
  - mode: normal
  - risk_complexity_signal: GitHub issue body is external tracker state but already approved as the single #40 tracker
  - delegation_decision: single-executor
  - delegation_reason: issue body update is a bounded tracker alignment mutation using source artifacts already produced
  - role_boundaries: single executor
  - expected_evidence_class: docs-only
  - tdd_status: not applicable; substitute evidence is GitHub read-back
  - sot_change: false
  - residual_risk: issue body is a tracker mirror, not the authoritative SoT
- evidence:
  - #40 title updated to `Architecture Debt Tracker - Single Issue`
  - #40 body lists current SoT artifacts and preserves single-issue tracking policy
  - #40 body marks M1/M2 complete and labels postcheck/runtime verify scopes without full E2E overclaim
  - #40 read-back confirmed the updated title/body
  - #40 selects CouchDB / Session Memory Migration Dead Code as the next automatic target

## M4. Automatic Backlog Continuation

- status: done
- orchestration:
  - mode: normal
  - risk_complexity_signal: deletion/dead-code cleanup can affect CLI compatibility and tests
  - delegation_decision: parallel-investigate
  - delegation_reason: read-only active/archive/test-only classification and test-only contract setup can be split from main executor implementation
  - role_boundaries: main executor owns implementation; delegated explorer maps candidate dead-code surfaces and test imports; Spark worker owns failing-first test addition; code_simplifier reviews behavior-preserving structure
  - expected_evidence_class: code-change
  - tdd_status: failing-first CLI surface classification tests passed after metadata implementation
  - sot_change: false
  - residual_risk: no CouchDB/session-memory file is deleted yet; deletion requires a narrower deletion-test probe and compatibility proof
- evidence:
  - `specs/architecture-debt-single-issue/couchdb-dead-code-audit.md` classifies active runtime, human-gated migration, legacy compatibility, archive/recovery, and deletion-test candidate surfaces
  - `worker/tests/test_neuron_cli.py` first failed because `COMMAND_METADATA` did not exist, then passed after implementation
  - `worker/lib/agent_knowledge/cli.py` now classifies `couchdb-session-memory-build`, `transcript-migration`, `couchdb-migration-flow`, and `neuron-session-memory-build`
  - `COMMAND_METADATA` keys are guarded as a subset of `COMMAND_HANDLERS`
  - focused CLI tests passed
  - M1/M4 worker regression tests passed
  - CouchDB/session-memory candidate baseline tests passed
  - `code_simplifier` reviewed the metadata as behavior-preserving and did not request changes

## M5+. Subsequent #40 Debt Items

- status: in-progress
- completed slice: CouchDB/session-memory dead-code item first pass
- evidence:
  - `session_memory/backfill.py` is not a routed command, but it is a public compatibility / safety-planning surface exported through `session_memory.__init__`
  - `couchdb_source/index_fallback.py` is not routed or re-exported, but it is archive/recovery compatibility with recall-cutover evidence
  - `couchdb_source/shadow_cutover.py` is not routed, but it is a public package export and human-gated migration compatibility surface
  - all three probes used source search plus targeted tests; no files were deleted
  - `specs/architecture-debt-single-issue/couchdb-dead-code-audit.md` records the preservation decision and future compatibility-removal gate
- completed slice: Model Connectors residual debt first pass
- evidence:
  - `worker/lib/agent_knowledge/model_connectors/structured_response.py` is now the shared structured response normalization contract
  - `worker/lib/agent_knowledge/model_connectors/openai_compatible.py` uses the shared normalizer
  - `worker/lib/agent_knowledge/llm_brain_core/graphiti_adapter.py` keeps private compatibility wrappers while delegating normalization to the shared connector module
  - `worker/tests/test_model_connectors.py` covers the shared structured response contract and reranker `top_logprobs` true/false scoring through public `OpenAICompatibleRerankerClient.ascore()`
  - targeted model connector, Graphiti, CouchDB, eval, and runtime verifier tests passed
  - `specs/architecture-debt-single-issue/model-connectors-residual-debt.md` records fake/no-network evidence and residual live canary gap
- completed slice: MCP Server and Tools spec coupling first pass
- evidence:
  - read-only architecture review identified `mcp_tools.py` literal schemas and `mcp_jsonrpc.py` dispatch if-chain as the central coupling
  - `worker/lib/agent_knowledge/mcp_tools.py` now exposes `tool_registry()` and `tool_names()` over the current `list_tools()` contract
  - `worker/tests/test_neuron_mcp_stdio.py` guards unique listed names and registry/list schema parity
  - stdio/adversarial MCP tests passed from the correct `worker` cwd
  - targeted optional `mcp-http` transport schema mapping test passed with `uv run --extra mcp-http`
  - `specs/architecture-debt-single-issue/mcp-tools-coupling-audit.md` records residual dispatch if-chain and future registry-shape decision
- completed slice: TargetProfile contract drift first pass
- evidence:
  - `RetiredIndexBridgeTargetAdapter` no longer falls back from `index-session-memory` to the `index-session-summary` dataset id
  - `RetiredIndexBridgeTargetAdapterTest.sessionMemoryDatasetDoesNotFallbackToSessionSummaryDataset` captured the prior fallback as a RED test before the fix
  - `env_profile_dataset_resolver` now maps `index-*` profiles to `RETIRED_INDEX_BRIDGE_<ROLE>_DATASET_ID`
  - `worker/tests/test_shadow_worker.py` cross-checks Java `application.yml` target profiles against Python resolver behavior plus `compose.yaml` and `.env.example` coverage
  - targeted Java TargetProfile/adapter/validator tests and Python shadow-worker resolver tests passed
  - `specs/architecture-debt-single-issue/targetprofile-contract-drift.md` records residual shared-schema and live-delivery gaps
- completed slice: compose env anchor cleanup first pass
- evidence:
  - `compose.yaml` now has `x-retired-index-bridge-env` for RetiredIndexBridge base URL, API key, and all seven per-profile dataset ids
  - `x-ingress-java-env` merges the shared RetiredIndexBridge anchor while keeping Java-only NATS, delivery-enabled, and pressure env local
  - `ingress-worker-py` merges the shared RetiredIndexBridge anchor while keeping Python-only live/shadow queue, state DB, pressure URL, Qdrant, and embedding env local
  - `ComposeConfigTest.retiredIndexBridgeEnvAnchorIsSharedByJavaAndPythonWorkers` captured the missing shared anchor as a RED test before the compose change
  - targeted `ComposeConfigTest` passed
  - PyYAML merge inspection confirmed `ingress-api`, `ingress-worker`, and `ingress-worker-py` all resolve the shared RetiredIndexBridge env keys
  - `specs/architecture-debt-single-issue/compose-env-anchor-cleanup.md` records the no-live-runtime limitation and future SnakeYAML hardening option
- completed slice: Ledger god-class first pass
- evidence:
  - `ledger_area_boundaries.py` now fails closed when an expected area mixin file is missing
  - `ledger_area_boundaries.py` now checks that `Ledger` keeps the expected mixin bases
  - `ledger_area_boundaries.py` now blocks direct inherited calls from `ledger_ingress_mixin` to memory-promotion dirty-marking methods
  - `Ledger._memory_promotion_area` provides a private seam for current behavior-preserving delegation
  - `IngressStatusMixin` routes indexed-conversation dirty memory side effects through that seam
  - targeted Ledger boundary, seam invariant, core, and transaction tests passed
  - `specs/architecture-debt-single-issue/ledger-god-class-first-pass.md` records residual multiple-inheritance and future area-object work
- completed slice: MCP typed registry first pass
- evidence:
  - `ToolContract` now gives each MCP tool an internal typed contract with `name`, `description`, `input_schema`, and `dispatch_owner`
  - `tool_contract_registry()` fails closed when a listed tool lacks dispatch-owner metadata or metadata is stale
  - public `list_tools()` and HTTP SDK conversion keep dispatch metadata hidden
  - targeted stdio/adversarial MCP and optional `mcp-http` transport tests passed
  - `specs/architecture-debt-single-issue/mcp-typed-registry-first-pass.md` records residual if-chain handler migration work
- completed slice: `llm_brain_core` package-depth first pass
- evidence:
  - `worker/eval/llm_brain_core_package_depth.py` now classifies all current root modules by area
  - the package-depth lint fails on unclassified root modules, stale manifest modules, and CLI module re-export from `__init__`
  - package-depth, existing layering, public-safe-util decouple, and retired index-client leakage tests passed
  - `specs/architecture-debt-single-issue/llm-brain-core-package-depth-first-pass.md` records residual object-substrate subpackage work
- completed slice: `llm_brain_core.objects` subpackage first pass
- evidence:
  - `agent_knowledge.llm_brain_core.objects` now owns object-substrate implementation modules for knowledge objects, object packs, reference corpus, golden query eval, OKF export, and object CLI
  - root `llm_brain_core.<object_module>` files remain public compatibility shims that re-export the moved implementations
  - repo-internal production imports now prefer `llm_brain_core.objects` for object CLI, object packs, reference corpus, and MCP JSON-RPC object helpers
  - `worker/tests/test_llm_brain_core_objects_subpackage.py` captured the missing subpackage as a RED test before implementation
  - `worker/eval/llm_brain_core_package_depth.py` now requires expected object-subpackage modules and root object compatibility shims
  - object-subpackage contract tests, package-depth guard tests/eval, object substrate tests, neuron CLI tests, and MCP stdio tests passed
  - `specs/architecture-debt-single-issue/llm-brain-core-objects-subpackage-first-pass.md` records residual legacy shim and broad `__init__` surface risk
- completed slice: MCP handler registry first pass
- evidence:
  - `worker/lib/agent_knowledge/mcp_jsonrpc.py` now exposes `tool_handler_registry()`
  - top-level `dispatch_tool_call()` now routes through registry lookup instead of a tool-name if-chain
  - handler registry keys are fail-closed against `tool_contract_registry()`
  - public `list_tools()` and `ToolContract.to_tool()` still do not expose handler callables or dispatch-only metadata
  - `worker/tests/test_mcp_handler_registry.py` captured the missing registry and non-registry dispatch as RED tests before implementation
  - MCP handler registry, stdio/adversarial MCP, and optional targeted HTTP tests passed
  - `specs/architecture-debt-single-issue/mcp-handler-registry-first-pass.md` records residual steward internal if-chain and handler/schema metadata split
- completed slice: MCP steward restricted handler first pass
- evidence:
  - `worker/lib/agent_knowledge/mcp_jsonrpc.py` now exposes canonical `steward_restricted_handler_registry()` plus compatibility alias `restricted_steward_handler_registry()`
  - restricted steward write tools now route through a table-driven registry instead of direct restricted tool-name comparisons in `_dispatch_steward_tool()`
  - denied restricted calls convert `StewardPermissionError` to `restricted_denied_payload()` and do not invalidate the session brain-card cache
  - successful restricted calls invalidate the session brain-card cache exactly once after steward dispatch returns
  - `worker/tests/test_mcp_steward_handler_registry.py` guards restricted registry keyset, denied/no-invalidation behavior, success/invalidation ordering, and no direct restricted comparison chain
  - MCP steward handler, handler registry, stdio/adversarial MCP, and optional targeted HTTP tests passed
  - `specs/architecture-debt-single-issue/mcp-steward-restricted-handler-first-pass.md` records residual read/proposal steward dispatch and private-shape test risk
- completed slice: targeted cumulative verification checkpoint after M15
- evidence:
  - Python cumulative targeted suite passed: object-substrate, package-depth, MCP handler/steward registries, stdio/adversarial MCP, Ledger boundaries/core/transaction, TargetProfile shadow worker, eval readiness, model connectors, CLI, layering, and retired index-client guard
  - `worker/eval/llm_brain_core_package_depth.py` passed
  - optional `mcp-http` targeted transport/schema test passed with `uv run --extra mcp-http`
  - Java targeted ComposeConfig, RetiredIndexBridge adapter, TargetProfileRegistry, and IngestJobValidator tests passed
  - `git diff --check` passed
- completed slice: MCP steward read/proposal handler first pass
- evidence:
  - `worker/lib/agent_knowledge/mcp_jsonrpc.py` now exposes `steward_read_proposal_handler_registry()`
  - steward read/proposal tools now route through a registry separate from restricted write tools
  - read/proposal registry validation fails closed on missing/stale handlers and restricted-tool overlap
  - read/proposal handlers do not invalidate the session brain-card cache
  - candidate/supersede proposal handlers still call `steward.select_source_span(arguments)`
  - `worker/tests/test_mcp_steward_read_proposal_registry.py` guards keyset, restricted disjointness, no invalidation, source-span selection, and no direct steward tool-name comparison chain
  - MCP steward read/proposal, restricted steward, handler registry, stdio/adversarial MCP, and optional targeted HTTP tests passed
  - `specs/architecture-debt-single-issue/mcp-steward-read-proposal-handler-first-pass.md` records residual private-shape tests and schema/owner/handler split
- completed slice: targeted cumulative verification checkpoint after M16
- evidence:
  - Python cumulative targeted suite passed with M16 included: object-substrate, package-depth, MCP read/proposal + restricted steward registries, MCP handler registry, stdio/adversarial MCP, Ledger boundaries/core/transaction, TargetProfile shadow worker, eval readiness, model connectors, CLI, layering, and retired index-client guard
  - full worker suite passed after restoring the `graphiti_adapter._normalize_structured_keys` compatibility alias
  - `worker/eval/llm_brain_core_package_depth.py` passed
  - optional `mcp-http` targeted transport/schema test passed with `uv run --extra mcp-http`
  - full root Gradle test passed
  - `git diff --check` passed
- completed slice: broad architecture review checkpoint after full-suite verification
- evidence:
  - read-only architecture review found no approved-design drift across the cumulative M0-M16 diff
  - review flagged progression risk from continuing to add implementation slices without PR/review preparation
  - recommended next implementation design target is MCP schema/owner/handler single internal definition
  - Ledger area-object extraction remains valuable but should follow MCP cleanup because its blast radius is larger
  - TargetProfile shared-schema artifact remains a follow-up candidate after the current diff is reviewed/split
- next target: continue implementation before PR split/review preparation

## M17. MCP Single Internal Definition

- status: done
- orchestration:
  - mode: normal
  - risk_complexity_signal: shared MCP schema/dispatch contract touches public tool listing, JSON-RPC dispatch, steward dispatch, HTTP schema conversion, and tests
  - delegation_decision: parallel-investigate
  - delegation_reason: main executor owns SoT/test/implementation while codebase_architecture_manager reviews drift risks and safe seams read-only
  - rejected_alternatives: single-executor rejected because MCP is a shared contract and previous review identified schema/owner/handler split as the next architecture target
  - single_executor_justification: not required
  - role_boundaries: main executor updates specs/code/tests; codebase_architecture_manager reports read-only architecture risks and guard suggestions
  - expected_evidence_class: code-change
  - tdd_status: failing-first registry contract tests planned
  - sot_change: false
  - residual_risk: target runtime dispatch remains local-test verified only unless separate runtime evidence is collected
- target:
  - unify MCP tool schema, dispatch owner, and handler callable into a single internal contract definition
  - keep public `list_tools()` output schema-compatible and free of dispatch-only metadata
  - keep restricted steward write handlers disjoint from read/proposal handlers
  - avoid live MCP proposal writes or runtime mutation
- evidence:
  - `worker/lib/agent_knowledge/mcp_jsonrpc.py` now exposes `ToolRuntimeContract` and `tool_runtime_contract_registry()`
  - runtime contracts combine public `ToolContract`, private dispatch owner, and handler callable without exposing dispatch metadata through `list_tools()`
  - `tool_handler_registry()`, `steward_read_proposal_handler_registry()`, and `steward_restricted_handler_registry()` derive handlers from runtime contracts
  - `worker/tests/test_mcp_handler_registry.py` captured missing runtime-contract unification as a RED test before implementation
  - read-only architecture review agreed MCP was the recommended first candidate and flagged public schema/handler leakage as stop conditions
  - code_simplifier reduced dispatch-owner handler extraction duplication and kept focused MCP tests green
  - focused MCP/stdio tests passed
  - optional MCP HTTP tests passed
  - full worker suite passed after M17 and again after M18

## M18. Ledger Area-Object Extraction

- status: done
- orchestration:
  - mode: normal
  - risk_complexity_signal: Ledger god-class boundary touches durable state, memory-promotion side effects, and multiple mixin inheritance
  - delegation_decision: verify
  - delegation_reason: implementer will keep the first extraction narrow; independent review/simplification is required after MCP verification before Ledger mutation
  - rejected_alternatives: direct broad Ledger decomposition rejected because current safe seam is only `_memory_promotion_area`
  - single_executor_justification: not required
  - role_boundaries: main executor implements a narrow area object after MCP passes; code_simplifier reviews behavior-preserving clarity; architecture review checks boundary drift
  - expected_evidence_class: code-change
  - tdd_status: failing-first Ledger boundary tests planned after MCP verification
  - sot_change: false
  - residual_risk: broad multiple-inheritance removal remains out of scope for this step
- target:
  - extract the first behavior-preserving Ledger area object behind an existing or newly minimal seam
  - preserve public Ledger API and durable-state semantics
  - strengthen boundary tests so ingress code cannot reach memory-promotion side effects through inherited calls
  - avoid GC/live data mutation
- evidence:
  - `worker/lib/agent_knowledge/ledger_memory_promotion_area.py` now owns the private memory-promotion dirty-marking area object
  - `Ledger._memory_promotion_area` returns `MemoryPromotionArea` instead of `self`
  - public `Ledger.mark_session_memory_dirty()` and `Ledger.mark_project_memory_dirty()` remain compatibility delegators through the area object
  - ingress indexed conversation-chunk side effects still route through `_memory_promotion_area`
  - `worker/tests/test_ledger_core.py` captured `_memory_promotion_area is not ledger` as a RED test before extraction and verifies dirty behavior is preserved
  - `worker/eval/ledger_area_boundaries.py` now fails if the memory-promotion area seam returns `self`
  - focused Ledger boundary/core tests passed
  - `worker/eval/ledger_area_boundaries.py` passed
  - full worker suite passed
  - full root Gradle test passed
  - `git diff --check` passed

## M19. PR Split and Review Preparation

- status: done
- orchestration:
  - mode: normal
  - risk_complexity_signal: cumulative M0-M18 diff spans docs, Python worker, Java/compose tests, MCP dispatch, and Ledger durable-state boundary
  - delegation_decision: single-executor
  - delegation_reason: docs-only review preparation uses already-collected implementation evidence and does not need additional file mutation outside specs
  - rejected_alternatives: opening PRs directly rejected because branch has not been pushed or split yet; adding more implementation rejected until review preparation is complete
  - single_executor_justification: docs-only artifact with no runtime claim and no live mutation
  - role_boundaries: single executor
  - expected_evidence_class: docs-only
  - tdd_status: not applicable; substitute evidence is source artifact plus clean diff check
  - sot_change: false
  - residual_risk: physical PR split still requires branch/push/PR workflow decision
- evidence:
  - `specs/architecture-debt-single-issue/pr-split-review-plan.md` defines six review slices after M18
  - review order keeps MCP before Ledger and Ledger last because it is durable-state authority
  - branch-level verification pack is recorded for reviewers
  - after M20-M23 follow-up, no further in-scope local implementation candidate remains before PR split/opening/review preparation

## M20. TargetProfile Shared Schema Artifact

- status: done
- orchestration:
  - mode: normal
  - risk_complexity_signal: Java registry, Spring config, Python resolver, compose, and `.env.example` previously knew logical profile/env-key contracts separately
  - delegation_decision: parallel-review
  - delegation_reason: main executor owns artifact/tests while codebase_architecture_manager checks contract placement and public/private boundary read-only
  - rejected_alternatives: runtime config binding rewrite rejected because this milestone only needs public-safe parity and drift prevention
  - single_executor_justification: not required
  - role_boundaries: main executor updates contract and tests; architecture reviewer reports drift or SoT conflicts only
  - expected_evidence_class: code-change
  - tdd_status: failing-first Java/Python artifact tests observed before artifact creation
  - sot_change: true; approved design extended M20-M23 before implementation
  - residual_risk: live retired bridge delivery and physical dataset id availability remain approval/runtime-gated
- evidence:
  - `docs/contracts/target-profiles.yaml` now records logical profile, backend kind, dataset role, and retired bridge dataset env key for every current profile
  - `docs/contracts/ingress-contract.md` defines the artifact as the machine-readable child of the backend-neutral ingress contract, not a physical runtime config source
  - `TargetProfileRegistryTest.registryAndApplicationYmlStayInParityWithSharedTargetProfileContract` compares the artifact with `TargetProfileRegistry.DEFAULT` and `application.yml`
  - `worker/tests/test_shadow_worker.py` reads the same artifact and checks Python resolver, `compose.yaml`, `.env.example`, and `application.yml` coverage
  - artifact guards reject physical dataset id/token/private value fields
  - targeted Java TargetProfile tests passed
  - targeted Python shadow-worker tests passed

## M21. RetiredIndexBridge Adapter Placement Guard

- status: done
- orchestration:
  - mode: normal
  - risk_complexity_signal: historical `targetAdapter` wording can reintroduce implementation leakage into backend-neutral ports
  - delegation_decision: parallel-review
  - delegation_reason: architecture package-boundary rules benefit from independent read-only interface review while main executor adds the guard
  - rejected_alternatives: package move rejected because current implementation placement already matches the desired boundary
  - single_executor_justification: not required
  - role_boundaries: main executor owns ArchUnit rules; architecture reviewer checks boundary shape and stale wording risk
  - expected_evidence_class: code-change
  - tdd_status: strengthened static architecture guard
  - sot_change: false
  - residual_risk: this does not remove retained compatibility classes or perform live adapter calls
- evidence:
  - `ArchitectureRulesTest.target_ports_should_not_depend_on_retired_bridge_implementation` blocks `target.port` from depending on `adapter.ext.retired_index_bridge`
  - `ArchitectureRulesTest.retired_bridge_implementation_classes_stay_in_ext_adapter_package` keeps RetiredIndexBridge implementation classes inside the external adapter package
  - targeted architecture tests passed

## M22. compose SnakeYAML Hardening

- status: done
- orchestration:
  - mode: normal
  - risk_complexity_signal: string-only compose guards can miss YAML merge drift between Java and Python services
  - delegation_decision: single-executor
  - delegation_reason: test-only hardening is narrowly scoped to `ComposeConfigTest` and existing compose contract
  - rejected_alternatives: compose runtime start/stop rejected because static YAML merge proof is sufficient and live mutation is out of scope
  - single_executor_justification: local static test hardening with no runtime side effect
  - role_boundaries: single executor
  - expected_evidence_class: code-change
  - tdd_status: strengthened parsed-YAML contract test
  - sot_change: false
  - residual_risk: local static parsing does not prove a live compose deployment
- evidence:
  - `ComposeConfigTest.retiredIndexBridgeEnvAnchorResolvesThroughYamlMergeForJavaAndPythonServices` parses `compose.yaml` with SnakeYAML
  - parsed env maps prove `ingress-api`, `ingress-worker`, and `ingress-worker-py` resolve the same shared retired bridge common env keys
  - parsed env maps prove Python live queue/delivery controls remain service-local and do not move into the shared retired bridge anchor
  - targeted compose config tests passed

## M23. k3s Public Contract Hardening

- status: done
- orchestration:
  - mode: normal
  - risk_complexity_signal: deploy/k3s public docs can accidentally imply live migration or expose private ops assumptions
  - delegation_decision: parallel-review
  - delegation_reason: main executor owns static tests while codebase_architecture_manager reviews public/private boundary and live-apply risk read-only
  - rejected_alternatives: live k3s dry-run/apply rejected because this milestone only approves public static contract hardening
  - single_executor_justification: not required
  - role_boundaries: main executor adds public static tests; architecture reviewer checks no live mutation or private values are introduced
  - expected_evidence_class: code-change
  - tdd_status: focused static contract tests added
  - sot_change: false
  - residual_risk: actual k3s migration, private ops overlay values, and cluster runtime proof remain approval-gated outside this public repo slice
- evidence:
  - `worker/tests/test_k3s_public_contract.py` reads `deploy/k3s/public-contract/workload-inventory.yaml`, `base/config-contract.yaml`, and `ops-overlay-contract.yaml`
  - tests guard canary safety, workqueue isolation, scale-out preconditions, NetworkPolicy/CNI caveat, backup/restore gates, and forbidden public data
  - tests scan `deploy/k3s/**` for live apply/host mutation commands that must not appear in this public contract
  - focused k3s public contract tests passed

## M24. Final Local Verification and Review Handoff

- status: done
- orchestration:
  - mode: normal
  - risk_complexity_signal: cumulative branch now spans specs, public contracts, Java architecture/compose tests, Python worker tests, MCP dispatch, and Ledger authority boundaries
  - delegation_decision: single-executor
  - delegation_reason: final local verification and commit preparation use already-scoped artifacts and no new implementation slice
  - rejected_alternatives: adding more implementation candidates rejected because M20-M23 closes the remaining in-scope post-review candidates
  - single_executor_justification: verification/docs/commit preparation only
  - role_boundaries: single executor
  - expected_evidence_class: branch-verification
  - tdd_status: not applicable; final verification aggregates prior focused tests
  - sot_change: false
  - residual_risk: physical PR split/opening and any live runtime/k3s proof remain separate workflow steps
- evidence:
  - full worker suite passed: `1536 passed, 8 skipped`
  - optional MCP HTTP targeted tests passed: `51 passed`
  - `worker/eval/ledger_area_boundaries.py` passed with `area boundaries OK`
  - `worker/eval/llm_brain_core_package_depth.py` passed with `llm_brain_core package depth OK`
  - full root Gradle test passed
  - focused post-fix worker tests passed: `tests/test_shadow_worker.py tests/test_k3s_public_contract.py` reported `6 passed`
  - `git diff --check` passed after removing one trailing blank line
  - #40 body read-back confirmed M20-M24 Done, no remaining in-scope local implementation candidates, and next work as PR split/opening/review preparation
