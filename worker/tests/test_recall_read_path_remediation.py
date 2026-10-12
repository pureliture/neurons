"""Read-path remediation: Korean lexical matching + context budget degradation.

Two live defects were found by driving the deployed MCP tools (2026-10-12):

1. ``OntologyEpisode.search_text()`` reused the ``ensure_ascii=True`` hash
   serializer, so every non-ASCII character in a card payload was hidden behind
   ``\\uXXXX`` escapes. Korean query terms (>= 3 chars) could therefore never
   match the graph episode filter, and the MCP returned empty results for cards
   whose titles literally contained the query.
2. ``graph_first_resolver`` context mode returned ``response_budget_exceeded``
   instead of degrading to a smaller page with a cursor when per-field
   compression and duplicate-type dropping could not fit the 3072-byte budget —
   making the default (limit=5) context call fail for a full project.
"""
from __future__ import annotations

import json

from agent_knowledge.mcp_payload import tool_result_bytes
from agent_knowledge.llm_brain_core._util import stable_json
from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
from agent_knowledge.llm_brain_core.graphiti_adapter import _matches, _terms
from agent_knowledge.llm_brain_core.hybrid_graph import _match_score
from agent_knowledge.llm_brain_core.models import OntologyEpisode

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def _episode_with_title(title: str) -> OntologyEpisode:
    return OntologyEpisode.from_payload(
        event_id="event:read-path-test",
        entity_type="Decision",
        natural_id="decision:mem_read_path_test",
        payload={"authority_memory_id": "mem_read_path_test", "title": title},
    )


def test_episode_search_text_exposes_raw_korean_for_lexical_matching():
    episode = _episode_with_title("카드 그래프 재투영 완료")
    text = episode.search_text()

    assert "재투영" in text
    assert _matches(text, _terms("카드 그래프 재투영")) is True
    assert _matches(text, _terms("재투영")) is True


def test_search_text_does_not_change_hash_serialization():
    # Identity contracts (hash_payload / episode ids) must keep the escaped
    # serializer; only the lexical search text is allowed to be unicode-raw.
    assert stable_json({"t": "승격"}) == '{"t":"\\uc2b9\\uaca9"}'


def test_hybrid_match_score_counts_korean_terms():
    assert _match_score({"text": "재투영 완료"}, ["재투영"]) == 1


# --------------------------------------------------------------- context budget

def _id_heavy_card(memory_id: str, content_hash: str, card_type: str) -> dict:
    return {
        "memory_id": memory_id,
        "project": "neurons",
        "card_type": card_type,
        "title": f"제목 {card_type}",
        "summary": f"요약 {card_type}",
        "typed_payload": {"decision": "결정"},
        "currentness": "current",
        "content_hash": content_hash,
    }


class _ContextStore:
    def __init__(self, cards):
        self._cards = list(cards)

    def list_context_cards(self, **kwargs):
        assert kwargs["steward_only"] is True
        return list(self._cards)[: kwargs.get("limit", 100)]


def test_context_over_budget_returns_reduced_page_with_cursor_not_error(monkeypatch):
    # A tighter byte budget makes the already-shrunk floor (id + content_hash
    # per card) exceed the wire budget deterministically, exercising the same
    # page-reduction path the live default call hit at limit=5.
    import agent_knowledge.llm_brain_core.graph_first_resolver as resolver_module

    monkeypatch.setattr(resolver_module, "_MAX_BYTES", 2500)
    long_a = "mem_" + "a" * 170
    long_b = "mem_" + "b" * 170
    cards = [
        _id_heavy_card(long_a, HASH_A, "decision"),
        _id_heavy_card(long_b, HASH_B, "status"),
    ]
    resolver = GraphFirstResolver(_ContextStore(cards))

    first = resolver.resolve(project="neurons", mode="context", limit=5)

    assert first.get("error_code") is None
    assert tool_result_bytes(first) <= 2500
    assert [item["id"] for item in first["items"]] == [long_a]
    assert first["has_more"] is True and first["next_cursor"]

    second = resolver.resolve(
        project="neurons", mode="context", limit=5, cursor=first["next_cursor"]
    )
    assert second.get("error_code") is None
    assert tool_result_bytes(second) <= 2500
    assert [item["id"] for item in second["items"]] == [long_b]
    assert second["has_more"] is False
