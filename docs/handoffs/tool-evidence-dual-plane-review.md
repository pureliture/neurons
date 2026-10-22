# 핸즈오프: tool evidence의 이중 계층 구조 판단

## 목적

neurons에서 tool 실행 정보가 **두 개의 서로 다른 소비 경로**로 나뉘어 들어가는 구조가
생겼습니다. 이 구조가 의도된 것인지, 아니면 한쪽이 불필요한 중복인지 판단하고
결정해 주세요. 판단 근거는 코드 실측으로 이미 정리되어 있습니다.

## 현재 상태

브랜치: `claude/antigravity-tool-evidence-compaction` (격리 worktree에서 작업).
이 PR은 `origin/main` 위에 antigravity 커밋 4개만 cherry-pick한 형태다.

```
ef926a0 fix(antigravity): tool 결과를 turn 텍스트에서 제거하고 고신호 요약만 남김
bacbfee fix(antigravity): tool 호출과 결과를 짝지어 evidence로 만들고 entity pass에 편입
d64aff9 docs(evidence): 화이트리스트 계약과 드롭 근거를 코드에 명시
```

검증: `cd worker && uv run pytest -q` → 4242 passed, 0 failed, 524 skipped

## 배경: antigravity에서만 문제가 있었다

Claude/Codex/Gemini/Grok은 tool 호출이 `tool_use`/`tool_result` **content block**으로
전달되므로, 파서 단계에서 `text` 키만 추출하면 tool 결과가 자연스럽게 분리됩니다.

antigravity는 셸 step을 **두 레코드로 나눕니다**:

```
PLANNER_RESPONSE 레코드  → tool_calls에 명령, content는 빈 문자열 ""
GENERIC 레코드 (바로 다음) → "Created At:" 헤더 + exit code + 실제 출력
```

그래서 `_extract_message_text`의 text 블록 필터가 통하지 않아, 원본 stdout이
turn 텍스트 → conversation chunk → session memory → Graphiti entity extraction
입력까지 그대로 흘렀습니다.

## 해결한 것 3가지

### 1. 원본 stdout 제거 (`ef926a0`)

`_strip_antigravity_tool_result` — `"Command output:"` 마커 이후를 제거하되
exit-code 라인은 유지. 타임스탬프 헤더와 exit code는 "명령이 실행돼서 이걸로
끝났다"는 신호고 출력 바이트가 없기 때문입니다.

`_antigravity_tool_summary` — antigravity는 tool 호출마다 이미 `toolSummary`
라벨을 씁니다("Check running ollama and chatgpt processes"). 기존 코드는 이를
무시하고 인자 전체를 JSON 덤프했습니다. 라벨 우선, 없을 때만 식별 인자
(명령줄/대상경로) fallback.

실측: turn 텍스트 108,367자 → 31,167자 (-71%). extraction_text 내
`Command output:` / `CodeContent` / `argumentsJson` 모두 0회.

### 2. evidence가 출력을 못 읽던 버그 (`bacbfee`)

`extract_antigravity_tool_evidence`가 `output = record["content"]`를 읽는데
호출 레코드의 content는 항상 빈 문자열이었습니다. 호출 레코드와 다음 레코드를
짝지도록 수정.

| | 수정 전 | 수정 후 |
|---|---|---|
| result가 빈 evidence | 219건 (8%) | 0건 (0%) |
| 파일 커버리지 | 75% | 82% |
| outcome 판별 | 없음 | pass 11 / error 84 |

수정 후 실제 출력:

```
command: uv run pytest -q       result: 198 passed in 6.08s
command: git status --short     result: git status: 3 file(s) changed ## HEAD
command: sqlite3 ... .schema    result: Command exited with code 0. CREATE TABLE ...
```

### 3. Graphiti entity pass에 evidence 편입 (`bacbfee`)

`_extraction_text_from_evidence` (신규) — materialization 시점에 이미 확보되는
`tool_evidence_bundles`에서 evidence 섹션만 뽑아 extraction 입력 뒤에 붙입니다.
별도 예산 2,000자(대화 prose 8,000자를 밀어내지 않게). `episode_id`는
`extraction_text`를 content_hash에서 제외하므로 영향 없습니다.

## 판단이 필요한 지점 (이 세션이 넘기는 것)

### 핵심 질문: 같은 정보가 두 계층에 존재하는 게 맞나

tool 실행 정보가 이제 두 경로로 나뉩니다.

| | recall 경로 | Graphiti entity pass |
|---|---|---|
| 진입점 | `pack_session_memory_document` (memory_regeneration.py:1279) | `session_episode_from_couchdb_source` (runtime.py:714) |
| 입력 | chunk prose + `## Tool Evidence` 섹션 | chunk prose(8000자) + evidence addendum(2000자) |
| evidence 소스 | `list_tool_evidence_summaries` (ledger) | `resolved.tool_evidence_bundles` (CouchDB) |
| evidence 형식 | `### N cat/outcome`, `- command:`, `- result:` | `### N cat/outcome`, `- tool:`, `- command:`, `- result:` |
| 소비 | 검색/리콜 (Qdrant 등 검색 미러) | 엔티티·관계 추출 후 Neo4j 그래프 |

**질문 1**: 이 이중 구조가 정당한가, 아니면 한쪽이 중복인가?
- 정당하다고 보면: 두 경로의 질문이 다르다. recall은 "이 세션에서 무슨 일이
  있었나"를 찾고, graph는 "무엇이 무엇과 관련된entity인가"를 만든다. 그래서
  recall이 "무엇을 했는지"를 담고, graph는 "무엇이 무엇과 관련된entity인가"를
  만든다.
- 중복이라고 보면: 같은 tool_evidence 레코드가 두 저장소(ledger, CouchDB)에서
  따로 읽혀 같은 내용으로 두 문서에 들어간다. 한쪽 소스(canonical)로 통일할 수
  있는가?

**질문 2**: 두 경로의 evidence 렌더링 형식이 미세하게 다르다
(`- tool:` 줄 유무). 이 차이가 의도된 것인가, 아니면 하나의 렌더러로 통일할
수 있는가? 현재 각각 `memory_regeneration.py:_sectioned_tool_evidence_lines`와
`runtime.py:_extraction_text_from_evidence`가 따로 파싱한다.

**질문 3**: recall 경로의 session_memory 문서에는 여전히 **원본 chunk prose**가
들어간다. antigravity는 이제 stdout이 제거된 prose만 담으므로 recall에도 원본
stdout은 없다. 그렇다면 recall이 tool 결과를 "어떤 명령 + 결과"로만 갖고
있는 현재 상태가 정답이며, 추가 조치가 필요 없는가?

## 참고: 이미 결정된 사항 (재검토 대상 아님)

사용자가 명시적으로 결정함 — **evidence 화이트리스트는 현행 유지**:

- 유지되는 category: `test_result`(pytest), `git_state`(status/diff/commit...),
  `live_proof`(curl), `command_error`(traceback), `approval_evidence`,
  `local_audit`(sqlite3, wc)
- 드롭: `ls`/`cat`/`grep`/`find` 같은 탐색 verb, 그리고 **분류 불가 명령 전체**
  (`npm run build`, `node --test`, `kubectl` 등)
- 근거(실측): 일반 "command ran, exit code N" category로 넓히면 파일당 평균
  evidence가 약 5건 → 약 30건으로 늘어나고, 2,000자 extraction addendum를
  밀어내면서 대화 prose가 이미 가진 신호 이상을 더하지 못함
- 이 결정을 `_classify_exec_evidence` docstring에 명시 (`d64aff9`)

## 조사 방법

로컬 실데이터: `~/.gemini/antigravity/**/transcript.jsonl` (577개 파일, live
provider lane). neurons는 `antigravity`를 `live_allowed=True` lane으로 등록하고
있고 (`couchdb_source/historical_import.py:71`),
`LIVE_CUTOVER_PROVIDERS`에도 포함 (`shadow_cutover.py:30`).

읽기 전용으로 측정:
```bash
uv run --project worker python -c "..."
```

## 제약

- 이 PR의 브랜치는 `origin/main` 위에 antigravity 커밋만 cherry-pick한 형태다.
  이어서 작업할 때는 이 브랜치 위에서 진행하거나, 격리 worktree의
  `claude/antigravity-tool-evidence-compaction`에서 계속한 뒤 다시 cherry-pick한다.
- main/master checkout에서 파일 변경 금지. 격리 worktree를 쓸 것.
- 라이브 서버(SSH alias `llm-brain-server`, k3s) 변경 금지. read-only 확인만.
- CLAUDE.md 가드레일 준수. 특히 "Retired external index bridge ... live GC,
  Docker/systemd/firewall/package mutation" 계열은 current evidence + explicit
  user intent 없이 실행 금지.
- 커밋 메시지 마지막 줄 `Co-Authored-By: Claude <모델명> <noreply@anthropic.com>`.
- 응답과 문서는 한국어, 코드 식별자는 영어 원문.

## done 기준

- 이중 계층 구조에 대한 판단이 근거와 함께 결정됨 (유지 / 한쪽 통합 / 한쪽 제거)
- 결정에 따른 코드 변경이 반영되고 pytest 통과
- 결정이 코드 주석 또는 설계 문서에 기록됨
- 최종 보고: 판단 근거, 무엇을 바꿨는지, 남은 리스크
