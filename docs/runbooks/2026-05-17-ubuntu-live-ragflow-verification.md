# Ubuntu Live RAGFlow Verification

Date: 2026-05-17 KST
Host: `ragflow-ubuntu` (`ragflow-box`)
Workspace: `/home/ragflow/rag-ingress-queue-smoke`

## Scope

이 검증은 `rag-ingress-queue`를 Ubuntu에서 live RAGFlow target에 연결한 상태로 실행하고, 다음 gate를 확인한다.

- Live RAGFlow write: queue API -> NATS JetStream -> worker -> RAGFlow document upload/metadata/parse request.
- External authorization: RAGFlow document id 자체가 아니라 private Ledger gate를 통과한 document만 recall/promote eligible로 취급.
- Recall promote: RAGFlow retrieval candidate 중 Ledger authorization pass가 있는 document만 promote eligible.
- Runtime persistence: 검증 후 `rag-ingress-queue-smoke` compose stack을 계속 running 상태로 유지.

RAGFlow compose project, RAGFlow DB, RAGFlow volume은 직접 수정하지 않는다. RAGFlow REST API만 사용한다.

## Runtime Config

Live stack은 별도 compose project `rag-ingress-queue-smoke`에서 실행한다.

```bash
cd /home/ragflow/rag-ingress-queue-smoke
set -a
. /home/ragflow/.config/ragflow/env
set +a
export RAGFLOW_DELIVERY_ENABLED=true
export RAGFLOW_BASE_URL=http://host.docker.internal:9380
export RAGFLOW_VERIFY_BASE_URL=http://127.0.0.1:9380
export RAGFLOW_TRANSCRIPT_MEMORY_DATASET_ID=<transcript-memory dataset id from live RAGFlow API>
docker compose -f compose.yaml up --build -d
```

`RAGFLOW_API_KEY`는 `/home/ragflow/.config/ragflow/env`에서만 로드하며 evidence에는 출력하지 않는다.

## Evidence

Live write 문서:

```text
filename: rag_ingress_live_verify_614f22f62501.md
documentRefHash: sha256:ef3b5fe0386428ea
```

RAGFlow parser backlog 때문에 이 문서는 `RUNNING` 상태에 머물렀다. 같은 live-written document에 RAGFlow official chunk API로 searchable verification chunk를 추가한 뒤, 동일 document id를 private Ledger gate로 authorize하여 recall/promote eligibility를 검증했다.

Fresh same-document gate evidence:

```json
{
  "runtime": {
    "verified": true,
    "scope": "ubuntu-live-ragflow-write-same-document-chunk-ledger-recall-promote"
  },
  "externalAuthorization": {
    "preAuthorizationEligible": false,
    "ledgerAuthorization": "pass",
    "ledgerPathPrivate": true
  },
  "recallPromote": {
    "retrievalCandidateCount": 5,
    "authorizedResultCount": 1,
    "promoteEligible": true
  }
}
```

Reusable verifier evidence, updated after backlog-aware pressure was added:

```bash
./scripts/live-ragflow-verify.py \
  --existing-filename rag_ingress_live_verify_614f22f62501.md \
  --allow-same-document-chunk-fallback \
  --allow-preauthorized-existing-document \
  --allow-closed-existing-document \
  --timeout 180 \
  --evidence build/reports/rag-ingress-queue/live-existing-closed-pressure-verify-v2.json
```

Result:

```json
{
  "runtime": {
    "verified": true,
    "scope": "ubuntu-compose-existing-live-document-external-authorization-recall-promote",
    "targetPressure": "CLOSED"
  },
  "status": {
    "externalStatus": "configured",
    "target": {
      "name": "ragflow",
      "pressure": "CLOSED",
      "running": 2121,
      "unstart": 34,
      "sampled": 200
    }
  },
  "ragflowWrite": {
    "documentVisible": true,
    "indexedRun": "RUNNING",
    "searchableChunkSource": "ragflow_chunk_api_same_live_document"
  },
  "externalAuthorization": {
    "ledgerAuthorization": "pass",
    "ledgerPathPrivate": true
  },
  "recallPromote": {
    "retrievalCandidateCount": 5,
    "authorizedResultCount": 1,
    "promoteEligible": true
  }
}
```

## Current Runtime

검증 후 stack은 내려가지 않았다.

```text
rag-ingress-queue-smoke-ingress-api-1      running
rag-ingress-queue-smoke-ingress-worker-1   running
rag-ingress-queue-smoke-nats-jetstream-1   running
```

Current `/status`:

```json
{
  "queue": {
    "pending": 0,
    "inFlight": 0,
    "redelivered": 0,
    "deadLetter": 0
  },
  "target": {
    "name": "ragflow",
    "pressure": "CLOSED",
    "running": 2115,
    "unstart": 34,
    "sampled": 200
  },
  "externalStatus": "configured",
  "authorization": {
    "authorizedCount": 0
  }
}
```

`authorizedCount`는 queue API가 external Ledger를 직접 소유하지 않기 때문에 `0`으로 남는다. Recall/promote eligibility는 private Ledger evidence로 별도 증명한다.

## Known Constraint

`transcript-memory`에는 기존 RAGFlow parser backlog가 있다. 현재 backlog-aware pressure gate는 이 상태를 `CLOSED`로 판정하므로 fresh queue-to-RAGFlow write는 의도적으로 진행하지 않는다. 이 상태에서 verifier가 증명하는 것은 기존 live-written document에 대한 external authorization과 recall/promote eligibility이며, fresh write proof가 아니다.

상용 운영 기준:

- `pressure=CLOSED`: enqueue는 가능하지만 worker는 drain하지 않는다.
- queued work는 RAGFlow backlog가 threshold 아래로 내려가 `OPEN`이 된 뒤 drain된다.
- backlog가 닫힌 상태에서 새 live write를 강제로 밀어 넣지 않는다.
