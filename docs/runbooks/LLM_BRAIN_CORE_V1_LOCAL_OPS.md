# LLM-Brain Core v1 Ops Runbook

This runbook covers LLM-Brain Core v1 operations in `neurons` and the
thin-client SourceRef catalog contract in `dendrite`.

`neurons` server/brain containers are Ubuntu deployment targets. Run
LLM-Brain/Graphiti/Neo4j/FalkorDB container smokes through `ssh
ragflow-ubuntu`. Mac-local execution is limited to source editing, Python
tests, thin-client capture work, and SSH control-plane commands.

Source of truth:
- `specs/llm-brain-core-v1/requirements.md`
- `specs/llm-brain-core-v1/design.md`
- `specs/llm-brain-core-v1/implementation-matrix.md`

Set local checkout paths before running local source commands:

```bash
export NEURONS_ROOT=/path/to/neurons
export DENDRITE_ROOT=/path/to/dendrite
```

## Ubuntu Checkout Promotion

`neurons` LLM-Brain runtime promotion is performed on the Ubuntu host through
the canonical SSH alias. The non-destructive checkout path is:

```text
/home/ragflow/Projects/neurons
```

Create the first checkout only when the path does not already exist:

```bash
ssh ragflow-ubuntu 'set -eu; mkdir -p ~/Projects; test ! -e ~/Projects/neurons; git clone https://github.com/pureliture/neurons.git ~/Projects/neurons; cd ~/Projects/neurons; git checkout <commit>; git rev-parse HEAD; git status --short --branch'
```

If the checkout already exists, do not overwrite it in this runbook. Inspect it
first and decide whether a separate update/rollback plan is needed.

Current Ubuntu host preflight shape:

```bash
ssh ragflow-ubuntu 'command -v git; command -v python3; command -v zstd || true; command -v uv || true; command -v ruby || true; command -v java || true; command -v gradle || true'
ssh ragflow-ubuntu 'docker version --format "{{.Server.Version}}"'
ssh ragflow-ubuntu 'docker compose version --short'
```

The current promotion refresh found `git`, `python3`, and `zstd` present, and
`uv`, `ruby`, `java`, and `gradle` absent. Do not install packages inside this
lane; package mutation is a hard stop.

## Safety Boundary

Allowed without a live mutation approval:
- run unit/integration tests;
- run read-only CLI help and local archive export/import against test ledgers;
- create LLM-Brain portable archives from allowlisted derived tables;
- run `dendrite source-catalog scan` on explicitly supplied project roots;
- run `dendrite source-catalog resolve` only with explicit `approval_ref`.

Hard stop:
- raw transcript/body access;
- raw private path or secret output;
- RAGFlow write/delete/disable;
- Docker volume deletion;
- credential edit;
- firewall/systemd/package mutation;
- production K3s/Nomad or central server mutation.

## Graph Env Contract

These are the only environment keys the LLM-Brain graph runtime reads
(`agent_knowledge.llm_brain_core.runtime_graph` +
`GraphitiNeo4jConfig.from_env`). `LLM_BRAIN_*` is canonical; the bare
`NEO4J_*` / `GRAPHITI_*` / `OPENAI_*` / `MODEL_NAME` / `EMBEDDING_*` names are
legacy fallbacks read only when the canonical key is unset. Activation is via
the `--enable-graph` / `--graph-required` CLI flags plus
`LLM_BRAIN_GRAPH_ENABLED`; there is no `GRAPH_ENABLED` key.

| Canonical key | Legacy fallback | Default | Purpose |
| --- | --- | --- | --- |
| `LLM_BRAIN_GRAPH_ENABLED` | — | `false` | Best-effort backend toggle (truthy: `1`/`true`/`yes`/`on`). Equivalent to `--enable-graph`. `--graph-required` implies enabled. |
| `LLM_BRAIN_NEO4J_URI` | `NEO4J_URI` | `bolt://localhost:7687` | Neo4j bolt URI. |
| `LLM_BRAIN_NEO4J_USER` | `NEO4J_USER` | `neo4j` | Neo4j user. |
| `LLM_BRAIN_NEO4J_PASSWORD` | `NEO4J_PASSWORD` | `` | Neo4j password (secret; never echoed to public output). |
| `LLM_BRAIN_GRAPH_GROUP_ID` | — | `` | Default Graphiti `group_id`. Usually left empty; the per-project group key `/project/<project>` is passed explicitly. |
| `LLM_BRAIN_GRAPH_LLM_PROVIDER` | `GRAPHITI_LLM_PROVIDER` | `openai` | Graphiti LLM provider id (lowercased). |
| `LLM_BRAIN_LLM_MODEL` | `MODEL_NAME` | `` | Graphiti LLM model name. |
| `LLM_BRAIN_SMALL_LLM_MODEL` | `SMALL_MODEL_NAME` | `` | Graphiti small/cheap model name. |
| `LLM_BRAIN_LLM_BASE_URL` | `OPENAI_BASE_URL` | `` | LLM API base URL (OpenAI-compatible). |
| `LLM_BRAIN_LLM_API_KEY` | `OPENAI_API_KEY` | `` | LLM API key (secret). |
| `LLM_BRAIN_EMBEDDING_MODEL` | `EMBEDDING_MODEL` | `` | Embedding model name. |
| `LLM_BRAIN_EMBEDDING_BASE_URL` | `OPENAI_BASE_URL` | `` | Embedding API base URL. |
| `LLM_BRAIN_EMBEDDING_API_KEY` | `OPENAI_API_KEY` | `` | Embedding API key (secret). |
| `LLM_BRAIN_EMBEDDING_DIM` | — | `1024` | Embedding dimension (int). |
| `LLM_BRAIN_GRAPH_STORE_EPISODE_CONTENT` | — | `true` | Store raw episode content (off only for `0`/`false`/`no`). |
| `LLM_BRAIN_GRAPH_EXTRACT_ENTITIES` | — | `false` | Run Graphiti entity extraction (truthy: `1`/`true`/`yes`). Production default is episode-only. |
| `LLM_BRAIN_GRAPH_READ_TIMEOUT_SECONDS` | — | `30` | Per-call wait (s) for graph reads (search/retrieve). Non-positive or non-numeric falls back to default. A read past this bound degrades to `graph_status.status == "error"`, never a false `available`. |
| `LLM_BRAIN_GRAPH_WRITE_TIMEOUT_SECONDS` | — | `300` | Per-call wait (s) for graph writes (`upsert_episode`; longer because `extract_entities=true` runs an LLM). A write past this bound fails the upsert (counted as `failed`), not a silent hang. |

Notes:

- `_GRAPHITI_GROUP_ID_RE` in `graphiti_adapter.py` is an internal validation
  regex for `group_id` shape, not an environment variable.
- Secrets (`*_PASSWORD`, `*_API_KEY`) are inputs only; they must never appear in
  CLI/MCP responses, logs, or runbook evidence.

## Source Test Gate

Run in `neurons`:

```bash
cd "$NEURONS_ROOT/worker"
uv run pytest -q
```

Expected current result:

```text
773 passed, 7 skipped, 1 warning
```

Run in `dendrite`:

```bash
cd "$DENDRITE_ROOT"
uv run pytest -q
```

Expected current result:

```text
84 passed
```

## MCP Stdio Smoke

`neuron-knowledge mcp-stdio` exposes both the legacy knowledge tools and
read-oriented LLM-Brain tools.

```bash
cd "$NEURONS_ROOT/worker"
uv run neuron-knowledge mcp-stdio --ledger /path/to/ledger.sqlite
```

Codex and Claude Code are the first supported agent E2E targets. Both use the
same stdio MCP contract:

```bash
cd "$NEURONS_ROOT/worker"
LLM_BRAIN_GRAPH_ENABLED=true \
LLM_BRAIN_GRAPH_LLM_PROVIDER=openai-compatible \
LLM_BRAIN_NEO4J_URI=bolt://127.0.0.1:17687 \
LLM_BRAIN_NEO4J_USER=neo4j \
LLM_BRAIN_NEO4J_PASSWORD="$NEO4J_PASSWORD" \
LLM_BRAIN_LLM_BASE_URL=http://172.26.0.1:8930/v1 \
LLM_BRAIN_LLM_MODEL=gemini-3.5-flash-thinking \
LLM_BRAIN_EMBEDDING_BASE_URL=http://172.26.0.1:8930/v1 \
LLM_BRAIN_EMBEDDING_MODEL=gemini-embedding-2 \
LLM_BRAIN_EMBEDDING_DIM=3072 \
uv run neuron-knowledge mcp-stdio \
  --ledger /path/to/ledger.sqlite \
  --enable-graph \
  --graph-required
```

Agent config command shape:

```text
command: neuron-knowledge
args: ["mcp-stdio", "--ledger", "<ledger.sqlite>", "--enable-graph", "--graph-required"]
env: LLM_BRAIN_GRAPH_ENABLED=true and the Graphiti/Neo4j/LLM settings above
```

The agent smoke must call `brain_context_resolve`, not only `tools/list`, and
must verify:

```text
graph_status.status == "available"   (NOT "degraded" and NOT "error")
"graph_edge_degraded" not present in graph_status.details
"graph_edge_degraded" not present in the ContextPack gaps
memory_status.authority == "canonical_artifact_and_card"
raw_paths_printed == false by inspection of the JSON response
```

`graph_status.status == "degraded"` means the episode read survived but the
edge/relationship search failed (details include `graph_edge_degraded`). A
degraded graph is NOT a passing E2E gate: relationship recall is broken even
though episodes still load. Treat `degraded` and `error` as gate failures and
fix the edge index / backend before promoting.

`--graph-required` (must-have) runs a one-shot Neo4j connectivity probe at
startup. If the backend is unreachable, the entrypoint fails fast with a
non-zero exit before serving any tool call, instead of later surfacing an empty
but falsely `available` graph. `--enable-graph` alone is best-effort: an
unreachable backend degrades to an unavailable adapter without failing startup,
and it never implies `--graph-required`.

On Ubuntu, when `uv` is not installed, run the stdio import/tool-list smoke with
host Python and the checked-out library path:

```bash
ssh ragflow-ubuntu 'cd ~/Projects/neurons; tmp="$(mktemp -d)"; touch "$tmp/ledger.sqlite"; response="$(printf %s\\n "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{}}" | PYTHONPATH=worker/lib python3 -m agent_knowledge.cli mcp-stdio --ledger "$tmp/ledger.sqlite")"; RESPONSE="$response" python3 - <<'"'"'PY'"'"'
import json
import os

payload = json.loads(os.environ["RESPONSE"].splitlines()[0])
tools = {tool["name"] for tool in payload["result"]["tools"]}
required = {
    "brain_context_resolve",
    "brain_memory_search",
    "brain_incident_search",
    "brain_drift_explain",
    "brain_persona_get",
    "brain_persona_check",
    "brain_evidence_get",
}
print(json.dumps({
    "tool_count": len(tools),
    "missing": sorted(required - tools)
}, sort_keys=True))
PY'
```

Required LLM-Brain tools:
- `brain_context_resolve`
- `brain_memory_search`
- `brain_incident_search`
- `brain_drift_explain`
- `brain_persona_get`
- `brain_persona_check`
- `brain_evidence_get`

The tools must not expose raw backend identifiers, raw private paths, secrets,
RAGFlow dataset ids, or RAGFlow document ids.

## Graph Degrade Operations Matrix

The graph is a derived index. Canonical artifacts and MemoryCards always win, so
some graph states are normal degrade, not an outage. Repairing or restarting a
healthy runtime is a guardrail violation. Use this matrix before touching the
backend.

| Symptom (observed) | Normal degrade? | Action | Recovery (only if not normal) |
| --- | --- | --- | --- |
| `graph_status.status == "available"`, no `graph_edge_degraded` | n/a (healthy) | None. Do not restart. | — |
| `--enable-graph` (best-effort) and backend unreachable: adapter unavailable, `gaps` has `graph_unavailable` | Yes — best-effort by design | None required for read serving; canonical memory still answers. | Bring Neo4j back, then re-run with `--enable-graph` to re-attach. |
| `graph_status.status == "degraded"`, `details`/`gaps` include `graph_edge_degraded`: episodes load but edge/relationship search failed | Partial — episode reads survive, relationship recall is broken | Do not treat as full outage; do not wipe data. Investigate the edge index / search backend. | Rebuild/repair the edge index or fix the search backend; confirm `graph_edge_degraded` clears. |
| `graph_status.status == "error"` / `unavailable` with `--graph-required`: startup connectivity probe failed (non-zero exit, no tool served) | No — must-have contract not met | Fix the backend before serving. The fail-fast is correct; do not relax `--graph-required` to mask it. | Restore Neo4j reachability + credentials, re-run `--graph-required`; the probe must pass. |
| `brain-project` exits non-zero, `status: failed` | No — projection failed | Treat as incomplete bootstrap; do not claim coverage. | Fix the malformed SourceRef line or backend write path, re-run. |
| `truncated.any == true` on `brain-project` | Yes — bounded `--limit` window | None; it is a partial-window note, not a failure. | Raise `--limit` (artifacts cap 100) or page to widen coverage. |
| Graph write times out (`LLM_BRAIN_GRAPH_WRITE_TIMEOUT_SECONDS` exceeded), upsert counted as `failed` | Partial — a write timeout failure does not guarantee the in-flight Neo4j write did not land | Do not assume the episode is absent; do not hand-delete to "clean up". | Re-run the projection: `episode_id` MERGE is idempotent, so a re-projection converges any duplicate and self-heals (no second insert on identical content). |

Rule of thumb: `available` is the only healthy state; `degraded` and
`error`/`unavailable` are gate failures for promotion but are NOT, by themselves,
permission to restart a running backend. Diagnose first, restart only with
evidence and intent.

## Public Output Safety Checklist

Every CLI/MCP response, runbook evidence block, log line, or PR snippet copied
out of this runtime must pass all of these before sharing:

```text
[ ] no absolute paths (/Users/, /home/, /private/, C:\, UNC \\host)
[ ] no raw relative source paths (only hashed relative_path_hash)
[ ] no Neo4j/bolt URI, host:port, or backend connection string
[ ] no credentials: *_PASSWORD, *_API_KEY, Bearer tokens, NEO4J_PASSWORD value
[ ] no raw RAGFlow dataset_id or document_id
[ ] no raw transcript/source body
[ ] graph evidence shows status + details only, not raw backend node ids
[ ] schema_version present on versioned payloads (e.g. llm_brain_context_resolve.v1)
```

If any box cannot be checked, redact before sharing. A degraded-but-public-safe
response is acceptable to share; a leak is not, regardless of graph health.

## SourceRef Bootstrap and Graph Projection

`dendrite` scans local project roots and writes public SourceRef JSONL plus a
private same-device index. `neurons` imports only the public JSONL:

```bash
cd "$NEURONS_ROOT/worker"
LLM_BRAIN_GRAPH_ENABLED=true \
uv run neuron-knowledge brain-project \
  --ledger /path/to/ledger.sqlite \
  --project neurons \
  --source-ref-jsonl /path/to/dendrite-source-catalog.jsonl \
  --enable-graph \
  --graph-required
```

`brain-project` does all of the following in one production pass:

```text
1. register the SourceRef records imported from the supplied JSONL
2. read the most recent SessionMemoryArtifact rows for the project (LIMIT)
3. read the most recent accepted MemoryCards for the project (LIMIT)
4. project artifacts, MemoryCards, and SourceRefs into Graphiti/Neo4j
5. return inserted/duplicate/failed counts without raw paths
```

Scope and idempotency (important — this is not a "bootstrap import only" pass):

- SourceRefs projected = exactly the records imported from `--source-ref-jsonl`
  on this run.
- Artifacts and MemoryCards projected = the project's **most recent `--limit`
  rows** (newest-first), re-read from the Ledger on every run, not just the
  rows produced by this import. Re-running is idempotent: episode_id MERGEs on
  identical content, so unchanged rows come back as `duplicate`, not a second
  insert.
- Because steps 2-3 are bounded by `--limit`, a project with more than `--limit`
  artifacts or accepted cards re-projects only the newest window; older rows are
  not visited on this run. When a source returns at its bound, the report sets
  `truncated.<source> = true` so a partial-window run is visible instead of
  being mistaken for full coverage. Raise `--limit` (artifacts are capped at
  100) to widen the window.

Successful output shape:

```json
{
  "schema_version": "llm_brain_projection.v1",
  "status": "ok",
  "source_refs_imported": 1,
  "limit": 100,
  "truncated": {
    "any": false,
    "artifacts": false,
    "memory_cards": false
  },
  "raw_paths_printed": false,
  "projection": {
    "status": "succeeded",
    "failed": 0
  }
}
```

If any SourceRef line is malformed or graph projection fails, the command exits
non-zero and prints `status: failed`; do not treat partial graph population as a
completed bootstrap. A `truncated.any: true` result is **not** a failure, but it
means the run covered only the newest `--limit` window — widen `--limit` (or
page) before treating the projection as full-project coverage.

## Projection Cost and Resume

`brain-project` runs synchronously over the newest `--limit` canonical rows.
Two operational levers matter for cost and re-run time.

### extract_entities per-episode LLM cost

`LLM_BRAIN_GRAPH_EXTRACT_ENTITIES` controls how each episode is written:

| Mode | Env | LLM calls per episode | Cost shape |
| --- | --- | --- | --- |
| Episode-only (production default) | `LLM_BRAIN_GRAPH_EXTRACT_ENTITIES=false` | 0 | No LLM. One `EpisodicNode` MERGE per episode. Write time is backend I/O only; the write timeout's lower end applies. |
| Entity extraction | `LLM_BRAIN_GRAPH_EXTRACT_ENTITIES=true` | Multiple per episode (Graphiti `add_episode`: entity extraction + dedupe + edge extraction; embeddings if configured) | Each episode triggers several LLM completions plus embedding calls. Cost and latency scale with episode count, so a full project window of N episodes is roughly N × (per-episode extraction calls). This is why the write timeout default (300s) is far larger than the read timeout. |

Cost rule of thumb: a reproject of a project with N artifacts/cards/source-refs
issues ~N graph writes. In episode-only mode that is ~N backend writes and **no
LLM spend**. In `extract_entities=true` mode that is ~N × (entity + edge
extraction + embedding) LLM calls — budget and rate-limit accordingly before
enabling it on a large window, and prefer resume (below) to avoid paying for
already-extracted episodes again.

### Resume (skip already-projected episodes)

Re-running over the same window normally returns unchanged rows as `duplicate`
after a full upsert round-trip (and, in extract mode, after paying the LLM cost
again). Pass a file of already-projected `episode_id`s to skip them entirely:

```bash
cd "$NEURONS_ROOT/worker"
# capture episode_ids from a prior run's projection.episode_ids, one per line
uv run neuron-knowledge brain-project \
  --ledger /path/to/ledger.sqlite \
  --project <project> \
  --enable-graph \
  --resume-projected-ids /path/to/projected-episode-ids.txt
```

Skipped episodes are reported under `projection.skipped_resumed` (distinct from
`duplicates`, which still cost a round-trip). A missing/stale resume file is
best-effort: it contributes no ids and the run falls back to a full
re-projection rather than failing. `episode_id` encodes the content hash, so a
listed id is the same content; skipping it stays idempotent.

## Graph Index Lifecycle (reproject / group reset)

The graph is a **derived index**: canonical SessionMemoryArtifacts and
MemoryCards in the Ledger are the source of truth, and every graph node can be
regenerated from them. There are two standard lifecycle operations.

### 1. Project reproject (regenerate the derived index)

Re-running `brain-project` for a project re-reads the newest `--limit` canonical
rows and re-projects them. It is idempotent: `episode_id` MERGEs on identical
content, so unchanged rows return as `duplicate`, not a second insert. Use this
to refresh or rebuild the derived index after canonical writes.

```bash
cd "$NEURONS_ROOT/worker"
LLM_BRAIN_GRAPH_ENABLED=true \
uv run neuron-knowledge brain-project \
  --ledger /path/to/ledger.sqlite \
  --project <project> \
  --limit 100 \
  --enable-graph
```

Reproject is non-destructive: it never deletes graph nodes. A node that no
longer has a canonical source is **not** removed by reproject; removal is a
group-reset concern (below). Raise `--limit` if `truncated.any == true` so the
reproject window covers the rows you intend to regenerate.

### 2. Group reset (dispose a derived group before regeneration)

Each project's episodes are scoped to a Graphiti `group_id` derived from the
`/project/<project>` brain_id (see `_graphiti_group_id`). A group reset deletes
exactly that group's derived nodes so a clean reproject can regenerate them.
This is a **live graph mutation**: it is a hard-stop without explicit intent,
exact argv, a recorded pre-count, and a post-reproject recall check (see the
Safety Boundary and Graph Degrade Operations Matrix). Standard order:

```text
1. resolve the group_id for the project (the same derivation the adapter uses:
   public-safe /project/<project> -> _graphiti_group_id)
2. record the pre-reset Episodic/Entity node count for that group_id only
3. delete ONLY nodes whose group_id matches the resolved value
   (never a global wipe; never `docker compose down -v`; never another group)
4. reproject the project (operation 1) to regenerate the derived index
5. recall check: brain_context_resolve graph_status.status == "available" and
   no "graph_edge_degraded"; confirm the regenerated node count is sane
```

Because the index is derived, a group reset loses no canonical knowledge: the
Ledger still holds the artifacts and cards, and reproject rebuilds the group.
Never reset a group as a substitute for fixing a canonical write — that hides a
real data problem behind a regenerated index. Do not reset a healthy group; a
`degraded`/`error` graph status is a backend/edge-index problem first (consult
the degrade matrix), not automatically a reset trigger.

## Portable Export/Import

Export:

```bash
cd "$NEURONS_ROOT"
./scripts/brain-export --ledger /path/to/ledger.sqlite --out exports/brain.tar.gz --repo-root "$PWD"
```

Import:

```bash
cd "$NEURONS_ROOT"
./scripts/brain-import --ledger /path/to/new-ledger.sqlite --archive exports/brain.tar.gz
```

Archive includes only:
- `llm_brain_memory_cards`
- `llm_brain_session_memory_artifacts`
- `llm_brain_source_refs`
- selected spec docs

Archive excludes:
- raw transcript tables;
- RAGFlow document records;
- graph DB files;
- Docker volumes.

Use `.tar.zst` only when the local `zstd` binary is installed. Otherwise use
`.tar.gz`.

On Ubuntu without `uv`, the portable Python API can be smoke-tested without
raw transcript or graph DB file export:

```bash
ssh ragflow-ubuntu 'cd ~/Projects/neurons; tmp="$(mktemp -d)"; touch "$tmp/source.sqlite"; export_json="$(PYTHONPATH=worker/lib python3 -m agent_knowledge.cli brain-export --ledger "$tmp/source.sqlite" --out "$tmp/brain.tar.zst" --repo-root "$PWD")"; import_json="$(PYTHONPATH=worker/lib python3 -m agent_knowledge.cli brain-import --ledger "$tmp/target.sqlite" --archive "$tmp/brain.tar.zst")"; EXPORT_JSON="$export_json" IMPORT_JSON="$import_json" ARCHIVE="$tmp/brain.tar.zst" python3 - <<'"'"'PY'"'"'
import json
import os
from pathlib import Path

export_report = json.loads(os.environ["EXPORT_JSON"])
import_report = json.loads(os.environ["IMPORT_JSON"])
manifest = export_report.get("manifest", {})
print(json.dumps({
    "export_status": export_report.get("status"),
    "import_status": import_report.get("status"),
    "archive_exists": Path(os.environ["ARCHIVE"]).exists(),
    "raw_tables_included": manifest.get("raw_tables_included"),
    "graph_db_files_included": manifest.get("graph_db_files_included"),
}, sort_keys=True))
PY'
```

## Ubuntu Graph Runtime Smoke

Static compose profile check with Docker Compose:

```bash
cd "$NEURONS_ROOT"
docker compose --profile llm-brain-graph config --services
docker compose --profile llm-brain-graph config --volumes
```

Required services and volumes:

```text
nats-jetstream
ingress-api
ingress-worker-py
llm-brain-neo4j
llm-brain-neo4j-data
llm-brain-neo4j-logs
```

Live graph smoke requires:
- the copied or checked-out `neurons` worktree on Ubuntu;
- isolated compose project name and non-production ports;
- Neo4j credentials supplied through the smoke environment;
- `graphiti_core` and the `neo4j` Python driver available in the smoke runner;
- no volume deletion or reuse of production volumes.

If reusing a stopped verification Neo4j volume, confirm the baseline before
writing a smoke episode:

```text
container status: exited or created, not running
container/volume prefix: llm-brain-verify-*
production volume names: not mounted
existing Episodic count: recorded before the smoke
smoke natural_id/event_id: unique for the current run
```

Do not call a reused verification volume "clean". Treat it as a continuity
fixture and prove only the current smoke's unique episode plus the restored node
count.

Expected verification shape:

```text
neo4j health: healthy
Graphiti adapter smoke: status=available, details=["graphiti_neo4j"], matched=true
Neo4j dump/load smoke: neo4j dump-load ok episodes=3
```

Do not run `docker compose down -v` as part of this runbook. Graph DB data
directory sync between PCs is not allowed; use event replay or Neo4j dump/load.

Do not treat an older local Docker image as current merge-commit proof. The
current-pass Graphiti adapter smoke must use the checked-out commit or a runner
built from it.

Current 2026-06-19 promotion verification used a container runner built from the
Ubuntu checkout because host Python did not have `graphiti_core` or the `neo4j`
driver installed:

```text
worker image: llm-brain-verify-worker:8b9596d
adapter smoke: status=available, details=["graphiti_neo4j", "episodic_node_direct_read"], matched=true
dump/load smoke: status=ok, episodes=4, graph_db_files_synced_between_pcs=false
```

The dump/load current-pass used the existing stopped verification Neo4j data
volume and a new restore verification volume. It did not delete graph volumes
and did not sync graph DB files between PCs.

### Manual Live E2E Evidence (merge gate)

The in-repo test suite covers the graph seam with a Fake adapter and a
skip-unless-env live round-trip
(`tests/test_graphiti_neo4j_adapter.py::test_graphiti_neo4j_round_trip_against_live_backend`,
skipped unless `NEO4J_URI` / `LLM_BRAIN_NEO4J_URI` is set). The suite alone does
NOT prove a live backend. Before merging any change to the graph activation,
result, or status seam, capture manual live E2E evidence and attach it to the PR:

```text
1. backend reachable:   --graph-required startup did not fail the connectivity probe
2. round-trip:          one unique-natural_id episode upserted and read back from Neo4j
3. status healthy:      brain_context_resolve graph_status.status == "available"
4. no false-healthy:    graph_status.details has no "graph_edge_degraded";
                        gaps has no "graph_edge_degraded"
5. public-safe:         response has no raw paths, tokens, dataset/document ids,
                        or raw backend identifiers
6. skip honesty:        the skip-unless-env round-trip test ran (not skipped) for
                        this evidence, i.e. NEO4J_URI / LLM_BRAIN_NEO4J_URI was set
```

A change that cannot show items 1-6 from a live run must not claim live E2E
coverage; record that the live gate is pending and keep the merge blocked on it.

## Dendrite SourceRef Catalog

Scan an explicitly approved project root:

```bash
cd "$DENDRITE_ROOT"
uv run dendrite source-catalog scan \
  --root /path/to/project \
  --root-id project-root \
  --device-id "$HOSTNAME" \
  --public-out /tmp/source-catalog.jsonl \
  --private-index /tmp/dendrite-source-private/index.json
```

Public catalog records contain:
- `source_ref_id`
- `device_id_hash`
- `root_id`
- `relative_path_hash`
- `content_hash`
- timestamps and sync policy

Public catalog records must not contain:
- absolute paths;
- raw relative paths;
- file body;
- secrets.

Resolve only on the same device and only with approval:

```bash
uv run dendrite source-catalog resolve \
  --private-index /tmp/dendrite-source-private/index.json \
  --source-ref-id src_... \
  --requesting-device-id "$HOSTNAME" \
  --approval-ref approval:manual
```

Without `approval_ref`, resolution must return `approval_required` and empty
content. With approval, content is bounded by `--max-bytes` and redacted before
printing.

## Review Gate

CodeRabbit review is expected before PR-ready close.

Current review state:
- `neurons`: CodeRabbit light review found findings, fixes were committed.
- `dendrite`: CodeRabbit light review found one major finding, fixed in
  `e48f159`, follow-up review returned `findings: 0`.

If CodeRabbit returns `rate_limit`, keep tests and manual audit evidence, then
rerun the review after the reported wait time. Do not mark review clean from
stale findings.
