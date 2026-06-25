# LLM Brain Bulk Semantic Lane 전환 Requirements

## 승인 대상

- Source of truth: `requirements.md`
- Preview companion: `requirements.html`
- 대상 repo: `neurons`
- 대상 branch: `claude/ontology-completion`

## 목표

운영 semantic enrichment를 per-session Graphiti `add_episode` entity extraction
중심에서 **bulk semantic lane** 중심으로 전환한다. Hot path는 빠른 metadata-first /
episode-only projection만 담당하고, 비싼 entity/relation 의미추출은 여러 session을
묶어 처리하는 후행 batch lane으로 분리한다.

근거(M7 full-scope drain 실측):
- bulk 결과 3563/3577 source-valid ceiling(99.61%).
- 남은 14건은 의미/모델 실패가 아니라 CouchDB source materialization 오류
  (`session source docs have inconsistent project`).
- bulk 비용: Gemma-4 chat 615 calls / 약 2.6M tokens / 약 $0.28, 약 735 tokens/session.
- 기존 qwen accumulated semantic window 대비 약 91% token / 90% cost 절감.

## 현재 상태 (코드 기준 grounding)

- Hot-path 트리거 `llm-brain-graph-trigger` → `couchdb-graph-trigger --execute`
  → child `couchdb-graph-project`에 **항상 `--extract-entities`** 를 넘긴다.
  즉 운영 트리거가 per-session Graphiti 의미추출을 inline으로 돌리고 있다
  (compose `LLM_BRAIN_GRAPH_EXTRACT_ENTITIES=true`).
- Bulk lane 코어는 이미 branch에 착륙:
  `bulk_semantic.py`, `bulk_semantic_cli.py`, 테스트 `test_couchdb_graph_bulk_semantic_cli.py`.
  CLI `couchdb-graph-bulk-semantic`로 등록됨. 이미
  `--max-projects / --limit / --progress-jsonl / --dead-letter-jsonl`,
  dry/execute, `graph-project.lock` 공유(`runtime_dir/graph-project.lock`),
  JSON/ValueError singleton 재시도까지 구현돼 있다.
- `graph-project.lock`은 **runtime-dir 단위** lock이다. 같은 lock을 공유하려면
  bulk lane과 hot-path 트리거가 **같은 runtime-dir**를 써야 한다.

## 질문-답변 흐름

### Q1: 새 bulk semantic lane을 운영에서 처음부터 자동으로 돌릴까, opt-in으로 둘까?

**A: 일단 꺼두기.** 별도 opt-in profile + `.env.example` enable 플래그 off로 출하한다.
운영자가 준비되면 직접 켠다. 첫 롤아웃 안전 우선.

### Q2: 빠른 hot path와 비싼 bulk lane이 Neo4j에 동시 write 충돌하지 않게 줄을 세울까?

**A: 줄 세우되 배치를 작게.** hot-path 트리거·manual full batch와 동일한
`graph-project.lock`을 공유(같은 runtime-dir)하고, bulk batch를 작게 cap해서
lock 점유 시간을 짧게 유지한다. `already_running`은 skip + report로 처리한다.

### Q3: 기존 bulk 도구는 그대로 쓰고, 깨진 14건은 이번 작업 밖으로 둘까?

**A: 그대로 + 14건 나중에.** 기존 `bulk_semantic.py` / `bulk_semantic_cli.py`는
동결하고 재사용만 한다. 14건 `inconsistent project`는 의미추출 실패가 아니라 원본
데이터 정합성 버그이므로 이번 작업 범위 밖(별도 작업)으로 둔다.

### Q4(설계자 결정): hot-path per-session 의미추출은 어떻게 끌까?

**결정: 트리거 기본 episode-only + env opt-in.** hot-path 트리거 wrapper가 기본적으로
`--extract-entities`를 넘기지 않는다. compose `LLM_BRAIN_GRAPH_EXTRACT_ENTITIES`
기본값을 false로 flip한다. per-session 의미추출은 debug/manual opt-in
(env 플래그 또는 직접 CLY)에서만 켜진다.

## 기능 요구사항

### F1. Lane 분리 (hot path vs semantic batch)
- Hot path(신규 session ingest, periodic 트리거, 기본 graph projection)는 LLM
  의미추출을 inline으로 돌리지 않는다. metadata-first / episode-only만 수행한다.
- Hot path 목적: 빠른 Episodic/metadata projection, recall continuity, low latency, low cost.
- Semantic enrichment는 후행 bulk batch lane으로 분리한다. 여러 session을 한 번에
  묶어 Entity/Relation을 추출하고 deterministic writer로 Neo4j에 적재한다.

### F2. Bulk semantic scheduler/trigger
- 내부 실행은 `neuron-knowledge couchdb-graph-bulk-semantic`.
- dry-run: mutation/network 없이 plan만 출력.
- execute: bounded(max-projects/limit)로 bulk semantic 호출.
- hot-path 트리거·manual full batch와 동일한 `graph-project.lock` 공유(같은 runtime-dir)로
  pileup 방지. `already_running` graceful 처리.
- progress JSONL / dead-letter JSONL 유지.
- public-safe output: raw path / secret / transcript body / raw session id / DSN 미출력.

### F3. Hot-path 기본값 flip
- 운영 기본 path에서 per-session Graphiti `add_episode` entity extraction 비활성.
- 트리거 wrapper 기본 episode-only(`--extract-entities` 미전달).
- compose `LLM_BRAIN_GRAPH_EXTRACT_ENTITIES` 기본 false.
- per-session 의미추출은 debug/manual opt-in만 허용(env default가 회귀로 켜지지 않음).

### F4. Bulk lane 활성화 정책
- 새 bulk semantic 트리거 서비스는 기본 비활성(off-by-default).
- 별도 opt-in compose profile(예: `llm-brain-bulk-semantic`)로 분리.
- `.env.example`의 enable 플래그 기본 off. 운영자가 명시적으로 켠다.

### F5. Env knobs
- 추가: `LLM_BRAIN_BULK_SEMANTIC_MAX_SESSIONS_PER_CALL`,
  `LLM_BRAIN_BULK_SEMANTIC_MAX_SESSION_CHARS`,
  `LLM_BRAIN_BULK_SEMANTIC_MAX_TOKENS`,
  `LLM_BRAIN_BULK_SEMANTIC_TIMEOUT_SECONDS`,
  `LLM_BRAIN_BULK_SEMANTIC_EMBEDDINGS`.
- bulk 트리거 운영 knob: enable 플래그, interval, limit, project, provider,
  report-every, max-projects.
- `.env.example` 기본값은 allowed Gemma-4 MaaS chat + embedding만 사용.

### F6. 모델 정책 (강제)
- chat/extraction은 Cloud Vertex AI Model Garden MaaS **Gemma-4**(`gemma-4-26b-a4b-it-maas`)만.
- embedding은 허용된 embedding 모델(`gemini-embedding-2`)만.
- **Gemini / Gemini Flash chat·extraction 금지.**

### F7. compose / runbook
- `llm-brain-graph-trigger`가 더는 per-session Graphiti semantic을 돌리지 않게 한다.
- 별도 `llm-brain-bulk-semantic-trigger` 서비스로 분리(off-by-default profile).
- runbook에 bulk lane enable/disable, dry-run, bounded execute, lock 공유, rollback 기록.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| dry-run 안전성 | mutation 0, network 0, plan만 출력 |
| public-safe output | raw session id / transcript body / secret / DSN / raw path 미출력 |
| pileup 방지 | hot-path·manual·bulk가 `graph-project.lock` 공유, already_running graceful |
| lock 점유 | bulk batch cap으로 점유 시간 짧게 → hot path stall 최소화 |
| 관찰성 | progress JSONL + dead-letter JSONL 유지 |
| 복원력 | JSON/ValueError singleton fallback 보존 |
| 회귀 방지 | env default가 per-session semantic extraction으로 회귀하지 않음 |
| 비용/모델 | Gemma-4 MaaS + 허용 embedding만, Gemini/Flash chat·extraction 금지 |
| 런타임 인지 | 라이브 런타임은 `NEURON_LEDGER_PG_DSN`(PG) 사용. host `.env` SQLite CLI와 컨테이너 env 혼동 금지 |

## 운영 시나리오

- **신규 session 유입**: hot path가 episode/metadata만 빠르게 projection. 의미추출 없음.
- **의미 enrichment(운영자 opt-in)**: 운영자가 bulk profile/플래그를 켜면 bulk 트리거가
  여러 session을 묶어 Entity/Relation 추출 후 Neo4j 적재. 공유 lock으로 hot path와 직렬화.
- **검증/롤아웃**: dry-run으로 plan 확인 → bounded execute(`--max-projects 1~2`)로 소규모
  실행 → 결과/JSONL 확인. 라이브 mutation은 exact argv/postcheck/rollback 기록.
- **롤백**: bulk 트리거는 off-by-default라 profile/플래그를 끄면 lane 정지. hot path는
  episode-only로 계속 동작(recall continuity 유지).

## 테스트 요구사항

- dry-run은 mutation/network 없이 plan만 출력.
- execute는 bounded max-projects/limit로 bulk semantic 호출.
- lock already-running 처리.
- JSON/ValueError singleton fallback 보존.
- env default가 per-session semantic extraction으로 회귀하지 않음(hot-path episode-only 검증).
- public-safe output(raw 식별자/secret/DSN 미출력).

## 검증 요구사항

- worker targeted tests.
- full worker tests (`cd worker && uv run pytest -q`).
- root Gradle tests (`JAVA_HOME=... gradle test`).
- Ubuntu redeploy 후 smoke:
  - graph-required context resolve
  - graph status
  - bulk 트리거 dry-run
  - small bounded bulk execute(가능하면 `--max-projects 1~2`)
- 라이브 scheduler enable/redeploy는 mutation → exact argv, postcheck, rollback 기록.

## 범위 밖 (Out of Scope)

- 기존 `bulk_semantic.py` / `bulk_semantic_cli.py` 내부 로직 변경(동결, 재사용만).
- 14건 `inconsistent project` CouchDB source materialization 버그 수정(별도 작업).

## 미결정 / 제안 기본값 (승인 시 확정)

| 항목 | 제안 기본값 | 비고 |
| --- | --- | --- |
| bulk 트리거 enable | off | 운영자 opt-in |
| `LLM_BRAIN_BULK_SEMANTIC_EMBEDDINGS` | true | `.env.example`이 embedding 허용 명시 |
| bulk 트리거 interval | hot path(300s)보다 길게(예: 900s) | off-by-default라 위험 낮음 |
| bulk batch cap (max-projects/sessions-per-call) | 작게(M7 실측 기반) | lock 점유 최소화, Phase 2에서 수치 확정 |
| 트리거 wrapper 구현 형태 | 신규 thin wrapper CLI vs compose-loop 직접 호출 | Phase 2 approach proposal에서 결정 |
| Enrichment gap 수용 | hot-path off + bulk off 동안 신규 session은 bulk 켤 때까지 의미추출 없음 | recall은 episode/metadata로 유지. 수용 여부 확인 필요 |
