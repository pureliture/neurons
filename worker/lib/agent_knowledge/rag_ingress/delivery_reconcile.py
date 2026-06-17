"""Fake-only delivery reconcile scaffolding for M3."""

from __future__ import annotations

from datetime import datetime

from .delivery_executor import DeliveryBackend
from .state_db import RAGIngressStateDB


class DeliveryReconciler:
    def __init__(self, *, state_db: RAGIngressStateDB, backend: DeliveryBackend):
        self._state_db = state_db
        self._backend = backend

    def reconcile_once(self, job_id: str, *, now: datetime | None = None, max_attempts: int = 3) -> str:
        row = self._state_db.get_delivery_job(job_id)
        if row is None:
            raise KeyError(job_id)
        evidence = None
        dataset_ref = str(row.get("ragflow_dataset_id") or "")
        document_ref = str(row.get("ragflow_document_id") or "")
        if dataset_ref and document_ref:
            evidence = self._backend.status(dataset_ref, document_ref)
        if evidence is None:
            evidence = self._backend.find_by_natural_key(
                str(row["idempotency_key"]),
                str(row["payload_hash"]),
            )
        if evidence is None:
            return self._state_db.record_replayable_attempt(job_id, now=now, max_attempts=max_attempts)
        if evidence.status == "succeeded":
            self._state_db.record_delivery_evidence(
                job_id,
                status="succeeded",
                dataset_ref=evidence.dataset_ref,
                document_ref=evidence.document_ref,
                run=evidence.run,
                observed_at=evidence.observed_at or now,
            )
            return "succeeded"
        if evidence.status == "failed_retryable":
            return self._state_db.record_failed_retryable_attempt(
                job_id,
                run=evidence.run,
                dataset_ref=evidence.dataset_ref,
                document_ref=evidence.document_ref,
                observed_at=evidence.observed_at,
                now=now,
                max_attempts=max_attempts,
            )
        return self._state_db.record_replayable_attempt(job_id, now=now, max_attempts=max_attempts)
