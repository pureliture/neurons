# Repository Extraction M2 Requirements

## 승인 대상

- Source of truth: `requirements.md`
- Preview companion: `requirements.html`
- 승인 상태: 사용자 사전 승인. 본 문서는 자문자답으로 확정한 Phase 1 source다.

## 질문-답변 흐름

### Q: 이번 실제 리팩은 audit의 어느 후속 후보를 구현 대상으로 삼을까?

기존 승인 트랙인 M2 Repository extraction을 구현 대상으로 삼는다.

audit의 F-001은 ADR-0005와 `docs/specs/2026-06-26-architecture-modernization-campaign/design.md`가 이미 승인한 후속 트랙이다. 이번 작업은 HTML sketch를 새 source of truth로 삼지 않고, 기존 M1 private transaction seam 위에 첫 repository port를 실제 caller에 적용한다.

### Q: M2의 첫 extraction 범위는 어디까지인가?

`CurationService.approve`의 multi-write path를 첫 production caller migration으로 정한다.

`approve`는 memory card write, evidence write, candidate approval, optional profile fact write를 한 흐름에서 수행한다. M1에서 이미 rollback safety 대상이었으므로 repository extraction의 첫 실제 적용점으로 적합하다. `reject`, `disable`, `supersede`는 public compatibility behavior를 유지하고, 이번 작업에서 대량 caller migration으로 확장하지 않는다. `supersede`는 old card demotion과 new card approval을 함께 수행하므로 다음 multi-write migration 후보로 기록하되, 이번 M2 first caller migration에서 transaction-safe로 주장하지 않는다.

### Q: repository port는 무엇을 소유하나?

`MemoryCurationRepository`가 curation-owned write flow를 소유한다.

구현체는 기존 `Ledger`와 transaction-bound facade를 감싸며, SQL 또는 DB adapter shape를 새로 만들지 않는다. `MemoryCurationRepository`는 low-level Ledger proxy가 아니라 `approve_candidate` use-case port다. `ILedgerCoreDbAdapter`는 계속 connection seam으로 남고, public `UnitOfWork`나 public `ledger.transaction()`은 이번 범위 밖이다.

### Q: production 검증은 어디까지 할까?

로컬 테스트와 read-only production/runtime evidence까지 수행한다.

코드 구현 뒤 worker focused tests, relevant broader tests, root service checks를 실행한다. production 검증은 read-only 상태 확인으로 제한하며, 배포하지 않은 새 code path의 runtime activation을 증명했다고 표현하지 않는다. 배포, migration, GC 실행, Docker/systemd 변경, credential 변경, live data mutation은 별도 승인 없이는 하지 않는다.

### Q: 중간 품질 보정은 어떻게 할까?

구현 전후로 architecture review와 simplification review를 분리한다.

`codebase_architecture_manager`와 `system_architecture_manager`는 repository boundary, public compatibility, production/runtime proof boundary를 검토한다. `code_simplifier`는 구현 후 변경 파일의 clarity와 과잉 추상화를 검토한다.

## 기능 요구사항

- `CurationService.approve`는 `MemoryCurationRepository` port를 통해 curation-owned write flow를 수행한다.
- 기본 동작은 기존 `CurationService(ledger)` call site와 호환되어야 한다.
- 새 repository 구현은 기존 `Ledger._transaction()` rollback guard를 사용해 partial write를 방지한다.
- `approve`가 쓰는 memory card, evidence, candidate approval, optional profile fact write는 동일 transaction 안에서 실행되어야 한다.
- 기본 `LedgerMemoryCurationRepository`는 `Ledger._transaction()`이 없으면 fail-closed 해야 한다.
- repository extraction은 `ILedgerCoreDbAdapter`를 query/transaction adapter로 바꾸지 않는다.
- `reject`, `disable`, `supersede`의 existing behavior는 유지한다.
- `supersede`는 다음 multi-write repository migration 후보로 metadata에 남기고, 이번 milestone에서 transaction-safe 완료 상태로 표시하지 않는다.
- public `UnitOfWork` API 또는 public `ledger.transaction()`은 만들지 않는다.
- repository readiness metadata는 readiness-only에서 actual first caller migration 상태를 표현하되, public import contract 또는 stable protocol 완료로 표시하지 않는다.
- raw transcript, raw dataset_id, raw document_id, token, credential은 출력하지 않는다.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| Source control | `main`이 아니라 `codex/repository-extraction-m2` branch와 전용 worktree에서만 수정한다. |
| TDD | code-changing milestone은 failing/changed test를 먼저 만들고 red -> green -> refactor로 진행한다. |
| Compatibility | 기존 `CurationService(ledger)` public construction과 existing behavior를 유지한다. |
| Boundary | SQL dialect, DB adapter, public UnitOfWork, REST/gRPC Ledger Core API는 이번 범위 밖이다. |
| Safety | production-facing mutation, deployment, GC execute, credential edit, Docker/systemd mutation은 하지 않는다. RAGFlow/RetiredIndexBridge client construction, secret/env read, PUT/POST/DELETE, GC command도 수행하지 않는다. |
| Production proof | read-only runtime/source evidence만 수행한다. 새 code path activation proof가 아니라 non-mutation/current-runtime divergence 확인으로 보고한다. |
| Review | 요청된 3개 subagent role을 중간 품질 보정에 사용한다. |
| Language | 자연어 문서와 보고는 한국어로 작성하고 코드 식별자는 영문 유지. |

## 사용자 시나리오

- Maintainer가 `CurationService.approve`를 읽을 때 business flow가 repository port를 통해 보이고, Ledger SQL/mixin 세부가 service에 직접 퍼지지 않는다.
- 다음 M2 구현자가 `reject`, `disable`, `supersede`를 어떤 순서와 guard로 옮길지 `repository.py`의 상태를 보고 판단한다.
- 운영자는 production 검증 결과를 live mutation과 분리해서 신뢰한다.

## 미결정 항목

- 없음. 사용자가 `requirements.md`와 `design.md`를 모두 사전 승인했다.
