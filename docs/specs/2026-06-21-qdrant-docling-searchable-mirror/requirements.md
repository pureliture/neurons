# Qdrant+Docling Searchable Mirror PoC Requirements

## 승인 대상

- Source of truth: `requirements.md`
- Preview companion: none

## 질문-답변 흐름

### Q: 무엇을 대체하는가?

RAGFlow 전체 제품/운영면이 아니라, RAGFlow가 맡던 searchable runtime mirror 역할만 대체한다.

### Q: 무엇이 계속 canonical authority인가?

CouchDB transcript source, ledger PG MemoryCard/state, Neo4j/Graphiti derived ontology graph는 계속 각자의 authority를 유지한다.
Qdrant+Docling은 read/search mirror이며 canonical write authority가 아니다.

### Q: 언제 RAGFlow failover가 가능한가?

dual-write/read-compare가 통과하고 operator approval이 있기 전까지 RAGFlow failover는 금지한다.
PoC는 failover-ready를 주장하지 않고 `NO-GO`를 유지한다.

## 기능 요구사항

- `RagTargetAdapter`, `rag_ingress`, `session_memory`, `ontology` contract를 먼저 감사하고 보존한다.
- `IndexBackendAdapter` 뒤에 Qdrant+Docling searchable mirror PoC를 둔다.
- `targetProfile`은 logical key로 유지하고 physical collection/resource id는 adapter-private으로 둔다.
- Docling은 incoming `RagReadyDocument` body를 markdown 검색 문서로 normalize하는 경계로만 사용한다.
- Qdrant는 normalized markdown과 redacted metadata를 저장하는 searchable mirror로만 사용한다.
- PoC는 Apple Silicon local-first를 우선한다. Qdrant local mode 또는 injected fake client로 테스트 가능해야 한다.
- Ubuntu neurons host validation은 production mutation 없이 gate report와 digest-bound evidence packet으로 판단한다.
- RAGFlow dual-write/read-compare evidence packet 검증 전 failover는 report에서 blocked로 남아야 한다.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| Safety | live RAGFlow write/delete/disable, GC execute, Docker/systemd/firewall, credential edit 금지 |
| Authority | CouchDB/ledger PG/Neo4j canonical authority 유지 |
| Privacy | raw host, private path, token, API key, raw transcript body, raw dataset_id, raw document_id 출력 금지 |
| Compatibility | 기존 enqueue payload, `targetProfile`, `IndexBackendAdapter` contract 유지 |
| Local-first | Qdrant/Docling dependency는 optional `searchable-mirror` extra; tests는 fake/in-memory path로 network 없이 통과 |
| Cutover | digest-bound dual-write/read-compare packet + approval 전 `production_authority_status=NO-GO` |

## 사용자 시나리오

- 개발자는 local Apple Silicon에서 fake or local Qdrant client로 document submit/search smoke를 실행한다.
- operator는 Ubuntu host에서 dry-run gate report와 redacted evidence packet으로 mirror PoC readiness를 확인한다.
- RAGFlow는 계속 current searchable mirror로 남고, Qdrant mirror는 compare-only 후보로 남는다.
- read path는 local ledger/CouchDB/Neo4j authority를 winner로 유지하고 mirror candidate를 canonical truth로 승격하지 않는다.

## 수용 기준

- [x] 새 PoC adapter가 `IndexBackendAdapter` protocol과 호환된다.
- [x] submit은 Docling-normalized markdown, content hash, idempotency key, logical target profile을 Qdrant payload에 저장한다.
- [x] natural-key lookup은 empty key에서 fail-closed이고, 동일 `targetProfile` + `idempotencyKey` + `contentHash`만 match한다.
- [x] status lookup은 Qdrant point existence를 generic `IndexStatus`로만 반환한다.
- [x] private path/secret-like normalized text와 nested secret-like metadata는 submit 전에 fail-closed한다.
- [x] mirror query는 `targetProfile` payload filter를 사용하고 result에 canonical authority join 필요 상태를 표시한다.
- [x] gate report는 dry-run/redacted only이며 evidence packet 없이는 comparison-ready를 내지 않고 `production_authority_status=NO-GO`와 failover blocked를 유지한다.
- [x] M0~M7 milestone spec이 repo에 남아 구현/검증 루프의 source가 된다.

## 비목표

- RAGFlow 전체 제거 또는 live failover.
- production Qdrant deployment, Docker/systemd/firewall mutation.
- canonical authority를 Qdrant로 이전.
- raw transcript/source body 접근.
- embedding quality 최적화, reranker, hybrid search tuning.

## 미결정 항목

- 실제 production embedding model. PoC는 injected embedder 또는 local deterministic embedder를 허용한다.
- Qdrant physical collection naming. PoC는 adapter-private으로 유지한다.
- Evidence packet의 장기 보관 위치. 현재 PoC는 redacted packet path를 입력으로 받아 검증한다.
