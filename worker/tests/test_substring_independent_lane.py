"""Independent substring candidate lane plus rank-fusion behavior."""

from __future__ import annotations

import pytest

import test_graph_first_resolver as graph_helpers
from agent_knowledge.llm_brain_core.graph_first_resolver import (
    GraphFirstResolver,
    _rank_fuse,
    _rank_fuse_lanes,
)
from agent_knowledge.llm_brain_core.terms import (
    identifier_probe_tokens,
    is_short_ascii_identifier,
    normalize_identifier_text,
)


def _card_dict(memory_id: str, *, content_hash: str | None = None, summary: str = "") -> dict:
    return {
        "memory_id": memory_id,
        "content_hash": content_hash or f"sha256:{memory_id:0>64}",
        "summary": summary or f"summary {memory_id}",
        "title": f"title {memory_id}",
        "semantic_score": 0.9,
    }


def test_identifier_helpers_normalize_hyphens_and_keep_short_tokens() -> None:
    assert normalize_identifier_text("Go-CD  M2_v") == "gocdm2v"
    assert identifier_probe_tokens("GoCD M1 마이그레이션") == ["gocd", "m1", "마이그레이션"]
    assert identifier_probe_tokens("a b") == []
    assert is_short_ascii_identifier("m1") is True
    assert is_short_ascii_identifier("마이") is False
    assert is_short_ascii_identifier("m10") is False


def test_rank_fuse_two_lane_wrapper_matches_generalized_lanes() -> None:
    primary = [_card_dict("a"), _card_dict("b")]
    secondary = [_card_dict("b"), _card_dict("c")]

    assert _rank_fuse(primary, secondary) == _rank_fuse_lanes([primary, secondary])


def test_rank_fuse_three_lanes_sums_scores_and_keeps_first_appearance_order() -> None:
    vector_lane = [_card_dict("a"), _card_dict("b")]
    lexical_lane = [_card_dict("b"), _card_dict("c")]
    chunk_lane = [_card_dict("c")]

    fused = _rank_fuse_lanes([vector_lane, lexical_lane, chunk_lane])

    assert [card["memory_id"] for card in fused] == ["b", "c", "a"]


def test_rank_fuse_dedupes_lexical_duplicate_without_changing_identity() -> None:
    vector_lane = [_card_dict("a")]
    lexical_lane = [_card_dict("a")]

    fused = _rank_fuse_lanes([vector_lane, lexical_lane])

    assert [card["memory_id"] for card in fused] == ["a"]


class _LexicalStore(graph_helpers.Store):
    def __init__(self, *, vector_cards, lexical_cards, lexical_hash_override=None) -> None:
        super().__init__(cards=[*vector_cards, *lexical_cards], unprojected=True)
        self.vector_cards = list(vector_cards)
        self.lexical_cards = list(lexical_cards)
        self.lexical_hash_override = lexical_hash_override
        self.lexical_calls: list[dict] = []

    def hybrid_search(self, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["steward_only"] is True
        return list(self.vector_cards)

    def search_cards_lexical(self, **kwargs):  # type: ignore[no-untyped-def]
        self.lexical_calls.append(dict(kwargs))
        cards = [dict(card) for card in self.lexical_cards]
        if self.lexical_hash_override is not None:
            for card in cards:
                card["content_hash"] = self.lexical_hash_override
        return cards


def _resolve(store, query: str = "GoCD"):
    return GraphFirstResolver(store, graph_helpers.Graph(), lambda _text: [0.1, 0.2]).resolve(
        project="neurons", query=query
    )


def test_lexical_lane_adds_candidate_outside_vector_top_k() -> None:
    vector_card = _card_dict("vector-only")
    lexical_card = _card_dict("lexical-only")
    store = _LexicalStore(vector_cards=[vector_card], lexical_cards=[lexical_card])

    result = _resolve(store)

    ids = [hit["id"] for hit in result["items"]]
    assert "lexical-only" in ids
    assert result["metadata"]["lexical_lane_cards"] == 1
    assert store.lexical_calls[0]["project"] == "neurons"
    assert store.lexical_calls[0]["steward_only"] is True


def test_lexical_lane_drops_candidate_whose_hash_changed_since_candidate_read() -> None:
    lexical_card = _card_dict("lexical-only")
    store = _LexicalStore(
        vector_cards=[_card_dict("vector-only")],
        lexical_cards=[lexical_card],
        lexical_hash_override="sha256:" + "0" * 64,
    )

    result = _resolve(store)

    assert "lexical-only" not in [hit["id"] for hit in result["items"]]
    assert result["metadata"]["lexical_lane_cards"] == 0


def test_lexical_lane_failure_keeps_vector_path() -> None:
    class _BrokenLexicalStore(_LexicalStore):
        def search_cards_lexical(self, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("lexical lane unavailable")

    store = _BrokenLexicalStore(
        vector_cards=[_card_dict("vector-only")], lexical_cards=[]
    )

    result = _resolve(store)

    assert [hit["id"] for hit in result["items"]] == ["vector-only"]
    assert result["metadata"]["lexical_lane_cards"] == 0
