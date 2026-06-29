# Brain Steward MCP Hardening Requirements

## 승인 대상

- Source of truth: `requirements.md`
- Preview companion: (생성 안 함 — 사용자 사전 승인)
- 승인 상태: 사용자가 `requirements.md`/`design.md`를 사전 승인. grilling 질문은
  자문자답(self-answered)하며, 근거가 필요한 항목은 sonnet read-only 리서치로 확보했다.

## 배경

`feat(worker): proposal-only Brain Steward MCP surface`(머지됨)에 대해 code-simplifier,
codebase-architecture review, system-design review를 수행했다. 이 문서는 세 리뷰의 지적을
하나의 hardening 작업으로 통합한 요구사항이다. 구현 안전성(authority 불변·fail-closed
redaction·restricted 기본 차단)은 이미 견고하다. 이번 범위는 (a) 거버넌스 정합,
(b) supersede/stale 완결 경로 부재, (c) stale 저장이 raw ref를 복제하는 문제,
(d) restricted gate가 단일 boolean인 위험, (e) dispatch/모델 중복, (f) 관측성이다.

## 질문-답변 흐름 (자문자답 + 리서치 근거)

### Q1. 승인된 context-authority-roadmap이 "Hermes는 read-only consumer, proposal loop는 범위 밖"이라 못박는데, 이 표면이 충돌한다. 어떻게 해소하나?

A. Brain Steward를 **독립 승인 spec**(`specs/brain-steward-hardening/`)으로 격상해 SoT 충돌을
해소한다. proposal-only는 roadmap의 핵심 불변("Dendrite/agent는 authority를 결정하지 않는다")을
**위반하지 않는다** — agent는 후보·제안만 만들고 authoritative truth(accepted+current) 변경은
restricted human/manual gate에서만 일어나기 때문이다. roadmap design.md의 Hermes read-only 문구에
이 spec을 sanctioned extension으로 참조하는 1줄 cross-reference를 추가한다. roadmap의 다른 결정은
변경하지 않는다.

### Q2. supersede/stale proposal이 완결 경로 없이 review_queue에 영구 누적된다(high severity). 완결을 지금 배선하나?

A. 배선한다. 리서치 근거:
- supersede 완결: `LLMBrainMemoryService.supersede_accepted_card`가 new card accept + old card
  demote(`commit_supersession`로 `currentness=superseded`, `superseded_by=[new]`)를 한 메서드에서
  수행한다(재사용 가능).
- stale 완결: 동등 함수가 **없다**(needs-new). `commit_supersession`을 본떠 `commit_stale`
  promotion verb를 추가한다. accepted card를 `currentness=stale`, `freshness=historical`로 demote
  한다(상태 불변식상 'stale'은 superseded_by를 요구하지 않으므로 유효).
완결은 **restricted(human/manual gate)** tool로 노출한다: `memory_supersede_commit`,
`memory_stale_commit`. 기본 권한에서는 막힌다.

### Q3. stale proposal id가 reason을 무시해 같은 target 재표시 시 첫 reason을 덮어쓴다. 의도인가?

A. 의도가 아니다. stale proposal id에 **reason hash를 포함**해 서로 다른 reason은 서로 다른
proposal이 되게 한다. "target당 1 proposal"이 아니라 "(target, reason)당 1 proposal"로 명시한다.

### Q4. stale proposal이 target accepted card의 full envelope를 deep-copy해 ledger row에 raw ref를 저장한다. 어떻게 고치나?

A. **reference-only 최소 envelope**로 만든다. target을 deep-copy하지 않고, 리서치가 확인한 최소
형태(card_type=`status`, typed_payload 4필드, `source_refs`/`evidence_refs`/`evidence_hashes`는
빈 리스트, `derived_from`/`typed_payload.current_authority`로 target 참조)를 새로 빌드한다.
`validate_memory_card_envelope`을 통과하고 target의 raw ref/typed_payload를 복제하지 않는다.
lifecycle 필드 직접 수정 대신 `memory_promotion`에 stale proposal 빌더 verb를 둔다(아키텍처 A4와
동일 해법).

### Q5. restricted 위임이 단일 boolean이라, writable transport를 켜면서 flag를 올리는 순간 auto_accept까지 열린다. 어떻게 완화하나?

A. boolean을 **2단 granular permission**으로 쪼갠다: `allow_review_commit`(approve/reject/
supersede_commit/stale_commit)과 `allow_auto_accept`(별도, 기본 False). 가장 위험한 auto_accept는
review_commit을 허용해도 자동으로 열리지 않는다. `operator_approval_ref`는 비어 있으면 차단된다
(기존 `apply_auto_acceptance_plan`이 이미 강제). 모든 restricted commit은 audit feedback record를
남긴다(Q6). full permission-profile/identity 바인딩은 transport 계층 책임으로 두고 범위 밖이다.

### Q6. proposal/commit write에 audit 흔적이 read 응답 밖에 없다. 관측성을 어떻게 추가하나?

A. authority를 바꾸는 **restricted commit**(approve/reject/supersede_commit/stale_commit)에
기존 `build_feedback_record` + `upsert_llm_brain_feedback_record`로 audit record를 남긴다
(approve/reject는 이미 남김 — supersede/stale commit에 동일 seam 연결). 조회는
`list_llm_brain_feedback_records`. proposal create 자체는 review_queue가 곧 기록이므로 추가
record를 강제하지 않는다(YAGNI).

### Q7. dispatch가 source_span 필드 집합을 스키마·dispatch 튜플·빌더 3곳에 중복 보유하고 이미 어긋나 있다. 어떻게 정리하나?

A. source_span **인자 수용 책임을 `BrainStewardService`로 내린다**. dispatch는 raw arguments를
넘기고 service가 필드 선택/검증을 소유한다. dispatch의 `_STEWARD_SOURCE_SPAN_KEYS` 중복을
제거하고 dispatch를 얇은 라우터로 만든다. wire 스키마(`mcp_tools.py`)는 그대로 public contract로
유지한다.

### Q8. restricted 거부 페이로드가 dispatch와 service에 갈라져 있다. 어떻게 합치나?

A. denied 결정과 denied wire shape를 **service가 단독 소유**한다. service가
`brain_steward_restricted_denied.v1` 페이로드를 반환하고 dispatch는 손으로 구성하지 않는다.
미래 transport도 동일 fail-closed 의미를 복제 없이 얻는다.

### Q9. review-queue lifecycle 리터럴(SQL)과 `REVIEW_LIFECYCLE_STATES`(service)가 중복이다. 어떻게 단일화하나?

A. review lifecycle 집합을 **모델 계층(`memory_card.py`)에 단일 정의**하고 ledger와 service가 모두
거기서 import한다. 하위 계층(ledger)이 상위(service) 상수를 import하는 의존 역전을 피한다.

### Q10. `_authority_item`/`_review_item` projection이 중복이다. 공유하나?

A. 공통 15필드를 `_base_projection`으로 추출하고 각 함수가 전용 필드만 추가한다. `assert_public_safe`
fail-closed 방어는 유지한다. brain_query의 다른 목적 projection은 병합하지 않는다(별 계층).

## 기능 요구사항

- FR1. review lifecycle 집합을 단일 source로 정의하고 ledger 조회와 approve/reject 적격성이 동일
  정의를 참조한다.
- FR2. `memory_stale_mark`는 target raw ref/typed_payload를 복제하지 않는 reference-only 최소
  envelope proposal을 만든다. proposal id는 (target, reason)에 멱등이다.
- FR3. `memory_supersede_commit`(restricted)은 supersede proposal을 확정해 new card를 accept하고
  old card를 demote한다.
- FR4. `memory_stale_commit`(restricted)은 stale proposal을 확정해 target accepted card를
  `currentness=stale`로 demote한다.
- FR5. restricted 권한은 `allow_review_commit`과 `allow_auto_accept`로 분리되고 둘 다 기본 차단이며
  auto_accept는 review_commit 허용만으로 열리지 않는다.
- FR6. 모든 restricted commit은 audit feedback record를 남기고 `list_llm_brain_feedback_records`로
  조회된다.
- FR7. dispatch는 얇은 라우터가 되고 source_span 인자 수용과 restricted denied 페이로드는 service가
  소유한다. wire 스키마는 외부 호환을 유지한다.
- FR8. `_authority_item`/`_review_item`은 공유 base projection을 쓰되 출력은 동일하다.
- FR9. Brain Steward를 독립 승인 spec으로 문서화하고 contract 문서에 완결 경로·권한 모델·stale 저장
  형태·잔여 한계를 명시한다. roadmap design에 sanctioned-extension cross-reference를 추가한다.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| 안전 불변식 보존 | proposal은 비-accepted lane만, accepted+current 불변, read 출력 raw/private 미노출, 모든 거부는 write 이전 |
| 외부 호환 | MCP wire 스키마(tool 이름·키·enum), read/proposal 응답 형태 유지 |
| TDD | 모든 code-changing milestone은 red→green→refactor |
| 테스트 게이트 | `cd worker && uv run pytest -q` 전체 green, 기존 1276 통과 유지 |
| 비밀/원문 | host/token/dataset_id/document_id/raw transcript를 코드·테스트·문서·출력에 쓰지 않음 |
| 범위 격리 | RetiredIndexBridge/live GC/production apply/main 직접수정 없음. 단일 feature 브랜치 1 PR |

## 사용자 시나리오

- S1. Hermes가 stale하다고 판단한 카드를 `memory_stale_mark`로 제안한다 → review_queue에 reference
  -only proposal이 뜨고 원본 카드는 그대로다 → 운영자가 restricted `memory_stale_commit`으로 확정하면
  원본이 stale로 내려가고 proposal이 큐에서 빠지며 audit record가 남는다.
- S2. Hermes가 교체안을 `memory_supersede_propose`로 낸다 → 운영자가 `memory_supersede_commit`으로
  확정하면 new card가 accept되고 old card가 superseded로 demote된다.
- S3. writable transport에서 `allow_review_commit`만 켠 운영자는 approve/supersede/stale commit은
  하지만 auto_accept는 못 한다(별도 flag 필요).

## 미결정 항목

- 없음(모든 분기는 자문자답으로 닫힘). full permission-profile/identity 바인딩과 proposal-create
  단위 audit, brain_query projection 통합은 명시적 범위 밖(YAGNI, transport/후속).
