"""M8.1 real DeliveryBackend prep: state-DB payload -> IndexBackendAdapter submit.

This module turns a committed ``delivery_jobs`` row into an actual backend
submission, WITHOUT being wired to any production route:

- It is imported by no server/worker/LaunchAgent path. The only intended
  construction point is the operator-gated ``rag-ingress-state drain-deliveries``
  CLI live path (which itself requires an explicit approval record).
- It submits committed ``delivery_jobs`` only: the executor (``DeliveryExecutor``)
  claims the job and calls ``submit`` OUTSIDE the command transaction, so no
  RAGFlow/queue call ever happens inside a state-DB transaction.
- Payload availability gate (M8.1 Slice A): ``delivery_jobs`` is hash-only, so the
  payload is recovered from the ``delivery_payloads`` table and triple-checked
  (job.payload_hash == stored payload_hash == sha256(document body)). A missing or
  mismatching payload surfaces as a distinct ``payload_unavailable`` /
  ``payload_integrity_mismatch`` evidence status -- never a fake submit.
- Outcome mapping is conservative: ANY exception raised mid-submit becomes
  ``DeliveryOutcomeUncertain`` (the upload may or may not have reached the
  backend), while an explicit backend FAILED status is ``failed_retryable``.
  Uncertain and retryable are therefore never conflated.
- Reports/evidence carry refs and statuses only -- never payload bodies.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

from .delivery_executor import (
    DeliveryBackendEvidence,
    DeliveryJobView,
    DeliveryOutcomeUncertain,
)
from .index_backend import BackendDocumentHandle, IndexBackendAdapter, IndexStatus
from .server_runtime import document_from_ingress_payload
from .state_db import RAGIngressStateDB

PAYLOAD_OK = "ok"
PAYLOAD_MISSING = "payload_missing"
PAYLOAD_HASH_MISMATCH = "payload_hash_mismatch"

# IndexStatus -> delivery evidence status. Submit acceptance (the document now
# exists in the backend pipeline) counts as delivery success; parse progress is
# the reconciler's concern, not the delivery worker's.
_SUBMIT_STATUS_TO_EVIDENCE = {
    IndexStatus.INDEXED: "succeeded",
    IndexStatus.INDEXING: "succeeded",
    IndexStatus.PENDING: "succeeded",
    IndexStatus.FAILED: "failed_retryable",
    IndexStatus.UNKNOWN: "unknown",
}


def resolve_delivery_payload(
    state_db: RAGIngressStateDB,
    *,
    idempotency_key: str,
    expected_payload_hash: str,
) -> tuple[dict | None, str]:
    """Recover the redacted wire payload for a delivery job, fail-closed.

    Returns ``(payload, "ok")`` only when the stored payload exists AND the
    delivery job's payload_hash, the stored contentHash, and a fresh sha256 of the
    stored document body all agree. Anything else returns ``(None, reason)``.
    """
    payload = state_db.get_delivery_payload(idempotency_key)
    if payload is None:
        return None, PAYLOAD_MISSING
    content_hash = str(payload.get("contentHash") or "")
    body = str(((payload.get("payload") or {}).get("document") or {}).get("body") or "")
    recomputed = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    if content_hash != expected_payload_hash or recomputed != expected_payload_hash:
        return None, PAYLOAD_HASH_MISMATCH
    return payload, PAYLOAD_OK


class RagflowDeliveryBackend:
    """``DeliveryBackend`` protocol implementation over ``IndexBackendAdapter``."""

    def __init__(self, *, state_db: RAGIngressStateDB, index_backend: IndexBackendAdapter):
        self._state_db = state_db
        self._index_backend = index_backend

    def submit(self, job: DeliveryJobView) -> DeliveryBackendEvidence:
        payload, gate = resolve_delivery_payload(
            self._state_db,
            idempotency_key=job.idempotency_key,
            expected_payload_hash=job.payload_hash,
        )
        if gate != PAYLOAD_OK:
            return DeliveryBackendEvidence(
                idempotency_key=job.idempotency_key,
                payload_hash=job.payload_hash,
                dataset_ref="",
                document_ref="",
                run="",
                status="payload_unavailable" if gate == PAYLOAD_MISSING else "payload_integrity_mismatch",
            )
        existing = self.find_by_natural_key(job.idempotency_key, job.payload_hash)
        if existing is not None:
            return existing
        document = document_from_ingress_payload(payload)
        document = replace(
            document,
            metadata={**document.metadata, "content_hash": document.content_hash},
        )
        try:
            result = self._index_backend.submit_document(document)
        except Exception as exc:
            # the request may have reached the backend before failing; never
            # report a mid-flight error as a clean retryable failure
            raise DeliveryOutcomeUncertain(exc.__class__.__name__) from exc
        return DeliveryBackendEvidence(
            idempotency_key=job.idempotency_key,
            payload_hash=job.payload_hash,
            dataset_ref=result.dataset_ref,
            document_ref=result.document_ref,
            run=result.status,
            status=_SUBMIT_STATUS_TO_EVIDENCE.get(result.status, "unknown"),
        )

    def find_by_natural_key(self, idempotency_key: str, payload_hash: str) -> DeliveryBackendEvidence | None:
        row = self._state_db.get_row("delivery_jobs", "idempotency_key", idempotency_key)
        if row is None or str(row.get("payload_hash") or "") != payload_hash:
            return None
        lookup = getattr(self._index_backend, "find_by_natural_key", None)
        if not callable(lookup):
            return None
        handle = lookup(
            target_profile=str(row.get("target_profile") or ""),
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
        )
        if handle is None:
            return None
        detail = self._index_backend.document_status_detail(handle)
        return DeliveryBackendEvidence(
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            dataset_ref=handle.dataset_ref,
            document_ref=handle.document_ref,
            run=detail.backend_raw_status,
            status=_SUBMIT_STATUS_TO_EVIDENCE.get(detail.status, "unknown"),
        )

    def status(self, dataset_ref: str, document_ref: str) -> DeliveryBackendEvidence:
        detail = self._index_backend.document_status_detail(
            BackendDocumentHandle(dataset_ref=dataset_ref, document_ref=document_ref)
        )
        return DeliveryBackendEvidence(
            idempotency_key="",
            payload_hash="",
            dataset_ref=dataset_ref,
            document_ref=document_ref,
            run=detail.backend_raw_status,
            status=_SUBMIT_STATUS_TO_EVIDENCE.get(detail.status, "unknown"),
        )
