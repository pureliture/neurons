# Hermes Brain Steward Server-Side Surface — Requirements

## 승인 대상

- Source of truth: `specs/hermes-brain-steward/requirements.md`
- Preview companion: `specs/hermes-brain-steward/requirements.html`

## 배경 / 현재 상태 (discovery 결과)

이 작업은 zero-from-scratch가 아니다. discovery로 확인된 현재 상태:

- **proposal-only Brain Steward MCP surface는 이미 존재한다.** 커밋 `2d0b341`(#42)이
  `worker/lib/agent_knowledge/session_memory/brain_steward.py`에 11개 작업
  (`authority_pack_read`, `review_queue_list`, `candidate_create`, `stale_mark`,
  `supersede_propose`, `candidate_approve`, `candidate_reject`,
  `candidate_auto_accept`)을 구현했고, `mcp_tools.py` / `mcp_server.py`에 tool name,
  inputSchema, dispatch, `STEWARD_RESTRICTED_TOOL_NAMES`로 배선되어 있다.
- **Hermes는 read consumer로는 이미 인식된다.**
  `context_builder.py`의 `CONTEXT_AUTHORITY_CONSUMERS = {"unspecified", "codex",
  "claude-code", "hermes"}`, `mcp_tools.py:111` consumer enum, `cli.py` `--consumer`
  choices에 `hermes`가 포함되어 `brain_context_resolve` contextpack을 받는다.
- **proposal-only / redaction / restricted-denial invariant는 이미 테스트된다.**
  `worker/tests/test_brain_steward.py`가 candidate≠accepted, stale≠delete,
  supersede≠immediate-swap, review-queue no-raw, authority-pack accepted/current-only,
  restricted-blocked-by-default를 검증한다. (이 테스트들은 `provider="hermes"`,
  `approved_by="hermes"`를 이미 사용한다.)

따라서 이번 작업은 **기존 surface 위에 Hermes provider를 1급 시민으로 붙이는 증분**이며,
중복 재구현이 아니라 (a) ingest identity gap을 닫고 (b) proposer 귀속을 추가하고
(c) Hermes 기본 역할의 restricted 거부와 read-path 비누설을 명시적 회귀로 고정한다.

## 로드맵과의 관계 (의도된 boundary 확장)

`specs/context-authority-roadmap/design.md`는 Hermes를 "read-only consumer,
self-improvement/proposal loop is out of scope"로 명시했다(L41-42, L442). 이번 작업은
그 로드맵을 **의도적으로 한 단계 확장**해 Hermes에 **proposal-only** 표면을 연다.
확장의 안전 경계는 그대로다: Hermes는 accepted/current authoritative memory를 직접
확정하지 못한다. proposal(candidate/stale/supersede)만 남길 수 있고, 확정(approve/
auto_accept)은 사람/operator gate에 남는다. 이 확장이 기존 boundary regression test와
충돌하면, design 단계에서 "Hermes read-only" 단언을 "Hermes는 authoritative write를
하지 못한다(proposal은 허용)"로 정정하는 것이 SoT 변경 범위에 포함된다.

## 질문-답변 흐름 (자문자답)

### Q1: 이 작업의 1차 산출물은 "새 surface 빌드"인가, "기존 surface에 Hermes를 안전하게 붙이기"인가?

기존 surface에 Hermes를 붙이는 것이다. discovery로 11개 도구·dispatch·restricted gate·
redaction·6/8 필수 테스트가 이미 존재함을 확인했다. 새로 만드는 것은
(1) Hermes **ingest** provider identity 처리, (2) proposal에 **proposer 귀속**,
(3) Hermes 기본 역할 관점의 명시적 회귀 테스트들이다. 기존 코드를 중복 재작성하지 않는다.

### Q2: "Hermes provider identity"는 어느 표면을 말하는가? read consumer? ingest provider? 둘 다?

둘은 다른 표면이고 둘 다 다룬다.
- **read consumer identity**: `brain_context_resolve`를 호출하는 주체가 누구인가
  (`consumer=hermes`). → 이미 존재. 회귀 가드만 추가.
- **ingest provider identity**: Hermes provider가 만든 **세션/이벤트가 ingest될 때**
  `provider=hermes`가 수신·정규화·저장되는가. → 이번 작업의 핵심 gap. Codex/Claude/Hermes를
  구분 저장하고, 기존 ingest schema와 호환되게 한다.

### Q3: proposal-only란 정확히 무엇을 금지/허용하는가?

- 허용: candidate 생성, stale 표시 proposal, supersede proposal, authority pack 읽기,
  review queue 읽기.
- 금지(기본 Hermes 역할): accepted/current memory 직접 생성·수정·삭제, candidate
  approve/reject/auto_accept(확정). 이들은 restricted이며 사람/operator gate에서만 열린다.
- 불변식: candidate_create ≠ accepted memory 생성, stale_mark ≠ delete,
  supersede_propose ≠ 즉시 winner 교체.

### Q4: Hermes를 다른 provider와 어떻게 구분 저장하나? proposer는 card subject provider와 같은가?

다르다. card의 `provider` 필드는 **메모리 주체(subject)의 출처**를 뜻한다. proposer는
**proposal을 제출한 actor**(예: hermes)다. 이 둘을 혼동하지 않고, proposal에는 proposer
actor를 별도로 기록한다(정규화된 안전 label만; raw 식별자/secret 아님). 이로써 "어떤 agent가
이 proposal을 남겼나"의 audit이 가능해진다.

### Q5: restricted를 Hermes 기본 역할에서 막는 메커니즘은 무엇인가? 새 RBAC를 도입하나?

새 RBAC를 도입하지 않는다. 이 코드베이스의 현실적 메커니즘은
service 생성 시점의 `allow_restricted`(=`allow_restricted_steward`) 플래그다. Hermes가
연결하는 MCP transport/service는 이 플래그를 기본값 False로 구성하고, restricted 도구는
dispatch에서 `{"permission":"denied","write_performed":False}`로 fail-closed한다.
restricted enablement는 Hermes 능력이 아니라 별도 human/operator gate(또는 test-only flag)다.

### Q6: read tool이 candidate/proposal을 authoritative처럼 반환할 위험은?

`authority_pack_read`는 이미 `accepted_only=True, current_only=True`로 거른다.
`brain_context_resolve`/`brain_memory_search`(knowledge_search)도 candidate/proposal을
authoritative로 노출하면 안 된다. 이 비누설을 명시적 회귀 테스트로 고정한다. 만약 누설이
발견되면 그 필터링을 수정 범위에 포함한다.

### Q7: 안전(redaction) 책임은 새로 만드나, 재사용하나?

재사용한다. `brain_steward.assert_public_safe` + `memory_card._ensure_no_forbidden_content`가
raw transcript/dataset_id/document_id/secret/private path를 fail-closed로 거른다. 새 필드
(proposer 등)도 이 동일 seam을 통과해야 한다. 새 redaction 규칙을 만들지 않는다.

### Q8: 라이브 데이터/배포에 손대나?

아니다. 모든 작업은 worktree 코드 + 단위 테스트(임시 sqlite ledger fixture)로 한정한다.
live RAGFlow write/delete/disable, live GC, Docker/k3s apply, main 직접 수정은 금지다.

## 기능 요구사항

### FR1. Hermes ingest provider identity
- ingest 경로가 `provider`/`source` 값 `hermes`를 수신·검증·저장한다.
- Codex / Claude / Hermes가 구분되어 저장된다(서로 덮어쓰지 않음).
- session/source identity 정규화(별칭/대소문자 등)를 단일 지점에서 수행한다.
- 기존 ingest schema와 호환된다(기존 Codex/Claude ingest 깨지지 않음).

### FR2. Read tool — Hermes read-only 사용
- Hermes가 `brain_context_resolve`, `brain_memory_search`를 read-only로 사용할 수 있다.
- 반환은 accepted/current authoritative memory만 authoritative로 취급한다.
- candidate/proposal은 정답(authoritative)처럼 반환하지 않는다.

### FR3. Brain Steward proposal surface — Hermes proposer 귀속
- read: `memory_authority_pack_read`(accepted/current만), `memory_review_queue_list`(redacted).
- proposal: `memory_candidate_create`, `memory_stale_mark`, `memory_supersede_propose`.
- proposal에 proposer actor(예: hermes)가 정규화되어 기록되고, review queue 응답에 안전
  label로만 노출된다.

### FR4. Restricted 도구 게이트
- `memory_candidate_approve`, `memory_candidate_reject`, `memory_candidate_auto_accept`는
  restricted이며, Hermes 기본 역할(allow_restricted=False)에서 거부된다.
- 거부 시 어떤 write도 일어나지 않는다(fail-closed).

### FR5. Safety guard
- raw transcript / raw dataset_id / raw document_id / secret / private path를 반환하지 않는다.
- stale mark는 delete가 아니다. supersede propose는 즉시 winner 변경이 아니다.
  candidate create는 accepted memory 생성이 아니다.
- 모든 steward 응답은 fail-closed redaction(`assert_public_safe`)을 통과한다.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| 실행 전략 | TDD-first (red → green → refactor) |
| 테스트 러너 | `cd worker && uv run pytest` (관련 테스트 우선, 필요시 전체) |
| 호환성 | 기존 Codex/Claude read·ingest path 회귀 없음, public MCP tool 계약 유지 |
| 안전 | live data/GC/배포 mutation 금지, main 직접 수정 금지, redaction fail-closed |
| 식별자 정책 | 코드 식별자/도구명/필드명은 영문 유지, 응답/문서는 한국어 |
| 격리 | 전용 worktree `claude/hermes-brain-steward`에서만 작업 |

## 사용자 시나리오

- **S1 (ingest):** Hermes provider 세션이 neurons로 ingest되면 `provider=hermes`로
  저장되고, Codex/Claude 세션과 구분된다.
- **S2 (read):** Hermes가 `brain_context_resolve(consumer=hermes)`로 현재 따라야 할
  authoritative context pack을 받는다. candidate/proposal은 정답으로 섞이지 않는다.
- **S3 (propose):** Hermes가 `memory_candidate_create`로 새 후보를 남긴다. 이는 review
  queue에만 보이고 authority pack에는 없으며, proposer=hermes로 기록된다.
- **S4 (stale/supersede):** Hermes가 stale/supersede proposal을 남겨도 기존 accepted/
  current card는 그대로 유지된다(삭제·즉시교체 없음).
- **S5 (restricted 거부):** Hermes가 `memory_candidate_approve`를 호출하면 거부되고 어떤
  write도 일어나지 않는다.
- **S6 (regression):** 기존 Codex/Claude의 read·ingest·steward 경로가 그대로 동작한다.

## 필수 테스트 (완료 게이트)

1. `provider=hermes` ingest identity가 저장되고 Codex/Claude와 구분된다. **(신규)**
2. candidate create는 accepted/current memory를 만들지 않는다. (기존 보강/회귀)
3. stale mark는 memory를 삭제하지 않는다. (기존 보강/회귀)
4. supersede propose는 기존 winner를 즉시 교체하지 않는다. (기존 보강/회귀)
5. review queue는 raw/private payload를 반환하지 않는다. (기존 보강/회귀)
6. authority pack은 accepted/current만 포함한다. (기존 보강/회귀)
7. restricted approve/reject/auto_accept는 기본 Hermes 권한에서 거부된다(Hermes-role 명시). **(신규 관점)**
8. proposal에 proposer=hermes가 기록되고 안전 label로만 노출된다. **(신규)**
9. brain_context_resolve / brain_memory_search가 candidate/proposal을 authoritative로
   누설하지 않는다(Hermes consumer 포함). **(신규 회귀)**
10. 기존 Codex/Claude read path regression 없음. **(회귀)**

## 미결정 항목 (design에서 자문자답으로 확정)

- Hermes ingest provider 값이 들어오는 정확한 코드 지점(ingress API / worker transcript
  ingest / couchdb_source)과, 이미 hermes를 수용하는지 여부 → discovery 후 design에서 확정.
- proposer 필드의 정확한 이름·저장 위치(`steward_proposer` on proposal card vs span 확장).
- read-path 누설 회귀의 정확한 대상 함수(context_builder vs knowledge_search_service).
