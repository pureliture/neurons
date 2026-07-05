# LBrain Knowledge Object Substrate Production Validation Report

## Final Status

`PASS_WITH_GAPS`

Local implementation, package-level contracts, MCP dispatch tests, CLI smoke tests, and production-denial safety gates passed. Live production inclusion is not validated: the current worktree has uncommitted implementation files, and the configured LBrain MCP read path does not yet expose the new object-native tools.

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

## Denied As Expected

### production.corpus-ingest

- status: `denied_as_expected`
- evidence: `uv run neuron-knowledge corpus-ingest --project neurons --target production`
- result: exit code 1 with `status=denied`, `mutation_performed=false`, `network_used=false`

### production.object-proposal-and-decision

- status: `denied_as_expected`
- evidence: focused MCP stdio tests
- result:
  - production-scope object proposal is denied with no authoritative memory change.
  - object decision commit is restricted-denied by default with `authority_write_performed=false`.

## Not Validated

### live.production.object-tools-loaded

- status: `not_validated`
- reason: `not_deployed_to_production`
- evidence:
  - current worktree branch is `codex/lbrain-knowledge-object-substrate`.
  - implementation is still working-tree/untracked changes, not a deployed artifact.
  - available live/configured LBrain MCP namespace exposes existing read tools such as `memory_authority_pack_read`, `brain_context_resolve`, and `brain_memory_search`; it does not expose the new object-native tools in this session.

### live.production.runtime-verified-answers

- status: `not_validated`
- reason: `runtime_surface_missing_new_tools`
- evidence: configured LBrain MCP read path still reports stale/no-recent-source and runtime evidence gaps; no live `brain_objects_query` call is available on the deployed/configured tool surface.

### production.deployed-version-identity

- status: `not_validated`
- reason: `no_deployed_artifact_contains_worktree_changes`
- evidence: current changes are not committed or promoted through deployment. No image/GitOps/runtime identity check can honestly prove inclusion of these working-tree changes.

## Gaps

- Production deployment/promotion is outside this goal.
- Live MCP `tools/list` for the deployed runtime must expose the new object-native tool names before runtime verification can pass.
- A later production validation run should compare deployed source/image identity against the commit that contains this implementation.
- Reference corpus store remains not configured; local CLI correctly reports planned/no mutation rather than pretending ingest completed.
- Golden query baseline remains red by design; future goal must evaluate the new object-pack answers against those queries after deployment.

## Stop Conditions Checked

- No production ledger write was performed.
- No corpus production ingest was performed.
- No deployment, GitOps, image push, Argo sync, restart, graph/Qdrant write, GC, or raw private evidence access was performed.
- Production denial gate did not mutate state.

## Conclusion

The implementation is locally and contractually validated, and safety gates fail closed. It is not production-runtime verified because the deployed/configured LBrain read path does not yet include this working-tree implementation. The correct next production step is a separate promotion/deployment plan, followed by read-only live `tools/list` and object-query smoke against that deployed artifact.
