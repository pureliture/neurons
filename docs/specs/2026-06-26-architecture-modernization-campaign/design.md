# Architecture Modernization Campaign Design Spec

## Overview

`neurons`의 persistence modernization을 실행 가능한 campaign으로 설계한다. M1은
`ledger.py`를 크게 분해하기 전에 transaction failure safety를 테스트로 고정하고,
기존 behavior를 유지한 채 UoW seam을 도입한다.

## Requirements Reference

- Phase 1 source: `requirements.md`
- Preview companion: `requirements.html`
- 승인된 접근: Evidence-gated staged campaign

핵심 요구사항:

- M1은 실제 구현과 검증까지 완료한다.
- M1의 첫 safety surface는 transaction failure safety다.
- M1 증명 순서는 Ledger 직접 레벨 -> Service 레벨이다.
- M1은 DB migration, 대규모 책임 이동, live runtime mutation을 포함하지 않는다.
- M2/M3는 구현자가 바로 착수할 수 있을 만큼 `design.md`에서 구체화한다.
- 검증은 로컬 worker test와 read-only Ubuntu runtime check로 제한한다.

## Approach

### Evidence-gated staged campaign

M1을 가장 깊게 설계해 실제 구현 가능한 단위로 만들고, M2/M3는 다음 구현자가 바로
이어갈 수 있는 gate, done 정의, expected evidence를 갖춘 milestone으로 준비한다.

선택 이유:

- `ledger.py`와 mixin들은 이미 넓은 persistence surface를 갖고 있어, 큰 분해 전에
  실패 안전성을 먼저 고정해야 한다.
- 기존 `ILedgerCoreDbAdapter`는 connection seam이므로, M1의 transaction/UoW seam은
  이를 대체하지 않고 위에 얹어야 한다.
- `CurationService.approve`는 여러 ledger write를 순차 호출하므로 Service-level
  transaction failure safety를 증명하기 좋은 첫 use case다.
- M1 구현 범위를 작게 유지하면서도 Repository extraction과 dataset contract/config
  workstream으로 이어지는 판단 기준을 남길 수 있다.

## Current Code Surface

- `worker/lib/agent_knowledge/ledger.py`
  - `Ledger`는 여러 mixin을 합성하고 `_connect()`를 통해 DB connection을 연다.
  - `ILedgerCoreDbAdapter` / `SqliteLedgerDbAdapter`는 이미 connection creation seam이다.
- `worker/lib/agent_knowledge/ledger_memory_promotion_mixin.py`
  - `upsert_memory_candidate`, `update_memory_candidate_state` 등 candidate state write를
    소유한다.
- `worker/lib/agent_knowledge/ledger_native_memory_mixin.py`
  - `upsert_memory_card`, `add_memory_card_evidence`, `update_memory_card_state`,
    `upsert_profile_fact` 등 native memory/card write를 소유한다.
- `worker/lib/agent_knowledge/session_memory/curation.py`
  - `CurationService.approve`는 candidate read, card write, evidence write,
    candidate state update, optional profile fact write를 순차 실행한다.
- `worker/tests/test_ledger_source_ref_register_all_rollback.py`
  - existing rollback characterization pattern을 제공한다.
- `worker/tests/test_curation.py`
  - Service-level behavior baseline을 제공한다.

## Architecture

M1 UoW seam은 Ledger-owned transaction context로 둔다. API exposure는 private first다.
`Ledger`가 private transaction context를 열고, transaction-bound facade가 같은 connection을
공유하며 기존 write operation을 수행한다. 기존 public `ledger.*` 호출은 유지하고,
M1 target인 service 내부 multi-write use case만 private transaction context를 사용한다.

```text
CurationService
  -> Ledger._transaction()
    -> _LedgerTransaction / private transaction-bound facade
      -> shared sqlite/postgres connection from ILedgerCoreDbAdapter
      -> existing Ledger write semantics
```

Dependency direction:

```text
session_memory.curation -> Ledger private transaction seam -> DB adapter
existing callers -> Ledger public API
```

`ILedgerCoreDbAdapter`는 connection creation seam으로 유지한다. M1은 이 adapter를
대체하지 않고, adapter가 제공한 connection 위에 transaction lifetime seam을 추가한다.

## Data Flow

### Ledger 직접 레벨 failure safety

```text
test opens Ledger private transaction seam
  -> first write succeeds on shared transaction connection
  -> second injected write fails
  -> transaction context rolls back
  -> fresh read confirms first write did not survive
```

### Service 레벨 failure safety

```text
CurationService.approve
  -> reads candidate before mutation
  -> opens Ledger private transaction seam
  -> writes memory card
  -> writes evidence
  -> injected failure before candidate approval/profile fact
  -> transaction context rolls back
  -> fresh read confirms no card/evidence/profile partial state
```

M1 service target is `approve` only. `supersede`, `disable`, and `reject` keep their current
behavior in M1 and become follow-up atomicity candidates after the private transaction seam is
proven.

## Component Details

### `Ledger._transaction`

- 입력: none, optional future flags only if needed by implementation.
- 출력: context manager yielding private transaction-bound ledger facade.
- 의존성: existing `_connect()` and adapter-provided connection.
- 책임: commit/rollback lifetime, shared connection ownership, close behavior.
- 공개 범위: private M1 seam. public UoW API는 M1 범위 밖이다.

### `_LedgerTransaction`

- 입력: active connection.
- 출력: subset of Ledger write/read methods needed by M1.
- 의존성: existing SQL semantics from Ledger/mixins.
- 책임: M1 대상 operation이 새 connection을 열지 않고 active transaction connection을
  사용하게 한다.
- 공개 범위: module-private helper. 외부 caller가 직접 의존하지 않는다.

### `CurationService`

- 입력: existing Ledger-compatible object.
- 출력: existing return values.
- 의존성: Ledger public API.
- 책임: M1에서는 `approve`만 transaction context로 감싼다. 다른 service methods는
  compatibility tests로 현행 behavior를 보존한다.

M1은 모든 Ledger method를 transaction-aware로 바꾸지 않는다. transaction-bound facade는
M1 safety target에 필요한 최소 method subset부터 시작한다. `ledger.transaction()`이나
공개 `UnitOfWork` Protocol은 M2 이후 Repository extraction readiness에서 다시 판단한다.

## Error Handling

- transaction block 내부 예외는 rollback 후 원래 예외를 그대로 전파한다.
- rollback 중 예외가 발생하면 원래 실패 원인을 가리지 않도록 bounded error reporting을
  유지한다.
- nested transaction은 M1에서 지원하지 않는다. 감지 시 fail closed 한다.
- read-only Ledger에서 write transaction 요청은 기존 read-only write failure 성격을
  유지하거나 더 이른 명시적 error로 실패한다.
- live RAGFlow, Docker/systemd, credential, raw transcript/source mutation은 M1 error
  handling 범위 밖이다.

## Testing Strategy

- Ledger 직접 레벨 failure injection test
  - first write가 실제로 실행된 뒤 second write 실패를 주입한다.
  - fresh Ledger read로 partial state가 남지 않았음을 확인한다.
- Service 레벨 `CurationService.approve` failure injection test
  - memory card/evidence/candidate approval/profile fact 중간 지점 실패를 주입한다.
  - candidate는 pending 상태로 남고 card/evidence/profile partial state가 없어야 한다.
  - `supersede`, `disable`, `reject`는 M1 atomic target이 아니므로 기존 behavior regression
    test로만 보호한다.
- Compatibility tests
  - 기존 `test_curation.py` 승인/거절/disable/supersede behavior 유지.
  - 기존 `test_ledger_core.py` lifecycle behavior 유지.
  - 기존 `test_db_adapter.py` connection adapter seam 유지.
- Evidence checks
  - focused worker tests.
  - broader relevant worker tests.
  - read-only Ubuntu runtime status check, no live mutation.

## TDD Strategy

M1은 red -> green -> refactor 흐름을 따른다.

1. Ledger 직접 레벨에서 실패 주입 characterization test를 먼저 작성한다.
2. test가 현재 partial write 위험 또는 transaction boundary 부재를 드러내는지 확인한다.
3. 기존 behavior를 유지하면서 UoW seam을 도입한다.
4. `CurationService.approve` 흐름에 Service-level failure injection test를 추가한다.
5. 기존 `test_curation.py`, `test_ledger_core.py`, `test_db_adapter.py`와 관련 worker test를
   함께 통과시킨다.

## Milestones

- M1: Ledger persistence safety harness and UoW seam
  - Done: Ledger 직접 레벨과 Service 레벨 transaction failure safety가 테스트로 고정되고,
    기존 caller behavior가 유지된다.
  - Expected evidence: focused worker tests, relevant existing curation/ledger tests,
    read-only Ubuntu runtime check.
- M2: Repository extraction readiness
  - Scope: M1 transaction seam 위에서 `memory_candidates`, `memory_cards`,
    `memory_card_evidence`, `profile_facts`를 첫 repository extraction 후보로 평가한다.
  - Done: 첫 Repository 후보, caller migration order, rollback guard, public compatibility
    gate가 구현 직전 수준으로 구체화된다.
  - Expected evidence: M1 tests green, repository candidate method matrix, compatibility
    fixtures for existing `CurationService` behavior.
  - Abort criteria: extraction이 기존 `ledger.*` caller를 대량 변경하거나 public API break를
    요구하면 별도 requirements/design으로 회귀한다.
- M3: Dataset contract immutable config readiness
  - Scope: `dataset_contract.py`의 `LogicalDatasetRole`, canonical names, current runtime
    names, deprecated prefixes, target profiles, document kinds를 load-once config로 이동할
    준비를 한다.
  - Done: config schema, code-defined default fallback, startup validation, compatibility
    fixture, orchestration-driven rollout/abort criteria가 구현 직전 수준으로 구체화된다.
  - Expected evidence: generated config fixture matching current Python constants, validation
    test plan, no hot reload requirement, no secret/raw dataset id exposure.
  - Abort criteria: runtime hot reload, credential access, live RAGFlow mutation, or semantics
    drift가 필요해지면 별도 requirements/design으로 회귀한다.

## Open Questions

- None.
