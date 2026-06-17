# neurons CI 품질 신호 체계 Design Spec (v2, 리뷰 반영)

## Overview

neurons CI 신호 체계 v1. **test(gradle test, ArchUnit 포함)는 차단 게이트**, **worker pytest·PMD는
advisory(비차단)**. 트리거·명명 규약 일관화 + README 배지 가시화. 게이트화(branch protection)는
outward라 명시 동의(사전승인).

> **차단 모델 (명문화).** 워크플로 파일 자체는 **차단력이 없다** — 실행 결과(green/red status)만 게시한다.
> "gate"는 오직 GitHub **branch protection의 required status check 지정**으로 발생한다. 따라서
> `test.yml`의 'advisory'성 명명과 'gradle test = gate'는 **서로 다른 plane**이다: 전자는 파일,
> 후자는 repo 설정. required check로 지정된 체크만 merge를 막는다.

## Requirements Reference

- Phase 1 source: `requirements.md`
- 핵심: gradle test = required check / worker pytest·PMD = advisory / PR#5 PMD와 공존 / GC 2건 비목표

## Architecture

```
PR/main push
   ├─▶ test.yml
   │     ├─ job: gradle-test    (name=gradle-test, stable context)  ← required check 대상
   │     └─ job: worker-pytest  (advisory; required로 절대 지정 금지)
   └─▶ pmd.yml (PR#5)           (advisory, PR 코멘트)

branch protection (repo 설정, 사전승인 outward)
   └─ required_status_checks.contexts = ["gradle-test"]  ← 단 1개만
```

- **required check 이름 = 게시되는 status context = job의 `name` 필드.** 그래서 gradle-test job의
  `name`을 정확히 **`gradle-test`** 로 둔다(설명 텍스트 unit·API·worker-config·ArchUnit은 step name으로 이동).
  context와 spec 등록 이름이 같아야 self-lock이 안 난다.
- 공유 트리거 **동치 조건**: 두 워크플로의 `pull_request.types`는 동일(`opened/synchronize/reopened/ready_for_review`).
  `push: [main]`은 **gate성 워크플로(test.yml)만**. PMD가 PR-only인 것은 **의도된 차이**로 인정(advisory).

## Data Flow

1. PR 이벤트 → gradle-test·worker-pytest·pmd 실행 → PR에 status 게시.
2. branch protection이 `gradle-test` context만 required → red 시 merge 차단. worker-pytest·pmd는 신호만.
3. README 배지는 main 기준 상태 반영.

## Component Details

| 컴포넌트 | 변경 | 비고 |
|---|---|---|
| `test.yml` gradle-test | **job name → `gradle-test`** + `concurrency` 추가 | 설명은 step name으로 |
| `test.yml` worker-pytest | 현행 유지(advisory) | required 지정 금지 — M4 체크리스트로 강제 |
| `test.yml` 전역 | `concurrency: {group: ${{github.workflow}}-${{github.ref}}, cancel-in-progress: true}` | 중복 run 취소 |
| README 배지 | test.yml + pmd.yml 동적 배지 2개 | 가드 단언 문자열(`text=neurons` 등) 영역 **밖**에 배치. 추가 후 가드 테스트 재확인 |
| `pmd.yml` (PR#5) | **이 worktree에서 편집 안 함** | M2는 규약 동치 **검증·문서화 only** |
| branch protection | M4, 사전승인 outward | contexts=["gradle-test"] 단일 |

- worker-pytest의 advisory는 **branch protection 미지정**으로만 보장됨 → M4 체크리스트로 휴먼에러 차단.

## Error Handling

- **required check 이름 불일치 = 영구 self-lock**(최우선). M4에서 실제 게시된 context 이름을 복사해 등록(아래 M4 절차).
- worker pytest red(GC 2건) → advisory라 merge 안 막음. M4에서 required에 **절대 포함 금지**.
- branch protection은 체크 1회 실행 + admin 권한 필요 → 순서 M3(push/PR/실행) → M4(설정).
- **README pmd 배지 의존성**: pmd.yml이 **main에 있어야** 배지가 성립(현재 PR#5 미머지). → 결정: test 배지는 지금 추가(push 후 동작), pmd 배지는 추가하되 "PR#5 머지 후 resolve" 주석. 깨진 배지 노출이 싫으면 pmd 배지는 PR#5 머지 시 추가.
- ArchUnit↔Java25: ArchUnit 1.4.2 파싱 OK(로컬 검증).
- 3rd-party action: gate 승격 전 commit SHA 핀닝 강화 권장(현재 test.yml은 `contents:read`라 즉각 위험 낮음; pmd.yml의 write 권한 step은 PR#5 소관).

## Testing Strategy

- 로컬(수행됨): `gradle test` green(ArchUnit 5/5), `worker pytest` 522 pass/2 skip/2 GC fail.
- push 후: PR에서 gradle-test green, worker-pytest는 GC 2 red(예상), pmd 코멘트.
- 회귀 가드: `worker/tests/test_repo_readme_boundary.py`의 2함수
  (`test_repo_readme_names_neurons_as_server_authority`, `test_repo_readme_keeps_rag_ingress_as_service_lane_not_repo_identity`) green 유지.
  README 배지는 가드 단언 문자열 영역 밖에 추가.
- python: worker는 uv auto-download(requires-python>=3.13, `.python-version` 없음) — 동작하나 Testing Strategy에 기록.
- 로컬 gradle 버전 드리프트 주의(wrapper 없음, CI는 9.5.1).

## Milestones

- **M1** (로컬): test.yml 정정 — ① gradle-test job `name`을 `gradle-test`로, ② `concurrency` 추가, ③ README CI 배지 2개. 검증: 로컬 gradle test green, YAML 유효, 가드 테스트 green, 배지 markdown 형식 유효.
- **M2** (로컬, 검증·문서 only): pmd.yml(PR#5)과 트리거 동치 조건 확인·기록. pmd.yml 직접 편집 안 함.
- **M3** (outward, **사전승인됨**): 커밋 → push → PR 생성. 검증: PR에서 Actions 실행, gradle-test green.
- **M4** (outward, **사전승인됨**): branch protection으로 `gradle-test` required check 설정.
  절차: (a) gradle-test 1회 green 확인 → (b) `gh api repos/pureliture/neurons/commits/<sha>/check-runs --jq '.check_runs[].name'`로 **실제 게시 context 이름 확인** → (c) 그 이름 1개만 `required_status_checks.contexts`에 PUT (worker-pytest/pmd 절대 제외) → (d) 더미 red PR로 차단 검증 → (e) **rollback/abort**: 오설정 시 `gh api -X PUT .../branches/main/protection` contexts=[] 또는 `gh api -X DELETE repos/pureliture/neurons/branches/main/protection`. admin 권한·gh auth 토큰 사전 확인.
- **M5** (deferred): GC task(`task_9c045496`) green 후 worker-pytest를 required check로 승격.

## Open Questions

- PR 분할: worktree에 doc/ArchUnit/CI 섞임 → M3에서 단일 PR vs doc/ci 분리. (실행 시 결정)
- README pmd 배지: 지금 추가(PR#5 머지 전까지 unknown 표시) vs PR#5 머지 후 추가.
