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
cd /Users/ddalkak/Projects/neurons/.worktrees/llm-brain-core-design/worker
uv run pytest -q
```

Expected current result:

```text
763 passed, 7 skipped, 1 warning
```

Run in `dendrite`:

```bash
cd /Users/ddalkak/Projects/dendrite/.worktrees/source-ref-catalog
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
cd /Users/ddalkak/Projects/neurons/.worktrees/llm-brain-core-design/worker
uv run neuron-knowledge mcp-stdio --ledger /path/to/ledger.sqlite
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

## Portable Export/Import

Export:

```bash
cd /Users/ddalkak/Projects/neurons/.worktrees/llm-brain-core-design
./scripts/brain-export --ledger /path/to/ledger.sqlite --out exports/brain.tar.gz --repo-root "$PWD"
```

Import:

```bash
cd /Users/ddalkak/Projects/neurons/.worktrees/llm-brain-core-design
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

## Ubuntu Graph Runtime Smoke

Static compose profile check:

```bash
cd /Users/ddalkak/Projects/neurons/.worktrees/llm-brain-core-design
ruby -ryaml -e 'data=YAML.load_file("compose.yaml"); svc=data.fetch("services").fetch("llm-brain-neo4j"); raise "profile" unless svc.fetch("profiles") == ["llm-brain-graph"]; vols=data.fetch("volumes"); raise "data volume" unless vols.key?("llm-brain-neo4j-data"); raise "logs volume" unless vols.key?("llm-brain-neo4j-logs"); puts "compose yaml static check ok"'
```

Ubuntu host preflight:

```bash
ssh ragflow-ubuntu 'docker version --format "{{.Server.Version}}"'
ssh ragflow-ubuntu 'docker compose version --short'
```

Live graph smoke requires:
- the copied or checked-out `neurons` worktree on Ubuntu;
- isolated compose project name and non-production ports;
- Neo4j credentials supplied through the smoke environment;
- no volume deletion or reuse of production volumes.

Expected verification shape:

```text
neo4j health: healthy
Graphiti adapter smoke: status=available, details=["graphiti_neo4j"], matched=true
Neo4j dump/load smoke: neo4j dump-load ok episodes=3
```

Do not run `docker compose down -v` as part of this runbook. Graph DB data
directory sync between PCs is not allowed; use event replay or Neo4j dump/load.

## Dendrite SourceRef Catalog

Scan an explicitly approved project root:

```bash
cd /Users/ddalkak/Projects/dendrite/.worktrees/source-ref-catalog
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
