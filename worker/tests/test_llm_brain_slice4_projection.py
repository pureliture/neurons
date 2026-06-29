from __future__ import annotations

import pytest

from agent_knowledge.session_memory.memory_miner import build_memory_card_candidate_from_source_span
from agent_knowledge.session_memory.memory_promotion import human_approve_memory_card_candidate
from agent_knowledge.session_memory.index_projection import (
    RetiredIndexBridgeMemoryCardProjectionClient,
    build_projection_job,
    build_index_projection_payload,
    enqueue_projection_jobs,
    execute_projection_job,
    projection_idempotency_key,
    projection_lag_marker,
    render_projection_document,
)


PROJECT = "workspace-index-advisor"


def _source_span(**overrides):
    span = {
        "source_ref": {"source_id": "src_projection"},
        "span_ref": {"span_id": "span_projection"},
        "content_hash": "sha256:projection",
        "brain_id": f"/project/{PROJECT}",
        "card_type": "decision",
        "scope": "project",
        "project": PROJECT,
        "provider": "codex",
        "title": "Projection policy",
        "redacted_summary": "RetiredIndexBridge is a searchable mirror, not canonical state.",
        "typed_payload": {
            "decision": "Project accepted MemoryCards to RetiredIndexBridge as a mirror.",
            "rationale": "Local ledger remains canonical during projection lag.",
            "alternatives": ["Make RetiredIndexBridge canonical"],
            "consequence": "Projection failures become diagnostics, not state changes.",
            "authority_ref": "session-memory-decision-4",
        },
        "confidence": 0.86,
        "confidence_basis": "human-approved design decision",
    }
    span.update(overrides)
    return span


def _candidate():
    return build_memory_card_candidate_from_source_span(_source_span(), refresh_watermark="w4")


def _accepted_card():
    return human_approve_memory_card_candidate(
        _candidate(),
        approved_by="ddalkak",
        decision_id="decision_4",
        timestamp="2026-06-13T00:00:00+00:00",
    )["accepted_card"]


def _approval_record(job, *, dataset_id=""):
    record = {
        "approved": True,
        "operation": "index_projection_write",
        "idempotency_key": job["idempotency_key"],
        "dry_run_status": "dry_run",
        "approved_by": "ddalkak",
    }
    if dataset_id:
        record["dataset_id"] = dataset_id
        record["allowed_dataset_ids"] = [dataset_id]
    return record


def test_projection_job_requires_accepted_memory_card():
    with pytest.raises(ValueError, match="only accepted"):
        build_projection_job(_candidate())

    job = build_projection_job(_accepted_card())
    assert job["operation"] == "index_upsert"
    assert job["status"] == "queued"
    assert job["canonical_state_changed"] is False


def test_projection_payload_is_idempotent_and_redacted_metadata_only():
    card = _accepted_card()
    first_key = projection_idempotency_key(card)
    second_key = projection_idempotency_key(card)
    payload = build_index_projection_payload(card)

    assert first_key == second_key
    assert payload["memory_id"] == card["memory_id"]
    assert payload["metadata"]["approval_state"] == "approved"
    assert payload["metadata"]["source_ref_count"] == 1
    assert "source_refs" not in payload["metadata"]
    assert "source_ref" not in render_projection_document(payload)


def test_projection_queue_dedupes_jobs_by_idempotency_key():
    card = _accepted_card()
    plan = enqueue_projection_jobs([card, card, _candidate()])

    assert plan["write_performed"] is False
    assert plan["job_count"] == 1
    assert plan["skipped"] == [
        {"index": 1, "reason": "duplicate_idempotency_key"},
        {"index": 2, "reason": "only accepted MemoryCards can be projected"},
    ]


def test_projection_execute_dry_run_does_not_call_client():
    class ExplodingClient:
        def upsert_memory_card(self, payload, *, idempotency_key):
            raise AssertionError("dry-run must not call client")

    result = execute_projection_job(build_projection_job(_accepted_card()), client=ExplodingClient())

    assert result["status"] == "dry_run"
    assert result["write_performed"] is False
    assert result["canonical_state_changed"] is False
    assert result["projection_state"]["status"] == "projection_stale"


def test_projection_execute_with_fake_client_records_success_without_canonical_change():
    calls = {}

    class FakeClient:
        def upsert_memory_card(self, payload, *, idempotency_key):
            calls["payload"] = payload
            calls["idempotency_key"] = idempotency_key
            return {"document_id": "doc_projection"}

    job = build_projection_job(_accepted_card())
    result = execute_projection_job(
        {**job, "approval_record": _approval_record(job)},
        client=FakeClient(),
        allow_write=True,
    )

    assert result["status"] == "projected"
    assert result["write_performed"] is True
    assert result["canonical_state_changed"] is False
    assert calls["idempotency_key"] == job["idempotency_key"]


def test_projection_execute_write_requires_matching_approval_record():
    class ExplodingClient:
        def upsert_memory_card(self, payload, *, idempotency_key):
            raise AssertionError("missing approval must not call client")

    job = build_projection_job(_accepted_card())
    result = execute_projection_job(job, client=ExplodingClient(), allow_write=True)

    assert result["status"] == "blocked_approval_required"
    assert result["write_performed"] is False
    assert result["projection_state"]["reason"] == "missing_projection_approval_record"


def test_index_projection_adapter_uses_deterministic_document_without_delete_disable():
    calls = {"uploads": 0, "metadata": [], "parse": [], "delete": 0, "disable": 0}

    class FakeRetiredIndexBridge:
        def __init__(self):
            self.documents = []

        def list_documents(self, dataset_id, *, keywords="", page=1, page_size=20):
            return [
                document
                for document in self.documents
                if not keywords or document.get("name") == keywords
            ]

        def upload_document(self, dataset_id, content, *, filename):
            calls["uploads"] += 1
            assert "RetiredIndexBridge is a searchable mirror" in content
            document_id = "doc_projection"
            self.documents.append({"id": document_id, "name": filename})
            return {"document_id": document_id, "run": "UNSTART"}

        def update_metadata(self, dataset_id, document_id, metadata):
            calls["metadata"].append((dataset_id, document_id, metadata))

        def request_parse(self, dataset_id, document_ids):
            calls["parse"].append((dataset_id, document_ids))

        def delete_documents(self, dataset_id, document_ids):
            calls["delete"] += 1

        def disable_document(self, dataset_id, document_id):
            calls["disable"] += 1

    job = build_projection_job(_accepted_card())
    client = RetiredIndexBridgeMemoryCardProjectionClient(retired_index_bridge=FakeRetiredIndexBridge(), dataset_id="ds_llm_brain")

    first = client.upsert_memory_card(job["payload"], idempotency_key=job["idempotency_key"])
    second = client.upsert_memory_card(job["payload"], idempotency_key=job["idempotency_key"])

    assert first["status"] == "projected"
    assert second["status"] == "already_projected"
    assert calls["uploads"] == 1
    assert calls["metadata"][0][2]["idempotency_key"] == job["idempotency_key"]
    assert calls["parse"] == [("ds_llm_brain", ["doc_projection"])]
    assert calls["delete"] == 0
    assert calls["disable"] == 0


def test_projection_execute_failure_is_diagnostic_not_memory_rejection():
    class BrokenClient:
        def upsert_memory_card(self, payload, *, idempotency_key):
            raise RuntimeError("mirror down")

    job = build_projection_job(_accepted_card())
    result = execute_projection_job(
        {**job, "approval_record": _approval_record(job)},
        client=BrokenClient(),
        allow_write=True,
    )

    assert result["status"] == "write_failed"
    assert result["write_performed"] is False
    assert result["canonical_state_changed"] is False
    assert result["projection_state"]["status"] == "projection_stale"
    assert result["projection_state"]["reason"] == "RuntimeError"


def test_projection_lag_marker_keeps_local_ledger_as_winner():
    marker = projection_lag_marker(_accepted_card(), reason="mirror_hash_mismatch")

    assert marker["conflict_type"] == "projection_stale"
    assert marker["winner"] == "local_ledger"
    assert marker["canonical_state_changed"] is False
