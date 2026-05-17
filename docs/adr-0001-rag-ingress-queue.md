# ADR-0001: 범용 RAG ingress queue와 RAGFlow target adapter

Status: Proposed
Date: 2026-05-17
Deciders: local operator

## Context

Local PC와 Mac mini에서 transcript/session/document ingest 요청이 burst 형태로 발생할 수 있다. 현재는 RAGFlow REST를 직접 호출하는 흐름이 있어 downstream parser backlog를 producer가 충분히 통제하지 못한다.

초기 target은 RAGFlow지만, 나중에 target RAG가 다른 솔루션으로 바뀔 수 있다. 따라서 서버를 `ragflow-ingress-queue`로 잠그지 않고 `rag-ingress-queue`라는 범용 ingress queue로 설계한다.

`workspace-ragflow-advisor`의 최신 구조에서는 RAGFlow를 장기기억 본문과 검색 대상이 모이는 LLM Wiki로 둔다. Mac mini의 기존 `transcript-capture`, `transcript-worker`, `transcript-packer`는 `session-compactor` 역할로 재분류한다. Ubuntu의 `memory-regeneration-runner`는 RAGFlow redacted corpus를 읽고 derived document를 다시 enqueue하는 batch producer다.

## Decision

`rag-ingress-queue`를 Java 25 + Spring Boot 기반의 별도 Docker Compose project로 만든다. Queue engine은 NATS JetStream을 사용한다. RAGFlow는 `RagTargetAdapter`의 첫 구현체인 `RAGFlowAdapter`로 격리한다.

Core server는 다음 책임만 가진다.

- enqueue API
- message validation
- JetStream publish
- pull consumer worker lifecycle
- ack/retry/dead-letter policy 연결
- target adapter 호출
- redacted status/postcheck

Target-specific 구현은 adapter에 둔다.

Mac mini/private transcript source의 경우 source locator 해석, transcript parsing, redaction, packing은 producer-side boundary에 둔다. `rag-ingress-queue`는 이미 redacted 되었고 target delivery가 가능한 document payload/ref, content hash, metadata, target profile만 받는다. Ubuntu-side worker가 Mac-only private locator를 직접 해석하는 설계는 채택하지 않는다.

RAGFlow dataset partition은 target profile로 표현한다. 초기 RAGFlow profiles는 `ragflow-transcript-memory`, `ragflow-session-summary`, `ragflow-task-summary`, `ragflow-approved-memory-card`다. 단일 RAGFlow dataset에 metadata type만으로 모든 기억 데이터를 넣는 설계는 채택하지 않는다.

## Options Considered

### Option A: 기존 worker에 backlog throttle만 추가

| Dimension | Assessment |
|---|---|
| Complexity | Low |
| Runtime change | Low |
| Modularity | Low |
| Future target swap | Weak |

Pros: 가장 빠르게 적용 가능하다.
Cons: 범용 ingress queue가 아니며 direct write path가 남기 쉽다.

### Option B: `rag-ingress-queue` + NATS JetStream + adapter contract

| Dimension | Assessment |
|---|---|
| Complexity | Medium |
| Runtime change | Medium |
| Modularity | High |
| Future target swap | Strong |

Pros: queue 기능을 직접 구현하지 않고, write path를 단일화하며, RAGFlow lock-in을 피한다.
Cons: 별도 compose project, NATS 운영 postcheck, Spring Boot service packaging이 필요하다.

### Option C: Redis/RabbitMQ/Celery 기반 queue

| Dimension | Assessment |
|---|---|
| Complexity | Medium to High |
| Runtime change | Medium to High |
| Modularity | Medium |
| Future target swap | Medium |

Pros: 널리 알려진 queue ecosystem이다.
Cons: 기존 RAGFlow Redis와 겹칠 위험이 있고, 현재 목적에는 runtime surface가 커질 수 있다.

## Consequences

- RAGFlow compose project를 수정하지 않아도 된다.
- Local PC producer는 target RAG REST API를 몰라도 된다.
- NATS JetStream이 queue persistence, ack, redelivery, dead-letter에 가까운 책임을 맡는다.
- Spring Boot service는 얇은 gateway/worker로 유지해야 한다.
- RAGFlow-specific status와 IDs는 adapter-private metadata로 제한해야 한다.
- Mac-only private source access는 producer-side에 남고, Ubuntu worker는 provider transcript path를 읽지 않는다.
- RAGFlow document status table authorization pass 전 recall/promote는 계속 금지된다.
- `rag-ingress-queue`는 session/task summary 생성, memory card 생성, recall routing, lifecycle/GC 판단을 하지 않는다.
- `memory-regeneration-runner`는 queue consumer가 아니라 queue producer다.
- External document status table은 queue project가 직접 소유하지 않는다. Queue는 redacted job/status snapshot을 제공하고 external reconcile client가 상태표를 갱신한다.

## Implementation notes

- Java 25 runtime을 기준으로 한다.
- Spring Boot 4.x를 사용한다.
- `spring.threads.virtual.enabled=true`를 기본 설정으로 둔다.
- `ingress-api`와 `ingress-worker`는 같은 image, 다른 command로 구성할 수 있다.
- NATS는 `nats -js -sd /data` 형태로 JetStream을 활성화한다.
- Worker는 pull consumer + explicit ack 모델을 사용한다.
- `IngestJob`은 `redacted_rag_ready_document` 또는 worker가 읽을 수 있는 `redacted_document_ref`를 담는다. `private_locator`는 delivery payload가 아니다.
- 여기서 `Ledger`는 별도 장기기억 저장소가 아니라 RAGFlow document id/hash/state/provenance/authorization을 기록하는 external local 문서 상태표의 기존 코드 이름을 뜻한다.
- `rag-ingress-queue` worker는 Mac mini의 `ledger.py`/SQLite를 직접 갱신하지 않는다.
- RAGFlow target profile은 dataset role을 선택한다. Metadata `result_type`은 trace/filter 용도일 뿐 primary partition이 아니다.

## Done criteria

1. Direct RAGFlow write path가 producer에서 제거된다.
2. NATS JetStream stream/consumer가 redacted postcheck에 보인다.
3. RAGFlow delivery는 `RAGFlowAdapter`를 통해서만 발생한다.
4. Target pressure가 `THROTTLED` 또는 `CLOSED`일 때 worker가 delivery를 멈춘다.
5. Document status authorization pass 전 indexed memory가 recall/promote되지 않는다.
6. Derived memory documents도 direct RAGFlow write가 아니라 `/enqueue`를 통해 저장된다.
