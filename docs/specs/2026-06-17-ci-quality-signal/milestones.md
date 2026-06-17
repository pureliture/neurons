# Milestones — ci-quality-signal

## M1 test.yml 정정 + README 배지
- status: done
- evidence: YAML 유효(jobs=[gradle-test, worker-pytest], concurrency 있음, gradle-test name="gradle-test"), README 가드 2 passed, gradle test green(기존 검증, YAML 변경은 무관)

## M2 pmd 트리거 동치 검증·문서 (no-op edit)
- status: done
- evidence: pmd.yml(PR#5) pull_request.types == test.yml ([opened,synchronize,reopened,ready_for_review]). push:main은 test.yml만(의도된 차이). pmd.yml 미편집

## M2.5 publish 안전성 감사 + 정리
- status: done
- evidence: 감사 SAFE-WITH-FIXES(실 secret/key/credential/raw-id 0). `/Users/ddalkak`→`/Users/example` 4 test파일 교체, 0건 잔존, 47 tests pass. hostname/`/home/ragflow`는 이미 origin/main(public)에 존재(신규 노출 아님, 미수정).

## M3 커밋 → push → PR  (outward, 사전승인)
- status: blocked — **사전승인 범위 초과**
- note1: origin/main..HEAD = **83 commits**(local main 109). CI gate가 의미 있으려면 이 코드가 origin에 있어야 함(CI가 그 코드를 테스트) → push = 로컬 누적작업 전체 첫 PUBLIC 공개.
- note2: `/Users/ddalkak`가 미push 커밋 4개 history에 존재 → working tree 정리로는 history 누수 안 막힘. 제거하려면 83커밋 history rewrite(중대/위험).
- 사용자 결정 대기: 전체 공개+accept(deep-history username) / history scrub 후 공개 / 공개 보류

## M4 branch protection required check = gradle-test  (outward, 사전승인)
- status: blocked (M3 종속 — 원격에서 gradle-test 체크 1회 실행 필요)
- self-lock 방지: 실제 게시 context 이름 복사 후 등록 + rollback 절차

## M5 (deferred) GC green 후 worker-pytest gate 승격
- status: deferred (task_9c045496)
