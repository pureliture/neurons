# rag-ingress-queue Architecture Improvement Proposal
## Combined Analysis: rag-ingress-queue + sidebeam-backend Style Cross-Review

### ACO Advisory Context
- ACO Run: 7155fc88-d2c6-48a8-a414-9ad28c82dbc7 (Gemini + Codex, plan-critique preset)
- Target: rag-ingress-queue MVP (Java 25, Spring Boot 4.x, NATS JetStream, RetiredIndexBridge adapter)
- Reference Style: sidebeam-backend (Java 24, Spring Boot 3.5.4, GitLab external integration)
- Constraint: No live RetiredIndexBridge mutation. No Docker/Compose runtime mutation. No broad rewrites.

---

## Section 1: ACO Advisory Findings (from prior run)

### P1: StatusService RetiredIndexBridge-specific coupling
- StatusService directly depends on RetiredIndexBridgeGateway, RetiredIndexBridgePressurePolicy, RetiredIndexBridge-specific config
- Core API layer couples to adapter-private details
- Resolution: Add snapshot() to RagTargetAdapter; StatusService depends on contract only

### P1: JetStreamProvisioner drift risk
- updateStream/addOrUpdateConsumer silently modifies existing config at startup
- Resolution: Read existing config, compare subjects/retention/storage/ackPolicy/maxDeliver/ackWait, fail-closed on drift

### P1: Idempotency is process-local
- IdempotencyStore is in-memory ConcurrentHashMap; breaks on API restart/replica
- Resolution: Map idempotencyKey to Nats-Msg-Id header, document "single process best-effort", add JetStream dedupe note

### P1: THROTTLED state too coarse
- THROTTLED treated same as CLOSED (no fetch)
- Resolution: Add slow fetch (reduced batch or sleep) for THROTTLED, gated by config

### P2: RetiredIndexBridge adapter package separation
- RetiredIndexBridge-specific classes mixed in generic target package
- Resolution: Move RetiredIndexBridge classes to target.retired_index_bridge sub-package

### P2: Postcheck schema validation weak
- postcheck.sh only checks field existence via jq, not schema.json compliance
- Resolution: Add PostcheckOutputTest or schema validation step

---

## Section 2: sidebeam-backend Style Cross-Reference

### Strengths to Adopt

#### 1. Layered Package Organization
sidebeam: com.sidebeam.bookmark.{domain|controller|service|dto|repository}
           com.sidebeam.common.{core|rest|security|logging|cache}
           com.sidebeam.external.gitlab.{service|dto|config}

rag-ingress-queue current: com.local.ragingressqueue.{api|core|queue|worker|target}

Adopt: Split target package into generic contract (target/) and adapter-private impl (target.retired_index_bridge/)
Adopt: Consider common/ layer for shared infra (exception, response, config)

#### 2. Common Response Wrapper + Auto-Wrapping
sidebeam: ApiResponse<T> with success/data/error/timestamp + GlobalResponseBodyAdvice auto-wraps controller returns
rag-ingress-queue: Controller directly returns ResponseEntity with varying shapes

Adopt: Unified response envelope + auto-wrapping mechanism (if API contract permits)

#### 3. Exception Hierarchy + ErrorCode Enum
sidebeam: ApplicationException -> BusinessException/TechnicalException/ValidationException + ErrorCode(code, message, HttpStatus) + GlobalExceptionHandler
rag-ingress-queue: Controller directly assembles ResponseEntity with status codes; RetiredIndexBridgeDeliveryException is simple RuntimeException

Adopt: Exception hierarchy + ErrorCode enum + @RestControllerAdvice handler; controller focuses on business flow only

#### 4. ArchUnit Architecture Rules
sidebeam: ArchitectureTest enforces domain->controller dependency prohibition, service->controller prohibition
rag-ingress-queue: No architecture rule tests

Adopt: ArchUnit test for package dependency direction (api->core->queue->target)

#### 5. External Service Isolation Pattern
sidebeam: external.gitlab.{GitLabApiClient|GitLabDataAggregator|GitLabStorageFileRetriever}
           - RetryPolicy interface + SimpleRetryPolicy
           - WebClientConfig for global HTTP settings
           - @ConfigurationProperties for typed config
           - DTO separation (external DTO vs internal DTO)

rag-ingress-queue: target.{RetiredIndexBridgeGateway|HttpRetiredIndexBridgeGateway|RetiredIndexBridgeTargetAdapter}
           - No retry policy
           - Timeout hardcoded in adapter
           - HTTP client created inside adapter
           - @Value injection scattered

Adopt: RetryPolicy interface, global HTTP client config bean, @ConfigurationProperties for RetiredIndexBridge settings, external/internal DTO separation

#### 6. Sensitive Data Masking in Logs
sidebeam: SensitiveDataMaskingConverter (Logback) masks password/token/JWT/email/phone in all logs
rag-ingress-queue: RedactionGuard validates payloads but no runtime log masking for Bearer tokens in HTTP headers

Adopt: Logback pattern converter for runtime masking of API keys, tokens, dataset IDs in logs

#### 7. Correlation ID + MDC
sidebeam: CorrelationIdFilter injects X-Correlation-Id into MDC, reflected in response, used for latency logging
rag-ingress-queue: No distributed tracing identifier

Adopt: CorrelationIdFilter for HTTP requests, propagate to NATS message headers

#### 8. Jacoco Coverage Enforcement
sidebeam: 80% line, 70% branch coverage via jacocoTestCoverageVerification (CI blocking)
rag-ingress-queue: No coverage tooling

Adopt: Add Jacoco plugin with coverage gates

---

## Section 3: rag-ingress-queue Specific Gaps

### Missing from both ACO and sidebeam analysis
- Worker uses fixed targetProfile, ignores message's actual targetProfile
- No @ConfigurationProperties for queue/worker/adapter config (all @Value scattered)
- No structured logging (Logstash encoder)
- No Spring Security (API authentication is open)
- No Circuit Breaker for RetiredIndexBridge adapter
- No runtime metrics/micrometer integration
- DeliveryResult.targetRef is hardcoded "redacted" — opaque handle contract exists but not implemented

---

## Section 4: Proposed Minimal Safe Improvements

### Phase 1 (Low-risk, high-leverage)
1. Package restructure: Move RetiredIndexBridge classes to target.retired_index_bridge/
2. Add ArchUnit architecture test (package dependency direction)
3. Add Jacoco coverage plugin (start with lower threshold, e.g., 60/50)
4. Exception hierarchy: ApplicationException -> Business/Technical/Validation + ErrorCode
5. @RestControllerAdvice for unified error handling

### Phase 2 (Medium-risk)
6. StatusService refactor: depend on RagTargetAdapter.snapshot() only
7. JetStreamProvisioner drift detection fail-closed
8. Add RetryPolicy interface + simple retry for RetiredIndexBridge gateway
9. Add CorrelationIdFilter + MDC propagation
10. THROTTLED slow-fetch behavior

### Phase 3 (Higher complexity)
11. Worker multi-targetProfile routing
12. @ConfigurationProperties for all queue/adapter config
13. SensitiveDataMaskingConverter for runtime log redaction
14. Structured JSON logging (Logstash encoder)
15. Spring Security API key auth (if API exposure scope requires)

---

## Constraint Check
- No live RetiredIndexBridge mutation: All changes are code-only
- No Docker/Compose runtime mutation: No compose.yaml changes
- No broad rewrites: Each phase is additive or small refactor
- Test verification: JAVA_HOME + gradle test + postcheck.sh after each phase
