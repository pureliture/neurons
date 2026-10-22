"""Backend-neutral delivery payload gate.

A committed ``delivery_jobs`` row only carries a payload *hash*. The wire
payload is recovered from the ``delivery_payloads`` table and triple-checked
before any backend is allowed to see it:

- ``job.payload_hash`` == stored ``contentHash`` == ``sha256(document body)``.

A missing or mismatching payload surfaces as a distinct ``payload_missing`` /
``payload_hash_mismatch`` evidence status -- never a fake submit.

This module is backend-neutral on purpose: it knows nothing about which
backend the delivery lands in.
"""

from __future__ import annotations

import hashlib

from .state_db import RAGIngressStateDB

PAYLOAD_OK = "ok"
PAYLOAD_MISSING = "payload_missing"
PAYLOAD_HASH_MISMATCH = "payload_hash_mismatch"


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
    payload = state_db.get_effective_delivery_payload(idempotency_key)
    if payload is None:
        return None, PAYLOAD_MISSING
    content_hash = str(payload.get("contentHash") or "")
    body = str(((payload.get("payload") or {}).get("document") or {}).get("body") or "")
    recomputed = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    if content_hash != expected_payload_hash or recomputed != expected_payload_hash:
        return None, PAYLOAD_HASH_MISMATCH
    return payload, PAYLOAD_OK
