# Ubuntu Runtime Smoke Report

Date: 2026-05-17 17:51 KST
Host: `ragflow-ubuntu` (`ragflow-box`)
Workspace: `/home/ragflow/rag-ingress-queue-smoke`
Source revision currently deployed: `e9f2713-dirty` label in Ubuntu compose images

## Scope

This smoke verified the `rag-ingress-queue` Docker Compose runtime on Ubuntu. The latest runtime includes backlog-aware live RAGFlow pressure reads, actual JetStream queue counters in `/status`, and fail-closed worker no-drain behavior. It does not modify the existing RAGFlow compose stack.

## Commands

```bash
docker info --format '{{.ServerVersion}}'
docker compose version
docker compose -f compose.yaml up --build -d
bash scripts/postcheck.sh --timeout 30 --evidence build/reports/rag-ingress-queue/postcheck.json
./scripts/runtime-verify.py --expected-pressure CLOSED --timeout 60 --evidence build/reports/rag-ingress-queue/runtime-pressure-verify-v2.json
curl -fsS -H 'Content-Type: application/json' --data @/tmp/rag-ingress-enqueue.json http://127.0.0.1:18080/v1/ingest/enqueue
```

## Evidence

- Docker Engine: `29.5.0`
- Docker Compose: `v5.1.3`
- Compose services: `nats-jetstream`, `ingress-api`, `ingress-worker`
- Container state during smoke: all three services running
- API health: `{"component":"ingress-api","status":"ok"}`
- Online postcheck: passed API shape/redaction scan; `runtime.verified=false` by design because it does not prove worker delivery.
- Runtime verifier: `runtime.verified=true`, scope `ubuntu-compose-api-nats-pressure-worker-gate`, `expectedPressure=CLOSED`
- Enqueue response: `{"accepted":true,"jobId":"RAG_INGRESS_QUEUE:3","status":"queued","errors":[]}`
- Redaction rejection: `{"accepted":false,"status":"rejected","errors":["request rejected"]}` with no token echo
- JetStream stream info:
  - stream: `RAG_INGRESS_QUEUE`
  - subjects: `rag.ingress.>`
  - messages: `2`
  - first_seq: `2`
  - last_seq: `3`
- JetStream consumer info:
  - consumer: `rag_target_delivery_worker`
  - num_pending: `2`
  - num_ack_pending: `0`
  - num_redelivered: `0`
- Runtime verifier observed `num_pending=2` while proving no-drain semantics under `CLOSED` pressure.
- The two verification messages (`seq=2`, `seq=3`) were deleted afterward through JetStream message-delete API.
- Current `/status.queue.pending`: `0`
- Current `/status.target`: `pressure=CLOSED`, `running=2115`, `unstart=34`, `sampled=200`
- Compose runtime state: API healthy, NATS healthy, worker running, stack left up.

## Interpretation

Runtime verification is now stronger than the earlier Mac-local blocker:

- Verified: Ubuntu Docker/Compose build and startup.
- Verified: NATS JetStream starts with file storage.
- Verified: API publishes to JetStream and receives a publish ack.
- Verified: stream and durable consumer exist.
- Verified: target pressure is `CLOSED` because live RAGFlow parser backlog exceeds configured limits, so the worker does not fetch/deliver while the target is backlogged.
- Verified: invalid bearer-token payload is rejected without echo.
- Verified: `/status.queue` reports actual JetStream pending/in-flight/redelivery counters instead of hardcoded zeroes.

Not verified:

- fresh queue-to-RAGFlow write while target pressure is `OPEN`
- worker fetch/ack/nak against an enabled target adapter under `OPEN` pressure

Follow-up live verification is documented in
`docs/runbooks/2026-05-17-ubuntu-live-ragflow-verification.md`.
