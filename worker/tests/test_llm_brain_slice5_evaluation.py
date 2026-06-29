from __future__ import annotations

from agent_knowledge.session_memory.memory_miner import build_memory_card_candidate_from_source_span
from agent_knowledge.session_memory.memory_evaluation import (
    apply_auto_acceptance_plan,
    build_policy_version,
    evaluate_candidate_for_auto_policy,
    rollback_auto_policy_candidate,
    summarize_feedback_patterns,
    validate_auto_policy_operation,
)
from agent_knowledge.session_memory.memory_promotion import build_feedback_record, suggest_accept_from_evidence


PROJECT = "workspace-ragflow-advisor"


def _source_span(**overrides):
    span = {
        "source_ref": {"source_id": "src_eval"},
        "span_ref": {"span_id": "span_eval"},
        "content_hash": "sha256:evaluation",
        "brain_id": f"/project/{PROJECT}",
        "card_type": "decision",
        "scope": "project",
        "project": PROJECT,
        "provider": "codex",
        "title": "Evaluation policy",
        "redacted_summary": "Evaluation can mark auto-accept eligibility without applying truth.",
        "typed_payload": {
            "decision": "Auto policy needs readiness and explicit application gate.",
            "rationale": "User feedback patterns must calibrate policy behavior.",
            "alternatives": ["Auto-accept immediately"],
            "consequence": "Eligible candidates remain blocked until explicit approval.",
            "authority_ref": "session-memory-decision-5",
        },
        "confidence": 0.91,
        "confidence_basis": "high-confidence redacted decision",
    }
    span.update(overrides)
    return span


def _drift_payload(*, severity="high"):
    return {
        "subject": "Architecture currentness",
        "expected_state": "Latest session-memory accepted decision is authority.",
        "observed_state": "A historical architecture doc conflicts with it.",
        "drift_kind": "authority_conflict",
        "severity": severity,
        "authority_lane": "design",
        "source_precedence_rank": 1,
        "resolution_action": "needs_review",
        "suggested_action": "Ask for review before changing current truth.",
        "basis_refs": [{"source_id": "src_eval_drift", "span_id": "span_eval_drift"}],
    }


def _candidate(**overrides):
    return build_memory_card_candidate_from_source_span(
        _source_span(**overrides), refresh_watermark="w5"
    )


def _suggested_accept_candidate(**overrides):
    candidate = _candidate(**overrides)
    return suggest_accept_from_evidence(
        candidate,
        evidence={
            "evidence_kind": "commit",
            "decision_id": "decision_auto_accept",
            "content_hash": "sha256:auto-evidence",
            "source_ref": {"evidence_id": "commit_auto_accept"},
        },
        decision_id="decision_auto_accept",
    )["suggested_card"]


def _feedback_records(candidate, *, count=5, action="approve"):
    return [
        build_feedback_record(
            candidate=candidate,
            decision_id=f"decision_{index}",
            proposed_status="suggested_accept",
            final_status="accepted" if action == "approve" else "rejected",
            user_action=action,
            model_reason="feedback sample",
            confidence=0.9,
            conflict_state="none",
            timestamp=f"2026-06-13T00:0{index}:00+00:00",
        )
        for index in range(count)
    ]


def test_feedback_pattern_summary_counts_user_actions():
    candidate = _candidate()
    records = [
        *_feedback_records(candidate, count=3, action="approve"),
        *_feedback_records(candidate, count=1, action="reject"),
    ]

    summary = summarize_feedback_patterns(records)

    assert summary["feedback_count"] == 4
    assert summary["approved_count"] == 3
    assert summary["rejected_count"] == 1
    assert summary["approval_rate"] == 0.75


def test_auto_policy_readiness_failure_routes_to_needs_review():
    candidate = _suggested_accept_candidate()
    result = evaluate_candidate_for_auto_policy(
        candidate,
        feedback_records=_feedback_records(candidate, count=2),
        policy=build_policy_version(min_feedback_count=5),
    )

    assert result["decision"] == "needs_review"
    assert result["reason"] == "policy_readiness_insufficient_feedback"
    assert result["accepted_truth_changed"] is False
    assert result["review_result"]["review_card"]["approval_state"] == "needs_review"


def test_auto_policy_eligible_does_not_apply_accepted_truth_by_default():
    candidate = _suggested_accept_candidate()
    evaluation = evaluate_candidate_for_auto_policy(
        candidate,
        feedback_records=_feedback_records(candidate, count=6),
        policy=build_policy_version(min_feedback_count=5, min_approval_rate=0.8),
    )

    assert evaluation["decision"] == "auto_accept_eligible"
    assert evaluation["accepted_truth_changed"] is False
    assert evaluation["requires_execution_approval"] is True
    assert evaluation["suggested_judgment_state"] == "auto_status"
    assert evaluation["accepted_evidence_basis"]["decision_id"] == "decision_auto_accept"
    assert evaluation["reason_capsule"]["review_block_reason"] == (
        "auto_acceptance_requires_explicit_user_judgment"
    )

    application = apply_auto_acceptance_plan(candidate, evaluation)
    assert application["status"] == "blocked_pending_user_judgment"
    assert application["accepted_truth_changed"] is False
    assert application["write_performed"] is False

    explicit_attempt = apply_auto_acceptance_plan(candidate, evaluation, allow_auto_accept=True)
    assert explicit_attempt["status"] == "blocked_missing_operator_approval_ref"
    assert explicit_attempt["accepted_truth_changed"] is False

    approved_application = apply_auto_acceptance_plan(
        candidate,
        evaluation,
        allow_auto_accept=True,
        operator_approval_ref="user-approved-all-stop-conditions",
    )
    assert approved_application["status"] == "auto_accepted"
    assert approved_application["accepted_truth_changed"] is True
    assert approved_application["write_performed"] is False
    assert approved_application["accepted_card"]["approval_state"] == "auto_accepted"


def test_auto_policy_requires_direct_accepted_evidence_before_auto_acceptance():
    candidate = _candidate()
    result = evaluate_candidate_for_auto_policy(
        candidate,
        feedback_records=_feedback_records(candidate, count=6),
        policy=build_policy_version(min_feedback_count=5, min_approval_rate=0.8),
    )

    assert result["decision"] == "needs_review"
    assert result["reason"] == "missing_accepted_evidence"
    assert result["accepted_truth_changed"] is False

    forged_evaluation = {
        "decision": "auto_accept_eligible",
        "reason_capsule": {"policy_version": "policy.v0"},
        "accepted_evidence_basis": {},
    }
    application = apply_auto_acceptance_plan(
        candidate,
        forged_evaluation,
        allow_auto_accept=True,
        operator_approval_ref="user-approved-all-stop-conditions",
    )
    assert application["status"] == "blocked_missing_accepted_evidence"


def test_conflict_high_impact_and_low_confidence_always_need_review():
    feedback = _feedback_records(_suggested_accept_candidate(), count=6)

    conflicted = _candidate()
    conflicted["conflicts"] = [{"memory_id": "mem_other"}]
    assert evaluate_candidate_for_auto_policy(conflicted, feedback_records=feedback)["reason"] == "conflict"

    high_impact = _candidate(governance_tier="high")
    assert (
        evaluate_candidate_for_auto_policy(high_impact, feedback_records=feedback)["reason"]
        == "high_impact_authority_change"
    )

    low_confidence = _suggested_accept_candidate(confidence=0.4)
    assert (
        evaluate_candidate_for_auto_policy(low_confidence, feedback_records=feedback)["reason"]
        == "low_confidence"
    )

    high_drift = _candidate(
        card_type="drift",
        title="High severity drift",
        typed_payload=_drift_payload(severity="high"),
        content_hash="sha256:high-drift",
    )
    assert (
        evaluate_candidate_for_auto_policy(high_drift, feedback_records=feedback)["reason"]
        == "high_severity_drift"
    )


def test_auto_policy_rollback_returns_needs_review_without_truth_change():
    result = rollback_auto_policy_candidate(_candidate(), reason="misfire_detected")

    assert result["write_performed"] is False
    assert result["review_card"]["lifecycle_state"] == "needs_review"
    assert result["review_card"]["reason_capsule"]["review_block_reason"] == "misfire_detected"


def test_auto_policy_forbidden_operations_are_rejected():
    for operation in (
        "memory_delete",
        "live_gc_execute",
        "ragflow_dataset_delete",
        "ragflow_dataset_disable",
        "private_transcript_raw_exposure",
        "secret_source_return",
        "runtime_mutation_execute",
    ):
        try:
            validate_auto_policy_operation(operation)
        except ValueError as exc:
            assert "forbidden operation" in str(exc)
        else:
            raise AssertionError(f"{operation} should be forbidden")

    allowed = validate_auto_policy_operation("suggested_status")
    assert allowed["allowed_for_auto_policy"] is True


def test_apply_auto_acceptance_blocks_forbidden_requested_operations():
    candidate = _suggested_accept_candidate()
    evaluation = evaluate_candidate_for_auto_policy(
        candidate,
        feedback_records=_feedback_records(candidate, count=6),
        policy=build_policy_version(min_feedback_count=5),
    )

    application = apply_auto_acceptance_plan(
        candidate,
        {**evaluation, "requested_operations": ["runtime_mutation_execute"]},
        allow_auto_accept=True,
        operator_approval_ref="user-approved-all-stop-conditions",
    )

    assert application["status"] == "blocked_forbidden_operation"
    assert application["accepted_truth_changed"] is False
