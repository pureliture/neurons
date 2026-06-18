# LLM-Brain Core v1 Implementation Matrix

Source of Truth:
- `specs/llm-brain-core-v1/requirements.md`
- `specs/llm-brain-core-v1/design.md`

This file is the working autopilot matrix. It is not a replacement for the
SoT. If implementation reality requires changing requirements or design, route
back to grill-to-spec instead of editing the SoT silently.

## Requirement Status

| Requirement | Status | Evidence | Remaining Work |
| --- | --- | --- | --- |
| Implement v1 in `neurons`, not a new repo | implemented | `worker/lib/agent_knowledge/llm_brain_core/` exists and `neuron-knowledge brain-context-resolve` routes to it | Later extraction remains deferred after graph-core stabilization |
| Keep `dendrite`/`neurons` collection pipeline | implemented | Existing ingress/session-memory tests remain in worker suite | Dendrite SourceRef producer contract still needs cross-repo validation |
| Keep CouchDB as AI session raw store | implemented | `materialize_artifact_from_couchdb_source` reads CouchDB source docs without copying raw bodies | Live CouchDB adapter smoke remains separate from local tests |
| Keep NATS/ledger replay source | partial | `BrainEventEnvelope` and `BrainEventReplayStore` cover idempotency/tombstone/conflict rules | Central rebuild-to-episode shadow fixture still needed |
| `neurons` owns session-memory artifact and graph projection | partial | `LedgerSessionMemoryArtifactStore` stores artifacts; graph seam exists | Real Graphiti/Neo4j projection worker missing |
| Extract Task/Decision/Incident/PersonaFact candidates | partial | Existing MemoryCard miner/read-model reused by `BrainReadService` | Extraction pipeline into typed ontology episodes needs broader fixtures |
| Build latest-work ContextPack | implemented | `test_llm_brain_core_ragflow_disabled.py`, `test_llm_brain_core_runtime_integration.py` | Local graph-enhanced ranking still pending M6c |
| Incident/troubleshooting search | implemented with fake graph | `BrainReadService.brain_incident_search`; runtime incident test passes | Real graph backend search and replay smoke pending M6c |
| Time-aware drift explanation | implemented from cards | `BrainReadService.brain_drift_explain`; runtime drift test passes | Graph-backed temporal relation smoke pending M6c |
| PersonaFact check states | implemented | `BrainReadService.brain_persona_check`; tests cover aligned/conflict/drift/insufficient evidence | More evidence/confidence lifecycle cases can be added after graph integration |
| SourceRef/SpanRef redaction | implemented | `test_source_ref_policy_resolution.py`; `test_contextpack_no_raw_source_refs.py` | Dendrite same-device resolver action remains cross-repo contract work |
| Per-PC local brain and optional central brain | partial | Local ledger-backed service works offline; `BrainEvent` envelope exists | Compose profiles/export-import/central shadow runtime missing |
| Event/episode central sync, no graph DB file sync | partial | Replay model prevents duplicate/out-of-order/tombstone drift | Central rebuild fixture and runbook pending |
| RAGFlow bridge only, not core dependency | implemented for core path | Core tests pass with disabled bridge; MCP core tools do not instantiate RAGFlow | M9 bridge labeling compatibility test pending |
| Agent-facing read API | implemented for stdio surface | `mcp-stdio` exposes `brain_context_resolve`, `brain_memory_search`, `brain_incident_search`, `brain_drift_explain`, `brain_persona_get`, `brain_persona_check`, `brain_evidence_get` | HTTP adapter remains optional/deferred |
| Autopilot safety guard | implemented for pre-M9 path | `test_autopilot_no_ragflow_client_before_m9.py` | Review gate and full suite evidence still needed for final close |

## Milestone Status

| Milestone | Status | Evidence | Next Action |
| --- | --- | --- | --- |
| M0 Design freeze and safety baseline | done | SoT files present; RAGFlow demotion documented | Keep SoT unchanged unless grill-to-spec is re-entered |
| M1 Core contracts and safety guards | done | Core models/service/null graph tests pass | Maintain backward compatibility |
| M2 RAGFlow-free artifact and replay store | done | Artifact/replay tests pass without RAGFlow | Add central rebuild fixture in M7 |
| M3 SourceRef resolver contract | done | SourceRef golden state tests pass | Add dendrite producer/resolve contract later |
| M4 ContextPack builder | done | RAGFlow-disabled ContextPack tests pass | Add graph-enhanced ranking after M6c |
| M5 Incident, drift, persona | done with fake graph/cards | Incident/drift/persona tests pass | Add real graph backend smoke |
| M6a Graph adapter interface and fake backend | done | `FakeGraphMemoryAdapter` contract tests pass | Keep fake as deterministic contract backend |
| M6b Graphiti/Neo4j dependency approval gate | approved by goal envelope | User gave hardgate preapproval; destructive live ops still stop | Add dependency/compose proposal as code/docs before local integration |
| M6c Graphiti/Neo4j local integration | pending | No `GraphitiNeo4jGraphMemoryAdapter` exists | Implement adapter and local smoke, while graph-disabled path stays green |
| M7 Central sync shadow | partial | `BrainEventReplayStore` exists | Add deterministic central rebuild from event fixture |
| M8 Thin MCP/stdio surface | done | `uv run pytest -q tests/test_neuron_mcp_stdio.py ...` passed | Keep tools read-oriented and backend-neutral |
| M9 RAGFlow bridge compatibility | partial | Legacy RAGFlow search remains optional; core path disabled bridge works | Add explicit bridge status/precedence tests |

## Current Evidence

Latest targeted check:

```bash
cd worker
uv run pytest -q tests/test_neuron_mcp_stdio.py \
  tests/test_llm_brain_core_runtime_integration.py \
  tests/test_source_ref_policy_resolution.py
```

Result:

```text
16 passed
```

## Hard Stop Gates

- Raw private transcript or raw PC file body is required.
- Public output would expose raw absolute paths, secrets, dataset ids, document
  ids, or backend-private ids.
- RAGFlow write/delete/disable is required.
- Docker volume deletion, credential edit, firewall/systemd/package mutation,
  production K3s deployment, or central server mutation is required.
- A required change contradicts `requirements.md` or `design.md`.
