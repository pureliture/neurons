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

| | 선택지 A: 현행 유지 | 선택지 B: 죽은 코드만 정리 | 선택지 C: legacy/ 폐기 |
|---|---|---|---|
| 범위 | 없음 | 참조 0인 2개 source class + 미사용 builder 정리 | GC/backfill/cleanup 도구를 대체 백엔드로 이전 후 `legacy/` 통째 삭제 |
| native_memory | 미결 그대로 | 미결 그대로 | **대체 백엔드 결정 필요** (가장 큰 결정) |
| 위험 | RAGFlow 부활 시 경로 복원 가능 | 낮음 | 중간 — GC의 hard delete 경로 대체가 필요 |
| 효과 | 없음 | dead code 제거 | 748줄 + 격리 계약 제거 |

**권고: B.** 근거는 두 가지입니다.

첫째, **A는 748줄을 영원히 유지**하는데 그 代码가 가리키는 서버는 죽었고 credential도 0건입니다.

> ⚠️ **정정 (초안 검토 중 발견)**: 이 초안은 처음에 (a)분류 도구를 "자동 승인 하드 delete"라고 적었으나 **틀렸습니다**. 실측 결과 GC는 `validate_goal3_live_approval`(`native_memory_sync_approval.py:62`) 게이트 뒤에 있습니다. 이 게이트는 approval 파일의 schema(`agent_knowledge_live_approval.v1`)·operation 일치·`operator_approval.approved is True`를 요구하고, dataset·base URL·argv에 바인딩되어 불일치 시 fail-closed합니다. 또한 `--execute`는 `--backup-dir` 없이는 거부됩니다(`session_memory_gc.py:462`). **즉 자동 승인이 아니라 엄격한 operator 승인 절차입니다.** "자동 승인"을 근거로 삼을 수 없으므로 아래 논리는 그 부분을 빼고 다시 세웁니다.

유지해도 되는 논지는 이렇습니다: `session_memory_gc.py`·`transcript_volume_gc.py`는 `execute=True`면 `hard_delete_documents`로 RAGFlow에 **실제 하드 delete**를 수행합니다([`session_memory_gc.py:143`](worker/lib/agent_knowledge/session_memory/session_memory_gc.py:143)). RAGFlow 정리 세션은 `worker/deploy/session-memory/entrypoint.sh`에서 이 lane을 **삭제**했고, 그 이유를 이렇게 적었습니다:

> "retired external index bridge lane(gc/backfill)은 삭제됐다: 그 백엔드는 이미 퇴역했고 라이브에도 없으므로, 은퇴한 백엔드에 대해 자동 승인 live delete를 실행하는 lane은 유지할 수 없다. live GC는 operator 승인 절차로만 수행한다."

**코드(worker/lib)와 배포(worker/deploy)의 정리 수준이 어긋나 있습니다.** deploy 쪽 gc/backfill lane은 지웠는데, 그 lane의 구현 코드(`session_memory_gc.py`·`transcript_volume_gc.py`·`transcript_session_gc.py`·`transcript_backfill.py`·`gc_backup.py`·`cleanup_readiness.py`)는 남아 있습니다. 승인 절차는 이미 제대로 갖춰져 있으니 **즉시 위험은 낮습니다.** 다만 라이브에 없는 백엔드를 대상으로 하드 delete를 하는 코드가 남아 있는 것 자체가 불필요한 surface이고, `legacy/`가 748줄을 유지하는 근거가 됩니다. **B는 이 어긋남을 해소합니다.**

둘째, **C는 native_memory 결정이 선행돼야** 하고, 그 결정은 "외부 브리지에 직접 쓰는 memory 기능"이라는 제품 질문입니다. 이번 작업 범위 밖입니다.

## B의 구체 범위

**삭제** (참조 0, 동작 영향 없음):
- `memory_regeneration.py:145` `RetiredIndexBridgeRetrievalTranscriptMemorySource`
- `memory_regeneration.py:322` `SessionFileTranscriptMemorySource`
- `build_create_memory_request`, `build_list_memories_request` (외부 caller 없음; 내부 참조도 함께 정리)

**상태 명시** (삭제하지 않되, "지금 동작 불가"를 코드에 남김):
- `session_memory_gc.py`, `transcript_volume_gc.py`, `transcript_session_gc.py`, `transcript_backfill.py`, `gc_backup.py`, `cleanup_readiness.py` — 각 모듈 docstring에 "RAGFlow는 퇴역·라이브 credential 0건이므로 이 도구는 현재 동작 불가" 명시
- `native_memory_writer.py`, `native_memory_reconcile.py` — "외부 memory 쓰기는 `live_native_memory_sync_not_vendored`로 차단, 데이터는 로컬 `native_memory_mirror`에만" 명시

**유지**: `legacy/index_client.py` 자체와 guard 테스트 allowlist (a)분류 도구가 아직 살아있으므로 파일이 필요합니다.

**영향 범위**: 삭제 대상 2개 class는 참조 0이라 어떤 CLI도 영향받지 않습니다. `build_create_memory_request`·`build_list_memories_request`는 `RetiredIndexBridgeHttpClient` 내부에서만 쓰이므로, client 내부 참조를 함께 정리해야 합니다.

## native_memory 결정 (별도)

위 B는 native_memory를 **결정하지 않습니다**. 결정이 필요한 질문:

> "외부 브리지에 직접 쓰는 native memory"를 Postgres+pgvector / CouchDB / Qdrant 중 어디로 옮길 것인가, 아니면 이 기능 자체를 discontinued할 것인가?

이건 제품 판단이라 별도 승인이 필요합니다. 본 작업은 B만 수행하고 native_memory는 상태 문서화로 남깁니다.

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

## 판단 3 — recall 경로에 추가 조치 필요한가 (여기가 실질 발견)

**있습니다. 한 가지.**

recall의 chunk는 `ki.status = 'indexed'` 조건으로 걸러지지만, **evidence는 status 필터 없이 전부** 읽습니다:

```python
# ledger_ingress_mixin.py:1147  — chunk는 걸러진다
filters = ["ki.type = 'conversation_chunk'", "ki.status = 'indexed'"]

# ledger_native_memory_mixin.py:761-768  — evidence는 안 걸러진다
SELECT * FROM tool_evidence_summaries {where}
```

`tool_evidence_summaries.status`는 `'prepared'`(기본값) → `'queued'` → `'indexed'`로 전이합니다. 그런데 recall은 `prepared` 상태의 evidence까지 전부 읽습니다.

**결과**: recall 문서에 인덱스에 아직 올라가지 않은(=검색 불가능한) evidence가 섞여 들어갈 수 있습니다. recall 문서의 "## Tool Evidence"가 실제 검색 가능 범위와 어긋날 수 있습니다.

**권고 조치** (1줄 수정):
- `list_tool_evidence_summaries`에 `status = 'indexed'` 조건을 chunk와 동일하게 추가

**영향 범위**: recall 문서가 "검색 가능한 세션의 도구 실행 기록"으로 정합해집니다. `prepared`/`queued` evidence가 recall에서 빠집니다. 이는 chunk와 동일한 규칙이므로 정합성 개선입니다.

**대안**: evidence는 chunk보다 먼저 준비되므로(evidence가 있어야 chunk가 index된다) `indexed` 조건을 걸어도 실제로 빠지는 게 없을 수 있습니다. 이 경우 수정은 "정합성" 차원에서만 가치가 있고 동작 변화는 없습니다. **이 확인은 구현 시 ledger 상태 분포를 조회해서 실측해야 합니다.**

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
