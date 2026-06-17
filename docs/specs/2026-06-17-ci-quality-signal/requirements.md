# neurons CI 품질 신호 체계 Requirements

> 상태: **Phase 1 최종 — 승인 요청.** 승인 시 Phase 2(design.md)로 진행.

## 승인 대상

- Source of truth: `requirements.md` (이 파일)
- Preview companion: `requirements.html` (optional, 미생성)

## 배경 / 문제

CI 품질 작업이 spec 없이 direct-execute로 급조됨. 보완개발 전 요구사항을 정형화한다. 현재 사실:

- main에 `.github/workflows` **없음** — CI 0개.
- PMD는 open PR #5(`feature/pmd-pr-comment-reports`)에만 존재, **report-only/비차단**, 모든 PR 트리거.
- ArchUnit 5규칙(ADR-0002)을 `gradle test`에 추가했으나 **CI 밖**이라 PR 강제 안 됨.
- 직전 `.github/workflows/test.yml`(gradle test + worker pytest)을 direct-execute로 생성(advisory).
- worker pytest에 **기존 실패 2건**(`test_session_memory_gc`) — 별도 task(`task_9c045496`)로 위임.

## 질문-답변 흐름 (확정)

| Q | 결정 |
|---|---|
| Q1 스코프 | **B. CI 신호 체계 v1** (test + PMD를 일관 체계로) |
| 차단성 | **② test = gate / PMD = advisory** |
| Q7 self-lock | **A. 단계적** — gradle-test 즉시 gate, worker-pytest는 GC green 후 gate 승격 |
| Q2 PMD 관계 | **A. 공존 + 규약 정합** — PR#5 별도 `pmd.yml` 유지, `test.yml` 별도. 트리거·명명만 일관 |
| Q3 트리거 | **PR(opened/synchronize/reopened/ready_for_review) + push main. path 필터 없음**(테스트 빠름) |
| Q4 게이트 적용 | workflow는 즉시 작성. **branch protection(required check)은 outward mutation** — push/PR 이후 **명시 동의**로 `gh api` 또는 수동 |
| Q5 가시화 | **README에 CI 상태 배지 추가** (test/pmd) |
| Q6 산출물 | **docs/specs/ 에 커밋**(이 브랜치에 기록). doc-curator 등록 안 함 |

## 기능 요구사항

- `gradle test`(unit·API·worker-config·**ArchUnit** 포함)를 PR/main push에서 실행 → **required check(gate)** 대상.
- `worker pytest`(uv)를 PR/main push에서 실행, 당분간 **advisory**(비차단). GC 2건 green 후 gate 승격.
- PMD(`pmd.yml`, PR#5)는 **advisory** 유지(PR 코멘트). `test.yml`과 트리거·명명 규약 일관.
- gate화(required status check)는 GitHub branch protection으로 적용. 워크플로 파일 작성과 **분리된 단계**이며 명시 동의 필요.
- README에 CI 상태 배지(test gate / pmd advisory) 노출.
- 워크플로/로그에 secret·raw dataset_id 미노출(repo 가드레일).

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| 차단성 | test = gate(required check, merge 차단) / PMD = advisory(비차단) |
| 비용 | CI 가볍게 (gradle test ~수초, Docker 불필요) |
| 안전 | branch protection 등 outward/repo-설정 변경은 명시 동의·되돌림 가능하게 |
| redaction | 워크플로/로그 secret·raw dataset_id 미노출 |

## 사용자 시나리오

- 개발자가 PR을 연다 → `gradle test`(gate)·`worker pytest`(advisory)·PMD(advisory)가 자동 실행.
- `gradle test` 빨강이면 **merge 버튼 차단**(required check). ArchUnit 위반·테스트 회귀가 main 유입 방지.
- `worker pytest`/PMD 빨강은 신호만(머지 가능). GC task 완료 후 worker pytest를 gate로 승격.
- README 배지로 main의 현재 CI 상태를 한눈에 본다.

## 수용 기준 (Acceptance Criteria)

- [ ] `test.yml`이 PR/main push에서 gradle test + worker pytest를 실행한다.
- [ ] gradle test job이 ArchUnit 5규칙을 포함해 green이다(현재 검증됨).
- [ ] gradle-test job의 `name` = 게시되는 status context = branch protection 등록 이름이 **정확히 일치**(`gradle-test`) → self-lock 없음.
- [ ] test.yml에 `concurrency`(중복 run 취소) 존재.
- [ ] worker pytest job은 advisory(비차단), required check에 **미포함**. GC 2건 외 green.
- [ ] 트리거 동치: 두 워크플로 `pull_request.types` 동일, `push:[main]`은 test.yml만, PMD PR-only는 의도된 차이.
- [ ] README에 동적 CI 배지(test/pmd)가 있고, **가드 단언 문자열 영역 밖**에 위치, 링크 유효.
- [ ] gradle-test가 main의 required status check로 설정된다(사전승인 outward) + rollback 절차 명시.
- [ ] 회귀 가드 `worker/tests/test_repo_readme_boundary.py`(2함수) 계속 green.

## 비목표 (Non-goals)

- `test_session_memory_gc` 실패 2건의 근본 수정 — 별도 task(`task_9c045496`).
- worker pytest의 즉시 gate화(GC green 전까지 advisory).
- PMD를 차단 게이트로 전환.
- lint/coverage 등 추가 검사(YAGNI).

## 미결정 항목

- (없음 — 전부 확정. Q4의 branch protection 적용 시점만 execution에서 동의 게이트로 처리.)
