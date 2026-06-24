# RAGFlow→Qdrant Searchable-Mirror Cutover Design Spec

## Overview

RAGFlow의 vector retrieval(searchable mirror) 역할을 Qdrant로 무회귀·가역 단계로
교체한다. canonical authority(CouchDB/ledger PG/Neo4j)와 ledger-first recall은
그대로 두고, 미러는 archive/evidence 후보 lane에만 관여하며 항상 ledger-join을
거친다. 라이브 파괴 단계는 operator 승인 게이트로 남긴다.

## Requirements Reference

- Phase 1 source: `requirements.md`
- 핵심: searchable mirror only, 기존 OpenAI-compatible embedder/reranker 재사용,
  단계별 evidence 게이트, Stage 6 hard delete만 비가역.

## Approach Proposal

### A. 기존 seam 재사용 staged ladder (추천)

`IndexBackendAdapter`(이미 Qdrant adapter 존재) + `shadow_worker`(격리 dual-write
선례) + `product_surface_switch_plan`(recall 재배선 rollback/approval 패턴) +
`build_searchable_mirror_gate_report`(evidence packet) + `recall-cutover` 단계
패턴을 재사용한다. 증분·가역·기존 가드와 정합. **추천 이유**: 새 인프라 최소,
각 단계가 독립 evidence를 내고 Stage 4까지 되돌릴 수 있다.

### B. big-bang 전환

dual-write/parity 없이 한 번에 RAGFlow→Qdrant. **기각**: rollback 없음, 모든 human
gate(라이브 쓰기/삭제/재배선)를 동시에 침범, recall 회귀 위험.

### C. 병렬 신규 recall 서비스

Qdrant 전용 recall 경로를 새로 만든다. **기각**: ledger-join/권위 규약 중복,
brain.query lane 계약 위반, scope creep.

→ **A 채택.**

## Architecture

```text
[Stage 1 code-only]
RagReadyDocument ─► QdrantDoclingMirrorAdapter (IndexBackendAdapter)
   ├─ Docling/Passthrough normalize
   ├─ OpenAIEmbeddingProvider  (LLM_BRAIN_EMBEDDING_*, dim 1024)   ◄ 신규 seam
   ├─ payload(top-level filter fields + index)                     ◄ 보강
   └─ Qdrant upsert(point_id = natural key)
mirror query ─► (optional) OpenAI reranker ─► SearchableMirrorHit
                                              └─ ledger/CouchDB authority-join ◄ 신규
ledger.qdrant_collections (logical→collection, enabled)            ◄ 신규 registry
delete_document / GC hard-delete chokepoint(Qdrant)                ◄ seam→chokepoint

[Stage 2+ live, gated]
shadow_worker ─(dual-write)─► RAGFlow(record) + Qdrant(shadow)
read-compare harness ─► recall@k + golden ─► evidence packet
recall mirror lane ─(Stage4 switch)─► Qdrant (RAGFlow fallback ON)
write cutover(Stage5) ─► no-fallback 소비자 이전 + Qdrant GC
RAGFlow 벡터 mirror disable(Stage6) ─► backup → retention → hard delete
```

canonical authorities는 adapter 밖에 그대로 유지된다.

## Data Flow

1. ingress가 `RagReadyDocument`를 만들거나 복구한다(기존 경로).
2. adapter가 normalize→leak/secret fail-closed→embed(OpenAI-compatible)→payload
   구성→`point_id` upsert. 동기 INDEXED 반환.
3. mirror query는 vector 검색(옵션 rerank) 후 `SearchableMirrorHit`을 내고,
   product 사용 전 ledger/CouchDB authority-join을 강제한다.
4. (Stage 2) shadow가 RAGFlow와 Qdrant에 동시 기록하고 관찰만 한다.
5. (Stage 3) read-compare가 동일 query에 대해 두 경로를 ledger-join 후 비교해
   recall@k+golden 무회귀 evidence를 만든다.
6. (Stage 4) recall mirror lane을 Qdrant로 재배선(가역, rollback manifest 보존).
7. (Stage 5) write 전환 + no-fallback 소비자 이전 + Qdrant GC chokepoint 연결.
8. (Stage 6) backup→승인→RAGFlow 벡터 mirror disable; hard delete는 retention 후.

## Component Details

| 컴포넌트 | 입력 | 출력 | 의존 |
| --- | --- | --- | --- |
| `OpenAIEmbeddingProvider` | redacted markdown | `[float]`(dim 1024) | `LLM_BRAIN_EMBEDDING_*` OpenAI-compatible endpoint |
| payload+index 보강 | RagReadyDocument | top-level filter payload + Qdrant payload index | qdrant collection create |
| `qdrant_collections` registry | logical_name | collection/embedding_model/vector_size/distance/enabled | ledger(additive migration) |
| ledger-join gate | SearchableMirrorHit | authority-resolved 후보 | ledger.authorize_document / CouchDB |
| mirror reranker(옵션) | mirror 후보 | 재정렬 후보 | 기존 OpenAI-compatible cross-encoder |
| Qdrant GC chokepoint | BackendDocumentHandle | MirrorDeletionResult | delete seam(이번 추가) |
| dual-write shadow | ingress payload | RAGFlow+Qdrant 기록, ShadowResult | shadow_worker 격리 스트림 |
| read-compare harness | query cohort | recall@k/mismatch/golden evidence | RAGFlow retrieve + Qdrant query + ledger |

## Error Handling

- optional dep 부재: PoC 경로 구성 시에만 `SearchableMirrorUnavailable`.
- embed endpoint 오류: 호출자로 전파(가짜 성공 금지), delivery는 retryable/uncertain.
- blank natural key: `None`(전수 스캔 금지).
- leak/secret metadata: upsert 전 `ValueError` fail-closed.
- vector size 불일치: `ValueError`.
- ledger-join 실패: 후보를 권위로 승격하지 않음(비노출).
- read-compare 회귀: 컷오버 중단, recall RAGFlow 유지.
- backup 실패: 삭제 차단(백업 없는 삭제 금지).

## Testing Strategy

- 단위(no-network, fake/in-memory): EmbeddingProvider(차원/결정성), payload index
  필터, ledger registry CRUD, ledger-join 권위 강제, delete seam(이번 test 재사용),
  reranker 순서, gate report.
- 통합(소규모 라이브, Stage 2+): dual-write shadow 1건, read-compare 소량 cohort.
- 라이브 게이트: recall@k+golden 무회귀, 재배선 후 recall 출처 확인, backup 무결성,
  disable 후 recall 지속.
- 유지 green: `worker/tests/test_qdrant_docling_mirror.py`,
  `worker/tests/test_qdrant_mirror_delete_seam.py`,
  `worker/tests/test_brain_query.py`, `worker/tests/test_rag_ingress_readiness.py`.

## Milestones

Stage 1(M1–M5)은 code-only·가역·test-evidence라 사전승인 범위 안에서
agentic-execution 단일 goal로 실행 가능. Stage 2–6(M6–M10)은 라이브/게이트라
각 단계 operator 승인에서 멈춘다(사전승인 미적용).

- **M1 — OpenAIEmbeddingProvider (code-only)**: 기존 `LLM_BRAIN_EMBEDDING_*`
  OpenAI-compatible endpoint를 감싸는 `EmbeddingProvider`(dim 1024). done=fake로
  차원/결정성 단위 test green, vector size guard 동작.
- **M2 — payload top-level + index (code-only)**: filter 필드 top-level 승격 +
  collection create 시 payload index 선언. done=필터 test(특히 `privacy_class`)
  green, 기존 mirror test 무회귀.
- **M3 — ledger qdrant_collections registry (code-only)**: additive migration +
  read/write. done=migration test green, logical→collection 해소 test.
- **M4 — Qdrant hit ledger-join (code-only)**: SearchableMirrorHit→ledger authority
  join 강제. done=join test(권위 승격 차단) green.
- **M5 — mirror reranker reuse seam (code-only)**: 기존 OpenAI-compatible reranker로
  후보 재정렬 옵션. done=fake reranker 순서 test green.
- **M6 — dual-write shadow (live, gate)**: `shadow_worker` qdrant 분기로 격리
  dual-write(RAGFlow 무변경, 관찰). done=dual_write evidence(`total_count`,
  `target_profiles`) green, RAGFlow recall 무변경 확인.
- **M7 — read-compare parity (live, gate)**: recall@k+golden harness, soak window.
  done=`read_compare` mismatch 0 + recall@k 목표 + golden 회귀 0, 연속 green.
- **M8 — read cutover mirror lane (live, gate, 가역)**: recall mirror lane을 Qdrant로
  재배선(switch plan + rollback manifest + operator approval). done=재배선 후 recall
  출처 Qdrant 확인, regression 0, 안정 window.
- **M9 — write cutover + 소비자 이전 (live, gate)**: write 전환 + no-fallback 소비자
  (autopilot mining/ragflow_read_sot/GC runners/backfill/supersede/native reconcile)
  이전 + Qdrant GC hard-delete chokepoint 연결. done=각 소비자 source 전환 test +
  GC dry-run/coverage/backup/rollback evidence.
- **M10 — RAGFlow 벡터 mirror disable (live, gate, 비가역 마지막)**: backup→승인→
  disable, M9 closure-chain에 searchable-mirror gate cross-reference, hard delete는
  retention window 후 별도 승인. done=disable 후 recall 지속, rollback 근거 보존,
  operator approval 기록.

## Open Questions

- **M8 배선 항목(아키텍처 리뷰 후속)**: (1) `brain_query._combine_query_lanes`의 기존
  RAGFlow mirror-merge를 `join_mirror_hits_to_authority` 단일 seam으로 수렴시켜 두
  mirror lane이 supersede/revoke drop에서 발산하지 않게 한다. (2) adapter
  read/write가 `_qdrant_collection_is_enabled`를 consult하도록 enforcement 배선
  (현재 registry는 intended state만 기록). (3) query 시 registry의 `embedding_model`을
  검증해 same-dim 모델 스왑으로 미러 의미가 조용히 깨지지 않게 한다.
- per-point soft-disable(payload `enabled`) 채택 여부는 M9(GC). 그 전까지 supersede
  옛 point 정리는 hard-delete-only.
- recall@k parity 정량 임계(M7에서 RAGFlow 기준선 측정 후 고정).
- 단일 collection vs privacy 분리(접근정책 입력 시; 기본 단일).
- searchable-mirror gate ↔ M9 closure-chain schema 연결 지점(M10 착수 시).
- delete capability를 `index_backend.py` neutral 경계로 승격할지(M9에서 판단;
  현재는 `qdrant_docling_mirror.py` 국소 protocol).
