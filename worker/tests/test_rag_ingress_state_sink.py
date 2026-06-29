from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_knowledge.rag_ingress.backfill import state_db_counts
from agent_knowledge.rag_ingress.rag_ready_document import (
    build_ingress_enqueue_payload,
    build_rag_ready_document,
)
from agent_knowledge.rag_ingress.state_db import RAGIngressStateDB
from agent_knowledge.rag_ingress.state_sink import (
    StateDBIngressSink,
    authority_shadow_report,
)


def _state_db(tmp_path) -> RAGIngressStateDB:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    return RAGIngressStateDB(private / "state.sqlite")


def _payload(*, body: str = "# chunk\n\nhello", key: str | None = None) -> dict:
    document = build_rag_ready_document(
        target_profile="transcript-memory",
        document_kind="conversation_chunk",
        source_namespace="codex",
        source_alias="workspace-index-advisor/session",
        privacy_class="private",
        body=body,
        filename="conversation.md",
        metadata={"privacy_class": "private", "project": "workspace-index-advisor"},
    )
    payload = build_ingress_enqueue_payload(
        document,
        source={"provider": "codex", "source_alias": "workspace-index-advisor/session"},
    )
    if key is not None:
        payload["idempotencyKey"] = key
    return payload


def test_state_db_sink_accepts_payload_without_http(tmp_path):
    state_db = _state_db(tmp_path)
    sink = StateDBIngressSink(state_db=state_db)

    result = sink.accept_payload(_payload())

    assert result["status"] == "queued"
    assert result["job_id"].startswith("job_")
    assert state_db_counts(state_db)["commands"] == 1
    assert state_db_counts(state_db)["delivery_jobs"] == 1
    assert state_db.get_delivery_payload(_payload()["idempotencyKey"]) == _payload()
    assert sink.dual_write_fail_count == 0
    assert sink.dual_write_conflict_count == 0


def test_state_db_sink_conflict_rejects_without_overwrite(tmp_path):
    state_db = _state_db(tmp_path)
    sink = StateDBIngressSink(state_db=state_db)
    original = _payload(body="original", key="same-key")
    mutated = _payload(body="mutated", key="same-key")

    sink.accept_payload(original)

    with pytest.raises(RuntimeError, match="conflict"):
        sink.accept_payload(mutated)

    assert sink.dual_write_conflict_count == 1
    assert sink.dual_write_fail_count == 0
    assert state_db_counts(state_db)["commands"] == 1
    assert state_db.get_delivery_payload("same-key") == original


def test_state_db_sink_journal_failure_is_reported_not_blocking(tmp_path):
    class RefusingJournal:
        def record(self, _payload):
            return False

    state_db = _state_db(tmp_path)
    sink = StateDBIngressSink(state_db=state_db, journal=RefusingJournal())

    assert sink.accept_payload(_payload())["status"] == "queued"
    assert authority_shadow_report(sink) == {
        "journal_fail_count": 1,
        "dual_write_fail_count": 0,
        "dual_write_conflict_count": 0,
    }
    assert state_db_counts(state_db)["commands"] == 1


def test_state_db_sink_enqueue_document_builds_legacy_compatible_payload(tmp_path):
    state_db = _state_db(tmp_path)
    sink = StateDBIngressSink(state_db=state_db)
    packed = SimpleNamespace(
        filename="packed.md",
        body="# packed\n\nbody",
        metadata={"privacy_class": "private", "turn_count": 2},
    )

    result = sink.enqueue_document(
        source={"provider": "codex"},
        packed=packed,
        content_hash="sha256:abc",
        target_profile="transcript-memory",
        kind="conversation_chunk",
        idempotency_key="packed-key",
    )

    assert result["status"] == "queued"
    payload = state_db.get_delivery_payload("packed-key")
    assert payload["payload"]["document"]["filename"] == "packed.md"
    assert payload["payload"]["document"]["metadata"]["turn_count"] == "2"


def test_authority_shadow_report_none_without_injection(tmp_path):
    state_db = _state_db(tmp_path)
    sink = StateDBIngressSink(state_db=state_db)
    sink.journal = None
    sink.dual_write_state_db = None

    assert authority_shadow_report(sink) is None
    assert authority_shadow_report(None) is None
