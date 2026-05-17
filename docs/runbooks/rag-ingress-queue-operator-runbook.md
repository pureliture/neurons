# rag-ingress-queue Operator Runbook

Status: Initial implementation runbook
Date: 2026-05-17

## Purpose

`rag-ingress-queue`는 redacted RAG-ready document ingest 요청을 받아 NATS JetStream을 거쳐 target adapter로 전달하는 write gateway다. RAGFlow는 첫 adapter일 뿐이며, core API/worker는 raw target ID나 token을 노출하지 않는다.

## Prerequisites

- Amazon Corretto 25
- Gradle 9.x
- Docker daemon and Docker Compose for runtime smoke
- `jq`, `curl`, `rg` for postcheck evidence generation and redaction scan
- Separate explicit approval before any live RAGFlow call

## Local Build And Tests

```bash
JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test
```

This proves unit/API/worker/compose-file tests only. It does not prove Docker Compose NATS runtime connectivity or live RAGFlow delivery.

## Offline Postcheck

```bash
bash scripts/postcheck.sh --offline --timeout 30 --evidence build/reports/rag-ingress-queue/postcheck.json
```

Offline postcheck proves JSON evidence formatting and redaction scan only. It does not contact API, NATS, or RAGFlow.

## Compose Smoke

Ubuntu smoke evidence exists in `docs/runbooks/2026-05-17-ubuntu-runtime-smoke.md`. Mac-local Docker/Compose can still be unavailable; do not treat Mac-local blocker as runtime failure if Ubuntu smoke is the target environment.

Expected command:

```bash
docker compose -f compose.yaml up --build -d
bash scripts/postcheck.sh --timeout 30 --evidence build/reports/rag-ingress-queue/postcheck.json
docker compose -f compose.yaml down
```

Mac-local blocker observed on 2026-05-17: Docker daemon socket was unavailable and the installed Docker CLI did not provide the `docker compose` subcommand. Ubuntu `ops-host` has Docker Engine and Compose available and was used for runtime smoke.

Abort criteria:

- API `/healthz` unavailable after 30 seconds.
- `/status` response does not match the expected redacted operator shape.
- Forbidden pattern appears in postcheck evidence.
- Worker fetches or delivers while target pressure is `THROTTLED` or `CLOSED`.
- API returns accepted without a JetStream publish ack.

## Live RAGFlow Gate

Do not run live RAGFlow calls without a separate approval packet containing:

- argv/request
- timeout
- redaction policy
- abort criteria
- postcheck
- rollback owner
- expected evidence

Live RAGFlow smoke proves sanitized upload/status behavior only. It does not prove external authorization or recall/promote eligibility. The default worker config keeps `rag-ingress.target.ragflow.delivery-enabled=false` until this gate is explicitly approved.

## Rollback

Owner: local operator.

1. Stop `ingress-worker` first.
2. Keep NATS volume intact until queue state is inspected.
3. Restore previous API/worker image or config.
4. Verify RAGFlow compose project, volumes, and direct write paths were not modified.

## Persistence Limits

The MVP compose file uses a single NATS server with a named local volume. This is local MVP persistence, not high availability. A restart without `down -v` should preserve pending JetStream data, but that still needs runtime smoke evidence.
