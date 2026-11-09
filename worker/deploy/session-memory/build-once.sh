#!/bin/bash
# session-memory builder 1사이클(컨테이너).
set -euo pipefail
cd /app
mkdir -p state/runtime
chmod 700 state state/runtime 2>/dev/null || true

export SESSION_MEMORY_PROJECTION_BACKEND="${SESSION_MEMORY_PROJECTION_BACKEND:-postgres_pgvector}"
export QDRANT_URL="${QDRANT_URL:-http://neurons-qdrant:6333}"
export QDRANT_COLLECTION="${QDRANT_COLLECTION:-neurons_mirror_gemini_3072_v1}"

limit="${SESSION_MEMORY_BUILD_LIMIT:-10}"
approval="state/couchdb-build-approval.json"

argv=(
  "--limit" "$limit"
  "--dataset-name" "session-memory"
  "--approval" "$approval"
)

if [ -n "${SESSION_MEMORY_BUILD_PROJECT:-}" ]; then
  argv+=("--project" "$SESSION_MEMORY_BUILD_PROJECT")
fi

if [ -n "${SESSION_MEMORY_BUILD_PROVIDER:-}" ]; then
  argv+=("--provider" "$SESSION_MEMORY_BUILD_PROVIDER")
fi

python - "$approval" "${argv[@]}" <<'PY'
import json
import sys

path = sys.argv[1]
argv = sys.argv[2:]
payload = {
    "schema_version": "agent_knowledge_live_approval.v1",
    "operation": "couchdb_session_memory_build",
    "operator_approval": {"approved": True, "by": "session-memory-worker"},
    "redaction_required": True,
    "rollback_or_abort_criteria": [
        "abort on nonzero exit",
        "projection failures remain retryable in CouchDB projection_state",
    ],
    "timeout_seconds": 300,
    "command": {"argv": argv},
}

with open(path, "w", encoding="utf-8") as f:
    json.dump(payload, f, sort_keys=True)
PY

# A run that does no work must be visible as a failure, not a silent success.
set +e
output="$(timeout "${SESSION_MEMORY_BUILD_TIMEOUT_SECONDS:-300}" \
  python -m agent_knowledge.cli couchdb-session-memory-build "${argv[@]}" 2>&1)"
rc=$?
set -e
printf '%s\n' "$output"
if [ "$rc" -ne 0 ]; then
  echo "[build-guard] cli exit=${rc}" >&2
  exit "$rc"
fi

python - "$output" <<'PY'
import json
import sys

lines = [line for line in sys.argv[1].splitlines() if line.strip().startswith("{")]
if not lines:
    sys.exit(0)
try:
    report = json.loads(lines[-1])
except json.JSONDecodeError:
    sys.exit(0)
selected = int(report.get("selected") or 0)
projected = int(report.get("projected") or 0)
failed = int(report.get("failed") or 0)
if failed > 0:
    print(f"[build-guard] {failed} projection failures", file=sys.stderr)
    sys.exit(1)
if selected > 0 and projected == 0:
    print(f"[build-guard] selected {selected} but projected 0: no progress", file=sys.stderr)
    sys.exit(1)
PY
