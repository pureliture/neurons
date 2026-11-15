#!/bin/bash
# session-memory builder 1사이클(컨테이너).
set -euo pipefail
# SESSION_MEMORY_APP_DIR: 컨테이너 밖(격리 검증)에서 같은 스크립트를 돌릴 때만 바꾼다.
cd "${SESSION_MEMORY_APP_DIR:-/app}"
mkdir -p state/runtime
chmod 700 state state/runtime 2>/dev/null || true

export SESSION_MEMORY_PROJECTION_BACKEND="${SESSION_MEMORY_PROJECTION_BACKEND:-postgres_pgvector}"

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

# RUN_MODE=watch가 이벤트로 받은 세션만 처리할 때 쉼표 구분 session_id_hash를 넘긴다.
# 지정되면 CLI는 selection checkpoint를 읽지도 쓰지도 않는다.
if [ -n "${SESSION_MEMORY_BUILD_SESSION_IDS:-}" ]; then
  IFS=',' read -r -a watch_session_ids <<< "$SESSION_MEMORY_BUILD_SESSION_IDS"
  for watch_session_id in "${watch_session_ids[@]}"; do
    if [ -n "$watch_session_id" ]; then
      argv+=("--session-id-hash" "$watch_session_id")
    fi
  done
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

# Overall build limit policy (approved 2026-11-15):
# - unset/empty or 0 => unlimited; no `timeout` wrapper is used.
# - positive integer => legacy opt-in limit for backward compatibility.
# - anything else => explicit configuration error (fail closed).
# Individual HTTP request timeouts remain inside the CLI and are not changed.
timeout_setting="${SESSION_MEMORY_BUILD_TIMEOUT_SECONDS:-}"
timeout_mode="unlimited"
timeout_seconds=""
if [ -z "$timeout_setting" ]; then
  timeout_mode="unlimited"
elif [[ "$timeout_setting" =~ ^[0-9]+$ ]]; then
  if [ "$timeout_setting" -eq 0 ]; then
    timeout_mode="unlimited"
  else
    timeout_mode="limited"
    timeout_seconds="$timeout_setting"
  fi
else
  echo "[build-guard] invalid SESSION_MEMORY_BUILD_TIMEOUT_SECONDS='$timeout_setting' (unset/0=unlimited, positive integer=limit)" >&2
  exit 2
fi

# Stream child bytes through a byte-bounded, fixed-allowlist filter. Raw
# stdout/stderr is never echoed or stored; only the last JSON report line
# (<=1 MiB) is parsed, and the output is a fixed summary. The approval
# timeout_seconds field above is metadata only.
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
report_filter="$script_dir/stream_report.py"

set +e
if [ "$timeout_mode" = "limited" ]; then
  timeout "$timeout_seconds" \
    python -m agent_knowledge.cli couchdb-session-memory-build "${argv[@]}" 2>&1 | python "$report_filter"
else
  python -m agent_knowledge.cli couchdb-session-memory-build "${argv[@]}" 2>&1 | python "$report_filter"
fi
pipe_status=("${PIPESTATUS[@]}")
child_rc="${pipe_status[0]}"
filter_rc="${pipe_status[1]:-0}"
set -e
if [ "$child_rc" -ne 0 ]; then
  printf '{"schema_version":"build_guard_summary.v1","error":"child_exit","exit_code":%s}\n' "$child_rc"
  exit "$child_rc"
fi
exit "$filter_rc"
