# Neurons Public/Private Separation — Design Spec

> public 저장소에 커밋된다. 실제 사적 값(호스트명·tailnet·경로·해시·키)은 본문에
> 적지 않고 카테고리로만 지칭한다. 사적 패턴 목록은 **public에 커밋하지 않는다**(아래
> Component 4 참조).

## Overview

이미 PUBLIC인 `neurons`를 boundary contract(`docs/public-private-separation.md`)에
정합시킨다: 사적 항목을 현재 트리와 **main 히스토리**에서 제거하고, 사적 운영물을
`neurons-ops`로 스테이징하며, manifest + contract test + leak-scan으로 재발을 막는다.
모든 비가역(force-push, neurons-ops push)은 사람 승인 게이트 뒤에 둔다.

## Requirements Reference

- Phase 1 source: `requirements.md` (D1~D5, FR1~FR9)
- Boundary contract: `docs/public-private-separation.md`
- Deploy seam 선례: `deploy/k3s/public-contract/ops-overlay-contract.yaml`
- 핵심: 코드 시크릿은 0건(전부 env 주입); 노출은 토폴로지·식별자·라이브 증빙.
  rewrite 대상은 `main` 단일, 신규 브랜치는 방치 후 unique 커밋만 후속 cherry-pick.

## Architecture

```
                 ┌─────────────────────────────┐
                 │ separation-manifest (SoT)    │  경로→disposition
                 │  public / ops / sanitize     │  (정책의 실행가능 투영)
                 └───────────────┬─────────────┘
        ┌────────────────────────┼───────────────────────────┐
        ▼                        ▼                            ▼
  [C2 Sanitizer]          [C3 Ops-Stager]              [C4 Leak-Scanner]
  in-place 정리           private 항목 로컬 스테이징     pattern+path 0건 검증
  (reversible)            (push 없음, 게이트)           (현재트리 / 전히스토리)
        │                        │                            │
        └───────────┬───────────┘                            │
                    ▼                                         ▼
            [C5 History-Rewriter]  ◄── dry-run on throwaway clone, scan green
            filter-repo replace-text + invert-paths (main 단일)
                    │  (force-push = 사람 게이트)
                    ▼
            [C6 Guard]  contract test + CI leak-scan (재발 방지)
                    │
                    ▼
            [C7 Policy-doc re-land]  6fc366b → clean main (PR)
```

## Data Flow

1. 인벤토리(완료된 6-차원 조사) → **C1 manifest** 작성.
2. manifest의 `sanitize-then-public` → **C2** in-place 정리 → C4 현재트리 scan green.
3. manifest의 `private-neurons-ops` → **C3** 로컬 스테이징(이동 패치).
4. **C5** throwaway clone에서 main을 rewrite(C2 결과 + C3 제거 반영) → **C4 전-히스토리
   scan green** → diff 요약. **여기까지 push 없음.**
5. 사람 게이트 통과 → C5 force-push clean main → fork/캐시 확인.
6. **C6** guard를 clean main에 정착 → **C7** 정책문서 re-land.

## Component Details

### C1 separation-manifest
- 입력: tracked 파일 목록 + 6-차원 인벤토리.
- 출력: `deploy/.../separation-manifest.yaml`(가칭) — 경로(또는 glob)→`{disposition,
  rule, mechanic}`. mechanic ∈ {keep, replace-text, invert-path, env-stub, gitignore}.
- 의존: 없음. 정책 문서의 실행가능 투영.

### C2 Sanitizer
- 입력: manifest의 `sanitize-then-public` 항목.
- 동작: 사적 호스트/경로/포트/볼륨명/alias/옛 사용자경로 → placeholder·env 주입화;
  `.env.example` 표준 env 스텁(FR3, compose 소비 검증 후); `.gitignore` 보강 +
  추적 중 `__pycache__/*.pyc`·`.agents/` 제거(FR4).
- 출력: reversible 커밋(현재 트리). 의존: C1.

### C3 Ops-Stager
- 입력: manifest의 `private-neurons-ops` 항목.
- 동작: neurons-ops용 이동 패치/매니페스트를 **로컬 스테이징**(scratchpad 또는
  gitignored staging). **push 없음.**
- 출력: 스테이징 산출물 + 어떤 파일이 어디로 가는지 목록. 의존: C1.

### C4 Leak-Scanner (fail-closed)
- 입력: 검사 트리 또는 git 히스토리; **사적 pattern 목록**.
- 동작: credential-shaped 정규식 + 사적 패턴(호스트 alias·tailnet·사적 경로·라이브
  포트·라이브 해시 카테고리) 매칭. 1건이라도 있으면 비-0 종료.
- **pattern 목록은 public에 커밋 금지** — 실제 사적 문자열을 담으므로 neurons-ops
  보관 또는 런타임 주입(gitignored). public에는 "스캐너 코드 + 카테고리"만.
- 출력: 0건 여부 + 위치. 의존: 없음(독립 게이트).

### C5 History-Rewriter (main 단일)
- 입력: 사적 pattern 목록 + invert 대상 경로 목록(C1) + 사용자 승인.
- 동작: **throwaway clone**에서 `git filter-repo --replace-text <patterns>
  --invert-paths --paths-from-file <private-files>`를 `main`에 적용 → C4 전-히스토리
  scan green → diff 요약. 신규 브랜치는 대상 아님.
- 비가역: **force-push는 사람 게이트.** 통과 후 push + fork 확인 + 필요시 GitHub
  Support 캐시 purge 안내. Tailscale rename은 사용자 비동기 수행 권고(저장소 밖).
- 의존: C2, C3, C4.

### C6 Guard
- contract test(기존 `K3sMigrationContractTest` / `test_repo_instructions` 계열 확장)로
  manifest 정합 + 미분류 경로 fail-closed. CI에 leak-scan(현재 트리) 잡 추가.
- 의존: C1, C4.

### C7 Policy-doc re-land
- 닫힌 PR #39의 정책문서 커밋(로컬 6fc366b)을 clean main 위로 cherry-pick → PR.
- 의존: C5(clean main).

## Error Handling

- C4가 어디서든 비-0 → 해당 단계 중단, 누출 위치 보고, 비가역 진행 금지.
- C5 dry-run scan이 green이 아니면 force-push 게이트 진입 자체를 막는다.
- filter-repo가 clean clone/uncommitted 요건 위반 → 중단·보고(임의 강제 금지).
- cherry-pick(C7/후속) 충돌 → 중단·보고, SoT 임의 수정 금지(grill-to-spec 상류 회귀).

## Testing Strategy

- 단위: manifest 파서/분류, leak-scanner 정규식(양성/음성 픽스처 — 사적 값 없이
  generic shape만), sanitizer 변환.
- 계약: contract test가 모든 tracked 경로의 disposition 존재 + 미분류 fail-closed.
- 통합: throwaway clone rewrite 후 전-히스토리 leak-scan green; 기존 `gradle test` /
  `worker pytest` 전수 green 유지.

## TDD Strategy

code-changing 구간(C1 manifest 파서·C4 스캐너·C2 변환·C6 guard)은 red→green→refactor.
C5 history rewrite는 docs/ops 성격이나 **검증 우선**: throwaway clone에서 전-히스토리
leak-scan green을 push 전 게이트로 강제(substitute evidence). 사적 pattern 목록은
테스트에 담지 않고 런타임 주입.

## Milestones

- **M1 manifest + guard 골격** — 전 tracked 경로 disposition 부여, 미분류 fail-closed
  contract test. Done: 테스트 green, 미분류 0.
- **M2 sanitize 현재 트리** — C2 적용 + `.env.example`/`.gitignore`/pyc·.agents 정리.
  Done: C4 현재트리 scan green, `gradle test` + `worker pytest` green.
- **M3 leak-scanner + CI 게이트** — C4 도구(현재트리/히스토리 모드) + CI 잡. pattern
  목록은 비공개 소스. Done: 도구 동작, 현재트리 0건, CI 잡 추가.
- **M4 ops 스테이징** — C3로 private 항목 로컬 스테이징(push 없음). Done: 스테이징
  매니페스트 + 이동 목록.
- **M5 history rewrite dry-run (main)** — throwaway clone에서 rewrite + 전-히스토리
  scan green + diff 요약. **push 없음.** Done: dry-run clone 0건 evidence.
- **M6 [사람 게이트·비가역] cutover** — clean main force-push → fork/캐시 확인 →
  Tailscale rename 리마인드. Done: 사용자 승인 + push 완료 + 후속 확인.
- **M7 정책문서 re-land** — 6fc366b → clean main PR. Done: PR 머지.
- **후속(비대상·deferred)** — 신규 개발 브랜치 unique 커밋을 clean main 위로 커밋별
  스캔 후 cherry-pick.

agentic-execution은 **M1~M5까지(전부 reversible/로컬)** act→observe→adjust로 수행하고,
**M6에서 정지**하여 사람 승인을 받는다. M6/M7은 게이트 통과 후 진행.

## Open Questions

- OQ1: 라이브 컬렉션명·dogfood alias = env 파라미터화 vs placeholder 치환.
- OQ2: 후속 cherry-pick 대상 브랜치/커밋 범위 확정.
- OQ3: public 이미지 빌드 CI 도입 여부(도입 시 registry 자격은 neurons-ops).
- OQ4: manifest 정식 경로/포맷(`deploy/` 하위 vs 전용 디렉터리).
