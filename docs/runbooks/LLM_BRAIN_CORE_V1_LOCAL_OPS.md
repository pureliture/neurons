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
graph_status.status == "available"
memory_status.authority == "canonical_artifact_and_card"
raw_paths_printed == false by inspection of the JSON response
```

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

`brain-project` does all of the following in one production bootstrap pass:

```text
1. register SourceRef records in the Ledger-backed source catalog
2. read recent SessionMemoryArtifact rows for the project
3. read accepted MemoryCards for the project
4. project artifacts, MemoryCards, and SourceRefs into Graphiti/Neo4j
5. return projected/duplicate/failed counts without raw paths
```

Successful output shape:

```json
{
  "schema_version": "llm_brain_projection.v1",
  "status": "ok",
  "source_refs_imported": 1,
  "raw_paths_printed": false,
  "projection": {
    "status": "succeeded",
    "failed": 0
  }
}
```

If any SourceRef line is malformed or graph projection fails, the command exits
non-zero and prints `status: failed`; do not treat partial graph population as a
completed bootstrap.

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
