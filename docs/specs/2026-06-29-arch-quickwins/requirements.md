# Architecture Quick-Wins (issue #40) — Requirements

## 승인 대상

- Source of truth: `requirements.md`
- 입력: GitHub issue #40 "Architecture Deepening Review" + 첨부 `architecture-review.html`
- 대상 브랜치: 로테이션 완료된 clean main (b654ba8)

## 배경 / 문제

issue #40은 read-only 아키텍처 리뷰로 7개 개선 후보 + 5개 양호 구조를 도출했다.
본 cycle은 그중 **저위험·고레버리지 quick-wins 3개**만 구현한다.

## Scope (자문자답)

### Q: 7개 후보 중 이번 cycle에 무엇을 구현하나?
A: review가 TOP으로 꼽고 Deletion-Test PASS/저노력인 **#1, #3, #7**만. 나머지는 defer:
- **#1 shim 12개 삭제** (Strong·저노력·PASS) — 채택
- **#3 compose env DRY (YAML anchor)** (Strong·저노력·PASS) — 채택
- **#7 .env.example 범위 정합 + 커버리지 가드** (저노력·gap) — 채택
- **#2 Ledger god-class 4800줄** — defer (고노력·Deletion-Test FAIL·cross-area 의존, 별도 cycle + 자체 spec 필요)
- **#4 Java↔Python TargetProfile SSOT** — defer (중노력·별개 계약 작업)
- **#5 RagFlowTargetAdapter 503줄** — defer (중노력·"worth exploring")
- **#6 llm_brain_core flat 패키지** — defer (중노력·"worth exploring")

근거: 3개는 서로 독립적이고 기존 테스트로 검증 가능하며, 고위험 god-class 리팩터와
얽히지 않는다. YAGNI + 위험 격리.

### Q: 검증 권위는?
A: 기존 테스트 그대로가 spec이다. `worker` pytest 전수 + Java `gradle test`
(`ComposeConfigTest`, `ArchitectureRulesTest`). #7은 신규 커버리지 assertion을 추가한다.

### Q: #1의 caller 범위는? (prod 코드 영향?)
A: 인벤토리 결과 old root-path importer는 **worker/tests 22파일 + worker/eval 1파일뿐,
lib prod 코드 0**. 따라서 prod 동작 무변경, 테스트/eval import 경로만 기계적 치환.

### Q: #3 anchor가 ComposeConfigTest를 깨지 않나?
A: `ComposeConfigTest`는 `Files.readString()` + `contains()` raw-string 검사라 YAML을
파싱하지 않는다. anchor 본문에 키-값이 문자열로 그대로 존재하므로 기존 어서션 전부 green
유지. Docker Compose V2/V1 모두 YAML merge key 네이티브 지원이라 런타임 안전.

### Q: #7에서 root .env.example에 넣을 것과 뺄 것은?
A: 필수(`${VAR:?}`) 누락 2개(MCP_HTTP_HOST, LLM_BRAIN_ENV_FILE)와 dataset parity 갭
(RAGFLOW_TASK_SUMMARY_DATASET_ID)을 추가. 주요 optional 그룹(MCP_HTTP_PORT, Qdrant
mirror, RAG_INGRESS_* live-lane)은 주석 섹션으로 문서화. session-memory 전용
(RUN_MODE/SM_STATE_DIR)은 별도 compose project라 root에 넣지 않음(defer).

## 기능 요구사항

- FR1 (#1): 12개 pass-through shim(`worker/lib/agent_knowledge/{memory_card,memory_miner,
  memory_regeneration,transcript_model,transcript_ingest,transcript_packer,
  transcript_chunking,transcript_parsers,backfill,curation,query_planner,
  tool_evidence_sync}.py`)을 삭제하고, 모든 caller를 `agent_knowledge.session_memory.X`로
  치환한다(worker/tests 22 + worker/eval 1). `test_worker.py` importlib 문자열 12개와
  `test_transcript_ingest_worker.py`의 shim assertion도 직접 경로로 갱신.
- FR2 (#3): compose.yaml에 anchor 2개 도입 — `x-ingress-java-env`(13키: ingress-api +
  ingress-worker), `x-llm-brain-worker-env`(19키: llm-brain-tools + graph-trigger +
  bulk-semantic-trigger). 서비스 고유 키(SPRING_PROFILES_ACTIVE 등)와 bulk-semantic의
  `LLM_BRAIN_GRAPH_EXTRACT_ENTITIES: "false"` override는 anchor 밖에 유지. mcp/worker-py는
  anchor 미적용.
- FR3 (#7): root `.env.example`에 필수 누락 2개 + dataset parity 1개 추가, 주요 optional
  그룹 주석 문서화, profile 섹션 구획. `ComposeConfigTest`에 "compose의 모든 `${VAR:?}`
  필수 var가 .env.example 키에 존재" 커버리지 assertion 추가.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| prod 동작 | 무변경 (#1 import-only, #3 resolved-env 동일, #7 docs/test only) |
| 검증 | worker pytest 전수 green, gradle test green, #7 신규 assertion red→green |
| 가역성 | worktree 격리(claude/issue-40-arch-quickwins), main 직접수정 없음 |
| 안전 | 라이브 호스트/시크릿/런타임 미접촉 |
| YAGNI | #2/#4/#5/#6 비구현, session-memory env 별도화 defer |

## 사용자 시나리오

- S1: 기여자가 `agent_knowledge/` 루트에서 memory_card.py를 보면 = 실제 구현(루트 41→29파일).
- S2: 운영자가 compose env default를 바꿀 때 anchor 1곳만 수정 → 서비스 간 silent divergence 제거.
- S3: 새 운영자가 .env.example만 보고 필수 env를 빠짐없이 설정. 필수 var 누락은 CI가 차단.

## 미결정 / 후속

- OQ1: #7 optional var(Qdrant/RAG_INGRESS_*) 주석 vs placeholder 표기 — 주석 채택.
- OQ2: session-memory 전용 .env.example 신설 여부 — 이번 defer.
- OQ3: compose anchor YAML 유효성 CI 검사(docker compose config) 도입 — 이번엔 실행 중
  smoke로만, CI 도입은 follow-up.
- OQ4: #2/#4/#5/#6 후속 cycle 일정.
