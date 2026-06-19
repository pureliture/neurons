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
| Implement v1 in `neurons`, not a new repo | implemented | `worker/lib/agent_knowledge/llm_brain_core/` exists and `neuron-knowledge brain-context-resolve` routes to it | Graph core stays in `neurons` for v1; extracting it into a new repo is not a v1 goal |
| Keep `dendrite`/`neurons` collection pipeline | implemented | Existing ingress/session-memory tests remain in worker suite; dendrite `codex/source-ref-catalog` adds SourceRef scan/resolve contract through `e48f159`; dendrite full suite returned `84 passed` | Dendrite merge is release integration, not a v1 evidence gap |
| Keep CouchDB as AI session raw store | implemented and Ubuntu live-smoked | `materialize_artifact_from_couchdb_source` reads CouchDB source docs without copying raw bodies; Ubuntu `CouchDBHttpSourceStore` fields-only smoke returned `status=available`, `db=transcript_source`, `raw_body_returned=false` | None for v1 CouchDB raw-store read contract |
| Keep NATS/ledger replay source | implemented for v1 shadow | `BrainEventEnvelope`, `BrainEventReplayStore`, and `CentralBrainShadowRebuilder` cover idempotency/tombstone/conflict/rebuild rules; Ubuntu worker-container shadow smoke returned `status=succeeded`, `projected=1`, duplicate event ignored | Production transport is outside the v1 interface; v1 owns envelope + replay shadow |
| `neurons` owns session-memory artifact and graph projection | implemented and verification-time Ubuntu live-smoked | `LedgerSessionMemoryArtifactStore` stores artifacts; `GraphProjectionWorker` projects artifacts, MemoryCards, and SourceRefs to `GraphMemoryAdapter`; verification-time Ubuntu Graphiti/Neo4j smoke returned `status=available`, `details=["graphiti_neo4j"]`, `episode_count=2`, `matched=true`; current Ubuntu promotion refresh verified host imports/MCP/portable paths and found graph dependencies absent from host Python | None for v1 graph projection contract; current-pass Graphiti smoke requires dependency bootstrap or containerized runner |
| Extract Task/Decision/Incident/PersonaFact candidates | implemented for accepted MemoryCard ontology projection | Existing MemoryCard miner/read-model reused by `BrainReadService`; `build_ontology_episode_batch` maps Session/Task/Decision/SourceRef fixtures; default Graphiti storage persists public-safe ontology episodes deterministically | LLM-driven Graphiti entity extraction is opt-in and outside v1 acceptance |
| Build latest-work ContextPack | implemented and Ubuntu CLI-smoked | `test_llm_brain_core_ragflow_disabled.py`, `test_llm_brain_core_runtime_integration.py`, `test_ontology_episode_batch.py`; Ubuntu `brain-context-resolve` returned `status=ok`, `schema_version=llm_brain_context_resolve.v1` | None for v1 ContextPack contract |
| Incident/troubleshooting search | implemented with graph contract | `BrainReadService.brain_incident_search`; runtime incident tests pass; `GraphProjectionWorker` projects ontology episodes through the graph adapter interface; Ubuntu Graphiti/Neo4j episode search returned matched Task entities | None for v1 incident search contract |
| Time-aware drift explanation | implemented from cards and graph contract | `BrainReadService.brain_drift_explain`; runtime drift tests pass; graph-backed ontology projection tests pass; Ubuntu Neo4j dump/load preserved `Episodic` nodes (`episodes=3`) | None for v1 drift contract |
| PersonaFact check states | implemented | `BrainReadService.brain_persona_check`; tests cover aligned/conflict/drift/insufficient evidence | Confidence lifecycle expansion is post-v1 product work, not a v1 gap |
| SourceRef/SpanRef redaction | implemented across neurons+dendrite branches | `test_source_ref_policy_resolution.py`; `test_contextpack_no_raw_source_refs.py`; dendrite `test_source_catalog.py`; CodeRabbit follow-up `findings: 0`; dendrite full suite returned `84 passed` | Dendrite merge is release integration, not a v1 evidence gap |
| Per-PC local brain and optional central brain | implemented for v1 local + central shadow | Local ledger-backed service works offline; `BrainEvent` envelope, central shadow rebuild, and portable export/import exist; Ubuntu central shadow smoke returned `status=succeeded` | Production transport is outside the v1 local interface |
| Event/episode central sync, no graph DB file sync | implemented for v1 shadow | `test_central_sync_shadow_rebuild.py` rebuilds derived graph state from BrainEvents; Ubuntu worker-container shadow smoke rebuilt from events without graph file sync | Production transport/runbook is not a v1 interface |
| RAGFlow bridge only, not core dependency | implemented and Ubuntu live-smoked for read-only bridge | Core tests pass with disabled bridge; M9 `document_bridge.py` labels RAGFlow as external read-only evidence and does not override canonical memory; Ubuntu bridge smoke returned `dataset_count=2`, `evidence_count=3`, `status=available` | None for v1 read-only bridge contract; RAGFlow write/delete/disable remains forbidden without separate approval |
| Agent-facing read API | implemented and Ubuntu stdio-smoked | `mcp-stdio` exposes `brain_context_resolve`, `brain_memory_search`, `brain_incident_search`, `brain_drift_explain`, `brain_persona_get`, `brain_persona_check`, `brain_evidence_get`; Ubuntu stdio smoke returned `missing=[]`, `tool_count=10` | HTTP adapter is not a v1 interface |
| Portable Git/Compose/export-import | implemented and Ubuntu container-smoked | `brain-export`/`brain-import` export allowlisted LLM-Brain JSONL tables and specs, excluding raw transcript tables and graph DB files; Ubuntu archive roundtrip returned `roundtrip_ok=true`; Neo4j dump/load returned `episodes=3` | None for v1 portability contract |
| Docs/runbooks | implemented | `docs/runbooks/LLM_BRAIN_CORE_V1_LOCAL_OPS.md` documents tests, Ubuntu container graph smoke, MCP smoke, export/import, SourceRef scan/resolve, and review gate | Keep current with future production deployment changes |
| Autopilot safety guard | implemented | `test_autopilot_no_ragflow_client_before_m9.py`; full worker suite and CodeRabbit follow-up review are clean | Maintain gate in future M9+ changes |

## Milestone Status

| Milestone | Status | Evidence | Next Action |
| --- | --- | --- | --- |
| M0 Design freeze and safety baseline | done | SoT files present; RAGFlow demotion documented | Keep SoT unchanged unless grill-to-spec is re-entered |
| M1 Core contracts and safety guards | done | Core models/service/null graph tests pass | Maintain backward compatibility |
| M2 RAGFlow-free artifact and replay store | done | Artifact/replay tests pass without RAGFlow | Maintain replay compatibility |
| M3 SourceRef resolver contract | done | SourceRef golden state tests pass; dendrite SourceRef catalog/resolve contract passes | Maintain thin-client/server boundary |
| M4 ContextPack builder | done | RAGFlow-disabled ContextPack tests pass; graph-only task fallback is covered | Maintain graph-disabled degradation |
| M5 Incident, drift, persona | done with graph contract and Ubuntu graph smoke | Incident/drift/persona tests pass; ontology projection covers graph adapter input; Ubuntu Graphiti/Neo4j smoke returned matching ontology episodes | Maintain regression coverage |
| M6a Graph adapter interface and fake backend | done | `FakeGraphMemoryAdapter` contract tests pass | Keep fake as deterministic contract backend |
| M6b Graphiti/Neo4j dependency approval gate | done | User gave hardgate preapproval; dependency and compose profile are code/docs-backed; destructive live ops still stop | Maintain approval gate for live graph DB mutation |
| M6c Graphiti/Neo4j Ubuntu integration | done with verification-time live smoke | `GraphitiNeo4jGraphMemoryAdapter`, `GraphProjectionWorker`, and `llm-brain-neo4j` compose profile exist; verification-time Ubuntu Neo4j container was healthy; adapter smoke returned `status=available`; dump/load returned `episodes=3`; current Ubuntu promotion refresh verified compose profile and host config parsing only because `graphiti_core` and `neo4j` Python modules are absent on the host checkout | Keep destructive graph DB operations out of autopilot; run current-pass graph smoke only through an approved dependency/bootstrap runner or isolated container |
| M7 Central sync shadow | done for v1 shadow | `CentralBrainShadowRebuilder` deterministically rebuilds derived graph projection from replayed current payloads; Ubuntu worker-container smoke returned `status=succeeded` | Production transport remains outside v1 local milestone |
| M8 Thin MCP/stdio surface | done | `uv run pytest -q tests/test_neuron_mcp_stdio.py ...` passed | Keep tools read-oriented and backend-neutral |
| M9 RAGFlow bridge compatibility | done for read-only bridge contract and Ubuntu live smoke | `test_ragflow_bridge_compatibility.py` proves bridge hit is external evidence and bridge outage does not fail ContextPack; Ubuntu RAGFlow bridge smoke returned `dataset_count=2`, `evidence_count=3` | Keep bridge read-only and non-canonical |

## Current Evidence

M13 current Ubuntu promotion refresh, 2026-06-19:

```text
Ubuntu checkout: /home/ragflow/Projects/neurons
Commit: 8b9596dfdbd2af0a95ea01b3a407971941a87e45
Git state: detached HEAD, clean at checkout creation
Docker server: 29.5.0
Docker Compose: 5.1.3
```

Current Ubuntu compose profile check:

```text
docker compose --profile llm-brain-graph config --services
```

Result:

```text
nats-jetstream
ingress-api
ingress-worker-py
llm-brain-neo4j
```

The generated compose config includes:

```text
LLM_BRAIN_NEO4J_URI=bolt://llm-brain-neo4j:7687
NEO4J_AUTH=neo4j/llmbrain
llm-brain-neo4j-data
llm-brain-neo4j-logs
127.0.0.1 host publish binding
```

Current Ubuntu host tool reality:

```text
present: git, python3, zstd
absent: uv, ruby, java, gradle
```

No package install was performed because package mutation is a hard stop for
the current autopilot lane.

Current Ubuntu host Python smokes, using `PYTHONPATH=worker/lib`:

```json
{
  "imports": [
    "agent_knowledge.llm_brain_core.portable_cli",
    "agent_knowledge.mcp_server",
    "agent_knowledge.llm_brain_core.graphiti_adapter"
  ],
  "mcp_stdio_tools": {
    "tool_count": 10,
    "missing": []
  },
  "portable_archive": {
    "export_status": "exported",
    "import_status": "imported",
    "raw_tables_included": false,
    "graph_db_files_included": false
  }
}
```

Current Ubuntu Graphiti dependency state:

```text
graphiti_core: absent
neo4j Python driver: absent
Graphiti config parse: ok
```

Therefore the previous Graphiti/Neo4j adapter evidence below remains valid
verification-time evidence for the implementation, but it is not a current-pass
host dependency proof for the newly checked-out Ubuntu worktree. A current-pass
adapter smoke must either bootstrap approved Python dependencies or run through
an isolated container runner without destructive Docker/volume operations.

Current Ubuntu read-only runtime probes:

```json
{
  "couchdb": {
    "container": "running",
    "root": "reachable",
    "raw_body_returned": false
  },
  "nats": {
    "container": "healthy",
    "healthz": "ok"
  },
  "ingress_api": {
    "container": "healthy",
    "spring_health": "UP"
  },
  "ragflow": {
    "container": "running",
    "api_auth_boundary": "401 on /api/v1/datasets",
    "credential_used": false
  }
}
```

Latest targeted local worker check:

```bash
cd worker
uv run pytest -q tests/test_graphiti_neo4j_adapter.py \
  tests/test_graph_projection_worker.py \
  tests/test_llm_brain_core_runtime_integration.py \
  tests/test_ontology_episode_batch.py
```

Result:

```text
17 passed, 1 warning
```

Latest full worker check:

```bash
cd worker
uv run pytest -q
```

Result:

```text
773 passed, 7 skipped, 1 warning
```

Ubuntu CouchDB read-only adapter smoke:

```json
{
  "status": "available",
  "details": ["couchdb_http_source_store"],
  "db": "transcript_source",
  "raw_body_returned": false,
  "doc_type_counts": {
    "transcript_session": 3577,
    "conversation_chunk": 55772,
    "tool_evidence_bundle": 3262,
    "coverage_manifest": 3577,
    "projection_state": 3577,
    "retention_manifest": 0
  }
}
```

Ubuntu graph runtime smoke at verification time:

```bash
ssh ragflow-ubuntu 'docker inspect -f "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}" llm-brain-verify-1972ee1-llm-brain-neo4j-1'
```

Result:

```text
healthy
```

The non-production verification containers were stopped after evidence capture
without deleting volumes.

Ubuntu Graphiti/Neo4j adapter smoke:

```json
{
  "status": "available",
  "details": ["graphiti_neo4j"],
  "entity_types": ["Task", "Task"],
  "episode_count": 2,
  "matched": true
}
```

Ubuntu Neo4j dump/load smoke:

```text
neo4j dump-load ok episodes=3
```

Ubuntu central shadow smoke:

```json
{
  "status": "succeeded",
  "projected": 1,
  "duplicates": ["evt_ubuntu_sync_dup"],
  "quarantined": [],
  "search_status": "available",
  "episode_count": 1
}
```

Ubuntu MCP stdio smoke:

```json
{
  "status": "ok",
  "tool_count": 10,
  "missing": []
}
```

Ubuntu `brain-context-resolve` CLI smoke:

```json
{
  "status": "ok",
  "schema_version": "llm_brain_context_resolve.v1",
  "memory_authority": "canonical_artifact_and_card",
  "graph_status": "unavailable"
}
```

Ubuntu RAGFlow read-only bridge smoke:

```json
{
  "status": "available",
  "details": ["ragflow_read_only_bridge"],
  "dataset_count": 2,
  "evidence_count": 3
}
```

Ubuntu portable archive smoke:

```json
{
  "export_rc": 0,
  "import_rc": 0,
  "archive_exists": true,
  "roundtrip_ok": true
}
```

Cross-repo dendrite SourceRef check:

```bash
cd /Users/example/Projects/dendrite/.worktrees/source-ref-catalog
uv run pytest -q
```

Result:

```text
84 passed
```

Latest full worker check:

```bash
cd worker
uv run pytest -q
```

Result:

```text
773 passed, 7 skipped, 1 warning
```

Dendrite review:

```text
CodeRabbit light review on codex/source-ref-catalog found 1 valid major finding.
Fix commit: e48f159 SourceRef resolve bounded read 보강.
Follow-up CodeRabbit light review: findings 0.
```

Neurons review:

```text
CodeRabbit light review on worker/lib/agent_knowledge/llm_brain_core found valid findings.
Fix commits: 310427c, 7dd2b07.
Follow-up CodeRabbit light review after cooldown: findings 0.
```

Compose profile static check:

```bash
docker compose --profile llm-brain-graph config --services
docker compose --profile llm-brain-graph config --volumes
```

Result:

```text
nats-jetstream
ingress-api
ingress-worker-py
llm-brain-neo4j
llm-brain-neo4j-data
llm-brain-neo4j-logs
```

Container deployment target:

All `neurons` LLM-Brain/Graphiti/Neo4j/FalkorDB runtime containers are Ubuntu
deployment targets. Mac-local execution is limited to source editing, Python
tests, thin-client capture code, and SSH control-plane commands.

## Hard Stop Gates

- Raw private transcript or raw PC file body is required.
- Public output would expose raw absolute paths, secrets, dataset ids, document
  ids, or backend-private ids.
- RAGFlow write/delete/disable is required.
- Docker volume deletion, credential edit, firewall/systemd/package mutation,
  production K3s/Nomad deployment, or central server mutation is required.
- A required change contradicts `requirements.md` or `design.md`.
