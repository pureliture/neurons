# rag-ingress-queue Python delivery worker (co-located)

이 디렉터리는 rag-ingress-queue가 소유하는 **Python live delivery worker**다.
G2부터 라이브 delivery는 Java `ingress-worker`가 아니라 이 Python worker가 수행하며
(은퇴한 Java worker는 `compose.yaml`에서 `profiles: [retired]`), 그 소스가 그동안
`workspace-ragflow-advisor`의 `agent-knowledge` lib에만 물리적으로 존재했다.
이 패키지는 그 **delivery 서브셋을 co-locate(벤더링)** 한 것이다. Java 코드는 바뀌지 않는다.

- 출처(provenance): `agent-knowledge` advisor source revision `d571800`의 벤더링 사본.
- 외부 의존: `nats-py>=2.6` 하나(런타임). 그 외 표준 라이브러리만 사용.

## 무엇을 가져왔고(vendored) 무엇을 뺐나

라이브 worker가 실제로 실행하는 경로(`shadow_worker.run_consume` →
`process_payload` → normalize → server full redaction → leak check → RAGFlow
submit)와 그 의존 폐포만 가져왔다.

vendored (`lib/agent_knowledge/`):
- `ledger.py` — server/brain-side SQLite state authority for knowledge item
  lifecycle, transcript/session-memory state, memory cards, RAGFlow projection
  audit, scheduler/GC evidence, and legacy-retirement gates. This is not a
  dendrite/client dependency.
- `rag_ingress/shadow_worker.py` — NATS JetStream pull-consume worker 엔트리포인트
- `rag_ingress/server_runtime.py` — `apply_server_redaction` / `normalize_ingest_job_payload` / `document_from_ingress_payload` / `public_ingress_leak_violations`
- `rag_ingress/index_backend.py` — `RAGFlowIndexBackendAdapter` (upload/metadata/parse + `find_by_natural_key`)
- `rag_ingress/rag_ready_document.py` — backend-neutral 문서 모델 + content_hash/idempotency_key 빌더
- `rag_ingress/idempotency.py` / `state_db.py` / `domain_state.py` / `ingress_journal.py`
  — server-owned durable ingress state primitives and byte-faithful replay
  journal. These are now owned here even when the live worker still uses the
  smaller `shadow_ingest_log` path.
- `rag_ingress/delivery_executor.py` / `delivery_backend.py` /
  `delivery_reconcile.py` / `delivery_drain.py` / `backfill.py` /
  `backfill_apply.py` / `state_sink.py` — approval-gated server
  delivery/backfill primitives plus the state-DB-only ingress accept seam
  covered by fake-backend tests. These are not wired into the live worker
  defaults by this slice.
- `rag_ingress/product_surface_switch_plan.py` /
  `state_shadow_readiness.py` / `retirement_readiness.py` — read-only
  server-side readiness and legacy-retirement planning gates. These produce
  dry-run/approval packets only; they do not mutate runtime, RAGFlow, GC, or
  live product config.
- `rag_ingress/replay_delivery.py` — server-owned replay-requested row
  selection, convergence-faithful payload reconstruction, byte-faithful journal
  replay, candidate-set digest gating, and redacted reporting. It uses an
  injected ingress client plus a local replay payload validator, and deliberately
  does not import client `outbox_client` or monolith CLI wiring.
- `session_memory/memory_card.py` / `session_memory/transcript_model.py` plus
  top-level compatibility aliases — server/brain-side MemoryCard candidate,
  envelope validation, redaction, and text-bound helpers used by `ledger.py`.
- `session_memory/curation.py` plus top-level compatibility alias — core
  ledger-backed MemoryCard candidate approval/reject/disable/supersede
  transitions. CLI/MCP search product surfaces remain out of this slice.
- `session_memory/memory_miner.py` plus top-level compatibility alias —
  injected-completion and source-span MemoryCard candidate mining. It performs
  no ledger write, queue write, RAGFlow dataset write, or raw transcript lookup.
- `session_memory/brain_query.py` / `query_planner.py` /
  `native_memory_governance.py` — pure brain query, resolve, query planning,
  and mirror-governance logic.
- `session_memory/brain_read_model.py` / `native_memory_recall.py` /
  `native_memory_mirror.py` — server-side ledger read-model adapter plus
  native-memory active-set filtering and local mirror store. RAGFlow access is
  injected and recall-only in this slice; writer/reconcile/regeneration upload
  or disable runners remain out.
- `session_memory/native_memory_writer.py` / `native_memory_reconcile.py` /
  `native_memory_write_runner.py` — server-side native-memory mirror write,
  supersede-sync, and injected RAGFlow message disable reconciliation logic.
  These modules are vendored with fake-client unit coverage only; the live
  `native-memory-sync` CLI/approval wiring remains out until the CLI surface is
  split.
- `document_envelope.py`, `session_memory/transcript_packer.py`,
  `session_memory/transcript_parsers.py`, and
  `session_memory/tool_evidence_sync.py` — server-side tool-evidence extraction,
  packing, and queue-sync core split from the historical mixed
  `transcript_ingest` module. This slice uses local ledger plus injected ingress
  sink tests only; monolith CLI exposure and direct RAGFlow writes remain out.
- `session_memory/transcript_chunking.py` and
  `session_memory/transcript_ingest.py` — server-side transcript chunk build
  plus injected enqueue/state-sink core. This is not the old mixed monolith
  worker: it has no `IngressQueueClient`, client `outbox_client`, monolith CLI,
  or direct RAGFlow upload/parse/status path.
- `session_memory/memory_promotion.py` / `memory_evaluation.py` /
  `ragflow_projection.py` / `llm_brain_service.py` — LLM-brain MemoryCard
  promotion, auto-policy evaluation gates, projection job building/execution,
  and canonical ledger integration. Projection write requires explicit
  `allow_write` plus an approval record and is covered here only with fake
  clients; live RAGFlow projection remains deployment/runtime gated.
- `session_memory/terminal_skipped_quarantine.py` /
  `session_memory/zombie_snapshot_repair.py` — local-ledger-only safety repair
  tools. They do not call RAGFlow, network, delete, disable, or live GC APIs;
  heavier GC/delete/disable modules remain out of this slice.
- `session_memory/gc_backup.py` — recoverable-delete backup record store only:
  private-directory JSON write/read/list and raw RAGFlow document-id hashing.
  Restore/upload/parse CLI behavior remains out of this slice.
- `session_memory/memory_regeneration.py` — server-owned session/project-memory
  regeneration core. Current worker tests cover dry-run document packing,
  ledger-backed transcript source planning, and injected project-memory enqueue
  sinks. Monolith CLI compatibility and live direct RAGFlow sync remain out of
  this slice.
- `redaction.py` — server full public redaction 본체(inline 정규식, denylist 파일 의존 없음)
- `events.py`, `spool.py`, `ragflow_client.py` — 위 모듈의 폐포 의존

**의도적으로 제외(가져오지 않음):**
- `outbox_client.py` — client(producer) 측 코드. server worker 불필요.
- `state_store.py` (`LedgerIngestStateStore`) — Ledger 직접 의존. 가져오지 않는다.
- `rag-ingress-state replay-deliver` monolith CLI wiring — public CLI
  compatibility surface. The replay core is vendored here; CLI/approval command
  exposure remains a separate compatibility/shim step.
- `transcript_ingest.py` monolith module — still mixed. The server-owned
  transcript worker core is present here, but the old HTTP client/outbox,
  direct RAGFlow indexing, and public CLI compatibility wiring remain out.
- `native-memory-sync` CLI wiring and GC restore/live GC runners — still include
  monolith CLI/live approval, RAGFlow upload/disable, or private transcript-source
  surfaces. They require a separate safety-lane split before vendoring. The
  memory-regeneration server core is present, but its CLI/live direct-sync
  wiring remains excluded.
  → `rag_ingress/__init__.py`는 client/Ledger/import-heavy 모듈을 eager import하지
  않도록 유지한다(패키지 import만으로 Ledger가 끌려오지 않게).

## redelivery dedup (under-dedup 갭 수정)

라이브 worker는 lease가 없어 JetStream 재배달 시 동일 메시지를 재처리할 수 있고,
기존 코드의 `submit_document`는 무조건 upload→metadata→parse 했다(중복문서 위험,
은퇴한 Java worker보다 약함). 이를 닫기 위해 은퇴한 Java worker의 2계층 dedup을 복원했다:

1. **로컬 계층** — worker 자신의 durable `shadow_ingest_log`(idempotency_key→delivered+document_ref)를
   submit 전에 조회. 이미 delivered면 재업로드하지 않고 기존 ref 재사용. 재시작 안전.
2. **RAGFlow 폴백** — 로컬 row가 없으면 `find_by_natural_key`로 RAGFlow에서 동일
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
  `RAG_INGRESS_DELIVER=1`, 7개 `RAGFLOW_*_DATASET_ID`(profile별 라우팅),
  `RAGFLOW_BASE_URL`/`RAGFLOW_API_KEY`, `RAG_INGRESS_PRESSURE_URL`.
- 라이브 재배포 자체는 이 작업 범위 밖(별도 goal).

## 테스트

```
cd worker && python -m pytest          # or: uv run --with pytest python -m pytest
```
