# 결정 제안: 1단계 legacy 잔존 처리 / 2단계 evidence 이중 계층

작성 기준: main `3f429b8` (PR #331 `7fbe61f`, PR #334 `546eaaa` 반영 후)
라이브 실측: k3s control-plane `100.95.155.103`, `~/.kube/oci-k3s-poc-config`

---

# 1단계 — legacy RAGFlow 코드 잔존 처리

## 현재 실태

`worker/lib/agent_knowledge/legacy/index_client.py` (748줄)에는 RAGFlow wire protocol이 남아있다.
`legacy/__init__.py`의 계약은 "운영자 전용 archive/GC 도구 전용이며 활성 런타임 경로에서 도달 불가"다.

**이 계약은 문서만이 아니라 테스트로 강제된다.** `worker/tests/test_retired_index_bridge_protocol_guard.py`:
- `LEGACY_CLIENT_ALLOWED_MODULES` — 10개 모듈만 `legacy.index_client` import 허용
- `test_legacy_index_client_is_only_imported_by_operator_tooling` — 패키지 전체를 `rglob("*.py")`로 전수 스캔
- `test_ingress_runtime_does_not_import_the_legacy_transport` — `rag_ingress/**` 별도 스캔
- `test_active_source_carries_no_retired_bridge_wire_protocol` — `/api/v1/datasets`·`/api/v1/retrieval`·`/api/v1/memories`·`/api/v1/messages`·`/api/v1/chat/completions` 탐지
- `test_deploy_surface_has_no_retired_bridge_credentials` — compose.yaml·.env.example·worker/deploy/session-memory/compose.yaml 스캔

추가로 `test_autopilot_no_index_client_before_m9.py`, `test_extracted_seam_direction.py`도 관련 가드를 강제한다.

## 소비처 분류 (실측)

| 모듈 | 분류 | 근거 |
|---|---|---|
| `session_memory_gc.py` | **(a) 접속 필수** | `:108` 생성(execute 시), `:232 list_document_chunks`, `delete_documents` 경유 hard delete. body는 backup에 사용 |
| `transcript_volume_gc.py` | **(a) 접속 필수** | `:98` 생성(execute 시), `:136 list_document_chunks`, hard delete |
| `transcript_session_gc.py` | **(a) 접속 필수** | `:120` 항상 생성, `:196/:216 list_documents` |
| `transcript_backfill.py` | **(a) 접속 필수** | `:62` 항상 생성, `list_documents` → `mark_session_memory_dirty` |
| `gc_backup.py` | **(a) 접속 필수** | `:154` 생성, `upload_document`·`request_parse`, 반환 `document_id` 사용 |
| `cleanup_readiness.py` | **(a) 접속 필수 (read-only)** | `:257` 항상 생성, `list_datasets`·`list_documents` → `_dataset_report` |
| `neuron_session_memory.py` | **(a) live build/probe-meta 시** | `:19` private helper import, `:230/:358/:411` lazy client |
| `dirty_session_memory_sync.py` | **(a) `index_read_sot` 선택 시** | `:231` 생성. `transcript_read_source` 기본값은 `ledger` |
| `native_memory_writer.py` | **(a) 브리지 주입 시에만** | `envelope_code`/`envelope_failed`만 import(`:6`). `write()`가 `add_message` 호출, 성공 시에만 로컬 `native_memory_mirror` upsert |
| `native_memory_reconcile.py` | **(a) 브리지 주입 시에만** | `search_messages`·`disable_message` 호출 |
| `knowledge_search_service.py` | **(b) 접속 불필요** | legacy import 없음. `build_index_client`(`:900`)는 `DisabledRetiredIndexBridgeClient` 반환 |
| `mcp_server.py` | **(b) legacy import 없음** | disabled client re-export만 |
| `cli.py` | **(b) legacy import 없음** | `build_index_client`(`:109`)가 disabled client proxy |

**기본 런타임은 RAGFlow 클라이언트를 만들지 않는다.** `cli.py:291`·`mcp_server.py:3`이 `build_index_client()` → `DisabledRetiredIndexBridgeClient`를 쓴다.

## native_memory 제품 상태 (핵심)

`native_memory_write_runner.py:198-232` 실측:

```python
if args.execute or not args.dry_run:
    print(json.dumps({
        "status": "blocked_live_execution",
        "failed_error_class": "live_native_memory_sync_not_vendored",
        ...
    }))
    return 1
report = run_native_memory_sync(..., retired_index_bridge=None, dry_run=True)
```

- live 실행은 **`live_native_memory_sync_not_vendored`** 로 명시적으로 차단
- dry-run조차 bridge를 `None`으로 넘김 → 외부 memory API 호출은 **구조적으로 불가능**
- 실제 데이터는 로컬 ledger `native_memory_mirror` 테이블에만 저장
- `knowledge_search_service`의 native-memory semantic recall은 disabled client라 **항상 no-hit**
- 실사용 recall은 PG/Qdrant + ledger

**⇒ native_memory(외부 브리지에 직접 쓰는 기능)는 RAGFlow F/O 후 대체 백엔드로 **옮겨지지 않았다**. 이 자체가 미결 제품 결정이다.**

## 죽은 코드

- `RetiredIndexBridgeRetrievalTranscriptMemorySource` (`memory_regeneration.py:145`) — 참조 0
- `SessionFileTranscriptMemorySource` (`:322`) — 참조 0
- `build_create_memory_request`·`build_list_memories_request` — 프로덕션 live caller 없음
- `RetiredIndexBridgeTranscriptMemorySource` (`:122`) — `dirty_session_memory_sync.py`의 `index_read_sot` 경로에서만 사용

## 명시적으로 보존할 이름 (격리 계약 유지)

`legacy/` 밖으로 옮겨도 계약이 유지되는 것은 **3개뿐**:
- `RetiredIndexBridgeApiError`, `envelope_code`, `envelope_failed` — wire shape 없음

`build_*_request` 10개는 pure지만 `/api/v1/...` endpoint path를 내포하므로 **legacy 안에 남겨야** 격리 계약이 유지된다.

## 세 가지 선택지

> ⚠️ **사용자 질문으로 선택지가 정리됨**: "통역사를 완전 제거하면 6개 도구도 제거해야 하는 거 아닌가?"
> → **맞습니다.** 통역사를 지우면 6개 도구는 `ImportError`로 죽습니다. 그리고 6개 도구는
> RAGFlow 접속 외에 유의미한 일을 하지 않습니다(아래 실측). 따라서 "통역사만 남기고 도구는
> 주석 처리"(B)는 **일관되지 않은 중간 상태**입니다. **A 또는 C**가 정답입니다.

| | 선택지 A: 현행 유지 | 선택지 C: 통역사 + 6개 도구 함께 폐기 |
|---|---|---|
| 범위 | 없음 | `legacy/index_client.py` 748줄 + 6개 도구 + native_memory 2개 제거 |
| native_memory | 미결 그대로 | **같이 결정 필요** (아래) |
| 위험 | RAGFlow 부활 시 경로 복원 가능 | 낮음 — 이미 죽은 경로만 제거 |
| 효과 | 없음 | 죽은 surface 전량 제거 |

## 6개 도구가 RAGFlow 외에 하는 일 (실측)

| 도구 | RAGFlow 외 작업 | 결론 |
|---|---|---|
| `session_memory_gc.py` | `_mark_gc_deleted`·`_record_audit`로 ledger 기록. 단 이 기록(`memory_gc_audit` 테이블)의 **프로덕션 리더는 없고 테스트만** (`grep list_memory_gc_audit` → `test_gc_safety_auditor.py`·`test_pg_parity.py`·`test_db_adapter.py`·`test_ledger_pg_migrate.py` 전부 테스트) | RAGFlow 삭제 대상 선정에 필요한 부수 기록 |
| `transcript_volume_gc.py` | 없음 (RAGFlow 문서만 다룸) | 순수 RAGFlow 도구 |
| `transcript_session_gc.py` | 없음 | 순수 RAGFlow 도구 |
| `transcript_backfill.py` | `mark_session_memory_dirty` — **RAGFlow에서 읽은** 문서를 ledger에 "다시 만들어야 한다"고 표시 | RAGFlow 없으면 할 일 없음 |
| `gc_backup.py` | 없음 (백업을 RAGFlow에 올리는 도구) | 순수 RAGFlow 도구 |
| `cleanup_readiness.py` | 없음 (RAGFlow dataset 상태 점검) | 순수 RAGFlow 도구 |

**⇒ 6개 도구 중 RAGFlow 접속 외에 남길 가치가 있는 것은 없습니다.** `session_memory_gc.py`의 ledger
기록도 RAGFlow 삭제 대상을 **기록**할 뿐이고 그 기록을 읽는 프로덕션 코드는 없습니다.

## native_memory 도 함께 판단해야 함

`native_memory_writer.py`·`native_memory_reconcile.py`는 `legacy/index_client`에서
`envelope_code`·`envelope_failed`(순수 파서)를 import하고, RAGFlow **메모리 API**에 씁니다
(`add_message`·`search_messages`·`disable_message`).

- 실측: `native_memory_write_runner.py:203` — live 실행은 `failed_error_class: "live_native_memory_sync_not_vendored"`로 **명시적 차단**, dry-run조차 `retired_index_bridge=None`
- 실사용 데이터는 로컬 ledger `native_memory_mirror` 테이블에만 저장
- `knowledge_search_service`의 native-memory semantic recall은 disabled client라 **항상 no-hit**
- 실사용 recall은 PG/Qdrant + ledger

**⇒ native_memory(외부 서버에 직접 쓰는 memory 기능)는 RAGFlow F/O 후 대체 백엔드로 옮겨지지
않았습니다.** 이건 미결 제품 결정입니다.

`envelope_code`·`envelope_failed`는 RAGFlow 응답 envelope 파서이므로 native_memory가 사라지면
사용처도 사라집니다. 두 헬퍼는 `legacy/` 안에 남길 이유가 없습니다.

## C의 구체 범위 (native_memory 결정에 따라 2 갈래)

### C-1: native_memory를 **discontinued** (RAGFlow 기능 폐기)

가장 단순하고 일관됩니다. "외부 브리지에 직접 쓰는 memory"라는 기능 자체가 F/O된 백엔드에
묶여 있었으므로, 그 백엔드가 죽으면 기능도 죽습니다.

**삭제 대상**:
- `worker/lib/agent_knowledge/legacy/` 전체 (`index_client.py` 748줄 + `__init__.py`)
- `session_memory/session_memory_gc.py`, `transcript_volume_gc.py`, `transcript_session_gc.py`,
  `transcript_backfill.py`, `gc_backup.py`, `cleanup_readiness.py`
- `session_memory/native_memory_writer.py`, `native_memory_reconcile.py`,
  `native_memory_write_runner.py`, `native_memory_recall.py`, `native_memory_sync.py`,
  `native_memory_mirror.py`, `native_memory_reconcile.py`
- `session_memory/memory_regeneration.py:145` `RetiredIndexBridgeRetrievalTranscriptMemorySource`
- `session_memory/memory_regeneration.py:322` `SessionFileTranscriptMemorySource`
- `session_memory/memory_regeneration.py:122` `RetiredIndexBridgeTranscriptMemorySource`
  (이것도 `dirty_session_memory_sync.py`의 `index_read_sot` 경로에서만 쓰이므로 함께 제거)
- `cli.py`의 6개 명령 등록 (`cleanup-readiness`, `session-memory-gc`, `transcript-backfill`,
  `transcript-session-gc`, `transcript-volume-gc`) + native_memory 관련 명령
- `worker/tests/test_retired_index_bridge_protocol_guard.py`의 allowlist(전부 삭제 대상이므로
  테스트 자체 삭제) 및 `test_autopilot_no_index_client_before_m9.py`의 관련 항목
- 각 도구의 테스트 파일

**유지/추가**:
- `native_memory_mirror` ledger 테이블과 이미 저장된 행 — **데이터는 지우지 않습니다.** recall
  경로(`knowledge_search_service`)가 여전히 읽을 수 있으므로 읽기 전용으로 남기고, 쓰기 경로만
  제거하는 게 안전합니다. 단 native-memory semantic recall이 `search_messages`를 호출하는데
  bridge가 disabled면 이미 no-hit이므로 동작 변화는 없습니다.
- `memory_gc_audit` 테이블 — 빈 감사 로그. 스키마는 남겨도 되고, 통째로 지워도 됩니다.

### C-2: native_memory를 **대체 백엔드로 이전**

`Postgres+pgvector` / `CouchDB` / `Qdrant` 중 하나를 골라 "외부 서버에 직접 쓰는 memory"를
그쪽으로 옮깁니다. native memory의 의미(에이전트 대化的 요약/메모리를 벡터 저장소에 직접 기록)를
그대로 옮기는 작업이라, C-1보다 큽니다.

**이건 이번 작업에서 결정하지 않습니다.** 제품 결정입니다.

## 권고

**C-1(native_memory discontinued)**, 단 **native_memory의 데이터는 남기고 쓰기 경로만 제거**합니다.

근거:
1. 통역사·6개 도구·native_memory가 **하나의 사슬**입니다. RAGFlow가 죽으면 이 사슬 전체가
   동작 불능이고, 이미 라이브 deploy 쪽 gc/backfill lane은 삭제된 상태입니다. **일부만 남기는
   B는 가장 나쁜 상태**(죽은 코드가 두 형태로 남음)입니다.
2. native_memory의 실사용 데이터는 이미 로컬 `native_memory_mirror`에 있고, semantic recall은
   이미 no-hit입니다. 즉 **사용자가 체감하는 기능 손실이 없습니다.**
3. C-2(대체 백엔드)는 PG/Qdrant recall이 이미 그 역할을 수행 중입니다. 같은 일을 하는 경로를
   두 개 유지할 이유가 없습니다.
4. `memory_gc_audit` 프로덕션 리더가 없어 GC 감사 기록도 실용 가치가 없습니다.

**보류 사항**: native_memory가 discontinued가 아니라 "다른 백엔드로 옮김"이 맞다면 C-2가 됩니다.
이 한 가지가 1단계의 나머지를 결정합니다.

---

# 2단계 — evidence 이중 계층 구조 판단

## 사실관계

| | recall 경로 | Graphiti entity pass |
|---|---|---|
| 진입점 | `pack_session_memory_document` (`memory_regeneration.py:1279`) | `session_episode_from_couchdb_source` (`runtime.py:714`) |
| trigger | `memory_regeneration_cli.py:111,148`, `session_memory_private_sync_cli.py:140`, `dirty_session_memory_sync.py:73`, `memory_regeneration.py:1121` | `couchdb_projection_cli.py:226`, `bulk_semantic_cli.py:235` |
| chunk prose 소스 | ledger `transcript_chunks` — **`ki.status = 'indexed'` 만** (`ledger_ingress_mixin.py:1147`) | CouchDB `conversation_chunks` — **활성 source revision 만** |
| evidence 소스 | ledger `tool_evidence_summaries` — **status 필터 없음, 전부** (`ledger_native_memory_mixin.py:761-768`) | CouchDB `tool_evidence_bundles` — 활성 revision 만 |
| 렌더링 | `### N cat/outcome`, `- command:`, `- result:` | 동일 + `- tool:` 줄 |
| 소비 | 검색/리콜 (Qdrant 등 검색 미러) | 엔티티·관계 추출 후 Neo4j 그래프 |

**양쪽 evidence의 원본은 동일하다.** `ToolEvidenceSummaryRecord` 하나에서 파생되며, `transcript_parsers/evidence.py:293`이 유일한 생성 지점이다. 생성 후 ledger(`tool_evidence_summaries` 테이블)와 CouchDB(`tool_evidence_bundles` doc)로 각각 저장된다.

## 판단 1 — 이중 구조가 정당한가

**정당합니다. 다만 "같은 정보를 두 군데"가 아니라 "같은 원본을 다른 시점·범위로 읽는" 구조입니다.**

근거:
- recall은 ledger의 `indexed` chunk만 읽습니다. 즉 **검색 인덱스에 실제로 올라간 세션만** recall 대상입니다.
- Graphiti는 CouchDB의 **활성 source revision**만 읽습니다. 즉 **현재 상태의 세션 전체**가 그래프 대상입니다.
- evidence는 recall에서 status 필터 없이 전부 읽지만, chunk 자체가 `indexed`로 제한되므로 **같은 세션 안에서** evidence의 커버 범위는 chunk를 따라갑니다.

따라서 두 문서가 담는 것은:
- recall: "검색에 오른 세션의 도구 실행 기록" (recallable)
- Graphiti: "현재 활성 세션의 도구 실행 기록" (graphable)

**질문이 "이게 중복인가"에 대한 답은 "아니다"입니다.** 다만 아래 판단 3에서 실제로 손봐야 할 비대칭이 하나 나왔습니다.

## 판단 2 — 렌더러 통일 가능한가

**가능하지만 지금은 하지 않는 것을 권고합니다.**

두 렌더러는 파싱 대상이 다릅니다:
- recall(`_sectioned_tool_evidence_lines`) — ledger row dict → `category`/`outcome`/`evidence_index`/`command_summary`/`redacted_summary` 필드
- Graphiti(`_extraction_text_from_evidence`) — CouchDB doc body 문자열 → `### `/`- tool:`/`- command:`/`- result:` 라인 파싱

앞쪽은 **구조화된 필드**를, 뒤쪽은 **body 문자열**을 읽습니다. 즉 Graphiti 쪽이 "파싱"을 하고 recall 쪽은 "필드 매핑"을 합니다. 이 차이는 파싱 단계에서만 발생합니다.

**권고**: 공통 렌더러를 만들되, 입력 형태를 통일하는 게 먼저입니다. 지금 바로 만들면 두 경로의 출력 형식을 억지로 맞추느라 어느 쪽도 정확하지 않게 됩니다. 판단 3의 정리와 함께 하면 자연스럽게 정리됩니다.

## 판단 3 — recall 경로에 추가 조치 필요한가

### ⚠️ 초안의 지적이 틀렸습니다 (구현 전 실측에서 발견, 정정)

초안은 "recall의 evidence에 `status` 필터가 없어서 index 안 된 세션의 evidence가 섞인다"고 지적하고
`status = 'indexed'` 조건 추가를 권고했습니다. **구현 전 실측 결과 이 지적은 성립하지 않습니다.**

**근거 1 — evidence의 `status`는 `indexed`로 전이되지 않습니다.**
- `ToolEvidenceSummaryRecord`(`transcript_model.py:272`)에 `status` 필드가 **없습니다**.
- `upsert_tool_evidence_summary`(`ledger.py:2605`)의 `ON CONFLICT(evidence_id_hash) DO UPDATE SET` 목록에도
  `status`가 **없습니다** → 갱신되지 않고 DB 기본값 `'prepared'`로 남습니다.
- `tool_evidence_summaries` 테이블의 `status`를 갱신하는 코드가 저장소 전체에 **없습니다**
  (grep: `ledger.py`·`ledger_ingress_mixin.py`·`ledger_native_memory_mixin.py` 모두).
- `knowledge_items`(`ki`)에도 evidence는 등록되지 않습니다.

즉 **`status = 'indexed'` 조건을 추가하면 recall의 evidence가 전부 사라집니다.** 초안 권고는
recall evidence를 삭제하는 변경이었습니다.

**근거 2 — 범위 불일치는 구조적으로 발생하지 않습니다.**
recall이 evidence를 읽는 경로:
```
[1] list_indexed_transcript_chunks  → WHERE ki.type='conversation_chunk' AND ki.status='indexed'
[2] _group_by_session(chunks)        → index된 chunk들을 (session_id_hash, provider, project)로 그룹
[3] _list_tool_evidence_for(group)   → lister(..., session_id_hash=group.session_id_hash)
```
evidence는 **[3]에서 이미 그룹과 동일한 `session_id_hash`로 조회**됩니다. 그룹 자체가 index된 세션으로만
만들어지므로, "index 안 된 세션의 evidence"가 recall 문서에 들어올 경로가 없습니다.

### 결론 — 판단 3: 조치 불필요 (현행 유지)

recall의 범위는 이미 정합합니다. **코드 변경 없음.** 근거를 기록으로 남깁니다.

단, `list_tool_evidence_summaries`가 `status`를 무시한다는 점은 **의도된 설계**로 코드에 명시하는 게
좋습니다. 향후 누군가 chunk 쪽에만 필터를 추가하거나, evidence에 `status` 전이를 도입하면
`prepared`/`indexed`가 갈라질 수 있기 때문입니다. 이는 동작 변경이 아니라 **의도 기록**입니다.

**기록 위치**: `ledger_native_memory_mixin.py:741` `list_tool_evidence_summaries` docstring.
"`tool_evidence_summaries`는 `knowledge_items`에 등록되지 않고 `status` 전이도 없다. 따라서 이 조회는
status로 걸러지 않으며, 호출자는 session 단위 범위(=같은 `session_id_hash`)로만 걸러진다는 점을
전제로 한다. recall 경로는 `_group_by_session`이 index된 chunk로 그룹을 만든 뒤 같은
`session_id_hash`로 evidence를 조회하므로 범위가 정합한다."

## 판단 4 — provider 간 차이

- antigravity: 이제 stdout이 제거된 prose + `## Tool Events`(고신호 라벨)만 → recall·Graphiti 모두 "어떤 명령 + 결과"를 evidence로 받음
- Claude/Codex/Gemini/Grok: 원래부터 tool 결과가 chunk에 안 들어감 → 동일

**⇒ provider 간 차이는 없음.** 두 경로 모두 antigravity에서 압축된 evidence를 동일하게 취급합니다.

---

# 3단계 — 라이브 이미지 재빌드·재배포 (실측 완료, 실행은 승인 대기)

## 절차 (확인됨)

1. **빌드**: GoCD 자동 빌드. `k3s/ci-cd/neurons/gocd/pipelines/ci/{ingress-worker,session-memory-worker,ingress-api}.gocd.yaml` — `neurons` repo의 `main` push 트리거
2. **배포**: ArgoCD GitOps. canary overlay → `neurons-oci-canary-*` app → 검증 → `neurons-auto-delivery-*`로 production promote
3. **컴포넌트 정의**: `k3s/ci-cd/neurons/components.yaml`

`neurons` 저장소에 main을 push하면 GoCD가 자동으로 빌드합니다. 즉 **코드 PR 머지가 곧 빌드 트리거**입니다.

## 현재 라이브 이미지 (롤백 기준, 2026-10-22 실측)

| deployment | image tag | digest |
|---|---|---|
| `neurons-ingress-worker` | `ops-bbd0bb346bc7ace351656ea830affded0d990ba5` | `sha256:94d737e6e19109c2e5334934fa77e14c52143f68fa0663d6238f3a7e65a86a74` |
| `neurons-session-memory-worker` | `ops-2dbdc0725befc8e9c35a7cb1fac17e71f501e8e6` | `sha256:5a58398603623620b3f727ef92361d92eab3ad4c1d41592b1b5a8cdf844ead4e` |
| `neurons-ingress-api` | `ops-295bffae904e73966af603c14094bd657cb8fb2f` | `sha256:837ac26ba5a05c863fa479c9565b1f999534092c8ea6ccc9ee010cf519bcda7e` |

## 라이브 동작 변화 예측 (실측 기반)

`session-memory-worker`의 entrypoint(`worker/deploy/session-memory/entrypoint.sh`) 주석:
> "retired external index bridge lane(gc/backfill)은 삭제됐다 … live GC는 operator 승인 절차로만 수행한다."

그리고 `build-once.sh`는 `SESSION_MEMORY_PROJECTION_BACKEND="${SESSION_MEMORY_PROJECTION_BACKEND:-qdrant}"` — **기본값이 qdrant**이고, 라이브는 env로 `postgres_pgvector`를 명시합니다. 라이브 pod env 실측 확인함.

**⇒ 라이브 동작 변화 없음.** 새 이미지의 session-memory-worker도 `postgres_pgvector`로 동작합니다.

`ingress-worker`는 `worker/Dockerfile`의 `CMD ["python", "-m", "agent_knowledge.rag_ingress.shadow_worker"]`. `INGRESS_DELIVERY_BACKEND` 기본값이 `couchdb`로 변경됐지만, 라이브는 이미 `couchdb`를 명시하므로 동작 동일.

## canary `/status` 문자열

`pressure_read_failed` → `not_configured`. neurons-ops 저장소에서 이 문자열에 의존하는 alert/eval이 있는지 확인 필요 (3단계 첫 번째 확인 항목).

## 실행 시 제약

- 브리지 `delete_document`·`disable_document` 호출 금지
- 배포 전 이미지 태그/digest 기록 (위 표) → 이상 시 이전 태그로 ArgoCD revert
- Mac이 아니라 라이브 호스트에서 배포
- secrets/raw dataset_id/raw transcript body를 로그·PR 본문에 금지
