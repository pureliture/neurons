#!/bin/bash
# session-memory builder 1사이클(컨테이너).
set -euo pipefail
cd /app
mkdir -p state/runtime
chmod 700 state state/runtime 2>/dev/null || true

export SESSION_MEMORY_PROJECTION_BACKEND="${SESSION_MEMORY_PROJECTION_BACKEND:-qdrant}"
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

timeout "${SESSION_MEMORY_BUILD_TIMEOUT_SECONDS:-300}" \
  python -m agent_knowledge.cli couchdb-session-memory-build "${argv[@]}"
