import json
import sqlite3

import pytest

from agent_knowledge.ledger import Ledger
from agent_knowledge.repository import LedgerMemoryCurationRepository, build_repository_extraction_plan
from agent_knowledge.session_memory.curation import CurationService
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


def test_ledger_memory_curation_repository_approves_candidate_in_one_transaction(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    repository = LedgerMemoryCurationRepository(ledger)
    candidate = ledger.upsert_memory_candidate(_candidate("User prefers Korean answers.", "user_preference"))
    card = build_memory_card(candidate, approved_by="ddalkak")

    stored = repository.approve_candidate(candidate, card, approved_by="ddalkak")

    assert stored["memory_id"] == card["memory_id"]
    assert ledger.get_memory_candidate(candidate["candidate_id"])["approval_state"] == "approved"
    assert ledger.list_memory_card_evidence(card["memory_id"]) == [
        {"memory_id": card["memory_id"], "knowledge_id": "kn_chunk", "content_hash": "sha256:chunk"}
    ]
    assert ledger.list_profile_facts() == [
        {
            "memory_id": card["memory_id"],
            "project": PROJECT,
            "fact_type": "user_preference",
            "content_hash": card["content_hash"],
            "state": "active",
        }
    ]


def test_ledger_memory_curation_repository_rolls_back_partial_approval(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    repository = LedgerMemoryCurationRepository(ledger)
    candidate = ledger.upsert_memory_candidate(_candidate())

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

    bad_candidate = ledger.get_memory_candidate(candidate["candidate_id"])
    card = build_memory_card(bad_candidate, approved_by="ddalkak")

    with pytest.raises(sqlite3.IntegrityError):
        repository.approve_candidate(bad_candidate, card, approved_by="ddalkak")

    assert ledger.get_memory_candidate(candidate["candidate_id"])["approval_state"] == "pending"
    assert ledger.get_memory_card(card["memory_id"]) is None
    assert ledger.get_by_knowledge_id(card["memory_id"]) is None
    assert ledger.list_memory_card_evidence(card["memory_id"]) == []


def test_curation_service_approve_uses_injected_repository(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    candidate = ledger.upsert_memory_candidate(_candidate())
    calls = []

    class RecordingRepository:
        def approve_candidate(self, candidate_arg, card_arg, *, approved_by: str):
            calls.append((candidate_arg["candidate_id"], card_arg["memory_id"], approved_by))
            stored = dict(card_arg)
            stored["ledger_status"] = "recorded-by-repository"
            return stored

    service = CurationService(ledger, repository=RecordingRepository())

    stored = service.approve(candidate["candidate_id"], approved_by="ddalkak")

    assert stored["ledger_status"] == "recorded-by-repository"
    assert calls == [(candidate["candidate_id"], stored["memory_id"], "ddalkak")]
    assert ledger.get_memory_candidate(candidate["candidate_id"])["approval_state"] == "pending"


def test_ledger_memory_curation_repository_requires_transaction_seam():
    repository = LedgerMemoryCurationRepository(object())

    with pytest.raises(RuntimeError, match=r"requires Ledger\._transaction"):
        repository.approve_candidate(_candidate(), {"memory_id": "mem_x"}, approved_by="ddalkak")


def test_ledger_memory_curation_repository_fails_closed_when_card_readback_is_missing():
    calls = []

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return None

        def upsert_memory_card(self, card):
            calls.append(("upsert_memory_card", card["memory_id"]))
            return None

        def add_memory_card_evidence(self, memory_id, evidence_refs):
            calls.append(("add_memory_card_evidence", memory_id, evidence_refs))

    class LedgerDouble:
        def _transaction(self):
            return Transaction()

    repository = LedgerMemoryCurationRepository(LedgerDouble())
    candidate = _candidate()
    card = build_memory_card(candidate, approved_by="ddalkak")

    with pytest.raises(ValueError, match="failed to read back memory card after upsert"):
        repository.approve_candidate(candidate, card, approved_by="ddalkak")

    assert calls == [("upsert_memory_card", card["memory_id"])]


def test_ledger_memory_curation_repository_requires_candidate_identity_before_write():
    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return None

        def upsert_memory_card(self, card):
            raise AssertionError("write should not start when required fields are missing")

    class LedgerDouble:
        def _transaction(self):
            return Transaction()

    repository = LedgerMemoryCurationRepository(LedgerDouble())
    candidate = dict(_candidate())
    candidate["candidate_id"] = None
    card = build_memory_card(_candidate(), approved_by="ddalkak")

    with pytest.raises(ValueError, match="missing required memory curation field: candidate_id"):
        repository.approve_candidate(candidate, card, approved_by="ddalkak")


def test_repository_extraction_plan_reports_first_caller_migration():
    plan = build_repository_extraction_plan()
    first_candidate = plan["first_candidate"]
    next_multi_write_candidate = plan["next_multi_write_candidate"]

    assert plan["mode"] == "first_caller_migration"
    assert first_candidate["activation_state"] == "active_for_curation_approve"
    assert first_candidate["public_import_contract"] is False
    assert first_candidate["protocol_definition_stable"] is False
    assert plan["first_migrated_caller"] == {
        "caller": "CurationService.approve",
        "repository": "LedgerMemoryCurationRepository",
        "rollback_guard": "Ledger._transaction",
    }
    assert next_multi_write_candidate == {
        "caller": "CurationService.supersede",
        "reason": "old_card_demote_plus_new_card_approval_multi_write",
        "status": "not_migrated_in_m2_first_caller",
        "transaction_safe_claimed": False,
    }
