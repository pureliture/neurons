import json
import sqlite3

import pytest

from agent_knowledge.curation import CurationService
from agent_knowledge.ledger import Ledger
from agent_knowledge.memory_card import build_memory_candidate, build_memory_card


PROJECT = "workspace-ragflow-advisor"


def _candidate(statement="Keep RAGFlow core unmodified.", candidate_type="project_decision"):
    return build_memory_candidate(
        candidate_type=candidate_type,
        statement=statement,
        project=PROJECT,
        provider="claude",
        evidence_refs=[{"knowledge_id": "kn_chunk", "content_hash": "sha256:chunk"}],
    )


def test_curation_approves_candidate_into_auditable_memory_card(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    service = CurationService(ledger)
    candidate = service.add_candidate(_candidate())

    card = service.approve(candidate["candidate_id"], approved_by="ddalkak")

    stored_candidate = ledger.get_memory_candidate(candidate["candidate_id"])
    stored_card = ledger.get_memory_card(card["memory_id"])
    item = ledger.get_by_knowledge_id(card["memory_id"])

    assert stored_candidate["approval_state"] == "approved"
    assert stored_card["state"] == "active"
    assert card["ragflow_dataset_id"] == "local-approved-memory-cards"
    assert card["ragflow_document_id"] == f"memdoc_{card['memory_id']}"
    assert card["ledger_status"] == "indexed"
    assert item["type"] == "memory_card"
    assert item["status"] == "indexed"
    assert ledger.list_memory_card_evidence(card["memory_id"]) == [
        {"memory_id": card["memory_id"], "knowledge_id": "kn_chunk", "content_hash": "sha256:chunk"}
    ]
    assert "raw transcript" not in json.dumps(stored_card, sort_keys=True).lower()


def test_curation_approve_rolls_back_partial_card_state_when_evidence_write_fails(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    service = CurationService(ledger)
    candidate = service.add_candidate(_candidate())

    with ledger._connect() as connection:
        connection.execute(
            """
            UPDATE memory_candidates
            SET evidence_refs_json = ?
            WHERE candidate_id = ?
            """,
            (
                json.dumps([{"knowledge_id": None, "content_hash": "sha256:bad-evidence"}]),
                candidate["candidate_id"],
            ),
        )

    with pytest.raises(sqlite3.IntegrityError):
        service.approve(candidate["candidate_id"], approved_by="ddalkak")

    stored_candidate = ledger.get_memory_candidate(candidate["candidate_id"])
    memory_id = build_memory_card(candidate, approved_by="ddalkak")["memory_id"]

    assert stored_candidate["approval_state"] == "pending"
    assert ledger.get_memory_card(memory_id) is None
    assert ledger.get_by_knowledge_id(memory_id) is None
    assert ledger.list_memory_card_evidence(memory_id) == []


def test_curation_rejects_disables_and_supersedes_cards(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    service = CurationService(ledger)
    rejected = service.add_candidate(_candidate("Old unneeded fact."))
    old = service.add_candidate(_candidate("Use the old scheduler policy."))
    new = service.add_candidate(_candidate("Use the approval-gated scheduler policy."))

    service.reject(rejected["candidate_id"], reviewed_by="ddalkak", reason="not durable")
    old_card = service.approve(old["candidate_id"], approved_by="ddalkak")
    new_card = service.supersede(old_card["memory_id"], new["candidate_id"], approved_by="ddalkak", reason="superseded")
    disabled = service.disable(old_card["memory_id"], reviewed_by="ddalkak", reason="superseded")

    assert ledger.get_memory_candidate(rejected["candidate_id"])["approval_state"] == "rejected"
    assert disabled["state"] == "disabled"
    assert ledger.get_memory_card(new_card["memory_id"])["supersedes"] == old_card["memory_id"]
    assert ledger.get_by_knowledge_id(old_card["memory_id"])["authorization_status"] == "disabled"


def test_profile_fact_is_visible_but_never_auto_approved(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    service = CurationService(ledger)
    candidate = service.add_candidate(_candidate("User prefers Korean answers.", "user_preference"))

    assert candidate["requires_manual_approval"] is True
    assert ledger.list_profile_facts() == []

    card = service.approve(candidate["candidate_id"], approved_by="ddalkak")

    assert ledger.list_profile_facts() == [
        {
            "memory_id": card["memory_id"],
            "project": PROJECT,
            "fact_type": "user_preference",
            "content_hash": card["content_hash"],
            "state": "active",
        }
    ]


def test_approve_with_supersedes_is_rejected_in_favor_of_supersede_transition(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    service = CurationService(ledger)
    candidate = service.add_candidate(_candidate("Use new policy."))

    with pytest.raises(ValueError, match="use supersede"):
        service.approve(candidate["candidate_id"], approved_by="ddalkak", supersedes="mem_old")


def test_supersede_rejects_unknown_candidate_before_disabling_old_card(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    service = CurationService(ledger)
    old = service.approve(service.add_candidate(_candidate("Use stable policy."))["candidate_id"], approved_by="ddalkak")

    with pytest.raises(ValueError, match="unknown memory candidate"):
        service.supersede(old["memory_id"], "missing_candidate", approved_by="ddalkak", reason="missing")

    assert ledger.get_memory_card(old["memory_id"])["state"] == "active"
    assert ledger.get_by_knowledge_id(old["memory_id"])["authorization_status"] == "active"


def test_supersede_rejects_rejected_candidate_before_disabling_old_card(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    service = CurationService(ledger)
    old = service.approve(service.add_candidate(_candidate("Use stable policy."))["candidate_id"], approved_by="ddalkak")
    rejected = service.add_candidate(_candidate("Do not use rejected policy."))
    service.reject(rejected["candidate_id"], reviewed_by="ddalkak", reason="bad candidate")

    with pytest.raises(ValueError, match="pending or approved candidates"):
        service.supersede(old["memory_id"], rejected["candidate_id"], approved_by="ddalkak", reason="bad replacement")

    assert ledger.get_memory_card(old["memory_id"])["state"] == "active"
    assert ledger.get_by_knowledge_id(old["memory_id"])["authorization_status"] == "active"


def test_superseding_profile_fact_marks_old_profile_fact_superseded(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    service = CurationService(ledger)
    old = service.approve(
        service.add_candidate(_candidate("User prefers old context policy.", "user_preference"))["candidate_id"],
        approved_by="ddalkak",
    )
    new_candidate = service.add_candidate(_candidate("User prefers new context policy.", "user_preference"))

    new_card = service.supersede(
        old["memory_id"], new_candidate["candidate_id"], approved_by="ddalkak", reason="updated preference"
    )

    profile_facts = {fact["memory_id"]: fact for fact in ledger.list_profile_facts()}
    assert profile_facts[old["memory_id"]]["state"] == "superseded"
    assert profile_facts[new_card["memory_id"]]["state"] == "active"


def test_curation_lists_candidates_and_supersedes_as_auditable_transition(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    service = CurationService(ledger)
    old = service.approve(service.add_candidate(_candidate("Use old policy."))["candidate_id"], approved_by="ddalkak")
    new_candidate = service.add_candidate(_candidate("Use new policy."))

    new_card = service.supersede(
        old["memory_id"],
        new_candidate["candidate_id"],
        approved_by="ddalkak",
        reason="policy updated",
    )

    assert [candidate["candidate_id"] for candidate in ledger.list_memory_candidates()] == [
        old["candidate_id"],
        new_candidate["candidate_id"],
    ]
    assert ledger.get_memory_card(old["memory_id"])["state"] == "superseded"
    assert ledger.get_by_knowledge_id(old["memory_id"])["authorization_status"] == "disabled"
    assert new_card["supersedes"] == old["memory_id"]


def test_get_memory_card_state_returns_state_or_none(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    service = CurationService(ledger)
    old = service.add_candidate(_candidate("Use the old scheduler policy."))
    new = service.add_candidate(_candidate("Use the approval-gated scheduler policy."))
    old_card = service.approve(old["candidate_id"], approved_by="ddalkak")

    assert ledger.get_memory_card_state(old_card["memory_id"]) == "active"

    new_card = service.supersede(
        old_card["memory_id"], new["candidate_id"], approved_by="ddalkak", reason="superseded"
    )
    assert ledger.get_memory_card_state(old_card["memory_id"]) == "superseded"
    assert ledger.get_memory_card_state(new_card["memory_id"]) == "active"
    assert ledger.get_memory_card_state("mem_missing") is None
