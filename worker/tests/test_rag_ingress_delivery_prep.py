"""M8.1 delivery prep: payload availability gate + real backend + drain seam.

Covers:
- Slice A: delivery_payloads persistence (record/get/conflict/fill-gap) and the
  fail-closed triple-hash recovery gate.
- Slice B: RagflowDeliveryBackend submit/uncertain/retryable/payload-unavailable
  mapping over a fake IndexBackendAdapter (no network anywhere).
- Slice C: drain_pending_deliveries dry-run-first contract + live counters via a
  fake DeliveryBackend.
"""

import hashlib
import json
import os

import pytest

from agent_knowledge.rag_ingress.backfill_apply import apply_backfill_to_state_db
from agent_knowledge.rag_ingress.delivery_backend import (
    PAYLOAD_HASH_MISMATCH,
    PAYLOAD_MISSING,
    PAYLOAD_OK,
    RagflowDeliveryBackend,
    resolve_delivery_payload,
)
from agent_knowledge.rag_ingress.delivery_drain import drain_pending_deliveries
from agent_knowledge.rag_ingress.delivery_executor import (
    DeliveryBackendEvidence,
    DeliveryExecutor,
    DeliveryOutcomeUncertain,
)
from agent_knowledge.rag_ingress.index_backend import (
    BackendDocumentHandle,
    BackendStatusDetail,
    BackendSubmitResult,
    IndexStatus,
)
from agent_knowledge.rag_ingress.server_runtime import job_id_for_payload
from agent_knowledge.rag_ingress.state_db import RAGIngressStateDB


def _payload(*, key="k1", body="hello delivery body"):
    return {
        "schemaVersion": "rag_ingress_enqueue.v1",
        "source": {"host": "mac_mini", "producer": "test", "provider": "codex", "project": "p"},
        "payload": {
            "kind": "redacted_rag_ready_document",
            "redactionVersion": "redaction.v2",
            "document": {
                "filename": "doc.md",
                "contentType": "text/markdown",
                "body": body,
                "metadata": {"type": "conversation_chunk", "knowledge_id": f"kn_{key}"},
            },
        },
        "contentHash": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "targetProfile": "ragflow-transcript-memory",
        "kind": "conversation_chunk",
        "idempotencyKey": key,
    }


def _state_db(tmp_path):
    priv = tmp_path / "private"
    priv.mkdir(parents=True, exist_ok=True)
    os.chmod(priv, 0o700)
    return RAGIngressStateDB(priv / "state.sqlite")


def _seed(state_db, *payloads):
    result = apply_backfill_to_state_db(state_db=state_db, payloads=list(payloads), dry_run=False)
    assert result["conflict_count"] == 0
    return result


# ---- Slice A: delivery_payloads persistence ----

def test_record_delivery_payload_outcomes(tmp_path):
    state_db = _state_db(tmp_path)
    payload = _payload(key="ka", body="body a")

    assert state_db.record_delivery_payload(payload) == "recorded"
    assert state_db.record_delivery_payload(payload) == "already_present"
    mutated = _payload(key="ka", body="body a CHANGED")
    assert state_db.record_delivery_payload(mutated) == "conflict"
    # fail-closed: the stored payload was not overwritten by the conflict
    assert state_db.get_delivery_payload("ka") == payload


def test_backfill_apply_persists_delivery_payload(tmp_path):
    state_db = _state_db(tmp_path)
    payload = _payload(key="kb", body="body b")
    _seed(state_db, payload)

    assert state_db.get_delivery_payload("kb") == payload


def test_backfill_rerun_fills_payload_gap_for_old_candidates(tmp_path):
    state_db = _state_db(tmp_path)
    payload = _payload(key="kc", body="body c")
    _seed(state_db, payload)
    # simulate a candidate seeded before delivery_payloads existed
    with state_db.connect() as connection:
        connection.execute("DELETE FROM delivery_payloads WHERE idempotency_key = ?", ("kc",))
    assert state_db.get_delivery_payload("kc") is None

    rerun = apply_backfill_to_state_db(state_db=state_db, payloads=[payload], dry_run=False)

    assert rerun["already_present_count"] == 1
    assert state_db.get_delivery_payload("kc") == payload


def test_resolve_delivery_payload_gate(tmp_path):
    state_db = _state_db(tmp_path)
    payload = _payload(key="kd", body="body d")
    _seed(state_db, payload)

    resolved, gate = resolve_delivery_payload(
        state_db, idempotency_key="kd", expected_payload_hash=payload["contentHash"]
    )
    assert gate == PAYLOAD_OK
    assert resolved == payload

    _missing, gate = resolve_delivery_payload(
        state_db, idempotency_key="unknown", expected_payload_hash=payload["contentHash"]
    )
    assert gate == PAYLOAD_MISSING

    # delivery job expecting a different hash must not receive this payload
    _mismatch, gate = resolve_delivery_payload(
        state_db, idempotency_key="kd", expected_payload_hash="sha256:" + "0" * 64
    )
    assert gate == PAYLOAD_HASH_MISMATCH


def test_resolve_delivery_payload_detects_tampered_body(tmp_path):
    state_db = _state_db(tmp_path)
    payload = _payload(key="ke", body="body e")
    _seed(state_db, payload)
    # tamper the stored body while keeping the stored contentHash column intact
    tampered = json.loads(json.dumps(payload))
    tampered["payload"]["document"]["body"] = "tampered body"
    with state_db.connect() as connection:
        connection.execute(
            "UPDATE delivery_payloads SET payload_json = ? WHERE idempotency_key = ?",
            (json.dumps(tampered, sort_keys=True), "ke"),
        )

    _resolved, gate = resolve_delivery_payload(
        state_db, idempotency_key="ke", expected_payload_hash=payload["contentHash"]
    )
    assert gate == PAYLOAD_HASH_MISMATCH


# ---- Slice B: RagflowDeliveryBackend ----

class _FakeIndexAdapter:
    def __init__(self, *, result=None, error=None, detail=None, natural_key_handle=None):
        self._result = result
        self._error = error
        self._detail = detail
        self._natural_key_handle = natural_key_handle
        self.submitted = []
        self.natural_key_calls = []

    def submit_document(self, document, *, on_step_complete=None):
        if self._error is not None:
            raise self._error
        self.submitted.append(document)
        return self._result

    def document_status(self, handle):
        return self._detail.status

    def document_status_detail(self, handle):
        return self._detail

    def find_by_natural_key(self, *, target_profile, idempotency_key, payload_hash):
        self.natural_key_calls.append((target_profile, idempotency_key, payload_hash))
        return self._natural_key_handle


def _job_view(state_db, key):
    from agent_knowledge.rag_ingress.delivery_executor import DeliveryJobView

    row = state_db.get_row("delivery_jobs", "idempotency_key", key)
    return DeliveryJobView.from_row(row)


def test_backend_submit_success_returns_succeeded_evidence(tmp_path):
    state_db = _state_db(tmp_path)
    payload = _payload(key="kf", body="body f")
    _seed(state_db, payload)
    adapter = _FakeIndexAdapter(
        result=BackendSubmitResult(dataset_ref="ds_1", document_ref="doc_1", status=IndexStatus.PENDING)
    )
    backend = RagflowDeliveryBackend(state_db=state_db, index_backend=adapter)

    evidence = backend.submit(_job_view(state_db, "kf"))

    assert evidence.status == "succeeded"
    assert evidence.dataset_ref == "ds_1"
    assert evidence.document_ref == "doc_1"
    # the submitted document is the recovered redacted payload, byte-faithful body
    assert adapter.submitted[0].body == "body f"
    assert adapter.submitted[0].idempotency_key == "kf"
    assert adapter.submitted[0].metadata["content_hash"] == payload["contentHash"]


def test_backend_submit_reuses_existing_natural_key_without_upload(tmp_path):
    state_db = _state_db(tmp_path)
    payload = _payload(key="k_existing", body="body existing")
    _seed(state_db, payload)
    adapter = _FakeIndexAdapter(
        result=BackendSubmitResult(dataset_ref="ds_new", document_ref="doc_new", status=IndexStatus.PENDING),
        natural_key_handle=BackendDocumentHandle(dataset_ref="ds_existing", document_ref="doc_existing"),
        detail=BackendStatusDetail(status=IndexStatus.INDEXED, progress=1.0, backend_raw_status="DONE"),
    )
    backend = RagflowDeliveryBackend(state_db=state_db, index_backend=adapter)

    evidence = backend.submit(_job_view(state_db, "k_existing"))

    assert evidence.status == "succeeded"
    assert evidence.dataset_ref == "ds_existing"
    assert evidence.document_ref == "doc_existing"
    assert evidence.run == "DONE"
    assert adapter.submitted == []
    assert adapter.natural_key_calls == [
        ("ragflow-transcript-memory", "k_existing", payload["contentHash"])
    ]


def test_backend_submit_exception_raises_uncertain(tmp_path):
    state_db = _state_db(tmp_path)
    _seed(state_db, _payload(key="kg", body="body g"))
    adapter = _FakeIndexAdapter(error=TimeoutError("mid-flight"))
    backend = RagflowDeliveryBackend(state_db=state_db, index_backend=adapter)

    with pytest.raises(DeliveryOutcomeUncertain):
        backend.submit(_job_view(state_db, "kg"))


def test_backend_explicit_failed_maps_to_retryable(tmp_path):
    state_db = _state_db(tmp_path)
    _seed(state_db, _payload(key="kh", body="body h"))
    adapter = _FakeIndexAdapter(
        result=BackendSubmitResult(dataset_ref="ds", document_ref="doc", status=IndexStatus.FAILED)
    )
    backend = RagflowDeliveryBackend(state_db=state_db, index_backend=adapter)

    evidence = backend.submit(_job_view(state_db, "kh"))
    assert evidence.status == "failed_retryable"


def test_backend_missing_payload_is_distinct_status_and_no_submit(tmp_path):
    state_db = _state_db(tmp_path)
    _seed(state_db, _payload(key="ki", body="body i"))
    with state_db.connect() as connection:
        connection.execute("DELETE FROM delivery_payloads WHERE idempotency_key = ?", ("ki",))
    adapter = _FakeIndexAdapter(
        result=BackendSubmitResult(dataset_ref="ds", document_ref="doc", status=IndexStatus.PENDING)
    )
    backend = RagflowDeliveryBackend(state_db=state_db, index_backend=adapter)

    evidence = backend.submit(_job_view(state_db, "ki"))

    assert evidence.status == "payload_unavailable"
    assert adapter.submitted == []


def test_backend_with_executor_quarantines_after_uncertain_cap(tmp_path):
    state_db = _state_db(tmp_path)
    payload = _payload(key="kj", body="body j")
    _seed(state_db, payload)
    backend = RagflowDeliveryBackend(
        state_db=state_db, index_backend=_FakeIndexAdapter(error=TimeoutError("boom"))
    )
    executor = DeliveryExecutor(state_db=state_db, backend=backend, lease_owner="t")

    outcome = executor.execute_once(job_id_for_payload(payload), max_attempts=1)

    assert outcome == "quarantined"
    row = state_db.get_row("delivery_jobs", "idempotency_key", "kj")
    assert row["status"] == "quarantined"


def test_backend_find_by_natural_key_returns_status_evidence(tmp_path):
    state_db = _state_db(tmp_path)
    payload = _payload(key="kk", body="body k")
    _seed(state_db, payload)
    adapter = _FakeIndexAdapter(
        natural_key_handle=BackendDocumentHandle(dataset_ref="ds_lookup", document_ref="doc_lookup"),
        detail=BackendStatusDetail(status=IndexStatus.INDEXING, progress=0.2, backend_raw_status="RUNNING"),
    )
    backend = RagflowDeliveryBackend(state_db=state_db, index_backend=adapter)

    evidence = backend.find_by_natural_key("kk", payload["contentHash"])

    assert evidence is not None
    assert evidence.status == "succeeded"
    assert evidence.dataset_ref == "ds_lookup"
    assert evidence.document_ref == "doc_lookup"
    assert evidence.run == "RUNNING"
    assert adapter.natural_key_calls == [
        ("ragflow-transcript-memory", "kk", payload["contentHash"])
    ]


def test_backend_find_by_natural_key_is_none_for_unknown_or_mismatched_job(tmp_path):
    state_db = _state_db(tmp_path)
    backend = RagflowDeliveryBackend(
        state_db=state_db,
        index_backend=_FakeIndexAdapter(
            natural_key_handle=BackendDocumentHandle(dataset_ref="ds", document_ref="doc"),
            detail=BackendStatusDetail(status=IndexStatus.INDEXED, progress=1.0, backend_raw_status="DONE"),
        ),
    )
    assert backend.find_by_natural_key("k", "sha256:x") is None


def test_backend_status_maps_detail(tmp_path):
    state_db = _state_db(tmp_path)
    backend = RagflowDeliveryBackend(
        state_db=state_db,
        index_backend=_FakeIndexAdapter(
            detail=BackendStatusDetail(status=IndexStatus.INDEXED, progress=1.0, backend_raw_status="DONE")
        ),
    )
    evidence = backend.status("ds_1", "doc_1")
    assert evidence.status == "succeeded"
    assert evidence.run == "DONE"


# ---- Slice C: drain seam ----

class _FakeDeliveryBackend:
    def __init__(self, *, status="succeeded", raise_uncertain=False):
        self._status = status
        self._raise_uncertain = raise_uncertain
        self.calls = []

    def submit(self, job):
        self.calls.append(job.idempotency_key)
        if self._raise_uncertain:
            raise DeliveryOutcomeUncertain("boom")
        return DeliveryBackendEvidence(
            idempotency_key=job.idempotency_key,
            payload_hash=job.payload_hash,
            dataset_ref="ds",
            document_ref="doc",
            run="UNSTART",
            status=self._status,
        )

    def find_by_natural_key(self, idempotency_key, payload_hash):
        return None

    def status(self, dataset_ref, document_ref):
        raise AssertionError("not used")


def test_drain_dry_run_mutates_nothing_and_gates_payloads(tmp_path):
    state_db = _state_db(tmp_path)
    _seed(state_db, _payload(key="d1", body="drain 1"), _payload(key="d2", body="drain 2"))
    with state_db.connect() as connection:
        connection.execute("DELETE FROM delivery_payloads WHERE idempotency_key = ?", ("d2",))

    report = drain_pending_deliveries(state_db=state_db, dry_run=True)

    assert report["execution_status"] == "dry_run"
    assert report["selected_count"] == 2
    assert report["payload_available_count"] == 1
    assert report["payload_missing_count"] == 1
    assert report["blockers"] == ["delivery_payload_missing"]
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
    # no claim happened: both rows still pending with no lease
    for key in ("d1", "d2"):
        row = state_db.get_row("delivery_jobs", "idempotency_key", key)
        assert row["status"] == "pending"
        assert row["lease_owner"] == ""


def test_drain_live_succeeds_with_fake_backend(tmp_path):
    state_db = _state_db(tmp_path)
    _seed(state_db, _payload(key="d3", body="drain 3"))
    backend = _FakeDeliveryBackend(status="succeeded")

    report = drain_pending_deliveries(state_db=state_db, backend=backend, dry_run=False)

    assert report["execution_status"] == "executed"
    assert report["selected_count"] == 1
    assert report["claimed_count"] == 1
    assert report["executed_count"] == 1
    assert report["succeeded_count"] == 1
    assert report["mutation_performed"] is True
    assert state_db.get_row("delivery_jobs", "idempotency_key", "d3")["status"] == "succeeded"


def test_drain_live_counts_retryable_and_quarantined(tmp_path):
    state_db = _state_db(tmp_path)
    _seed(state_db, _payload(key="d4", body="drain 4"), _payload(key="d5", body="drain 5"))
    backend = _FakeDeliveryBackend(raise_uncertain=True)

    # M8.2 policy: claim only leases; only an actual uncertain/failed outcome
    # consumes an attempt.
    first = drain_pending_deliveries(state_db=state_db, backend=backend, dry_run=False, max_attempts=2)
    assert first["retryable_count"] == 2
    assert first["quarantined_count"] == 0
    assert state_db.get_row("delivery_jobs", "idempotency_key", "d4")["attempt_count"] == 1

    # replayable rows are no longer 'pending'; flip them back to exercise the cap
    with state_db.connect() as connection:
        connection.execute("UPDATE delivery_jobs SET status = 'pending', lease_owner = '', lease_until = ''")
    second = drain_pending_deliveries(state_db=state_db, backend=backend, dry_run=False, max_attempts=2)
    assert second["quarantined_count"] == 2
    assert second["execution_status"] == "executed"
    assert state_db.get_row("delivery_jobs", "idempotency_key", "d4")["attempt_count"] == 2


def test_drain_live_requires_backend(tmp_path):
    state_db = _state_db(tmp_path)
    with pytest.raises(ValueError):
        drain_pending_deliveries(state_db=state_db, backend=None, dry_run=False)


def test_drain_respects_limit(tmp_path):
    state_db = _state_db(tmp_path)
    _seed(state_db, _payload(key="d6", body="drain 6"), _payload(key="d7", body="drain 7"))
    report = drain_pending_deliveries(state_db=state_db, dry_run=True, limit=1)
    assert report["selected_count"] == 1


def test_drain_report_is_redacted(tmp_path):
    state_db = _state_db(tmp_path)
    _seed(state_db, _payload(key="secret-key-d8", body="secret drain body"))
    report = drain_pending_deliveries(state_db=state_db, dry_run=True)
    blob = json.dumps(report)
    assert "secret-key-d8" not in blob
    assert "secret drain body" not in blob
    assert str(tmp_path) not in blob
