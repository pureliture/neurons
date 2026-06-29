# Neurons Public/Private Separation — Requirements

> 본 문서는 public 저장소에 커밋된다. 실제 사적 값(호스트명·tailnet·경로·해시·키)은
> 절대 본문에 적지 않고 **카테고리로만** 지칭한다.

## 승인 대상

- Source of truth: `requirements.md`
- Preview companion: (필요 시) `requirements.html`
- Boundary contract(상위 정책): `docs/public-private-separation.md`
- Deploy seam 선례: `deploy/k3s/public-contract/ops-overlay-contract.yaml`
  (`approach: public-contract-private-ops-overlay`)

## 배경 / 문제

`pureliture/neurons`는 **이미 PUBLIC**이고 `neurons-ops`는 이미 PRIVATE로
존재한다. 따라서 이 작업은 "미래 분리 설계"가 아니라 **이미 노출된 상태의 정리
(remediation) + 경계 강제(enforcement)**다.

조사(6-차원 read-only 인벤토리)로 확인된 현 노출:

- **실제 코드 시크릿(키·비밀번호·토큰)은 tracked 파일과 git 히스토리 전체에서 0건.**
  모든 크레덴셜은 env 주입(`${VAR}` / `os.environ`)이라 값이 코드에 없다.
- 노출된 것은 **시크릿이 아니라 토폴로지·식별자·라이브 증빙**이다:
  - CI 정의 파일의 사적 Tailscale 호스트명 / tailnet 식별자, 내부 노드 ID,
    private ops 저장소 URL.
  - 운영 런북류의 사적 SSH alias, 사적 홈 경로, 라이브 서비스 포트.
  - 아키텍처/스펙 문서의 라이브 ledger 스냅샷 지문(부분 해시)·row counts,
    k3s canary/dry-run live evidence, 라이브 RetiredIndexBridge 문서 파일명·해시.
  - 소스 일부의 라이브 컬렉션명, dogfood 프로젝트 alias, 옛 사용자 절대경로.

## 목표

1. 노출된 사적 항목을 public 저장소(현재 파일 + git 히스토리)에서 제거한다.
2. 사적 운영 항목을 private `neurons-ops`로 이동(스테이징)한다.
3. 경계를 manifest + contract test + leak-scan으로 **재발 불가**하게 강제한다.
4. public 저장소를 boundary contract(`docs/public-private-separation.md`)에 정합시킨다.

비목표(YAGNI): 완전한 public `clone → configure → run` 달성, neurons-ops k3s
재배치, 이미 노출된 식별자의 실세계 회수(불가) 자체는 본 작업 범위 밖이다.

## 핵심 결정 (사용자 확정)

- **D1 remediation 강도 = 전체.** 현재 파일 정리 + **git 히스토리 재작성**까지 한다.
- **D2 neurons-ops 이동 = 포함(스테이징까지).** 사적 항목을 neurons-ops용으로
  로컬 스테이징/패치까지 만들되, **실제 private push는 사람 승인 게이트**.
- **D3 히스토리 mechanic = 히스토리 보존(`git filter-repo --replace-text`
  + `--invert-paths`)**, orphan 스냅샷 아님. cherry-pick-skip 방식은 기각
  (사적 문자열이 다수 커밋에 번져 있고 파일이 HEAD에 필요해 수동 filter-repo가 됨).
- **D4 공개 ref만 동시 청소, 로컬 브랜치는 방치 후 나중에 unique 커밋만 cherry-pick.**
  제약은 "브랜치 1개"가 아니라 "**공개된 ref는 전부 청소**". (정리 결과 현재 공개
  더러운 ref = `main` 하나로 축소됨.)
- **D5 force-push는 사람 승인 게이트.** 자동화는 reversible 구간까지만.

## 자문자답 (gray-box 결정)

### Q: public 저장소를 새로 만들 것인가, 현재 neurons를 그대로 public 본체로 둘 것인가?
A: 현재 `neurons`를 public 본체로 유지한다. 기존 contract(`ops-overlay-contract.yaml`)가
`publicRepo: neurons` / `privateRepo: neurons-ops`로 이미 의도를 박아두었고, 저장소가
이미 public이라 신규 추출보다 in-place remediation이 정합적이다.

### Q: 분류(public/private)의 권위는 무엇인가?
A: `docs/public-private-separation.md`(정책) + 본 작업이 만드는 **머신 체크 가능한
separation manifest**(경로→disposition). manifest가 정책의 실행 가능한 투영이다.

### Q: 라이브 증빙 문서(ledger dry-run, k3s canary/dry-run evidence)는?
A: private-neurons-ops. 히스토리에서 `--invert-paths`로 파일째 제거 대상.

### Q: 운영 런북(사적 alias/경로 포함)은 sanitize vs 삭제?
A: live-ops 런북(사적 호스트/경로/포트/라이브 수치 포함)은 neurons-ops 이동.
설치/제품 가이드 성격은 sanitize 후 public 유지. manifest에서 파일별로 분기.

### Q: Jenkinsfile은?
A: private repo URL·tailnet·노드 ID를 담아 neurons-ops 소유. public에는 두지 않거나
placeholder 템플릿만. public CI는 이미 있는 GitHub Actions(test/pmd)로 충분.

### Q: 이미 노출된 식별자(tailnet명·호스트 alias)의 회수는?
A: 불가(이미 public). git 정리는 미래 복제 표면만 줄인다. **실세계 무력화는
rotation뿐이며, 진짜 시크릿은 없으므로 rotation 대상은 사실상 Tailscale 머신/​tailnet
rename 하나**(저장소 밖, 사용자 수행 권고). 나머지(alias/경로/repo명)는 시크릿이
아니라 rotation 불필요.

## 기능 요구사항

- FR1: 모든 tracked 경로에 disposition(`public` / `private-neurons-ops` /
  `sanitize-then-public`)을 부여한 **separation manifest**를 만든다.
- FR2: `sanitize-then-public` 파일을 in-place로 정리한다(사적 호스트/경로/포트/
  볼륨명/alias/옛 사용자경로 → placeholder, env 주입화). reversible 커밋.
- FR3: `.env.example`에 부재한 표준 env(`RETIRED_INDEX_BRIDGE_API_KEY` 등) placeholder 스텁 보강
  (단, 해당 compose 범위에서 실제 소비되는지 검증 후).
- FR4: `.gitignore` 보강(추적 중인 `__pycache__/*.pyc`, `.agents/` 등 제거+무시).
- FR5: `private-neurons-ops` 항목을 neurons-ops용으로 **로컬 스테이징**(이동 패치/
  매니페스트). 실제 push 없음.
- FR6: **leak-scan 게이트** — credential-shaped 문자열 + 알려진 사적 패턴(호스트
  alias·tailnet·사적 경로·라이브 포트·라이브 해시 카테고리)을 현재 트리와
  (rewrite 후) 전 히스토리에서 0건임을 fail-closed로 검증.
- FR7: **히스토리 재작성(main 단일 대상)** — `filter-repo --replace-text`(번진
  문자열 치환) + `--invert-paths`(전체 private 파일 제거)로 clean 히스토리 생성.
  rewrite 대상은 `main` ref 하나로 한정하고, **신규 개발 브랜치는 비대상으로 방치**한다
  (나중에 unique 커밋만 clean main 위로 cherry-pick, 커밋별 스캔). throwaway clone에서
  dry-run + 전수 leak-scan green까지 자동화, **force-push는 사람 게이트**.
- FR8: 경계 회귀 가드 — manifest를 강제하는 contract test(기존
  `K3sMigrationContractTest` / `test_repo_instructions` 계열 확장) + CI leak-scan.
- FR9: 정책문서 PR(닫힌 #39, 로컬 6fc366b)은 clean main 위로 cherry-pick하여 re-land.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| 가역성 | force-push/neurons-ops push 전 모든 단계는 로컬·되돌림 가능 |
| 비가역 게이트 | history force-push, neurons-ops push는 사람 승인 |
| 안전 | 라이브 호스트/시크릿/런타임 미접촉. 본 작업은 repo 메타·docs·tooling만 |
| 누출 검증 | leak-scan은 fail-closed. 현재 트리 + 전 히스토리 모두 0건 |
| 공개 안전 | 스펙·테스트·tooling 자체가 사적 값을 담지 않음(카테고리 지칭) |
| fork/캐시 | force-push 후 fork 존재 확인 + 필요시 GitHub Support 캐시 purge 안내 |

## 사용자 시나리오

- S1: 외부인이 public `neurons`를 clone → 사적 호스트/경로/증빙을 발견할 수 없다.
- S2: 기여자가 사적 값을 담은 커밋을 올림 → CI leak-scan이 fail-closed로 차단.
- S3: 운영자가 neurons-ops에서 실제 overlay/secret/evidence를 소유 → public은 contract만.
- S4: 사용자가 history rewrite 승인 → clean 히스토리 force-push, fork/캐시 후속 확인.

## 미결정 / 후속

- OQ1: 라이브 컬렉션명·dogfood alias를 env 파라미터화할지, placeholder 치환만 할지.
- OQ2: 로컬 잔존 브랜치(ontology-completion 등)의 unique 커밋 re-land 범위.
- OQ3: public 이미지 빌드/배포 CI를 둘지(둔다면 registry 자격은 neurons-ops).
- OQ4: Tailscale rename 실행 시점(사용자 수행, 본 작업과 비동기).
