from __future__ import annotations

from agent_knowledge.curation import CurationService
from agent_knowledge.ledger import Ledger
from agent_knowledge.memory_card import build_memory_candidate
from agent_knowledge.session_memory.brain_query import BrainReadModel
from agent_knowledge.session_memory.brain_read_model import (
    LegacyLedgerBrainReadModel,
    build_semantic_recall,
)

PROJECT = "workspace-x"


def _approve_card(service, statement, *, project=PROJECT, ctype="procedural_rule"):
    cand = service.add_candidate(
        build_memory_candidate(
            candidate_type=ctype,
            statement=statement,
            project=project,
            provider="claude",
            evidence_refs=[{"knowledge_id": "kn", "content_hash": "sha256:c"}],
        )
    )
    return service.approve(cand["candidate_id"], approved_by="ddalkak")


def test_adapter_satisfies_protocol_and_reads_cards(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    service = CurationService(ledger)
    card = _approve_card(service, "uv run을 쓴다")
    adapter = LegacyLedgerBrainReadModel(ledger)
    assert isinstance(adapter, BrainReadModel)

    meta = adapter.get_card_meta(card["memory_id"])
    assert meta["memory_id"] == card["memory_id"]
    assert meta["state"] == "active"
    assert meta["card_type"] == "procedural_rule"

    recent = adapter.list_recent_cards(project=PROJECT, limit=5)
    assert [c["memory_id"] for c in recent] == [card["memory_id"]]
    assert adapter.list_accepted_cards(project=PROJECT, limit=5) == []

    assert adapter.get_card_meta("mem_missing") is None


def test_adapter_counts_active_cards_by_project(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    service = CurationService(ledger)
    _approve_card(service, "s1")
    _approve_card(service, "s2")
    _approve_card(service, "other", project="workspace-y")
    adapter = LegacyLedgerBrainReadModel(ledger)
    assert adapter.list_project_card_counts() == [
        ("workspace-x", 2),
        ("workspace-y", 1),
    ]


def test_build_semantic_recall_binds_recall_pipeline(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    captured = {}

    def fake_recall(*, ragflow, store, memory_id, query, brain_id=""):
        captured.update(memory_id=memory_id, query=query, brain_id=brain_id)
        return [{"session_tag": "mem:x"}]

    import agent_knowledge.session_memory.brain_read_model as mod

    monkeypatch.setattr(mod, "recall_active_native_memory", fake_recall)
    semantic = build_semantic_recall(ledger=ledger, ragflow=object(), memory_id="mem_main")
    hits = semantic("질문", "/project/p")
    assert hits == [{"session_tag": "mem:x"}]
    assert captured == {"memory_id": "mem_main", "query": "질문", "brain_id": "/project/p"}
