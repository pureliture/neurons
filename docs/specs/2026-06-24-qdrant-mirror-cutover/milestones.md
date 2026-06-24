# Milestones — qdrant-mirror-cutover (Stage 1, code-only)

Stage 1만 이 루프에서 실행한다(M1–M5, 가역·test-evidence). M6–M10은 라이브 게이트라
별도 operator 승인.

## M1 OpenAIEmbeddingProvider (기존 OpenAI-compatible endpoint 재사용)
- status: done
- evidence: tests/test_qdrant_embedding_provider.py 7 passed (config 재사용/size fail-closed/adapter protocol end-to-end, no network)

## M2 payload top-level filter fields + payload index
- status: done
- evidence: tests/test_qdrant_payload_schema.py + 기존 qdrant test 36 passed (top-level 승격, payload index 선언, privacy_class/result_type/project 다중 필터, 무회귀)

## M3 ledger qdrant_collections registry (additive migration)
- status: done
- evidence: tests/test_ledger_qdrant_collections.py 5 passed (upsert/get/list/enable fail-closed/reopen, additive 옆 ragflow_datasets)

## M4 Qdrant hit ledger-join (authority gate)
- status: done
- evidence: tests/test_qdrant_authority_join.py 5 passed (resolved flip, unresolved drop/flag, status gate, end-to-end query→join)

## M5 mirror reranker reuse seam (기존 OpenAI-compatible reranker)
- status: done
- evidence: tests/test_qdrant_rerank.py 6 passed (reorder/top_n/score-guard/config reuse/query→rerank→join compose)
- note(replan): ledger DDL 추가가 ledger_areas partition guard를 깨 → qdrant_collections를 AREA_D(native_memory)에 등록(SoT 변경 아님, 루프 내 replan). 전체 worker suite 889 passed.

## Stage 1 종료
- M1–M5 all done. 라이브 mutation 0.
- M6–M10(라이브 게이트)은 별도 operator 승인 — 이 루프 범위 밖.

## 적대적 검증(4-dim) 후속 수정
- BLOCKER(authority): mirror resolver가 status-only로 superseded/disabled/expired를
  못 걸러 권위로 샜음 → ledger에 `authorize_document_by_content_hash` 추가(canonical
  `_authorize_knowledge_item` 술어 재사용, behavior-preserving 추출). resolver가 그걸
  위임 → 미러가 canonical authority와 절대 발산 안 함. join이 권위 레코드로
  privacy/project/provider/currentness reconcile.
- MAJOR(privacy): query를 fail-closed scoping(미scoped 쿼리 거부) + `privacy_class`
  파라미터 + SearchableMirrorHit에 privacy_class 노출.
- MAJOR(test): server-side filter-shape 검증, enable disabled-branch, end-to-end
  unresolved-drop(real ledger) 테스트 추가.
- minor: collection enable fail-closed-all(any-disabled), config api_key 미반환,
  area count 주석, schema_migrations seed, embedding 우선순위/submit size-guard/rerank
  order/to_dict 테스트.
- 전체 worker suite 896 passed, 9 skipped. authority refactor 무회귀.
