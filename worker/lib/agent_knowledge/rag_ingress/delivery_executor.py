"""Fake-only committed delivery job executor scaffolding.

This module is not wired to production routes or CLIs. It exercises the M3
outbox contract against an injected backend-shaped object.
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

    def execute_once(self, job_id: str, *, now: datetime | None = None, max_attempts: int = 3) -> str:
        row = self._state_db.get_delivery_job(job_id)
        if row is None:
            raise KeyError(job_id)
        if row.get("status") in {"succeeded", "quarantined"}:
            return str(row["status"])
        if row.get("status") == "pending":
            if not self._state_db.claim_delivery_job(
                job_id,
                lease_owner=self._lease_owner,
                lease_seconds=self._lease_seconds,
                now=now,
            ):
                return "claim_rejected"
        if not self._state_db.mark_delivery_executing(job_id, lease_owner=self._lease_owner, now=now):
            return "stale_owner_rejected"

        job = DeliveryJobView.from_row(self._state_db.get_delivery_job(job_id) or row)
        try:
            evidence = self._backend.submit(job)
        except DeliveryOutcomeUncertain:
            return self._state_db.record_replayable_attempt(job_id, now=now, max_attempts=max_attempts)

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
                return "stale_owner_rejected"
            return "succeeded"
        if evidence.status == "failed_retryable":
            return self._state_db.record_failed_retryable_attempt(
                job_id,
                run=evidence.run,
                dataset_ref=evidence.dataset_ref,
                document_ref=evidence.document_ref,
                lease_owner=self._lease_owner,
                observed_at=evidence.observed_at,
                now=now,
                max_attempts=max_attempts,
            )
        return self._state_db.record_replayable_attempt(job_id, now=now, max_attempts=max_attempts)
