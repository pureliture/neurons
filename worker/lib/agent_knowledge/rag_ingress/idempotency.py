"""Idempotency outcome classifier for RAG ingress state.

The classifier is intentionally pure. Storage callers are responsible for
recording the returned outcome; this module only decides which outcome applies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


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
) -> IdempotencyDecision:
    """Classify a new attempt against an existing record.

    ``existing`` is expected to be the current local record for the same natural
    key. Passing a mismatched key is treated as no match so callers can safely
    use broader lookup projections in tests.
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

    existing_hash = str(existing.get("payload_hash") or "")
    if existing_hash == payload_hash:
        return IdempotencyDecision(IdempotencyOutcome.DUPLICATE, "same_key_same_payload_hash")
    return IdempotencyDecision(IdempotencyOutcome.CONFLICT, "same_key_different_payload_hash")
