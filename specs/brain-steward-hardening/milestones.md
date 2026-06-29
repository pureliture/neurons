# Milestones — brain-steward-hardening

## M0 simplifier tidy (pre-loop)
- status: done
- evidence: committed d50f670, steward+stdio tests green (64)

## M1 single review-lifecycle definition
- status: done
- evidence: memory_card.REVIEW_LIFECYCLE_STATES 신설; ledger/service 공유; 큐/적격성 일치 테스트 green

## M2 reference-only stale proposal
- status: done
- evidence: build_stale_proposal_card verb; 저장 envelope에 target raw payload 부재 + (target,reason) 멱등 테스트 green

## M3 supersede/stale completion commit paths
- status: done
- evidence: commit_stale verb + restricted commit tool 2종; 기본 차단/flag시 target demote+큐 제외 테스트 green (79)

## M4 granular restricted permissions + commit audit
- status: done
- evidence: review_commit/auto_accept 분리; auto_accept가 review_commit만으론 안 열림 + commit feedback record 테스트 green (30)

## M5 thin dispatch + service-owned denial/arg
- status: done
- evidence: select_source_span/restricted_denied_payload service 소유; dispatch 중복 튜플 제거; mcp 계약 테스트 green (76)

## M6 shared base projection
- status: done
- evidence: _base_projection 추출; projection 필드셋 고정 테스트 green; 출력 동일

## M7 governance / docs
- status: done
- evidence: contract 문서 갱신(완결/granular 권한/stale 저장/audit) + roadmap cross-reference; 코드 변경 없음(narrow 예외, 소스/계약 일관)

## Final
- 전체 worker 테스트 green (1288 pre-M7; M7은 docs-only)
