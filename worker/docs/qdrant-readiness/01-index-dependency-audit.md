# 01. RetiredIndexBridge read/write 의존성 audit

RetiredIndexBridge를 searchable mirror에서 떼어내기 전에, 어떤 코드가 RetiredIndexBridge에 read/write로
묶여 있는지와 RetiredIndexBridge를 끄면 각 경로가 어떻게 동작하는지를 전수 정리한다. 모든
client 호출은 `agent_knowledge/index_client.py`의 `RetiredIndexBridgeHttpClient`를 거친다.

핵심 분류 축: **fallback 보유 여부**. recall 표면의 일부는 ledger/CouchDB가
authority라 RetiredIndexBridge가 없어도 degrade만 하지만, 일부 경로는 RetiredIndexBridge가 사실상 유일
source라 RetiredIndexBridge를 끄면 빈 결과/정지가 된다. 이 "no-fallback" 경로들이 RetiredIndexBridge
disable의 진짜 blocker다.

## A. Read plane

### A-1. RetiredIndexBridge client read surface (`index_client.py`)

| symbol | endpoint 역할 | 용도 |
| --- | --- | --- |
| `retrieve` | vector similarity search | knowledge.search, brain.query mirror lane, supersede 후보 recall, transcript retrieval source |
| `list_documents` | document 열거(keyword) | dedup, status reconcile, GC 후보 열거, backfill seed |
| `list_document_chunks` | chunk body fetch | transcript/session-memory body 재구성, GC backup 전 body 확보 |
| `get_document_status` | parse 상태 polling | ingest/parse 완료 대기, ledger 상태 전이 gate |
| `get_document_meta` | 단건 meta_fields | meta 누락 시 metadata envelope 재구성 |
| `list_datasets` | dataset 열거(이름→id) | logical name → physical dataset 라우팅(데이터 authority 아님) |
| `search_messages` | RetiredIndexBridge Memory semantic search | native-memory recall, superseded message reconcile |

### A-2. recall/read 소비 표면

| 파일 / symbol | 역할 | RetiredIndexBridge off 시 동작 | fallback |
| --- | --- | --- | --- |
| `llm_brain_core/document_bridge.py` · `RetiredIndexBridgeDocumentBridge.search_documents` | brain.query document-bridge lane | status=`unavailable` 반환 | ✅ lane만 비고 ledger 결과 유효 |
| `mcp_server.py` · `KnowledgeSearchService.search` | knowledge.search MCP tool | retrieve→`[]`, 빈 결과 | ✅ ledger가 authorize, 빈 ranking |
| `session_memory/brain_query.py` · `run_brain_query_v2` | archive/evidence_candidates lane | mirror lane 비고 ledger current/accepted lane이 승리 | ✅ ledger-only 응답이 authoritative |
| `session_memory/native_memory_recall.py` · `recall_active_native_memory` | native-memory semantic recall | 빈 list | ✅ ledger-recent 카드로 degrade |
| `session_memory/memory_regeneration.py` · `RetiredIndexBridgeRetrievalTranscriptMemorySource` | RetiredIndexBridge 후보 + ledger authority hybrid | 후보 set 비어 결과 0 | ✅ `LedgerTranscriptMemorySource` |
| `session_memory/supersede_detector.py` · `detect` | supersede vector 후보 recall + LLM judge | `None` 반환(=supersede 없음) | ⚠️ **fail-closed로 자동 supersede 정지**, ledger fallback 없음 |
| `session_memory/memory_regeneration.py` · `RetiredIndexBridgeTranscriptMemorySource` (`index_read_sot` mode) | Mac-ledger-free session-memory build source | source chunk 0 → 문서 미생성 | ❌ **no-fallback** |
| `session_memory/autopilot_cli.py` · `mine_live_candidates` | autopilot live mining source(session/transcript memory) | 후보 0 → 사이클이 아무것도 채굴 못함 | ❌ **no-fallback** |
| `session_memory/transcript_session_gc.py` · `_summarized_sessions`/`_scan_candidates` | GC 적격성·후보 열거 | summarized set 0 → GC 정지(삭제는 안 함=safe) | ❌ **no-fallback (정지)** |
| `session_memory/transcript_volume_gc.py` · `_resolve_transcript_doc_id`/`run` | volume GC 대상 doc id/body | doc id 빈값 → 후보 skip | ❌ **no-fallback (정지)** |
| `session_memory/transcript_backfill.py` · `TranscriptBackfillRunner.run` | un-summarized 세션 seed | seed 0 → backfill 정지 | ❌ **no-fallback (정지)** |
| `session_memory/cleanup_readiness.py` · `_dataset_report` | dataset 청결도 진단 | count 0 보고 | ❌ 진단 부정확 |
| `session_memory/memory_regeneration.py` · `mark_index_done_*` | queued 문서 status reconcile | queued가 영구히 안 풀림 | ❌ ledger 상태 stuck |
| `session_memory/native_memory_reconcile.py` · `reconcile_one` | superseded message 식별 | rows_search_failed 누적 | ❌ superseded item이 enable로 잔존 |
| `session_memory/index_projection.py` · `_find_existing_projection_document` | upload 전 idempotency 확인 | 빈값 → 중복 upload 가능 | ⚠️ 중복 위험 |
| `couchdb_source/index_fallback.py` · `RetiredIndexBridgeReader` | CouchDB 원본 소실 세션 복구 | 복구 불가 | (의도된 recovery 전용) |
| `rag_ingress/retired_index_bridge.py` · `RetiredIndexBridgeRetiredIndexBridgeAdapter.find_by_natural_key`/`document_status*` | ingest dedup·parse 상태 | 예외 → 재시도(중복 위험) | ⚠️ |
| `src/.../retired_index_bridge/HttpRetiredIndexBridgeGateway.java` · `findByContentHash`/`listDocumentsByKeyword` | Java ingest dedup | 예외 → delivery 재시도 | ⚠️ |

## B. Write plane

RetiredIndexBridge write는 5종뿐이다: `upload_document` + `update_metadata` + `request_parse`
(항상 순차), `disable_document`(soft), `delete_documents`(hard). **update-in-place는
없다** — 본문 변경은 항상 새 문서 upload다. hard delete는 전부
`session_memory/gc_safety_auditor.py`의 `hard_delete_documents` 단일 chokepoint를
지난다.

| 파일 / symbol | write 종류 | live route 여부 |
| --- | --- | --- |
| `rag_ingress/retired_index_bridge.py` · `RetiredIndexBridgeRetiredIndexBridgeAdapter.submit_document` | upload+meta+parse | ✅ `rag-ingress-worker`(shadow_worker, `SHADOW_DELIVER=1`, Ubuntu 상주 NATS consumer) |
| `rag_ingress/delivery_backend.py` · `RetiredIndexBridgeDeliveryBackend.submit` | adapter 위임 | operator-gated(`drain-deliveries`); CLI gate가 RetiredIndexBridge live 차단 |
| `rag_ingress/delivery_drain.py` · `drain_pending_deliveries` | delivery 실행 | CLI-only, RetiredIndexBridge live path는 `state_cli`에서 차단 |
| `session_memory/memory_regeneration.py` · `SessionMemoryRegenerationRunner.run` (sync) | upload+meta+parse+poll | ✅ `DirtySessionMemorySyncRunner` 경유(상시 live session-memory upload) |
| `session_memory/dirty_session_memory_sync.py` · `process_one_once` | 위 runner sync=True 구성 | ✅ neuron-knowledge CLI + mcp_server |
| `session_memory/index_projection.py` · `upsert_memory_card` | upload+meta+parse | ✅ autopilot `--allow-write`(standing pre-approval) |
| `session_memory/sync_roundtrip.py` · `rollback_session_memory_document*` | `disable_document` | ✅ sync 실패 시 rollback |
| `session_memory/gc_safety_auditor.py` · `hard_delete_documents` | `delete_documents` | 모든 GC delete의 단일 chokepoint(`--execute`+승인 파일) |
| `session_memory/session_memory_gc.py` / `transcript_volume_gc.py` / `transcript_session_gc.py` | hard delete | operator-gated CLI + container `deploy/session-memory/gc-run.py` |
| `session_memory/native_memory_reconcile.py` · `NativeMemoryReconciler` | `disable_message`(Memory API) | CLI `native-memory-reconcile` |
| `couchdb_source/index_projector.py` · `project` | upload+meta+parse | neuron-knowledge couchdb build |
| `rag_ingress/file_ingest.py` · `submit_file` | upload+parse | 미연결(라이브러리 함수) |
| `rag_ingress/shadow_worker.py` · `main` | backend 선택 gate | `INGRESS_DELIVERY_BACKEND`=retired_index_bridge/couchdb 분기 |

## C. RetiredIndexBridge disable의 진짜 blocker (요약)

RetiredIndexBridge를 끄려면 아래 **no-fallback 소비자**가 먼저 다른 source(CouchDB / Qdrant /
ledger)로 이전되거나 은퇴해야 한다:

1. **autopilot live mining** (`mine_live_candidates`) — session/transcript-memory를
   RetiredIndexBridge에서만 읽는다.
2. **`index_read_sot` session-memory build** — RetiredIndexBridgeTranscriptMemorySource.
3. **GC runners 3종** (session/transcript-session/transcript-volume) — 후보 열거를
   RetiredIndexBridge에 의존(끄면 삭제는 안전하게 멈추지만 GC 진행이 전면 정지).
4. **backfill seed** (`TranscriptBackfillRunner`).
5. **supersede vector stage** — 끄면 자동 supersede가 조용히 비활성.
6. **brain.query archive/evidence lane** — historical 비-ledger 카드가 invisible.
7. **status reconcilers** (`mark_index_done_*`) — queued 영구 stuck.
8. **native-memory reconcile** — superseded item이 enable로 잔존.

CouchDB index/delivery backend가 이미 ingest 대체 sink로 존재하므로(B 표),
write plane은 sink 교체로 끊을 수 있다. 그러나 위 read 소비자들은 Qdrant mirror
read나 CouchDB read로의 **개별 repoint가 필요**하며, 그 전까지 RetiredIndexBridge read는 끌 수
없다. 단계별 절차는 [`04-cutover-gates-and-rollback.md`](04-cutover-gates-and-rollback.md)
참조.
