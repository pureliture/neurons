# rag-ingress-queue Python delivery worker (co-located)

이 디렉터리는 rag-ingress-queue가 소유하는 **Python live delivery worker**다.
G2부터 라이브 delivery는 Java `ingress-worker`가 아니라 이 Python worker가 수행하며
(은퇴한 Java worker는 `compose.yaml`에서 `profiles: [retired]`), 그 소스가 그동안
`workspace-index-advisor`의 `agent-knowledge` lib에만 물리적으로 존재했다.
이 패키지는 그 **delivery 서브셋을 co-locate(벤더링)** 한 것이다. Java 코드는 바뀌지 않는다.

`SHADOW_DELIVER=1`과 `INGRESS_DELIVERY_BACKEND=couchdb`를 함께 설정하면
`shadow_worker`는 이 패키지의 `DeliveryExecutor`를 통해 private SQLite
command/lease state를 사용하고 CouchDB source store로 전달한다. 이 경로에는
`COUCHDB_URL`, `COUCHDB_USER`, `COUCHDB_PASSWORD`, `COUCHDB_DB`, 그리고
0700 private parent 아래의 `INGEST_STATE_DB_PATH`가 필요하다. 기본값은 여전히
`SHADOW_DELIVER=0`이며, `couchdb`가 유일한 delivery sink다. 은퇴한 external
index bridge는 runtime에서 사라졌으므로 다른 backend option은 없다(퇴역 기록:
`docs/retired/ragflow-retirement.md`). 이 문서는 source/CI 계약만 설명하며
production activation 증거는 아니다.

- 출처(provenance): `agent-knowledge` advisor source revision `d571800`의 벤더링 사본.
- 외부 의존: `nats-py>=2.6` 하나(런타임). 그 외 표준 라이브러리만 사용.

## PostgreSQL session-memory의 큰 입력과 격리

PG projector는 UTF-8 본문이 8KiB를 넘으면 원문을 보존한 채 청크로 나눠
임베딩한다. 청크 벡터의 바이트 수 가중 평균을 정규화해 기존 세션당 벡터 1개
계약을 유지한다. 새 집계 방식은 별도 profile과 `embedding_revision=2`를 사용한다.
기존에 게시된 벡터를 일괄 재작성하지 않는다.

`PG_EMBEDDING_CHUNK_CACHE`(기본 `/app/state/pg-embedding-chunks.sqlite3`)는 영속 볼륨에
둔다. 원문 없이 digest, 벡터, 완료 위치와 집계값만 저장한다. 캐시에서 청크가
퇴출돼도 집계 checkpoint로 재개한다. 청크 영수증과 집계 checkpoint는 각각 최대
16384개다. 세션별 기본 64개, 사이클별 기본 192개 신규 청크를 호출하며,
`PG_EMBEDDING_CHUNKS_PER_SESSION`, `PG_EMBEDDING_CHUNKS_PER_CYCLE`로 조절한다.
예산 소진은 `pending/embedding_chunks_pending`으로 남기고 다음 사이클에 재개한다.
외부 호출 실패는 실패 청크부터 재시도하며, 모든 청크가 끝나기 전에는 게시하지 않는다.

본문과 PG 식별 필드의 NUL은 외부 호출과 PG 쓰기 전에 `invalid_text_nul`로 거절한다.
원문과 해시는 수정하지 않는다. `failure_source_hash`가 같은 revision은 selector가
재선택하지 않으며, 새 source revision이 들어오면 다시 평가한다.
정책 버전이 없는 기존 선택 checkpoint는 pending 목록과 watermark를 유지한 채
과거 PG DataError만 한 번 재선택해 새 검증으로 평가한다.

## Graphiti 임베딩 중복 방지와 키워드 검색

Graphiti embedder는 기본적으로 프로세스 내 LRU 캐시(최대 256개)를 사용한다.
동일 batch 안의 중복 텍스트는 한 번만 임베딩하고, 동시 요청의 동일 텍스트도
진행 중인 결과를 공유한다. 캐시 키는 provider/model/endpoint/차원/credential
digest와 입력 digest를 조합하며, 입력 원문과 API key는 캐시에 저장하지 않는다.
실패한 호출은 캐시하지 않는다. `LLM_BRAIN_EMBEDDING_CACHE_ENABLED=false`로
캐시와 single-flight를 끄면 기존 embedder 호출 방식으로 돌아간다.

PostgreSQL embedding outbox는 lease 소유권과 현재 target의 `content_hash`를
트랜잭션 안에서 확인한다. 이미 오래된 작업은 임베딩 API를 부르기 전에
`cas_skipped`로 종료하고, 임베딩 이후의 기존 CAS 검사는 처리 중 발생한 변경을
막기 위해 계속 유지한다.

Graphiti의 기존 `search()`는 벡터 검색과 Full-text(BM25/Lucene)를 결합하는
하이브리드 경로다. 중복 방지는 이 검색 구성을 변경하거나 Lucene을 별도 설치하지
않으며, 키워드 검색과 벡터 검색은 계속 함께 사용한다.

## 무엇을 가져왔고(vendored) 무엇을 뺐나

라이브 worker가 실제로 실행하는 경로(`shadow_worker.run_consume` →
`process_payload` → normalize → server full redaction → leak check → backend
submit)와 그 의존 폐포만 가져왔다.

vendored (`lib/agent_knowledge/`):
- `ledger.py` — server/brain-side SQLite state authority for knowledge item
  lifecycle, transcript/session-memory state, memory cards, index projection
  audit, scheduler/GC evidence, and legacy-retirement gates. This is not a
  dendrite/client dependency.
- `rag_ingress/shadow_worker.py` — NATS JetStream pull-consume worker 엔트리포인트
- `rag_ingress/server_runtime.py` — `apply_server_redaction` / `normalize_ingest_job_payload` / `document_from_ingress_payload` / `public_ingress_leak_violations`
- `rag_ingress/index_adapter.py` — backend-neutral `IndexAdapter` 계약 (`IndexStatus`, natural-key 규칙). 현재 구현은 `couchdb_index_adapter.py`다.
- `rag_ingress/rag_ready_document.py` — backend-neutral 문서 모델 + content_hash/idempotency_key 빌더
- `rag_ingress/idempotency.py` / `state_db.py` / `domain_state.py` / `ingress_journal.py`
  — server-owned durable ingress state primitives and byte-faithful replay
  journal. These are now owned here even when the live worker still uses the
  smaller `shadow_ingest_log` path.
- `rag_ingress/delivery_executor.py` / `delivery_payload_gate.py` /
  `delivery_reconcile.py` / `delivery_drain.py` / `backfill.py` /
  `backfill_apply.py` / `state_sink.py` — server delivery/backfill primitives
  plus the state-DB ingress accept seam. `DeliveryExecutor` is wired by the
  explicit CouchDB branch of `shadow_worker`; delivery/backfill CLIs remain
  approval-gated and default-off.
- `rag_ingress/product_surface_switch_plan.py` /
  `state_shadow_readiness.py` / `retirement_readiness.py` — read-only
  server-side readiness and legacy-retirement planning gates. These produce
  dry-run/approval packets only; they do not mutate runtime, RetiredIndexBridge, GC, or
  live product config.
- `rag_ingress/replay_delivery.py` — server-owned replay-requested row
  selection, convergence-faithful payload reconstruction, byte-faithful journal
  replay, candidate-set digest gating, and redacted reporting. It uses an
  injected ingress client plus a local replay payload validator, and deliberately
  does not import client `outbox_client` or monolith CLI wiring.
- `rag_ingress/state_cli.py` — dry-run/fail-closed `rag-ingress-state`
  compatibility command for replay delivery planning, queue backfill planning,
  delivery drain dry-run, and delivery reconcile dry-run. Live approval-execute
  wiring and RetiredIndexBridge/ingress POST clients remain excluded.
- `session_memory/memory_card.py` / `session_memory/transcript_model.py` —
  server/brain-side MemoryCard candidate, envelope validation, redaction, and
  text-bound helpers used by `ledger.py`. Import via
  `agent_knowledge.session_memory.*` (the former top-level compatibility aliases
  were removed).
- `session_memory/curation.py` — core ledger-backed MemoryCard candidate
  approval/reject/disable/supersede transitions. CLI/MCP search product surfaces
  remain out of this slice.
- `session_memory/memory_miner.py` —
  injected-completion and source-span MemoryCard candidate mining. It performs
  no ledger write, queue write, RetiredIndexBridge dataset write, or raw transcript lookup.
- `session_memory/brain_query.py` / `query_planner.py` /
  `native_memory_governance.py` — pure brain query, resolve, query planning,
  and mirror-governance logic.
- `session_memory/brain_read_model.py` / `native_memory_recall.py` /
  `native_memory_mirror.py` — server-side ledger read-model adapter plus
  native-memory active-set filtering and local mirror store. RetiredIndexBridge access is
  injected and recall-only in this slice; writer/reconcile/regeneration upload
  or disable runners remain out.
- `session_memory/native_memory_writer.py` / `native_memory_reconcile.py` /
  `native_memory_write_runner.py` — server-side native-memory mirror write,
  supersede-sync, and injected RetiredIndexBridge message disable reconciliation logic.
  These modules are vendored with fake-client unit coverage only. The worker
  exposes a dry-run/fail-closed `native-memory-sync` console script; live
  RetiredIndexBridge sync, approval execution, and LaunchAgent wiring remain out.
- `session_memory/native_memory_sync_approval.py` — read-only approval-file
  validator for the future `native-memory-sync` live runner. It does not create
  clients, read secrets, or execute RetiredIndexBridge writes/disables.
- `document_envelope.py`, `session_memory/transcript_packer.py`,
  `session_memory/transcript_parsers.py`, and
  `session_memory/tool_evidence_sync.py` — server-side tool-evidence extraction,
  packing, and queue-sync core split from the historical mixed
  `transcript_ingest` module. This slice uses local ledger plus injected ingress
  sink tests only; monolith CLI exposure and direct RetiredIndexBridge writes remain out.
- `session_memory/transcript_chunking.py` and
  `session_memory/transcript_ingest.py` — server-side transcript chunk build
  plus injected enqueue/state-sink core. This is not the old mixed monolith
  worker: it has no `IngressQueueClient`, client `outbox_client`, monolith CLI,
  or direct RetiredIndexBridge upload/parse/status path.
- `session_memory/neuron_session_memory.py` — neuron-local session-memory build
  entrypoint. Dry-run mode reads only the worker shadow log; `--probe-meta`
  additionally performs RetiredIndexBridge metadata reads to count project/provider source
  coverage without ledger writes, RetiredIndexBridge writes, raw ids, or raw paths. Live
  build remains approval-gated and fail-closed.
- `session_memory/memory_promotion.py` / `memory_evaluation.py` /
  `index_projection.py` / `llm_brain_service.py` — LLM-brain MemoryCard
  promotion, auto-policy evaluation gates, projection job building/execution,
  and canonical ledger integration. Projection write requires explicit
  `allow_write` plus an approval record and is covered here only with fake
  clients; live RetiredIndexBridge projection remains deployment/runtime gated.
- `session_memory/terminal_skipped_quarantine.py` /
  `session_memory/zombie_snapshot_repair.py` — local-ledger-only safety repair
  tools. They do not call RetiredIndexBridge, network, delete, disable, or live GC APIs;
  heavier GC/delete/disable modules remain out of this slice.
- `session_memory/gc_backup.py` — recoverable-delete backup record store only:
  private-directory JSON write/read/list and raw RetiredIndexBridge document-id hashing.
  Restore/upload/parse CLI behavior remains out of this slice.
- `session_memory/session_memory_gc.py`, `transcript_memory_gc.py`, and
  `transcript_volume_gc.py` — dry-run-only GC safety planners. They select
  eligible candidates and report the same redaction/mutation flags, but live
  execute/disable/delete paths are fail-closed in this worker slice. Worker
  console scripts are exposed as `session-memory-gc`, `transcript-memory-gc`,
  and `transcript-volume-gc` for dry-run planning and blocked-live-execution
  evidence only.
- `session_memory/transcript_backfill.py` — server/brain-side backlog
  promotion seed helper. It scans RetiredIndexBridge read surfaces for transcript sessions
  without an active session-memory summary, then marks those sessions dirty in
  the neuron-local ledger. It performs no RetiredIndexBridge write/delete/disable and is
  exposed as `transcript-backfill` plus `neuron-knowledge transcript-backfill`.
- `session_memory/memory_regeneration.py` — server-owned session/project-memory
  regeneration core. Current worker tests cover dry-run document packing,
  ledger-backed transcript source planning, and injected project-memory enqueue
  sinks. Monolith CLI compatibility and live direct RetiredIndexBridge sync remain out of
  this slice.
- `session_memory/memory_regeneration_cli.py` — dry-run/fail-closed
  `memory-regeneration` compatibility command for project-memory planning,
  session-memory build dry-run, and dirty project-memory dry-run. Enqueue,
  sync, promotion, cleanup, disable, reset, RetiredIndexBridge read/write, and scheduler
  wiring remain excluded.
- `session_memory/backfill.py` and `session_memory/transcript_quality.py` —
  fixture-only backfill inventory, dry-run classification, quality audit, and
  execute-plan building. This is planning-only: Gate F execute, live RetiredIndexBridge
  upload/parse, and private broad-source scans remain excluded.
- `redaction.py` — server full public redaction 본체(inline 정규식, denylist 파일 의존 없음)
- `events.py`, `spool.py`, `index_client.py` — 위 모듈의 폐포 의존

**의도적으로 제외(가져오지 않음):**
- `outbox_client.py` — client(producer) 측 코드. server worker 불필요.
- `state_store.py` (`LedgerIngestStateStore`) — Ledger 직접 의존. 가져오지 않는다.
- `rag-ingress-state` live approval-execute wiring — the worker exposes a
  dry-run/fail-closed compatibility command, but live replay POST, state-DB
  apply, RetiredIndexBridge drain/reconcile, and LaunchAgent/server wiring remain excluded.
- `transcript_ingest.py` monolith module — still mixed. The server-owned
  transcript worker core is present here, but the old HTTP client/outbox,
  direct RetiredIndexBridge indexing, and public CLI compatibility wiring remain out.
- `native-memory-sync` CLI/LaunchAgent wiring and GC restore/live GC runners —
  the worker has only dry-run/fail-closed `native-memory-sync`; live
  approval-execute/LaunchAgent wiring still includes RetiredIndexBridge upload/disable or
  private transcript-source surfaces and remains excluded.
- Gate F backfill execute/live indexing — approval-gated live RetiredIndexBridge mutation
  remains excluded; only fixture planning is present.
- `memory-regeneration` live approval-execute wiring — the worker exposes only
  dry-run/read-only planning. Enqueue/sync/promote/cleanup/disable/reset and
  scheduler wiring remain out.
- GC restore/upload/parse and live GC execute/disable/delete — approval-gated
  live RetiredIndexBridge mutation remains excluded; only dry-run planners and backup store
  are present.
  → `rag_ingress/__init__.py`는 client/Ledger/import-heavy 모듈을 eager import하지
    않도록 유지한다(패키지 import만으로 Ledger가 끌려오지 않게).

## Phase 3 세션 메모리 운영 도구

- Shadow 평가는 `uv run python -m agent_knowledge.session_memory.shadow_eval --out <path>`
  로 실행한다. CouchDB의 완전 materialized source만 읽고 PostgreSQL에는 접속하지 않으며,
  CouchDB source를 읽기 전에 local embedding profile이 아니면 실패하고 M0/S4 recall을 측정한다.
  M0는 상위 100개 세션 행을 사용한다. S4는 최대 5라운드 동안 라운드마다 상위 100개
  segment를 모으고, 이미 나온 세션은 다음 라운드에서 제외해 최대 20개 세션을 찾는다.
  후보 세션의 점수는 전체 segment 중 최고 유사도로 계산하고, 페널티는 세션 전체 segment 수를 기준으로 한다.
  후보 풀 밖 정답은 미검색(rank `None`)이다.
  표본 이외의 source 본문이나 식별 정보는 보고서에 기록하지 않는다.
- Stale planner는 `uv run neuron-knowledge session-memory-stale-plan --retention-days 14 --limit-sessions 20000`
  로 실행한다. PG와 CouchDB를 읽기만 하고
  활성 local receipt 및 authority 판정에 실패한 행은 `unresolved`로 남긴다. legacy receipt와
  다른 local profile receipt가 가리키는 행은 `protected_other_receipt`로 보호한다.
- `stale_past_retention`은 `observed_at`이 아니라 current receipt 행의 최대 `created_at`을 기준으로 계산하며,
  대체 시각을 알 수 없으면 retention 대상으로 세지 않는다.
- 전체 PG 스캔은 본문과 벡터 없이 메타데이터만 읽지만, 현재 receipt 검증은 authority 확인을 위해
  해당 receipt 행의 본문을 별도로 읽는다.
- 이 도구는 dry-run 전용이며 execute/delete 옵션이 없고, 보고서에는 raw ID나 project 이름을 넣지 않는다.

## redelivery dedup (under-dedup 갭 수정)

라이브 worker는 lease가 없어 JetStream 재배달 시 동일 메시지를 재처리할 수 있고,
기존 코드의 `submit_document`는 무조건 upload→metadata→parse 했다(중복문서 위험,
은퇴한 Java worker보다 약함). 이를 닫기 위해 은퇴한 Java worker의 2계층 dedup을 복원했다:

1. **로컬 계층** — worker 자신의 durable `shadow_ingest_log`(idempotency_key→delivered+document_ref)를
   submit 전에 조회. 이미 delivered면 재업로드하지 않고 기존 ref 재사용. 재시작 안전.
2. **RetiredIndexBridge 폴백** — 로컬 row가 없으면 `find_by_natural_key`로 RetiredIndexBridge에서 동일
   content_hash/idempotency_key 문서를 찾아 재사용. 첫 시도가 업로드 후 기록 전에
   죽었거나 로컬 볼륨이 소실된 경우를 커버.

폴백이 실제로 매칭되도록 `submit_document`가 `content_hash`+`idempotency_key`를
업로드 메타데이터에 주입한다(Java worker의 contentHash 기록과 동등 parity).

## 실행

- 이미지 기본값은 안전(격리 shadow 스트림, delivery off). compose `ingress-worker-py`
  서비스도 라이브 consume/deliver를 **opt-in**으로 둬서 `compose up`(ubuntu-smoke)은
  안전하다. 라이브 배포는 env-file로 주입한다:
  `RAG_INGRESS_STREAM=RAG_INGRESS_QUEUE`, `RAG_INGRESS_SUBJECT=rag.ingress.>`,
  `RAG_INGRESS_DURABLE=rag_target_delivery_worker`, `RAG_INGRESS_ALLOW_LIVE_QUEUE=1`,
  `RAG_INGRESS_DELIVER=1`, 7개 `RETIRED_INDEX_BRIDGE_*_DATASET_ID`(profile별 라우팅),
  `RETIRED_INDEX_BRIDGE_BASE_URL`/`RETIRED_INDEX_BRIDGE_API_KEY`, `RAG_INGRESS_PRESSURE_URL`.
- 라이브 재배포 자체는 이 작업 범위 밖(별도 goal).

## 로컬 Model2Vec 임베딩 서비스 (`local-embedding`)

유료 Gemini 임베딩 API 호출 없이 로컬 Model2Vec(`minishlab/potion-multilingual-128M`, revision `73908c3438cf03b6a01bcb9611d62b23d0726f08`, `model2vec==0.10.0`, 256차원, 프로필 `lbrain-memory-model2vec-256-v1`, 모델 별칭 `local-model2vec-256-v1`)을 실행·검증하는 서비스와 클라이언트 어댑터를 포함한다. 정본 스펙은 `specs/model2vec-local-embedding/`(`requirements.md`, `design.md`, `milestones.md`)에 있다.

### 1. 의존성 설치 (`local-embedding` optional extra)

기본 worker 이미지는 모델 라이브러리나 가중치를 자동 포함하지 않는다. 로컬 임베딩 서비스 실행 환경에서는 `local-embedding` extra를 명시적으로 동기화한다:

```bash
cd worker && uv sync --group dev --extra mcp-http --extra local-embedding
```

### 2. 모델 파일 설치 및 SHA-256 무결성 검증

모델 가중치는 Git에 커밋하지 않는다. 서버 시작(`serve`)과 readiness(`/readyz`)는 네트워크 다운로드를 수행하지 않으므로, 시작 전에 명시적 `install` 명령으로 모델 파일을 준비하고 3개 파일(`model.safetensors`, `tokenizer.json`, `config.json`)의 SHA-256을 검증한다:

```bash
# (A) 로컬에 이미 받은 스냅샷 디렉터리에서 복사·검증 설치
cd worker && uv run neuron-local-embedding install \
  --model-dir /var/lib/neurons/models/potion-multilingual-128M \
  --source-dir /path/to/local/snapshot

# (B) 초기 1회 명시적 다운로드 후 SHA-256 검증 설치 (운영/설치 시점 전용)
cd worker && uv run neuron-local-embedding install \
  --model-dir /var/lib/neurons/models/potion-multilingual-128M \
  --download

# (C) 오프라인 무결성 및 로딩 self-check 검증 (외부 네트워크 호출 없음)
cd worker && uv run neuron-local-embedding verify \
  --model-dir /var/lib/neurons/models/potion-multilingual-128M
```

검증 대상 고정 SHA-256:
- `model.safetensors`: `14b5eb39cb4ce5666da8ad1f3dc6be4346e9b2d601c073302fa0a31bf7943397`
- `tokenizer.json`: `19f1909063da3cfe3bd83a782381f040dccea475f4816de11116444a73e1b6a1`
- `config.json`: `595e4cab2093732efd5dbe084fd5c1826b5eea693b73b4c1fd971672867d2e54`

### 3. 서비스 실행 및 오프라인 재시작

서비스는 기본적으로 loopback(`127.0.0.1:18120`)만 바인딩하며, 시작/재시작 시 `HF_HUB_OFFLINE=1` 및 `TRANSFORMERS_OFFLINE=1` 상태로 검증된 로컬 디렉터리에서만 모델을 로드한다:

```bash
export NEURON_LOCAL_EMBEDDING_MODEL_DIR=/var/lib/neurons/models/potion-multilingual-128M
cd worker && uv run neuron-local-embedding serve --host 127.0.0.1 --port 18120
# 또는: cd worker && uv run neuron-knowledge local-embedding serve --host 127.0.0.1 --port 18120
```

- `GET /livez`: 프로세스 생존 상태 확인 (`200 OK`)
- `GET /readyz`: 고정 모델 파일 SHA-256, `model2vec==0.10.0`, 256차원 준비 상태 확인 (준비 시 `200 OK`, 미준비/불일치 시 `503 model_unavailable` + `Retry-After: 2`)
- `POST /v1/embeddings`: `model="local-model2vec-256-v1"`, 단건 문자열 또는 문자열 배열(최대 16개, 항목당 최대 8KiB UTF-8, 배치 총합 최대 128KiB UTF-8, 요청 JSON 최대 160KiB), `encoding_format` 생략/`float`/`base64`(IEEE754 float32 little-endian) 지원. 동시 추론 1, 대기열 8, 대기 제한 10초 초과 시 `429 capacity_exceeded` 반환.
- 모든 Neurons 로컬 임베딩 소비자는 공유 클라이언트 어댑터(`canonical_embedding_v1`)를 통해 호출 전 전체 입력의 `input_hash`(`sha256:<64hex>`)를 계산하고, 8192 bytes UTF-8 문자 경계 보존 분할 및 바이트 길이 가중합 후 L2 정규화를 수행한다.

## 테스트

```
cd worker && uv run --group dev --extra mcp-http --extra local-embedding pytest -q
```

## Hermes 혼합 project scope 판정

Hermes는 한 세션 안에서 profile이 바뀔 수 있어 같은 세션의 CouchDB 문서가
`project` 값에 서로 다른 label(profile 이름 / workspace 이름)을 가질 수 있다.
판정은 `couchdb_source/hermes_project_scope.py`의
`decide_hermes_project_scope()` 하나로 하고, graph projection lane
(`llm_brain_core/runtime.py`)과 PostgreSQL pgvector session-memory build lane
(`couchdb_source/session_memory_materializer.py`)이 같은 결과를 따른다.

- workspace-vs-profile: session 문서의 workspace로 정규화하고 note
  `hermes_profile_normalized_to_workspace`를 남긴다. CouchDB 문서는 바꾸지 않는다.
- 그 외(profile-vs-profile, profile 세션에 workspace 혼입, 미등록 label):
  `inconsistent_hermes_project_scope`로 두 lane 모두 거부한다. graph lane은
  `skipped_inconsistent`로 집계하고 현재 source hash를 ledger projection state에
  `source_invalid`로 기록해 resume과 `couchdb-graph-status` backlog/coverage에서
  기존 `source_invalid` 규칙대로 제외한다(dead-letter 아님). PG lane은
  `projection_state`에 terminal 실패 사유로 기록하고 build 보고서에
  `skipped_inconsistent`를 더한다. 두 lane 모두 source revision이 바뀌면 다시
  판정한다.

profile 이름은 코드에 두지 않는다. `HERMES_PROFILE_NAMES`(쉼표 구분)로 주입하며
기본값은 빈 집합(fail-closed)이다. 운영 profile 목록은 private 배포 설정
(`neurons-ops`)에서 관리한다.
