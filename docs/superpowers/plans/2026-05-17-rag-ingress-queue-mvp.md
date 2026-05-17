# rag-ingress-queue MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** Implementation in progress; JVM build/offline postcheck verified, Ubuntu compose runtime smoke verified through publish ack, live RAGFlow gate pending

**Goal:** Java 25 + Spring Boot 4.x 기반 `rag-ingress-queue` MVP를 만들어 redacted RAG-ready document enqueue, NATS JetStream publish, worker pressure gate, target adapter boundary, redacted status/postcheck를 검증 가능하게 구현한다.

**Architecture:** `ingress-api`는 validation과 JetStream publish만 담당하고, `ingress-worker`는 durable pull consumer와 `RagTargetAdapter` 호출만 담당한다. RAGFlow-specific dataset/document ID, parser status, credential은 `RAGFlowAdapter` 내부와 adapter-private opaque handle로 격리한다.

**Tech Stack:** Corretto 25, Spring Boot 4.x, Gradle, NATS JetStream, JUnit 5, Spring Boot Test, fake-port unit tests, Docker Compose runtime smoke, structured JSON logging. Testcontainers-backed NATS integration remains deferred until Docker runtime is available.

---

## Current Baseline

- Branch at plan creation: `plan/rag-ingress-queue-mvp-2026-05-17`
- Existing implementation files: none
- Existing docs: `README.md`, `docs/requirements.md`, `docs/adr-0001-rag-ingress-queue.md`, `docs/rag-ingress-queue-architecture.html`
- Local runtime check on 2026-05-17:
  - `java -version`: available after Corretto 25 installation
  - `gradle -version`: available after Gradle installation
  - `docker --version`: available, Docker 29.3.0
  - `docker compose version`: blocked, compose plugin unavailable

This means local build/test verification can run with Corretto 25 and Gradle. Runtime compose verification remains blocked until Docker daemon and Docker Compose are available.

Implementation note from 2026-05-17: the initial Gradle/Spring Boot skeleton, validation/API/worker tests, JetStream publish ack publisher, durable pull consumer wiring, fail-closed RAGFlow adapter skeleton, compose file, and offline postcheck are implemented locally. Ubuntu runtime smoke on `ops-host` verified Docker/Compose startup, API health, JetStream stream/consumer creation, and enqueue publish ack `RAG_INGRESS_QUEUE:1`. Live RAGFlow delivery remains a separate approval gate.

## Progress Visibility Rules

Use milestone-based updates only:

- `Implementation: [░░░░░░░░░░] 5% — verified, 범위/금지선/현재 상태 확인`
- `Implementation: [██░░░░░░░░] 20% — verified, RED tests 작성 완료`
- `Implementation: [██████░░░░] 60% — GREEN 구현 완료, full verification 대기`
- `Runtime verification: [███░░░░░░░] 30% — limited smoke 완료, 핵심 endpoint 미검증`

Status labels:

- `investigating`: 원인/범위 확인 중
- `blocked`: 필요한 runtime, credential, approval, dependency가 없음
- `needs-review`: subagent review 또는 human approval 대기
- `verified`: fresh command evidence 있음

Milestones:

| Milestone | Implementation | Runtime verification |
|---|---:|---:|
| Scope/current state confirmed | 5% | 0% |
| Plan and RED tests | 10-20% | 0% |
| GREEN implementation | 30-60% | 0-20% |
| Focused/full verification | 70-80% | 30-80% |
| Subagent review and P0/P1 fixed | 90% | 80-90% |
| Docs/runbook/plan aligned | 95% | 90-95% |
| Exact stage/commit and final report | 100% | 100% only with fresh evidence |

## Phase Plugin Matrix

| Phase | Purpose | Skill/Plugin |
|---|---|---|
| 0. Worktree and toolchain gate | avoid main branch implementation, confirm Corretto 25/Gradle/Docker availability | `superpowers:using-git-worktrees`, RTK policy |
| 1. Documentation/source refresh | keep README/requirements/ADR/spec aligned | `documentation`, `architecture`, `system-design` |
| 2. RED test design | define failing tests before production code | `testing-strategy`, `superpowers:test-driven-development` |
| 3. Core implementation | domain, validation, API, queue, worker, adapter | `superpowers:subagent-driven-development` |
| 4. Dependency/API docs lookup | Spring Boot/NATS syntax and compatibility checks | Context7 first; if quota exhausted, official docs fallback |
| 5. Code review | spec compliance first, code quality second | `superpowers:requesting-code-review`, code-simplifier-style review |
| 6. Deploy gate | compose smoke, postcheck, rollback criteria | `deploy-checklist` |
| 7. Tech debt pass | keep boundaries small and avoid RAGFlow lock-in | `tech-debt` |
| 8. Completion gate | evidence before completion claim | `superpowers:verification-before-completion` |

## Subagent Handoff Packet

Every implementation subagent receives exactly one task packet with:

- Task name and purpose.
- Files it may create or modify.
- RED command, failing test class/method names, expected failure text.
- GREEN command and expected pass evidence.
- Runtime command, timeout, abort criteria, and evidence path when runtime is involved.
- Spec references: this plan plus `docs/superpowers/specs/2026-05-17-rag-ingress-queue-mvp-spec.md`.
- Status reporting format: `DONE`, `DONE_WITH_CONCERNS`, `NEEDS_CONTEXT`, or `BLOCKED`.

Model policy:

- Implementation subagents: `model: gpt-5.5`, `reasoning_effort: high`.
- Simple coding helper subagents that do not own implementation may use `model: gpt-5.3-codex-spark`.
- Review subagents: `model: gpt-5.5`, `reasoning_effort: high`.

Evidence format for every task:

```text
RED:
  command:
  failing test:
  expected failure:
GREEN:
  command:
  expected pass:
REGRESSION:
  command:
  expected pass:
RUNTIME:
  command:
  timeout:
  abort criteria:
  evidence path:
  does not prove:
```

## Shared Safety Contracts

### Redaction denylist

Create one shared denylist source and reuse it in Java tests, `scripts/postcheck.sh`, and final scans.

File:

```text
scripts/redaction-denylist.txt
```

Initial patterns:

```text
Bearer\s+
\bapi[_-]?key\b
\btoken\b
\bdataset_id\b
\bdocument_id\b
/Users/[^\s]+
private_locator
raw_transcript
```

### Safe logging

- Domain objects containing document body must not rely on Java record default `toString()`.
- Use `SafeJobSummary` for logs, exceptions, postcheck, and status output.
- Log capture tests must assert absence of body text, `/Users/`, `dataset_id`, `document_id`, `token`, and `Bearer`.

### Payload envelope

Public DTOs use a target-neutral versioned envelope:

```json
{
  "schemaVersion": "rag_ingress_enqueue.v1",
  "payload": {
    "kind": "redacted_rag_ready_document"
  }
}
```

`redacted_document_ref` is reserved in the contract and tests as a disabled extension point. MVP accepts inline `redacted_rag_ready_document`; `redacted_document_ref` returns a clear 422 until worker-readable blob storage is configured.

## File Structure

Create:

- `settings.gradle`: Gradle project name.
- `build.gradle`: Gradle build, Spring Boot 4.x, Java 25, test dependencies.
- `src/main/java/com/local/ragingressqueue/RagIngressQueueApplication.java`: Spring Boot entrypoint.
- `src/main/java/com/local/ragingressqueue/api/IngressController.java`: `/v1/ingest/enqueue`, `/healthz`, `/status`.
- `src/main/java/com/local/ragingressqueue/api/dto/*.java`: API request/response DTOs.
- `src/main/java/com/local/ragingressqueue/core/*.java`: target-neutral domain records and enums.
- `src/main/java/com/local/ragingressqueue/core/validation/*.java`: request validation and redaction guard.
- `src/main/java/com/local/ragingressqueue/core/SafeJobSummary.java`: redacted log/status summary only.
- `src/main/java/com/local/ragingressqueue/queue/*.java`: publish/fetch ports plus NATS JetStream implementation.
- `src/main/java/com/local/ragingressqueue/queue/NatsJetStreamProvisioner.java`: idempotent stream/consumer creation and drift check.
- `src/main/java/com/local/ragingressqueue/worker/*.java`: worker loop, pressure gate, delivery orchestration.
- `src/main/java/com/local/ragingressqueue/target/*.java`: `RagTargetAdapter` contract and target status model.
- `src/main/java/com/local/ragingressqueue/target/ragflow/*.java`: first RAGFlow adapter skeleton with adapter-private opaque references.
- `src/main/resources/application.yml`: virtual threads, actuator, queue/profile config.
- `src/test/java/com/local/ragingressqueue/**`: unit, Web MVC, fake adapter/fake gateway, compose contract tests; Testcontainers integration is deferred until Docker is available.
- `compose.yaml`: separate `rag-ingress-queue` services.
- `scripts/redaction-denylist.txt`: shared forbidden pattern source.
- `scripts/postcheck.sh`: redacted local postcheck.
- `docs/runbooks/rag-ingress-queue-operator-runbook.md`: quick start, config, smoke, rollback.

Modify:

- `README.md`: link spec/plan/runbook and update quick start once implementation exists.
- `docs/requirements.md`: only if implementation discovers a requirement conflict.
- `docs/adr-0001-rag-ingress-queue.md`: only if an architecture decision changes.

## Task 0: Toolchain and Branch Gate

**Files:**
- No code files.

- [ ] **Step 1: Confirm branch is not `main` before implementation**

Run:

```bash
rtk git branch --show-current
```

Expected: branch name is not `main`.

- [ ] **Step 2: Confirm toolchain status**

Run:

```bash
rtk java -version
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle -version'
rtk docker --version
rtk docker compose version
```

Expected for full local verification:

```text
java: Java 25 runtime available
gradle: Gradle 9.x available with Amazon Corretto 25 launcher/daemon JVM
docker: available
docker compose: available
```

Observed after toolchain installation: Corretto 25 and Gradle are available. If Docker daemon or Compose remain blocked, mark runtime verification `blocked` and do not claim compose smoke success.

- [ ] **Step 3: Record evidence**

Create or update local-only evidence under:

```text
build/reports/rag-ingress-queue/toolchain.txt
```

The file must not contain secrets or private payloads.

## Task 1: Build Skeleton and Application Config

**Files:**
- Create: `settings.gradle`
- Create: `build.gradle`
- Create: `src/main/java/com/local/ragingressqueue/RagIngressQueueApplication.java`
- Create: `src/main/resources/application.yml`
- Test: `src/test/java/com/local/ragingressqueue/RagIngressQueueApplicationTests.java`

- [ ] **Step 1: Write RED context-load test**

Create `src/test/java/com/local/ragingressqueue/RagIngressQueueApplicationTests.java`:

```java
package com.local.ragingressqueue;

import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;

@SpringBootTest
class RagIngressQueueApplicationTests {
    @Test
    void contextLoads() {
    }
}
```

- [ ] **Step 2: Run RED**

Run:

```bash
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests RagIngressQueueApplicationTests'
```

Expected: FAIL because `settings.gradle`, `build.gradle`, and/or application entrypoint does not exist.

- [ ] **Step 3: Add Gradle build**

Create `settings.gradle`:

```groovy
pluginManagement {
    repositories {
        gradlePluginPortal()
        mavenCentral()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        mavenCentral()
    }
}

rootProject.name = 'rag-ingress-queue'
```

Create `build.gradle`:

```groovy
plugins {
    id 'java'
    id 'org.springframework.boot' version '4.0.6'
    id 'io.spring.dependency-management' version '1.1.7'
}

group = 'com.local'
version = '0.1.0-SNAPSHOT'

java {
    toolchain {
        languageVersion = JavaLanguageVersion.of(25)
    }
}

dependencies {
    implementation 'org.springframework.boot:spring-boot-starter-webmvc'
    implementation 'org.springframework.boot:spring-boot-starter-actuator'
    implementation 'org.springframework.boot:spring-boot-starter-validation'
    implementation 'io.nats:jnats:2.25.2'

    testImplementation 'org.springframework.boot:spring-boot-starter-test'
    testImplementation 'org.testcontainers:testcontainers:2.0.5'
    testImplementation 'org.testcontainers:testcontainers-junit-jupiter:2.0.5'
}

tasks.withType(Test).configureEach {
    useJUnitPlatform()
}
```

- [ ] **Step 4: Add application entrypoint**

Create `src/main/java/com/local/ragingressqueue/RagIngressQueueApplication.java`:

```java
package com.local.ragingressqueue;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class RagIngressQueueApplication {
    public static void main(String[] args) {
        SpringApplication.run(RagIngressQueueApplication.class, args);
    }
}
```

- [ ] **Step 5: Add baseline config**

Create `src/main/resources/application.yml`:

```yaml
spring:
  application:
    name: rag-ingress-queue
  threads:
    virtual:
      enabled: true
  main:
    keep-alive: true

management:
  endpoints:
    web:
      exposure:
        include: health,info

rag-ingress:
  nats:
    url: nats://localhost:4222
    stream: RAG_INGRESS_QUEUE
    consumer: rag_target_delivery_worker
    provision-on-startup: true
  target-profiles:
    ragflow-transcript-memory:
      adapter: ragflow
      dataset-role: transcript-memory
    ragflow-session-summary:
      adapter: ragflow
      dataset-role: session-summary
    ragflow-task-summary:
      adapter: ragflow
      dataset-role: task-summary
    ragflow-approved-memory-card:
      adapter: ragflow
      dataset-role: approved-memory-card
```

- [ ] **Step 6: Run GREEN**

Run:

```bash
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests RagIngressQueueApplicationTests'
```

Expected: PASS.

## Task 2: Core Domain and Validation RED/GREEN

**Files:**
- Create: `src/main/java/com/local/ragingressqueue/core/IngestJob.java`
- Create: `src/main/java/com/local/ragingressqueue/core/DocumentPayload.java`
- Create: `src/main/java/com/local/ragingressqueue/core/TargetProfile.java`
- Create: `src/main/java/com/local/ragingressqueue/core/TargetPressure.java`
- Create: `src/main/java/com/local/ragingressqueue/core/TargetIndexingState.java`
- Create: `src/main/java/com/local/ragingressqueue/core/IdempotencyKey.java`
- Create: `src/main/java/com/local/ragingressqueue/core/validation/IngestJobValidator.java`
- Create: `src/main/java/com/local/ragingressqueue/core/validation/ContentHashVerifier.java`
- Test: `src/test/java/com/local/ragingressqueue/core/validation/IngestJobValidatorTest.java`

- [ ] **Step 1: Write RED validation tests**

Create `src/test/java/com/local/ragingressqueue/core/validation/IngestJobValidatorTest.java` with tests for:

```java
package com.local.ragingressqueue.core.validation;

import com.local.ragingressqueue.core.DocumentPayload;
import com.local.ragingressqueue.core.IngestJob;
import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

class IngestJobValidatorTest {
    private final IngestJobValidator validator = new IngestJobValidator();

    @Test
    void acceptsRedactedRagReadyDocument() {
        IngestJob job = validJob();
        assertThat(validator.validate(job)).isEmpty();
    }

    @Test
    void rejectsRagflowNamedPayloadKindInPublicDto() {
        IngestJob job = validJob().withPayload(validJob().payload().withKind("ragflow_ready_document"));
        assertThat(validator.validate(job)).anyMatch(v -> v.contains("payload.kind"));
    }

    @Test
    void rejectsPrivateLocatorPayload() {
        IngestJob job = validJob().withPayload(validJob().payload().withKind("private_locator"));
        assertThat(validator.validate(job)).anyMatch(v -> v.contains("private_locator"));
    }

    @Test
    void rejectsNonCanonicalContentHash() {
        IngestJob job = validJob().withContentHash("sha256:redacted");
        assertThat(validator.validate(job)).anyMatch(v -> v.contains("contentHash"));
    }

    @Test
    void rejectsDigestMismatchForCanonicalBody() {
        IngestJob job = validJob().withContentHash("sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
        assertThat(validator.validate(job)).anyMatch(v -> v.contains("contentHash mismatch"));
    }

    @Test
    void acceptsExplicitIdempotencyKey() {
        IngestJob job = validJob().withIdempotencyKey("operator-provided-key-001");
        assertThat(validator.validate(job)).isEmpty();
    }

    @Test
    void rejectsUnknownTopLevelKind() {
        IngestJob job = validJob().withKind("unexpected_kind");
        assertThat(validator.validate(job)).anyMatch(v -> v.contains("kind is unknown"));
    }

    @Test
    void targetStatesDoNotIncludeAuthorized() {
        assertThat(com.local.ragingressqueue.core.TargetIndexingState.values())
            .extracting(Enum::name)
            .doesNotContain("AUTHORIZED");
    }

    private IngestJob validJob() {
        return new IngestJob(
            Map.of("type", "local_pc", "provider", "codex", "project", "workspace-ragflow-advisor"),
            new DocumentPayload(
                "redacted_rag_ready_document",
                "redaction.v2",
                "chunk.md",
                "text/markdown",
                "---\nschema_version: agent_knowledge_document.v2\nresult_type: conversation_chunk\n---\nredacted body",
                Map.of("schema_version", "agent_knowledge_document.v2", "result_type", "conversation_chunk")
            ),
            ContentHashVerifier.sha256Hex("---\nschema_version: agent_knowledge_document.v2\nresult_type: conversation_chunk\n---\nredacted body"),
            "ragflow-transcript-memory",
            "conversation_chunk",
            null
        );
    }
}
```

- [ ] **Step 2: Run RED**

Run:

```bash
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests IngestJobValidatorTest'
```

Expected: FAIL because domain and validator classes do not exist.

- [ ] **Step 3: Implement minimal domain and validator**

Create Java records/enums exactly matching the test API. `TargetIndexingState` values:

```java
ACCEPTED, DELIVERED, INDEXING, INDEXED, FAILED, THROTTLED
```

`IngestJobValidator.validate(IngestJob job)` must return `List<String>` and reject:

- `payload.kind` not equal to `redacted_rag_ready_document`
- any `payload.kind` starting with `ragflow_`
- `private_locator`
- non-hex `contentHash`
- canonical body digest mismatch
- unknown `targetProfile`
- explicit `idempotencyKey` conflicts with different content hash

- [ ] **Step 4: Run GREEN**

Run:

```bash
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests IngestJobValidatorTest'
```

Expected: PASS.

## Task 3: Redaction Guard

**Files:**
- Create: `src/main/java/com/local/ragingressqueue/core/validation/RedactionGuard.java`
- Create: `src/main/java/com/local/ragingressqueue/core/SafeJobSummary.java`
- Test: `src/test/java/com/local/ragingressqueue/core/validation/RedactionGuardTest.java`
- Test: `src/test/java/com/local/ragingressqueue/core/SafeJobSummaryTest.java`

- [ ] **Step 1: Write RED tests**

Create tests with these method names:

- `rejectsBearerToken()`: bearer-looking token is rejected
- `rejectsRawDatasetAndDocumentIds()`: `dataset_id` and `document_id` field names are rejected
- `rejectsPrivatePath()`: `/Users/` path-like strings are rejected
- `rejectsRawTranscriptFixture()`: raw transcript fixture containing `UserPromptSubmit` plus unredacted local path is rejected
- `acceptsValidRedactedFrontmatter()`: valid redacted markdown frontmatter is accepted
- `safeJobSummaryDoesNotExposeBodyOrMetadata()`: `SafeJobSummary.toString()` omits body, metadata, private path, and raw IDs
- `domainToStringDoesNotExposeBody()`: `IngestJob.toString()` and `DocumentPayload.toString()` are overridden or guarded so body text is absent

- [ ] **Step 2: Run RED**

Run:

```bash
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests RedactionGuardTest'
```

Expected: FAIL because `RedactionGuard` does not exist.

- [ ] **Step 3: Implement minimal scanner**

`RedactionGuard.inspect(String body)` returns `List<String>`. It must use denylist regexes for:

```text
Bearer\s+[A-Za-z0-9._~+/=-]+
\b(dataset_id|document_id|api_key|token)\b
/Users/[^\\s]+
private_locator
raw_transcript
```

It must also require frontmatter keys:

```text
schema_version:
result_type:
```

`SafeJobSummary` must include only:

```text
jobId/hashPrefix, source provider/project, targetProfile, kind, contentType, pressure/status
```

- [ ] **Step 4: Run GREEN**

Run:

```bash
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests RedactionGuardTest'
```

Expected: PASS.

## Task 4: HTTP API with Fake Queue Publisher

**Files:**
- Create: `src/main/java/com/local/ragingressqueue/api/IngressController.java`
- Create: `src/main/java/com/local/ragingressqueue/api/dto/EnqueueRequest.java`
- Create: `src/main/java/com/local/ragingressqueue/api/dto/EnqueueResponse.java`
- Create: `src/main/java/com/local/ragingressqueue/api/dto/PayloadEnvelope.java`
- Create: `src/main/java/com/local/ragingressqueue/queue/IngestPublisher.java`
- Test: `src/test/java/com/local/ragingressqueue/api/IngressControllerTest.java`
- Test: `src/test/java/com/local/ragingressqueue/api/IngressControllerLogCaptureTest.java`

- [ ] **Step 1: Write RED Web MVC tests**

Test cases:

- valid enqueue returns `{accepted:true,status:"queued"}` and stable redacted `jobId`
- missing `source` returns HTTP 400
- explicit `idempotencyKey` is accepted
- same `idempotencyKey` with different content hash returns HTTP 409
- invalid `private_locator` returns HTTP 400
- reserved `redacted_document_ref` returns HTTP 422 until blob storage is configured
- payload with bearer token returns HTTP 400 and response does not echo token
- publisher failure without publish ack returns HTTP 503
- `/healthz` returns component status
- `/status` returns queue counts and target pressure without raw IDs
- log capture proves body, token, raw IDs, and private path are absent

- [ ] **Step 2: Run RED**

Run:

```bash
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests IngressControllerTest'
```

Expected: FAIL because controller and DTOs do not exist.

- [ ] **Step 3: Implement API**

Implementation rules:

- Convert `EnqueueRequest` to `IngestJob`.
- Require `schemaVersion=rag_ingress_enqueue.v1`.
- Model payload as a variant envelope, not as a RAGFlow-specific DTO.
- Run `IngestJobValidator` and `RedactionGuard`.
- Use `IngestPublisher.publish(IngestJob job)`.
- Return HTTP 202 for accepted publish.
- Return HTTP 400 for validation/redaction rejection.
- Return HTTP 409 for `idempotencyKey` conflict.
- Return HTTP 422 for reserved-but-disabled `redacted_document_ref`.
- Return HTTP 503 if publisher reports no publish ack.
- Response must never include body, token, raw IDs, or private path.
- Logs must use `SafeJobSummary` only.

- [ ] **Step 4: Run GREEN**

Run:

```bash
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests IngressControllerTest'
```

Expected: PASS.

## Task 5: JetStream Publisher Contract and Integration

**Files:**
- Create: `src/main/java/com/local/ragingressqueue/queue/NatsJetStreamPublisher.java`
- Create: `src/main/java/com/local/ragingressqueue/queue/SubjectRouter.java`
- Create: `src/main/java/com/local/ragingressqueue/queue/JetStreamProvisioner.java`
- Create: `src/main/java/com/local/ragingressqueue/queue/IngestConsumer.java`
- Test: `src/test/java/com/local/ragingressqueue/queue/SubjectRouterTest.java`
- Test: `src/test/java/com/local/ragingressqueue/queue/NatsJetStreamPublisherContractTest.java`
- Test: `src/test/java/com/local/ragingressqueue/queue/JetStreamProvisionerTest.java`
- Test: `src/test/java/com/local/ragingressqueue/queue/NatsJetStreamPublisherIntegrationTest.java`

- [ ] **Step 1: Write RED subject routing tests**

Expected mapping:

```text
conversation_chunk -> rag.ingress.transcript
session_summary -> rag.ingress.document
task_summary -> rag.ingress.document
approved_memory_card -> rag.ingress.document
```

- [ ] **Step 2: Run RED**

Run:

```bash
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests SubjectRouterTest'
```

Expected: FAIL because `SubjectRouter` does not exist.

- [ ] **Step 3: Implement subject router and fake publisher contract**

`NatsJetStreamPublisher` must only report accepted after publish ack is received. In unit tests, use a fake `JetStreamClient` port so no live NATS is required.

- [ ] **Step 4: Run GREEN**

Run:

```bash
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests SubjectRouterTest --tests NatsJetStreamPublisherContractTest'
```

Expected: PASS.

- [ ] **Step 5: Write RED provisioner and Testcontainers integration tests**

Test method names:

- `createsStreamAndDurableConsumerWhenMissing()`
- `failsWhenExistingStreamSubjectConfigDrifts()`
- `publishesOnlyAfterAck()`
- `fetchesAndExplicitlyAcksMessage()`
- `nakRedeliveryCanReachQuarantineCandidate()`

Run:

```bash
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests JetStreamProvisionerTest --tests NatsJetStreamPublisherIntegrationTest'
```

Expected: FAIL before `JetStreamProvisioner` and Testcontainers-backed implementation exist.

- [ ] **Step 6: Add Testcontainers NATS integration**

Add an integration test profile/class that:

- starts `nats:2-alpine` with `-js`
- calls `JetStreamProvisioner` to create `RAG_INGRESS_QUEUE` and `rag_target_delivery_worker`
- fails on stream/consumer config drift
- publishes a valid job
- fetches via durable pull consumer
- acks explicitly
- exercises nak/retry/max-deliver enough to produce a redacted quarantine candidate in fake policy

Run:

```bash
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests NatsJetStreamPublisherIntegrationTest'
```

Expected: PASS when Docker and Gradle are available. If Docker runtime is blocked, record exact blocker and do not claim integration verification.

## Task 6: Worker Pressure Gate

**Files:**
- Create: `src/main/java/com/local/ragingressqueue/worker/IngestWorker.java`
- Create: `src/main/java/com/local/ragingressqueue/worker/DeliveryDecision.java`
- Create: `src/main/java/com/local/ragingressqueue/target/RagTargetAdapter.java`
- Create: `src/main/java/com/local/ragingressqueue/target/TargetStatusSnapshot.java`
- Test: `src/test/java/com/local/ragingressqueue/worker/IngestWorkerTest.java`

- [ ] **Step 1: Write RED worker tests**

Test cases:

- `OPEN` calls adapter `deliver`
- `THROTTLED` does not call queue consumer `fetch` and does not call adapter `deliver`
- `CLOSED` does not call queue consumer `fetch` and does not call adapter `deliver`
- failed delivery requests retry/nak path
- max-deliver exceeded maps to terminal quarantine candidate
- queue ack is not exposed as `INDEXED`
- target `INDEXED` is not exposed as external authorization

- [ ] **Step 2: Run RED**

Run:

```bash
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests IngestWorkerTest'
```

Expected: FAIL because worker classes do not exist.

- [ ] **Step 3: Implement fail-closed worker orchestration**

Rules:

- Do not fetch/deliver new work unless target pressure is `OPEN`.
- Keep queue ack separate from target indexing and authorization.
- Expose redacted delivery snapshot only.
- Use `IngestConsumer` fake in tests to verify `fetch()` call counts.

- [ ] **Step 4: Run GREEN**

Run:

```bash
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests IngestWorkerTest'
```

Expected: PASS.

## Task 7: RAGFlow Adapter Skeleton and Adapter-Private References

**Files:**
- Create: `src/main/java/com/local/ragingressqueue/target/ragflow/RagFlowAdapter.java`
- Create: `src/main/java/com/local/ragingressqueue/target/ragflow/RagFlowTargetRef.java`
- Create: `src/main/java/com/local/ragingressqueue/target/ragflow/RagFlowStatusSnapshotMapper.java`
- Test: `src/test/java/com/local/ragingressqueue/target/ragflow/RagFlowAdapterTest.java`

- [ ] **Step 1: Write RED adapter tests**

Test cases:

- RAGFlow `DONE` maps to `INDEXED`
- RAGFlow raw document ID is converted to opaque handle/hash in public snapshot
- snapshot contains `jobId`, `contentHash`, `targetProfile`, generic status, and redacted target ref for external reconcile client
- pressure `UNSTART`/`RUNNING` counts above threshold maps to `THROTTLED` or `CLOSED`
- adapter never returns `AUTHORIZED`
- adapter never mutates an external document status table

- [ ] **Step 2: Run RED**

Run:

```bash
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests RagFlowAdapterTest'
```

Expected: FAIL because adapter classes do not exist.

- [ ] **Step 3: Implement skeleton with fake HTTP client port**

Do not call live RAGFlow in unit tests. Introduce a client port that can be backed by fake responses.

- [ ] **Step 4: Run GREEN**

Run:

```bash
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests RagFlowAdapterTest'
```

Expected: PASS.

## Task 8: Redacted Status and Postcheck

**Files:**
- Create: `src/main/java/com/local/ragingressqueue/api/dto/StatusResponse.java`
- Create: `src/main/java/com/local/ragingressqueue/status/StatusService.java`
- Create: `src/main/java/com/local/ragingressqueue/status/ExternalStatusSummary.java`
- Create: `scripts/postcheck.sh`
- Create: `scripts/postcheck.schema.json`
- Test: `src/test/java/com/local/ragingressqueue/status/StatusServiceTest.java`
- Test: `src/test/java/com/local/ragingressqueue/status/PostcheckOutputTest.java`

- [ ] **Step 1: Write RED status tests**

Assert `/status` and `StatusService` include:

- queue pending/inFlight/redelivered/deadLetter counts
- target name and pressure
- `documentStatus.indexedCandidateCount`
- `authorization.authorizedCount`
- `externalStatus` as `not_configured`, `unavailable`, or `ok`
- queue ack count is not used as indexed count
- indexed candidate count is not used as authorized count
- no token, raw dataset_id, raw document_id, private path, or body

- [ ] **Step 2: Run RED**

Run:

```bash
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests StatusServiceTest'
```

Expected: FAIL because status service does not exist.

- [ ] **Step 3: Implement status service and postcheck script**

`scripts/postcheck.sh` must:

- call API `/healthz`
- call API `/status`
- optionally query NATS stream/consumer when `nats` CLI is available
- scan its own output for forbidden patterns and exit non-zero if found
- emit JSON matching `scripts/postcheck.schema.json`
- use timeout `30s`
- abort on API health failure, status schema mismatch, forbidden pattern hit, stream/consumer drift, or worker pressure-gate failure
- write evidence to `build/reports/rag-ingress-queue/postcheck.json`

- [ ] **Step 4: Run GREEN**

Run:

```bash
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests StatusServiceTest'
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests PostcheckOutputTest'
rtk bash scripts/postcheck.sh --offline --timeout 30
```

Expected: tests PASS and offline postcheck returns exit 0.

## Task 9: Compose and Runtime Smoke

**Files:**
- Create: `compose.yaml`
- Create: `Dockerfile`
- Test: `src/test/java/com/local/ragingressqueue/runtime/ComposeConfigTest.java`

- [ ] **Step 1: Write RED config tests**

Assert:

- compose project contains `nats-jetstream`, `ingress-api`, `ingress-worker`
- compose file does not reference RAGFlow service names, volumes, or compose project
- NATS enables JetStream with file storage
- API/worker receive `RAG_INGRESS_NATS_URL=nats://nats-jetstream:4222`
- API and worker use same image with different command/env
- named volume preserves pending messages across container restart without `down -v`
- compose declares local MVP persistence and not HA semantics in runbook

- [ ] **Step 2: Run RED**

Run:

```bash
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests ComposeConfigTest'
```

Expected: FAIL because compose/Dockerfile do not exist.

- [ ] **Step 3: Implement compose and Dockerfile**

`compose.yaml` must define only this project:

```yaml
services:
  nats-jetstream:
    image: nats:2-alpine
    command: ["-js", "-sd", "/data"]
    volumes:
      - nats_data:/data
    ports:
      - "127.0.0.1:4222:4222"
  ingress-api:
    build: .
    ports:
      - "127.0.0.1:8080:8080"
    environment:
      SPRING_PROFILES_ACTIVE: api
      RAG_INGRESS_NATS_URL: nats://nats-jetstream:4222
    depends_on:
      - nats-jetstream
  ingress-worker:
    build: .
    environment:
      SPRING_PROFILES_ACTIVE: worker
      SPRING_MAIN_WEB_APPLICATION_TYPE: none
      RAG_INGRESS_NATS_URL: nats://nats-jetstream:4222
    depends_on:
      - nats-jetstream
volumes:
  nats_data:
```

- [ ] **Step 4: Run focused verification**

Run:

```bash
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests ComposeConfigTest'
```

Expected: PASS.

- [ ] **Step 5: Run runtime smoke when compose is available**

Run:

```bash
rtk docker compose -f compose.yaml up --build -d
rtk bash scripts/postcheck.sh --timeout 30 --evidence build/reports/rag-ingress-queue/postcheck.json
rtk docker compose -f compose.yaml down
```

Expected: API shape postcheck PASS plus separate NATS/worker smoke evidence. If `docker compose` is unavailable, mark runtime verification `blocked` with the exact command output. The postcheck JSON must not set `runtime.verified=true` unless JetStream stream/consumer, publish ack, and worker fetch/ack/nak evidence were also gathered.

Abort criteria:

- API `/healthz` unavailable after 30 seconds.
- `/status` does not match `scripts/postcheck.schema.json`.
- Forbidden pattern appears in postcheck output.
- stream or consumer config drift is detected.
- target pressure is `THROTTLED` or `CLOSED` and worker still fetches/delivers.

## Task 10: Documentation and Runbook

**Files:**
- Modify: `README.md`
- Create: `docs/runbooks/rag-ingress-queue-operator-runbook.md`
- Modify: `docs/superpowers/specs/2026-05-17-rag-ingress-queue-mvp-spec.md` only if implementation changes the spec.

- [ ] **Step 1: Write runbook**

Runbook sections:

- purpose and non-goals
- prerequisites
- local config
- test commands
- compose smoke commands
- postcheck command
- live RAGFlow approval gate
- rollback owner and procedure
- evidence artifact path
- local MVP persistence limits: single NATS server, named volume, not HA
- restart smoke: pending message survives container restart when volume is preserved

- [ ] **Step 2: Update README**

README must link:

- MVP spec
- implementation plan
- review summary
- operator runbook

README must separate:

- implementation proof
- fake/Testcontainers proof
- runtime/live proof
- authorization proof

- [ ] **Step 3: Verify docs have no forbidden examples**

Run:

```bash
rtk rg -n -f scripts/redaction-denylist.txt README.md docs
```

Expected: only policy/negative-test mentions appear. No actual secret, private path, or raw identifier example appears.

- [ ] **Step 4: Align requirements payload example**

Update `docs/requirements.md` so the public example uses:

```json
"kind": "redacted_rag_ready_document"
```

Do not reintroduce `ragflow_ready_document` as a public payload kind.

## Task 11: Final Review and Completion Gate

**Files:**
- All changed files.

- [ ] **Step 1: Run focused tests**

Run:

```bash
rtk sh -lc 'JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test'
```

Expected: PASS. If blocked by missing Java/Gradle, final report must say implementation verification is blocked.

- [ ] **Step 2: Run redaction scan**

Run:

```bash
rtk rg -n -f scripts/redaction-denylist.txt README.md docs src scripts compose.yaml Dockerfile build.gradle settings.gradle
```

Expected: only negative-test/policy mentions, no live secrets or private payloads.

- [ ] **Step 3: Run subagent review**

Dispatch at least three reviewers:

- spec compliance reviewer
- code quality/maintainability reviewer
- security/redaction/deploy reviewer

Use `model: gpt-5.5`, `reasoning_effort: high` for all final reviewers.

All P0/P1 issues must be fixed before completion.

- [ ] **Step 4: Stage exact files only**

Run:

```bash
rtk git status --short
rtk git add settings.gradle build.gradle src compose.yaml Dockerfile scripts README.md docs/superpowers docs/runbooks
rtk git diff --cached --stat
```

Expected: only intended files are staged.

- [ ] **Step 5: Commit only after fresh verification**

Commit message must end with:

```text
Co-Authored-By: Codex GPT-5 <noreply@openai.com>
```

If the active model name differs, replace `GPT-5` with the current session model name.

## Live RAGFlow Gate

Do not run live RAGFlow calls without a separate explicit approval packet containing:

- argv/request
- timeout
- redaction policy
- abort criteria
- postcheck
- rollback owner
- expected evidence

Approved live smoke proves only:

- sanitized upload/status path can run
- raw IDs stay out of generic outputs
- RAGFlow `DONE` maps to `INDEXED` candidate

Approved live smoke does not prove:

- external document status authorization
- recall/promote eligibility
- memory-regeneration correctness
