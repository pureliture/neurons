# rag-ingress-queue Operator Runbook

Status: Initial implementation runbook
Date: 2026-05-17

## Purpose

`rag-ingress-queue`는 redacted RAG-ready document ingest 요청을 받아 NATS JetStream을 거쳐 target adapter로 전달하는 write gateway다. RAGFlow는 첫 adapter일 뿐이며, core API/worker는 raw target ID나 token을 노출하지 않는다.

## Runtime Worker (G2 이후)

이 runbook 초안(2026-05-17) 이후 G2 cutover에서 live delivery worker가 바뀌었다. 현재 `compose.yaml` 기준:

- Java `ingress-worker`는 은퇴했고 `profiles: ["retired"]`로 게이트되어 일반 `docker compose up`에서는 기동되지 않는다.
- live delivery는 co-located Python worker `ingress-worker-py`(`build: ./worker`)가 수행하며, `RAG_INGRESS_QUEUE` WorkQueue durable `rag_target_delivery_worker`의 단일 consumer다. 상세는 `worker/README.md`를 본다.
- `gradle test`는 여전히 Java API/worker/compose-file 단위 테스트를 증명한다(아래 Local Build And Tests). live delivery 경로 자체는 Python worker가 소유한다.
- WorkQueue는 consumer를 하나만 허용하므로 은퇴한 Java worker와 `ingress-worker-py`를 동시에 같은 durable에 붙이지 않는다.

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

Mac-local blocker observed on 2026-05-17: Docker daemon socket was unavailable and the installed Docker CLI did not provide the `docker compose` subcommand. Ubuntu `ragflow-ubuntu` has Docker Engine and Compose available and was used for runtime smoke.

Abort criteria:

- API `/healthz` unavailable after 30 seconds.
- `/status` response does not match the expected redacted operator shape.
- Forbidden pattern appears in postcheck evidence.
- Worker fetches or delivers while target pressure is `THROTTLED` or `CLOSED`.
- API returns accepted without a JetStream publish ack.

## RAGFlow Pressure Gate

Production runtime must not treat “RAGFlow is configured” as enough evidence to drain the spool. The API and worker both read a redacted RAGFlow document status sample and calculate pressure from parsing backlog:

- `OPEN`: backlog below all configured thresholds.
- `THROTTLED`: `RUNNING` or `UNSTART` count reaches the throttle threshold. The worker must not fetch from JetStream.
- `CLOSED`: hard threshold reached, configuration missing, or the RAGFlow pressure read fails. The worker must not fetch from JetStream.

Default thresholds:

```text
RAGFLOW_PRESSURE_RUNNING_THROTTLE_THRESHOLD=20
RAGFLOW_PRESSURE_UNSTART_THROTTLE_THRESHOLD=5
RAGFLOW_PRESSURE_RUNNING_CLOSED_THRESHOLD=100
RAGFLOW_PRESSURE_UNSTART_CLOSED_THRESHOLD=25
```

When live RAGFlow is already backlogged, run the runtime gate with the actual blocked pressure:

```bash
python3 scripts/runtime-verify.py --expected-pressure CLOSED
```

This proves that ingest accepts a sanitized job into JetStream while the worker leaves it pending instead of adding more load to RAGFlow.

## Live RAGFlow Gate

Do not run live RAGFlow calls without a separate approval packet containing:

- argv/request
- timeout
- redaction policy
- abort criteria
- postcheck
- rollback owner
- expected evidence

Live RAGFlow smoke proves sanitized upload/status behavior only. It does not prove external authorization or recall/promote eligibility by itself. If pressure is `THROTTLED` or `CLOSED`, do not enqueue a new live write; use a previously written live document for external authorization and recall/promote verification, or wait until pressure returns to `OPEN`. Existing-document verification must be reported as existing-document recall/promote verification, not as a fresh queue-to-RAGFlow write proof.

## Rollback

Owner: local operator.

1. Stop the live delivery worker first (현재 `ingress-worker-py`; 은퇴한 Java `ingress-worker`는 `retired` profile이라 기동돼 있지 않다).
2. Keep NATS volume intact until queue state is inspected.
3. Restore previous API/worker image or config.
4. Verify RAGFlow compose project, volumes, and direct write paths were not modified.

## Persistence Limits

The MVP compose file uses a single NATS server with a named local volume. This is local MVP persistence, not high availability. A restart without `down -v` should preserve pending JetStream data, but that still needs runtime smoke evidence.
