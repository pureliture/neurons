# Repository Extraction M2 Design Spec

## Overview

M2의 첫 실제 repository extraction은 `CurationService.approve`를 `MemoryCurationRepository` port 뒤로 옮기는 것이다. 기존 Ledger public API와 M1 private transaction seam은 유지하고, service layer가 Ledger write 세부에 직접 의존하는 면을 줄인다.

## Requirements Reference

- Phase 1 source: `requirements.md`
- Preview companion: `requirements.html`
- 승인 상태: 사용자 사전 승인
- 핵심 요구사항:
  - `CurationService.approve`의 curation-owned write flow를 repository port로 이동한다.
  - existing `CurationService(ledger)` call site와 behavior를 유지한다.
  - M1 rollback guard인 `Ledger._transaction()`을 계속 사용한다.
  - public `UnitOfWork`, public `ledger.transaction()`, REST/gRPC Ledger Core API, DB adapter shape 변경은 제외한다.
  - production 검증은 read-only evidence로 제한한다.

## Approach

추천 접근은 **default adapter repository**다.

1. 추천: default adapter repository
   - `MemoryCurationRepository` protocol을 `approve_candidate` use-case port로 확장하고, `LedgerMemoryCurationRepository`가 `Ledger`와 `_LedgerTransaction`을 감싼다.
   - `CurationService`는 기본 생성 시 이 repository를 만들어 사용한다.
   - 장점: 기존 caller 호환성 유지, M1 transaction seam 재사용, test seam 명확.
   - 단점: `CurationService`가 당분간 read용 `ledger`와 write용 repository를 함께 들고 있다.

2. 대안: public UnitOfWork 선도입
   - service가 public UoW에 의존하게 한다.
   - 장점: 장기 목표와 더 가까움.
   - 단점: ADR-0005의 M2 이후 gate를 앞당기며 public API 위험이 크다.

3. 대안: repository만 만들고 caller migration 보류
   - 장점: 위험 낮음.
   - 단점: 이번 목표인 실제 리팩이 아니라 readiness-only 반복이다.

이번 design은 1번을 채택한다.

## Architecture

```text
CurationService
  -> MemoryCurationRepository
    -> LedgerMemoryCurationRepository
      -> Ledger._transaction()
        -> _LedgerTransaction
          -> existing Ledger SQL/mixin semantics
```

Dependency direction:

```text
session_memory.curation -> repository port -> Ledger private transaction seam -> DB adapter connect seam
```

`CurationService`는 business flow를 유지하되, approval write orchestration을 repository method로 위임한다. `LedgerMemoryCurationRepository`는 low-level Ledger proxy가 아니라 `approve_candidate` use-case adapter이며, SQL을 새로 작성하지 않고 existing Ledger/transaction-bound facade method를 호출한다. `MemoryCurationRepository`는 이번 milestone의 active use-case seam이지 stable public import contract가 아니다.

## Data Flow

### Approve candidate

```text
CurationService.approve(candidate_id, approved_by)
  -> ledger.get_memory_candidate(candidate_id)
  -> build_memory_card(candidate)
  -> repository.approve_candidate(candidate, card, approved_by)
    -> Ledger._transaction()
      -> upsert_memory_card
      -> add_memory_card_evidence
      -> update_memory_candidate_state("approved")
      -> optional upsert_profile_fact
  -> return stored card
```

### Non-target paths

```text
reject / disable / supersede
  -> existing Ledger calls
  -> behavior preserved
```

`supersede`는 old card demotion과 new card approval을 결합한 다음 multi-write migration 후보로 남긴다. 이번 milestone은 `approve`에 대해서만 transaction-safe repository path를 주장하며, `supersede`의 unchanged behavior를 transaction-safe 완료로 해석하지 않는다.

## Component Details

### `MemoryCurationRepository`

- 입력: candidate/card dictionaries and review metadata.
- 출력: stored memory card or updated row mappings.
- 의존성: none at Protocol level.
- 책임: curation-owned write flow를 service에서 분리한다.

### `LedgerMemoryCurationRepository`

- 입력: `Ledger` instance.
- 출력: existing Ledger-compatible mapping rows.
- 의존성: `Ledger._transaction()`.
- 책임:
  - `approve_candidate` multi-write를 한 transaction으로 묶는다.
  - `Ledger._transaction()`이 없으면 fail-closed 한다.
  - SQL/DB adapter abstraction을 새로 만들지 않는다.

### `CurationService`

- 입력: existing `ledger`, optional `repository`.
- 출력: existing return values.
- 의존성: `MemoryCurationRepository` for approval write path.
- 책임:
  - candidate read and memory card build를 유지한다.
  - approval write sequence는 repository에 위임한다.
  - `reject`, `disable`, `supersede`는 이번 milestone에서 existing behavior를 유지한다.

### `repository.py` metadata

- `build_repository_extraction_plan()`은 `readiness_only`가 아니라 first caller migration status를 보여준다.
- `public_import_contract`와 `protocol_definition_stable`은 `False`로 유지한다.
- `next_multi_write_candidate`는 `CurationService.supersede`를 가리키며, transaction-safe claim은 `False`다.
- public compatibility gate와 abort criteria는 유지한다.

## Error Handling

- unknown candidate는 기존처럼 `ValueError`를 던진다.
- `approve(..., supersedes=...)`는 기존처럼 `ValueError("use supersede...")`를 던진다.
- repository approval 중 예외가 발생하면 `Ledger._transaction()` rollback semantics가 partial writes를 제거해야 한다.
- 기본 `LedgerMemoryCurationRepository`는 transaction seam이 없으면 `RuntimeError`로 fail-closed 한다. test double compatibility는 `CurationService(..., repository=...)` injection seam으로만 제공한다.
- SoT 변경이 필요해지면 agentic-execution 안에서 design을 고치지 않고 grill-to-spec으로 회귀한다.

## Testing Strategy

- 새 repository unit tests:
  - `LedgerMemoryCurationRepository.approve_candidate`가 card/evidence/candidate/profile fact를 저장한다.
  - evidence write failure가 partial card state를 rollback한다.
  - transaction seam이 없으면 default repository가 fail-closed 한다.
- service integration tests:
  - `CurationService.approve`가 injected repository를 사용한다.
  - 기존 `test_curation.py` behavior가 유지된다.
- metadata tests:
  - repository extraction plan이 actual first caller migration을 반영한다.
  - repository extraction plan이 stable public contract를 과장하지 않고 `supersede` residual risk를 남긴다.

## TDD Strategy

1. repository behavior와 service injection test를 먼저 추가해 실패를 확인한다.
2. `LedgerMemoryCurationRepository`와 `CurationService` wiring을 구현한다.
3. focused tests를 통과시킨다.
4. broader worker tests와 root checks를 실행한다.
5. read-only production/runtime proof를 수행한다.

## Milestones

- M1: Spec and baseline lock
  - Done: `requirements.md`, `requirements.html`, `design.md`가 생성되고 placeholder scan이 통과한다.
  - Expected evidence: artifact files and self-review search.
- M2: Repository port activation
  - Done: `CurationService.approve`가 repository port를 통해 writes를 수행하고 rollback safety가 유지된다.
  - Expected evidence: focused red-to-green repository/curation tests.
- M3: Compatibility and review hardening
  - Done: existing curation/ledger behavior가 유지되고 requested review roles에서 나온 actionable issue가 반영되거나 명시적으로 non-actionable 처리된다.
  - Expected evidence: relevant worker tests and review summaries.
- M4: Production read-only verification
  - Done: local tests/checks와 read-only production/runtime evidence가 분리되어 보고된다.
  - Expected evidence: worker pytest, root check where available, read-only runtime non-mutation/divergence proof. Deployment not performed and new code path activation not runtime-verified unless a later deployment is explicitly approved.

## Open Questions

None.

## Design Self-Review

- approved M2 scope인 repository extraction에만 집중한다.
- public UnitOfWork와 public `ledger.transaction()`은 만들지 않는다.
- `ILedgerCoreDbAdapter` shape를 바꾸지 않는다.
- production 검증은 read-only evidence로 제한한다.
- production read-only evidence는 activation proof가 아니라 현재 runtime divergence와 non-mutation proof로 보고한다.
- RAGFlow/RetiredIndexBridge client construction, secret/env read, PUT/POST/DELETE, GC command는 수행하지 않는다.
- code-changing milestone은 TDD-first로 실행한다.
