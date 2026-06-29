# rag-ingress-queue 요구사항

Status: Draft aligned requirements
Date: 2026-05-17
Owner: local operator
Target project path: `<repo>`

## 1. 목적

`rag-ingress-queue`는 local PC, Mac mini, future producer에서 발생하는 redacted RAG-ready document ingest 요청을 받아 downstream RAG target이 감당 가능한 속도로 전달하는 범용 ingress queue 서버다.

현재 첫 target은 RetiredIndexBridge다. 그러나 core server는 RetiredIndexBridge 전용으로 만들지 않는다. RetiredIndexBridge는 `RagTargetAdapter` 구현체 중 하나로 둔다.

현재 `workspace-index-advisor`의 확정 구조에서는 RetiredIndexBridge를 장기기억 본문과 검색 대상이 모이는 `LLM wiki`로 사용한다. `rag-ingress-queue`는 그 wiki로 안전하게 쓰기 위한 delivery/write-admission 계층이며, session summary 생성, long-term memory 판단, recall routing, lifecycle/GC 판단은 하지 않는다.

## 2. 문제 배경

현재 ingest 흐름은 Mac mini 쪽 runtime이 RetiredIndexBridge REST endpoint를 직접 호출하는 구조다. 이 구조에서는 producer가 RetiredIndexBridge parser backlog를 충분히 고려하지 못하고, 대량 migration이나 여러 session이 동시에 발생할 때 downstream RAG에 과도한 backlog가 쌓일 수 있다.

이미 RetiredIndexBridge 내부에 backlog가 쌓이면 RetiredIndexBridge가 처리할 수는 있지만, 지속 운영에서는 write admission control, retry, redelivery, dead-letter, target-specific backpressure 정책이 producer와 분리되어야 한다.

## 3. 범위

### In scope

- Java 25 + Spring Boot 기반 server 구현 요구사항
- Java virtual threads 기반 blocking I/O 처리 모델
- NATS JetStream 기반 queue, ack, retry, redelivery, dead-letter 위임
- 별도 Docker Compose project 구성
- local PC/Mac mini producer용 HTTP enqueue API
- redacted RAG-ready document payload/ref enqueue contract
- Mac producer-side source resolution, redaction, packing boundary
- worker의 pull consumer 처리
- target adapter contract
- 첫 adapter로 RetiredIndexBridge REST adapter 정의
- MCP/read plane과 ingest write path 분리
- operator-facing status, health, redacted evidence
- target profile별 RetiredIndexBridge dataset routing
- `session-compactor`와 `memory-regeneration-runner`가 모두 사용할 수 있는 write gateway

### Out of scope

- RetiredIndexBridge Docker Compose project 수정
- RetiredIndexBridge 내부 Redis 공유
- RetiredIndexBridge DB/volume 직접 조작
- MCP를 bulk ingest write path로 사용하는 설계
- raw transcript body, token, raw dataset_id, raw document_id, private path 노출
- Ubuntu-side worker가 Mac-only private locator를 직접 해석하는 설계
- document status authorization pass 전 recall/promote 활성화
- session summary, task summary, approved memory card 생성
- memory-regeneration-runner 구현
- recall routing, lifecycle plan, GC/delete 판단
- RetiredIndexBridge Memory/Agent feature 생성 또는 mirror 운영
- Mac mini local `ledger.py`/document status SQLite를 queue worker가 직접 갱신하는 설계

## 4. 고정 기술 결정

| 항목 | 결정 |
|---|---|
| Runtime | `Java 25` |
| Framework | `Spring Boot 4.x` |
| Concurrency | Java virtual threads |
| Queue engine | `NATS JetStream` |
| Queue model | Work queue stream + durable pull consumer |
| Deployment | RetiredIndexBridge와 분리된 Docker Compose project |
| First target adapter | `RetiredIndexBridgeAdapter` |
| Product name | `rag-ingress-queue` |

Spring Boot 4.x는 Java 25를 target runtime으로 쓰는 요구사항에 맞는 후보로 둔다. Spring Boot 공식 system requirements는 Spring Boot 4.0.6이 Java 17 이상을 요구하고 Java 26까지 호환된다고 설명한다.

Spring Boot virtual threads는 `spring.threads.virtual.enabled=true`로 활성화하는 방향을 기본값으로 둔다. 단, HTTP connection pool, NATS pull batch, worker concurrency, target adapter rate limit은 별도 설정으로 제한한다.

## 5. 전체 아키텍처

```text
Local PC / Mac mini session-compactor / Ubuntu memory-regeneration-runner
  parse + redaction + pack target-ready document
        |
        v
  POST /v1/ingest/enqueue
        |
        v
rag-ingress-queue ingress-api
  validate + publish to JetStream
        |
        v
NATS JetStream
  stream + durable pull consumer
        |
        v
rag-ingress-queue ingress-worker
  pull batch -> check target pressure -> deliver
        |
        v
RagTargetAdapter
  RetiredIndexBridgeAdapter first
        |
        v
Downstream RAG target
```

`memory-regeneration-runner`는 queue consumer가 아니다. Ubuntu에서 RetiredIndexBridge redacted corpus를 읽어 session/task summary 또는 approved memory card 후보 문서를 만든 뒤, 그 결과물을 다시 `/enqueue`로 넣는 batch producer다.

## 6. Compose project 요구사항

별도 compose project를 사용한다.

```text
project: rag-ingress-queue
services:
  nats-jetstream
  ingress-api
  ingress-worker
volumes:
  nats_data
```

RetiredIndexBridge compose project와 network, volume, service lifecycle을 분리한다. RetiredIndexBridge와 통신할 때는 host-published endpoint 또는 명시적으로 허용된 network bridge를 사용한다. 초기안은 RetiredIndexBridge compose 파일을 수정하지 않는 것을 원칙으로 한다.

## 7. NATS JetStream 요구사항

### Stream

```text
name: RAG_INGRESS_QUEUE
subjects:
  - rag.ingress.transcript
  - rag.ingress.document
retention: WorkQueuePolicy
storage: file
```

`WorkQueuePolicy`는 work queue 성격에 맞게 ack된 message를 stream에서 제거하는 모델로 사용한다.

### Consumer

```text
name: rag_target_delivery_worker
mode: pull
ack_policy: explicit
max_deliver: 5
ack_wait: configurable
max_ack_pending: bounded
```

worker는 pull consumer를 사용한다. Pull consumer는 worker가 필요한 만큼 batch를 가져오는 구조이므로 target pressure가 높을 때 fetch 자체를 줄이거나 중단할 수 있다.

## 8. Spring Boot service 요구사항

### 공통

- Java 25 runtime image 사용
- Spring Boot 4.x
- `spring.threads.virtual.enabled=true`
- structured JSON logging
- secret redaction filter
- Actuator health endpoint
- graceful shutdown

### `ingress-api`

Responsibilities:

- `POST /v1/ingest/enqueue`
- request validation
- idempotency key 생성 또는 검증
- JetStream publish
- publish ack 확인
- raw transcript body 저장 금지
- redacted RAG-ready document payload 또는 redacted document blob reference만 허용
- Mac-only private locator payload 거부

Non-responsibilities:

- RAG target 직접 upload
- target backlog polling
- recall/promote 결정

### `ingress-worker`

Responsibilities:

- JetStream pull consumer
- bounded batch fetch
- target adapter pressure check
- target adapter delivery
- status polling 또는 deferred recheck
- delivery job status update
- adapter-private target document reference 관리
- external document status table이 reconcile할 수 있는 redacted status snapshot 제공
- success ack
- retry/nak/dead-letter policy

Non-responsibilities:

- producer authentication policy의 복잡한 확장
- target-specific payload schema를 core에 노출
- provider transcript parsing, redaction, packing
- Mac-only source locator 해석
- raw private path/body logging
- summary/card generation
- recall/promote authorization 판단
- Mac mini local document status SQLite 직접 mutation

## 9. API 요구사항

### `POST /v1/ingest/enqueue`

Request shape:

```json
{
  "source": {
    "type": "local_pc",
    "provider": "codex",
    "project": "workspace-index-advisor"
  },
  "payload": {
    "kind": "redacted_rag_ready_document",
    "redactionVersion": "redaction.v2",
    "document": {
      "filename": "ak-conv-codex-workspace-index-advisor-session-t0001-t0008-redacted.md",
      "contentType": "text/markdown",
      "body": "---\nschema_version: agent_knowledge_document.v2\nresult_type: conversation_chunk\n---\nredacted conversation chunk",
      "metadata": {
        "schema_version": "agent_knowledge_document.v2",
        "result_type": "conversation_chunk",
        "provider": "codex",
        "project": "workspace-index-advisor",
        "session_id_hash": "sha256:redacted",
        "content_hash": "sha256:redacted"
      }
    }
  },
  "contentHash": "sha256:redacted",
  "targetProfile": "index-transcript-memory",
  "kind": "conversation_chunk"
}
```

Large payloads may later use a `payload.kind=redacted_document_ref` shape, but the referenced blob must be readable by the Ubuntu-side worker and must already be redacted and target-ready. A Mac-only private locator is not a valid steady-state queue payload.

Initial target profiles:

| targetProfile | RetiredIndexBridge dataset role | document kind |
|---|---|---|
| `index-transcript-memory` | `transcript-memory` | `conversation_chunk` |
| `index-session-summary` | `session-summary` | `session_summary` |
| `index-task-summary` | `task-summary` | `task_summary` |
| `index-approved-memory-card` | `approved-memory-card` | `approved_memory_card` |

> 📌 위 표는 2026-05-17 draft 시점의 **초기 4개** profile이다. 현재 구현된 `TargetProfileRegistry`는
> **7개**(추가: `index-session-memory`·`index-project-memory`·`index-procedural-memory`)이며,
> 유효 profile의 단일 진실 공급원(SSOT)은 [docs/contracts/ingress-contract.md](contracts/ingress-contract.md) §3이다.
> 이 요구사항 문서는 점-인-타임 기록이므로 표는 그대로 둔다.

RetiredIndexBridge partitioning is dataset-first. Metadata `result_type` remains useful for trace/filter inside each dataset, but must not be the primary partitioning strategy for all memory data in a single dataset.

Response shape:

```json
{
  "accepted": true,
  "jobId": "redacted-stable-id",
  "status": "queued"
}
```

The API must not return raw private path, raw dataset_id, raw document_id, token, or raw transcript body.

### `GET /healthz`

Returns API process health only.

### `GET /status`

Returns redacted operator summary:

```json
{
  "queue": {
    "pending": 0,
    "inFlight": 0,
    "redelivered": 0,
    "deadLetter": 0
  },
  "target": {
    "name": "retired_index_bridge",
    "pressure": "open|throttled|closed"
  }
}
```

## 10. `RagTargetAdapter` contract

Core server must depend on this contract, not on RetiredIndexBridge-specific implementation details. `IngestJob` contains a redacted target-ready document or a redacted document reference, not a provider transcript locator.

```java
public interface RagTargetAdapter {
    TargetPressure checkPressure(TargetProfile profile);
    DeliveryResult deliver(IngestJob job, TargetProfile profile);
    IndexingStatus getStatus(TargetDocumentRef ref, TargetProfile profile);
    ReconcileResult reconcile(TargetDocumentRef ref, DocumentStatusSnapshot statusState);
}
```

### Generic target states

- `ACCEPTED`
- `DELIVERED`
- `INDEXING`
- `INDEXED`
- `FAILED`
- `THROTTLED`
- `AUTHORIZED`

## 11. RetiredIndexBridge adapter 요구사항

`RetiredIndexBridgeAdapter`는 RetiredIndexBridge REST API와 parser status를 generic target 상태로 변환한다.

Responsibilities:

- RetiredIndexBridge health/status 확인
- target pressure 계산
- redacted target-ready document upload/parse request
- document status polling
- RetiredIndexBridge `DONE`을 generic `INDEXED` 후보로 변환
- failed/index_timeout/reconcile 후보 판별

RetiredIndexBridge-specific values such as raw dataset_id and raw document_id must remain adapter-private and must not appear in generic operator output.

## 12. Backpressure 요구사항

Worker must check target pressure before delivery.

RetiredIndexBridge first adapter pressure inputs:

- `UNSTART` document count
- `RUNNING` document count
- stale `indexing` age/count
- RetiredIndexBridge healthz
- recent upload/parse error rate

Generic pressure states:

- `OPEN`: worker may deliver next batch
- `THROTTLED`: worker should reduce fetch/delivery rate
- `CLOSED`: worker must stop delivery and delay/nak without flooding target

## 13. External document status gate 및 authorization 요구사항

- 여기서 `Ledger`는 `workspace-index-advisor`의 기존 `ledger.py`/문서 상태표를 가리키는 이름이며, 별도 장기기억 저장소가 아니다.
- RetiredIndexBridge document status table은 RetiredIndexBridge document id/hash/state/provenance/authorization을 기록하는 external local 상태표다.
- `rag-ingress-queue`는 이 상태표를 직접 소유하거나 직접 갱신하지 않는다.
- `rag-ingress-queue`는 external reconcile client가 상태표를 갱신할 수 있도록 job id, content hash, target profile, generic status, redacted target ref/status snapshot을 제공한다.
- Queue ack와 document status table indexed는 같은 의미가 아니다.
- Target `INDEXED`와 recall/promote authorization도 같은 의미가 아니다.
- document status table authorization pass 전에는 indexed transcript memory를 recall/promote에 사용하지 않는다.
- Document status output must be redacted.
- RetiredIndexBridge live `DONE`인데 local status table이 stale이면 reconcile 대상이다.

## 14. MCP 및 호출 채널 분리

MCP는 tool/read plane으로 유지한다.

Forbidden write paths:

```text
Mac mini -> RetiredIndexBridge REST direct write
MCP -> RetiredIndexBridge REST bulk ingest write
migration script -> RetiredIndexBridge REST direct write
memory-regeneration-runner -> RetiredIndexBridge REST direct write
```

Allowed write path:

```text
Local PC producer -> rag-ingress-queue /enqueue -> NATS JetStream -> worker -> RagTargetAdapter
Mac mini session-compactor -> rag-ingress-queue /enqueue -> NATS JetStream -> worker -> RetiredIndexBridgeAdapter
Ubuntu memory-regeneration-runner -> rag-ingress-queue /enqueue -> NATS JetStream -> worker -> RetiredIndexBridgeAdapter
```

MCP에 ingest tool이 필요해지는 경우에도 MCP implementation은 RAG target을 직접 write하지 않고 `rag-ingress-queue /enqueue`로 위임한다.

## 15. 보안 및 redaction 요구사항

Must not log or expose:

- `RETIRED_INDEX_BRIDGE_API_KEY`
- bearer token
- raw dataset_id
- raw document_id
- raw private path
- raw transcript path/body
- full raw RAG target payload

Allowed evidence:

- counts
- redacted IDs
- hashes
- status class
- target adapter name
- pressure state
- latest run status

## 16. 운영 postcheck 요구사항

Postcheck must report:

- compose project status
- `nats-jetstream` health
- stream pending count
- consumer pending / ack pending / redelivered count
- ingress API health
- worker health
- target adapter pressure state
- document status indexed count and authorization count
- dead-letter count

## 17. 성공 기준

1. Local PC producer가 RAG target REST를 직접 호출하지 않는다.
2. `rag-ingress-queue /enqueue`가 message를 JetStream에 publish하고 publish ack를 확인한다.
3. Worker가 pull consumer로 message를 가져와 target pressure가 `OPEN`일 때만 delivery한다.
4. RetiredIndexBridge-specific details는 `RetiredIndexBridgeAdapter` 안에 격리된다.
5. Postcheck가 queue, worker, target, document status, authorization 상태를 redacted summary로 보여준다.
6. RetiredIndexBridge memory corpus는 dataset-first partitioning을 사용한다.

## 18. Human review checklist

- HTML 산출물에서 write path와 MCP read/tool plane이 시각적으로 분리되어야 한다.
- Docker Compose project, network, volume, port, env ownership이 RetiredIndexBridge와 겹치지 않는다는 점이 보여야 한다.
- `RagTargetAdapter` contract와 `RetiredIndexBridgeAdapter` v1 경계가 분리되어야 한다.
- `ingested`, `indexed`, `authorized`, `recall/promote eligible`은 서로 다른 상태로 표현되어야 한다.
- token, raw dataset_id, raw document_id, private path, transcript body는 예시와 다이어그램에 없어야 한다.

## 19. `workspace-index-advisor` 즉시 적용 요구사항

현재 적용 대상은 `workspace-index-advisor`의 spool 기반 session RAG 경로다. 이 경로는
provider hook을 RetiredIndexBridge 직접 write path로 쓰지 않고, Mac mini에서 provider transcript를
parse/redaction/pack한 뒤 redacted RetiredIndexBridge-ready document만 queue로 넘긴다.

현재 코드 기준으로 `session-compactor`는 새 컴포넌트가 아니라 기존
`transcript-capture`, `transcript-worker`, `transcript-packer` 역할을 묶어 부르는
운영 이름이다. 이미 구현된 capture/redaction/chunk/pack 경로는 유지하고, steady-state
write sink만 RetiredIndexBridge direct upload에서 queue enqueue로 바꾼다.

### 현재 경로

```text
Codex UserPromptSubmit
  -> session-entry-recall codex-adapter
  -> bounded additionalContext

Codex Stop
  -> transcript-capture
  -> private TranscriptCaptureSpool
  -> transcript-worker
  -> parse/redaction/pack
  -> RetiredIndexBridge upload/metadata/parse/status poll
  -> RetiredIndexBridge document status indexed state
```

현재 `Stop` hook capture는 raw transcript body를 보내지 않는다. Capture request는
`locator_only` 정책이며, private source locator는 public output에 나오면 안 된다.
`TranscriptCaptureSpool`은 `pending`, `processing`, `acked`, `quarantine` 상태를 가진
producer-side durable inbox로 취급한다.

### Cutover 목표

```text
Codex Stop
  -> transcript-capture
  -> private TranscriptCaptureSpool
  -> session-compactor enqueue sink
  -> parse/redaction/pack on Mac mini
  -> enqueue redacted RetiredIndexBridge-ready document
  -> rag-ingress-queue /enqueue
  -> NATS JetStream
  -> ingress-worker
  -> RagTargetAdapter / RetiredIndexBridgeAdapter
  -> RetiredIndexBridge document status indexed + authorization postcheck
```

요구사항:

- `UserPromptSubmit` recall path는 read/tool plane으로 유지하고 ingest queue에 넣지 않는다.
- `Stop` hook과 `TranscriptCaptureSpool`은 첫 cutover에서 유지한다.
- `session-compactor enqueue sink`는 capture request를 검증하고 Mac mini에서 transcript source를 resolve한 뒤 `redaction.v2`와 conversation chunk packing을 완료한다.
- `session-compactor enqueue sink`는 redacted RAG-ready document를 `rag.ingress.transcript` job으로 변환한다.
- `session-compactor enqueue sink`는 JetStream publish ack 또는 `/enqueue accepted` 증거 전에는 local spool item을 `acked`로 이동하지 않는다.
- 기존 `transcript-worker`의 RetiredIndexBridge direct upload 책임은 post-cutover steady-state에서 제거한다.
- Transcript parsing, `redaction.v2`, conversation chunk packing은 Mac mini producer boundary에 남긴다.
- RetiredIndexBridge document upload, metadata update, parse request, status polling은 Ubuntu-side queue worker와 `RetiredIndexBridgeAdapter` 경계 안으로 이동한다.
- `acked` capture request는 RetiredIndexBridge indexing 완료 증거가 아니다.
- JetStream ack는 document status authorization pass 증거가 아니다.
- document status authorization pass 전에는 indexed transcript memory를 recall/promote에 사용하지 않는다.
- Postcheck는 spool counts, queue counts, RetiredIndexBridge pressure, indexed count, authorization count를 모두 분리해서 보여준다.

### `workspace-index-advisor` 코드 매핑

| 현재 surface | 역할 | 적용 후 판단 |
|---|---|---|
| `session_memory/codex_hook_plan.py` | `UserPromptSubmit` / `Stop` hook command plan | hook shape는 유지 |
| `session_memory/transcript_capture.py` | `locator_only` capture request normalization + `TranscriptCaptureSpool` | producer-side inbox 유지 |
| `session_memory/transcript_ingest.py` | capture spool claim, transcript parse, RetiredIndexBridge direct upload/index poll | parse/redaction/pack은 session-compactor enqueue sink로 유지하고 direct upload 부분만 queue worker/adapter로 이동 |
| `ledger.py` | session/turn/chunk, RetiredIndexBridge document ref, indexed state, authorization metadata | 이름은 ledger지만 역할은 RetiredIndexBridge document status table로 유지 |
| `scheduler_runtime.py` | lifecycle ingest + transcript ingest scheduler command plan | scheduler enablement는 별도 approval gate |

## 20. RetiredIndexBridge LLM Wiki dataset 및 regeneration boundary

RetiredIndexBridge는 장기기억 본문과 검색 대상이 모이는 LLM wiki다. `rag-ingress-queue`는 RetiredIndexBridge에 넣는 배송 계층이고, RetiredIndexBridge dataset의 의미를 생성하거나 recall 순서를 판단하지 않는다.

Dataset partitioning:

| Dataset | Owner producer | Purpose |
|---|---|---|
| `transcript-memory` | Mac mini session-compactor | redacted source conversation chunks |
| `session-summary` | Ubuntu memory-regeneration-runner | session 단위 압축본 |
| `task-summary` | Ubuntu memory-regeneration-runner | task/goal 단위 압축본 |
| `approved-memory-card` | curation/regeneration producer | 장기기억 canonical card |

Batch regeneration path:

```text
memory-regeneration-runner
  -> read RetiredIndexBridge redacted transcript-memory corpus
  -> group by session/task fields
  -> generate summary/card candidate documents
  -> eval/dedupe/dry-run report
  -> enqueue approved redacted derived documents
  -> rag-ingress-queue
  -> RetiredIndexBridge derived datasets
```

Requirements:

- `memory-regeneration-runner` is a producer, not a queue consumer.
- `memory-regeneration-runner` must not write RetiredIndexBridge directly in steady-state.
- `rag-ingress-queue` must support target profiles for all four initial RetiredIndexBridge dataset roles.
- Dataset separation is required. A single RetiredIndexBridge dataset with metadata-only type partitioning is not acceptable for the steady-state memory corpus.
- RetiredIndexBridge Search App may be used for operator review if available; a custom review UI is not required for MVP.
- RetiredIndexBridge Memory/Agent feature is explicitly lower priority and not part of the ingress queue MVP.

## 21. 참고 공식 문서

- Spring Boot system requirements: https://docs.spring.io/spring-boot/system-requirements.html
- Spring Boot virtual threads option: https://docs.spring.io/spring-boot/reference/features/spring-application.html
- NATS Docker JetStream: https://docs.nats.io/running-a-nats-service/nats_docker/jetstream_docker
- NATS JetStream streams: https://docs.nats.io/nats-concepts/jetstream/streams
- NATS JetStream consumers: https://docs.nats.io/nats-concepts/jetstream/consumers
