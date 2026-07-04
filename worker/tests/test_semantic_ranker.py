from __future__ import annotations

from agent_knowledge.session_memory.semantic_ranker import EmbeddingSemanticRanker


class _FakeEmbeddingProvider:
    size = 2

    def __init__(self):
        self.calls = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if "target" in text:
            return [1.0, 0.0]
        return [0.0, 1.0]


def test_embedding_semantic_ranker_adds_vector_scores_and_caches_card_vectors():
    provider = _FakeEmbeddingProvider()
    ranker = EmbeddingSemanticRanker(embedding_provider=provider)
    cards = [
        {"memory_id": "mem_noise", "summary": "noise"},
        {"memory_id": "mem_target", "summary": "target"},
    ]

    first = ranker(query="target", cards=cards, limit=2)
    second = ranker(query="target", cards=cards, limit=2)

    assert [card["memory_id"] for card in first] == ["mem_target", "mem_noise"]
    assert first[0]["_semantic_score"] > first[1]["_semantic_score"]
    assert [card["memory_id"] for card in second] == ["mem_target", "mem_noise"]
    assert provider.calls.count("target") == 1
    assert provider.calls.count("noise") == 1
