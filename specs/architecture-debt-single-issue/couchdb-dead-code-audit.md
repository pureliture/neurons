# CouchDB / Session-Memory Dead-Code Audit

This audit is the M4 read-only classification for GitHub issue #40. It does not delete files and it does not perform live CouchDB, graph, Qdrant, GC, deploy, Docker, systemd, or firewall mutation.

## Finding

The current CouchDB/session-memory surface is not a single dead-code island. It contains active runtime code, human-gated migration/archive tooling, compatibility CLI surface, and test-only seams. Immediate deletion is blocked until a deletion test proves that a specific file or command is not imported, not routed, and not required by current specs.

## Classification

| Surface | Classification | Evidence | Deletion risk |
| --- | --- | --- | --- |
| `worker/lib/agent_knowledge/couchdb_source/document_model.py` | active runtime/source model | Imported by delivery backend, graph projection, build CLI, tests, and package re-export. | High; owns document IDs, source doc types, redaction guards. |
| `worker/lib/agent_knowledge/couchdb_source/source_store.py` | active runtime/test seam | Imported by delivery backend, projection code, build CLI tests, and in-memory store tests. | High; store protocol and fake are broad test/runtime seams. |
| `worker/lib/agent_knowledge/couchdb_source/couchdb_http_store.py` | active runtime adapter | Used by build CLI, delivery backend factories, graph/status projection CLIs, and tests. | High; live adapter surface. |
| `worker/lib/agent_knowledge/couchdb_source/build_cli.py` | active CLI | Routed as `neuron-knowledge couchdb-session-memory-build`; session-memory deploy script tests reference this command. | High; current builder entrypoint. |
| `worker/lib/agent_knowledge/couchdb_source/session_memory_materializer.py` | active builder core | Used by build CLI, qdrant backfill, graph projection tests, and retirement verifier tests. | High; materialization seam. |
| `worker/lib/agent_knowledge/couchdb_source/tool_evidence_bundler.py` | active builder helper | Used by materialization and CouchDB tests. | Medium; helper can move only with materializer refactor. |
| `worker/lib/agent_knowledge/couchdb_source/project_authority.py` | active safety helper | Used by historical import and tested directly; affects retirement eligibility. | Medium; deletion risks unsafe project attribution. |
| `worker/lib/agent_knowledge/couchdb_source/retention.py` | active policy helper | Tested directly and re-exported from package. | Medium; no deletion without retention policy decision. |
| `worker/lib/agent_knowledge/couchdb_source/index_projector.py` | partial / retired-bridge adapter | Used by build CLI tests and RetiredIndexBridge projection path. | Medium; candidate for future adapter isolation, not blind deletion. |
| `worker/lib/agent_knowledge/couchdb_source/index_fallback.py` | archive/recovery tooling | Recovery path for unverified projects; low reference count but explicit safety purpose. | Medium; deletion needs recovery-path decision. |
| `worker/lib/agent_knowledge/couchdb_source/historical_import.py` | migration/archive tooling | Used by tests and shadow cutover. | Medium; deletion needs migration-finalized proof. |
| `worker/lib/agent_knowledge/couchdb_source/shadow_cutover.py` | human-gated migration tooling | Tested directly; models shadow/couchdb-only switch logic. | Medium; deletion needs explicit cutover history decision. |
| `worker/lib/agent_knowledge/couchdb_source/retirement_verifier.py` | human-gated safety verifier | Tested directly; verifies transcript-memory retirement readiness. | Medium; deletion needs retirement proof replacement. |
| `worker/lib/agent_knowledge/couchdb_source/migration_cli.py` | compatibility / archive CLI | Routed as `neuron-knowledge transcript-migration`; tested directly. | Medium; removal breaks CLI compatibility and tests. |
| `worker/lib/agent_knowledge/couchdb_source/migration_flow_cli.py` | compatibility / migration orchestration CLI | Routed as `neuron-knowledge couchdb-migration-flow`; tested directly. | Medium; removal breaks CLI compatibility and tests. |
| `worker/lib/agent_knowledge/session_memory/neuron_session_memory.py` | legacy session-memory builder compatibility | Exposed as `neuron-session-memory-build` script and `neuron-knowledge neuron-session-memory-build`. | Medium; candidate for comparison against CouchDB-native builder before deprecation. |

## Immediate Deletion Decision

No file is safe to delete in M4 without a narrower design decision or a failing deletion/compatibility test first.

Safe next code-changing slice:

1. Add a focused CLI surface classification test that names CouchDB commands as `active`, `compatibility`, or `human_gated_migration`.
2. Add a small command metadata map in `agent_knowledge.cli` so future cleanup can prove whether a command is active runtime or historical compatibility before deletion.
3. Use that map to keep `couchdb-session-memory-build` active and classify `transcript-migration` / `couchdb-migration-flow` as human-gated migration compatibility.

## M4 Implemented Slice

Timestamp: 2026-07-05 19:58:30 KST

- Added `COMMAND_METADATA` in `worker/lib/agent_knowledge/cli.py`.
- Added `worker/tests/test_neuron_cli.py` coverage that proves metadata keys are routed commands.
- Classified:
  - `couchdb-session-memory-build`: `active_runtime`, `deletion_candidate=false`, `live_mutation_requires_approval=true`
  - `transcript-migration`: `human_gated_migration`, `deletion_candidate=false`, `live_mutation_requires_approval=true`
  - `couchdb-migration-flow`: `human_gated_migration`, `deletion_candidate=false`, `live_mutation_requires_approval=true`
  - `neuron-session-memory-build`: `legacy_compatibility`, `deletion_candidate=false`
- Verified:
  - `cd worker && uv run pytest -q tests/test_neuron_cli.py -k couchdb_command_surface`
  - `cd worker && uv run pytest -q tests/test_neuron_cli.py`
  - `cd worker && uv run pytest -q tests/test_eval_readiness.py tests/test_eval_loop.py tests/test_eval_notify_discord.py tests/test_golden_grader.py tests/test_neuron_cli.py`
  - `uv run python scripts/test_runtime_verifiers.py`
  - `cd worker && uv run pytest -q tests/test_couchdb_build_cli.py tests/test_couchdb_migration_cli.py tests/test_couchdb_migration_flow_cli.py tests/test_couchdb_shadow_cutover.py tests/test_couchdb_index_fallback.py tests/test_session_memory_backfill_planning.py`

## Next Automatic Slice Candidate

Start with `worker/lib/agent_knowledge/session_memory/backfill.py`, `worker/lib/agent_knowledge/couchdb_source/index_fallback.py`, or `worker/lib/agent_knowledge/couchdb_source/shadow_cutover.py` only after proving, with tests and source search, that the target is not routed by `COMMAND_HANDLERS`, not exported as an active package API, and not required by current specs. The default next step is a deletion-test probe, not file removal.

## M5 Probe 1: `session_memory/backfill.py`

Timestamp: 2026-07-05 20:02:45 KST

Conclusion: do not delete as dead code in the current slice.

Evidence:

- `agent_knowledge.cli` does not route `worker/lib/agent_knowledge/session_memory/backfill.py` as a direct command.
- `worker/lib/agent_knowledge/session_memory/__init__.py` exports `APPROVAL_REQUIRED_FIELDS`, `build_execute_plan`, `inventory_fixture_sources`, and `dry_run_backfill` from `.backfill`.
- `worker/tests/test_session_memory_backfill_planning.py` directly imports and verifies public schemas, redaction, fixture-root safety, and bounded planning behavior.
- `worker/tests/test_worker.py` includes `agent_knowledge.session_memory.backfill` in the vendored import boundary.
- Probe tests passed: `cd worker && uv run pytest -q tests/test_session_memory_backfill_planning.py tests/test_worker.py`.

Reclassification: public compatibility / safety-planning surface. It can become a deprecation/removal target only after the public export contract and vendored boundary expectations are intentionally changed.

Next probe: `worker/lib/agent_knowledge/couchdb_source/index_fallback.py`.

## M5 Probe 2: `couchdb_source/index_fallback.py`

Timestamp: 2026-07-05 20:02:45 KST

Conclusion: do not delete as dead code in the current slice.

Evidence:

- `agent_knowledge.cli` does not route `worker/lib/agent_knowledge/couchdb_source/index_fallback.py` as a direct command.
- `worker/lib/agent_knowledge/couchdb_source/__init__.py` does not re-export this module.
- `worker/tests/test_couchdb_index_fallback.py` directly imports `reconstruct_sessions` and `RETIRED_INDEX_BRIDGE_FALLBACK_STATUS`; deleting the module fails that recovery-path test.
- `specs/recall-cutover/requirements.md` records CouchDB transcript-source state as `clean 3326 + index_fallback 241`, so the fallback status is part of historical cutover evidence.
- Probe tests passed: `cd worker && uv run pytest -q tests/test_couchdb_index_fallback.py tests/test_couchdb_build_cli.py tests/test_worker.py`.

Reclassification: archive/recovery compatibility surface. It can become a removal target only after the recovery-path contract is explicitly retired or replaced.

Next probe: `worker/lib/agent_knowledge/couchdb_source/shadow_cutover.py`.

## M5 Probe 3: `couchdb_source/shadow_cutover.py`

Timestamp: 2026-07-05 20:03:49 KST

Conclusion: do not delete as dead code in the current slice.

Evidence:

- `agent_knowledge.cli` does not route `worker/lib/agent_knowledge/couchdb_source/shadow_cutover.py` as a direct command.
- `worker/lib/agent_knowledge/couchdb_source/__init__.py` re-exports `LIVE_CUTOVER_PROVIDERS`, `CutoverNotReady`, `CutoverPhase`, `RecordingComparisonSink`, `ShadowCoordinator`, and `ShadowObservation` from `.shadow_cutover`.
- `worker/tests/test_couchdb_shadow_cutover.py` directly imports and verifies the SHADOW -> COUCHDB_ONLY state machine.
- `specs/couchdb-transcript-migration/milestones.md` records `shadow_cutover.py` as the completed M4 code/test artifact, with live event stream and real RetiredIndexBridge comparison writes still human-gated.
- Probe tests passed: `cd worker && uv run pytest -q tests/test_couchdb_shadow_cutover.py tests/test_couchdb_migration_flow_cli.py tests/test_worker.py`.

Reclassification: human-gated migration compatibility surface. It is not active runtime command code, but it is still a public package export and completed migration gate model. Removal requires an explicit cutover-history retirement decision.

## M5 Deletion Probe Decision

All three first-pass deletion candidates are preserved:

- `session_memory/backfill.py`: public compatibility / safety-planning surface
- `couchdb_source/index_fallback.py`: archive/recovery compatibility surface
- `couchdb_source/shadow_cutover.py`: human-gated migration compatibility surface

The safe outcome for the CouchDB/session-memory dead-code item is reclassification plus metadata, not deletion. Future cleanup should target deprecation metadata or compatibility removal only after a separate public API/export decision.

## Stop Conditions

- Do not remove CLI commands before compatibility impact is explicitly accepted.
- Do not remove migration/archive helpers before tests prove no current command, deploy script, spec, or read path imports them.
- Do not execute live cleanup, data deletion, GC, or runtime mutation from this campaign without separate approval.
