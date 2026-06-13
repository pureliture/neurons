from __future__ import annotations

import json
import stat

from agent_knowledge.rag_ingress.ingress_journal import IngressJournal


def _payload(knowledge_id: str = "kn_1") -> dict:
    return {
        "schemaVersion": "rag_ingress_enqueue.v1",
        "payload": {
            "kind": "redacted_rag_ready_document",
            "document": {
                "body": "# Redacted\n",
                "filename": "ak-conv.md",
                "metadata": {"knowledge_id": knowledge_id, "privacy_class": "private"},
            },
        },
    }


def test_ingress_journal_records_byte_faithful_private_payload(tmp_path):
    journal = IngressJournal(tmp_path / "journal")
    payload = _payload()

    assert journal.record(payload) is True

    entries = list((tmp_path / "journal").glob("entry_*.json"))
    assert len(entries) == 1
    assert stat.S_IMODE((tmp_path / "journal").stat().st_mode) == 0o700
    assert stat.S_IMODE(entries[0].stat().st_mode) == 0o600
    assert json.loads(entries[0].read_text(encoding="utf-8")) == payload
    assert journal.get("kn_1") == payload
    assert journal.count() == 1


def test_ingress_journal_latest_entry_wins_for_same_knowledge_id(tmp_path):
    journal = IngressJournal(tmp_path / "journal")
    first = _payload()
    second = _payload()
    second["payload"]["document"]["body"] = "# Redacted updated\n"

    assert journal.record(first) is True
    assert journal.record(second) is True

    assert journal.get("kn_1") == second
    assert journal.count() == 1


def test_ingress_journal_refuses_unaddressable_payload(tmp_path):
    journal = IngressJournal(tmp_path / "journal")
    payload = _payload("")

    assert journal.record(payload) is False

    assert journal.get("") is None
    assert journal.count() == 0
