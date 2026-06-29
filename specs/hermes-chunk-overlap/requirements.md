# Hermes Chunk Overlap Resolution Requirements

## 승인 대상

- Source of truth: `requirements.md`
- Preview companion: `requirements.html` (필요 시)
- 승인 상태: pre-approved by user directive (grill 자문자답 + 5개 sonnet 리서치 근거)

## 질문-답변 흐름

자문자답. 답은 neurons 코드 리서치(ingress/worker-delivery/session-memory/supersede-dedup/
contracts 5개 슬라이스) 근거.

### Q: 무엇이 문제인가?

dendrite가 자란 Hermes 세션을 재전송하면(전체파일 version hash 특성상 정상 사용에서 발생),
같은 `session_id_hash`에 대해 더 긴 `conversation_chunk`가 들어온다. neurons에서:

- CouchDB는 chunk를 `conversation_chunk:{session_id_hash}:{chunk_id}`로 저장하고, `chunk_id`는
  내용 주소(`sha256(session_id_hash:turn_start:turn_end:text)`)라 **짧은/긴 chunk가 별도 문서**로
  공존한다(서로 덮어쓰지 않음).
- **canonical 경로(M3 `materialize_session_memory`)**는 세션의 모든 chunk를 fetch해
  `(turn_start_index, _id)` 순으로 **dedup 없이 concat**한다 → 짧은+긴 본문이 둘 다 임베드 →
  **recall에 중복/겹침 내용**.

### Q: 이미 존재하는 해결 수단이 있는가?

있다(부분적). **regeneration 경로**(`_canonicalize_session_chunks_for_memory`)는 이미 2단계
dedup을 한다: (1) exact-dup 제거, (2) **subsumption** — 더 긴 chunk가 짧은 chunk의 turn window를
포함하고 텍스트도 포함하면 짧은 것을 drop. 그러나 이 로직은 **regen/sync 경로에만** 있고
**canonical M3 경로에는 없다**. 그래서 M3가 갭이다.

### Q: 어디서 고치는가? (핵심 결정)

**canonical M3 materializer(`materialize_session_memory`)에 동일한 subsumption + exact-dup
canonicalization을 적용**해 concat 전에 겹치는 chunk를 정리한다. recall substrate가
CouchDB→session-memory이므로 여기를 고쳐야 실제 recall이 깨끗해진다.

- 대안 기각 1: Java RetiredIndexBridge `supersedePriorVersions`(기본 off, `logical_document_id` 필요) —
  RetiredIndexBridge projection layer만 정리하고 **CouchDB M3 substrate는 그대로 중복**이라 부적합.
- 대안 기각 2: MemoryCard supersede(`supersedes`/`currentness`) — 그건 brain card layer이고
  session-memory chunk layer가 아니라 층위가 다름.

### Q: 어떤 규칙인가?

regen 경로와 **동일한 정책**: 같은 세션의 chunk들 중
- exact-dup(같은 content_hash + 동일 turn/part/char 범위) → 1개만 유지.
- subsumption: 더 긴 chunk가 짧은 chunk의 turn window를 strict 포함하고 sanitized text도 포함하면
  짧은 것을 drop(superset/긴 쪽이 이긴다).
정리 후 남은 chunk를 기존처럼 `(turn_start_index, ...)` 순으로 concat.

### Q: 원본 chunk를 삭제하는가?

아니다. canonicalization은 **build 시점 in-memory**로만 적용한다. CouchDB의 chunk 문서는 이력으로
보존하고, materialized body에만 미반영한다. 삭제/파괴 없음(안전·가역).

### Q: coverage gate와 충돌하나?

아니다. coverage_manifest의 `materialization_loss` 판정은 **저장된 chunk 수 ≥ 기대 수**다.
subsumption은 fetch 이후 in-memory 정리라 저장 수를 바꾸지 않는다 → `fully_materialized`는
그대로 정확. (겹침은 누락이 아니라 잉여이므로 coverage와 직교.)

### Q: dendrite 변경이 필요한가?

아니다. 이 neurons 측 정리가 dendrite의 재전송(전체파일 version hash)을 **안전하게 흡수**한다.
앞서 보류했던 dendrite 멱등키(세션별 지문) 변경은 **불필요**해진다. thin-client 무변경.

### Q: 범위/경계는?

worker Python(session-memory build)만. Java/ingress 변경 없음(`conversation_chunk`는 이미 수용).
live RetiredIndexBridge mutation/GC 없음. 원본 삭제 없음. Hermes #48(provider identity/steward)와 정합 유지.

## 기능 요구사항

- canonical 경로(`materialize_session_memory`)가 concat 전에 같은 세션 chunk에 대해 exact-dup
  제거 + subsumption(긴 쪽이 짧은 쪽을 포함하면 drop)을 적용한다.
- subsumption 정책은 regeneration 경로의 기존 정책과 **동일**해야 한다(두 경로 일관). 가능하면
  공유 pure 함수로 추출해 양쪽이 같은 로직을 쓴다.
- 겹치지 않는 chunk(서로 다른 turn 범위)는 모두 보존하고 기존 순서로 concat한다.
- canonicalization은 in-memory build 시점만. CouchDB 원본 chunk를 삭제/수정하지 않는다.
- coverage gate(`materialization_loss`/`fully_materialized`) 동작을 바꾸지 않는다(저장 수 기준 유지).
- 기존 materializer/regeneration/recall 동작을 회귀시키지 않는다.
- assert_public_safe 등 기존 redaction seam을 통과한다(새 필드 도입 시).

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| Worktree isolation | `claude/hermes-chunk-overlap` worktree에서 작업. main 직접 수정 금지. |
| TDD | red -> green -> refactor. 동작 test 먼저. |
| Layer | 수정은 worker session-memory build(canonical M3)에 한정. Java/ingress 무변경. |
| Non-destructive | CouchDB 원본 chunk 삭제/수정 금지(in-memory canonicalization만). live RetiredIndexBridge/GC 금지. |
| Consistency | M3와 regeneration의 overlap 정책 일치(가능하면 공유 함수). |
| Thin-client | dendrite 변경 불필요(재전송을 neurons가 흡수). |
| Compatibility | 기존 materialize/regen/recall/coverage 동작 무회귀. Hermes #48 정합. |
| Tests | `cd worker && uv run pytest -q`. |

## 사용자 시나리오

- Hermes 세션이 자란 뒤 재마이그레이션/재캡처되어 더 긴 chunk가 들어와도, recall에는 **겹치지 않는
  단일 정리본**만 보인다(짧은 chunk는 긴 chunk에 흡수됨).
- 운영자가 canonical build를 돌리면 같은 세션의 중복/겹침이 자동 정리되어 별도 수작업이 불필요하다.
- Maintainer가 M3와 regen이 같은 overlap 정책을 쓰는지 확인할 수 있다.

## 검증 완료 기준

- L1 자동 검증: `cd worker && uv run pytest -q` 통과. M3 subsumption/exact-dup/non-overlap/
  coverage-불변/regression 테스트 포함. 기존 regen·recall 테스트 무회귀.
- L2(선택, 합성 fixture): 한 세션에 짧은(turn 1-2) + 긴(turn 1-4, 1-2 텍스트 포함) chunk를 넣고
  materialize → body에 긴 것만, 중복 없음. 겹치지 않는 두 chunk는 둘 다 보존.
- L3(별도 승인): 실제 CouchDB/RetiredIndexBridge 런타임에서의 재투영 검증.

## 허용 / 금지 범위

- 허용: M3 materializer in-memory canonicalization(subsumption+exact-dup), 공유 pure 함수 추출,
  합성 fixture 테스트.
- 별도 승인: 실제 런타임 재투영, live RetiredIndexBridge write/delete.
- 금지: CouchDB 원본 chunk 삭제/수정, live RetiredIndexBridge/GC mutation, Java/ingress kind 계약 변경,
  dendrite 동작 변경(불필요).

## 미결정 항목

- 공유 함수 추출 시 regeneration 경로를 같은 함수로 리팩터할지(그 경로 테스트가 green이면 진행,
  위험하면 M3만 적용하고 regen은 후속). design에서 결정.
- 향후 chunk-level supersede 필드(이력 링크)는 이번 scope 밖(YAGNI). 필요 시 별도 grill.
