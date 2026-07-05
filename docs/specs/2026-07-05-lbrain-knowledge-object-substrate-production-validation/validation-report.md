# LBrain Knowledge Object Substrate Production Validation Report

## Final Status

`PASS_WITH_GAPS`

Local implementation, package-level contracts, MCP dispatch tests, CLI smoke tests, and production-denial safety gates passed.

P1 live production activation follow-up now validates the deployed HTTP MCP runtime and the user-level configured endpoint: object-native tools are exposed, `brain_objects_query` returns an object pack, and production proposal/decision calls deny with no mutation. The remaining gap is the current Codex session's `mcp__lbrain` tool registry, which still does not expose object-native tools even though the configured endpoint smoke passes.

## Validated

### local.worker.full-regression

- status: `validated`
- evidence: `cd worker && uv run pytest -q`
- result: `1406 passed, 9 skipped, 1 warning`
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
- evidence: read-only live MCP smoke against the deployed production HTTP MCP runtime.
- result:
  - deployed runtime exposes `brain_objects_query`, `brain_object_explain`, `brain_corpus_status`, `brain_corpus_ingest_plan`, `brain_object_proposal_create`, `brain_object_decision_commit`, and `brain_review_proposals`.
  - tool count: 27
  - deployment health: `Synced/Healthy`, ready `1/1`, restart count `0`
  - service health: `status=ok`

### configured.codex-endpoint.http-mcp-object-tools-loaded

- status: `validated`
- evidence: standalone MCP client smoke against the user-level Codex LBrain MCP endpoint from local config.
- result:
  - configured endpoint exposes `brain_objects_query`, `brain_object_explain`, `brain_corpus_status`, `brain_corpus_ingest_plan`, `brain_object_proposal_create`, `brain_object_decision_commit`, and `brain_review_proposals`.
  - tool count: 27
  - `brain_objects_query` returned `brain_objects_query.v1` with `object_pack.v1`, `route=documentation_cleanup`, one public-safe object, and two explicit gaps.
  - production proposal and restricted decision calls returned denied/no-mutation.

### live.production.brain-objects-query

- status: `validated`
- evidence: read-only live `brain_objects_query` smoke.
- result: returned `brain_objects_query.v1` with `object_pack.v1`, `route=documentation_cleanup`, one public-safe object, and two explicit gaps.

### live.production.deployed-version-identity

- status: `validated`
- evidence: deployed MCP image identity and Git ancestry check.
- result:
  - production Argo application tracks `main` and is `Synced/Healthy`.
  - deployed MCP image source commit is `c216ff4`.
  - source commit `c216ff4` includes PR #64 merge commit `7a0b6a6`.
  - repo `origin/main` is ahead at PR #66; that does not invalidate the P1 MCP evidence because the deployed MCP image identity still includes the object-native tool merge.

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

## Gaps

- Current Codex session's `mcp__lbrain` tool registry must refresh/reload to expose object-native tools directly.
- P1 remains `PASS_WITH_GAPS` until the agent-visible `mcp__lbrain` namespace can call `brain_objects_query` without a separate standalone MCP client probe.
- Reference corpus store remains not configured; local CLI correctly reports planned/no mutation rather than pretending ingest completed.
- Golden query baseline remains red by design; future goal must evaluate the new object-pack answers against those queries after deployment.

## Stop Conditions Checked

- No production ledger write was performed.
- No corpus production ingest was performed.
- No graph/Qdrant write, GC, accepted/current promotion, corpus write, ledger write, or raw private evidence access was performed during validation.
- Production denial gate did not mutate state.

## Conclusion

The implementation is locally and contractually validated, safety gates fail closed, and the deployed/configured HTTP MCP runtime is now production-runtime verified for P1 read-only object tools. The result remains `PASS_WITH_GAPS` because the current Codex session's `mcp__lbrain` tool registry still does not expose object-native tools directly.
