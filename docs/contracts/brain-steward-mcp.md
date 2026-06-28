# Brain Steward MCP Surface (proposal-only v1)

Hermes 같은 agent가 `neurons`의 authoritative memory를 **직접 바꾸지 않고** 안전하게
관리 후보(candidate)와 proposal을 남길 수 있게 하는 agent-facing MCP 표면이다.

첫 버전의 목표는 자가확정(self-acceptance)이 아니라 **proposal-only 안전성**이다. Hermes는
후보를 만들고, stale/supersede를 제안할 수 있지만, 무엇이 authoritative memory가 되는지는
사람(또는 명시적 gate)이 결정한다.

## Authority model

- `neurons` ledger가 authority다. Qdrant/graph/RAGFlow는 canonical authority가 아니다.
- **accepted + current** MemoryCard만 authoritative memory다.
- candidate/proposal은 recall에서 정답처럼 쓰이지 않는다. authority pack에 포함되지 않는다.
- stale/superseded memory는 authority pack에서 빠지고, review queue에서 downgraded
  evidence로만 노출된다.
- 이 표면은 기존 `brain_context_resolve` read path와 충돌하지 않는다. authority pack은
  ledger의 accepted+current card만 직접 읽는 별도 read다.

## Tool classification

| Tool | Class | 효과 |
| --- | --- | --- |
| `memory_authority_pack_read` | read | accepted+current MemoryCard만 redacted view로 반환 |
| `memory_review_queue_list` | read | pending candidate/stale/supersede proposal을 redacted view로 반환 |
| `memory_candidate_create` | proposal | 새 MemoryCard 후보를 candidate(또는 needs_review)로 생성 |
| `memory_stale_mark` | proposal | 특정 MemoryCard가 stale하다는 proposal 생성(원본 불변) |
| `memory_supersede_propose` | proposal | 기존 MemoryCard를 새 후보로 대체하자는 proposal 생성(즉시 교체 안 함) |
| `memory_candidate_approve` | restricted | candidate를 accepted authoritative memory로 승격 |
| `memory_candidate_reject` | restricted | candidate를 rejected로 확정 |
| `memory_candidate_auto_accept` | restricted | auto-accept 정책으로 candidate 승격 |

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
  assignment가 들어간 card는 ledger에 **저장되지 않는다**.
- proposal write: `_persist_proposal`은 ledger upsert **이전에** review-queue projection을
  `assert_public_safe`로 검증한다. 안전하지 않은 proposal은 한 줄도 저장되지 않으므로 review
  queue 읽기를 망가뜨리지 않는다.
- 잔여 한계: redacted summary/title는 agent-authored free text다. 무작위 hex처럼 보이는
  raw dataset_id/document_id **값**이나 bare host:port는 denylist로 구분할 수 없다. 이에 대한
  방어는 denylist가 아니라 structural projection이다 — 두 read tool은 `source_refs`/
  `typed_payload`/`envelope_json` 같은 raw ref를 애초에 반환하지 않는다.

### Proposal tools

proposal tool은 항상 **non-accepted lifecycle**(candidate/needs_review)로만 ledger에 남는다.

- 후보 `memory_id`는 `mem_steward_` prefix로 발급되어 accepted/miner candidate id와 분리된다.
- proposal은 idempotent하다(같은 입력 → 같은 후보).
- `memory_candidate_create`는 accepted memory를 만들지 않는다.
- `memory_stale_mark`는 대상 card를 삭제하거나 수정하지 않는다. stale proposal은 대상
  `memory_id`를 `target_memory_id`로 참조하는 별도 record다.
- `memory_supersede_propose`는 기존 card를 즉시 교체하지 않는다. supersede 의도는 새 후보의
  `supersedes`에 기록되고, 대상 card는 accepted+current 그대로 남는다.
- `_persist_proposal`은 accepted card를 덮어쓰는 어떤 write도 fail-closed로 거부한다.

`memory_candidate_create` / `memory_supersede_propose`의 입력은 raw transcript가 아니라
**redacted source_span**이다: `card_type`, `project`, `provider`, `typed_payload`,
sha256 `content_hash`, opaque `source_ref`/`span_ref`, 그리고 `redacted_summary`.

### Restricted tools (기본 차단)

`approve`/`reject`/`auto_accept`는 Hermes가 바로 쓰면 안 된다. 기본 권한에서는 **어떤 write도
수행하지 않고 거부**한다(`{"permission":"denied", "write_performed": false}`).

위임은 `KnowledgeSearchService(allow_restricted_steward=True)` 또는
`BrainStewardService(allow_restricted=True)`로만 열린다. 이는 human/manual gate 또는
명시적 test-only path를 위한 flag이며, 기본값은 `False`다. flag 없이 호출하면
`StewardPermissionError`가 ledger write **이전에** raise된다.

## Code map

- 서비스/proposal·read 로직: `worker/lib/agent_knowledge/session_memory/brain_steward.py`
- tool contract(schema): `worker/lib/agent_knowledge/mcp_tools.py`
- JSON-RPC dispatch: `worker/lib/agent_knowledge/mcp_jsonrpc.py`
- review-queue ledger read: `worker/lib/agent_knowledge/ledger_native_memory_mixin.py`
  (`list_llm_brain_review_queue`)
- 서비스 wiring/flag: `worker/lib/agent_knowledge/knowledge_search_service.py`
- 재사용한 기존 모델: MemoryCard envelope/validation(`session_memory/memory_card.py`),
  candidate builder(`session_memory/memory_miner.py`), promotion(`session_memory/memory_promotion.py`,
  `session_memory/llm_brain_service.py`)
- tests: `worker/tests/test_brain_steward.py`

## Transport / writability

라이브 recall MCP transport는 read-only ledger(`Ledger.open_read_only`)로 서비스를 만든다.
proposal/restricted write는 read-only ledger 위에서 fail-closed로 거부된다(`_guard_writable`).
라이브 proposal write를 켜려면 writable ledger를 쓰는 별도 transport 배선이 필요하다 — 이는
의도적 다음 단계이며, read 경로(authority pack/review queue)는 read-only transport에서 그대로
동작한다.

## Next steps (out of scope for v1)

- proposal write를 위한 writable transport 배선
- human/manual approval gate UI 또는 operator 경로 연결
- restricted flag를 runtime feature flag/permission profile로 승격
- review queue의 사람-검토 후 supersede commit(old card demotion) 연결
