# ADR-0002: 컴포넌트 드리븐 레이어드 아키텍처 + 포트-어댑터 패턴

Status: Accepted
Date: 2026-05-17
Deciders: local operator
Related: ADR-0001, docs/superpowers/reviews/backend-architecture-design.html

---

## Context

`rag-ingress-queue` MVP가 Java 25 + Spring Boot 4.x 기반으로 구현되면서, 초기 구조는 기술 계층(계층형) 중심으로 패키지가 분리되었다.

```
api/          → Controller, Service
core/         → Domain, Validation
queue/        → NATS JetStream publish/consume/provision
worker/       → Worker loop, orchestration
target/       → RagTargetAdapter + RAGFlow 구현체 (혼합)
```

이 구조는 기능(도메인) 단위가 아니라 기술 계층을 기준으로 분리되어 있어 다음과 같은 문제를 초래한다.

### 문제점

1. **기능이 패키지에 분산됨**
   - `IngressController`(ingest 기능)와 `StatusService`(status 기능)가 둘 다 `api/`에 있음
   - `IngestWorker`(delivery 기능)는 `worker/`에 따로 있지만 enqueue와의 연결 관계가 패키지로 표현되지 않음

2. **Port와 Adapter가 한 패키지에 섞임**
   - `RagTargetAdapter`(Port, 제네릭 계약)와 `RagFlowTargetAdapter`(Adapter, 구현체)가 둘 다 `target/`에 있음
   - `IngestPublisher`(Port)와 `NatsIngestPublisher`(Adapter)가 둘 다 `queue/`에 있음
   - 새 RAG 타겟이나 큐 엔진을 추가할 때 Port가 Adapter 구현에 묶임

3. **외부 시스템의 종류가 구분되지 않음**
   - NATS(기술 인프라)와 RAGFlow(비즈니스 서비스)가 같은 `queue/`와 `target/`에 있음
   - 인프라 교체와 외부 서비스 교체가 서로 다른 리스크지만 구조상 구분되지 않음

4. **공통 인프라가 기능 패키지에 산재됨**
   - 예외 처리, 로깅, 설정 외부화가 각 기능 패키지에 흩어져 있음
   - 별도의 공통(common) 계층이 없음

### 원인

초기 구현에서 "빠르게 동작하는 것"을 우선시했고, 패키지 구조는 기술 단위로 쪼갰다. 기능이 커지면서 계층 간 경계가 흐려지고, 외부 시스템의 Port/Adapter 분리가 명확하지 않게 되었다.

---

## Decision

**컴포넌트 드리븐 레이어드 아키텍처 + 포트-어댑터 패턴**을 채택한다.

### 설계 원칙

1. **기능(도메인) 단위 패키징** — 기능이 패키지의 최상위 기준이 됨
2. **계층 내부 분리** — 각 기능 안에서 Controller → Service → Domain → DTO 순으로 분리
3. **Port/Adapter 명확 분리** — Port(인터페이스)와 Adapter(구현체)를 별도 패키지에 위치
4. **어댑터 분류** — 인프라 어댑터(기술)와 외부 서비스 어댑터(비즈니스)를 구분
5. **공통 인프라 분리** — 예외, 설정, 로깅, 필터 등을 `common/`에 집중
6. **계층 의존성 규칙** — ArchUnit으로 코드화하여 CI에서 자동 검증

### 확정된 패키지 구조

```
com.local.ragingressqueue
├── ingest/                      ← 인제스트 기능 (enqueue)
│   ├── api/
│   │   └── IngressController.java
│   ├── service/
│   │   └── EnqueueService.java
│   ├── domain/
│   │   ├── IngestJob.java
│   │   ├── DocumentPayload.java
│   │   └── validation/
│   │       ├── IngestJobValidator.java
│   │       └── RedactionGuard.java
│   └── dto/
│       ├── EnqueueRequest.java
│       └── EnqueueResponse.java
├── delivery/                    ← 딜리버리 기능 (worker + target delivery)
│   ├── worker/
│   │   ├── IngestWorker.java
│   │   └── WorkerLoopRunner.java
│   ├── service/
│   │   └── DeliveryService.java       ← targetProfile별 라우팅
│   └── domain/
│       ├── DeliveryDecision.java
│       └── TargetPressure.java
├── status/                      ← 상태 조회 기능 (분리)
│   ├── api/
│   │   └── StatusController.java
│   └── service/
│       └── StatusService.java           ← RagTargetAdapter(Port)만 의존
├── queue/                       ← 큐 포트 (인터페이스만)
│   └── port/
│       ├── IngestPublisher.java
│       └── IngestConsumer.java
├── target/                      ← 타겟 포트 (인터페이스만)
│   └── port/
│       ├── RagTargetAdapter.java
│       └── TargetStatusSnapshot.java
├── adapter/                     ← 어댑터 구현체 (Port의 구현)
│   ├── infra/                   ← 인프라 어댑터 (기술)
│   │   └── nats/
│   │       ├── NatsIngestPublisher.java
│   │       ├── NatsIngestConsumer.java
│   │       ├── NatsJetStreamProvisioner.java
│   │       └── NatsQueueStatusProvider.java
│   └── ext/                     ← 외부 서비스 어댑터 (비즈니스)
│       └── ragflow/
│           ├── RagFlowTargetAdapter.java
│           ├── HttpRagFlowGateway.java
│           ├── RagFlowPressurePolicy.java
│           └── RagFlowDocumentRef.java
└── common/                      ← 공통 인프라
    ├── config/
    │   ├── NatsJetStreamConfiguration.java
    │   └── NatsProperties.java          ← @ConfigurationProperties
    ├── exception/
    │   ├── ApplicationException.java
    │   ├── BusinessException.java
    │   ├── TechnicalException.java
    │   ├── ValidationException.java
    │   ├── ErrorCode.java
    │   └── GlobalExceptionHandler.java
    ├── logging/
    │   └── SensitiveDataMaskingConverter.java
    └── web/
        └── CorrelationIdFilter.java
```

### 계층 의존성 규칙

```
api (표현) → service (응용) → domain (도메인) → port (포트) ← adapter (어댑터)
                ↑                                    ↑
         common (공통) ← 참조 가능            common (공통) ← 참조 가능
```

**허용 의존:**
- api는 service, domain, port를 의존할 수 있음
- service는 domain, port를 의존할 수 있음
- domain은 port를 의존할 수 있음
- adapter는 port를 구현함 (의존 아님, 구현 관계)
- 모든 계층은 common을 참조할 수 있음

**금지 의존:**
- service는 api를 알 수 없음 (순환 참조 방지)
- domain은 adapter를 알 수 없음 (순수성 유지)
- adapter 내부 구현은 다른 adapter에 노출 불가
- port는 adapter를 알 수 없음 (역방향 의존 금지)

### ArchUnit 검증 규칙 (5개)

| # | 규칙 | 검증 내용 |
|---|------|----------|
| 1 | 서비스는 컨트롤러를 알 수 없다 | `..service..` 패키지가 `..api..` 패키지를 의존하면 실패 |
| 2 | 도메인은 어댑터를 알 수 없다 | `..domain..` 패키지가 `..adapter..` 패키지를 의존하면 실패 |
| 3 | 포트는 어댑터를 알 수 없다 | `..port..` 패키지가 `..adapter..` 패키지를 의존하면 실패 |
| 4 | 어댑터는 포트를 구현해야 한다 | `..adapter..` 패키지가 `..port..` 패키지를 의존하지 않으면 실패 |
| 5 | 공통은 순수해야 한다 | `..common..` 패키지가 기능 패키지를 의존하면 실패 |

### 어댑터 분류 규칙

| 구분 | 설명 | 예시 |
|------|------|------|
| 인프라 어댑터 (adapter/infra/) | 기술 인프라를 도메인 방식으로 감쌈 | NATS, Redis, DB 클라이언트 |
| 외부 서비스 어댑터 (adapter/ext/) | 외부 비즈니스 서비스를 도메인 방식으로 감쌈 | RAGFlow, GitLab, 외부 인증 서버 |

핵심 차이:
- **인프라 어댑터**: 우리가 기술을 도메인 방식으로 사용한다
- **외부 서비스 어댑터**: 외부 서비스를 우리 도메인 방식으로 사용한다

---

## Options Considered

### Option A: 현재 구조 유지 (기술 계층 중심)

| Dimension | Assessment |
|---|---|
| Complexity | Low (현재 상태 유지) |
| Migration effort | None |
| Modularity | Low — 기능이 패키지에 분산 |
| Future target swap | Weak — Port/Adapter가 혼합 |

Pros: 당장 아무것도 안 해도 됨.
Cons: 기능 확장 시 계층 간 결합이 강화됨. 새 RAG 타겟 추가 시 `target/` 전체를 수정해야 함.

### Option B: 헥사고날 아키텍처 (Pure Hexagonal)

| Dimension | Assessment |
|---|---|
| Complexity | High |
| Migration effort | High — 모든 코드의 의존 방향을 뒤집음 |
| Modularity | Very High — 진정한 Port/Adapter 분리 |
| Future target swap | Strong |

Pros: 이론적으로 완벽한 모듈러 구조.
Cons: 현재 규모(50개 파일 내외)에 비해 과함. Spring의 @ComponentScan과 충돌 가능성. 팀 학습 곡선.

### Option C: 컴포넌트 드리븐 레이어드 + 포트-어댑터 (선택안)

| Dimension | Assessment |
|---|---|
| Complexity | Medium |
| Migration effort | Medium — 패키지 이동 + import 수정 |
| Modularity | High — 기능 단위 + Port/Adapter 분리 |
| Future target swap | Strong — Port만 구현하면 됨 |

Pros:
- sidebeam-backend의 검증된 패턴을 기반으로 함
- 현재 코드베이스 크기에 적합한 수준의 구조
- Spring Boot와 자연스럽게 결합 (패키지 이동만으로 충분)
- ArchUnit으로 계층 규칙을 코드화하여 자동 검증 가능

Cons:
- 패키지 이동으로 인한 import 수정이 필요 (20+ 파일)
- 테스트 경로 변경 필요
- 초기 마이그레이션 비용

---

## Migration Strategy

마이그레이션은 **단계적**으로 진행한다. 한 번에 전체를 옮기지 않고, 기능 단위로 순차 이동하며 각 단계마다 빌드/테스트를 검증한다.

### Phase 1: 공통 인프라 (common/)
- 예외 계층 생성
- @ConfigurationProperties 클래스 생성
- application.yml 수정
- 검증: `gradle test`

### Phase 2: 포트 분리 (queue/port/, target/port/)
- 인터페이스 파일 이동
- import 수정
- 검증: `gradle test`

### Phase 3: 기능 단위 패키징 (ingest/, delivery/, status/)
- 파일 이동
- import 수정
- 검증: `gradle test`

### Phase 4: 어댑터 이동 (adapter/infra/, adapter/ext/)
- 구현체 이동
- import 수정
- 검증: `gradle test`

### Phase 5: ArchUnit 테스트
- ArchitectureTest.java 작성
- 5개 규칙 구현
- 검증: `gradle test` (규칙 위반 시 실패해야 함)

---

## Consequences

### Positive

- 기능 단위로 코드를 찾기 쉬워짐 (가시성 향상)
- Port/Adapter 분리로 새 타겟/큐 엔진 교체가 용이함
- 계층 의존 규칙이 코드로 강제됨 (ArchUnit)
- 공통 인프라가 한 곳에 집중됨
- sidebeam-backend의 검증된 패턴과 일관성 유지

### Negative

- 마이그레이션 중간에 패키지가 혼재될 수 있음 (짧은 기간)
- import 경로 수정으로 인한 git history 복잡화
- 팀원 학습 필요 (패키지 규칙, ArchUnit)

### Neutral

- Spring Boot의 @ComponentScan은 패키지 이동 후에도 정상 동작 (루트 패키지 기준)
- 테스트는 이동된 패키지 경로만 수정하면 됨

---

## Done Criteria

1. 모든 파일이 확정된 패키지 구조에 위치함
2. `gradle test`가 PASS (ArchUnit 규칙 포함)
3. `gradle bootJar`가 성공
4. HTML 리뷰 문서와 실제 코드가 일치
5. ADR-0002 문서가 코드와 동기화됨

---

## Status Update — 구현 현실 (2026-06-17)

> 이 ADR의 Decision/Done Criteria는 **목표 설계도**로 유지한다(Accepted 결정 본문은 재작성하지 않음).
> 아래는 현재 코드(`main` HEAD)와 위 설계의 차이, 그리고 그에 대한 결정을 기록한다.

### 닫힌 격차 — ArchUnit 강제 (2026-06-17)

- ArchUnit(`com.tngtech.archunit:archunit-junit5:1.4.2`)을 추가하고 「계층 의존성 규칙」을
  `src/test/java/com/local/ragingressqueue/architecture/ArchitectureRulesTest.java`에 5개 규칙으로 코드화했다.
  `gradle test`가 이를 강제한다 → **Done Criteria #2의 "ArchUnit 규칙 포함"이 충족됐다.**
- Rule 1(service↛api)·Rule 2(domain↛adapter)·Rule 3(port↛adapter)·Rule 4(port 구현체는 adapter)는
  현 코드 그대로 통과한다.

### 인정된 편차 (Accepted deviations)

리팩터 리스크 대비 가치가 낮아, 아래는 코드를 ADR에 맞추지 않고 **편차로 인정**한다(필요 시 추후 ADR로 닫는다).

- **Rule 5(common 순수성) 범위 정련.** 원안("common이 기능 패키지를 의존하면 실패")을 그대로 쓰면 현 코드가 위반한다:
  합성 루트 `common.config`(Spring bean 조립)는 본질적으로 feature/adapter를 참조하고
  (`WorkerConfiguration`→`delivery.worker`, `NatsJetStreamConfiguration`→`adapter.infra.nats`),
  `common.logging.SafeJobSummary`는 `ingest.domain` value-type을 참조한다. 따라서 강제 규칙은
  **합성 루트(`common.config`) 면제 + feature domain value-type 공유 허용**으로 정련하고, feature의
  **service/api/worker 로직** 역참조만 금지한다. 합성 루트를 `common` 밖으로 옮기는 일은 별도 과제다.
- **`status/api/StatusController.java` 미생성.** `/status`·`/healthz`는 현재 `ingest/api/IngressController`가
  서빙하고 `StatusService`(status/service)에 위임한다. 전용 컨트롤러 분리는 현 규모에서 보류한다.
- **`common/exception/*` + `GlobalExceptionHandler` 미추출.** enqueue 에러는 `IngressController` 안에서
  inline `ResponseEntity.status(400/409/422/503)`로 처리된다. 예외 계층화는 보류한다.
- **`delivery/service/DeliveryService.java` 미생성.** targetProfile 라우팅은 별도 service로 추출돼 있지 않다.
- **`common/web/CorrelationIdFilter.java` 미구현.**

### 일치하는 골격

기능 단위 패키징(`ingest/`·`delivery/`·`status/`), Port 분리(`queue/port/`·`target/port/`),
어댑터 분류(`adapter/infra/nats/`·`adapter/ext/ragflow/`), 공통 계층(`common/config/`·`common/logging/`).
component-driven + Port/Adapter 방향성은 코드에 반영돼 있고 이제 ArchUnit으로 강제된다.

## References

- sidebeam-backend: `com.sidebeam.bookmark.*`, `com.sidebeam.common.*`, `com.sidebeam.external.gitlab.*` 패턴
- docs/superpowers/reviews/backend-architecture-design.html
- docs/superpowers/reviews/architecture-improvement-review.html
- ACO Run #1: `7155fc88-d2c6-48a8-a414-9ad28c82dbc7`
- ACO Run #2: `fb5a8515-7334-4ad4-bb54-20f8871bd9bb`
