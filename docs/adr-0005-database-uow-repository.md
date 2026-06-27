# ADR-0005: Database Unit of Work와 Repository 경계 도입

## Status
Accepted

## Context

`worker/lib/agent_knowledge/ledger.py`는 SQLite/PostgreSQL 접근, 원시 SQL, 도메인 상태 전이, lifecycle 검증을 한 모듈 안에서 함께 다룬다. 그 결과 기능 추가나 DB backend 전환을 할 때 호출자 compatibility와 transaction safety를 동시에 걱정해야 한다.

현재 architecture modernization campaign의 M1은 큰 분해보다 먼저 transaction failure safety를 characterization test로 고정하고, `Ledger` 내부의 private transaction seam을 만든다. Repository와 공개 `UnitOfWork`는 그 위에서 안전하게 추출할 후속 경계다.

## Decision

- `UnitOfWork`는 서비스 계층이 여러 repository 작업을 하나의 원자적 작업 단위로 묶는 port로 둔다.
- `MemoryCardRepository`, `SessionRepository`, `TranscriptRepository`, `KnowledgeItemRepository`처럼 도메인별 repository port를 정의한다.
- 구체 DB 구현은 adapter로 분리하고, application/service code는 port에 의존하도록 점진 전환한다.
- M1에서는 기존 public API를 유지한 채 private transaction seam과 rollback 증명을 먼저 완료한다.
- 공개 `UnitOfWork`와 repository caller migration은 M2 이후 milestone에서 별도 gate와 테스트를 두고 진행한다.

## Consequences

### Positive

- 비즈니스 흐름과 DB I/O 쿼리 책임을 분리할 수 있다.
- PostgreSQL 중심 runtime으로 이동할 때 service layer 변경 범위를 줄일 수 있다.
- transaction boundary를 명시적으로 테스트하고 추적할 수 있다.

### Negative

- 기존 `ledger.py`의 쿼리와 상태 전이를 domain repository로 나누는 초기 migration 비용이 크다.
- 섣불리 public port를 노출하면 기존 호출자 compatibility와 M1 rollback safety 검증이 흐려질 수 있다.

## Follow-up

- M1: `Ledger` private transaction seam과 rollback characterization tests를 완료한다.
- M2: repository port 후보를 작게 도입하고, `CurationService` 같은 실제 caller를 하나씩 이주시킨다.
- M2 이후: adapter별 contract test를 추가해 SQLite/PostgreSQL behavior drift를 막는다.
