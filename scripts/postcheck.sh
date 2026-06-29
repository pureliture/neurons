#!/usr/bin/env bash
set -euo pipefail

MODE="online"
TIMEOUT_SECONDS="30"
EVIDENCE_PATH="build/reports/rag-ingress-queue/postcheck.json"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --offline)
      MODE="offline"
      shift
      ;;
    --timeout)
      TIMEOUT_SECONDS="$2"
      shift 2
      ;;
    --evidence)
      EVIDENCE_PATH="$2"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

mkdir -p "$(dirname "$EVIDENCE_PATH")"

if [[ "$MODE" == "offline" ]]; then
  cat > "$EVIDENCE_PATH" <<'JSON'
{
  "mode": "offline",
  "health": {"status": "skipped"},
  "queue": {"pending": 0, "inFlight": 0, "redelivered": 0, "deadLetter": 0},
  "target": {"name": "retired_index_bridge", "pressure": "CLOSED"},
  "documentStatus": {"indexedCandidateCount": 0},
  "authorization": {"authorizedCount": 0},
  "externalStatus": "not_configured",
  "runtime": {"verified": false, "reason": "offline postcheck does not contact API or NATS"}
}
JSON
else
  API_URL="${RAG_INGRESS_API_URL:-http://127.0.0.1:18080}"
  health="$(curl --max-time "$TIMEOUT_SECONDS" -fsS "$API_URL/healthz")"
  status="$(curl --max-time "$TIMEOUT_SECONDS" -fsS "$API_URL/status")"
  jq -n \
    --argjson health "$health" \
    --argjson status "$status" \
    '{
      mode: "online",
      health: $health,
      queue: $status.queue,
      target: $status.target,
      documentStatus: $status.documentStatus,
      authorization: $status.authorization,
      externalStatus: $status.externalStatus,
      runtime: {
        verified: false,
        reason: "online postcheck verifies API shape only; JetStream and worker smoke require compose/runtime evidence"
      }
    }' > "$EVIDENCE_PATH"
fi

jq -e '.mode and .queue and .target and .documentStatus and .authorization and .externalStatus and .runtime' "$EVIDENCE_PATH" >/dev/null

denylist_hits="$(mktemp -t rag-ingress-postcheck-denylist.XXXXXX)"
trap 'rm -f "$denylist_hits"' EXIT
if rg -n -f scripts/redaction-denylist.txt "$EVIDENCE_PATH" >"$denylist_hits"; then
  cat "$denylist_hits" >&2
  exit 1
fi

cat "$EVIDENCE_PATH"
