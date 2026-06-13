from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping

from .memory_card import validate_feedback_record, validate_memory_card_envelope


ACCEPTED_EVIDENCE_KINDS = ("commit", "merge", "runtime", "runtime_verification")


def human_approve_memory_card_candidate(
    candidate: Mapping[str, Any],
    *,
    approved_by: str,
    decision_id: str,
    artifact_id: str = "",
    user_reason: str | None = None,
    timestamp: str | None = None,
) -> dict:
    """Build a human-accepted MemoryCard and feedback record without persisting it."""

    source = validate_memory_card_envelope(candidate)
    approved_at = timestamp or _now()
    accepted = deepcopy(source)
    accepted.update(
        {
            "lifecycle_state": "human_accepted",
            "judgment_state": "none",
            "status": "accepted",
            "approval_state": "approved",
            "freshness": "current",
            "currentness": "current",
            "approved_by": approved_by,
            "approved_at": approved_at,
        }
    )
    validate_memory_card_envelope(accepted)
    feedback = build_feedback_record(
        candidate=accepted,
        decision_id=decision_id,
        artifact_id=artifact_id,
        proposed_status=str(source.get("status") or "candidate"),
        final_status="accepted",
        user_action="approve",
        user_reason=user_reason,
        model_reason="Human approval accepted the candidate.",
        confidence=float(accepted.get("confidence") or 0),
        conflict_state="none",
        timestamp=approved_at,
    )
    return {
        "schema_version": "llm_brain_human_approval_result.v1",
        "promotion_path": "human_approval",
        "write_performed": False,
        "accepted_card": accepted,
        "feedback_record": feedback,
    }


def human_reject_memory_card_candidate(
    candidate: Mapping[str, Any],
    *,
    rejected_by: str,
    decision_id: str,
    reason: str,
    artifact_id: str = "",
    timestamp: str | None = None,
) -> dict:
    """Build a human-rejected MemoryCard candidate result without persisting it."""

    source = validate_memory_card_envelope(candidate)
    rejected_at = timestamp or _now()
    rejected = deepcopy(source)
    rejected.update(
        {
            "lifecycle_state": "human_rejected",
            "judgment_state": "none",
            "status": "rejected",
            "approval_state": "rejected",
            "rejected_by": rejected_by,
            "rejected_at": rejected_at,
        }
    )
    validate_memory_card_envelope(rejected)
    feedback = build_feedback_record(
        candidate=rejected,
        decision_id=decision_id,
        artifact_id=artifact_id,
        proposed_status=str(source.get("status") or "candidate"),
        final_status="rejected",
        user_action="reject",
        user_reason=reason,
        model_reason="Human rejected the candidate.",
        confidence=float(rejected.get("confidence") or 0),
        conflict_state="none",
        timestamp=rejected_at,
    )
    return {
        "schema_version": "llm_brain_human_rejection_result.v1",
        "promotion_path": "human_rejection",
        "write_performed": False,
        "rejected_card": rejected,
        "feedback_record": feedback,
    }


def suggest_accept_from_evidence(
    candidate: Mapping[str, Any],
    *,
    evidence: Mapping[str, Any],
    decision_id: str,
    artifact_id: str = "",
    timestamp: str | None = None,
) -> dict:
    """Turn accepted evidence into suggested_accept under initial policy mode."""

    source = validate_memory_card_envelope(candidate)
    evidence_kind = str(evidence.get("evidence_kind") or "")
    if evidence_kind not in ACCEPTED_EVIDENCE_KINDS:
        raise ValueError("accepted evidence must be commit, merge, runtime, or runtime_verification")
    if str(evidence.get("decision_id") or "") != decision_id:
        raise ValueError("accepted evidence enrichment requires direct decision_id reference")
    content_hash = str(evidence.get("content_hash") or "")
    if content_hash and not content_hash.startswith("sha256:"):
        raise ValueError("accepted evidence content_hash must be sha256 when present")
    evidence_ref = dict(evidence.get("source_ref") or {"evidence_id": str(evidence.get("evidence_id") or evidence_kind)})
    evidence_ref.update(
        {
            "evidence_kind": evidence_kind,
            "decision_id": decision_id,
        }
    )
    if content_hash:
        evidence_ref["content_hash"] = content_hash
    suggested = deepcopy(source)
    if content_hash and content_hash not in suggested["evidence_hashes"]:
        suggested["evidence_hashes"].append(content_hash)
    suggested["evidence_refs"].append(evidence_ref)
    suggested.update(
        {
            "lifecycle_state": "suggested_accept",
            "judgment_state": "suggested_status",
            "status": "suggested_accept",
            "approval_state": "suggested",
            "freshness": "recent",
            "currentness": "unknown",
            "reason_capsule": _reason_capsule(
                model_reason="Accepted evidence supports promotion, but initial policy requires review.",
                policy_version=str(evidence.get("policy_version") or "policy.v0"),
                evaluator_version=str(evidence.get("evaluator_version") or "eval.v0"),
                deterministic_signals=[{"kind": evidence_kind, "content_hash": content_hash}],
                review_block_reason="initial_policy_human_approval_required",
            ),
        }
    )
    validate_memory_card_envelope(suggested)
    return {
        "schema_version": "llm_brain_suggested_accept_result.v1",
        "promotion_path": "accepted_evidence_suggested_accept",
        "write_performed": False,
        "requires_human_review": True,
        "suggested_card": suggested,
        "feedback_record": None,
        "decision_id": decision_id,
        "artifact_id": artifact_id,
        "observed_at": timestamp or _now(),
    }


def suggest_superseded_classification(
    candidate: Mapping[str, Any],
    *,
    superseded_by: str,
    decision_id: str,
    reason: str,
    artifact_id: str = "",
    timestamp: str | None = None,
) -> dict:
    """Create a suggested_superseded judgment without changing current truth."""

    source = validate_memory_card_envelope(candidate)
    suggested = deepcopy(source)
    suggested.update(
        {
            "lifecycle_state": "candidate",
            "judgment_state": "suggested_superseded",
            "status": "suggested_superseded",
            "approval_state": "suggested",
            "freshness": "historical",
            "currentness": "superseded",
            "superseded_by": [superseded_by],
            "reason_capsule": _reason_capsule(
                model_reason=reason,
                deterministic_signals=[{"kind": "supersession_candidate", "superseded_by": superseded_by}],
                review_block_reason="initial_policy_human_approval_required",
            ),
        }
    )
    validate_memory_card_envelope(suggested)
    return {
        "schema_version": "llm_brain_suggested_superseded_result.v1",
        "promotion_path": "suggested_superseded",
        "write_performed": False,
        "requires_human_review": True,
        "accepted_truth_changed": False,
        "suggested_card": suggested,
        "feedback_record": None,
        "decision_id": decision_id,
        "artifact_id": artifact_id,
        "observed_at": timestamp or _now(),
        "notification_state": "not_applicable_until_auto_accepted",
    }


def mark_candidate_needs_review(
    candidate: Mapping[str, Any],
    *,
    reason: str,
    decision_id: str,
    artifact_id: str = "",
    conflict_state: str = "none",
    timestamp: str | None = None,
) -> dict:
    source = validate_memory_card_envelope(candidate)
    reviewed = deepcopy(source)
    reviewed.update(
        {
            "lifecycle_state": "needs_review",
            "judgment_state": "needs_review",
            "status": "needs_review",
            "approval_state": "needs_review",
            "reason_capsule": _reason_capsule(
                model_reason=reason,
                deterministic_signals=[{"kind": "review_gate"}],
                review_block_reason=reason,
            ),
        }
    )
    validate_memory_card_envelope(reviewed)
    feedback = build_feedback_record(
        candidate=reviewed,
        decision_id=decision_id,
        artifact_id=artifact_id,
        proposed_status=str(source.get("status") or "candidate"),
        final_status="needs_review",
        user_action="correct",
        user_reason=None,
        corrected_status="needs_review",
        correction_reason=reason,
        model_reason=reason,
        confidence=float(reviewed.get("confidence") or 0),
        conflict_state=conflict_state,
        timestamp=timestamp or _now(),
    )
    return {
        "schema_version": "llm_brain_needs_review_result.v1",
        "promotion_path": "needs_review",
        "write_performed": False,
        "review_card": reviewed,
        "feedback_record": feedback,
    }


def build_feedback_record(
    *,
    candidate: Mapping[str, Any],
    decision_id: str,
    proposed_status: str,
    final_status: str,
    user_action: str,
    model_reason: str,
    confidence: float,
    conflict_state: str,
    timestamp: str,
    artifact_id: str = "",
    user_reason: str | None = None,
    corrected_status: str | None = None,
    correction_reason: str | None = None,
) -> dict:
    record = {
        "feedback_id": "fb_" + str(candidate.get("memory_id") or "candidate"),
        "decision_id": decision_id,
        "memory_id": str(candidate.get("memory_id") or ""),
        "repo_id": str(candidate.get("project") or ""),
        "artifact_id": artifact_id,
        "proposed_status": proposed_status,
        "final_status": final_status,
        "user_action": user_action,
        "user_reason": user_reason,
        "corrected_status": corrected_status,
        "correction_reason": correction_reason,
        "model_reason": model_reason,
        "confidence": confidence,
        "deterministic_signals": list(
            ((candidate.get("reason_capsule") or {}).get("deterministic_signals") or [])
            if isinstance(candidate.get("reason_capsule"), Mapping)
            else []
        ),
        "evidence_snapshot": {
            "evidence_hashes": list(candidate.get("evidence_hashes") or []),
            "source_ref_count": len(candidate.get("source_refs") or []),
        },
        "source_refs": list(candidate.get("source_refs") or []),
        "conflict_state": conflict_state,
        "policy_version": str((candidate.get("reason_capsule") or {}).get("policy_version") or "policy.v0")
        if isinstance(candidate.get("reason_capsule"), Mapping)
        else "policy.v0",
        "evaluator_version": str((candidate.get("reason_capsule") or {}).get("evaluator_version") or "eval.v0")
        if isinstance(candidate.get("reason_capsule"), Mapping)
        else "eval.v0",
        "timestamp": timestamp,
    }
    return validate_feedback_record(record)


def _reason_capsule(
    *,
    model_reason: str,
    deterministic_signals: list[dict],
    policy_version: str = "policy.v0",
    evaluator_version: str = "eval.v0",
    review_block_reason: str | None = None,
) -> dict:
    return {
        "rule_hits": [],
        "deterministic_signals": deterministic_signals,
        "evidence_gap": [],
        "model_reason": model_reason,
        "policy_version": policy_version,
        "evaluator_version": evaluator_version,
        "review_block_reason": review_block_reason,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
