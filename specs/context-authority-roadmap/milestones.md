# Milestones - context-authority-roadmap

## M1.1 ContextPack authority block
- status: done
- evidence: RED `tests/test_context_authority_pack.py` failed on missing `authority`; GREEN targeted tests passed; full worker suite passed with 1176 passed / 9 skipped.

## M1.2 MCP agent smoke carries authority block
- status: done
- evidence: MCP regression test passed, and live `mcp-stdio` JSON-RPC smoke returned `authority.schema_version=context_authority_pack.v1`.

## M1.3 Neo4j workbench projection checkpoint
- status: done
- evidence: RED missing `authority_projection` module; GREEN `authority_episodes_from_context_pack` projects Document / WorkflowContract / PreferenceRule / EvidenceGap episodes through `GraphProjectionWorker` into `FakeGraphMemoryAdapter`.

## M1.4 Runtime evidence gaps for unverified deployment claims
- status: done
- evidence: RED runtime request only returned `graph_unavailable`; GREEN ContextPack authority gaps include `runtime_evidence_unverified`; CLI smoke returns the same gap.

## M1.5 Boundary guardrail regression
- status: done
- evidence: Existing M1-M5 core package guard caught forbidden retired bridge literal in core code; label was changed to generic retired-document-bridge wording; targeted boundary tests, full worker suite, and root Gradle tests passed.

## M1.6 Neo4j workbench projection metadata
- status: done
- evidence: RED authority projection payloads did not carry the design-required `projection_version` and `source_card_id`; GREEN Document, WorkflowContract, PreferenceRule, and EvidenceGap projection episodes now carry `projection_version=context_authority_projection.v1` plus source-card metadata so Neo4j workbench nodes remain traceable to authority cards/evidence; focused projection test passed, worker full suite passed with 1213 passed / 9 skipped, and root Gradle passed.

## M2.1 DocumentAuthorityCard model
- status: done
- evidence: RED missing `document_authority` module; GREEN `DocumentAuthorityCard` classifies approved Markdown source as `source_of_truth` and HTML preview as `generated_companion`; ContextPack and authority projection tests were updated to consume the model.

## M2.2 DocumentEvidenceEdge model
- status: done
- evidence: RED document cards lacked `evidence_edges` and projection payload dropped them; GREEN `DocumentEvidenceEdge` captures memory_card, session, commit, pull_request, and live evidence, and Document episodes preserve edges for Neo4j workbench inspection.

## M2.3 Document inventory authority fallback
- status: done
- evidence: RED ContextPack returned no documents when only `current_files` contained Markdown/HTML docs; GREEN inventory paths create low-confidence DocumentAuthorityCards while accepted memory-card evidence wins over duplicate inventory entries.

## M2.4 Document authority read paths
- status: done
- evidence: RED `BrainReadService` had no `brain_docs_current`, `brain_docs_explain`, or `brain_docs_archive_candidates`; GREEN read paths reuse ContextPack authority documents, filter current vs archive candidates, and keep archive/delete proposal-only.

## M2 Document Authority Synthesis
- status: done
- evidence: DocumentAuthorityCard, DocumentEvidenceEdge, inventory fallback, current/explain/archive read paths, ContextPack consumption, and Neo4j workbench projection all have passing targeted tests; worker full suite and root Gradle passed after the milestone.

## M3.1 WorkflowContractCard model
- status: done
- evidence: RED missing `workflow_authority` module; GREEN `WorkflowContractCard` carries rule, scope, reason, confidence, evidence refs, exceptions, and `auto_update_allowed=false`; ContextPack and authority graph projection preserve the richer contract fields.

## M3.2 Workflow authority read paths
- status: done
- evidence: RED `BrainReadService` had no `brain_workflows_current` or `brain_workflows_explain`; GREEN read paths reuse ContextPack `authority.workflow_contracts`, preserve evidence/scope/reason/exceptions, keep aggregate `auto_update_allowed=false`, and tolerate minor rule wording differences; focused tests passed, worker full suite passed with 1183 passed / 9 skipped, and root Gradle passed.

## M3.3 WorkflowDefaultCard and SkillEvolutionCard read-only models
- status: done
- evidence: RED workflow authority tests could not import `workflow_default_cards_from_memory_cards` or `skill_evolution_cards_from_memory_cards`; GREEN `WorkflowDefaultCard` and `SkillEvolutionCard` carry scope/reason/evidence/confidence and always keep `auto_update_allowed=false`; `workflow_default` cards are consumable as workflow contracts while `skill_evolution` remains evidence-only; focused workflow/projection/context tests passed, worker full suite passed with 1185 passed / 9 skipped, and root Gradle passed.

## M4.1 PreferenceRuleCard model
- status: done
- evidence: RED missing `preference_authority` module; GREEN `PreferenceRuleCard` carries rule, scope, reason, confidence, currentness, evidence refs, and exceptions; workflow contracts are not consumed as preferences; ContextPack and authority projection preserve richer preference fields; focused tests passed, worker full suite passed with 1187 passed / 9 skipped, and root Gradle passed.

## M4.2 Preference relevance filtering
- status: done
- evidence: RED ContextPack applied runtime/proof preference to unrelated workflow requests; GREEN preference relevance filter keeps global/project/communication preferences always available and applies scoped preferences only when current request/files match scope terms; focused tests passed, worker full suite passed with 1188 passed / 9 skipped, and root Gradle passed.

## M4 User Preference Memory
- status: done
- evidence: PreferenceRuleCard is scoped and confidence-bearing, workflow contracts remain separate, and ContextPack only applies preferences relevant to the current repo/task context; worker full suite and root Gradle passed after M4.2.

## M5.1 Markdown authority bundle builder
- status: done
- evidence: RED missing `authority_bundle` module; GREEN `build_markdown_authority_bundle` renders a pure reviewable bundle with `index.md`, `documents/`, `workflows/`, `preferences/`, and `evidence-gaps/` entries; document files include source/status/evidence/confidence and generated artifact markers; focused tests passed, worker full suite passed with 1189 passed / 9 skipped, and root Gradle passed.

## M5.2 Markdown authority bundle drift check
- status: done
- evidence: RED missing `check_markdown_authority_bundle_drift`; GREEN drift check compares expected ContextPack bundle with current file map and reports missing, extra, and changed files; focused tests passed, worker full suite passed with 1190 passed / 9 skipped, and root Gradle passed.

## M5 OKF / Markdown Authority Bundle
- status: done
- evidence: Bundle export includes source, evidence, status, confidence, and generated artifact markers; bundle files are deterministic Markdown/frontmatter targets suitable for Git review; ContextPack vs bundle drift check exists; worker full suite and root Gradle passed after M5.2.

## M6.1 ContextPack response modes
- status: done
- evidence: RED `ContextPack.to_dict(mode=...)` was unsupported; GREEN `full` keeps the existing wire shape while `compact` and `degraded` preserve schema/memory/graph/bridge/authority status fields and omit verbose sections explicitly; focused ContextPack/MCP tests passed, worker full suite passed with 1191 passed / 9 skipped, and root Gradle passed.

## M6.2 Agent-facing response mode controls
- status: done
- evidence: RED CLI rejected `--response-mode`, MCP schema lacked `response_mode`, and MCP compact calls returned full packs; GREEN CLI and MCP accept `full`, `compact`, and `degraded`, while existing full responses remain backward-compatible; focused CLI/MCP tests passed, worker full suite passed with 1194 passed / 9 skipped, and root Gradle passed.

## M6.3 Search mirror status in Context Authority Pack
- status: done
- evidence: RED ContextPack authority block did not expose the Qdrant/Docling searchable mirror role required by the design; GREEN `authority.search_mirror.qdrant_docling` reports searchable mirror status as candidate-only, not canonical memory, and requiring document authority join before product use. Status is no longer hardcoded as unavailable: unset mirror status is `unverified`, configured-without-live-probe is `configured_unverified`, and future probe paths can supply `available`, `degraded`, or `unavailable`; focused ContextPack/bundle/projection/MCP tests cover the contract.

## M6.4 Consumer read-only contract
- status: done
- evidence: RED MCP schema did not expose `consumer`, and ContextPack authority output did not state Codex/Claude Code/Hermes read-only consumer boundaries; GREEN `consumer` accepts `unspecified`, `codex`, `claude-code`, and `hermes`, `authority.consumer_contract` marks all consumers read-only with `mutation_allowed=false`, and MCP stdio serves the same ContextPack contract to Codex, Claude Code, and Hermes; focused tests passed with 12 passed, worker full suite passed with 1213 passed / 9 skipped, and root Gradle passed.

## M6 Context Pack API Hardening
- status: done
- evidence: Codex/Claude/Hermes MCP smokes pass, compact/full/degraded response contracts are tested through model/CLI/MCP paths, authority projection consistency remains covered by focused projection tests, Qdrant/Docling mirror status is explicit as candidate-only/non-canonical and unverified unless a status seam supplies evidence, consumer contracts remain read-only, and backend boundary guardrails pass in the full worker suite.

## M7.1 Dendrite local evidence edge input contract
- status: done
- evidence: RED missing `local_evidence` module; GREEN `LocalEvidenceEdge` and `local_evidence_edges_from_capture` build `SessionFile` and `CommitFile` locator/hash edges while rejecting raw body fields so raw file bodies remain local; focused tests passed, worker full suite passed with 1196 passed / 9 skipped, and root Gradle passed.

## M7.2 Local evidence graph projection input
- status: done
- evidence: RED missing `local_evidence_episodes_from_capture`; GREEN local evidence capture records project into `LocalEvidenceEdge` ontology episodes and can be written/read through the graph projection worker without raw body fields; worker full suite passed with 1197 passed / 9 skipped, and root Gradle passed.

## M7 Dendrite Local Evidence Capture
- status: done
- evidence: `Session -> File` and `Commit -> File` edge inputs are available as locator/hash records, raw local file bodies are rejected by contract, and the resulting capture artifacts can feed later `neurons-local` graph consumption; worker full suite and root Gradle passed after M7.2.

## M8.1 neurons-local central-safe sync artifact
- status: done
- evidence: RED missing `local_brain` module; GREEN `build_neurons_local_sync_artifact` emits `neurons_local_sync_artifact.v1` with device/project/hash-only evidence edges, marks `central_safe=true`, and rejects raw body residue; focused tests passed, worker full suite passed with 1199 passed / 9 skipped, and root Gradle passed.

## M8.2 neurons-local offline ContextPack
- status: done
- evidence: RED missing `resolve_neurons_local_context`; GREEN local memory cards plus local evidence records resolve an offline compact ContextPack with `local_mode=true`, unavailable external graph status, and central-safe sync artifact attached; worker full suite passed with 1200 passed / 9 skipped, and root Gradle passed.

## M8 neurons-local per-PC Brain
- status: done
- evidence: One PC can resolve local context offline from accepted local cards, local node emits central-safe sync artifacts, and local privacy policy rejects raw body residue; worker full suite and root Gradle passed after M8.2.

## M9.1 neurons-central federation merge/conflict contract
- status: done
- evidence: RED missing `central_federation` module; GREEN `federate_neurons_local_artifacts` dedupes central-safe local artifacts across devices, reports file content hash conflicts with device/content evidence, and rejects raw file body or graph DB residue; focused tests passed, worker full suite passed with 1203 passed / 9 skipped, and root Gradle passed.

## M9 neurons-central Federation
- status: done
- evidence: Central authority can merge multiple device artifacts, conflicts are explainable, and raw PC file bodies plus graph DB files are not centrally synced; worker full suite and root Gradle passed after M9.1.

## M10.1 RepoStyleProfile builder
- status: done
- evidence: RED missing `repo_style_profile` module; GREEN `repo_style_profile_from_memory_cards` builds file/commit/session-linked repo style claims and explicitly ignores user preferences or insufficient historical observations as non-style-authority inputs; focused tests passed, worker full suite passed with 1204 passed / 9 skipped, and root Gradle passed.

## M10 Repo Style Profile
- status: done
- evidence: Style claims link to files, commits, sessions, and repo scope, and the system distinguishes user preference from accidental historical code; worker full suite and root Gradle passed after M10.1.

## Infra-A.1 Compose baseline report
- status: done
- evidence: RED missing `infra_baseline` module; GREEN `compose_baseline_report` reports compose as the runtime target, keeps `k3s_migration_implied=false`, checks loopback-published ports, restart/healthcheck/volume service coverage, profile-gated services, and safe delivery defaults with live queue/delivery off by default; focused tests passed, worker full suite passed with 1205 passed / 9 skipped, and root Gradle passed.

## Infra-A Compose Baseline / Hardening
- status: done
- evidence: Compose remains the near-term operational target, safe shadow/delivery-off defaults are reportable, and the report explicitly does not imply production k3s migration; worker full suite and root Gradle passed after Infra-A.1.

## Infra-B.1 k3s PoC canary plan contract
- status: done
- evidence: RED missing `k3s_poc_canary_plan`; GREEN plan contract enforces non-production namespace, private access policy, stateless Deployment canaries first, no stateful DB migration, compose rollback target, and operator approval before execution; focused tests passed, worker full suite passed with 1207 passed / 9 skipped, and root Gradle passed.

## Infra-B.2 k3s PoC canary manifest bundle
- status: done
- evidence: RED missing `k3s_poc_canary_manifest_bundle`; GREEN manifest bundle renders non-production Namespace, stateless Deployment resources, Tailscale-private NetworkPolicy, rollback commands to scale down canaries and restore compose, and preserves `production_migration_implied=false` plus `stateful_db_migration_allowed=false`; focused tests passed, worker full suite passed with 1208 passed / 9 skipped, and root Gradle passed.

## Infra-B.3 k3s PoC operator approval packet
- status: done
- evidence: RED missing `k3s_poc_operator_approval_packet`; GREEN approval packet lists server-side dry-run, apply, rollout/pod postchecks, compose rollback proof commands, marks external mutation and rollback proof as required, and remains `approved=false` until operator approval; focused tests passed, worker full suite passed with 1209 passed / 9 skipped, and root Gradle passed.

## Infra-B.4 k3s PoC execution evidence verifier
- status: done
- evidence: RED missing `k3s_poc_execution_evidence`; GREEN verifier requires an explicit operator approval record, validates dry-run/apply/postcheck/rollback command result records without executing infrastructure commands itself, marks canary and rollback proof separately, and blocks when rollback proof or approval is missing; focused tests passed with 12 passed, worker full suite passed with 1212 passed / 9 skipped, and root Gradle passed.

## Infra-B.5 live k3s canary and rollback proof
- status: done
- evidence: recorded operator claim, not a durable current-runtime proof artifact. The claimed run says Ubuntu `ragflow-box` k3s had one Ready control-plane node before the PoC, a temporary `neurons-canary` namespace ran a stateless `llm-brain-mcp` canary first with a public stateless image and then with `neurons-mcp-neuron-knowledge-mcp:latest` via a temporary local registry, `neuron-knowledge --help` and local HTTP postcheck succeeded, rollback scaled the Deployment to 0, compose stayed healthy, and the namespace plus temporary registry were removed. `current_runtime_verified=false` until a redacted execution evidence artifact records timestamp, approval, image digest, namespace cleanup, compose postcheck, and rollback proof. No DB, Neo4j, Qdrant, CouchDB, or production k3s migration was performed.

## Infra-B k3s PoC
- status: done
- evidence: Repo-local canary plan, manifest bundle, operator approval packet, and execution evidence verifier exist. The live Ubuntu k3s canary entry is recorded as an operator claim that supports follow-up hardening, not as current production migration readiness. Stateful DB migration was not attempted, and `current_runtime_verified=false` until durable redacted evidence is checked in or attached through the evidence lane.
