# rag-ingress-queue

`rag-ingress-queue`는 local PC와 Mac mini에서 생성되는 redacted RAG-ready transcript/session/document ingest 요청을 받아, downstream RAG target이 감당 가능한 속도로 전달하는 범용 ingress queue 서버다.

첫 번째 target adapter는 `RAGFlowAdapter`다. 단, core server는 RAGFlow에 종속되지 않는다. 나중에 다른 RAG 솔루션을 붙일 수 있도록 `RagTargetAdapter` contract를 기준으로 설계한다.

현재 `workspace-ragflow-advisor` 구조에서는 RAGFlow가 LLM Wiki이자 장기기억 본문 저장소다. `rag-ingress-queue`는 그 뒤의 지식 서버가 아니라 RAGFlow 앞의 write gateway다.

## 고정된 기술 선택

- Language/runtime: `Java 25`
- Framework: `Spring Boot 4.x`
- Concurrency: Java virtual threads
- Queue engine: `NATS JetStream`
- Deployment: RAGFlow와 분리된 별도 Docker Compose project
- Write path: producer -> `rag-ingress-queue` -> target adapter -> RAG target
- Producer payload: Mac-only private locator가 아니라 redacted RAG-ready document payload/ref
- Read/tool plane: MCP는 bulk ingest write path가 아니라 read/tool plane으로 분리

## 산출물

- [요구사항 문서](docs/requirements.md)
- [ADR-0001: rag-ingress-queue architecture](docs/adr-0001-rag-ingress-queue.md)
- [시각 리뷰 HTML](docs/rag-ingress-queue-architecture.html)

## 핵심 원칙

1. Producer는 RAG target을 직접 write하지 않는다.
2. Mac-only source locator 해석, transcript parsing, redaction, packing은 producer-side boundary에 둔다.
3. Queue는 redacted RAG-ready payload/ref만 받고 delivery, backpressure, retry, status polling을 담당한다.
4. Queue, ack, retry, redelivery, dead-letter는 `NATS JetStream`에 맡긴다.
5. Core server는 RAG target을 모른다. target-specific 처리는 adapter에 격리한다.
6. `RAGFlowAdapter`는 첫 adapter일 뿐이다.
7. Indexed 상태가 되어도 external RAGFlow document status table의 authorization pass 전에는 recall/promote에 사용하지 않는다. 여기서 `Ledger`는 별도 기억 저장소가 아니라 기존 `ledger.py` 상태표 이름이다.
8. `rag-ingress-queue`는 session summary, task summary, memory card 생성이나 recall routing을 하지 않는다.
9. RAGFlow memory corpus는 dataset-first partitioning을 사용한다.

## 초기 서비스 구성

```text
rag-ingress-queue compose project
  nats-jetstream
  ingress-api
  ingress-worker

RAGFlow compose project
  기존 RAGFlow stack, 수정하지 않음
```

## workspace-ragflow-advisor 적용 구조

```text
Mac mini session-compactor
  기존 transcript-capture / transcript-worker / transcript-packer
  -> redacted conversation_chunk enqueue
  -> rag-ingress-queue
  -> RAGFlow transcript-memory

Ubuntu memory-regeneration-runner
  -> RAGFlow transcript-memory read
  -> session/task/card derived documents
  -> rag-ingress-queue
  -> RAGFlow session-summary / task-summary / approved-memory-card
```

`memory-regeneration-runner`는 queue consumer가 아니라 producer다. `rag-ingress-queue` 뒤에는 target adapter delivery만 둔다.

초기 RAGFlow target profiles:

| targetProfile | RAGFlow dataset |
|---|---|
| `ragflow-transcript-memory` | `transcript-memory` |
| `ragflow-session-summary` | `session-summary` |
| `ragflow-task-summary` | `task-summary` |
| `ragflow-approved-memory-card` | `approved-memory-card` |

## 참고 공식 문서

- [Spring Boot system requirements](https://docs.spring.io/spring-boot/system-requirements.html)
- [Spring Boot virtual threads option](https://docs.spring.io/spring-boot/reference/features/spring-application.html)
- [NATS JetStream Docker](https://docs.nats.io/running-a-nats-service/nats_docker/jetstream_docker)
- [NATS JetStream streams](https://docs.nats.io/nats-concepts/jetstream/streams)
- [NATS JetStream consumers](https://docs.nats.io/nats-concepts/jetstream/consumers)
