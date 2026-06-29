# Architecture Quick-Wins (issue #40) — Design Spec

## Overview

issue #40 리뷰의 저위험 quick-wins 3개(#1 shim 삭제, #3 compose env DRY anchor,
#7 .env.example 정합+가드)를 clean main 위에서 구현한다. 세 작업은 독립적이며 prod
동작을 바꾸지 않는다(import-only / resolved-env 동일 / docs+test).

## Requirements Reference

- Phase 1 source: `requirements.md` (FR1~FR3, scope #1/#3/#7, #2/#4/#5/#6 defer)
- 검증 권위: 기존 worker pytest + Java gradle test; #7은 신규 coverage assertion.

## Architecture

세 개의 독립 작업 단위. 공유 컴포넌트 없음.

```
M1 (#1 shims)      M2 (#3 compose anchor)     M3 (#7 env coverage)
worker/lib + tests compose.yaml               .env.example
   ↓ import rewrite    ↓ x-anchor merge          ↓ add vars + section
delete 12 shims     preserve per-service       ComposeConfigTest
   ↓                   + bulk override            ↓ new @Test (required ⊆ documented)
worker pytest       ComposeConfigTest green    gradle test
```

## Component Details

### M1 — shim 삭제 (#1)
- 입력: 12 shim 파일, 23 caller(test 22 + eval 1), test_worker importlib 문자열, transcript_ingest assertion.
- 동작: caller의 `agent_knowledge.X` → `agent_knowledge.session_memory.X` 치환 → 12 shim 삭제.
- 의존: 없음. session_memory/<name> 타깃 전부 존재 확인됨. public_safe_util은 이미 직접 경로.
- 출력: 루트 패키지 41→29 파일, import-only 변경.

### M2 — compose anchor (#3)
- 입력: compose.yaml의 중복 env 블록 2그룹.
- 동작:
  - `x-ingress-java-env: &ingress-java-env` (13키) → ingress-api, ingress-worker가 `<<: *ingress-java-env`.
    서비스 고유 유지: ingress-api `SPRING_PROFILES_ACTIVE: api`; ingress-worker
    `SPRING_PROFILES_ACTIVE: worker` + `SPRING_MAIN_WEB_APPLICATION_TYPE: none`.
  - `x-llm-brain-worker-env: &llm-brain-worker-env` (19키) → llm-brain-tools(고유 0),
    graph-trigger(고유 5), bulk-semantic-trigger(고유 12 + `LLM_BRAIN_GRAPH_EXTRACT_ENTITIES: "false"` override 유지).
  - mcp(env_file 사용), ingress-worker-py(구조 상이)는 anchor 미적용.
- 의존: ComposeConfigTest(raw-string, 무변경 green).
- 출력: resolved-env 동일, env 정의 중앙화.

### M3 — .env.example 정합 + 가드 (#7)
- 입력: compose.yaml `${VAR:?}` 필수 집합, 현재 .env.example 키 집합.
- 동작:
  - 필수 추가: `MCP_HTTP_HOST`(주석: tailnet IP, 절대 0.0.0.0 금지), `LLM_BRAIN_ENV_FILE`
    (주석: deploy-time 서버측 경로, repo .env 아님).
  - parity 갭: `RETIRED_INDEX_BRIDGE_TASK_SUMMARY_DATASET_ID`(7번째 dataset id) 추가.
  - optional 주석 섹션: MCP_HTTP_PORT, Qdrant mirror(MIRROR_DUAL_WRITE/QDRANT_*),
    RAG_INGRESS_* live-lane. profile 섹션으로 구획.
  - `ComposeConfigTest`에 `@Test envExampleCoversAllRequiredComposeVars()`:
    compose.yaml에서 `${VAR:?}` var 추출 → .env.example `^KEY=` 키와 비교 →
    required ⊆ documented assert.
- 의존: ComposeConfigTest(같은 파일에 메서드 추가).
- 출력: 필수 env 100% 문서화 + CI 가드.

## Data Flow

각 M은 독립. M3의 신규 assertion이 TDD 축(현재 {MCP_HTTP_HOST, LLM_BRAIN_ENV_FILE} 누락
→ red → 추가 → green). M1은 기존 import 테스트가 spec(치환 → green). M2는 기존
ComposeConfigTest green + `docker compose config` smoke.

## Error Handling

- M1: 치환 누락 시 ImportError로 pytest 즉시 실패 → 누락 caller 식별. test_worker importlib
  문자열은 grep로 12개 전수 확인(단 agent_knowledge.document_envelope는 shim 아님 — 제외).
- M2: anchor 오타/들여쓰기 오류는 raw-string 테스트가 못 잡음 → 실행 중 `docker compose
  config --quiet` smoke로 YAML 유효성 + merge 해석 확인. bulk override 누락 회귀 주의
  (resolved-env grep로 EXTRACT_ENTITIES=false 확인).
- M3: 신규 assertion이 optional(`${VAR:-}`)이 아닌 required(`${VAR:?}`)만 강제 → 거짓양성 없음.

## Testing Strategy

- M1: `cd worker && uv run pytest -q` 전수 green(특히 test_memory_card/miner/curation/
  ledger_transaction/worker/transcript_ingest_worker/tool_evidence_*/llm_brain_slice*).
- M2: `gradle test`(ComposeConfigTest green) + `docker compose -f compose.yaml config --quiet`
  (docker 가용 시) + resolved-env spot check(SPRING_PROFILES_ACTIVE api/worker, bulk EXTRACT_ENTITIES false).
- M3: 신규 assertion red(현재 갭) → 변수 추가 후 green; `gradle test` 전수 green.

## TDD Strategy

code-changing 작업. M3 신규 assertion은 red→green TDD. M1/M2는 기존 테스트가 실행 가능한
spec 역할(치환/anchor 후 green이 evidence). M2의 YAML 유효성은 test seam이 약해
`docker compose config` smoke를 substitute evidence로 사용(docker 부재 시 사유 기록).

## Milestones

- **M1 shim 12개 삭제 + caller 치환** — Done: 12 shim 파일 부재, 23 caller + test_worker
  문자열 + transcript_ingest assertion 갱신, `worker` pytest 전수 green, 루트 패키지 41→29.
- **M2 compose env anchor 2개** — Done: x-ingress-java-env(13)/x-llm-brain-worker-env(19)
  도입, 서비스 고유키 + bulk override 보존, ComposeConfigTest green, `docker compose config`
  smoke green(또는 docker 부재 사유 기록), resolved-env spot check 일치.
- **M3 .env.example 정합 + coverage 가드** — Done: 필수 2 + parity 1 + optional 주석 추가,
  ComposeConfigTest 신규 assertion red→green, `gradle test` 전수 green.
- (defer) #2/#4/#5/#6 후속 cycle.

세 M은 독립이라 순서 자유. 권장: M3 assertion(red) → M1(기계적) → M2(anchor) → 전수 재검증.
agentic-execution은 M1~M3를 act→observe→adjust로 수행, 각 M evidence 확보 후 PR.

## Open Questions

- OQ1: optional var 주석 표기(채택) vs placeholder.
- OQ2: session-memory 전용 .env.example 신설(defer).
- OQ3: compose anchor YAML 유효성 CI 검사 도입(follow-up).
- OQ4: 결과를 PR로 main에 올릴 때 main 보호(check 필요) 통과 경로.
