# Milestones — couchdb-transcript-migration

Working state for `/agentic-execution` of `design.md`. Live operations against
real private transcripts / live RAGFlow / live archive are human-gated
(AGENTS.md raw-transcript rule; CLAUDE.md live RAGFlow write/delete/GC rule) and
are NOT performed inside this loop. Code + tests are built with fixtures/fakes.

## M1 CouchDB source model + ownership contract
- status: done
- evidence: `worker/lib/agent_knowledge/couchdb_source/{document_model,source_store}.py`
  + `worker/tests/test_couchdb_source_{model,store}.py`. 24 new tests pass; full
  suite 549 passed / 2 skipped (baseline was 525/2). Six document families,
  deterministic ids/hashes (parity-locked to `transcript_model._sha256`),
  fail-closed redaction boundary (`public_ingress_leak_violations`), ownership
  rules (`transcript-memory` retired; only `session-memory` is a valid RAGFlow
  target), idempotent in-memory store seam.

## M2 Historical import + project authority resolver
- status: done (all 5 lanes incl. agy)
- evidence: `couchdb_source/{project_authority,historical_import}.py` +
  `tests/test_couchdb_{project_authority,historical_import}.py`. 16 new tests
  pass; full suite 565/2. Hierarchy resolver (capture metadata > path/cwd/marker
  > server inference) with ambiguity + RAGFlow-mismatch reporting; import
  orchestrator parses via existing `parse_transcript_source`, applies the
  stricter public-ingress redaction at the store boundary, writes
  transcript_session + conversation_chunk + coverage_manifest, fail-closed on
  unreadable source / leak / unsupported lane.
- agy resolved (user, 2026-06-17): agy shares the Antigravity transcript format.
  `_parse_antigravity_native_jsonl` + `extract_antigravity_tool_evidence`
  parameterized with a `provider` label (default unchanged); `agy` added to the
  parse + tool-evidence dispatch and to `PROVIDER_LANES` (live_allowed=True);
  stored under the distinct `agy` provider label. agy import/live tests updated.
- divergence (invariant 2, replanned in-loop, NOT an SoT change): reused
  `conversation_chunk` doc id keyed on content-addressed `chunk_id` (M1
  refinement) since chunks share part_index=1.

## M3 Tool evidence bundling + session-memory materialization
- status: done
- evidence: `couchdb_source/{tool_evidence_bundler,session_memory_materializer}.py`
  + `tests/test_couchdb_tool_evidence_and_materialize.py`. 8 new tests; full
  suite 573/2. Bounded bundles via existing `chunk_tool_evidence_records` ->
  tool_evidence_bundle docs (index range + coverage hash); coverage manifest
  updated with tool evidence counts; session-memory materializer embeds full
  tool evidence summary for RAGFlow-only recall; projection goes to
  session-memory only (transcript-memory rejected), fail-closed on
  materialization loss / projector error (source kept, projection_state=failed).

## M4 Shadow live cutover logic (code+tests; live run gated)
- status: done (logic); live event stream + real RAGFlow comparison human-gated
- evidence: `couchdb_source/shadow_cutover.py` +
  `tests/test_couchdb_shadow_cutover.py`. 6 new tests; full suite 579/2.
  SHADOW->COUCHDB_ONLY state machine; shadow writes CouchDB + records
  comparison-only transcript-memory write via injected sink; COUCHDB_ONLY stops
  new comparison writes; gemini live = scope_violation; agy live =
  parser_unavailable; gated stability verdict (per-provider coverage + mixed
  projects). Default required set incl. agy blocks full cutover until agy parser
  exists; `required_providers` lets an operator cut over ready lanes.
- note: actual live provider event stream and real RAGFlow transcript-memory
  comparison write are injected seams; running them live is human-gated.

## M5 RAGFlow transcript-memory retirement verifier (code+tests; live retire gated)
- status: done (verifier); live retire + runtime callsite removal human-gated
- evidence: `couchdb_source/retirement_verifier.py` +
  `tests/test_couchdb_retirement_verifier.py`. 7 new tests; full suite 593/2.
  Three independent gates (coverage / rebuild / recall-smoke); a single gate is
  never sufficient; ambiguous sessions excluded; aggregate ready requires every
  eligible session to pass all three. Report carries an explicit
  `live_action_required` note.
- human gate: removing the ~87 RAGFlow transcript-memory write/read callsites is
  an explicit API-breaking change (AGENTS.md "keep public CLI/API compat unless
  user approves a break"; existing tests like
  test_ragflow_transcript_memory_read_sot.py depend on it), and the live RAGFlow
  transcript-memory disable/delete is a hard-to-reverse live GC op. Both are NOT
  done autonomously; they run after the verifier passes + user approval.

## M6 Retention + cold archive (code+tests; live archive gated)
- status: done (logic); live compaction/archive write human-gated
- evidence: `couchdb_source/retention.py` + `tests/test_couchdb_retention.py`.
  7 new tests; full suite 593/2. Tiered plan (hot_full -> hot_manifest_only ->
  cold_archive_ref) by age; body removal gated on projection PROJECTED + coverage
  intact + cold_archive_ref backup (GC-style gate); `apply_retention` defaults to
  dry_run; compaction deletes heavy chunk/bundle bodies from hot, keeps coverage
  + retention manifests for audit/rollback; added `store.delete`.
- human gate: running apply_retention against the live hot store and writing the
  cold archive are hard-to-reverse live ops; not done autonomously.

## Exit gates (agentic-execution)
- evidence-before-done: every milestone has tests; full worker suite 593 passed /
  2 skipped; `gradle test` BUILD SUCCESSFUL; `neuron-knowledge --show-boundary`
  unchanged. All Python code lives under `worker/lib/agent_knowledge/couchdb_source/`.
- no SoT change: `design.md` not modified; in-loop refinements (chunk_id key,
  store-boundary public-ingress redaction, agy=antigravity format) are
  implementation, not design changes.
- worktree isolation: branch `codex/couchdb-transcript-migration-spec`; `main` clean.

## Human-gated live operations (NOT performed autonomously)
These need real private-transcript / live-RAGFlow access and explicit per-op
approval (AGENTS.md raw-transcript + live RAGFlow/GC rules). Required sequence:
1. Live historical import of real provider transcripts -> populate CouchDB source.
2. Retirement verifier passes on real coverage (coverage + rebuild + recall smoke).
3. Runtime cutover: remove ~87 RAGFlow transcript-memory write/read callsites
   (API-breaking) -> CouchDB-only new writes.
4. Live RAGFlow transcript-memory disable/delete.
5. Live retention compaction + cold archive write.
Steps 3-5 must follow step 2; doing them before verified coverage risks loss.
