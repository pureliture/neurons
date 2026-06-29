# rag-ingress-queue MVP Spec

Status: Reviewed draft
Date: 2026-05-17
Goal owner: local operator
Project path: `<repo>`

## Goal

`rag-ingress-queue`의 첫 구현 단위는 RetiredIndexBridge 앞단에 위치하는 범용 write gateway다. Local PC, Mac mini `session-compactor`, Ubuntu `memory-regeneration-runner`가 redacted RAG-ready document ingest 요청을 `POST /v1/ingest/enqueue`로 넣으면, server는 NATS JetStream에 publish하고 worker는 target pressure가 허용될 때만 `RagTargetAdapter`를 통해 downstream RAG target으로 전달한다.

MVP는 RetiredIndexBridge를 첫 target adapter로 포함하되 core server가 RetiredIndexBridge-specific field, raw ID, parser status, dataset credential에 결합되지 않도록 한다.

## Source Documents

- `README.md`
- `docs/requirements.md`
- `docs/adr-0001-rag-ingress-queue.md`
- `docs/rag-ingress-queue-architecture.html`
- Spring Boot 공식 문서: `https://docs.spring.io/spring-boot/system-requirements.html`
- Spring Boot virtual threads 공식 문서: `https://docs.spring.io/spring-boot/reference/features/spring-application.html`
- NATS JetStream streams 공식 문서: `https://docs.nats.io/nats-concepts/jetstream/streams`
- NATS JetStream consumers 공식 문서: `https://docs.nats.io/nats-concepts/jetstream/consumers`

Documentation lookup note:

- Context7 was attempted first on 2026-05-17, but the tool reported monthly quota exhaustion.
- Official Spring/NATS docs above were used as fallback primary sources.
- Implementation must record the consulted version/date in build or runbook docs whenever dependency pins change.

## Non-Goals

- RetiredIndexBridge Docker Compose project 수정
- RetiredIndexBridge 내부 Redis, DB, volume 직접 조작
- raw transcript body, raw private path, token, raw dataset_id, raw document_id 노출
- MCP를 bulk ingest write path로 사용
- Mac-only private locator를 Ubuntu worker가 직접 해석
- queue worker가 Mac mini `ledger.py`/SQLite를 직접 갱신
- session summary, task summary, memory card 생성
- recall/promote authorization 판단
- RetiredIndexBridge Memory/Agent feature 또는 mirror 운영

## Architecture Decisions

| Decision | Spec Constraint | Skill/Plugin Gate |
|---|---|---|
| Java 25 + Spring Boot 4.x | Spring Boot 4.0.6 공식 요구사항상 Java 25는 지원 범위 안에 있다. | Context7 first, official docs fallback, `architecture` |
| Virtual threads | `spring.threads.virtual.enabled=true`를 기본 설정으로 두고 worker process에는 `spring.main.keep-alive=true`를 포함한다. | Context7 first, official docs fallback |
| Queue engine | NATS JetStream `WorkQueuePolicy`, durable pull consumer, explicit ack를 사용한다. | Context7 first, `system-design` |
| Adapter boundary | Core는 `RagTargetAdapter` contract만 의존하고 `RetiredIndexBridgeAdapter`를 첫 구현체로 둔다. | `architecture`, `tech-debt` |
| Dataset routing | `targetProfile`은 configured target profile ID다. 초기 profile은 RetiredIndexBridge dataset role에 매핑되지만 raw dataset ID와 adapter-private mapping은 public API에 나오지 않는다. Metadata-only single dataset partitioning은 금지한다. | `system-design` |
| Status semantics | `queued`, `delivered`, `indexed`, `authorized`, `recall/promote eligible`을 별도 상태로 유지한다. | `testing-strategy`, `deploy-checklist` |

## Component Scope

### ingress-api

Responsibilities:

- `POST /v1/ingest/enqueue`
- `GET /healthz`
- `GET /status`
- request validation
- idempotency key 생성 또는 검증
- JetStream publish와 publish ack 확인
- redacted response 생성

Non-responsibilities:

- RAG target 직접 upload
- target backlog polling
- recall/promote 결정
- raw payload persistence

### ingress-worker

Responsibilities:

- durable pull consumer를 통한 bounded batch fetch
- target pressure check
- adapter delivery
- target status polling 또는 deferred recheck
- success ack, retry/nak, dead-letter 후보 처리
- redacted delivery/status snapshot 생성
- external reconcile client가 사용할 수 있는 redacted snapshot 제공

Non-responsibilities:

- producer-side transcript parse/redaction/packing
- Mac-only source locator 해석
- `ledger.py`/SQLite 직접 갱신
- summary/card generation
- authorization 또는 recall/promote eligibility 판단

### NATS JetStream

Stream:

```text
name: RAG_INGRESS_QUEUE
subjects:
  - rag.ingress.transcript
  - rag.ingress.document
retention: WorkQueuePolicy
storage: file
limits:
  max_bytes: configurable
  max_age: configurable
  discard: new for hard safety limits unless explicitly changed
access:
  bind: compose-network/local-only by default
  auth: required when exposed outside local compose network
```

Durable pull consumer:

```text
name: rag_target_delivery_worker
ack_policy: explicit
max_deliver: 5
ack_wait: configurable
max_ack_pending: bounded
```

Dead-letter policy:

- Dead-letter는 JetStream이 자동으로 별도 DLQ에 옮겨준다고 가정하지 않는다.
- MVP는 `MaxDeliver`/advisory/redelivery 상태를 감지해 project-defined terminal/quarantine policy와 redacted `deadLetter` count로 노출한다.
- DLQ/quarantine records must pass the same redaction scanner as normal status output.

### RagTargetAdapter

Initial Java contract:

```java
public interface RagTargetAdapter {
    TargetPressure checkPressure(TargetProfile profile);
    DeliveryResult deliver(IngestJob job, TargetProfile profile);
    IndexingStatus getStatus(TargetDocumentRef ref, TargetProfile profile);
    TargetStatusSnapshot snapshot(TargetDocumentRef ref, TargetProfile profile);
}
```

Generic target states:

- `ACCEPTED`
- `DELIVERED`
- `INDEXING`
- `INDEXED`
- `FAILED`
- `THROTTLED`

`AUTHORIZED` is not a RAG target indexing state. Authorization and `recall/promote eligible` are external document status table or reconcile client states. Queue worker and adapter code must not mutate that table directly.

## API Contract

### `POST /v1/ingest/enqueue`

Accepted request:

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
  "contentHash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "targetProfile": "index-transcript-memory",
  "kind": "conversation_chunk"
}
```

Response:

```json
{
  "accepted": true,
  "jobId": "redacted-stable-id",
  "status": "queued"
}
```

Payload contract:

- MVP enqueue implementation prioritizes inline `redacted_rag_ready_document`.
- Core domain model must preserve a future `redacted_document_ref` extension point.
- Any future ref must point only to worker-readable, already redacted, target-ready blob storage.
- Mac-only `private_locator` is not a valid steady-state queue payload.
- Public API and queue DTO payload kinds must not use `index_*` names.

Validation requirements:

- `payload.kind` must be `redacted_rag_ready_document` for inline MVP.
- `payload.redactionVersion` must equal `redaction.v2`.
- `targetProfile` must be one of the configured profile IDs.
- `contentHash` must match `sha256:<64 lowercase hex chars>` and represent the canonical redacted body digest. Logs and responses may display a hash prefix only.
- `payload.document.body` must pass producer redaction proof, allowed frontmatter schema, denylist scanner, and size limits before enqueue.
- payloads containing `private_locator`, raw transcript path-like fields, `dataset_id`, `document_id`, `token`, bearer-looking values, or raw target IDs must be rejected at ingress.
- adapter-private target references must be opaque handles or hashes outside the adapter package.
- API response must not include raw private path, raw dataset_id, raw document_id, token, or raw transcript body.

### `GET /healthz`

MVP response:

```json
{
  "status": "ok",
  "component": "ingress-api"
}
```

### `GET /status`

MVP response:

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
    "pressure": "OPEN"
  }
}
```

`GET /status` is operator-facing and must remain redacted.

## Target Profiles

| targetProfile | RetiredIndexBridge dataset role | document kind |
|---|---|---|
| `index-transcript-memory` | `transcript-memory` | `conversation_chunk` |
| `index-session-summary` | `session-summary` | `session_summary` |
| `index-task-summary` | `task-summary` | `task_summary` |
| `index-approved-memory-card` | `approved-memory-card` | `approved_memory_card` |

Raw dataset IDs are adapter-private and must not appear in generic API output, logs, docs examples, or postcheck summaries.

MCP remains a read/tool plane. If a future MCP ingest tool is added, it must delegate to `rag-ingress-queue /enqueue` and must not call RetiredIndexBridge REST directly.

## Backpressure Behavior

RetiredIndexBridge adapter pressure inputs:

- RetiredIndexBridge health
- `UNSTART` document count
- `RUNNING` document count
- stale indexing age/count
- recent upload/parse error rate

Generic pressure behavior:

| Pressure | Worker Behavior |
|---|---|
| `OPEN` | fetch bounded batch and deliver |
| `THROTTLED` | fail-closed for MVP: do not create new upload/parse requests that increase target backlog |
| `CLOSED` | stop delivery and delay/nak without flooding target |

MVP default is fail-closed. New delivery occurs only when pressure is `OPEN`. Future `THROTTLED` trickle delivery requires a separate setting, tests, and deploy gate.

## Status and Authorization Boundaries

- JetStream publish ack means queue accepted the job, not RetiredIndexBridge indexed it.
- JetStream consumer ack means worker finished the queue delivery unit, not recall authorization.
- RetiredIndexBridge `DONE` can map to generic `INDEXED` candidate, not authorization.
- External document status table authorization pass is required before indexed transcript memory can be used for recall/promote.
- `Ledger` in this project context means the existing external document status table, not a separate memory store.
- Queue worker may expose a redacted target status snapshot for an external reconcile client, but must not directly mutate external document status or authorization state.

## Testing Strategy

Use TDD for every behavior-bearing implementation task.

| Area | Test Type | Required Cases |
|---|---|---|
| Request validation | Unit + Web MVC tests | valid enqueue, missing source, invalid targetProfile, private locator rejection, raw ID/token rejection |
| Redaction guard | Unit + Web MVC + log capture tests | bearer token rejection, raw dataset_id/document_id rejection, private path detection, raw transcript fixture rejection, postcheck/status output scan |
| Idempotency/job ID | Unit tests | stable ID from canonical `contentHash`/profile/kind, explicit idempotency key accepted |
| JetStream publish | Fake publisher contract first, Testcontainers NATS integration next | publish subject mapping, publish ack required, publish failure maps to 503/queued false, durable pull consumer, explicit ack, nak/retry, max_deliver/quarantine candidate |
| Worker pressure gate | Unit tests | `OPEN` delivers, `THROTTLED` does not create new delivery, `CLOSED` does not deliver |
| Adapter boundary | Unit tests | core depends on `RagTargetAdapter`, RetiredIndexBridge-specific IDs stay private |
| Status/authorization split | Unit tests | `INDEXED` target status never becomes `AUTHORIZED`; external authorization state is separate |
| Status endpoint | Unit/Web tests | queue counts included, target pressure included, secrets absent |
| Compose/postcheck | Smoke tests | NATS JetStream starts, API health responds, stream/consumer visible, redacted output scan passes |

Coverage target for MVP:

- Domain validation and redaction guard: high confidence with focused unit tests
- API layer: representative Web MVC tests
- NATS/RetiredIndexBridge: fake adapters first, live/Testcontainers smoke after core behavior is green
- Live RetiredIndexBridge smoke is approval-gated and must not run without a separate explicit approval.
- Live RetiredIndexBridge smoke, when approved, proves sanitized upload/status polling/redacted output only; it does not prove external authorization readiness.

TDD evidence required per implementation task:

- RED: command, failing test name, and expected failure reason.
- GREEN: same command passing after minimal implementation.
- REFACTOR/REGRESSION: broader suite command and result.
- Runtime evidence: command, timeout, abort criteria, expected evidence path, and what the evidence does not prove.

## Deploy Checklist

Pre-deploy:

- [ ] All unit and Web MVC tests pass.
- [ ] Testcontainers NATS integration tests pass or are explicitly marked blocked with reason.
- [ ] Build succeeds with Java 25-compatible toolchain.
- [ ] `application.yml` enables `spring.threads.virtual.enabled=true`.
- [ ] Worker runtime sets `spring.main.keep-alive=true`.
- [ ] No raw token, raw dataset_id, raw document_id, private path, or transcript body appears in examples, logs, API responses, or postcheck output.
- [ ] NATS stream and durable pull consumer definitions are documented and reproducible.
- [ ] JetStream limits, local bind/auth posture, and terminal quarantine policy are documented.
- [ ] RetiredIndexBridge compose project is not modified.
- [ ] Dependency pins and docs consulted date are recorded in implementation/runbook docs.

Deploy:

- [ ] Start separate `rag-ingress-queue` Docker Compose project.
- [ ] Verify `nats-jetstream` health.
- [ ] Verify `ingress-api /healthz`.
- [ ] Publish one redacted sample enqueue request.
- [ ] Verify JetStream pending/consumer counts.
- [ ] Verify worker does not deliver when target pressure is `CLOSED`.
- [ ] Verify worker delivers only through `RagTargetAdapter` when pressure is `OPEN`.

Post-deploy:

- [ ] Confirm dead-letter count is zero or explained.
- [ ] Confirm target pressure is visible and redacted.
- [ ] Confirm indexed count and authorization count remain separate.
- [ ] Confirm external reconcile path, not worker direct mutation, owns document status table updates.
- [ ] Store local evidence under a non-secret local artifact path such as `build/reports/rag-ingress-queue/`.

Rollback triggers:

- API starts returning unredacted sensitive fields.
- Worker writes RetiredIndexBridge directly outside `RagTargetAdapter`.
- `CLOSED` target pressure still produces delivery attempts.
- `THROTTLED` target pressure still produces new upload/parse requests.
- NATS redelivery/dead-letter count rises without bounded retry behavior or quarantine explanation.
- RetiredIndexBridge compose project or volumes are changed by this project.

Rollback owner/procedure:

- Owner: local operator.
- Stop worker first to halt downstream delivery.
- Keep NATS data intact until queue state is inspected.
- Restore previous API/worker image or config.
- Verify RetiredIndexBridge compose project, volumes, and direct write paths were not modified.

## Tech Debt Guardrails

| Debt Type | Guardrail |
|---|---|
| Architecture debt | Keep core interfaces target-neutral; all RetiredIndexBridge-specific mapping stays in adapter package. |
| Code debt | Keep validation, redaction, queue publish, worker delivery, and target adapter responsibilities in separate classes. |
| Test debt | Require RED/GREEN evidence for validation, pressure, adapter privacy, and status endpoint behavior. |
| Dependency debt | Use Spring Boot managed dependency versions where possible; document unmanaged NATS client version, docs consulted date, and upgrade/compatibility smoke command. |
| Documentation debt | Update README/runbook/plan when commands, ports, status fields, or compose service names change. |
| Infrastructure debt | Compose project must remain separate from RetiredIndexBridge; postcheck must prove service, stream, consumer, and pressure status. |

## Phase and Skill Matrix

| Phase | Output | Required Skill/Plugin |
|---|---|---|
| 0. Goal and boundary check | `/goal`, git branch/worktree boundary, source document inventory | `superpowers:using-git-worktrees`, memory quick pass, RTK policy |
| 1. Spec draft | This spec document | `architecture`, `system-design`, `testing-strategy`, `documentation`, `deploy-checklist`, `tech-debt` |
| 2. Spec review | At least 3 reviewer reports: architecture, test/deploy, security/tech debt | Subagents with `model: gpt-5.5`, `reasoning_effort: high` |
| 3. Implementation plan | Superpowers plan under `docs/superpowers/plans/` | `superpowers:writing-plans`, `superpowers:test-driven-development`, Context7 first then official docs fallback |
| 4. Plan review | At least 3 reviewer reports: implementation sequencing, test strategy, tech debt/security | Subagents with `model: gpt-5.5`, `reasoning_effort: high` |
| 5. Implementation | TDD tasks, one implementer at a time, two-stage review after each task | `superpowers:subagent-driven-development`, `superpowers:requesting-code-review` |
| 6. Final verification | Build/test/smoke evidence, docs/runbook update, exact staged files | `superpowers:verification-before-completion`, `deploy-checklist` |

## Acceptance Criteria

1. `POST /v1/ingest/enqueue` accepts valid redacted RAG-ready documents and publishes to JetStream only after validation.
2. Invalid, `index_*` payload kind, raw target ID, raw token, raw transcript, or private-locator payloads are rejected without leaking sensitive data.
3. Worker uses a durable pull consumer and checks target pressure before delivery.
4. `RagTargetAdapter` is the only target delivery boundary visible to core worker code.
5. RetiredIndexBridge-specific raw values are adapter-private.
6. `/healthz` and `/status` return redacted operator-facing data.
7. `queued`, `delivered`, `indexed`, external `authorized`, and `recall/promote eligible` are not collapsed into one state.
8. Separate Docker Compose project can start NATS JetStream, API, and worker without modifying RetiredIndexBridge compose.
9. Tests cover validation, redaction, idempotency, pressure, adapter privacy, and status output.
10. README and operator runbook document quick start, config, smoke test, postcheck, rollback triggers, and known non-goals.
11. MVP fail-closed behavior is proven: only `OPEN` target pressure creates new delivery.
12. JetStream persistence, retry, max-deliver, and project-defined quarantine/DLQ policy have redacted evidence.
