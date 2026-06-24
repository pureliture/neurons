# RAGFlow→Qdrant Searchable-Mirror Cutover Requirements

## 승인 대상

- Source of truth: `requirements.md`
- Preview companion: none (source Markdown 기준 승인)
- 진행 방식: 자문자답(self-Q&A) grilling. 사용자 위임 + 휴먼 게이트 사전승인으로
  결정 가능한 항목은 자체 확정한다. 단, 라이브 파괴 단계는 게이트로 남긴다(Q7).

## 정본 근거 (이 spec이 따르는 source)

- `worker/docs/qdrant-readiness/00..04` — read/write audit, schema/metadata mapping,
  embedding ownership+lifecycle, gate ladder.
- `docs/specs/2026-06-21-qdrant-docling-searchable-mirror/{requirements,design,milestones}.md`
  — Qdrant+Docling mirror PoC(M0–M7, done). searchable mirror only, IndexBackendAdapter
  호환, evidence packet gate, production NO-GO.
- `specs/recall-cutover/{requirements,design}.md` — recall을 RAGFlow session-memory로
  컷오버 후 transcript-memory 은퇴(RC1–RC6 done).

## 배경 (확정된 라이브 상태)

- canonical authority: CouchDB transcript source, ledger PG MemoryCard/state,
  Neo4j/Graphiti ontology. Qdrant/RAGFlow는 mirror이며 authority 아님.
- recall은 **ledger-first**다. `mcp_server.KnowledgeSearchService.brain_query`의 권위
  read model은 `LegacyLedgerBrainReadModel`(ledger)이고, RAGFlow `retrieve`는
  launch에 `--dataset-id`가 있을 때만 켜지는 archive/evidence 보조 lane이다.
  옛 recall 토글(`--state-db-recall`/`--ragflow-direct-recall`)은 현재 no-op.
- embedding/rerank: repo에 이미 OpenAI-compatible embedder/reranker가 있다
  (`graphiti_adapter.GraphitiNeo4jConfig`: `LLM_BRAIN_EMBEDDING_*`,
  `OPENAI_BASE_URL`, `embedding_dim=1024`; graphiti_core OpenAI embedder +
  cross-encoder reranker).
- `IndexBackendAdapter`에 delete 없음. 이번 readiness에서 Qdrant adapter에 delete
  seam 초안(`delete_document`/`delete_by_natural_key`, 라이브 미연결) 추가됨.

## 질문-답변 흐름 (자문자답)

### Q1: Qdrant가 대체하는 정확한 대상은?

RAGFlow의 **vector retrieval(searchable mirror)** 역할만 대체한다. 구체적으로
brain.query archive/evidence 후보 lane, knowledge.search ranking,
supersede_detector 벡터 후보 단계, document-bridge lane이 쓰는 `ragflow.retrieve`.
RAGFlow 제품/운영면 전체나 canonical authority는 대체하지 않는다.

### Q2: 미러에 무엇을 적재하는가?

recall surface가 벡터 검색하는 dataset(=launch `--dataset-id`로 붙는 archive/
evidence 후보 dataset; 현재 derived memory는 단일 `derived-memory-items`로
consolidate)의 **redact된 파생 문서**다. 적재 안 함: raw transcript(CouchDB),
canonical MemoryCard 원본(ledger), session-memory 권위 recall(ledger-first라 미러
밖), secret-like 필드.

### Q3: embedding/rerank는 무엇을 쓰는가?

**새 모델 결정 없음.** 기존 OpenAI-compatible embedder(`LLM_BRAIN_EMBEDDING_*`,
`OPENAI_BASE_URL`, dim 1024)를 `EmbeddingProvider`로 재사용하고, 기존
OpenAI-compatible reranker(cross-encoder)를 mirror 후보 재정렬에 재사용한다.
vector size=1024, distance=Cosine. Gemini Flash 계열 아님, 새 secret 없음.

### Q4: ingestion/update/delete lifecycle은?

- create: `point_id_for_natural_key(target_profile, idempotency_key, content_hash)`로
  단일 upsert(동기 INDEXED). leak/secret fail-closed.
- update-in-place 없음: 같은 content_hash 재upsert=overwrite, 본문 변경=새
  content_hash=새 point.
- supersede: 새 point upsert + 옛 point disable/delete.
- delete: 이번에 추가한 delete seam을 GC chokepoint로 연결(Qdrant 전용 hard-delete
  chokepoint 신설).
- disable(soft): Qdrant native 없음 → payload `enabled` 필드 + 모든 read filter에
  `enabled=true` 강제. 최종 삭제는 hard delete.

### Q5: collection/schema 전략은?

단일 collection + payload filter를 기본으로 한다. filter 필드(`target_profile`,
`privacy_class`(필수), `result_type`, `project`, `provider`, `session_id_hash`,
`content_hash`, `idempotency_key`, `document_kind`)를 top-level payload로 승격하고
collection 생성 시 payload index를 선언한다. logical→collection 매핑·enable 상태는
ledger `qdrant_collections` 레지스트리(additive migration)가 authority다.

### Q6: 안전한 컷오버 단계 순서는?

Stage 1 code-only readiness → Stage 2 dual-write shadow(관찰) → Stage 3 shadow-read
parity(recall@k+golden, soak) → Stage 4 read cutover(mirror lane만) → Stage 5 write
cutover + no-fallback 소비자 이전 + Qdrant GC chokepoint → Stage 6 RAGFlow 벡터
mirror disable. 각 단계는 이전 단계 evidence가 green일 때만 진입. RAGFlow는 Stage 6
이전까지 ON(fallback). Stage 4까지 가역, Stage 6 hard delete만 비가역(backup 선행).

### Q7: 사전승인과 라이브 파괴 단계의 경계는?

사용자 휴먼 게이트 사전승인은 **설계 + 가역·code-only 실행(Stage 1)의 in-loop
게이트**에만 적용된다. 라이브 RAGFlow disable/delete/GC execute/routing/env mutation
(Stage 4–6의 라이브 부분)은 repo 가드레일상 blanket 사전승인으로 못 덮는다 — 각 건
current evidence, exact argv, bounded timeout, redaction, postcheck, rollback/abort
기준을 분리한 **operator 승인 게이트**로 남긴다. agentic-execution은 그 지점에서
멈춘다(hard-to-reverse human gate).

### Q8: 범위 밖(YAGNI)은?

RAGFlow 벡터 미러 외 RAGFlow 전체 제거, production Qdrant 배포/Docker/systemd/
firewall, canonical authority의 Qdrant 이전, hybrid search 튜닝(기존 reranker 재사용
이상), recall을 ledger-first로 바꾸는 일(이미 done), raw transcript 접근.

## 기능 요구사항

- Qdrant `EmbeddingProvider`가 기존 OpenAI-compatible embedding endpoint를 재사용한다
  (dim 1024, Cosine). 테스트는 fake/no-network로 통과한다.
- mirror 후보 재정렬에 기존 OpenAI-compatible reranker를 옵션으로 재사용한다.
- filter 필드를 top-level payload로 승격하고 collection 생성 시 payload index를
  선언한다(`privacy_class` 필수 포함).
- ledger `qdrant_collections` 레지스트리(logical_name, collection, embedding_model,
  vector_size, distance, payload_index_version, enabled)를 additive migration으로
  추가한다.
- Qdrant hit을 product 사용 전 ledger/CouchDB로 authority-join 강제한다
  (`canonical_resolution_required` 충족).
- delete seam을 Qdrant 전용 GC hard-delete chokepoint로 연결한다(disable=payload
  enabled, hard delete는 retention/backup 후).
- dual-write shadow sink, read-compare parity harness(recall@k+golden),
  read/write 단계별 cutover와 rollback을 evidence 분리해 단계 게이트로 만든다.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| Authority | CouchDB/ledger PG/Neo4j canonical 유지. mirror는 비권위, 항상 ledger-join. |
| Safety | 라이브 RAGFlow disable/delete/GC/routing/env는 단계별 evidence+operator 승인 게이트. 사전승인으로 미적용. |
| Reversibility | Stage 4까지 가역(recall 되돌리기). Stage 6 hard delete만 비가역, backup이 rollback. |
| Recall no-regression | mirror lane recall@k가 RAGFlow 기준선 대비 목표 이상 + golden 회귀 0(soak window). |
| Embedding | 기존 OpenAI-compatible(LLM_BRAIN_EMBEDDING_*, dim 1024) 재사용. 새 모델/secret 없음. Gemini Flash 아님. |
| Privacy | raw host/path/token/dataset_id/document_id/transcript body 출력 금지. leak/secret fail-closed. |
| Compatibility | 기존 enqueue payload, targetProfile, IndexBackendAdapter contract 유지. |
| Local-first | qdrant/docling는 optional `searchable-mirror` extra. 테스트는 fake/in-memory로 network 없이 통과. |
| Idempotency | natural-key point_id로 upsert/dedup 결정적·재실행 안전. |

## 사용자 시나리오

- 개발자는 Stage 1 code-only(embedding provider/payload index/ledger registry/
  ledger-join)를 worktree에서 구현하고 worker test green으로 검증한다(라이브 0).
- 운영자는 Stage 2에서 Qdrant를 격리 shadow sink로 dual-write하고, Stage 3에서
  recall@k+golden parity가 무회귀임을 evidence packet으로 확인한다.
- 운영자는 Stage 4에서 mirror lane recall을 Qdrant로 재배선(가역)하고 안정 window를
  관측한다. RAGFlow는 fallback으로 ON.
- 운영자는 Stage 5에서 write를 전환하고 no-fallback 소비자를 이전한 뒤, Stage 6에서
  backup·승인 후 RAGFlow 벡터 미러를 disable한다(hard delete는 retention 후 별도).

## 미결정 항목

- recall@k parity 정량 임계 최종값(Stage 3에서 RAGFlow 기준선 측정 후 고정).
- 단일 collection vs `privacy_class`별 분리(접근정책 입력 시 재검토; 기본 단일).
- M9 closure-chain에 searchable-mirror gate를 cross-reference하는 정확한 schema 연결
  지점(Stage 6 착수 시 확정).
- 라이브 recall mcp-stdio가 `--dataset-id` 없이 떠 있는지의 런타임 확인(벡터 lane
  실사용 여부) — 결정이 아니라 사실 확인.
