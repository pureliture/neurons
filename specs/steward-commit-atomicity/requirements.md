# Brain Steward Restricted-Commit Atomicity Requirements

## 승인 대상

- Source of truth: `requirements.md`
- 승인 상태: 사용자가 `requirements.md`/`design.md`를 사전 승인. grilling 질문은 자문자답하며,
  근거는 sonnet read-only 리서치로 확보했다.
- 출처: GitHub issue #49(PR #47 CodeRabbit review에서 파생).

## 배경

Brain Steward의 restricted commit 경로(`stale_commit`, `candidate_reject`, `supersede_commit`,
`candidate_approve`)는 여러 ledger write를 **각각 별도 connection에서 commit**한다. 중간 write가
실패하면 부분 커밋 상태가 남는다 — 예: target은 stale로 demote됐는데 proposal이 큐에 남거나
audit feedback이 누락. 같은 다중-write 패턴은 기존 approve/supersede에도 이미 존재한다.

리서치로 확인한 사실:
- `Ledger._transaction()` context manager(`ledger.py`)와 `_LedgerTransaction` facade가 **이미
  구현**되어 있다. 단일 connection으로 여러 write를 실행하고 블록 종료 시 1회 commit, 예외 시
  rollback한다. sqlite(`ClosingSqliteConnection.__exit__`)와 PostgreSQL(`_PgConnection.__exit__`)이
  같은 시맨틱이라 코드 한 벌로 두 백엔드 모두 커버된다.
- 단 이 seam은 현재 curation 경로만 쓰고, llm_brain restricted commit 경로는 안 쓴다.
  `_LedgerTransaction`에 llm_brain upsert 메서드가 없다.
- ADR-0005가 `Ledger._transaction`을 Brain Steward restricted commit의 rollback 지점으로 이미
  지목했다(`repository.py`의 `rollback_guard.transaction_seam`).
- #48(provider identity)은 restricted commit의 write 순서·횟수를 바꾸지 않았다.

## 질문-답변 흐름 (자문자답 + 리서치 근거)

### Q1. 트랜잭션 seam을 새로 만드나, 기존 것을 재사용하나?

A. **기존 `Ledger._transaction()`/`_LedgerTransaction`을 재사용**한다. 새 UoW/repository port는
ADR-0005가 M2+로 미뤘으므로 범위 밖(YAGNI). public UnitOfWork도 만들지 않는다.

### Q2. `_LedgerTransaction`이 llm_brain upsert를 어떻게 제공하나? 로직 중복 없이?

A. upsert 로직(검증 + content_hash 계산 + SQL)을 **connection-주입 helper**로 추출한다. 기존 public
`upsert_llm_brain_memory_card`/`upsert_llm_brain_feedback_record`는 자기 connection을 열어 helper를
호출하고(동작 불변), `_LedgerTransaction`의 동명 메서드는 공유 connection으로 같은 helper를 호출한다.
중복 없이 단일 connection에서 묶인다. public API 시그니처는 유지(기존 caller 호환).

### Q3. 어느 경로를 원자적으로 묶나?

A. 다중 write를 하는 restricted commit 4경로:
- `BrainStewardService.stale_commit`(3 write: target demote + proposal 종료 + audit)
- `BrainStewardService.candidate_reject`(2 write: rejected card + audit)
- `LLMBrainMemoryService.supersede_accepted_card`(3 write: new accept + audit + old demote)
- `LLMBrainMemoryService.accept_human_approved_candidate`(2 write: accepted card + audit)
단일 write인 `accept_auto_policy_candidate`는 이미 원자적이라 제외(YAGNI).

### Q4. 부분 커밋 방지를 어떻게 증명하나?

A. **rollback characterization test**(ADR-0005 명시)를 둔다. 트랜잭션 중간 write를 실패하게 fault
injection(예: 두 번째 upsert가 raise)한 뒤, target이 demote되지 않고 proposal이 그대로 pending이며
audit이 안 남는 등 **부분 상태가 0**임을 단언한다. 정상 경로 commit 결과(target demote + 큐 제외 +
audit)도 그대로 유지됨을 단언한다.

### Q5. public ledger API나 다른 경로를 바꾸나?

A. 아니다. public 메서드 시그니처 유지, curation transaction 메서드 불변, auto_accept 단일-write
경로 불변, 외부 MCP wire/응답 형태 불변. `_transaction()`은 private 그대로 사용한다(cross-module
private 사용은 repo 관례).

## 기능 요구사항

- FR1. restricted commit 4경로(stale_commit/candidate_reject/supersede_accepted_card/
  accept_human_approved_candidate)의 모든 ledger write가 하나의 트랜잭션으로 묶여 commit된다.
- FR2. 트랜잭션 중간 실패 시 그 경로의 모든 write가 rollback되어 부분 커밋 상태가 남지 않는다.
- FR3. `_LedgerTransaction`이 `upsert_llm_brain_memory_card`/`upsert_llm_brain_feedback_record`를
  공유 connection에서 제공하되 upsert 로직은 중복되지 않는다(connection-주입 helper 재사용).
- FR4. 기존 public `upsert_llm_brain_*` 메서드의 동작과 시그니처는 변하지 않는다.
- FR5. sqlite와 PostgreSQL 백엔드 모두에서 동일하게 동작한다(코드 한 벌).

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| 동작 보존 | 정상 경로 결과(카드 상태/큐/audit) 불변, MCP wire/응답 형태 불변, public ledger API 불변 |
| 백엔드 | sqlite + PostgreSQL 양쪽 동일(중첩 트랜잭션 방지 기존 `_transaction_active` 가드 준수) |
| TDD | code-changing milestone은 red→green→refactor. rollback test는 fault-injection 기반 |
| 테스트 게이트 | `cd worker && uv run pytest -q` 전체 green 유지 |
| 범위 격리 | 새 public UoW/repository port 없음(ADR-0005 M2+), 단일 feature 브랜치 1 PR |
| 안전 | host/token/dataset_id/document_id/raw transcript를 코드·테스트·문서·출력에 쓰지 않음 |

## 사용자 시나리오

- S1. 운영자가 `memory_stale_commit`을 실행하던 중 audit write 직전 프로세스가 실패한다 → target은
  여전히 current(demote 안 됨), proposal은 그대로 pending, audit 없음 — 부분 상태가 전혀 안 남는다.
  재시도하면 정상 완결된다.
- S2. `memory_supersede_commit` 중 old card demote 직전 실패 → new card도 accept되지 않고 old도
  current 그대로 — 두 카드가 동시에 current가 되는 일이 없다.

## 미결정 항목

- 없음(모든 분기 자문자답으로 닫힘). public UnitOfWork/repository port, curation 경로 트랜잭션화,
  auto_accept 단일-write 경로는 명시적 범위 밖(ADR-0005 M2+ / YAGNI).
