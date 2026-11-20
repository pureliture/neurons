import pytest

from agent_knowledge.session_memory.brain_query import _normalize_query_memory_card
from agent_knowledge.session_memory.index_projection import _require_projectable_card
from agent_knowledge.session_memory.memory_card import (
    validate_memory_card_envelope,
    validate_status_expires_at,
    validate_typed_payload,
)
from agent_knowledge.session_memory.memory_evaluation import classify_candidate_block_reason

VALID = [
    "2026-11-20T12:00:00Z",
    "2026-11-20T12:00Z",
    "2026-11-20T12:00:00.1234567890Z",
    "2026-11-20T12:00:00+09:00",
    "2026-11-20T12:00+09:00",
    "2026-11-20T12:00:00+15:59",
    "2026-11-20T12:00:00-15:59",
    "2026-11-20T24:00:00Z",
    "2026-11-20T24:00Z",
    "2026-11-20T24:00:00.000Z",
    "2026-11-20T12:59:60Z",
    "2026-11-20T12:00:60.5Z",
    "2024-02-29T12:00:00Z",
]

INVALID = [
    "2026-11-20 12:00:00+00:00",
    "2026-11-20T12:00:00+0000",
    "2026-11-20T12:00:00",
    "2026-11-20",
    "2026-11-20t12:00:00z",
    "٢٠٢٦-١١-٢٠T١٢:٠٠:٠٠Z",
    "2026-02-30T12:00:00Z",
    "2026-02-29T12:00:00Z",
    "2026-13-01T12:00:00Z",
    "2026-11-20T24:00:01Z",
    "2026-11-20T24:00:00.123Z",
    "2026-11-20T24:00:60Z",
    "2026-11-20T12:60:00Z",
    "2026-11-20T12:00:61Z",
    "2026-11-20T12:00:00+16:00",
    "2026-11-20T12:00:00+15:60",
    "2026-11-20T12:00:00-16:00",
    "until next deploy",
    "2999-01-01T23:59:60.5Z",
    "2999-01-01T12:00:00." + "0" * 200 + "Z",
]


def _status_payload(expires_at):
    return {
        "status_value": "active",
        "observed_at": "2026-11-20T00:00:00Z",
        "expires_at": expires_at,
        "current_authority": True,
    }


def _accepted_card(expires_at):
    return {
        "memory_id": "mem_synthetic_status_01",
        "brain_id": "brain_synthetic",
        "card_type": "status",
        "scope": "project",
        "project": "neurons",
        "provider": "codex",
        "title": "Synthetic status",
        "summary": "Synthetic status summary",
        "render_text": "Synthetic status render",
        "lifecycle_state": "auto_accepted",
        "judgment_state": "auto_status",
        "status": "accepted",
        "approval_state": "auto_accepted",
        "governance_tier": "medium",
        "freshness": "current",
        "currentness": "current",
        "confidence": 0.9,
        "confidence_basis": "deterministic",
        "source_refs": [{"source_type": "session", "source_id": "synthetic"}],
        "evidence_refs": [],
        "evidence_hashes": [],
        "derived_from": [],
        "supersedes": [],
        "superseded_by": [],
        "conflicts": [],
        "active_until": None,
        "content_hash": "sha256:" + "1" * 64,
        "typed_payload": _status_payload(expires_at),
        "auto_policy_ready": True,
        "reason_capsule": {
            "rule_hits": [],
            "deterministic_signals": [],
            "evidence_gap": [],
            "model_reason": "synthetic",
            "policy_version": "p",
            "evaluator_version": "e",
            "review_block_reason": "",
        },
        "authorization_status": "active",
    }


@pytest.mark.parametrize("value", VALID)
def test_status_expiry_accepts_exact_pg_readable_set(value):
    validate_status_expires_at(value)
    validate_typed_payload("status", _status_payload(value), strict_status_expiry=True)
    validate_typed_payload("status", _status_payload(value))


@pytest.mark.parametrize("value", INVALID)
def test_status_expiry_rejects_values_pg_authority_filter_cannot_read(value):
    with pytest.raises(ValueError, match="expires_at"):
        validate_status_expires_at(value)
    # Proposal/read paths must retain an existing bad card for review.
    validate_typed_payload("status", _status_payload(value))
    # Canonical commit/write must be fail-closed.
    with pytest.raises(ValueError, match="expires_at"):
        validate_typed_payload("status", _status_payload(value), strict_status_expiry=True)


def test_existing_malformed_card_remains_readable_and_reviewable():
    card = _accepted_card("until next deploy")

    # Read/list path used by brain.query.
    readable = validate_memory_card_envelope(card)
    assert readable["typed_payload"]["expires_at"] == "until next deploy"
    assert _normalize_query_memory_card(brain_id="/project/neurons", card=card)

    # Repair/projection eligibility read path.
    assert _require_projectable_card(card)

    # Approval/commit path is blocked; existing data is not silently rewritten.
    with pytest.raises(ValueError, match="expires_at"):
        validate_memory_card_envelope(card, strict_status_expiry=True)
    assert classify_candidate_block_reason(card) == "invalid_status_expires_at"


def test_empty_expiry_remains_no_expiry():
    validate_status_expires_at("")
    validate_typed_payload("status", _status_payload(""), strict_status_expiry=True)


@pytest.mark.parametrize(
    "value",
    [
        "2026-11-20 12:00:00+00:00",
        "until next deploy",
        "2999-01-01T23:59:60.5Z",
        "2999-01-01T12:00:00." + "0" * 200 + "Z",
    ],
)
def test_auto_accept_blocks_non_pg_readable_expiry(value):
    candidate = _accepted_card(value)
    candidate["lifecycle_state"] = "needs_review"
    candidate["judgment_state"] = "needs_review"
    candidate["status"] = "candidate"
    candidate["approval_state"] = "suggested"
    candidate["authorization_status"] = "disabled"
    assert classify_candidate_block_reason(candidate) == "invalid_status_expires_at"
