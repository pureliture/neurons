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
- status: in-progress

## M4 Qdrant hit ledger-join (authority gate)
- status: pending

## M5 mirror reranker reuse seam (기존 OpenAI-compatible reranker)
- status: pending
