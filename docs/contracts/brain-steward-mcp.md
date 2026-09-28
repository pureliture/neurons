# Brain Steward MCP Surface (proposal-only)

Hermes 같은 agent가 `neurons`의 authoritative memory를 **직접 바꾸지 않고** 안전하게
관리 후보(candidate)와 proposal을 남길 수 있게 하는 agent-facing MCP 표면이다.

목표는 자가확정(self-acceptance)이 아니라 **proposal-only 안전성**이다. Hermes는 후보를
만들고 stale/supersede를 제안할 수 있지만, 무엇이 authoritative memory가 되는지는 사람(또는
명시적 gate)이 restricted 경로로 확정한다.

> Hardening 반영: 승인된 spec `specs/brain-steward-hardening/`에 따라 supersede/stale
> **완결 경로**(`memory_supersede_commit`/`memory_stale_commit`), stale의 **reference-only
> 저장**, **granular restricted 권한 + commit audit**, dispatch/모델 중복 제거가 추가됐다.

## Relationship to context-authority-roadmap

`specs/context-authority-roadmap/design.md`는 Hermes를 read-only consumer로 두고 proposal
loop를 범위 밖으로 둔다. proposal-only Brain Steward는 그 핵심 불변("agent는 authority를
결정하지 않는다")을 **위반하지 않는다** — agent는 후보·제안만 만들고 authoritative truth
(accepted+current) 변경은 restricted human/manual gate에서만 일어난다. 따라서 이 표면은 roadmap의
sanctioned proposal-only extension이며, SoT는 `specs/brain-steward-hardening/`이다.

## Authority model

- 승인 카드 steward lane의 authority는 PostgreSQL `memory_cards` + `steward_card_decisions`다. Qdrant/graph/RetiredIndexBridge는 canonical authority가 아니다. 세션 ledger와 별도 레거시 카드 lane은 이번 전환 대상이 아니다.
- **accepted + current** MemoryCard만 authoritative memory다.
- candidate/proposal은 recall에서 정답처럼 쓰이지 않는다. authority pack에 포함되지 않는다.
- stale/superseded memory는 authority pack에서 빠지고, review queue에서 downgraded
  evidence로만 노출된다.
- 이 표면은 기존 `brain_context_resolve` read path와 충돌하지 않는다. authority pack은
  PG steward lane의 accepted+current card만 직접 읽는 별도 read다.

## Tool classification

| Tool | Class | 효과 |
| --- | --- | --- |
| `memory_authority_pack_read` | read | accepted+current MemoryCard만 redacted view로 반환 |
| `memory_review_queue_list` | read | pending candidate/stale/supersede proposal을 redacted view로 반환 |
| `memory_candidate_create` | proposal | 새 MemoryCard 후보를 candidate(또는 needs_review)로 생성 |
| `memory_stale_mark` | proposal | 특정 MemoryCard가 stale하다는 proposal 생성(원본 불변) |
| `memory_supersede_propose` | proposal | 기존 MemoryCard를 새 후보로 대체하자는 proposal 생성(즉시 교체 안 함) |
| `memory_candidate_approve` | restricted (review_commit) | candidate를 accepted authoritative memory로 승격 |
| `memory_candidate_reject` | restricted (review_commit) | candidate를 rejected로 확정(audit feedback record) |
| `memory_supersede_commit` | restricted (review_commit) | supersede proposal 확정: 교체 후보 accept + 기존 card superseded로 demote |
| `memory_stale_commit` | restricted (review_commit) | stale proposal 확정: 대상 accepted card를 currentness=stale로 demote(audit) |
| `memory_candidate_auto_accept` | restricted (auto_accept) | auto-accept 정책으로 candidate 승격(별도 capability) |

### Read tools

두 read tool은 안전한 필드만 projection한다. 다음은 **절대 반환하지 않는다**:
raw transcript, raw `dataset_id`, raw `document_id`, private path, secret/token/cookie/bearer,
raw source locator(`source_refs`/`typed_payload`/`render_text`/`envelope_json`).

반환은 redacted summary/title, enum 상태(lifecycle/approval/currentness/freshness/governance_tier),
opaque `memory_id`, sha256 evidence hash 개수, source ref 개수, 그리고 proposal의 경우
`proposal_kind`/`target_memory_id`/`supersedes`로 제한된다. 응답은 마지막에
`assert_public_safe`로 fail-closed 검증된다.

#### Output 안전성 hardening

- write 경계: `validate_memory_card_envelope`는 `summary`/`render_text`뿐 아니라
  agent-supplied free text인 `title`/`confidence_basis`도 forbidden-content scan한다.
  `/Users/`·`~/`·`/private/`·`/Volumes/` private path, raw transcript, Bearer/secret
  assignment가 들어간 card는 PG steward 저장소에 **저장되지 않는다**.
- proposal write: `_persist_proposal`은 PostgreSQL 저장 **이전에** review-queue projection을
  `assert_public_safe`로 검증한다. 안전하지 않은 proposal은 한 줄도 저장되지 않으므로 review
  queue 읽기를 망가뜨리지 않는다.
- 잔여 한계: redacted summary/title는 agent-authored free text다. 무작위 hex처럼 보이는
  raw dataset_id/document_id **값**이나 bare host:port는 denylist로 구분할 수 없다. 이에 대한
  방어는 denylist가 아니라 structural projection이다 — 두 read tool은 `source_refs`/
  `typed_payload`/`envelope_json` 같은 raw ref를 애초에 반환하지 않는다.

### Proposal tools

proposal tool은 항상 **non-accepted lifecycle**(candidate/needs_review)로만 PostgreSQL steward lane에 남는다.

- 후보 `memory_id`는 `mem_steward_` prefix로 발급되어 accepted/miner candidate id와 분리된다.
- proposal은 idempotent하다(같은 입력 → 같은 후보).
- `memory_candidate_create`는 accepted memory를 만들지 않는다.
- `memory_stale_mark`는 대상 card를 삭제하거나 수정하지 않는다. stale proposal은 대상의 raw
  payload를 **복제하지 않는** reference-only record다(`card_type=status`, `source_refs`/
  `evidence_refs`/`evidence_hashes`는 빈 리스트, `typed_payload.current_authority`와
  `derived_from`로 대상만 참조). proposal id는 `(target, reason)`에 멱등이라 서로 다른 reason은
  서로 다른 proposal이 된다.
- `memory_supersede_propose`는 기존 card를 즉시 교체하지 않는다. supersede 의도는 새 후보의
  `supersedes`에 기록되고, 대상 card는 accepted+current 그대로 남는다.
- `_persist_proposal`은 accepted card를 덮어쓰는 어떤 write도 fail-closed로 거부한다.

### Completion (restricted commit)

proposal은 restricted commit으로만 authoritative truth가 된다 — 따라서 review queue에 영구
누적되지 않는다.

- `memory_supersede_commit`은 supersede proposal을 확정해 교체 후보를 accept하고 기존 card를
  `currentness=superseded`(+`superseded_by`)로 atomically demote한다. proposal은 accept되며
  review queue를 떠난다.
- `memory_stale_commit`은 stale proposal을 확정해 대상 accepted card를 `currentness=stale`로
  demote한다. proposal은 committed 상태로 전이되어 queue를 떠나고, 대상은 현재 authority/recall lane에서
  빠진다. 결정 감사와 당시 반환 결과는 PG에 저장된다.
- 승인 시 `valid_from`을 결정 시각으로 설정하고 교체/stale 시 이전 카드의 `valid_to`를 닫는다.
  결정 ID 재시도는 이후 카드 상태가 바뀌어도 당시 감사 결과를 반환한다.
- 완결 전이에는 `memory_promotion`(`commit_supersession`/`commit_stale`)과
  `LLMBrainMemoryService.supersede_accepted_card`의 상태 전이 함수를 재사용하지만 저장은 PG 트랜잭션이다.

`memory_candidate_create` / `memory_supersede_propose`의 입력은 raw transcript가 아니라
**redacted source_span**이다: `card_type`, `project`, `provider`, `typed_payload`,
sha256 `content_hash`, opaque `source_ref`/`span_ref`, 그리고 `redacted_summary`.

### Restricted tools (기본 차단, granular)

restricted tool은 Hermes가 바로 쓰면 안 된다. 기본 권한에서는 **어떤 write도 수행하지 않고
거부**한다(`{"permission":"denied", "write_performed": false}`). 권한은 **capability별로 분리**된다:

- `review_commit` — `approve`/`reject`/`supersede_commit`/`stale_commit`.
  `KnowledgeSearchService(allow_restricted_steward=True)` 또는
  `BrainStewardService(allow_restricted=True)`로 연다.
- `auto_accept` — 가장 위험한 `auto_accept`는 **별도 capability**다.
  `KnowledgeSearchService(allow_steward_auto_accept=True)` 또는
  `BrainStewardService(allow_auto_accept=True)`로만 열리며, review_commit 허용만으로는 열리지
  않는다. `operator_approval_ref`가 비어 있으면 `apply_auto_acceptance_plan`이 차단한다.

두 capability 모두 기본값 `False`다. 부족하면 `StewardPermissionError`가 PG write
**이전에** raise된다. authority를 바꾸는 commit(approve/reject/stale_commit/supersede_commit)은
PG의 `steward_card_decisions`에 감사와 결정 당시 결과를 남긴다. SQLite
`list_llm_brain_feedback_records`는 이 steward 결정의 권위 조회가 아니다.
identity/permission profile 바인딩은 transport 계층 책임으로 범위 밖이다.

## Code map

- 서비스/proposal·read 로직: `worker/lib/agent_knowledge/session_memory/brain_steward.py`
- tool contract(schema): `worker/lib/agent_knowledge/mcp_tools.py`
- JSON-RPC dispatch: `worker/lib/agent_knowledge/mcp_jsonrpc.py`
- steward PG 권위/감사/review queue: `worker/lib/agent_knowledge/postgres_store/pgvector_store.py`
  (`memory_cards.steward_envelope`, `steward_card_decisions`)
- 서비스 wiring/flag: `worker/lib/agent_knowledge/knowledge_search_service.py`
- 재사용한 상태 전이: MemoryCard envelope/validation(`session_memory/memory_card.py`),
  candidate builder(`session_memory/memory_miner.py`), promotion(`session_memory/memory_promotion.py`,
  `session_memory/llm_brain_service.py`)
- 완결/stale 상태 함수: `session_memory/memory_promotion.py`(`commit_stale`, `build_stale_proposal_card`,
  `commit_supersession`)
- 공유 lifecycle 상수: `session_memory/memory_card.py`(`REVIEW_LIFECYCLE_STATES`)
- spec(SoT): `specs/brain-steward-hardening/{requirements,design}.md`
- tests: `worker/tests/test_brain_steward.py`

## PG 단일 카드 권위 전환 (PR #271 수정 중)

**현재 코드의 steward 카드 경로는 PG 단일 권위**로 후보·검토·결정 감사·교체·stale·조회 및 outbox를 처리한다. 카드·감사·outbox 상태 전이는 하나의 PG 트랜잭션이며, 승인 전 후보는 outbox에 들어가지 않는다. 이 구현은 운영에 아직 배포되지 않았으므로 운영 동작으로 해석하면 안 된다. 정확한 범위와 운영 컷오버 경계는 `specs/lbrain-architecture-rationalization/approved-card-pg-single-store.md`를 따른다. 기존 SQLite 카드의 이관·삭제는 별도 승인 단계다.

## Transport / writability

라이브 recall MCP transport는 read-only ledger(`Ledger.open_read_only`)로 서비스를 만든다.
이 transport의 쓰기 거부 권한은 PG 연결 여부와 독립적이다. proposal/restricted write는
read-only transport에서 fail-closed로 거부된다(`_guard_writable`).
라이브 proposal write를 켜려면 writable transport와 PG 연결 및 권한 승인이 별도로 필요하다.
read 경로(authority pack/review queue)는 PG가 배선된 read-only transport에서 동작한다.

## Next steps (out of scope)

- proposal write를 위한 writable transport 배선
- human/manual approval gate UI 또는 operator 경로 연결
- granular capability flag를 caller identity / runtime permission profile에 바인딩
- proposal-create 단위 audit(현재는 commit 단위), free-text 잔여 누출은 structural projection으로
  완화하고 denylist 한계는 위에 명시
