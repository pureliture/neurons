"""CouchDB-backed live delivery executor with an injected backend boundary.

``shadow_worker`` uses this executor when ``SHADOW_DELIVER=1`` and
``INGRESS_DELIVERY_BACKEND=couchdb``. The canonical job/payload/lease state is
the worker's private SQLite state DB; CouchDB is the delivery sink. Other
callers may still inject a backend for isolated tests, but this module is no
longer fake-only or an inactive outbox sketch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .state_db import RAGIngressStateDB


@dataclass(frozen=True)
class DeliveryJobView:
    job_id: str
    idempotency_key: str
    payload_hash: str
    target_profile: str
    document_kind: str

    @classmethod
    def from_row(cls, row: dict) -> "DeliveryJobView":
        return cls(
            job_id=str(row["job_id"]),
            idempotency_key=str(row["idempotency_key"]),
            payload_hash=str(row["payload_hash"]),
            target_profile=str(row["target_profile"]),
            document_kind=str(row["document_kind"]),
        )


@dataclass(frozen=True)
class DeliveryBackendEvidence:
    idempotency_key: str
    payload_hash: str
    dataset_ref: str
    document_ref: str
    run: str
    status: str
    observed_at: datetime | None = None


@dataclass(frozen=True)
class DeliveryExecutionReceipt:
    status: str
    submit_attempted: bool = False


class DeliveryOutcomeUncertain(RuntimeError):
    pass


class DeliveryBackend(Protocol):
    def submit(self, job: DeliveryJobView) -> DeliveryBackendEvidence: ...

    def find_by_natural_key(self, idempotency_key: str, payload_hash: str) -> DeliveryBackendEvidence | None: ...

    def status(self, dataset_ref: str, document_ref: str) -> DeliveryBackendEvidence: ...


class DeliveryExecutor:
    def __init__(
        self,
        *,
        state_db: RAGIngressStateDB,
        backend: DeliveryBackend,
        lease_owner: str,
        lease_seconds: int = 600,
    ):
        self._state_db = state_db
        self._backend = backend
        self._lease_owner = lease_owner
        self._lease_seconds = max(int(lease_seconds), 60)

    def correct_and_execute_procedural_once(
        self, job_id: str, *, expected_updated_at: str,
        expected_payload_identity: str, now: datetime | None = None,
    ) -> DeliveryExecutionReceipt:
        """Public bounded correction/reprocessing; never creates another queue message.

        An existing audit is GET-only: callers must reconcile, never resend this
        operation after an uncertain transport result.
        """
        from ..couchdb_source.document_model import build_repo_usage_pattern_document
        from .state_db import StateDBError
        import copy
        row = self._state_db.get_delivery_job(job_id)
        if row is None:
            raise KeyError(job_id)
        if self._state_db.get_domain_record("source-correction:" + job_id) is not None:
            raise StateDBError("source_correction_already_recorded_get_only")
        original = self._state_db.get_delivery_payload(str(row["idempotency_key"]))
        if original is None:
            raise StateDBError("source_correction_payload_missing")
        corrected = copy.deepcopy(original)
        old_hash = corrected["payload"]["document"]["metadata"]["session_id_hash"]
        corrected["payload"]["document"]["metadata"]["session_id_hash"] = "sha256:" + old_hash
        # Pure preparation before the audit/state mutation catches unsupported
        # contract, hash, privacy and destination without any remote write.
        build_repo_usage_pattern_document(payload=corrected, job_id=job_id)
        preflight = getattr(self._backend, "preflight_procedural_correction", None)
        if preflight is None:
            raise StateDBError("source_correction_backend_not_supported")
        preflight(DeliveryJobView.from_row(row), original, corrected)
        self._state_db.correct_procedural_source_hash(
            job_id, expected_updated_at=expected_updated_at,
            expected_payload_identity=expected_payload_identity,
            lease_owner=self._lease_owner, now=now,
        )
        return self.execute_once_with_receipt(job_id, now=now)

    def execute_once(self, job_id: str, *, now: datetime | None = None, max_attempts: int = 3) -> str:
        return self.execute_once_with_receipt(
            job_id, now=now, max_attempts=max_attempts
        ).status

    def execute_once_with_receipt(
        self, job_id: str, *, now: datetime | None = None, max_attempts: int = 3
    ) -> DeliveryExecutionReceipt:
        row = self._state_db.get_delivery_job(job_id)
        if row is None:
            raise KeyError(job_id)
        status = str(row.get("status") or "")
        if status in {"succeeded", "quarantined"}:
            return DeliveryExecutionReceipt(status)
        correction = self._state_db.get_domain_record("source-correction:" + job_id)
        if correction is not None and status != "claimed":
            return DeliveryExecutionReceipt("correction_reconcile_required")
        if correction is not None and row.get("lease_owner") != self._lease_owner:
            return DeliveryExecutionReceipt("stale_owner_rejected")
        if status in {"pending", "replayable", "failed_retryable", "claimed", "executing"}:
            if not self._state_db.claim_delivery_job(
                job_id,
                lease_owner=self._lease_owner,
                lease_seconds=self._lease_seconds,
                now=now,
                max_attempts=max_attempts,
            ):
                current = self._state_db.get_delivery_job(job_id)
                if current is not None and current.get("status") == "quarantined":
                    return DeliveryExecutionReceipt("quarantined")
                return DeliveryExecutionReceipt("claim_rejected")
        elif status not in {"claimed", "executing"}:
            return DeliveryExecutionReceipt("claim_rejected")
        if not self._state_db.mark_delivery_executing(job_id, lease_owner=self._lease_owner, now=now):
            return DeliveryExecutionReceipt("stale_owner_rejected")

        job = DeliveryJobView.from_row(self._state_db.get_delivery_job(job_id) or row)
        try:
            evidence = self._backend.submit(job)
        except DeliveryOutcomeUncertain:
            return DeliveryExecutionReceipt(
                self._state_db.record_replayable_attempt(
                    job_id,
                    lease_owner=self._lease_owner,
                    now=now,
                    max_attempts=max_attempts,
                ),
                submit_attempted=True,
            )

        if evidence.status in {"quarantined", "payload_unavailable", "payload_integrity_mismatch"}:
            if not self._state_db.complete_delivery_with_evidence(
                job_id,
                lease_owner=self._lease_owner,
                status="quarantined",
                dataset_ref=evidence.dataset_ref,
                document_ref=evidence.document_ref,
                run=evidence.run,
                last_error_class=f"delivery_{evidence.status}",
                observed_at=evidence.observed_at,
                now=now,
            ):
                return DeliveryExecutionReceipt("stale_owner_rejected", submit_attempted=True)
            return DeliveryExecutionReceipt("quarantined", submit_attempted=True)
        if evidence.status == "succeeded":
            if not self._state_db.complete_delivery_with_evidence(
                job_id,
                lease_owner=self._lease_owner,
                status="succeeded",
                dataset_ref=evidence.dataset_ref,
                document_ref=evidence.document_ref,
                run=evidence.run,
                observed_at=evidence.observed_at,
                now=now,
            ):
                return DeliveryExecutionReceipt("stale_owner_rejected", submit_attempted=True)
            return DeliveryExecutionReceipt("succeeded", submit_attempted=True)
        if evidence.status == "failed_retryable":
            return DeliveryExecutionReceipt(
                self._state_db.record_failed_retryable_attempt(
                    job_id,
                    run=evidence.run,
                    dataset_ref=evidence.dataset_ref,
                    document_ref=evidence.document_ref,
                    lease_owner=self._lease_owner,
                    observed_at=evidence.observed_at,
                    now=now,
                    max_attempts=max_attempts,
                ),
                submit_attempted=True,
            )
        return DeliveryExecutionReceipt(
            self._state_db.record_replayable_attempt(
                job_id,
                lease_owner=self._lease_owner,
                now=now,
                max_attempts=max_attempts,
            ),
            submit_attempted=True,
        )
