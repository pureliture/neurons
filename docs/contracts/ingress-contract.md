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

`TargetProfileRegistry`가 runtime의 유효 `targetProfile` 판정을 소유한다.
각 profile은 `BackendKind` + logical `datasetRole`로 매핑된다. **물리 dataset id는 여기에 없다.**
public-safe machine-readable contract는 `docs/contracts/target-profiles.yaml`이다. 이 artifact는 이 섹션의
logical profile 표를 Java/Python/compose parity tests가 함께 읽을 수 있게 만든 하위 계약이며,
physical dataset id, token, private ops value를 절대 담지 않는다.

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
- `docs/contracts/target-profiles.yaml`은 `TargetProfileRegistry.DEFAULT`, `application.yml`, Python
  `env_profile_dataset_resolver`, `compose.yaml`, `.env.example`의 logical/env-key parity를 검증하는
  public-safe contract artifact다.
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

### CouchDB delivery 실패 분류 (`CouchDBDeliveryBackend.submit`)

legacy/unpinned transcript 경로의 순서는 다음과 같다.

1. 기존 `transcript_session` aggregate를 읽고 `assert_transcript_session_identity`로
   **identity pre-check** (아직 write 없음)
2. 결정론적 id의 `conversation_chunk` 문서 write (`put_if_absent`)
3. `transcript_session` aggregate CAS merge (merge 내부에서 1과 같은 identity 검사를 반복)

실패는 두 lane 중 하나로만 분류한다(fail-closed: 애매하면 uncertain).

| 예외 | lane | 처리 |
|---|---|---|
| `SourceContractViolation` | terminal | 즉시 `payload_integrity_mismatch` evidence → executor가 `quarantined`. `run = source_contract_violation:<reason_code>` |
| `CouchDBError` (네트워크/HTTP) | uncertain | `DeliveryOutcomeUncertain` → `replayable`, `max_attempts`까지 재시도 |
| `SourceStoreConflict` (CAS/동시성 경합) | uncertain | 위와 동일 |
| 그 외 `SourceStoreError` (coverage 미수렴, active control plane 미해결, store 상태 손상) | uncertain | 위와 동일 |
| 예상 못 한 예외 | uncertain | 위와 동일 |

`SourceContractViolation`은 **같은 delivery job을 재시도해도 항상 같은 이유로 실패**하는
검증에서만 raise한다. 판단 기준: 검사가 incoming payload와, 어떤 ingress write 경로도
다시 쓰지 않는 session identity field(`transcript_session`의
`session_id_hash`/`provider`/`project`/`_id`)에만 의존해야 한다. 현재 분류된 reason code:

- `transcript_session_session_id_hash_changed` / `transcript_session_provider_changed` /
  `transcript_session_project_changed` — 기존 aggregate와 incoming envelope의 identity 불일치
  (예: 같은 `session_id_hash` 아래 다른 project를 주장하는 mixed-project payload)
- `transcript_session_document_id_changed` — aggregate 문서 id 불일치
- `aggregate_merge_requires_transcript_session` — aggregate merge에 `transcript_session`이 아닌 문서 전달

반대로 "existing aggregate is not a transcript_session document"(store 상태 손상),
coverage manifest 미수렴, `SourceRevisionResolutionError` 등 **store 상태가 바뀌면 결과가
바뀔 수 있는** 실패는 plain `SourceStoreError`로 남겨 uncertain lane을 유지한다.
`reason_code`는 고정 식별자(`[a-z0-9_]`)이며 payload 값(project 이름 등)을 담지 않는다.

**의도적으로 받아들인 좁은 경합.** unpinned legacy `transcript_session`은 ingress가 아닌
경로(`historical_import`의 `store.put(session_doc)`, 운영자 교정)로 문서 전체가 교체될 수
있어 identity가 엄밀히 불변은 아니다. 그런 교정이 delivery retry 사이(기본 `max_attempts`
3회)에 들어오면 다음 retry가 성공했을 수도 있지만, 이 경우에도 즉시 quarantine하고 교정
이후 payload를 **재적재**로 처리한다. 이 경합을 위해 quarantine을 미루면 진짜
mixed-identity payload를 모두 `max_attempts`까지 사유 없이 replay하게 되기 때문이다.

**orphan chunk 문서.** pre-check(1)가 계약 위반 payload를 chunk write(2) 이전에 거절하므로
일반적인 경우 store에는 아무것도 남지 않는다. pre-check 통과 후 merge(3) 사이에 aggregate가
동시 교체되는 경합에서만 이미 기록된 chunk가 orphan으로 남을 수 있다. 그 chunk도 결정론적
`_id`를 가지므로 같은 payload를 재처리해도 두 번째 chunk 문서가 생기지 않는다(idempotent
duplicate). 이미 존재하는 orphan chunk의 정리(GC)는 이 계약의 범위 밖이다.

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
