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
        self.submit_status = "succeeded"
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
            status=self.submit_status,
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
    assert job["index_document_id"] == "doc_job_timeout"
    assert backend.submit_calls == 1
    assert [row["job_id"] for row in db.list_rows("delivery_jobs")] == ["job_timeout"]


def test_different_owner_reclaims_uncertain_job_without_manual_pending_reset(tmp_path):
    db = _db(tmp_path)
    _create_job(db, job_id="job_uncertain_reclaim")
    backend = FakeDeliveryBackend()
    backend.submit_mode = "timeout_after_success"

    first = DeliveryExecutor(state_db=db, backend=backend, lease_owner="worker_a")
    assert first.execute_once("job_uncertain_reclaim", now=NOW, max_attempts=4) == "replayable"
    replayable = db.get_delivery_job("job_uncertain_reclaim")
    assert replayable["status"] == "replayable"
    assert replayable["lease_owner"] == ""
    assert replayable["lease_until"] == ""

    backend.submit_mode = "success"
    second = DeliveryExecutor(state_db=db, backend=backend, lease_owner="worker_b")
    assert second.execute_once("job_uncertain_reclaim", now=NOW + timedelta(seconds=60), max_attempts=4) == "succeeded"

    job = db.get_delivery_job("job_uncertain_reclaim")
    assert job["status"] == "succeeded"
    assert backend.submit_calls == 2


def test_payload_integrity_failure_is_terminal_quarantine_not_replayable(tmp_path):
    db = _db(tmp_path)
    _create_job(db, job_id="job_payload_integrity")
    backend = FakeDeliveryBackend()
    backend.submit_status = "payload_integrity_mismatch"

    outcome = DeliveryExecutor(
        state_db=db, backend=backend, lease_owner="worker_integrity"
    ).execute_once("job_payload_integrity", now=NOW, max_attempts=4)

    job = db.get_delivery_job("job_payload_integrity")
    assert outcome == "quarantined"
    assert job["status"] == "quarantined"
    assert job["last_error_class"] == "delivery_payload_integrity_mismatch"
    assert job["lease_owner"] == ""
    assert job["lease_until"] == ""


def test_replayable_record_rejects_a_stale_live_lease_owner_without_writing(tmp_path):
    db = _db(tmp_path)
    _create_job(db, job_id="job_replay_stale_owner")
    assert db.claim_delivery_job(
        "job_replay_stale_owner", lease_owner="owner_a", now=NOW, lease_seconds=60
    )

    before = db.get_delivery_job("job_replay_stale_owner")
    assert db.record_replayable_attempt(
        "job_replay_stale_owner", lease_owner="owner_b", now=NOW, max_attempts=4
    ) == "stale_owner_rejected"

    job = db.get_delivery_job("job_replay_stale_owner")
    assert job["status"] == "claimed"
    assert job["lease_owner"] == "owner_a"
    assert job == before

    assert db.record_replayable_attempt(
        "job_replay_stale_owner", lease_owner="owner_a", now=NOW + timedelta(seconds=61), max_attempts=4
    ) == "stale_owner_rejected"
    assert db.get_delivery_job("job_replay_stale_owner") == before


def test_claim_attempt_limit_returns_terminal_quarantine_to_executor(tmp_path):
    db = _db(tmp_path)
    _create_job(db, job_id="job_claim_attempt_limit")
    assert db.record_replayable_attempt(
        "job_claim_attempt_limit", now=NOW, max_attempts=4, next_retry_seconds=0
    ) == "replayable"

    backend = FakeDeliveryBackend()
    outcome = DeliveryExecutor(
        state_db=db, backend=backend, lease_owner="worker_limit"
    ).execute_once("job_claim_attempt_limit", now=NOW + timedelta(seconds=1), max_attempts=1)

    job = db.get_delivery_job("job_claim_attempt_limit")
    assert outcome == "quarantined"
    assert job["status"] == "quarantined"
    assert job["lease_owner"] == ""
    assert job["lease_until"] == ""
    assert backend.submit_calls == 0


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
    assert db.get_delivery_job("job_async_fail")["index_run_id"] == "FAIL"

    assert reconciler.reconcile_once("job_async_fail", now=NOW, max_attempts=2) == "quarantined"
    job = db.get_delivery_job("job_async_fail")
    assert job["status"] == "quarantined"
    assert job["last_error_class"] == "async_parse_failed"


def test_stale_owner_delivery_execution_is_rejected_without_overwriting_owner_state(tmp_path):
    db = _db(tmp_path)
    _create_job(db, job_id="job_stale")
    assert db.claim_delivery_job("job_stale", lease_owner="owner_1", now=NOW, lease_seconds=1)

    executor = DeliveryExecutor(state_db=db, backend=FakeDeliveryBackend(), lease_owner="owner_2")

    before = db.get_delivery_job("job_stale")
    assert executor.execute_once("job_stale", now=NOW, max_attempts=3) == "claim_rejected"
    assert db.get_delivery_job("job_stale") == before


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
    assert job["last_error_class"] == ""


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
    assert job["last_error_class"] == ""


def test_executor_reclaims_expired_lease_without_backdating_backend_observed_at(tmp_path):
    db = _db(tmp_path)
    _create_job(db, job_id="job_backdate")
    backend = FakeDeliveryBackend()
    backend.observed_at = NOW
    executor = DeliveryExecutor(state_db=db, backend=backend, lease_owner="owner_1")
    assert db.claim_delivery_job("job_backdate", lease_owner="owner_1", now=NOW, lease_seconds=1)

    assert executor.execute_once("job_backdate", now=NOW + timedelta(seconds=2), max_attempts=3) == "succeeded"
    job = db.get_delivery_job("job_backdate")
    assert job["status"] == "succeeded"
    assert job["index_document_id"] == "doc_job_backdate"
    assert job["last_error_class"] == ""


def test_terminal_delivery_job_is_not_reexecuted(tmp_path):
    db = _db(tmp_path)
    _create_job(db, job_id="job_terminal")
    backend = FakeDeliveryBackend()
    executor = DeliveryExecutor(state_db=db, backend=backend, lease_owner="worker_1")

    assert executor.execute_once("job_terminal", now=NOW, max_attempts=3) == "succeeded"
    assert executor.execute_once("job_terminal", now=NOW, max_attempts=3) == "succeeded"
    assert backend.submit_calls == 1


def test_claim_delivery_job_rechecks_status_due_time_and_expired_lease_in_transaction(tmp_path):
    db = _db(tmp_path)
    _create_job(db, job_id="job_pending_immediate")
    assert db.claim_delivery_job("job_pending_immediate", lease_owner="owner_pending", now=NOW)

    _create_job(db, job_id="job_replay_due", idempotency_key="delivery_replay_due")
    assert db.record_replayable_attempt(
        "job_replay_due", now=NOW, max_attempts=4, next_retry_seconds=60
    ) == "replayable"
    assert not db.claim_delivery_job(
        "job_replay_due", lease_owner="owner_before_due", now=NOW + timedelta(seconds=59)
    )
    assert db.claim_delivery_job(
        "job_replay_due", lease_owner="owner_due", now=NOW + timedelta(seconds=60)
    )

    _create_job(db, job_id="job_failed_due", idempotency_key="delivery_failed_due")
    assert db.record_failed_retryable_attempt(
        "job_failed_due", now=NOW, max_attempts=4, next_retry_seconds=60
    ) == "failed_retryable"
    assert not db.claim_delivery_job(
        "job_failed_due", lease_owner="owner_before_due", now=NOW + timedelta(seconds=59)
    )
    assert db.claim_delivery_job(
        "job_failed_due", lease_owner="owner_due", now=NOW + timedelta(seconds=60)
    )

    _create_job(db, job_id="job_expired_executing", idempotency_key="delivery_expired_executing")
    assert db.claim_delivery_job(
        "job_expired_executing", lease_owner="owner_old", now=NOW, lease_seconds=1
    )
    assert db.mark_delivery_executing("job_expired_executing", lease_owner="owner_old", now=NOW)
    assert db.claim_delivery_job(
        "job_expired_executing", lease_owner="owner_new", now=NOW + timedelta(seconds=2)
    )
    reclaimed = db.get_delivery_job("job_expired_executing")
    assert reclaimed["status"] == "claimed"
    assert reclaimed["lease_owner"] == "owner_new"


def test_executor_reclaims_expired_claimed_job_before_submitting(tmp_path):
    db = _db(tmp_path)
    _create_job(db, job_id="job_executor_expired_claim")
    assert db.claim_delivery_job(
        "job_executor_expired_claim", lease_owner="owner_old", now=NOW, lease_seconds=1
    )
    backend = FakeDeliveryBackend()

    assert DeliveryExecutor(
        state_db=db, backend=backend, lease_owner="owner_new"
    ).execute_once("job_executor_expired_claim", now=NOW + timedelta(seconds=2)) == "succeeded"
    assert backend.submit_calls == 1


def test_terminal_and_stale_completion_are_zero_write_to_preserve_newer_evidence(tmp_path):
    db = _db(tmp_path)
    _create_job(db, job_id="job_terminal_toctou")
    assert db.claim_delivery_job("job_terminal_toctou", lease_owner="owner_old", now=NOW)
    db.record_delivery_evidence(
        "job_terminal_toctou",
        status="succeeded",
        dataset_ref="ds_terminal",
        document_ref="doc_terminal",
        run="DONE",
        last_error_class="remote_success",
        observed_at=NOW,
    )
    terminal = db.get_delivery_job("job_terminal_toctou")

    assert not db.claim_delivery_job(
        "job_terminal_toctou", lease_owner="owner_new", now=NOW + timedelta(seconds=1)
    )
    assert not db.complete_delivery_with_evidence(
        "job_terminal_toctou",
        lease_owner="owner_old",
        status="succeeded",
        dataset_ref="ds_stale",
        document_ref="doc_stale",
        run="DONE",
        now=NOW + timedelta(seconds=1),
    )
    assert db.get_delivery_job("job_terminal_toctou") == terminal
