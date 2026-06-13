from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent_knowledge.rag_ingress.delivery_executor import (
    DeliveryBackendEvidence,
    DeliveryExecutor,
    DeliveryOutcomeUncertain,
)
from agent_knowledge.rag_ingress.delivery_reconcile import DeliveryReconciler
from agent_knowledge.rag_ingress.domain_state import build_delivery_projection_record
from agent_knowledge.rag_ingress.state_db import CommandResultSpec, DeliveryJobSpec, RAGIngressStateDB


NOW = datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)


class FakeDeliveryBackend:
    def __init__(self):
        self.evidence_by_key: dict[tuple[str, str], DeliveryBackendEvidence] = {}
        self.evidence_by_ref: dict[tuple[str, str], DeliveryBackendEvidence] = {}
        self.submit_calls = 0
        self.submit_mode = "success"
        self.status_mode = "success"
        self.observed_at = NOW

    def submit(self, job):
        self.submit_calls += 1
        evidence = DeliveryBackendEvidence(
            idempotency_key=job.idempotency_key,
            payload_hash=job.payload_hash,
            dataset_ref="ds_fake",
            document_ref=f"doc_{job.job_id}",
            run="DONE",
            status="succeeded",
            observed_at=self.observed_at,
        )
        self._store(evidence)
        if self.submit_mode == "timeout_after_success":
            raise DeliveryOutcomeUncertain("timeout after remote success")
        return evidence

    def find_by_natural_key(self, idempotency_key, payload_hash):
        return self.evidence_by_key.get((idempotency_key, payload_hash))

    def status(self, dataset_ref, document_ref):
        if self.status_mode == "async_fail":
            return DeliveryBackendEvidence(
                idempotency_key="delivery_key",
                payload_hash="sha256:payload",
                dataset_ref=dataset_ref,
                document_ref=document_ref,
                run="FAIL",
                status="failed_retryable",
                observed_at=NOW,
            )
        return self.evidence_by_ref[(dataset_ref, document_ref)]

    def _store(self, evidence):
        self.evidence_by_key[(evidence.idempotency_key, evidence.payload_hash)] = evidence
        self.evidence_by_ref[(evidence.dataset_ref, evidence.document_ref)] = evidence


def _db(tmp_path):
    return RAGIngressStateDB(tmp_path / "private" / "rag-ingress-state.sqlite")


def _create_job(db: RAGIngressStateDB, *, job_id="job_1", idempotency_key="delivery_key"):
    return db.command_transaction().execute(
        command_id=f"cmd_{job_id}",
        command_type="transcript_ingest",
        idempotency_key=f"cmd_{job_id}",
        payload_hash="sha256:payload",
        result=CommandResultSpec(decision="accepted"),
        domain_records=[
            build_delivery_projection_record(
                domain_record_id=f"domain_{job_id}",
                resource_id_hash=f"resource_{job_id}",
                lifecycle_status="prepared",
                payload_hash="sha256:payload",
                target_profile="transcript-memory",
                document_kind="conversation_chunk",
            )
        ],
        delivery_jobs=[
            DeliveryJobSpec(
                job_id=job_id,
                idempotency_key=idempotency_key,
                payload_hash="sha256:payload",
                target_profile="transcript-memory",
                document_kind="conversation_chunk",
            )
        ],
        now=NOW,
    )


def test_timeout_after_success_replay_reconciles_to_single_delivery_job(tmp_path):
    db = _db(tmp_path)
    _create_job(db, job_id="job_timeout")
    backend = FakeDeliveryBackend()
    backend.submit_mode = "timeout_after_success"

    executor = DeliveryExecutor(state_db=db, backend=backend, lease_owner="worker_1")
    assert executor.execute_once("job_timeout", now=NOW, max_attempts=4) == "replayable"
    assert db.get_delivery_job("job_timeout")["status"] == "replayable"

    reconciler = DeliveryReconciler(state_db=db, backend=backend)
    assert reconciler.reconcile_once("job_timeout", now=NOW, max_attempts=4) == "succeeded"

    job = db.get_delivery_job("job_timeout")
    assert job["status"] == "succeeded"
    assert job["ragflow_document_id"] == "doc_job_timeout"
    assert backend.submit_calls == 1
    assert [row["job_id"] for row in db.list_rows("delivery_jobs")] == ["job_timeout"]


def test_async_parse_fail_maps_to_failed_retryable_then_quarantine(tmp_path):
    db = _db(tmp_path)
    _create_job(db, job_id="job_async_fail")
    backend = FakeDeliveryBackend()
    executor = DeliveryExecutor(state_db=db, backend=backend, lease_owner="worker_1")
    assert executor.execute_once("job_async_fail", now=NOW, max_attempts=5) == "succeeded"

    backend.status_mode = "async_fail"
    reconciler = DeliveryReconciler(state_db=db, backend=backend)
    assert reconciler.reconcile_once("job_async_fail", now=NOW, max_attempts=4) == "failed_retryable"
    assert db.get_delivery_job("job_async_fail")["status"] == "failed_retryable"
    assert db.get_delivery_job("job_async_fail")["ragflow_run"] == "FAIL"

    assert reconciler.reconcile_once("job_async_fail", now=NOW, max_attempts=2) == "quarantined"
    job = db.get_delivery_job("job_async_fail")
    assert job["status"] == "quarantined"
    assert job["last_error_class"] == "async_parse_failed"


def test_stale_owner_delivery_execution_is_rejected_and_recorded(tmp_path):
    db = _db(tmp_path)
    _create_job(db, job_id="job_stale")
    assert db.claim_delivery_job("job_stale", lease_owner="owner_1", now=NOW, lease_seconds=1)

    executor = DeliveryExecutor(state_db=db, backend=FakeDeliveryBackend(), lease_owner="owner_2")

    assert executor.execute_once("job_stale", now=NOW, max_attempts=3) == "stale_owner_rejected"
    assert db.get_delivery_job("job_stale")["lease_owner"] == "owner_1"
    assert db.get_delivery_job("job_stale")["last_error_class"] == "stale_owner_rejected"


def test_delivery_success_completion_rejects_mismatched_owner(tmp_path):
    db = _db(tmp_path)
    _create_job(db, job_id="job_mismatch")
    assert db.claim_delivery_job("job_mismatch", lease_owner="owner_1", now=NOW, lease_seconds=10)
    assert db.mark_delivery_executing("job_mismatch", lease_owner="owner_1", now=NOW)

    assert not db.complete_delivery_with_evidence(
        "job_mismatch",
        lease_owner="owner_2",
        status="succeeded",
        dataset_ref="ds_fake",
        document_ref="doc_mismatch",
        run="DONE",
        observed_at=NOW,
    )
    job = db.get_delivery_job("job_mismatch")
    assert job["status"] == "executing"
    assert job["last_error_class"] == "stale_owner_rejected"


def test_delivery_success_completion_rejects_expired_owner(tmp_path):
    db = _db(tmp_path)
    _create_job(db, job_id="job_expired")
    assert db.claim_delivery_job("job_expired", lease_owner="owner_1", now=NOW, lease_seconds=1)
    assert db.mark_delivery_executing("job_expired", lease_owner="owner_1", now=NOW)

    assert not db.complete_delivery_with_evidence(
        "job_expired",
        lease_owner="owner_1",
        status="succeeded",
        dataset_ref="ds_fake",
        document_ref="doc_expired",
        run="DONE",
        observed_at=NOW + timedelta(seconds=2),
    )
    job = db.get_delivery_job("job_expired")
    assert job["status"] == "executing"
    assert job["last_error_class"] == "stale_owner_rejected"


def test_executor_completion_cannot_backdate_expired_lease_with_backend_observed_at(tmp_path):
    db = _db(tmp_path)
    _create_job(db, job_id="job_backdate")
    backend = FakeDeliveryBackend()
    backend.observed_at = NOW
    executor = DeliveryExecutor(state_db=db, backend=backend, lease_owner="owner_1")
    assert db.claim_delivery_job("job_backdate", lease_owner="owner_1", now=NOW, lease_seconds=1)

    assert executor.execute_once("job_backdate", now=NOW + timedelta(seconds=2), max_attempts=3) == "stale_owner_rejected"
    job = db.get_delivery_job("job_backdate")
    assert job["status"] == "claimed"
    assert job["ragflow_document_id"] == ""
    assert job["last_error_class"] == "stale_owner_rejected"


def test_terminal_delivery_job_is_not_reexecuted(tmp_path):
    db = _db(tmp_path)
    _create_job(db, job_id="job_terminal")
    backend = FakeDeliveryBackend()
    executor = DeliveryExecutor(state_db=db, backend=backend, lease_owner="worker_1")

    assert executor.execute_once("job_terminal", now=NOW, max_attempts=3) == "succeeded"
    assert executor.execute_once("job_terminal", now=NOW, max_attempts=3) == "succeeded"
    assert backend.submit_calls == 1
