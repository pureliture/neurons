import sqlite3

import pytest

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.memory_card import build_memory_candidate, build_memory_card


PROJECT = "workspace-index-advisor"


def _candidate(statement="Keep RetiredIndexBridge core unmodified.", candidate_type="project_decision"):
    return build_memory_candidate(
        candidate_type=candidate_type,
        statement=statement,
        project=PROJECT,
        provider="claude",
        evidence_refs=[{"knowledge_id": "kn_chunk", "content_hash": "sha256:chunk"}],
    )


def test_private_transaction_rolls_back_memory_card_partial_writes(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    card = build_memory_card(_candidate(), approved_by="ddalkak")

    with pytest.raises(sqlite3.IntegrityError):
        with ledger._transaction() as tx:
            tx.upsert_memory_card(card)
            tx.add_memory_card_evidence(
                card["memory_id"],
                [{"knowledge_id": None, "content_hash": "sha256:bad-evidence"}],
            )

    assert ledger.get_memory_card(card["memory_id"]) is None
    assert ledger.get_by_knowledge_id(card["memory_id"]) is None
    assert ledger.list_memory_card_evidence(card["memory_id"]) == []


def test_private_transaction_rejects_content_hash_owned_by_different_knowledge_id(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    card = build_memory_card(_candidate(), approved_by="ddalkak")

    with ledger._transaction() as tx:
        tx.upsert_memory_card(card)

    with pytest.raises(ValueError, match="content hash already belongs"):
        with ledger._transaction() as tx:
            tx._upsert_prepared(
                knowledge_id="mem_conflicting_id",
                content_hash=card["content_hash"],
                provider=card["provider"],
                project=card["project"],
                domain="agent_memory",
                type="memory_card",
                title="Conflicting memory",
                summary="Conflicting memory",
                privacy_level="private",
            )

    assert ledger.get_by_knowledge_id("mem_conflicting_id") is None
    assert ledger.get_by_knowledge_id(card["memory_id"])["content_hash"] == card["content_hash"]


def test_private_transaction_upsert_prepared_is_idempotent_for_same_card(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    card = build_memory_card(_candidate(), approved_by="ddalkak")

    with ledger._transaction() as tx:
        tx.upsert_memory_card(card)
    with ledger._transaction() as tx:
        stored = tx.upsert_memory_card(card)

    assert stored["memory_id"] == card["memory_id"]
    assert stored["content_hash"] == card["content_hash"]
    assert stored["ledger_status"] == "indexed"
