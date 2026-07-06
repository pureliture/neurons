# LBrain Ontology-Style Knowledge Product Roadmap

## Status

이 roadmap은 calendar가 아니라 evidence gate를 기준으로 진행합니다.

이 문서는 percentage completion을 부여하지 않습니다. 첫 formal denominator는 여기서 시작합니다: 각 phase는 gate evidence가 있고 production/read-path 상태가 정직하게 label될 때만 완료됩니다.

Current state:

- Phase 1 substrate implementation: local/test scope에서는 완료되었습니다.
- Production validation follow-up: `PASS_WITH_GAPS`; local/safety gates와 latest-main deployed HTTP MCP runtime/configured endpoint smoke는 통과했지만, 현재 Codex session tool registry에는 object-native tools가 아직 없습니다. production ledger/corpus/runtime mutation approval gate는 사전승인되었지만, bounded production pilot/write evidence는 아직 실행되지 않았습니다.
- P1 Production MCP Activation: `PASS_WITH_GAPS`; deployed/configured HTTP MCP는 latest main 기반 image로 object-native tools를 노출하고, read-only query 및 production write denied/no-mutation smoke를 통과했습니다. 현재 Codex session의 `mcp__lbrain` namespace가 아직 object-native tools를 직접 노출하지 않는 gap은 남아 있습니다.
- P2 Living Reference Corpus Store: `PASS_WITH_GAPS`; local/test corpus policy, configured local/test store, first-class reference object rows, CLI/MCP status, idempotence, unscoped production-denial evidence는 존재하지만, real private Palantir manifest ingest 및 bounded production ingest pilot evidence는 여전히 gap입니다.
- P3 Processing And Object Extraction Pipeline: `PASS_WITH_GAPS` / `local_validated`; local/test reference corpus extraction preview는 deterministic objects, edges, public-safe chunk preview, strategy comparison, evaluator evidence, blocked-extraction gaps를 생성합니다. local_test `source-to-candidate-graph` CLI 및 `brain_source_to_candidate_graph` MCP tool은 configured reference corpus store를 candidate graph review pack으로 연결합니다. candidate graph review pack은 candidate objects/edges/evidence/confidence/edit actions를 surface하고 reviewer edit fixture는 authority mutation 없이 candidate state만 바꿉니다. `source-to-candidate-runtime-readiness` CLI 및 `brain_source_to_candidate_runtime_readiness` MCP tool은 post-deploy sanitized evidence packet을 PASS/PASS_WITH_GAPS/FAIL로 판정합니다. deployed/runtime source-to-candidate wiring 및 live graph/Qdrant projection join은 아직 증명되지 않았습니다.
- P4 Review Queue And Authority Promotion: `PASS_WITH_GAPS` / `local_validated`; local/test decision commit은 authority state/audit history를 기록하고, object queries는 local/test stale, superseded, retired, archive-only, rejected states를 surface하며, object explain은 local/test decision history를 반환합니다. `candidate-review-edit` / `approval-board-decide` CLI 및 `brain_candidate_review_edit` / `brain_approval_board_decide` MCP tools가 candidate edit에서 local_test approval-board preview까지 연결합니다. unscoped production denial은 유지되지만, scoped production mutation gate는 사전승인 상태이며 실제 production authority pilot/write evidence는 아직 없습니다.
- P5 Continuous Golden Query Quality Gates: `PASS_WITH_GAPS` / `in_progress`; phase coverage report는 P1-P10 golden query families를 나열하고, source-to-authority quality gate는 source_to_candidate_graph, candidate_review_edit, approval_board_local_test, authority_read_after_write, production_decision_denial path를 검증합니다. activation progress report는 P2-P9 scope, P2/P3/P4 minimum review-loop checkpoint, next phase P5, remaining P5-P9 gaps를 한 JSON gate로 반환합니다. `product_surface_checks`는 `brain_objects_query`, object-native MCP tool registry surface, runtime readiness tool, local_test/default production-denial policy를 함께 검증합니다. `product_evidence_checks`는 P6-P9 evidence를 fail-closed로 검증하고, report는 `production_approval_gate=preapproved`와 `production_mutation_execution=not_performed_by_local_gate`를 분리해서 반환합니다. release quality gate는 명시적으로 `not_green` 상태로 유지합니다.
- P6 Session, Device, Project, And Work-Unit 360: `PASS_WITH_GAPS` / `local_validated`; local/test session project rollup preview는 Device/Session/Repository/Branch/WorkUnit/Spec/PullRequest/Commit objects를 생성하고, same-device와 all-device fixture rollup을 분리하며, safe handoff pack과 resume context를 반환합니다. live multi-device runtime evidence는 아직 증명되지 않았습니다.
- P7 Preference, Style, And Artifact Memory: `PASS_WITH_GAPS` / `local_validated`; local/test artifact preference pack은 accepted/proposal lanes, profile objects, no-UI HTML artifact check를 검증하지만, live agent context pack 및 production authority promotion은 아직 gap입니다.
- P8 Runtime Truth, Security, And Deployment Authority: `PASS_WITH_GAPS` / `local_validated`; local/test runtime authority policy, artifact identity join, private authority redaction, preapproved-scope permission check, and no-write local gate checks pass, but bounded production runtime authority pilot/write evidence and permission audit remain gaps.
- P9 Agent Context Productization: `PASS_WITH_GAPS` / `local_validated`; local/test consumer compact packs, degraded/stale disclosure, reference object lane, surface policy, proposal-safe action hints, `brain_objects_query` read-path hint, and object-native review/readiness `tool_hints` pass. Runtime readiness report now checks whether live agent context contains those `tool_hints`, but production startup/read path and runtime enforcement remain gaps.
- Product activation: 완료되지 않았습니다; local_test store-to-candidate graph wiring 및 review/approval CLI/MCP proof는 추가되었지만 configured deployed agent read path refresh, deployed/runtime source-to-candidate extraction wiring, deployed review surface hardening, and approval-board promotion runtime integration이 여전히 필요합니다.
- UI/object browser: full UI는 product activation prerequisite가 아니지만, minimal candidate object/edge/evidence edit surface는 P3/P4 product workflow의 prerequisite입니다.

Roadmap lock state:

- This document is the planning SoT only for phase ordering and gate definitions.
- It is not proof that a phase is complete.
- A phase can move to `production_validated` only with live deployed/read-path evidence plus either a no-mutation report when mutation is out of scope, or bounded mutation evidence with postcheck/rollback criteria when mutation is explicitly in scope.

## Product Goal

LBrain should become a working knowledge product for development work, not just a memory retrieval surface.

The target is an ontology-style product in the practical sense:

- typed objects for work knowledge
- typed relationships between those objects
- evidence and freshness on claims
- AI-assisted candidate graph extraction from source material
- human correction of object, edge, and evidence candidates
- proposal/review/action lifecycle
- accepted/current authority separated from archive, reference, graph, search, and runtime evidence
- agent-facing context packs for Codex, Claude, Gemini, Hermes, and later tools

This is not a goal to clone Palantir. Palantir reference material is used to reduce the problem into needed mechanics: object, link, action, function, pipeline, governance, and application surface.

## Product Operating Model

The product direction is:

```text
source material
→ AI extraction
→ candidate KnowledgeObjects, edges, and evidence refs
→ human review and correction
→ approval board decision
→ accepted/current authority only after approval
```

Automatic extraction should help create and maintain the graph, but it must not directly rewrite production authority.

User-facing correction is part of the core workflow, not a cosmetic UI layer. A reviewer must be able to:

- rename or rewrite object claims
- add, remove, merge, or split objects
- add, remove, or correct typed edges
- attach, replace, or reject evidence refs
- promote, reject, hold, stale-mark, supersede, retire, or request more evidence

Authority rule:

```text
AI output = draft/candidate graph
human-approved decision = accepted/current authority
```

Therefore P3 and P4 must include a minimal editable review surface even if the full P10 object browser remains deferred.

## Palantir Corpus Traceability

The current local Palantir reference corpus manifest is the evidence baseline for this roadmap.

Manifest state:

- corpus name: `palantir-ontology`
- source count: 65
- sources with URL: 39
- manual text sources without URL: 26
- source types: PDF 6, web page 33, text 26
- authority lane: `reference_only`
- review state: `unreviewed`
- ingest state: `normalized`

The local filesystem contains raw markdown, normalized markdown, metadata, and manifest files, so file count is higher than source count. Roadmap gates must use manifest source count, not raw filesystem file count. If a later corpus has more sources, it must enter P2 as a new manifest version with its own hash, source count, and freshness gaps.

Corpus-derived roadmap mapping:

| Palantir-derived mechanic | Roadmap phase |
| --- | --- |
| Object, link, action, function, application, governance model | P0, P3, P4, P8, P9 |
| Source-schema is not the domain object | P0, P3 |
| Data, logic, action, security as one decision model | P3, P4, P8 |
| Document extraction strategy, chunking, evaluation, deployment lifecycle | P2, P3, continuous P5 |
| Function evaluation, debugging, variance, model comparison | P3, continuous P5 |
| MCP as context plus tools/actions | P1, P4, P9 |
| Application surface and permission-scoped interaction | P8, P9, P10 |

## Non-Goals

- Do not treat search, graph, session archive, or raw corpus text as accepted/current authority.
- Do not require a UI before CLI/MCP quality is usable.
- Do not store raw private transcript, secret, token, private runtime evidence, raw dataset id, or raw document id in public repo artifacts.
- Do not collapse merge, CI, deploy, and live runtime evidence into one status.
- Do not claim production readiness from local tests.
- Do not assign roadmap percentage until every phase has accepted gate criteria and comparable product weight.

## Current Evidence Baseline

Completed local/test substrate gates:

- `KnowledgeObjectEnvelope`, `KnowledgeEdge`, `EvidenceRef`, `ReviewProposal`, and `AuthorityDecision` model exist.
- `authority_lane` and `verification_state` are separated.
- reference corpus ingest planning exists.
- documentation cleanup, runtime truth, preference/style, and agent context object packs have local/test coverage.
- MCP/CLI object surfaces exist locally.
- production proposal and restricted authority mutation paths deny by default.
- OKF export exists as a review/export companion, not canonical authority.
- golden query baseline exists and records current low-quality behavior.

Known production gaps:

- deployed/configured HTTP LBrain MCP exposes object-native tools, but the current Codex session's `mcp__lbrain` namespace is still stale and does not expose them.
- reference corpus store is not configured as a living LBrain corpus store.
- golden queries are baseline red, not production-quality green.
- accepted/current promotion workflow is not open for production object decisions.
- object extraction and processing pipeline is not wired as an automatic candidate-graph factory.
- editable object/edge/evidence review surface is not implemented.
- session/device/project 360 is specified but not production-usable.

Known roadmap lock gaps from multi-agent review:

- P1 must prove the deployed/configured read path contains the exact commit, image, or artifact that includes object-native tools.
- P2 must close source rights, raw-body retention, deletion, return capability, and managed snapshot policy.
- P3 must include extraction strategy comparison, chunk preview, evaluation metrics, cost/speed, and debug trace gates.
- P4 must define how approved production authority promotion opens, audits, rolls back, and stays scoped.
- P5 must run continuously across P1-P9, not only as a late phase.
- P8/P9 must include object/property/action permission and application restriction gates.

## Continuous Quality Gate

P5 is both a named phase and a continuous gate. It starts at P1 and stays active through P9.

Each phase must define at least one golden query or evaluator slice that proves the new capability improves user-meaningful answers. A phase is not product-complete if it only exposes tools, schemas, or storage while the relevant answer still falls back to generic safety/current cards.

Minimum continuous checks:

- answer includes object, edge, evidence, freshness, gap, and recommended action when applicable
- accepted/current, reference-only, proposal/review, archive, derived projection, and runtime evidence lanes stay separated
- missing authority is stated as a gap instead of hidden
- production/runtime claims require live evidence
- raw/private evidence is redacted or denied
- query routing explains source lanes and stop reason

## Phase Gates

### P0. Local Object Substrate Foundation

State: complete for local/test scope.

Purpose:

Provide the shared object/edge/evidence/review vocabulary used by all later phases.

Done evidence:

- local worker regression passed
- root Gradle regression passed
- object model tests passed
- MCP/CLI object contract tests passed
- production mutation denial tests passed

Remaining boundary:

This phase does not prove production deployment or live LBrain quality.

### P1. Production MCP Activation

State: `PASS_WITH_GAPS` as of 2026-07-06 latest production recheck.

Deployed HTTP MCP runtime activation and configured endpoint smoke passed on a latest-main image. The latest configured-endpoint smoke exposes object-native tools, returns a public-safe `brain_objects_query.v1` response, and denies production proposal/decision mutation without writes. Current Codex session tool-registry activation remains a gap.

Purpose:

Make the new object-native read surfaces available on the configured/deployed LBrain MCP read path.

Required capabilities:

- deployed `tools/list` exposes object-native tools
- deployed object query can answer in public-safe shape
- deployed object explain and corpus status can return gaps without mutation
- configured MCP read path identifies the deployed artifact or source/image identity
- deployed schema exposes read, explain, corpus status, review proposal listing, and proposal-safe action tools
- proposal-safe actions either write only to approved local/test scope or return denied/no-mutation in production
- production write paths remain denied unless a later approved gate opens them

Gate evidence:

- read-only live MCP tool-list proof
- read-only live `brain_objects_query` smoke
- deployed artifact identity check that ties the configured read path to the exact commit, image, or build artifact
- live denial smoke for production proposal/decision calls when no production mutation gate is open
- no production ledger/corpus mutation during activation validation

Current evidence summary:

- latest live production evidence recorded the Argo application tracking `main` and `Synced/Healthy`
- source repo `origin/main` contains PR #93 merge commit `7bda476`
- deployed MCP image identity is tied to a latest-main source image that includes the object-native PR stack
- live HTTP MCP `tools/list` exposes `brain_objects_query`, `brain_object_explain`, `brain_corpus_status`, `brain_corpus_ingest_plan`, `brain_object_proposal_create`, `brain_object_decision_commit`, and `brain_review_proposals`
- live read-only `brain_objects_query` returns `brain_objects_query.v1` in public-safe shape
- user-level Codex LBrain MCP config source includes object-native tools, and standalone smoke against the configured endpoint returns the same object-native tool list and read-only query shape
- latest standalone configured-endpoint smoke exposes all required object-native tools and denies production proposal/decision mutation with no authoritative memory change
- production-scope `brain_object_proposal_create` returns denied/no-mutation
- `brain_object_decision_commit` returns denied/no-mutation
- no production ledger/corpus mutation was performed
- current Codex session's `mcp__lbrain` namespace does not expose `brain_objects_query` even though the configured endpoint smoke passes; this keeps P1 at `PASS_WITH_GAPS`, not `PASS`
- raw live evidence, host topology, private ledger details, and raw dataset/document ids remain outside this public repo

Next gate:

- restart or refresh the Codex LBrain MCP tool registry until the configured `mcp__lbrain` namespace exposes object-native tools directly, then rerun read-only smoke through that configured path.
- keep production authority writes denied until the approval-board and scoped promotion gate are approved.

### P2. Living Reference Corpus Store

State: `PASS_WITH_GAPS`.

Local/test reference corpus store gates pass. The phase is not production-validated, and it is not proof that the private/local Palantir manifest has been ingested.

Purpose:

Turn external reference material, including the Palantir corpus, into managed LBrain reference objects without confusing them with accepted/current authority.

Required capabilities:

- `ReferenceCorpus`, `DocumentSource`, `DocumentSnapshot`, `DocumentVersion`, `DocumentChunk`, `ExtractionRun`, and `FreshnessCheck` persisted in an approved store
- corpus policy supports `external_object_store`, `managed_snapshot`, and `metadata_only`
- source URL, hash, freshness, source rights, storage mode, and missing-evidence gaps are tracked
- raw corpus body policy is explicit per corpus
- retention, deletion, redaction, and return-capability policy is explicit per storage mode
- managed snapshot requires approved source-rights and raw-body policy
- manual text without source URL remains usable as reference material but carries a freshness/source gap
- corpus objects stay `reference_only` until review/promotion

Gate evidence:

- Palantir corpus manifest loads with expected source count, URL count, manual-text gap count, and manifest hash
- corpus status reports source counts, hash state, freshness gaps, and extraction runs
- corpus status reports source-rights, retention, deletion, redaction, and raw-return policy
- repeated ingest is idempotent
- production ingest remains gated

Current local/test evidence summary:

- sanitized reference corpus fixture maps to `ReferenceCorpus`, `DocumentSource`, `DocumentVersion`, `DocumentSnapshot`, `DocumentChunk`, `ExtractionRun`, and `FreshnessCheck` metadata without raw body return
- ingest plan reports manifest hash, hash verification state, source count, missing manual URL gap count, storage mode, raw body policy, and no writes planned
- `corpus-ingest-plan --manifest-file ...` loads an operator-supplied manifest read-only and reports source URL count, manual text count, source type distribution, and manifest hash
- sanitized full-count Palantir-shaped fixture proves the P2 count gate shape for 65 sources, 39 sources with URL, 26 manual text sources without URL, and PDF/Web/Text distribution 6/33/26 without raw body access
- `corpus-ingest-plan --expect-source-count ... --expect-source-url-count ... --expect-manual-text-without-url-count ... --expect-source-type-count ...` compares operator expected counts against the loaded manifest and returns `count_gate_status=pass` without writes
- expected-count mismatches return `count_gate_status=fail`, public-safe `count_gate_gaps`, CLI exit 1, and `writes_planned=false`
- MCP `brain_corpus_ingest_plan` schema and dispatch expose the same expected-count gate for read-only plan validation
- managed snapshot metadata carries raw-return denial, retention, redaction, deletion, and source-rights policy
- re-ingest produces stable corpus/source/snapshot/chunk/run ids for unchanged hashes
- content hash mismatch blocks extraction output instead of creating reference objects
- CLI and MCP `brain_corpus_status` report storage mode support and raw-body/source-rights policy even while the persistent reference corpus store is empty
- local/test ledger-backed `reference_corpus_bundles` store persists sanitized corpus metadata, keeps repeated ingest idempotent by corpus id, and returns read-after-write corpus status counts
- local/test ledger-backed first-class rows persist public-safe `DocumentSource`, `DocumentVersion`, `DocumentSnapshot`, `DocumentChunk`, `FreshnessCheck`, and `ExtractionRun` metadata separately from the aggregate bundle
- `brain_corpus_status` reports first-class store counts plus limited public-safe rows for document sources, versions, snapshots, chunks, freshness checks, and extraction runs
- CLI `corpus-ingest --target local_test --ledger ... --manifest-file ...` can load a sanitized manifest into the local/test store; production target remains denied before manifest/store write
- CLI `corpus-ingest --target local_test --manifest-file ...` and `corpus-status` can use configured `NEURON_REFERENCE_CORPUS_LEDGER` for a local/test store read-after-write path without printing the ledger path
- CLI production corpus ingest remains denied/no-mutation even when a local/test reference corpus ledger is configured
- MCP `brain_corpus_status` reads the local/test ledger-backed corpus store through `KnowledgeSearchService.core_brain()`
- ledger area boundary manifest assigns reference corpus bundle and first-class object tables to the LBrain object/native-memory area and the boundary guard passes
- focused evidence: `cd worker && uv run pytest -q tests/test_reference_corpus.py tests/test_neuron_cli.py tests/test_neuron_mcp_stdio.py`
- focused result: `98 passed, 1 warning`
- ledger boundary evidence: `cd worker && uv run pytest -q tests/test_ledger_area_boundaries.py`
- ledger boundary result: `10 passed`
- worker regression evidence: `cd worker && uv run pytest -q`
- worker regression result: `1509 passed, 9 skipped, 1 warning`
- root regression evidence: `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
- root regression result: `BUILD SUCCESSFUL`

Remaining gaps:

- real local/private Palantir corpus manifest has not been loaded through an approved persistent LBrain corpus store in this phase branch
- sanitized full-count fixture and expected-count gate are local/test contract evidence only; they are not proof that a private/local Palantir manifest exists or has been ingested
- production corpus ingest remains denied/gated
- standalone corpus status still reports `reference_corpus_store_empty` when neither `--ledger` nor configured `NEURON_REFERENCE_CORPUS_LEDGER` is supplied
- no production ledger/corpus mutation has been performed or claimed

### P3. Processing And Object Extraction Pipeline

State: local_validated / PASS_WITH_GAPS.

Purpose:

Convert files, docs, sessions, PRs, commits, runtime evidence, code style, and artifact preferences into editable candidate knowledge objects and edges.

Required capabilities:

- extractor registry for repo documents, reference documents, sessions, work units, PRs, commits, tests, runtime surfaces, style rules, and artifact preferences
- extraction strategy registry for chunking, mapping, summarization, style inference, runtime evidence mapping, and preference inference
- extraction run records with input hash, output object ids, evaluator result, and failure reason
- extraction run records quality metrics, cost estimate, speed, token budget, and debug trace availability
- extracted objects and edges are marked candidate/draft until reviewed
- extraction output includes proposed edits, merge/split suggestions, confidence, and evidence gaps
- chunk preview and public-safe output preview exist before managed snapshot or corpus-derived object promotion
- reviewer can edit object names, claims, edge types, and evidence refs before approval
- evaluator supports deterministic fixture checks, golden query checks, variance checks, and model/prompt comparison where LLM extraction is used
- freshness checks separated from authority decisions
- public-safe projection for every object returned through MCP

Gate evidence:

- fixture extraction creates deterministic objects and edges
- failed extraction reports gaps instead of inventing authority
- extraction strategy comparison exists for Palantir corpus document mapping and repo document cleanup mapping
- chunk preview proves evidence can be inspected without raw/private leakage
- candidate graph preview shows objects, edges, evidence refs, confidence, and edit actions
- reviewer edit fixture changes candidate object/edge/evidence without changing accepted/current authority
- evaluator report ties extractor output to at least one golden query slice
- graph/search projection can join to objects but cannot become canonical authority

Current local/test evidence:

- stacked branch note: this phase branch is based on the P2 reference corpus store branch because P2 is not merged to `main` yet
- extractor registry report exists for implemented `reference_corpus_manifest` and planned-gap extractor entries
- deterministic reference corpus extraction preview creates `ReferenceCorpus` and `ReferenceDocument` objects plus `member_of_corpus` edges
- chunk preview omits raw body storage refs and keeps raw body return denied
- extraction run preview reports quality metrics, zero model calls, zero LLM token budget, speed class, and debug trace availability
- hash mismatch blocks extraction output and reports gaps without inventing authority
- documentation cleanup strategy comparison compares `document_authority_pack_v1` against `path_inventory_only_v1`
- documentation cleanup strategy comparison reports lane counts, evidence counts, recommended action counts, and evaluator evidence for the current-vs-archive golden query slice
- full repo-document extraction preview maps repo inventory into `RepoDocument` objects, `supersedes` / `requires_evidence` edges, evidence refs, recommended actions, and extraction-run metrics
- full repo-document extraction preview reports missing accepted-current document lanes as gaps instead of inventing authority
- runtime truth extraction preview separates PR merge evidence from deployment/runtime truth
- runtime truth extraction preview returns `runtime_evidence_unverified` without inferring deploy from merge when live evidence is missing
- runtime truth extraction preview creates a candidate `RuntimeTruth` object and `validated_by` edge only when sanitized live evidence is explicitly `runtime_verified`
- preference/style extraction preview maps preference and repo-style memory cards into `ArtifactPreference` and `StyleRule` objects
- preference/style extraction preview reports source evidence refs without raw body inference and rejects raw-session-body inference as a strategy gap
- work-unit extraction preview groups session, PR, commit, and test evidence into a candidate `WorkUnit` object
- work-unit extraction preview emits evidence refs and `supported_by_evidence` / `validated_by` edges without raw transcript body return
- session-detail extraction preview maps session metadata into `Session` objects with `part_of_work_unit` and `supported_by_evidence` edges
- session-detail extraction preview keeps raw body return denied, reports ignored raw session body as a gap, and hashes sanitized metadata only
- PR/commit detail extraction preview maps PR metadata, commits, and test runs into separate `PullRequest`, `Commit`, and `TestRun` objects
- PR/commit detail extraction preview emits `includes_commit` and `validated_by` edges, reports missing test refs as gaps, and rejects merge-only runtime truth inference
- graph/search projection join preview maps derived graph/search hits into `ProjectionHit` objects and `projection_join` edges
- graph/search projection join preview keeps projection objects and edges in `derived_projection`, reports unknown join targets as gaps, and rejects projection-as-authority strategy
- local_test `source-to-candidate-graph` CLI reads the configured reference corpus store and returns a `candidate_graph_review` pack without ledger, production, or authority mutation
- `brain_source_to_candidate_graph` MCP tool exposes the same local_test store-to-candidate graph preview and keeps production target denied/no-mutation
- production `source-to-candidate-graph` target is denied/no-mutation before ledger access
- candidate graph review pack exposes candidate objects, edges, evidence refs, confidence, allowed reviewer actions, and minimal editable object fields
- candidate reviewer edit fixture changes candidate object fields, edge type, and evidence summary; rejects direct `authority_lane` edits; preserves the original extraction hash; and keeps `authority_write_performed=false`
- broader evaluator suite preview aggregates deterministic fixture checks, golden-query checks, strategy comparison checks, variance checks, and model/prompt comparison status
- broader evaluator suite preview reports stable deterministic outputs as pass, reports changed outputs as `variance_detected`, and marks model/prompt comparison `not_applicable_no_llm` while all current preview extractors use zero model calls
- candidate review focused evidence: `cd worker && uv run pytest -q tests/test_object_packs.py`
- candidate review focused result: `11 passed, 1 warning`
- store-to-candidate CLI focused evidence: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_extractor_registry_reports_implemented_and_gap_extractors tests/test_neuron_cli.py::test_neuron_knowledge_help_lists_server_owned_commands tests/test_neuron_cli.py::test_neuron_knowledge_source_to_candidate_graph_uses_configured_local_test_store tests/test_neuron_cli.py::test_neuron_knowledge_source_to_candidate_graph_denies_production_without_mutation tests/test_neuron_cli.py::test_neuron_knowledge_source_to_candidate_graph_does_not_create_missing_local_store`
- store-to-candidate CLI focused result: `5 passed, 1 warning`
- store-to-candidate MCP focused evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py::test_mcp_tool_list_exposes_object_substrate_tools tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_graph_and_review_approval_preview_roundtrip tests/test_neuron_mcp_stdio.py::test_mcp_approval_board_preview_denies_production_without_mutation`
- store-to-candidate MCP focused result: `3 passed, 1 warning`
- store-to-candidate CLI smoke: local_test configured store returns `status=PASS_WITH_GAPS`, `candidate_graph_review`, `candidate_count=3`, `accepted_count=0`, and all quality gate checks `PASS`
- store-to-candidate production-denial smoke: production target returns `status=denied`, `mutation_performed=false`, `production_mutation_performed=false`, `network_used=false`, and does not create the requested ledger path
- candidate review adjacent evidence: `cd worker && uv run pytest -q tests/test_object_packs.py tests/test_extraction_pipeline.py tests/test_llm_brain_core_objects_subpackage.py`
- candidate review adjacent result: `49 passed, 1 warning`
- focused evidence: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py`
- focused result: `19 passed, 1 warning`
- adjacent regression evidence: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py tests/test_object_packs.py tests/test_reference_corpus.py tests/test_neuron_cli.py tests/test_neuron_mcp_stdio.py tests/test_preference_authority_model.py tests/test_repo_style_profile.py tests/test_llm_brain_core_package_depth.py tests/test_llm_brain_core_objects_subpackage.py tests/test_llm_brain_core_layering.py`
- adjacent regression result: `180 passed, 1 warning`
- worker regression evidence: `cd worker && uv run pytest -q`
- worker regression result: `1577 passed, 9 skipped, 1 warning`

PASS_WITH_GAPS rationale:

- Local/test P3 gate evidence is present for deterministic extraction, failed extraction gaps, strategy comparison, chunk preview, configured-store-to-candidate graph CLI wiring, candidate review/edit surface, evaluator reports, and derived projection join authority separation.
- The remaining live graph/Qdrant projection join proof requires configured runtime evidence and is not proven by local fixture tests.
- This phase did not perform or claim production authority, corpus, graph, search, or deployment mutation.

Remaining gaps:

- P3 is not production-complete; local/test reference corpus extraction preview, repo-document extraction preview, documentation cleanup strategy comparison, runtime truth extraction preview, preference/style extraction preview, work-unit extraction preview, session-detail extraction preview, PR/commit detail extraction preview, graph/search projection join preview, and broader evaluator suite preview slices are implemented
- evaluator coverage is still local/test only; it covers reference corpus, repo-document cleanup, documentation cleanup, PR merge/deploy truth, preference/style, temporal work recall, session detail extraction, PR commit/test provenance, graph/search projection join, deterministic variance, and no-LLM model/prompt applicability
- graph/search projection join is proven only for local/test fixture hits, not for a live graph/Qdrant projection surface
- deployed/runtime source-to-candidate graph wiring is not live-proven; the new proof is local_test CLI wiring only
- no production authority, corpus, graph, search, or deployment mutation has been performed or claimed

### P4. Review Queue And Authority Promotion

State: `PASS_WITH_GAPS` / local_validated.

Purpose:

Create a closed lifecycle from candidate object to accepted/current, stale, superseded, retired, rejected, or archive-only authority.

Required capabilities:

- approval board for candidate KnowledgeObjects, edges, and evidence refs
- proposal creation for current/stale/supersede/retire/reject decisions
- review queue listing with evidence and confidence
- reviewer actions for promote, reject, hold, merge, split, stale-mark, supersede, retire, and request more evidence
- reviewer edits preserve original AI extraction output as audit evidence
- restricted authority decision gate
- read-after-write proof for local/test and approved production flows
- audit record for who/what promoted an object and from which evidence
- approved production promotion plan with explicit object classes, allowed actions, reviewer role, rollback path, and maximum blast radius
- authority demotion path for stale, superseded, retired, rejected, and archive-only states
- object decision history that can explain why a fact is current, stale, superseded, or retired
- separate permission scopes for human approval, agent proposal, production write, and read-only query

Gate evidence:

- proposal write never changes accepted/current authority
- restricted decision is denied by default in production
- approval board can show candidate object, candidate edges, evidence refs, conflicts, gaps, and recommended action
- reviewer edit changes candidate state without mutating accepted/current authority
- approved local/test decision updates authority lane correctly
- approved production pilot updates only scoped object classes and proves read-after-write
- rollback or supersession can reverse or demote an accepted/current decision without deleting audit history
- audit trail cites proposal id, evidence refs, approver identity hash, before/after lanes, and decision reason
- stale/superseded/retired state is visible in object queries

Current local/test evidence:

- object-native proposal creation writes only to the local/test review queue and reports `proposal_write_performed=true`, `authority_write_performed=false`, and `authoritative_memory_changed=false`
- `brain_review_proposals` can read local/test proposal metadata after write without exposing raw/private evidence
- default and production-scope `brain_object_decision_commit` remains denied/no-mutation
- `brain_object_decision_commit` with explicit `ledger_scope=local_test` writes an `AuthorityDecision`, updates local/test object authority state, marks the proposal accepted, invalidates the authority cache, and returns read-after-write evidence
- local/test authority decision audit records proposal id, evidence refs, approver identity hash, previous authority lane, new authority lane, and decision reason
- `brain_objects_query` overlays local/test object authority state onto returned objects and lane indexes after a decision commit
- local/test object queries now surface stale, superseded, retired, archive-only, and rejected states without deleting audit history or mutating production
- `brain_object_explain` returns local/test authority state plus decision history for object ids with committed decisions, while still reporting that the object body comes from ledger state only when no object store is configured
- production-scope `brain_object_decision_commit` remains denied/no-mutation and returns `object_authority_promotion_plan.v1` with allowed object class, decision types, reviewer role, required gate evidence, rollback path, blast radius, and no-mutation report
- ledger boundary manifest assigns `object_review_proposals`, `object_authority_decisions`, and `object_authority_states` to the native-memory/object area
- candidate graph approval-board preview shows editable candidate object state, related edges, evidence refs, confidence, gaps, recommended action, and allowed reviewer actions
- reviewer edit fixture changes only candidate object/edge/evidence state, rejects direct authority-lane edits, preserves original extraction hash, and performs no authority write
- local_test approval-board decision preview promotes a candidate object to `accepted_current`, records an `AuthorityDecision`, rebuilds lane indexes, and marks the write scope as local_test only
- production-scope approval-board decision preview is denied with `production_approval_gate_required` and returns a no-mutation promotion plan
- `candidate-review-edit` CLI applies reviewer JSON edits to candidate packs without authority or production mutation
- `approval-board-decide` CLI connects edited candidate packs to local_test approval-board decisions and keeps production target denied/no-mutation
- `brain_candidate_review_edit` and `brain_approval_board_decide` MCP tools expose the same preview flow for agent surfaces and keep production approval denied/no-mutation
- review/approval CLI focused evidence: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_extractor_registry_reports_implemented_and_gap_extractors tests/test_neuron_cli.py::test_neuron_knowledge_help_lists_server_owned_commands tests/test_neuron_cli.py::test_neuron_knowledge_source_to_candidate_graph_uses_configured_local_test_store tests/test_neuron_cli.py::test_neuron_knowledge_source_to_candidate_graph_denies_production_without_mutation tests/test_neuron_cli.py::test_neuron_knowledge_source_to_candidate_graph_does_not_create_missing_local_store tests/test_neuron_cli.py::test_neuron_knowledge_candidate_review_and_approval_board_cli_chain_local_test tests/test_neuron_cli.py::test_neuron_knowledge_approval_board_cli_denies_production_without_mutation`
- review/approval CLI focused result: `7 passed, 1 warning`
- review/approval MCP focused evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py::test_mcp_tool_list_exposes_object_substrate_tools tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_graph_and_review_approval_preview_roundtrip tests/test_neuron_mcp_stdio.py::test_mcp_approval_board_preview_denies_production_without_mutation`
- review/approval MCP focused result: `3 passed, 1 warning`
- candidate review focused evidence: `cd worker && uv run pytest -q tests/test_object_packs.py`
- candidate review focused result: `11 passed, 1 warning`
- object explain history evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py::test_mcp_brain_object_explain_includes_local_authority_decision_history`
- object explain history result: `1 passed, 1 warning`
- production-denial plan evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py::test_mcp_object_decision_commit_is_restricted_denied_by_default`
- production-denial plan result: `1 passed, 1 warning`
- focused evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py::test_mcp_object_decision_commit_local_test_updates_authority_state_with_audit`
- focused result: `1 passed, 1 warning`
- object-query visibility evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_overlays_local_authority_state`
- object-query visibility result: `5 passed, 1 warning`
- MCP regression evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py`
- MCP regression result: `73 passed, 1 warning`
- object/model/boundary regression evidence: `cd worker && uv run pytest -q tests/test_object_packs.py tests/test_knowledge_objects.py tests/test_ledger_area_boundaries.py`
- object/model/boundary regression result: `23 passed, 1 warning`
- ledger boundary evidence: `cd worker && uv run pytest -q tests/test_ledger_area_boundaries.py`
- ledger boundary result: `10 passed`
- worker regression evidence: `cd worker && uv run pytest -q`
- worker regression result: `1577 passed, 9 skipped, 1 warning`
- root regression evidence: `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
- root regression result: `BUILD SUCCESSFUL`

PASS_WITH_GAPS rationale:

- Local/test P4 gate evidence is present for proposal creation, review queue listing, default production denial/no-mutation, candidate approval-board decision preview, local/test authority decision commit, audit state, stale/superseded/retired/archive/rejected object-query visibility, and object decision history explainability.
- Production authority promotion remains intentionally closed without a human approval gate and scoped live pilot evidence.
- The current production plan is a read-only denied response that documents the required reviewer role, allowed classes/actions, rollback path, gate evidence, and blast radius. It is not a production approval record and did not mutate production authority.

Remaining gaps:

- approved production authority promotion remains closed and unproven; the current production plan is read-only denial metadata, not an approval record or production pilot
- production rollback/supersession/demotion flows are not yet implemented beyond the local/test stored before/after lane audit shape and object-query state overlay
- production proposal/decision write remains denied and no production ledger/corpus mutation has been performed

### P5. Continuous Golden Query Quality Gates

State: `PASS_WITH_GAPS` / `in_progress`; continuous from P1 onward.

Purpose:

Make LBrain quality measurable by user-meaningful questions, not by tool availability.

Required golden query families:

- temporal repo recall
- documentation cleanup
- stale/archive discovery
- code change impact analysis
- PR merge and deploy truth
- current SoT versus stale archive separation
- reference corpus freshness/source authority
- corpus-to-design concept extraction
- code style drift
- HTML/visualization review preference

Required evaluator families:

- extractor fixture quality
- function/evaluator debug trace
- LLM extraction variance
- model or prompt comparison when applicable
- corpus freshness/source-authority checks
- runtime truth evidence checks

Gate evidence:

- every phase has a phase-specific golden query slice
- each query returns object, edge, evidence, freshness, gap, and recommended action
- empty authority lanes are stated explicitly
- runtime claims require runtime evidence
- query routing does not fall back to generic safety/current cards for domain-specific questions
- failed queries are reported as product gaps, not hidden as successful tool calls

Current local/test evidence:

- `golden-query-eval --phase-coverage` returns `knowledge_object_phase_golden_query_coverage.v1`
- phase coverage report lists P1-P10 golden query families, required quality axes, evaluator owner, result status, and explicit gaps
- current report status is `PASS_WITH_GAPS` and `release_quality_gate=not_green`; it does not claim production-quality answers
- `evaluate_object_pack_response(..., required_axes=...)` can now enforce the full object, edge, evidence, freshness, gap, and recommended-action axis set without changing legacy evaluator callers
- strict evaluator fails empty authority lanes unless the gap list explicitly states the empty lane
- strict evaluator fails deployment/runtime truth claims unless runtime evidence or an explicit runtime evidence gap is present
- P1-P4 are represented as PASS_WITH_GAPS slices with their production/live gaps preserved
- source-to-authority quality gate covers source-to-candidate graph, candidate review edit, local_test approval-board promotion, authority read-after-write, and production decision denial paths
- local_test store-to-candidate CLI/MCP proof covers configured reference corpus store read, candidate graph review pack creation, and production target denied/no-mutation
- source-to-authority quality report now includes `product_surface_checks` for `brain_objects_query`, `brain_source_to_candidate_graph`, `brain_candidate_review_edit`, `brain_approval_board_decide`, and `brain_source_to_candidate_runtime_readiness` MCP registry/policy surface
- `brain_objects_query` local MCP read path now returns context-authority object packs for broad authority/archive queries, preference/style object packs for style queries, and runtime truth gap packs for merge/deploy queries instead of falling back to `object_pack_route_not_implemented`
- source-to-candidate runtime readiness CLI can evaluate sanitized post-deploy evidence for MCP read/review tools, `brain_objects_query` route smokes, agent context `tool_hints`, deployed identity, and production-denial smokes without network or mutation
- activation progress report returns `lbrain_product_activation_progress.v1` with `scope_phases=[P2..P9]`, `minimum_review_loop_checkpoint.status=PASS_WITH_GAPS`, `next_phase=P5`, `remaining_phases=[P5..P9]`, `goal_complete=false`, `production_ready=false`, `production_approval_gate=preapproved`, `production_mutation_execution=not_performed_by_local_gate`, and `production_mutation_performed=false`
- activation progress `product_evidence_summary` now includes P6 session/project/work-unit rollup evidence, P7 artifact preference memory evidence, P8 runtime authority evidence, and P9 agent context product evidence as sanitized local previews
- P6 evidence summary includes `object_extraction_session_project_rollup_preview.v1`, `object_count=8`, `edge_count=16`, `evidence_count=1`, and `session_project_handoff_pack.v1`
- P7 evidence summary includes `object_extraction_preference_style_preview.v1`, accepted artifact preference pack status `pass`, and source evidence refs without raw body
- P8 evidence summary keeps merge/deploy/runtime separated with `runtime_unverified_count=1`, production promotion `permission=allowed`, `permission_reason=approved_scope_present`, and `authority_write_performed=false`
- activation progress `product_evidence_checks` returns P6/P7/P8/P9 `result=PASS` and fails closed when required phase evidence is missing or mutation is claimed without evidence
- P9 evidence summary includes `agent_context_product_pack.v1`, Codex tool hints for `brain_objects_query` plus object-native review/readiness tools, style/preference section evidence, and `mutation_allowed=false`
- `candidate_graph_review` packs state empty authority lanes explicitly so P5 strict axis checks do not hide candidate-vs-authority separation
- P6-P9 remain represented with local/test evidence plus production/live gaps, and P10 remains planned
- activation progress focused evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_phase_golden_query_coverage_reports_pass_with_gaps_not_green tests/test_golden_query_eval.py::test_source_to_authority_quality_gate_covers_review_approval_and_read_path_without_production_mutation tests/test_golden_query_eval.py::test_product_activation_progress_keeps_p2_to_p9_scope_visible tests/test_golden_query_eval.py::test_product_evidence_summary_fails_closed_when_required_phase_evidence_is_missing`
- activation progress focused result: `4 passed, 1 warning`
- activation progress adjacent evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py tests/test_neuron_cli.py tests/test_extraction_pipeline.py tests/test_context_pack_builder.py`
- activation progress adjacent result: `84 passed, 1 warning`
- activation progress CLI smoke: `cd worker && uv run neuron-knowledge golden-query-eval --activation-progress`
- activation progress CLI smoke result: `status=PASS_WITH_GAPS`, `release_quality_gate=not_green`, `minimum_review_loop_checkpoint.status=PASS_WITH_GAPS`, `next_phase=P5`, `goal_complete=false`, `production_ready=false`, `product_evidence_status=PASS`, `production_approval_gate=preapproved`, `production_mutation_execution=not_performed_by_local_gate`, `product_evidence_summary phases=P6/P7/P8/P9`, `production_mutation_performed=false`
- source-to-authority gate focused evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_source_to_authority_quality_gate_covers_review_approval_and_read_path_without_production_mutation`
- source-to-authority gate focused result: `1 passed, 1 warning`
- source-to-authority CLI evidence: `cd worker && uv run pytest -q tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_source_to_authority_gate`
- source-to-authority CLI result: `1 passed, 1 warning`
- source-to-authority CLI smoke: `cd worker && uv run neuron-knowledge golden-query-eval --source-to-authority-gate`
- source-to-authority CLI smoke result: `status=PASS_WITH_GAPS`, `release_quality_gate=not_green`, `production_mutation_performed=false`, `authority_write_scope=local_test`
- runtime readiness CLI smoke: `cd worker && uv run neuron-knowledge source-to-candidate-runtime-readiness --expected-commit 7218cb2`
- runtime readiness CLI smoke result: `status=PASS_WITH_GAPS`, `live_evidence_provided=false`, `production_mutation_performed=false`, `network_used=false`, live MCP read/review tools/object query route smokes/context tool hints/deployed identity/production denial claims `not_validated`
- runtime readiness focused evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_evaluates_sanitized_evidence_without_mutation tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_without_evidence_preserves_live_gaps tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_default_route_returns_agent_context_objects tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_style_route_uses_preference_objects tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_deploy_route_returns_runtime_gap_pack`
- runtime readiness focused result: `10 passed, 1 warning`
- focused evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_phase_golden_query_coverage_reports_pass_with_gaps_not_green`
- focused result: `1 passed, 1 warning`
- strict-axis evaluator evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_eval_strict_axes_require_edge_freshness_and_gap_fields`
- strict-axis evaluator result: `1 passed, 1 warning`
- empty-lane disclosure evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_eval_strict_axes_require_empty_authority_lane_disclosure`
- empty-lane disclosure result: `1 passed, 1 warning`
- runtime evidence gate evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_eval_strict_axes_require_runtime_evidence_for_runtime_claims`
- runtime evidence gate result: `1 passed, 1 warning`
- CLI evidence: `cd worker && uv run pytest -q tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_phase_coverage`
- CLI result: `1 passed, 1 warning`
- adjacent regression evidence: `cd worker && uv run pytest -q tests/test_neuron_cli.py tests/test_golden_query_eval.py tests/test_llm_brain_core_objects_subpackage.py tests/test_object_packs.py`
- adjacent regression result: `55 passed, 1 warning`
- broader adjacent regression evidence: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py tests/test_golden_query_eval.py tests/test_neuron_cli.py tests/test_object_packs.py tests/test_llm_brain_core_objects_subpackage.py`
- broader adjacent regression result: `89 passed, 1 warning`
- object query route focused evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_roundtrip tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_applies_object_type_filter_and_response_mode tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_default_route_returns_agent_context_objects tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_style_route_uses_preference_objects tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_deploy_route_returns_runtime_gap_pack tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_overlays_local_authority_state`
- object query route focused result: `10 passed, 1 warning`
- object/CLI/MCP adjacent evidence: `cd worker && uv run pytest -q tests/test_neuron_cli.py tests/test_golden_query_eval.py tests/test_llm_brain_core_objects_subpackage.py tests/test_object_packs.py tests/test_neuron_mcp_stdio.py`
- object/CLI/MCP adjacent result: `128 passed, 1 warning`
- CLI smoke: `cd worker && uv run neuron-knowledge golden-query-eval --phase-coverage`
- CLI smoke result: `status=PASS_WITH_GAPS`, `release_quality_gate=not_green`
- worker regression evidence: `cd worker && uv run pytest -q`
- worker regression result: `1590 passed, 9 skipped, 1 warning`
- root regression evidence: `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
- root regression result: `BUILD SUCCESSFUL`

Remaining gaps:

- P5 is not production-green; this slice adds source-to-authority path gate evidence, not production-quality golden answer generation
- P6-P9 production/live slices and P10 product surface are still intentionally reported as gaps where runtime evidence is missing
- release quality remains `not_green` until data, processing, authority, runtime, preference, and context lanes can support production-quality answers

### P6. Session, Device, Project, And Work-Unit 360

State: `PASS_WITH_GAPS` / local_validated.

Purpose:

Support questions across one PC, many PCs, one project, one branch, one session, PRs, specs, commits, and handoff contexts.

Required capabilities:

- hashed device/session/project identity
- bidirectional edges between `Device`, `Session`, `WorkUnit`, `Repository`, `Branch`, `Spec`, `PullRequest`, and `Commit`
- per-device answers and all-device project rollups
- temporal recall such as "어제 이 repo에서 뭐 했어?"
- handoff pack generated from current work objects and gaps

Gate evidence:

- one-device and all-device fixture queries produce different but compatible answers
- raw host/path is not exposed
- project rollup can cite sessions, specs, PRs, and commits without raw transcript body

Current local/test evidence:

- `run_session_project_rollup_preview` maps redacted session metadata into `Device`, `Session`, `Repository`, `Branch`, and `WorkUnit` objects
- local/test rollup emits `repository_has_branch`, `session_on_device`, `session_in_repository`, `session_on_branch`, and `part_of_work_unit` edges
- same-device scope and all-device scope produce different visible session counts while preserving all-device rollup counts
- optional Spec, PullRequest, and Commit metadata can be linked into the same WorkUnit rollup with bidirectional object edges
- local/test rollup emits `session_project_handoff_pack.v1` from current work objects, edge counts, explicit gaps, and a `session_project_resume_context.v1`
- resume context carries latest session ref, active branch, work unit refs, linked Spec/PullRequest/Commit refs, local/test gaps, and live gap disclosure without raw transcript/body return
- local path sentinels and source bodies are not returned
- P5 phase coverage now marks P6 as `PASS_WITH_GAPS` with `live_multi_device_rollup_unproven`, not `handoff_pack_not_implemented`
- focused evidence: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_session_project_rollup_preview_separates_same_device_and_all_devices`
- focused result: `1 passed, 1 warning`
- bidirectional linked metadata evidence: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_session_project_rollup_preview_links_specs_prs_and_commits_bidirectionally`
- bidirectional linked metadata result: `1 passed, 1 warning`
- handoff pack evidence: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_session_project_rollup_preview_builds_safe_handoff_pack`
- handoff pack result: `1 passed, 1 warning`
- phase coverage evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_phase_golden_query_coverage_reports_pass_with_gaps_not_green`
- phase coverage result: `1 passed, 1 warning`
- adjacent regression evidence: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py tests/test_golden_query_eval.py tests/test_llm_brain_core_objects_subpackage.py`
- adjacent regression result: `46 passed, 1 warning`
- worker regression evidence: `cd worker && uv run pytest -q`
- worker regression result: `1577 passed, 9 skipped, 1 warning`
- root regression evidence: `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
- root regression result: `BUILD SUCCESSFUL`

Remaining gaps:

- local/test P6 gate evidence is present for same-device/all-device fixture rollups, safe handoff/resume context generation, and bidirectional linked metadata edges
- PR/commit/test provenance is covered only by local/test metadata fixtures, not live repository history
- live multi-device/project rollup evidence is unproven

### P7. Preference, Style, And Artifact Memory

State: local_validated.

Result: PASS_WITH_GAPS.

Purpose:

Let AI tools start with the user's accepted preferences instead of rediscovering them each session.

Required capabilities:

- `PersonalCodeStyleProfile`
- `RepoStyleProfile`
- `HtmlReviewProfile`
- `VisualizationProfile`
- `ArtifactPreferencePack`
- inferred versus accepted preference separation
- evidence refs to examples without raw source/body storage
- diff/artifact review suggestions for style or preference drift

Gate evidence:

- inferred rule enters review/proposal state first
- accepted preference appears in agent context pack
- old code inertia is not automatically promoted into style authority
- HTML review artifact can be checked against accepted preference without requiring UI

Local validation evidence:

- artifact preference pack gate: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_preference_style_extraction_preview_builds_artifact_preference_pack_lanes`
- artifact preference pack result: `1 passed, 1 warning`
- HTML artifact review gate: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_preference_style_extraction_preview_checks_html_artifact_without_ui`
- HTML artifact review result: `1 passed, 1 warning`
- phase coverage gate: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_phase_golden_query_coverage_reports_pass_with_gaps_not_green`
- phase coverage result: `1 passed, 1 warning`

Implemented local/test scope:

- `ArtifactPreferencePack`, `PersonalCodeStyleProfile`, `RepoStyleProfile`, `HtmlReviewProfile`, and `VisualizationProfile` preview objects
- accepted versus proposal lane separation for preferences and style claims
- accepted preference context pack lane with public-safe evidence refs
- inferred preference and legacy style inertia routed to review/proposal lane first
- HTML review artifact summary/metrics preference check that does not require UI rendering and does not return artifact body
- diff/artifact review suggestions for HTML, visualization, and repo style drift

Remaining gaps:

- accepted preference context pack is not live-proven in a deployed agent read path
- production preference/style authority promotion remains closed until an approved write gate exists
- HTML artifact check is local/test summary/metrics validation only, not a live product consumer workflow

### P8. Runtime Truth, Security, And Deployment Authority

State: local_validated.

Result: PASS_WITH_GAPS.

Purpose:

Answer operational truth questions with merge, CI, deploy authority, and live runtime evidence separated.

Required capabilities:

- `PullRequest`, `Commit`, `CIStatus`, `DeploymentTarget`, `RuntimeSurface`, `RuntimeTruth`, and `LiveEvidenceGap`
- public repo CI separated from private deploy authority
- live runtime check represented as evidence, not assumption
- deployed artifact identity joined to source commit where available
- object/property/action permission model for runtime and authority claims
- application or agent-surface restrictions for who may read, propose, approve, or execute actions
- audit trail for permission-sensitive object reads and authority-changing actions

Gate evidence:

- merged PR does not imply deployed
- missing live evidence returns `runtime_evidence_unverified`
- private deploy authority is referenced without leaking private values
- permission denial is public-safe and does not leak protected object values
- action permission test proves an agent cannot promote authority without approved scope

Local validation evidence:

- missing live evidence gate: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_runtime_truth_extraction_preview_keeps_merge_and_deploy_separate_without_live_evidence`
- missing live evidence result: `1 passed, 1 warning`
- live evidence object gate: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_runtime_truth_extraction_preview_creates_runtime_verified_object_only_with_live_evidence`
- live evidence object result: `1 passed, 1 warning`
- runtime authority policy gate: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_runtime_truth_extraction_preview_denies_authority_promotion_without_leaking_private_deploy_values`
- runtime authority policy result: `1 passed, 1 warning`
- phase coverage gate: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_phase_golden_query_coverage_reports_pass_with_gaps_not_green`
- phase coverage result: `1 passed, 1 warning`

Implemented local/test scope:

- `PullRequest`, `Commit`, `CIStatus`, `DeploymentTarget`, `RuntimeSurface`, `RuntimeTruth`, and `LiveEvidenceGap` preview objects
- merge, CI, deployment target, artifact identity, and live runtime evidence are represented as separate evidence surfaces
- missing live evidence returns `runtime_evidence_unverified` and does not create a runtime-verified truth object
- deployment target identity joins artifact digest to source commit when provided, but remains runtime-unverified without live evidence
- private deploy authority is represented by presence/digest fields only; protected connection values are not returned
- runtime authority promotion without approved scope returns denied/no-mutation and records a public-safe audit event

Remaining gaps:

- no live rollout artifact identity proof is attached to this local/test branch
- production permission-sensitive audit flow is not live-proven
- production authority promotion remains denied until an approved runtime write gate exists

### P9. Agent Context Productization

State: local_validated.

Result: PASS_WITH_GAPS.

Purpose:

Make LBrain useful at agent startup and during review, not only through explicit search.

Required capabilities:

- compact context packs for Codex, Claude, Gemini, Hermes
- current authority, reference objects, style/preference, active work, guardrails, and required verification in one pack
- consumer-specific shaping from the same authority substrate
- degraded mode that states missing lanes and stale evidence
- application-surface policy that says which object types and actions each consumer can see or request
- proposal-safe action hints that distinguish "can suggest" from "can execute"

Gate evidence:

- each consumer can request a compact pack
- pack cites authority lane and gaps
- stale/no-recent-source state is visible instead of hidden
- consumer pack omits object properties and actions outside its allowed surface
- pack tells the agent which missing evidence must be gathered before promotion

Local validation evidence:

- compact consumer pack gate: `cd worker && uv run pytest -q tests/test_context_pack_builder.py::test_builder_adds_consumer_specific_compact_agent_context_pack_with_safe_action_hints`
- compact consumer pack result: `1 passed, 1 warning`
- phase coverage gate: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_phase_golden_query_coverage_reports_pass_with_gaps_not_green`
- phase coverage result: `1 passed, 1 warning`

Implemented local/test scope:

- `agent_context_product_pack.v1` is attached to the context authority block for `codex`, `claude-code`, `gemini`, and `hermes`
- compact sections cover current authority, reference objects, style/preference, active work, guardrails, and required verification
- consumer surface policy is read-only, omits protected properties, and keeps mutation disabled
- degraded mode exposes graph/runtime evidence gaps instead of hiding them
- stale memory count is visible in freshness metadata
- proposal-safe action hints distinguish `suggest_allowed` from `execute_allowed` and list missing evidence before promotion
- object-native read/review `tool_hints` list `brain_objects_query`, `brain_source_to_candidate_graph`, `brain_candidate_review_edit`, and `brain_approval_board_decide` as suggest-only local_test/read-only preview surfaces with production mutation disabled
- object-native readiness `tool_hints` list `brain_source_to_candidate_runtime_readiness` as a suggest-only sanitized-evidence evaluator with production mutation disabled
- runtime readiness report includes a live agent context `tool_hints` claim so post-deploy startup/read-path evidence can be checked without upgrading local tests into runtime proof

Remaining gaps:

- production agent startup/read path has not live-proven these compact packs
- consumer action surface policy is local/test only, not enforced by deployed runtime
- production authority-changing actions remain denied until approved scope, audit, and rollback gates exist

### P10. UI And Object Browser Surface

State: planned; deferred and open.

Decision: DEFER.

Result: PASS_WITH_GAPS for start-readiness review.

Purpose:

Provide human inspection and review workflows after object contracts, authority lifecycle, and corpus store are stable.

Position:

Full UI is not required to activate CLI/MCP product quality. It remains open for later productization.

However, a minimal review/edit surface is required before P3/P4 can be considered product-ready, because users must be able to correct AI-extracted objects, edges, and evidence before authority promotion.

Possible surfaces:

- review queue UI
- corpus management UI
- object graph browser
- HTML dashboard for inspection
- work-unit and project 360 view
- minimal candidate object/edge/evidence editor for P3/P4

Entry criteria:

- P1 production MCP activation is complete
- P2 corpus status is usable
- P4 review lifecycle has stable object contracts
- P5 golden query quality shows object answers are useful enough to inspect visually

Defer decision evidence:

- P1 remains `in_progress` with current Codex session object-native MCP namespace gap.
- P5 remains `in_progress` and release quality gate is not green.
- P8 and P9 are local/test validated only; broader production runtime authority, production permission audit, production startup/read path, and runtime enforcement remain gaps.
- UI is explicitly not a prerequisite for MCP/read-path activation, authority writes, or production rollout.

Decision outcome:

- Do not start UI/object browser implementation in this roadmap run.
- Keep P10 open as a later product surface after the read path, authority lifecycle, and quality gates are production-proven.
- Do not defer the minimal candidate edit/review surface needed by P3/P4.
- If P10 is later started, the first slice must be read-only/local, must use existing object packs, and must not introduce production mutation or protected-value disclosure.

## Recommended Execution Order

P5 runs continuously across the sequence below. It is listed as a phase because the full golden-query suite becomes a release gate, but phase-specific evaluator slices must run during P1-P9.

1. P1 Production MCP Activation
2. P2 Living Reference Corpus Store
3. P3 Processing And Object Extraction Pipeline
4. P4 Review Queue And Authority Promotion
5. P5 Continuous Golden Query Quality Gates
6. P6 Session, Device, Project, And Work-Unit 360
7. P7 Preference, Style, And Artifact Memory
8. P8 Runtime Truth, Security, And Deployment Authority
9. P9 Agent Context Productization
10. P10 UI And Object Browser Surface

P5 must not be declared green until data, processing, authority, runtime, preference, and context lanes can support production-quality answers.

## Decision Rules

- If a feature can only work by treating candidate/search/archive data as current authority, do not ship it.
- If a corpus has no source URL or freshness check, return the gap instead of implying currentness.
- If a production path would write authority, require a separate approved production gate.
- If a query asks about deployment, require live evidence or return runtime gap.
- If an answer depends on raw private content, return a redacted evidence ref or deny the answer.
- If a UI would force premature object semantics, defer UI and fix the object contract first.
- If an action would change authority, require scoped permission, human approval, audit, and rollback or supersession path.
- If an object property or action is outside a consumer's allowed surface, omit or deny it instead of returning a partial secret.
- If a phase cannot improve its golden query slice, keep the phase in `planned` or `in_progress` even if code and tools exist.

## Progress Accounting

Use phase states, not percentages.

Evidence result labels such as `PASS`, `PASS_WITH_GAPS`, and `FAIL` can appear in phase summaries and notes. They do not replace the progress states below.

Allowed states:

- `not_started`
- `planned`
- `in_progress`
- `blocked`
- `local_validated`
- `production_validated`
- `complete`

Current accounting:

| Phase | State | Notes |
| --- | --- | --- |
| P0 Local Object Substrate Foundation | `complete` | complete for local/test scope |
| P1 Production MCP Activation | `in_progress` | `PASS_WITH_GAPS`; latest-main deployed/configured endpoint validated, current Codex session tool registry gap remains |
| P2 Living Reference Corpus Store | `local_validated` | `PASS_WITH_GAPS`; local/test store and status gates pass, real private manifest ingest and production approval remain gaps |
| P3 Processing And Object Extraction Pipeline | `local_validated` | `PASS_WITH_GAPS`; local/test extraction previews, store-to-candidate CLI/MCP wiring, and candidate review/edit pack pass, but deployed/runtime source-to-candidate wiring and live projection join remain gaps |
| P4 Review Queue And Authority Promotion | `local_validated` | `PASS_WITH_GAPS`; local/test authority state, audit gates, review/approval CLI/MCP chain, approval-board preview, local_test promotion preview, and reviewer edit no-mutation proof pass; production authority mutation remains denied |
| P5 Continuous Golden Query Quality Gates | `in_progress` | `PASS_WITH_GAPS`; phase coverage, source-to-authority path gate, and P2-P9 activation progress gate exist, release quality gate remains `not_green` |
| P6 Session, Device, Project, And Work-Unit 360 | `local_validated` | `PASS_WITH_GAPS`; local/test rollup and handoff gates pass, live multi-device runtime evidence remains a gap |
| P7 Preference, Style, And Artifact Memory | `local_validated` | `PASS_WITH_GAPS`; local/test artifact preference pack lanes and no-UI HTML artifact check pass, live agent context pack and production authority promotion remain gaps |
| P8 Runtime Truth, Security, And Deployment Authority | `local_validated` | `PASS_WITH_GAPS`; local/test runtime authority policy, artifact identity join, private authority redaction, and denial/no-mutation checks pass; broader production runtime authority and permission audit remain gaps |
| P9 Agent Context Productization | `local_validated` | `PASS_WITH_GAPS`; local/test consumer compact packs, degraded/stale disclosure, surface policy, and proposal-safe action hints pass; production startup/read path and runtime enforcement remain gaps |
| P10 UI And Object Browser Surface | `planned` | `PASS_WITH_GAPS` for start-readiness review; full object browser deferred, but minimal P3/P4 candidate edit/review surface is now a prerequisite |

Delivery integration status:

- PR #84 through PR #93 are merged into `main`.
- Final head and merge SHAs below are GitHub delivery evidence only. They are not deploy, live runtime, or production readiness evidence.
- P1 through P10 phase branches were cleaned up or are eligible for cleanup after merge verification.
- This delivery record does not close the P1 configured-agent namespace gap, deployed source-to-candidate graph runtime wiring, P5 release-quality `not_green` status, P6-P9 production/live proof gaps, or production authority promotion gates.
- Historical PR body previews and issue drafts remain in `pr-delivery-package.md`; use the delivery record below as the current SHA source.

Merged PR delivery record:

| Phase | PR | Branch | Final/current head | Merge commit | Base |
| --- | --- | --- | --- | --- | --- |
| P1 Production MCP Activation | #84 | `codex/p1-production-mcp-activation-live` | `9cf7f9b` | `dea6f8d` | `main` |
| P2 Living Reference Corpus Store | #85 | `codex/p2-living-reference-corpus-store` | `c0695ba` | `7295092` | `main` |
| P3 Processing And Object Extraction Pipeline | #86 | `codex/p3-processing-object-extraction-pipeline` | `09b88a2` | `2740766` | `main` |
| P4 Review Queue And Authority Promotion | #87 | `codex/p4-review-authority-promotion` | `db8caec` | `45eb6cd` | `main` |
| P5 Continuous Golden Query Quality Gates | #88 | `codex/p5-continuous-golden-query-quality` | `912e3bf` | `9d0c3cc` | `main` |
| P6 Session, Device, Project, And Work-Unit 360 | #89 | `codex/p6-session-device-project-workunit-360` | `cb1a016` | `9f4969b` | `main` |
| P7 Preference, Style, And Artifact Memory | #90 | `codex/p7-preference-style-artifact-memory` | `9409b4a` | `af08043` | `main` |
| P8 Runtime Truth, Security, And Deployment Authority | #91 | `codex/p8-runtime-truth-security-deployment-authority` | `8191d48` | `4ace498` | `main` |
| P9 Agent Context Productization | #92 | `codex/p9-agent-context-productization` | `c6854e3` | `2d9c92a` | `main` |
| P10 UI And Object Browser Surface | #93 | `codex/p10-ui-object-browser-defer-decision` | `8c483d0` | `7bda476` | `main` |

PR creation gate:

- Required: linked issue number or explicit user approval for GitHub PR mutation.
- Required PR body constraint: include a real closing reference such as `Closes #N`.
- If no linked issue exists, prepare PR body previews but do not create PRs.
- Do not claim merge, CI, deploy, or live runtime evidence from branch push alone.

## Next Design Targets

The next agentic-execution loop should keep the full product activation scope, not stop at P4:

```text
P2 source/corpus storage
→ P3 candidate graph extraction and editing
→ P4 approval-board promotion
→ P5 quality gates
→ P6 project/session/work-unit rollup
→ P7 preference/artifact memory
→ P8 runtime authority
→ P9 agent context productization
```

Continue production read-path validation only when the configured LBrain MCP object-native tool namespace can be proven from the active agent path. Keep production authority writes denied until the approval board, audit trail, rollback/supersession path, and scoped promotion gate are approved.

Recommended goal:

```text
LBrain source-to-candidate-graph product activation through P9, with full P10 object browser deferred and minimal P3/P4 edit/review surface included.
```

Expected outputs:

- source/corpus storage requirements
- candidate extraction design
- minimal candidate object/edge/evidence edit surface contract
- approval-board promotion contract
- candidate object/edge/evidence edit fixtures
- no-authority-mutation proof for extraction and reviewer edits
- local/test promotion read-after-write proof
- P5 quality gate evidence for source-to-graph, review, approval, and authority read paths
- P6 project/session/work-unit rollup evidence
- P7 preference/artifact memory evidence
- P8 runtime authority evidence with merge, CI, deploy, and live runtime separated
- P9 agent context pack evidence
- production promotion gate plan with human approval, audit, rollback/supersession, and scoped object classes
- golden query slices for source-to-graph, review, approval, and authority read paths
- no production mutation
- no protected content, credentials, topology, or raw external ID output
- clear PASS / PASS_WITH_GAPS / FAIL result with live gaps preserved
