# Ubuntu Runtime Smoke Report

Date: 2026-05-17 17:51 KST
Host: `ragflow-ubuntu` (`ragflow-box`)
Workspace: `/home/ragflow/rag-ingress-queue-smoke`
Source commit: `9fa79a6 feat: implement rag ingress queue mvp`

## Scope

This smoke verified the `rag-ingress-queue` Docker Compose runtime on Ubuntu. It did not run live RAGFlow delivery and did not modify the existing RAGFlow compose stack.

## Commands

```bash
docker info --format '{{.ServerVersion}}'
docker compose version
docker compose -f compose.yaml up --build -d
bash scripts/postcheck.sh --timeout 30 --evidence build/reports/rag-ingress-queue/postcheck.json
./scripts/runtime-verify.py --timeout 30 --evidence build/reports/rag-ingress-queue/runtime-verify.json
curl -fsS -H 'Content-Type: application/json' --data @/tmp/rag-ingress-enqueue.json http://127.0.0.1:8080/v1/ingest/enqueue
docker compose -f compose.yaml down
```

## Evidence

- Docker Engine: `29.5.0`
- Docker Compose: `v5.1.3`
- Compose services: `nats-jetstream`, `ingress-api`, `ingress-worker`
- Container state during smoke: all three services running
- API health: `{"component":"ingress-api","status":"ok"}`
- Online postcheck: passed API shape/redaction scan; `runtime.verified=false` by design because it does not prove worker delivery.
- Runtime verifier: `runtime.verified=true`, scope `ubuntu-compose-api-nats-closed-pressure-worker-gate`
- Enqueue response: `{"accepted":true,"jobId":"RAG_INGRESS_QUEUE:1","status":"queued","errors":[]}`
- Redaction rejection: `{"accepted":false,"status":"rejected","errors":["request rejected"]}` with no token echo
- JetStream stream info:
  - stream: `RAG_INGRESS_QUEUE`
  - subjects: `rag.ingress.>`
  - messages: `1`
  - first_seq: `1`
  - last_seq: `1`
- JetStream consumer info:
  - consumer: `rag_target_delivery_worker`
  - num_pending: `1`
  - num_ack_pending: `0`
  - num_redelivered: `0`
  - delivered consumer_seq/stream_seq: `0/0`

## Interpretation

Runtime verification is now stronger than the earlier Mac-local blocker:

- Verified: Ubuntu Docker/Compose build and startup.
- Verified: NATS JetStream starts with file storage.
- Verified: API publishes to JetStream and receives a publish ack.
- Verified: stream and durable consumer exist.
- Verified: default target pressure is `CLOSED`, so the worker does not fetch/deliver while live RAGFlow delivery is disabled.
- Verified: invalid bearer-token payload is rejected without echo.

Not verified:

- live RAGFlow upload/status behavior
- worker fetch/ack/nak against an enabled target adapter
- external authorization pass for recall/promote eligibility

## Cleanup

`docker compose -f compose.yaml down` was run. The named NATS volume was not removed with `down -v`.
