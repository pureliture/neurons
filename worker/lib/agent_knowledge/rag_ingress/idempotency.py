"""Idempotency outcome classifier for RAG ingress state.

The classifier is intentionally pure. Storage callers are responsible for
recording the returned outcome; this module only decides which outcome applies.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping


def envelope_fingerprint(payload: Mapping[str, object]) -> str:
    """Canonical fingerprint of a complete ingress envelope.

    Unlike ``contentHash`` this covers every field in the payload mapping,
    including metadata, so a metadata-only mutation cannot masquerade as an
    idempotent retransmission.
    """

    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class IdempotencyOutcome:
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    QUARANTINED = "quarantined"
    REPLAYABLE = "replayable"


TERMINAL_PRESERVED_OUTCOMES = frozenset(
    {
        IdempotencyOutcome.QUARANTINED,
        IdempotencyOutcome.REPLAYABLE,
    }
)


@dataclass(frozen=True)
class IdempotencyDecision:
    outcome: str
    reason: str

    @property
    def accepted(self) -> bool:
        return self.outcome == IdempotencyOutcome.ACCEPTED

    @property
    def duplicate(self) -> bool:
        return self.outcome == IdempotencyOutcome.DUPLICATE

    @property
    def conflict(self) -> bool:
        return self.outcome == IdempotencyOutcome.CONFLICT


def classify_idempotency(
    existing: Mapping[str, object] | None,
    *,
    idempotency_key: str,
    payload_hash: str,
    envelope_fingerprint: str = "",
) -> IdempotencyDecision:
    """Classify a new attempt against an existing record.

    ``existing`` is expected to be the current local record for the same natural
    key. Passing a mismatched key is treated as no match so callers can safely
    use broader lookup projections in tests.

    Terminal QUARANTINED/REPLAYABLE records always win. For a matching key the
    canonical envelope fingerprint is compared before any duplicate decision:
    a missing or mismatched fingerprint fails closed as a conflict, even when
    the legacy ``payload_hash`` still agrees.
    """

    if not idempotency_key:
        raise ValueError("idempotency_key is required")
    if not payload_hash:
        raise ValueError("payload_hash is required")
    if existing is None:
        return IdempotencyDecision(IdempotencyOutcome.ACCEPTED, "new_idempotency_key")

    existing_outcome = str(existing.get("accept_outcome") or existing.get("outcome") or "")
    existing_status = str(existing.get("status") or "")
    if existing_outcome in TERMINAL_PRESERVED_OUTCOMES:
        return IdempotencyDecision(existing_outcome, f"existing_{existing_outcome}")
    if existing_status in TERMINAL_PRESERVED_OUTCOMES:
        return IdempotencyDecision(existing_status, f"existing_{existing_status}")

    existing_key = str(existing.get("idempotency_key") or "")
    if existing_key != idempotency_key:
        return IdempotencyDecision(IdempotencyOutcome.ACCEPTED, "different_idempotency_key")

    if not envelope_fingerprint:
        return IdempotencyDecision(IdempotencyOutcome.CONFLICT, "missing_incoming_envelope_fingerprint")
    existing_fingerprint = str(existing.get("envelope_fingerprint") or "")
    if not existing_fingerprint:
        return IdempotencyDecision(IdempotencyOutcome.CONFLICT, "missing_existing_envelope_fingerprint")
    if existing_fingerprint != envelope_fingerprint:
        return IdempotencyDecision(IdempotencyOutcome.CONFLICT, "same_key_different_envelope_fingerprint")

    existing_hash = str(existing.get("payload_hash") or "")
    if existing_hash == payload_hash:
        return IdempotencyDecision(IdempotencyOutcome.DUPLICATE, "same_key_same_envelope")
    return IdempotencyDecision(IdempotencyOutcome.CONFLICT, "same_key_different_payload_hash")
