"""The public brain read model must follow the PostgreSQL steward authority."""
from __future__ import annotations

import hashlib

from agent_knowledge.knowledge_search_service import (
    DisabledRetiredIndexBridgeClient,
    KnowledgeSearchService,
)
from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.brain_steward import BrainStewardService


SPAN = {
    "card_type": "preference", "project": "steward-wiring", "provider": "hermes",
    "scope": "project", "title": "PostgreSQL authority",
    "redacted_summary": "Use PostgreSQL for approved cards",
    "typed_payload": {
        "preference": "Use PostgreSQL for approved cards", "explicitness": "explicit",
        "repeated_count": 1, "confirmation_status": "confirmed", "applies_to": "card_storage",
    },
    "source_ref": {"source_id": "wiring-source"},
    "span_ref": {"span_id": "wiring-span"},
    "content_hash": "sha256:" + hashlib.sha256(b"steward-wiring").hexdigest(),
    "confidence": 0.9, "confidence_basis": "human confirmation",
}


def test_public_read_uses_pg_steward_not_sqlite(tmp_path, isolated_pg_store):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    store = isolated_pg_store
    service = KnowledgeSearchService(
        ledger=ledger,
        retired_index_bridge=DisabledRetiredIndexBridgeClient(), dataset_ids=[],
        pgvector_store=store,
    )
    steward = BrainStewardService(ledger, pgvector_store=store, allow_restricted=True)
    memory_id = steward.candidate_create(source_span=SPAN)["memory_id"]
    assert service._brain_card_cache.list_accepted_cards(project="steward-wiring", limit=10) == []
    assert service.brain_steward().review_queue_list(project="steward-wiring")["count"] == 1
    steward.candidate_approve(candidate_memory_id=memory_id, approved_by="operator", decision_id="wiring-decision")
    service.invalidate_brain_card_cache()
    cached = service._brain_card_cache.list_accepted_cards(project="steward-wiring", limit=10)
    assert [card["memory_id"] for card in cached] == [memory_id]
    result = service.brain_query(brain_id="/project/steward-wiring", query="PostgreSQL approved cards")
    assert any(card["memory_id"] == memory_id for card in result["accepted"])
    assert ledger.get_llm_brain_memory_card(memory_id) is None
    assert service.brain_resolve(query="steward-wiring")["candidates"][0]["brain_id"] == "/project/steward-wiring"
