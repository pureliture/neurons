from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .memory_card import validate_feedback_record, validate_memory_card_envelope
from .memory_promotion import ACCEPTED_EVIDENCE_KINDS, mark_candidate_needs_review


DEFAULT_POLICY = {
    "policy_version": "policy.v0",
    "evaluator_version": "eval.v0",
    "min_feedback_count": 5,
    "min_approval_rate": 0.8,
    "confidence_threshold": 0.85,
    "allowed_governance_tiers": ("low", "medium"),
    "allowed_card_types": ("decision", "task", "drift", "preference", "status", "evidence"),
}
FORBIDDEN_AUTO_POLICY_OPERATIONS = (
    "memory_delete",
    "live_gc_execute",
    "ragflow_dataset_delete",
    "ragflow_dataset_disable",
    "private_transcript_raw_exposure",
    "secret_source_return",
    "runtime_mutation_execute",
)


def build_policy_version(**overrides) -> dict:
    policy = dict(DEFAULT_POLICY)
    policy.update(overrides)
    return policy


def validate_auto_policy_operation(operation: str) -> dict:
    operation = str(operation or "")
    if operation in FORBIDDEN_AUTO_POLICY_OPERATIONS:
        raise ValueError(f"auto policy cannot perform forbidden operation: {operation}")
    return {
        "operation": operation,
        "allowed_for_auto_policy": True,
        "forbidden_operations": list(FORBIDDEN_AUTO_POLICY_OPERATIONS),
    }


def summarize_feedback_patterns(records: list[Mapping[str, Any]]) -> dict:
    validated = [validate_feedback_record(record) for record in records]
    approved = sum(1 for record in validated if record["user_action"] == "approve")
    rejected = sum(1 for record in validated if record["user_action"] == "reject")
    corrected = sum(1 for record in validated if record["user_action"] == "correct")
    total = len(validated)
    approval_rate = approved / total if total else 0.0
    return {
        "schema_version": "llm_brain_feedback_pattern_summary.v1",
        "feedback_count": total,
        "approved_count": approved,
        "rejected_count": rejected,
        "corrected_count": corrected,
        "approval_rate": approval_rate,
        "policy_training_ready": total > 0,
    }


def evaluate_candidate_for_auto_policy(
    candidate: Mapping[str, Any],
    *,
    feedback_records: list[Mapping[str, Any]],
    policy: Mapping[str, Any] | None = None,
) -> dict:
    card = validate_memory_card_envelope(candidate)
    active_policy = build_policy_version(**dict(policy or {}))
    summary = summarize_feedback_patterns(feedback_records)
    forced_review_reason = _forced_review_reason(card, active_policy)
    readiness_reason = _readiness_failure_reason(summary, active_policy)
    if forced_review_reason or readiness_reason:
        reason = forced_review_reason or readiness_reason or "needs_review"
        review = mark_candidate_needs_review(
            card,
            reason=reason,
            decision_id=_decision_id_for_card(card),
            conflict_state="conflict" if reason == "conflict" else "none",
        )
        return {
            "schema_version": "llm_brain_auto_policy_evaluation.v1",
            "decision": "needs_review",
            "reason": reason,
            "policy": active_policy,
            "feedback_summary": summary,
            "accepted_truth_changed": False,
            "review_result": review,
            "reason_capsule": review["review_card"]["reason_capsule"],
        }
    accepted_evidence_basis = _accepted_evidence_basis(card)
    reason_capsule = {
        "rule_hits": ["policy_readiness_passed", "accepted_evidence_present", "low_risk_high_confidence"],
        "deterministic_signals": [
            {"kind": "feedback_count", "value": summary["feedback_count"]},
            {"kind": "approval_rate", "value": summary["approval_rate"]},
            {"kind": "confidence", "value": card["confidence"]},
            {
                "kind": "accepted_evidence",
                "decision_id": accepted_evidence_basis["decision_id"],
                "evidence_kind": accepted_evidence_basis["evidence_kind"],
            },
        ],
        "evidence_gap": [],
        "model_reason": "Candidate is eligible for future auto acceptance, but application is gated.",
        "policy_version": str(active_policy["policy_version"]),
        "evaluator_version": str(active_policy["evaluator_version"]),
        "review_block_reason": "auto_acceptance_requires_explicit_user_judgment",
    }
    return {
        "schema_version": "llm_brain_auto_policy_evaluation.v1",
        "decision": "auto_accept_eligible",
        "reason": "readiness_passed",
        "policy": active_policy,
        "feedback_summary": summary,
        "accepted_truth_changed": False,
        "requires_execution_approval": True,
        "suggested_judgment_state": "auto_status",
        "accepted_evidence_basis": accepted_evidence_basis,
        "reason_capsule": reason_capsule,
    }


def apply_auto_acceptance_plan(
    candidate: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    *,
    allow_auto_accept: bool = False,
    operator_approval_ref: str = "",
    requested_operations: list[str] | None = None,
) -> dict:
    try:
        _validate_requested_auto_operations(
            [*list(evaluation.get("requested_operations") or []), *list(requested_operations or [])]
        )
    except ValueError as exc:
        return {
            "schema_version": "llm_brain_auto_acceptance_application.v1",
            "status": "blocked_forbidden_operation",
            "accepted_truth_changed": False,
            "write_performed": False,
            "reason": str(exc),
        }
    if evaluation.get("decision") != "auto_accept_eligible":
        return {
            "schema_version": "llm_brain_auto_acceptance_application.v1",
            "status": "not_eligible",
            "accepted_truth_changed": False,
            "write_performed": False,
        }
    if not _accepted_evidence_basis(validate_memory_card_envelope(candidate)) or not evaluation.get("accepted_evidence_basis"):
        return {
            "schema_version": "llm_brain_auto_acceptance_application.v1",
            "status": "blocked_missing_accepted_evidence",
            "accepted_truth_changed": False,
            "write_performed": False,
            "reason": "auto acceptance requires direct accepted evidence basis",
        }
    if not allow_auto_accept:
        return {
            "schema_version": "llm_brain_auto_acceptance_application.v1",
            "status": "blocked_pending_user_judgment",
            "accepted_truth_changed": False,
            "write_performed": False,
            "reason": "auto policy accepted/current truth confirmation requires explicit user judgment",
        }
    if not operator_approval_ref:
        return {
            "schema_version": "llm_brain_auto_acceptance_application.v1",
            "status": "blocked_missing_operator_approval_ref",
            "accepted_truth_changed": False,
            "write_performed": False,
            "reason": "operator_approval_ref is required for auto accepted/current truth confirmation",
        }
    card = deepcopy(validate_memory_card_envelope(candidate))
    card.update(
        {
            "lifecycle_state": "auto_accepted",
            "judgment_state": "auto_status",
            "approval_state": "auto_accepted",
            "status": "accepted",
            "freshness": "current",
            "currentness": "current",
            "auto_policy_ready": True,
            "operator_approval_ref": operator_approval_ref,
            "reason_capsule": dict(evaluation["reason_capsule"]),
        }
    )
    validate_memory_card_envelope(card)
    return {
        "schema_version": "llm_brain_auto_acceptance_application.v1",
        "status": "auto_accepted",
        "accepted_truth_changed": True,
        "write_performed": False,
        "accepted_card": card,
    }


def rollback_auto_policy_candidate(candidate: Mapping[str, Any], *, reason: str) -> dict:
    return mark_candidate_needs_review(
        candidate,
        reason=reason,
        decision_id=_decision_id_for_card(candidate),
        conflict_state="none",
    )


def _readiness_failure_reason(summary: Mapping[str, Any], policy: Mapping[str, Any]) -> str:
    if int(summary["feedback_count"]) < int(policy["min_feedback_count"]):
        return "policy_readiness_insufficient_feedback"
    if float(summary["approval_rate"]) < float(policy["min_approval_rate"]):
        return "policy_readiness_low_approval_rate"
    return ""


def _forced_review_reason(card: Mapping[str, Any], policy: Mapping[str, Any]) -> str:
    if not card.get("source_refs"):
        return "missing_source_refs"
    if card.get("conflicts"):
        return "conflict"
    if card.get("currentness") == "conflicted":
        return "conflict"
    payload = card.get("typed_payload") or {}
    if card.get("card_type") == "drift" and payload.get("severity") == "high":
        return "high_severity_drift"
    if card.get("governance_tier") not in tuple(policy["allowed_governance_tiers"]):
        return "high_impact_authority_change"
    if card.get("card_type") not in tuple(policy["allowed_card_types"]):
        return "high_impact_authority_change"
    if float(card.get("confidence") or 0) < float(policy["confidence_threshold"]):
        return "low_confidence"
    if card.get("currentness") == "superseded":
        return "high_impact_authority_change"
    if not _accepted_evidence_basis(card):
        return "missing_accepted_evidence"
    return ""


def _accepted_evidence_basis(card: Mapping[str, Any]) -> dict:
    if str(card.get("lifecycle_state") or "") != "suggested_accept":
        return {}
    for evidence_ref in card.get("evidence_refs") or []:
        if not isinstance(evidence_ref, Mapping):
            continue
        evidence_kind = str(evidence_ref.get("evidence_kind") or "")
        decision_id = str(evidence_ref.get("decision_id") or "")
        if evidence_kind in ACCEPTED_EVIDENCE_KINDS and decision_id:
            return {
                "evidence_kind": evidence_kind,
                "decision_id": decision_id,
                "content_hash": str(evidence_ref.get("content_hash") or ""),
            }
    return {}


def _decision_id_for_card(card: Mapping[str, Any]) -> str:
    basis = _accepted_evidence_basis(card)
    if basis.get("decision_id"):
        return str(basis["decision_id"])
    for ref in [*list(card.get("evidence_refs") or []), *list(card.get("source_refs") or [])]:
        if isinstance(ref, Mapping) and ref.get("decision_id"):
            return str(ref["decision_id"])
    return str(card.get("memory_id") or "")


def _validate_requested_auto_operations(operations: list[Any]) -> None:
    for operation in operations:
        validate_auto_policy_operation(str(operation or ""))
