# LLM Brain Bulk Semantic Lane Design Spec

## Overview

운영 semantic enrichment를 per-session Graphiti `add_episode` extraction에서 후행
bulk batch lane으로 전환한다. Hot path(`llm-brain-graph-trigger`)는 episode-only
projection만 남기고, 비싼 Entity/Relation 추출은 여러 session을 묶어 처리하는
off-by-default bulk lane(`couchdb-graph-bulk-semantic`)으로 분리한다. 이미 착륙한
bulk 코어를 동결·재사용하고, 그 위에 얇은 trigger wrapper + opt-in compose 서비스만 얹는다.

## Requirements Reference

- Phase 1 source: `requirements.md`
- Preview companion: `requirements.html`
- 핵심 요구: hot path episode-only / bulk semantic 후행 / bulk lane off-by-default /
  공유 `graph-project.lock` / Gemma-4 MaaS only / dry-run 무변경·무네트워크 / public-safe /
  기존 bulk 코어 동결 / 14건 materialization out-of-scope.
- 승인된 기본값: `LLM_BRAIN_BULK_SEMANTIC_EMBEDDINGS=true`, bulk trigger interval `900s`,
  enrichment-gap 수용.
- 승인된 approach: **얇은 trigger wrapper CLI**(무변경 dry-run + schema 봉투 + 테스트 seam).

## Grounding (코드로 검증된 사실)

구현자가 재유도하지 않도록, 적대검증으로 확인된 코드 사실을 고정한다.

- 기존 child `couchdb-graph-bulk-semantic`에는 **dry-run/plan 모드가 없다**. argparse 직후
  바로 `_acquire_runtime_lock` → 추출·기록 파이프라인. 유일한 early return은 lock 경합
  (`status=already_running`). → 무변경 dry-run은 wrapper에서만 가능
  (`bulk_semantic_cli.py:45-122,143-152`).
- `LLM_BRAIN_BULK_SEMANTIC_*` 5+1개 env는 **이미 배선됨**. `MAX_SESSIONS_PER_CALL`(기본5)·
  `MAX_SESSION_CHARS`(기본1600)·`ALLOW_EMPTY_SESSIONS`(기본false)는 CLI argparse
  default 계층(`bulk_semantic_cli.py:63-66,71-74,100-103`), `MAX_TOKENS`(4096)·
  `TIMEOUT_SECONDS`(600)·`EMBEDDINGS`(true)는 `bulk_semantic.py from_env`
  (`:109-116,177`). → 새 env 배선 코드 불필요, `.env.example` 문서화 + 서비스 env 전달만.
- 공유 lock: `bulk_semantic_cli`가 `couchdb_projection_cli`에서
  `_acquire_runtime_lock`를 import(`:25-29`) → `<runtime-dir>/graph-project.lock`을
  `flock LOCK_EX|LOCK_NB`로 연다(`couchdb_projection_cli.py:305,307`). bulk 서비스가
  graph-trigger와 **같은 runtime-dir**를 가리키면 동일 lock 파일에서 경합 → 직렬화.
- episode-only: `--extract-entities`·`--reextract-entities` 둘 다 없으면
  `extract_entities=None` → `EXTRACTION_LEVEL_EPISODIC`, per-episode LLM 호출 없이
  `EpisodicNode` 직접 save, recall(`search_context`)은 flag와 무관하게 유지
  (`couchdb_projection_cli.py:76,136-140`).
- hot-path flip은 env만으론 **불충분**: `graph_trigger_cli.py:155`가 `--extract-entities`를
  **무조건** append하고, child는 per-run flag를 env보다 우선한다. → 그 줄을 조건부로 바꿔야 함.
- bulk lane은 `EXTRACTION_LEVEL_ENTITY`로 기록/재개(`bulk_semantic_cli.py:212,338,503`),
  hot path는 `EXTRACTION_LEVEL_EPISODIC` → **projection-state plane 분리**. deterministic
  writer는 raw Cypher `MERGE`(idempotent). 공유 lock은 wall-clock write 경합 직렬화용.

## Architecture

```
                        ┌─────────────────────────────────────────────┐
  HOT PATH (always-on)  │ llm-brain-graph-trigger (profile core)       │
                        │   sleep-loop → couchdb-graph-trigger         │
                        │   (episode-only: --extract-entities 제거)    │
                        └───────────────┬─────────────────────────────┘
                                        │ acquire
                              <runtime-dir>/graph-project.lock  ◄── 공유 (직렬화)
                                        │ acquire
                        ┌───────────────┴─────────────────────────────┐
  SEMANTIC LANE         │ llm-brain-bulk-semantic-trigger              │
  (off-by-default,      │   profile: llm-brain-bulk-semantic           │
   opt-in profile)      │   sleep-loop → couchdb-bulk-semantic-trigger │  ← NEW wrapper
                        │       (dry-run | --execute)                  │
                        │       └─► couchdb-graph-bulk-semantic (FROZEN child)
                        │             DeterministicGraphitiSemanticWriter → Neo4j (MERGE)
                        └──────────────────────────────────────────────┘
```

- Wrapper `bulk_semantic_trigger_cli.py`는 `graph_trigger_cli.py`를 그대로 미러링한다.
  소유 책임: ① schema 봉투(`llm_brain_bulk_semantic_trigger.v1`), ② 무변경 dry-run
  early-return(child 미호출), ③ `ChildMain` 주입 seam(테스트), ④ runtime-dir 기반 JSONL
  경로 유도, ⑤ status 정규화(`already_running`/`ok`/`failed`).
- child(`bulk_semantic_cli`)와 코어(`bulk_semantic`)는 **동결**, verbatim 재사용.
- 모델: graph-trigger의 LLM/embedding env 재사용 — `LLM_BRAIN_LLM_MODEL=gemma-4-26b-a4b-it-maas`,
  `LLM_BRAIN_EMBEDDING_MODEL=gemini-embedding-2`. 새 chat 경로 없음, Gemini/Flash 없음.

## Data Flow

1. compose sleep-loop → `neuron-knowledge couchdb-bulk-semantic-trigger --execute
   --runtime-dir <shared> ...knobs`.
2. wrapper `main` → `run_bulk_semantic_trigger` → `_child_argv` + `plan` dict 구성.
3. `execute=False` → dry-run 봉투 반환(`mutation_performed=False`, `network_used=False`,
   `raw_paths_printed=False`), **child 미호출**.
4. `execute=True` → `_call_child`가 `bulk_semantic_cli.main` 주입 실행, `redirect_stdout`로
   JSON 1줄 캡처.
5. child가 `<runtime-dir>/graph-project.lock` 획득. 경합 시 `status=already_running` exit 0.
6. 정상 시 `_select_sessions(limit)` → `EXTRACTION_LEVEL_ENTITY` 재개 skip →
   `max_sessions_per_call` 묶음 → 1 LLM call/batch → `DeterministicGraphitiSemanticWriter`
   `MERGE` 기록(+옵션 embedding) → `mark_projected(EXTRACTION_LEVEL_ENTITY)`.
7. child가 schema JSON 1줄 출력(`ok/partial/failed/already_running`).
8. wrapper가 child_status를 trigger status로 정규화 → 봉투 출력.
9. loop가 `rc=` 로깅 후 `INTERVAL_SECONDS` sleep. progress/dead-letter는 runtime-dir JSONL append.

## Component Details

### `bulk_semantic_trigger_cli.py` (NEW, `graph_trigger_cli.py` 미러)
- 입력: CLI `--ledger`(필수), `--runtime-dir`(필수), `--limit`, `--project`, `--provider`,
  `--max-projects`, `--max-sessions-per-call`, `--max-session-chars`, `--report-every`,
  `--allow-empty-sessions`, `--execute`, `--couchdb-*`; keyword
  `run_bulk_semantic_trigger(..., bulk_main: ChildMain = bulk_semantic_cli.main)`.
- 출력: stdout 1줄 `{schema_version: llm_brain_bulk_semantic_trigger.v1, status:
  dry_run|ok|already_running|failed, execute, plan{mode,bounded,limit,
  child_command:"couchdb-graph-bulk-semantic", child_argv_count,
  runtime_lock:"graph-project.lock", raw_paths_printed:false}, [step],
  mutation_performed, network_used, raw_paths_printed:false}`. exit 0(dry_run/ok/
  already_running)·1(failed).
- 의존: `bulk_semantic_cli.main`(frozen), stdlib(argparse/contextlib/io/json), `ChildMain` alias.

### `_child_argv` (wrapper 내부)
- 입력: trigger params + 유도 경로 `progress_jsonl=runtime_dir/"bulk-semantic-progress.jsonl"`,
  `dead_letter_jsonl=runtime_dir/"bulk-semantic-dead-letter.jsonl"`.
- 출력: child argv. 항상 `--ledger/--limit/--runtime-dir/--progress-jsonl/
  --dead-letter-jsonl/--report-every/--max-projects/--max-sessions-per-call/
  --max-session-chars`; 조건부 `--project/--provider/--allow-empty-sessions/--couchdb-*`.
  **`--enable-graph`·`--extract-entities` 없음**(그건 graph child 소유).
- 의존: 없음(순수).

### `graph_trigger_cli.py` (EDIT)
- 목적: 스케줄 hot path를 episode-only로. `--extract-entities`를 조건부로
  (`extract_entities: bool=False` keyword + `--extract-entities` store_true) 바꾸고,
  `:155` 무조건 append를 그 플래그로 게이트(`:163-164` reextract 패턴 미러).
- 출력: 플래그 없으면 child argv에 `--extract-entities` 미포함.
- 의존: `couchdb_projection_cli.main`(불변).

### `cli.py` (EDIT)
- `:106` 근처 `COMMAND_HANDLERS`에 `"couchdb-bulk-semantic-trigger":
  ...trigger.main` 등록(+상단 import).

### `compose.yaml` (EDIT)
- off-by-default 서비스 `llm-brain-bulk-semantic-trigger` 추가 + hot-path env/command flip.

## Error Handling

- dry-run: child 미호출 → mutation/network False.
- execute: `_call_child`가 `SystemExit` → int code 흡수. `_parse_json`은 마지막 비공백
  stdout 줄 파싱, 실패 시 `{}`(child는 항상 JSON 1줄 → 견고).
- status 정규화: `already_running`(lock 경합, exit 0, **retry-storm 없음** — loop가 sleep);
  exit 0 → `ok`; 그 외 → `failed`. `mutation_performed=network_used=bool(status=="ok")`.
- child: lock은 `finally` 해제(`bulk_semantic_cli.py:316-318`). LLM JSON 파싱 실패는
  singleton fallback(`:393-421`), 그 외 LLM/Neo4j 오류는 batch를
  `bulk-semantic-dead-letter.jsonl`로. 내장 retry/backoff 없음 — sleep-loop가 외부 retry.
- **모니터링 주의**: `--max-projects`는 projected가 아니라 materialized를 센다. 한 tick에서
  모든 LLM 호출이 실패하면 `stopped_after_max_projects=True`인데 `projected=0`일 수 있다 →
  알람은 budget flag가 아니라 `projection.failed`를 봐야 한다.
- **public-safe 주의**: `by_provider` 키와 progress-jsonl의 provider 필드는 raw다. JSONL은
  runtime 볼륨 내부 보관, 외부 sink로 redaction 없이 보내지 않는다. wrapper stdout은
  public-safe(`raw_paths_printed=false`, project_ref sanitized).

## Testing Strategy

- 신규 `worker/tests/test_bulk_semantic_trigger_cli.py`: dry_run / execute_ok /
  already_running / failed + public-safe(경로·project redaction). 기존
  `test_graph_trigger_cli.py`의 no-mock inline child-stub + `_exploding_child` 패턴 재사용.
- 기존 `test_graph_trigger_cli.py:54` **EDIT**(추가가 아니라 단언 반전): 기본 argv에
  `--extract-entities` **부재**, 새 플래그 줄 때만 존재.
- 회귀: `test_couchdb_graph_bulk_semantic_cli.py`는 green 유지(child 동결 증명).
- 게이트: `cd worker && uv run pytest -q`; `JAVA_HOME=$(/usr/libexec/java_home -v 25)
  gradle test`(JVM 무변경); `uv run neuron-knowledge --show-boundary`에 신규 명령 노출;
  `docker compose -f compose.yaml config`(no profile)에 bulk 서비스 **미포함**,
  `--profile llm-brain-bulk-semantic config`엔 포함(off-by-default 증명).

### 테스트 케이스
- **dry_run / 무변경 + public-safe**: `run_bulk_semantic_trigger(execute=False,
  bulk_main=_exploding_child, limit=17, runtime_dir=tmp_path/"runtime")` →
  `status=="dry_run"`, mutation/network False, `plan["bounded"]`, `plan["limit"]==17`,
  `plan["child_command"]=="couchdb-graph-bulk-semantic"`,
  `plan["runtime_lock"]=="graph-project.lock"`, `raw_paths_printed False`,
  `str(tmp_path) not in json.dumps(report)`. (child 미호출 증명: `_exploding_child` raise)
- **execute_ok / argv + project redaction**: inline `_child`가 argv append, `{status:"ok"}`
  출력, 0 반환; `execute=True, project="neurons", limit=3, max_projects=5` →
  `status=="ok"`, mutation/network True, `len(calls)==1`, argv에 `--runtime-dir/
  --max-projects/--max-sessions-per-call/--max-session-chars`, `["--limit","3"]`,
  `--enable-graph`·`--extract-entities` **부재**, `"neurons" not in json.dumps(report)`.
- **already_running / lock 공존**: child `{status:"already_running"}` exit 0 →
  `status=="already_running"`, mutation/network False.
- **failed / soft-fail 전파**: child `{status:"failed"}` exit 1 → `status=="failed"`,
  `step["exit_code"]==1`.
- **hot-path flip(episode-only 기본)**: `test_graph_trigger_cli.py:54` EDIT — 플래그 없으면
  `--extract-entities` 부재(`--enable-graph`은 유지), `extract_entities=True`면 존재.
  "env default가 on으로 회귀 안 함" 가드.
- **회귀(frozen child green)**: 기존 `test_couchdb_graph_bulk_semantic_cli.py` green.

## TDD Strategy

red → green → refactor.
- **RED**: `test_bulk_semantic_trigger_cli.py` 4+1 케이스 작성 + `test_graph_trigger_cli.py:54`
  단언 반전. `uv run pytest -q` → 모듈/플래그 부재로 실패.
- **GREEN**: `bulk_semantic_trigger_cli.py` 생성(`graph_trigger_cli.py` 미러: schema
  `llm_brain_bulk_semantic_trigger.v1`, `child_command="couchdb-graph-bulk-semantic"`,
  `_call_child` name `"bulk_semantic"`, `_child_argv` bulk 플래그 매핑, `--enable-graph/
  --extract-entities` 없음); `graph_trigger`의 `--extract-entities` 조건부화; `cli.py` 등록.
  재실행 → green.
- **REFACTOR**: 중복이 거슬리면 `_call_child/_parse_json/ChildMain` 공통화(선택). 더 읽기
  좋으면 copy 유지 — YAGNI.

## Milestones

- **M1 — Hot-path episode-only flip**: `graph_trigger_cli.py` `--extract-entities` 조건부화 +
  `test_graph_trigger_cli.py:54` 반전. done = 기본 argv에 `--extract-entities` 부재 테스트
  green, `--reextract-entities`/신규 플래그 opt-in 경로 유지.
- **M2 — Bulk trigger wrapper + 등록**: `bulk_semantic_trigger_cli.py` +
  `test_bulk_semantic_trigger_cli.py`(4+1) + `cli.py` 등록. done = 4 케이스 green,
  `--show-boundary`에 `couchdb-bulk-semantic-trigger` 노출, frozen child 회귀 green.
- **M3 — compose 서비스 + env(.env.example) + runbook**: off-by-default 서비스 추가, 공유
  runtime-dir, `LLM_BRAIN_GRAPH_EXTRACT_ENTITIES` 기본 false flip(`:247,:301` + `.env.example:33`),
  6 `BULK_SEMANTIC_*` + 6 `BULK_SEMANTIC_TRIGGER_*` 문서화, runbook 섹션. done = `config`
  profile 게이팅 증명, gradle `ComposeConfigTest` 영향 확인.
- **M4 — Verification + 라이브 smoke**: worker targeted → full worker → gradle. 이후 Ubuntu
  redeploy smoke(graph status, bulk dry-run, bounded `--max-projects 1~2` execute). done =
  로컬 게이트 green + 라이브 mutation은 exact argv/postcheck/rollback 기록.

## Open Questions

- **canary preview 충분성**: wrapper dry-run은 무변경이지만 child에 plan 모드가 없어 실제
  selected/skipped 카운트는 못 낸다. plan 수준 미리보기(limit/max-projects/child_argv_count)로
  live-enable 게이트가 충분한가, 아니면 보조로 `--max-projects 1` 소규모 실측 canary도 둘까.
- **runbook 편집 범위**: `LLM_BRAIN_CORE_V1_LOCAL_OPS.md`에 Bulk Semantic Trigger Scheduler
  섹션(knobs/dry-run 사전점검/live-enable 게이트/rollback=해당 서비스만 stop) 추가가 이번
  변경에 포함인지 확인(현재 M3에 포함 가정).
- **모니터링 구분**: child soft-fail(`exit 0 + status==failed`)을 wrapper hard-fail(`exit 1`)과
  알람상 구분할지.
- **`LLM_BRAIN_BULK_SEMANTIC_TRIGGER_ENABLE`**: 실제 off-switch는 compose profile(구조적)이라
  이 env는 advisory/문서용. `.env.example`·runbook에 "기동은 profile gate"로 명시.
