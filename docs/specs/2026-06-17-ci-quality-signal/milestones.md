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
- status: done
- 결정: history scrub은 6브랜치 blast radius라 취소, username 수용하고 공개. local main(미audit 26커밋) 직접 push 안 함 — 내 브랜치만 push.
- evidence: 4커밋 push, PR #6 생성(https://github.com/pureliture/neurons/pull/6). 백업 bundle `/tmp/neurons-backup-prescrub.bundle`.

## M4 branch protection required check = gradle-test  (outward, 사전승인)
- status: done
- evidence: gradle-test CI green 확인 → 실제 context 이름 `gradle-test` 1개만 required 등록(strict=false, enforce_admins=false). PR #6 mergeable=MERGEABLE/UNSTABLE — gradle-test=SUCCESS로 머지가능, worker-pytest=FAILURE는 비차단(GC self-lock 없음 검증).
- rollback: `gh api -X DELETE repos/pureliture/neurons/branches/main/protection` 또는 contexts=[] PUT.

## M5 (deferred) GC green 후 worker-pytest gate 승격
- status: deferred (task_9c045496 — GC 2건 green 후 worker-pytest를 required에 추가)
