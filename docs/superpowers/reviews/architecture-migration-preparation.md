# Architecture Migration Preparation Checklist

## Status: READY FOR EXECUTION

이 문서는 `rag-ingress-queue` 컴포넌트 드리븐 레이어드 아키텍처 마이그레이션의 모든 준비물이 구비되었음을 확인하는 체크리스트입니다.

---

## Phase 0: Review Documents Committed

- [x] `docs/superpowers/reviews/architecture-improvement-review.html` — ACO 리뷰 + 개선 후보
- [x] `docs/superpowers/reviews/backend-architecture-design.html` — 백엔드 아키텍처 설계
- [x] `docs/superpowers/reviews/sidebeam-cross-review-combined.md` — 종합 분석
- [x] `docs/adr-0002-component-driven-layered-architecture.md` — 공식 ADR

---

## Phase 1: Source Code Analysis Complete

### All Main Source Files Read
- [x] `RagIngressQueueApplication.java` — Spring Boot entrypoint
- [x] `api/IngressController.java` — REST controller
- [x] `api/StatusService.java` — Status service (RAGFlow coupled)
- [x] `api/IdempotencyStore.java` — In-memory idempotency
- [x] `core/IngestJob.java` — Domain record
- [x] `core/DocumentPayload.java` — Domain record
- [x] `core/TargetProfile.java` — Domain record
- [x] `core/TargetPressure.java` — Domain enum
- [x] `core/TargetIndexingState.java` — Domain enum
- [x] `core/SafeJobSummary.java` — Safe logging summary
- [x] `core/validation/IngestJobValidator.java` — Validation logic
- [x] `core/validation/RedactionGuard.java` — Redaction guard
- [x] `core/validation/ContentHashVerifier.java` — Hash verification
- [x] `queue/IngestPublisher.java` — Port interface
- [x] `queue/IngestConsumer.java` — Port interface
- [x] `queue/NatsIngestPublisher.java` — Adapter implementation
- [x] `queue/NatsIngestConsumer.java` — Adapter implementation
- [x] `queue/NatsJetStreamConfiguration.java` — Configuration
- [x] `queue/NatsJetStreamProvisioner.java` — Stream provisioner
- [x] `queue/JetStreamPublisherGateway.java` — Gateway interface
- [x] `queue/JetStreamConsumerGateway.java` — Gateway interface
- [x] `queue/NatsJetStreamPublisherGateway.java` — NATS implementation
- [x] `queue/NatsJetStreamConsumerGateway.java` — NATS implementation
- [x] `queue/SubjectRouter.java` — Subject routing
- [x] `queue/IngestJobMessageCodec.java` — Message codec
- [x] `queue/IngestMessage.java` — Message record
- [x] `queue/RawIngestMessage.java` — Raw message record
- [x] `queue/AcknowledgementHandle.java` — Ack handle
- [x] `queue/JetStreamPublishAck.java` — Publish ack record
- [x] `queue/PublishResult.java` — Publish result
- [x] `queue/QueueStatusProvider.java` — Status provider interface
- [x] `queue/NatsQueueStatusProvider.java` — NATS status provider
- [x] `queue/QueueStatusSnapshot.java` — Status snapshot
- [x] `worker/IngestWorker.java` — Worker logic
- [x] `worker/WorkerLoopRunner.java` — Worker runner
- [x] `worker/WorkerConfiguration.java` — Worker config
- [x] `worker/DeliveryDecision.java` — Decision record
- [x] `target/RagTargetAdapter.java` — Port interface
- [x] `target/RagFlowTargetAdapter.java` — Adapter implementation
- [x] `target/HttpRagFlowGateway.java` — HTTP gateway
- [x] `target/RagFlowGateway.java` — Gateway interface
- [x] `target/RagFlowPressurePolicy.java` — Pressure policy
- [x] `target/RagFlowPressureSnapshot.java` — Pressure snapshot
- [x] `target/RagFlowDocumentRef.java` — Document ref
- [x] `target/RagFlowDeliveryException.java` — Exception
- [x] `target/DeliveryResult.java` — Result record
- [x] `target/TargetStatusSnapshot.java` — Status snapshot
- [x] `api/dto/EnqueueRequest.java` — Request DTO
- [x] `api/dto/EnqueueResponse.java` — Response DTO
- [x] `api/dto/PayloadEnvelope.java` — Payload envelope
- [x] `api/dto/DocumentRequest.java` — Document DTO

### All Test Files Identified
- [x] `RagIngressQueueApplicationTests.java`
- [x] `api/IngressControllerTest.java`
- [x] `api/StatusServiceTest.java`
- [x] `core/SafeJobSummaryTest.java`
- [x] `core/validation/IngestJobValidatorTest.java`
- [x] `core/validation/RedactionGuardTest.java`
- [x] `queue/NatsIngestConsumerTest.java`
- [x] `queue/NatsIngestPublisherTest.java`
- [x] `queue/SubjectRouterTest.java`
- [x] `runtime/ComposeConfigTest.java`
- [x] `target/HttpRagFlowGatewayTest.java`
- [x] `target/RagFlowTargetAdapterTest.java`
- [x] `worker/IngestWorkerTest.java`

### Build & Config Files Read
- [x] `build.gradle` — Dependencies, plugins
- [x] `settings.gradle` — Project settings
- [x] `Dockerfile` — Multi-stage build
- [x] `compose.yaml` — Docker Compose
- [x] `application.yml` — Spring config
- [x] `scripts/postcheck.sh` — Postcheck script
- [x] `scripts/postcheck.schema.json` — Postcheck schema
- [x] `scripts/redaction-denylist.txt` — Redaction patterns

---

## Phase 2: Reference Project Analyzed

- [x] sidebeam-backend cloned and analyzed
- [x] Package structure documented
- [x] Layer patterns documented (domain/service/controller/external/common)
- [x] Exception hierarchy documented
- [x] Configuration properties pattern documented
- [x] ArchUnit patterns documented
- [x] External service isolation patterns documented

---

## Phase 3: ACO Advisory Reviews Completed

### ACO Run #1
- [x] Run ID: `7155fc88-d2c6-48a8-a414-9ad28c82dbc7`
- [x] Providers: Gemini + Codex
- [x] Focus: Plan critique (P1/P2 findings)

### ACO Run #2
- [x] Run ID: `fb5a8515-7334-4ad4-bb54-20f8871bd9bb`
- [x] Providers: Gemini + Codex
- [x] Focus: Combined proposal critique

---

## Phase 4: Target Architecture Defined

### Package Structure (Final)
```
com.local.ragingressqueue
├── ingest/
│   ├── api/
│   ├── service/
│   ├── domain/
│   │   └── validation/
│   └── dto/
├── delivery/
│   ├── worker/
│   ├── service/
│   └── domain/
├── status/
│   ├── api/
│   └── service/
├── queue/
│   └── port/
├── target/
│   └── port/
├── adapter/
│   ├── infra/
│   │   └── nats/
│   └── ext/
│       └── ragflow/
└── common/
    ├── config/
    ├── exception/
    ├── logging/
    └── web/
```

### Layer Dependency Rules
- [x] api → service → domain → port ← adapter (documented)
- [x] common is referenceable by all layers (documented)
- [x] Forbidden dependencies defined (documented)

### ArchUnit Rules (5 defined)
- [x] Rule 1: service may not depend on api
- [x] Rule 2: domain may not depend on adapter
- [x] Rule 3: port may not depend on adapter
- [x] Rule 4: adapter must depend on port
- [x] Rule 5: common may not depend on feature packages

---

## Phase 5: Gradle Dependencies Prepared

### Required Additions
```groovy
// ArchUnit for architecture testing
implementation 'com.tngtech.archunit:archunit-junit5:1.4.1'

// Jacoco for coverage (report-only initially)
id 'jacoco'

// Logstash encoder for structured logging (optional)
implementation 'net.logstash.logback:logstash-logback-encoder:8.0'
```

- [x] ArchUnit version confirmed (1.4.1)
- [x] Spring Boot validation starter already present
- [x] Jacoco plugin addition planned
- [x] Logstash encoder addition planned (optional)

---

## Phase 6: New File Inventory

### Required New Files (Tier 1-2)
1. [ ] `common/exception/ApplicationException.java`
2. [ ] `common/exception/BusinessException.java`
3. [ ] `common/exception/TechnicalException.java`
4. [ ] `common/exception/ValidationException.java`
5. [ ] `common/exception/ErrorCode.java`
6. [ ] `common/exception/GlobalExceptionHandler.java`
7. [ ] `common/config/QueueProperties.java` (@ConfigurationProperties)
8. [ ] `common/config/RagFlowProperties.java` (@ConfigurationProperties)
9. [ ] `common/config/WorkerProperties.java` (@ConfigurationProperties)
10. [ ] `architecture/ArchitectureTest.java`

### Optional New Files (Tier 3)
11. [ ] `common/logging/SensitiveDataMaskingConverter.java`
12. [ ] `common/web/CorrelationIdFilter.java`
13. [ ] `resources/logback-spring.xml`

---

## Phase 7: File Move Plan

### Source Files (20+)
| # | Current Path | Target Path | Notes |
|---|-------------|-------------|-------|
| 1 | `api/IngressController.java` | `ingest/api/IngressController.java` | |
| 2 | `api/StatusService.java` | `status/service/StatusService.java` | Refactor to depend on Port only |
| 3 | `api/IdempotencyStore.java` | `ingest/service/IdempotencyStore.java` | |
| 4 | `core/IngestJob.java` | `ingest/domain/IngestJob.java` | |
| 5 | `core/DocumentPayload.java` | `ingest/domain/DocumentPayload.java` | |
| 6 | `core/TargetProfile.java` | `ingest/domain/TargetProfile.java` | Or delivery/domain/ |
| 7 | `core/TargetPressure.java` | `delivery/domain/TargetPressure.java` | |
| 8 | `core/TargetIndexingState.java` | `target/port/TargetIndexingState.java` | Or common/ |
| 9 | `core/SafeJobSummary.java` | `common/logging/SafeJobSummary.java` | |
| 10 | `core/validation/*` | `ingest/domain/validation/*` | |
| 11 | `queue/IngestPublisher.java` | `queue/port/IngestPublisher.java` | Port |
| 12 | `queue/IngestConsumer.java` | `queue/port/IngestConsumer.java` | Port |
| 13 | `queue/NatsIngestPublisher.java` | `adapter/infra/nats/NatsIngestPublisher.java` | Adapter |
| 14 | `queue/NatsIngestConsumer.java` | `adapter/infra/nats/NatsIngestConsumer.java` | Adapter |
| 15 | `queue/NatsJetStreamConfiguration.java` | `common/config/NatsJetStreamConfiguration.java` | Config |
| 16 | `queue/NatsJetStreamProvisioner.java` | `adapter/infra/nats/NatsJetStreamProvisioner.java` | Adapter |
| 17 | `queue/NatsQueueStatusProvider.java` | `adapter/infra/nats/NatsQueueStatusProvider.java` | Adapter |
| 18 | `queue/NatsJetStreamPublisherGateway.java` | `adapter/infra/nats/NatsJetStreamPublisherGateway.java` | Adapter |
| 19 | `queue/NatsJetStreamConsumerGateway.java` | `adapter/infra/nats/NatsJetStreamConsumerGateway.java` | Adapter |
| 20 | `queue/SubjectRouter.java` | `adapter/infra/nats/SubjectRouter.java` | Or ingest/domain/ |
| 21 | `queue/IngestJobMessageCodec.java` | `adapter/infra/nats/IngestJobMessageCodec.java` | Or queue/port/ |
| 22 | `worker/IngestWorker.java` | `delivery/worker/IngestWorker.java` | |
| 23 | `worker/WorkerLoopRunner.java` | `delivery/worker/WorkerLoopRunner.java` | |
| 24 | `worker/WorkerConfiguration.java` | `common/config/WorkerConfiguration.java` | Or delivery/ |
| 25 | `worker/DeliveryDecision.java` | `delivery/domain/DeliveryDecision.java` | |
| 26 | `target/RagTargetAdapter.java` | `target/port/RagTargetAdapter.java` | Port |
| 27 | `target/RagFlowTargetAdapter.java` | `adapter/ext/ragflow/RagFlowTargetAdapter.java` | Adapter |
| 28 | `target/HttpRagFlowGateway.java` | `adapter/ext/ragflow/HttpRagFlowGateway.java` | Adapter |
| 29 | `target/RagFlowGateway.java` | `adapter/ext/ragflow/RagFlowGateway.java` | Adapter |
| 30 | `target/RagFlowPressurePolicy.java` | `adapter/ext/ragflow/RagFlowPressurePolicy.java` | Adapter |
| 31 | `target/RagFlowPressureSnapshot.java` | `adapter/ext/ragflow/RagFlowPressureSnapshot.java` | Adapter |
| 32 | `target/RagFlowDocumentRef.java` | `adapter/ext/ragflow/RagFlowDocumentRef.java` | Adapter |
| 33 | `target/RagFlowDeliveryException.java` | `adapter/ext/ragflow/RagFlowDeliveryException.java` | Adapter |
| 34 | `target/DeliveryResult.java` | `delivery/domain/DeliveryResult.java` | |
| 35 | `target/TargetStatusSnapshot.java` | `target/port/TargetStatusSnapshot.java` | |

### Test Files (13)
| # | Current Path | Target Path |
|---|-------------|-------------|
| 1 | `api/IngressControllerTest.java` | `ingest/api/IngressControllerTest.java` |
| 2 | `api/StatusServiceTest.java` | `status/service/StatusServiceTest.java` |
| 3 | `core/SafeJobSummaryTest.java` | `common/logging/SafeJobSummaryTest.java` |
| 4 | `core/validation/IngestJobValidatorTest.java` | `ingest/domain/validation/IngestJobValidatorTest.java` |
| 5 | `core/validation/RedactionGuardTest.java` | `ingest/domain/validation/RedactionGuardTest.java` |
| 6 | `queue/NatsIngestConsumerTest.java` | `adapter/infra/nats/NatsIngestConsumerTest.java` |
| 7 | `queue/NatsIngestPublisherTest.java` | `adapter/infra/nats/NatsIngestPublisherTest.java` |
| 8 | `queue/SubjectRouterTest.java` | `adapter/infra/nats/SubjectRouterTest.java` |
| 9 | `target/HttpRagFlowGatewayTest.java` | `adapter/ext/ragflow/HttpRagFlowGatewayTest.java` |
| 10 | `target/RagFlowTargetAdapterTest.java` | `adapter/ext/ragflow/RagFlowTargetAdapterTest.java` |
| 11 | `worker/IngestWorkerTest.java` | `delivery/worker/IngestWorkerTest.java` |
| 12 | `runtime/ComposeConfigTest.java` | (unchanged) |
| 13 | `RagIngressQueueApplicationTests.java` | (unchanged) |

---

## Phase 8: Configuration Changes

### application.yml Changes
- [x] Current structure analyzed
- [x] @ConfigurationProperties binding structure planned
- [x] Queue config: `rag-ingress.nats.*` → `queue.*`
- [x] RagFlow config: `rag-ingress.target.ragflow.*` → `ragflow.*`
- [x] Worker config: `rag-ingress.worker.*` → `worker.*`

### build.gradle Changes
- [x] ArchUnit dependency addition planned
- [x] Jacoco plugin addition planned
- [x] Logstash encoder addition planned (optional)

---

## Phase 9: Verification Commands Prepared

### Pre-Migration Baseline
```bash
JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test
gradle bootJar
bash scripts/postcheck.sh --offline --timeout 30 --evidence build/reports/rag-ingress-queue/postcheck.json
```

### Post-Migration Verification (per step)
```bash
JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test
gradle bootJar
```

### Final Verification
```bash
JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test
bash scripts/postcheck.sh --offline --timeout 30 --evidence build/reports/rag-ingress-queue/postcheck.json
```

- [x] All verification commands documented
- [x] Evidence path defined: `build/reports/rag-ingress-queue/postcheck.json`

---

## Phase 10: Risk Assessment

| Risk | Likelihood | Impact | Mitigation | Status |
|------|-----------|--------|-----------|--------|
| Import path errors | High | Medium | IDE auto-refactor + build verification | Prepared |
| Spring bean scan failure | Medium | High | @ComponentScan verify | Prepared |
| Test failures | Medium | Medium | Step-by-step migration | Prepared |
| ArchUnit rule violations | Medium | Low | Gradual rule addition | Prepared |
| @Profile mismatch | Low | Medium | Profile path verification | Prepared |
| Git history complexity | High | Low | Acceptable for structural change | Accepted |

---

## Phase 11: Commit Strategy

### Commit Sequence (planned)
1. `docs: add architecture review HTML and cross-reference analysis` — COMPLETED
2. `docs: add ADR-0002 component-driven layered architecture` — COMPLETED
3. `refactor: move source to component-driven layered packages` — PLANNED (single commit for all moves)
4. `feat: add ArchUnit architecture tests` — PLANNED
5. `feat: add exception hierarchy and GlobalExceptionHandler` — PLANNED
6. `feat: add @ConfigurationProperties for queue and ragflow config` — PLANNED

---

## CONCLUSION

All preparation materials are ready. The following prerequisites are satisfied:

1. ✅ Architecture direction is defined and documented (ADR-0002)
2. ✅ All source files have been read and understood
3. ✅ All test files have been identified
4. ✅ Reference patterns (sidebeam-backend) have been analyzed
5. ✅ ACO advisory reviews have been completed
6. ✅ Package structure is finalized
7. ✅ Layer dependency rules are defined
8. ✅ ArchUnit rules are specified
9. ✅ Gradle dependency additions are planned
10. ✅ File move plan is complete (35 source files + 13 test files)
11. ✅ New file inventory is complete (10+ files)
12. ✅ Verification commands are prepared
13. ✅ Risk assessment is complete
14. ✅ Commit strategy is planned

**Status: READY TO START CODE MIGRATION**

Ready to execute Phase 4 (Code Application) when instructed.
