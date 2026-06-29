# Backend-neutral RAG-ready document ingress contract

> 상태: **contract/adapter groundwork — live ingest 아님.**
> 이 문서는 `rag-ingress-queue`를 RetiredIndexBridge 전용 delivery queue가 아니라 backend-neutral한
> **RAG-ready document ingress bus**로 고정하는 계약이다. RetiredIndexBridge는 첫 backend adapter일 뿐이다.

## 1. 무엇인가 / 무엇이 아닌가

- 이것은 Kafka-like general event bus가 **아니다**. RAG-ready document만 받는 **indexing ingress bus**다.
- queue는 source type을 직접 이해하지 않는다. producer/source adapter가 `text/markdown + metadata`
  형태의 redacted RAG-ready document로 변환한 뒤 enqueue한다.
- queue는 logical `targetProfile`을 보고 backend adapter로 전달한다. 물리 backend 자원(예: RetiredIndexBridge
  dataset id)은 adapter/config 안에만 존재하며 public 표면에 절대 노출되지 않는다.

```text
POST /v1/ingest/enqueue
  -> validate (RAG-ready document request)
  -> idempotency / content-hash dedup
  -> queue storage (NATS JetStream)
  -> worker
  -> RagTargetAdapter (= backend adapter 경계)
       - RetiredIndexBridgeTargetAdapter  (현재 유일 구현)
       - future backend        (BackendKind 추가 + adapter 구현)
  -> status / DLQ
```

## 2. Public enqueue contract (`rag_ingress_enqueue.v1`)

요청은 backend-neutral이다. RetiredIndexBridge dataset id / document id / token은 **요청에 등장하지 않는다.**

| 필드 | 의미 |
|---|---|
| `schemaVersion` | `rag_ingress_enqueue.v1` 고정 |
| `source` | producer 출처 메타(map). `provider`, `project` 필수. `host`는 선택적 redacted/stable host alias다. |
| `payload.kind` | **payload 형식 식별자** — 항상 `redacted_rag_ready_document` (target-neutral). `index_*` / `private_locator` 거부 |
| `payload.redactionVersion` | `redaction.v2` |
| `payload.document` | `{ filename, contentType, body, metadata }` — redacted markdown |
| `contentHash` | `sha256:<64 lowercase hex>` (body와 일치해야 함) |
| `targetProfile` | **logical routing key** (§3). backend 자원 id 아님 |
| `kind` (top-level) | **document 종류** (예: `conversation_chunk`, `session_summary` …) |
| `idempotencyKey` | optional, producer 제공 (§5) |

> 주의: `payload.kind`와 top-level `kind`는 **서로 다른 필드**다. `payload.kind`는 payload 봉투의 형식
> (항상 `redacted_rag_ready_document`), top-level `kind`는 문서 자체의 종류(`conversation_chunk` 등)다.

> 호환성: 기존 enqueue 스키마와 동일하다. 이번 slice는 이 계약을 **변경하지 않는다.**

`source.host`가 제공되면 worker는 document metadata의 `source_host`로 보존할 수 있다.
이 값은 private operational provenance이며 attribution/debug 전용이다. `contentHash`,
`idempotencyKey`, backend natural key, dataset routing, recall authorization의 일부가 아니다.
Producer는 raw private hostname/path/token이 아니라 redacted 또는 stable alias만 보내야 한다.

## 3. Target profile registry — logical profile → backend kind

`TargetProfileRegistry`가 유효 `targetProfile`의 **단일 진실 공급원(SSOT)** 이다.
각 profile은 `BackendKind` + logical `datasetRole`로 매핑된다. **물리 dataset id는 여기에 없다.**

| targetProfile | backendKind | datasetRole (logical) | 물리 dataset id |
|---|---|---|---|
| `index-transcript-memory` | `RETIRED_INDEX_BRIDGE` | `transcript-memory` | private config (`RETIRED_INDEX_BRIDGE_*_DATASET_ID`) |
| `index-session-memory` | `RETIRED_INDEX_BRIDGE` | `session-memory` | private config |
| `index-session-summary` | `RETIRED_INDEX_BRIDGE` | `session-summary` | private config |
| `index-project-memory` | `RETIRED_INDEX_BRIDGE` | `project-memory` | private config |
| `index-task-summary` | `RETIRED_INDEX_BRIDGE` | `task-summary` | private config |
| `index-approved-memory-card` | `RETIRED_INDEX_BRIDGE` | `approved-memory-card` | private config |
| `index-procedural-memory` | `RETIRED_INDEX_BRIDGE` | `procedural-memory` | private config |

- `IngestJobValidator`는 이 registry에 known-profile 판정을 위임한다. 미지의 profile은 enqueue에서 거부된다.
- registry는 `application.yml`의 `rag-ingress.target-profiles`와 **parity 테스트**로 일치가 강제된다
  (`adapter == backendKind.toLowerCase()`, `dataset-role == datasetRole`).
- **명명 주의**: `index-*` 접두는 현 단계의 **임시 adapter-prefix logical key**다. backend-neutral
  완성형에서는 backend 명을 떼고 의미 기반 profile 명으로 이행할 수 있으며, 물리 dataset id는 계속 private.
- follow-up: 전면 `@ConfigurationProperties` 바인딩 + startup fail-fast 게이트(이번 slice는 Spring
  재배선 회귀 위험으로 제외).

## 4. Generic status model

public/job 상태는 backend-neutral 단일 enum `IngestStatus`로 표현한다. backend run state를 직접 요구하지 않는다.

```
ACCEPTED -> QUEUED -> IN_FLIGHT -> INDEXED
                         └─> FAILED (재시도) ─┐
                                              └─> DEAD_LETTER (종단)
```

| IngestStatus | 의미 |
|---|---|
| `ACCEPTED` | enqueue 검증 통과, publish 이전 |
| `QUEUED` | 큐에 durable 보관, worker 대기 (backpressure / 재시도 대기 포함) |
| `IN_FLIGHT` | backend adapter로 전달됨. backend indexing은 비동기, 아직 미확정 |
| `INDEXED` | backend가 indexed 확정 |
| `FAILED` | 전달 시도 실패(비종단, 재시도 가능) |
| `DEAD_LETTER` | 종단 실패(재시도 소진/격리/backend 취소) |

### 4.1 RetiredIndexBridge run-state → IngestStatus (adapter 내부 매핑)

RetiredIndexBridge run state(`DONE/FAIL/RUNNING/...`)는 `RetiredIndexBridgeStatusMapper` 안에서만 다뤄지고 밖으로 새지 않는다.

| RetiredIndexBridge run | IngestStatus |
|---|---|
| `UNSTART` | `QUEUED` |
| `RUNNING` | `IN_FLIGHT` |
| `DONE` | `INDEXED` |
| `FAIL` / `FAILED` | `FAILED` |
| `CANCEL` | `DEAD_LETTER` |
| `null` / `""` / unknown | `FAILED` (fail-closed) |

> `DEAD_LETTER`는 run-state에서 직접 만들지 않는다(`CANCEL` 제외). 재시도 소진에 의한 종단 dead-letter는
> worker의 max-deliver/quarantine 정책이 판정한다(§5).

### 4.2 Worker DeliveryDecision → IngestStatus

| DeliveryDecision | IngestStatus |
|---|---|
| `DELIVERED` | `IN_FLIGHT` |
| `SKIPPED_PRESSURE` / `NO_WORK` | `QUEUED` |
| `RETRY_SCHEDULED` | `FAILED` |
| `QUARANTINE_CANDIDATE` | `DEAD_LETTER` |

## 5. Idempotency / DLQ / retry semantics

RAG document delivery에 필요한 만큼만. Kafka 식 replay/topic/partition으로 넓히지 않는다.

### Idempotency
- `idempotencyKey`가 있으면 `IdempotencyStore`가 key→contentHash를 기억한다.
  - 같은 key + 같은 `contentHash` 재요청 → **충돌 아님**(안전한 replay).
  - 같은 key + 다른 `contentHash` → `409 idempotency_conflict`.
  - key 없음/blank → idempotency 미적용(충돌 없음).
- backend(content_hash) dedup: `RetiredIndexBridgeTargetAdapter`는 동일 content_hash 조각이 이미 dataset에 있으면
  upload를 건너뛰고 delivered로 처리한다(코퍼스 중복 방지).
- `source.host`/`metadata.source_host`는 dedup identity가 아니다. 같은 `contentHash`와
  `idempotencyKey`는 host가 달라도 같은 replay/natural-key로 취급한다.

### Retry / DLQ
- 큐/재전송/dead-letter는 NATS JetStream에 위임한다.
- worker: target pressure가 `OPEN`이 아니면 `nak`(재전송), 전달 실패 시 `nak`로 재시도,
  `deliveryAttempt`가 `MAX_DELIVER(5)`를 넘으면 `ack` 후 **quarantine candidate**(= `DEAD_LETTER`).
- 검증 실패한 큐 payload는 `ack` 후 quarantine(무한 재전송 방지).

## 6. 금지 / 범위 (이번 slice)

- live RetiredIndexBridge upload/parse 금지(contract/test/dry-run 중심).
- enqueue API breaking change 금지. 기존 RetiredIndexBridge delivery 동작 회귀 금지.
- Kafka-like topic/partition/consumer-group 확장 금지.
- output/log에 secret · 물리 dataset id 노출 금지.
- producer repo(`workspace-index-advisor`) 수정 금지.

## 7. Follow-ups (이번 slice 밖)

- `TargetProfileRegistry`의 `@ConfigurationProperties` 바인딩 + startup fail-fast.
- `getStatus`의 live RetiredIndexBridge run-state polling 배선(`RetiredIndexBridgeStatusMapper` 경유).
- `/status`의 multi-backend 집계(현재는 단일 대표 backend 표시).
