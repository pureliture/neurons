"""List/query items must show record age; context items stay compact."""
from __future__ import annotations

from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
from agent_knowledge.mcp_payload import tool_result_bytes

HASH = "sha256:" + "a" * 64
CREATED = "2026-07-04T09:32:40Z"


def _card(card_type: str = "decision", **extra) -> dict:
    card = {
        "memory_id": "mem_a", "project": "neurons", "card_type": card_type,
        "title": "Card title", "summary": "Card summary",
        "typed_payload": {"decision": "Keep the boundary"} if card_type == "decision" else {"task_state": "in_progress", "next_action": "Run the regression"},
        "currentness": "current", "content_hash": HASH,
        "lifecycle_state": "human_accepted", "authorization_status": "active",
        "created_at": CREATED,
    }
    card.update(extra)
    return card


class Store:
    def __init__(self, cards):
        self.cards = cards

    def list_context_cards(self, **_kwargs):
        return [dict(c) for c in self.cards]

    def list_authorized_cards(self, **_kwargs):
        return [dict(c) for c in self.cards]

    def graph_projection_health(self, *_a, **_k):
        return {"unprojected": False, "projection_lag_ms": None}


def test_list_item_exposes_created_at_so_old_records_are_distinguishable():
    result = GraphFirstResolver(Store([_card()])).resolve(project="neurons", mode="list", limit=5)
    assert result.get("error_code") is None, result
    assert result["items"][0]["created_at"] == CREATED
    assert result["items"][0]["content_hash"] == HASH


def test_context_section_item_stays_compact_and_budget_is_unchanged():
    # Context items for section types are identity-only by contract: adding a
    # timestamp there would spend startup-pack bytes on provenance the reader
    # gets from list mode instead.
    result = GraphFirstResolver(Store([_card()])).resolve(project="neurons", mode="context", limit=5)
    assert result.get("error_code") is None, result
    assert set(result["items"][0]) == {"id", "card_type", "currentness"}
    assert tool_result_bytes(result) <= 3072


def test_context_non_section_item_omits_created_at():
    result = GraphFirstResolver(Store([_card("evidence")])).resolve(project="neurons", mode="context", limit=5)
    assert result.get("error_code") is None, result
    assert "created_at" not in result["items"][0]
    assert "content_hash" in result["items"][0]


def test_card_without_created_at_does_not_emit_empty_field():
    card = _card()
    del card["created_at"]
    result = GraphFirstResolver(Store([card])).resolve(project="neurons", mode="list", limit=5)
    assert "created_at" not in result["items"][0]
