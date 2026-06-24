"""M5: optional mirror reranker seam (reuses OpenAI-compatible reranker).

No network: rank_fn is injected. Covers reorder-by-score, top_n truncation,
score-count guard, config reuse, and query->rerank->authority-join composition.
"""

from __future__ import annotations

import pytest

from agent_knowledge.rag_ingress.qdrant_authority_join import (
    LedgerContentHashAuthorityResolver,
    join_mirror_hits_to_authority,
)
from agent_knowledge.rag_ingress.qdrant_docling_mirror import (
    HashEmbeddingProvider,
    PassthroughMarkdownNormalizer,
    QdrantDoclingMirrorAdapter,
)
from agent_knowledge.rag_ingress.qdrant_docling_testing import InMemoryQdrantClient
from agent_knowledge.rag_ingress.qdrant_rerank import (
    OpenAICompatibleReranker,
    build_openai_reranker,
    resolve_reranker_config,
)
from agent_knowledge.rag_ingress.rag_ready_document import build_rag_ready_document


def _by_keyword_rank(keyword: str):
    # higher score when the keyword appears in the candidate text
    def _rank(query: str, texts: list[str]) -> list[float]:
        return [2.0 if keyword in text else 0.0 for text in texts]

    return _rank


def test_rerank_reorders_by_score_and_truncates_top_n():
    reranker = OpenAICompatibleReranker(rank_fn=_by_keyword_rank("ledger"), text_key="summary")
    candidates = [
        {"summary": "note about caches", "content_hash": "h1"},
        {"summary": "decision about ledger authority", "content_hash": "h2"},
        {"summary": "ledger recall path", "content_hash": "h3"},
    ]
    ranked = reranker.rerank(query="ledger", candidates=candidates, top_n=2)
    assert len(ranked) == 2
    assert {r["content_hash"] for r in ranked} == {"h2", "h3"}
    assert ranked[0]["rerank_score"] == 2.0


def test_rerank_empty_is_empty():
    reranker = OpenAICompatibleReranker(rank_fn=_by_keyword_rank("x"))
    assert reranker.rerank(query="q", candidates=[], top_n=5) == []


def test_rerank_strict_order_and_top_n_floor_of_one():
    # scores [0, 5, 1] -> strict order B, C, A
    reranker = OpenAICompatibleReranker(rank_fn=lambda _q, texts: [0.0, 5.0, 1.0], text_key="summary")
    candidates = [{"summary": "a"}, {"summary": "b"}, {"summary": "c"}]
    ranked = reranker.rerank(query="q", candidates=candidates, top_n=3)
    assert [r["summary"] for r in ranked] == ["b", "c", "a"]
    # top_n=0 floors to 1 (documented surprising contract)
    one = reranker.rerank(query="q", candidates=candidates, top_n=0)
    assert len(one) == 1 and one[0]["summary"] == "b"


def test_rerank_score_count_mismatch_raises():
    reranker = OpenAICompatibleReranker(rank_fn=lambda _q, _t: [1.0])
    with pytest.raises(ValueError):
        reranker.rerank(query="q", candidates=[{"summary": "a"}, {"summary": "b"}], top_n=2)


def test_resolve_reranker_config_reuses_llm_endpoint_env():
    cfg = resolve_reranker_config(
        {"LLM_BRAIN_LLM_MODEL": "reranker-x", "OPENAI_BASE_URL": "http://127.0.0.1:8930/v1"}
    )
    assert cfg["model"] == "reranker-x"
    assert cfg["base_url"] == "http://127.0.0.1:8930/v1"


def test_build_reranker_with_injected_rank_fn():
    reranker = build_openai_reranker(rank_fn=_by_keyword_rank("z"))
    assert isinstance(reranker, OpenAICompatibleReranker)


def test_query_then_rerank_then_authority_join_compose():
    client = InMemoryQdrantClient()
    adapter = QdrantDoclingMirrorAdapter(
        client=client,
        normalizer=PassthroughMarkdownNormalizer(),
        embedding_provider=HashEmbeddingProvider(size=32),
    )
    hashes = {}
    for body in ("decision about ledger authority", "unrelated cache note"):
        doc = build_rag_ready_document(
            target_profile="derived-memory-items",
            document_kind="approved_memory_card",
            source_namespace="workspace-neurons",
            source_alias="cards/x.md",
            privacy_class="private",
            body=body,
            filename="x.md",
            metadata={"project": "neurons"},
        )
        adapter.submit_document(doc)
        hashes[body] = doc.content_hash

    hits = adapter.query_mirror_candidates("ledger", target_profile="derived-memory-items", limit=5)
    reranked = build_openai_reranker(rank_fn=_by_keyword_rank("ledger")).rerank(
        query="ledger", candidates=hits, top_n=5
    )
    # all authorized in this fake ledger
    ledger = {h: {"privacy_level": "private", "currentness": "current"} for h in hashes.values()}

    class _L:
        def authorize_document_by_content_hash(self, ch, *, filters=None):
            return ledger.get(ch)

    joined = join_mirror_hits_to_authority(reranked, resolver=LedgerContentHashAuthorityResolver(_L()))
    assert joined
    assert joined[0]["content_hash"] == hashes["decision about ledger authority"]
    assert all(h["authority_join_status"] == "resolved" for h in joined)
