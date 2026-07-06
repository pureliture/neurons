# LBrain Knowledge Object Substrate Production Validation Report

## Final Status

`PASS_WITH_GAPS`

Local implementation, package-level contracts, MCP dispatch tests, CLI smoke tests, production-denial safety gates는 통과했습니다.

P1 live production activation follow-up는 deployed HTTP MCP runtime 및 user-level configured endpoint를 검증했습니다: object-native tools가 노출되고, `brain_objects_query`는 explicit authority gaps가 포함된 object pack을 반환하며, production proposal/decision calls는 mutation 없이 deny됩니다. 남은 gaps는 분리되어 있습니다: configured endpoint smoke는 통과하지만 현재 Codex session의 `mcp__lbrain` tool registry는 object-native tools를 아직 노출하지 않으며, live MCP image가 #73/current-main source refactor를 포함하는지는 증명되지 않았습니다.

PR #73 및 ops deploy-button merge 이후 최신 recheck 결과는 `PASS_WITH_GAPS`로 유지됩니다: configured endpoint는 여전히 object-native tools를 노출하고 production proposal/decision mutation을 deny하지만, 이 Codex session은 여전히 stale `mcp__lbrain` callable registry를 가지고 있으며 current-main MCP image identity는 증명되지 않은 상태입니다.

PR #95 source-to-candidate activation continuation은 local/test product surface를 확장했으며, post-deploy sanitized evidence packet을 평가하는 `source-to-candidate-runtime-readiness` CLI와 `brain_source_to_candidate_runtime_readiness` MCP tool을 추가했습니다. 현재 branch-local smoke는 `PASS_WITH_GAPS`입니다: local product surface는 validated지만 live MCP review/readiness tools, live agent context `tool_hints`, deployed identity, and live production denial smokes는 evidence packet이 없어 `not_validated`로 남습니다. 이 command/tool은 network나 production mutation을 수행하지 않습니다.

## Validated

### local.worker.full-regression

- status: `validated`
- evidence: `cd worker && uv run pytest -q`
- result: `1586 passed, 9 skipped, 1 warning`
- note: covers object model, reference corpus, object packs, MCP stdio, CLI, context authority, ledger area boundary, and existing worker regression surface.

### local.root.gradle

- status: `validated`
- evidence: `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
- result: `BUILD SUCCESSFUL`

### local.static.diff-check

- status: `validated`
- evidence: `git diff --check`
- result: pass

### local.mcp.object-tools

- status: `validated`
- evidence: `uv run pytest -q tests/test_neuron_mcp_stdio.py tests/test_neuron_cli.py tests/test_context_authority_pack.py tests/test_object_packs.py tests/test_knowledge_objects.py -q`
- result: pass
- covered claims:
  - `brain_objects_query` returns object-pack shape with lane/gap/action fields.
  - `brain_object_proposal_create` writes only local/test ledger proposals.
  - `brain_review_proposals` reads object-native local/test proposal metadata.
  - `brain_object_decision_commit` is restricted-denied by default.
  - `brain_corpus_ingest_plan` reports `manifest_ref_not_loaded` when only a ref is provided.

### local.cli.object-query

- status: `validated`
- evidence: `uv run neuron-knowledge object-query --repository neurons --branch codex/lbrain-knowledge-object-substrate --query '이 repo 문서 최신화하려면 뭘 봐야 해?'`
- result: returned `brain_objects_query.v1` with `documentation_cleanup` object pack and explicit gaps: `accepted_current documents empty`, `review_proposals_needed`.

### local.cli.okf-export

- status: `validated`
- evidence: `uv run neuron-knowledge okf-export --root okf`
- result: returned export preview file list for manifest, objects, edges, evidence, and documentation cleanup pack.

### local.cli.corpus-ingest-plan

- status: `validated`
- evidence: `uv run neuron-knowledge corpus-ingest-plan --project neurons --storage-mode metadata_only --corpus-name palantir-ontology`
- result: returned `reference_corpus_ingest_plan.v1`, `authority_lane=reference_only`, `writes_planned=false`.

### local.golden-query-baseline

- status: `validated`
- evidence: `uv run neuron-knowledge golden-query-eval --baseline`
- result: returned `knowledge_object_golden_query_eval.v1`, `status=baseline_red`.
- interpretation: baseline-red is expected for the legacy/current response shape and is the regression target for future production-quality answers.

### local.product-activation-progress-gate

- status: `validated`
- evidence: `uv run neuron-knowledge golden-query-eval --activation-progress`
- result: returned `lbrain_product_activation_progress.v1`, `status=PASS_WITH_GAPS`, `release_quality_gate=not_green`, `minimum_review_loop_checkpoint.status=PASS_WITH_GAPS`, `next_phase=P5`, `goal_complete=false`, `production_ready=false`, `product_evidence_status=PASS`, `production_approval_gate=preapproved`, `production_mutation_execution=not_performed_by_local_gate`, `product_evidence_summary phases=P6/P7/P8/P9`, `production_mutation_performed=false`.
- interpretation: this is a local P5 progress gate that keeps P2-P9 scope and gaps visible. The evidence summary covers P6 session/project/work-unit rollup, P7 artifact preference memory, P8 runtime authority preview, and P9 agent context product pack as sanitized local previews only. The human approval gate for production ledger/corpus/runtime mutation is preapproved, but this local gate did not execute production mutation and does not prove production readiness or deployed/runtime activation.

### local.source-to-candidate-runtime-readiness-surface

- status: `validated`
- evidence: `uv run neuron-knowledge source-to-candidate-runtime-readiness --expected-commit 7218cb2`
- result: returned `source_to_candidate_runtime_readiness.v1`, `status=PASS_WITH_GAPS`, `live_evidence_provided=false`, `production_mutation_performed=false`, `network_used=false`
- interpretation: this validates the report surface and local product-surface claim only. It does not prove deployed/runtime source-to-candidate activation.

### local.mcp.source-to-candidate-runtime-readiness-tool

- status: `validated`
- evidence: focused MCP stdio tests for `brain_source_to_candidate_runtime_readiness`
- result:
  - sanitized evidence packet returns `source_to_candidate_runtime_readiness.v1`, `status=PASS`
  - missing evidence returns `PASS_WITH_GAPS`
  - `production_mutation_performed=false`
  - `network_used=false`

### lbrain.current-read-path

- status: `validated`
- evidence: LBrain MCP `memory_authority_pack_read(project=neurons)` and `brain_context_resolve(...)`
- result:
  - accepted/current authority pack count: 7
  - current authority includes live mutation requiring separate gates
  - compact context resolves current design file as active inventory candidate
  - runtime evidence remains `runtime_evidence_unverified`

### live.production.http-mcp-object-tools-loaded

- status: `validated`
- evidence: read-only live MCP smoke against the deployed production HTTP MCP runtime on 2026-07-06.
- result:
  - deployed runtime exposes `brain_objects_query`, `brain_object_explain`, `brain_corpus_status`, `brain_corpus_ingest_plan`, `brain_object_proposal_create`, `brain_object_decision_commit`, and `brain_review_proposals`.
  - tool count: 27
  - redacted live rollout and service-health evidence was captured outside this public document.

### configured.codex-endpoint.http-mcp-object-tools-loaded

- status: `validated`
- evidence: standalone MCP client smoke against the user-level Codex LBrain MCP endpoint from local config; rechecked 2026-07-06 after PR #73 was merged.
- result:
  - configured endpoint exposes `brain_objects_query`, `brain_object_explain`, `brain_corpus_status`, `brain_corpus_ingest_plan`, `brain_object_proposal_create`, `brain_object_decision_commit`, and `brain_review_proposals`.
  - tool count: 27
  - latest `brain_objects_query` smoke returned `brain_objects_query.v1` with `object_pack.v1` and `route=authority_archive_separation`.
  - production proposal and restricted decision calls returned denied/no-mutation.

### live.production.brain-objects-query

- status: `validated`
- evidence: read-only live `brain_objects_query` smoke.
- result: returned `brain_objects_query.v1` with `object_pack.v1`, `route=documentation_cleanup`, and explicit authority gaps.

### live.production.deployed-version-identity

- status: `validated_for_object_tools` / `gap_for_current_main_identity`
- evidence: redacted deployed MCP image identity check, Git ancestry check, source main check, and latest GitOps desired-state recheck.
- result:
  - public source `origin/main` contains PR #73.
  - redacted private evidence showed the deployed MCP image identity was sufficient for object-tool availability, but not sufficient to prove current-source-main/#73 identity.
  - latest desired-state recheck still did not provide current-source-main MCP image identity.
  - this public report intentionally omits raw ops revision, live image identity, and runtime-status values; those belong in `neurons-ops` or private evidence storage.
  - current shell could not directly re-read live runtime controller status, so desired-state evidence was not upgraded into a live rollout identity claim.

## Denied As Expected

### production.corpus-ingest

- status: `denied_as_expected`
- evidence: `uv run neuron-knowledge corpus-ingest --project neurons --target production`
- result: exit code 1 with `status=denied`, `mutation_performed=false`, `network_used=false`

### production.object-proposal-and-decision

- status: `denied_as_expected`
- evidence: focused MCP stdio tests and read-only live HTTP MCP smoke
- result:
  - production-scope object proposal is denied with no authoritative memory change.
  - object decision commit is restricted-denied by default with `authority_write_performed=false`.
  - live smoke returned `proposal_write_performed=false`, `authority_write_performed=false`, and `authoritative_memory_changed=false` for both production proposal denial and restricted decision denial.

## Not Validated

### configured.codex-mcp.object-tools-loaded

- status: `not_validated`
- reason: `current_session_tool_registry_stale`
- evidence:
  - deployed HTTP MCP runtime exposes object-native tools.
  - local Codex MCP allowlist source has been updated to include object-native tool names.
  - standalone smoke against the configured endpoint exposes and calls object-native tools successfully.
  - current Codex `mcp__lbrain` callable namespace still exposes the pre-object-native read tools only in this session.
  - `brain_objects_query` is not callable from the configured Codex namespace in this session.

### configured.codex-mcp.runtime-verified-answers

- status: `not_validated`
- reason: `current_session_tool_registry_missing_new_tools`
- evidence: configured Codex namespace cannot yet run `brain_objects_query` directly, so agent-facing product activation is not complete from this session's callable tool surface.

### live.production.source-to-candidate-review-tools

- status: `not_validated`
- reason: `live_evidence_packet_not_supplied`
- evidence:
  - local readiness report expects `brain_source_to_candidate_graph`, `brain_candidate_review_edit`, `brain_approval_board_decide`, and `brain_source_to_candidate_runtime_readiness` in deployed MCP `tools/list`.
  - current branch-local smoke did not contact live MCP and therefore reports `live_mcp_review_tools_unverified`.

### live.production.agent-context-tool-hints

- status: `not_validated`
- reason: `live_evidence_packet_not_supplied`
- evidence:
  - local readiness report expects live `agent_context_product_pack.v1` to include object-native review `tool_hints`.
  - current branch-local smoke did not read deployed agent startup/context output and therefore reports `live_agent_context_tool_hints_unverified`.

### live.production.source-to-candidate-denial-smokes

- status: `not_validated`
- reason: `live_evidence_packet_not_supplied`
- evidence:
  - local readiness report requires live production-denial evidence for `brain_source_to_candidate_graph` and `brain_approval_board_decide`.
  - current branch-local smoke did not call deployed production-denial tools and therefore reports denial claims as `not_validated`.

### live.production.current-main-image-identity

- status: `not_validated`
- reason: `live_mcp_image_not_current_source_main`
- evidence:
  - public source `origin/main` includes PR #73.
  - redacted live MCP image proof remains below current-source-main identity.
  - latest GitOps desired-state recheck still does not show a current-source-main MCP image.
  - direct live runtime controller status could not be re-read from this shell, so no stronger live rollout identity evidence was captured.
  - this does not invalidate the object-native P1 tool proof, but it prevents claiming that the #73/current-main source is deployed in MCP.

## Gaps

- Current Codex session's `mcp__lbrain` tool registry must refresh/reload to expose object-native tools directly.
- P1 remains `PASS_WITH_GAPS` until the agent-visible `mcp__lbrain` namespace can call `brain_objects_query` without a separate standalone MCP client probe.
- Live MCP image identity must move to current source `main` before claiming PR #73 is deployed in MCP.
- Direct live Kubernetes/Argo status access must be available, or equivalent redacted live evidence must be supplied, before desired-state GitOps evidence is described as live rollout evidence.
- Reference corpus store remains not configured; local CLI correctly reports planned/no mutation rather than pretending ingest completed.
- Golden query baseline remains red by design; future goal must evaluate the new object-pack answers against those queries after deployment.

## Stop Conditions Checked

- No production ledger write was performed.
- No corpus production ingest was performed.
- No production proposal or authority decision write was performed; denial smokes reported `proposal_write_performed=false`, `authority_write_performed=false`, and `authoritative_memory_changed=false`.
- No graph/Qdrant write, GC, accepted/current promotion, corpus write, ledger write, or raw private evidence access was performed during validation.
- Production denial gate did not mutate state.

## Conclusion

Implementation은 local 및 contract scope에서 검증되었고 safety gates는 fail-closed로 동작하며, deployed/configured HTTP MCP runtime은 P1 read-only object tools에 대해 production-runtime verified 상태입니다. 결과는 `PASS_WITH_GAPS`로 유지됩니다. 현재 Codex session의 `mcp__lbrain` tool registry가 object-native tools를 직접 노출하지 않고, live MCP image identity가 current-source-main / PR #73이라고 증명되지 않았기 때문입니다.
