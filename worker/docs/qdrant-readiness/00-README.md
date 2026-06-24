# RAGFlow → Qdrant searchable-mirror 교체 readiness

이 디렉터리는 RAGFlow의 **vector/search mirror** 역할을 Qdrant로 교체하기 위한
사전 작업(spec + audit + code-only readiness) 문서다. **live cutover/disable/delete는
범위 밖**이며, 여기 어떤 문서도 그것을 지시하거나 수행하지 않는다.

## 정본 spec (이 문서들이 따르는 source)

이 readiness 문서는 새 결정이 아니라 **기존 spec의 audit/보강**이다. 충돌 시 아래가
우선한다:

- `docs/specs/2026-06-21-qdrant-docling-searchable-mirror/{requirements,design,milestones}.md`
  — Qdrant+Docling searchable mirror PoC(M0–M7). "searchable mirror only,
  IndexBackendAdapter 호환, evidence packet gate, production NO-GO", embedding
  model은 명시적 미결정.
- `specs/recall-cutover/{requirements,design}.md` — recall을 RAGFlow
  `session-memory`로 컷오버하고 transcript-memory를 은퇴(RC1–RC6 done). 단 그 이후
  recall은 **ledger-first**로 옮겨졌다(권위 read model = ledger; RAGFlow
  retrieval은 `--dataset-id`가 있을 때만 도는 archive/evidence 보조 lane). 따라서
  Qdrant 미러의 권위 결정은 ledger이고 미러 범위는 archive/evidence lane이다 —
  근거는 [`02-collection-schema-metadata-mapping.md`](02-collection-schema-metadata-mapping.md)
  §0의 검증 항목. (recall-cutover의 "RAGFlow session-memory recall"은 그 시점
  상태이며 ledger-first 전환이 그것을 대체한다.)
- `specs/couchdb-transcript-migration/` — CouchDB transcript source 이관.

## Scope

| 포함 | 제외 |
| --- | --- |
| RAGFlow read/write 의존성 audit | live RAGFlow write/delete/disable |
| Qdrant collection/schema/metadata filter 설계 | live routing/recall surface 변경 |
| embedding ownership + ingestion/update/delete lifecycle 정의 | current runner env 변경 |
| dual-write / shadow-read / recall parity / rollback gate spec | live GC, 실제 cutover 실행 |
| fake Qdrant adapter 수준의 code-only seam + test | Gemini Flash 계열 embedding 사용 |

## 현재 상태 (이 readiness 시점)

- RAGFlow는 **searchable runtime mirror + vector retrieval**이고 source of truth가
  아니다. canonical authority는 local ledger(PG) / CouchDB transcript source /
  Neo4j ontology다. 이 사실은 golden eval의 `authority-model`,
  `recall-transport` subject로도 고정돼 있다.
- backend-neutral 경계는 `rag_ingress/index_backend.py`의 `IndexBackendAdapter`
  protocol이다. 현재 구현 adapter는 `RAGFlowIndexBackendAdapter`,
  `CouchDBIndexBackendAdapter`, 그리고 PoC인 `QdrantDoclingMirrorAdapter` 셋이다.
- `QdrantDoclingMirrorAdapter`(`rag_ingress/qdrant_docling_mirror.py`)는 submit /
  find_by_natural_key / status / query를 구현한 PoC다. 어떤 live ingest/recall
  route에도 연결돼 있지 않고, optional import(`qdrant-client`, `docling`,
  pyproject extra `searchable-mirror`) 뒤에 있다.
- cutover gate의 골격(`build_searchable_mirror_gate_report`,
  evidence packet schema `agent_knowledge_searchable_mirror_gate_evidence.v1`)은
  이미 존재하지만, 이를 채우는 dual-write/read-compare harness는 아직 없다.
- 별도 plane인 M9 state/recall retirement chain
  (`retirement_readiness.py`, `state_shadow_readiness.py`,
  `product_surface_switch_plan.py`)은 ledger/recall-routing 은퇴를 다루며,
  **Qdrant searchable-mirror gate와는 아직 cross-reference가 없다**.

## 문서 구성

1. [`01-ragflow-dependency-audit.md`](01-ragflow-dependency-audit.md) — RAGFlow
   read/write 의존성 전수 audit과 "RAGFlow를 끄면 무엇이 깨지는가" 분류.
2. [`02-collection-schema-metadata-mapping.md`](02-collection-schema-metadata-mapping.md)
   — Qdrant collection/schema/payload/metadata filter 매핑 설계.
3. [`03-embedding-ownership-and-lifecycle.md`](03-embedding-ownership-and-lifecycle.md)
   — embedding ownership 전환과 ingestion/update/delete lifecycle 정의.
4. [`04-cutover-gates-and-rollback.md`](04-cutover-gates-and-rollback.md) —
   dual-write / shadow-read / recall parity / rollback gate spec과
   "RAGFlow를 언제 안전하게 끌 수 있는가" 단계별 gate ladder.

## 이번 readiness에서 추가된 code-only 변경

- `rag_ingress/qdrant_docling_testing.py` — 재사용 가능한 `InMemoryQdrantClient`
  (테스트/local 전용 fake). 기존엔 test 파일 내부 private stub만 있어 재사용
  불가였던 gap을 메운다.
- `rag_ingress/qdrant_docling_mirror.py` — 추가형 delete seam
  (`MirrorDeletionResult`, `MirrorDeletionCapable`, `delete_document`,
  `delete_by_natural_key`). GC/retirement가 mirror에 대해 target할 neutral delete
  지점의 초안이며 **어떤 live route에도 연결되지 않았다**.
- `tests/test_qdrant_mirror_delete_seam.py` — 위 seam의 contract test.

검증: `cd worker && uv run pytest -q` 전체 통과(이 변경 포함). live runtime
mutation 없음.

## 안전 경계 (AGENTS.md / CLAUDE.md 준수)

- `RAGFLOW_API_KEY` 하나만 사용. 새 token env 도입 없음.
- raw host/path/token/dataset_id/document_id/transcript body를 문서에 쓰지 않는다.
  이 문서들은 symbol 이름과 logical role만 참조한다.
- live disable/delete/GC/routing/env mutation은 별도 evidence·승인 절차
  (문서 04의 gate ladder)를 통과하기 전에는 수행하지 않는다.
