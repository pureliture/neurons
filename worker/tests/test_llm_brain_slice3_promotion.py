from __future__ import annotations

import pytest

from agent_knowledge.memory_miner import build_memory_card_candidate_from_source_span
from agent_knowledge.session_memory.memory_promotion import (
    build_feedback_record,
    human_approve_memory_card_candidate,
    human_reject_memory_card_candidate,
    mark_candidate_needs_review,
    suggest_accept_from_evidence,
    suggest_superseded_classification,
)


PROJECT = "workspace-ragflow-advisor"


def _source_span(**overrides):
    span = {
        "source_ref": {"source_id": "src_promotion"},
        "span_ref": {"span_id": "span_promotion"},
        "content_hash": "sha256:promotion",
        "brain_id": f"/project/{PROJECT}",
        "card_type": "decision",
        "scope": "project",
        "project": PROJECT,
        "provider": "codex",
        "title": "Promotion policy",
        "redacted_summary": "Human approval is the initial accepted path.",
        "typed_payload": {
            "decision": "Initial accepted MemoryCard promotion requires human approval.",
            "rationale": "Accepted evidence starts as suggested_accept while policy matures.",
            "alternatives": ["Auto-accept all commit evidence"],
            "consequence": "Feedback data can calibrate future automation.",
            "authority_ref": "session-memory-decision-3",
        },
        "confidence": 0.82,
        "confidence_basis": "redacted session-memory decision",
    }
    span.update(overrides)
    return span


def _candidate():
    return build_memory_card_candidate_from_source_span(_source_span(), refresh_watermark="w3")


def test_human_approval_builds_accepted_card_and_feedback_without_write():
    result = human_approve_memory_card_candidate(
        _candidate(),
        approved_by="ddalkak",
        decision_id="decision_3",
        artifact_id="design.md",
        timestamp="2026-06-13T00:00:00+00:00",
    )

    accepted = result["accepted_card"]
    feedback = result["feedback_record"]
    assert result["write_performed"] is False
    assert result["promotion_path"] == "human_approval"
    assert accepted["lifecycle_state"] == "human_accepted"
    assert accepted["approval_state"] == "approved"
    assert accepted["status"] == "accepted"
    assert accepted["currentness"] == "current"
    assert feedback["user_action"] == "approve"
    assert feedback["final_status"] == "accepted"
    assert feedback["memory_id"] == accepted["memory_id"]


def test_human_rejection_builds_rejected_card_and_feedback_without_write():
    result = human_reject_memory_card_candidate(
        _candidate(),
        rejected_by="ddalkak",
        decision_id="decision_3",
        reason="Not a durable decision.",
        artifact_id="design.md",
        timestamp="2026-06-13T00:00:30+00:00",
    )

    rejected = result["rejected_card"]
    feedback = result["feedback_record"]
    assert result["write_performed"] is False
    assert result["promotion_path"] == "human_rejection"
    assert rejected["lifecycle_state"] == "human_rejected"
    assert rejected["approval_state"] == "rejected"
    assert rejected["status"] == "rejected"
    assert feedback["user_action"] == "reject"
    assert feedback["user_reason"] == "Not a durable decision."


def test_accepted_evidence_creates_suggested_accept_not_current_truth():
    result = suggest_accept_from_evidence(
        _candidate(),
        evidence={
            "evidence_kind": "commit",
            "decision_id": "decision_3",
            "content_hash": "sha256:commit",
            "source_ref": {"source_id": "commit_123"},
        },
        decision_id="decision_3",
        artifact_id="commit:123",
        timestamp="2026-06-13T00:01:00+00:00",
    )

    suggested = result["suggested_card"]
    assert result["write_performed"] is False
    assert result["requires_human_review"] is True
    assert result["feedback_record"] is None
    assert suggested["lifecycle_state"] == "suggested_accept"
    assert suggested["judgment_state"] == "suggested_status"
    assert suggested["approval_state"] == "suggested"
    assert suggested["currentness"] == "unknown"
    assert "sha256:commit" in suggested["evidence_hashes"]
    assert suggested["reason_capsule"]["review_block_reason"] == "initial_policy_human_approval_required"


def test_accepted_evidence_rejects_unknown_or_unhashed_evidence():
    with pytest.raises(ValueError, match="accepted evidence"):
        suggest_accept_from_evidence(
            _candidate(),
            evidence={"evidence_kind": "slack_message", "content_hash": "sha256:x"},
            decision_id="decision_3",
        )
    with pytest.raises(ValueError, match="sha256"):
        suggest_accept_from_evidence(
            _candidate(),
            evidence={"evidence_kind": "commit", "decision_id": "decision_3", "content_hash": "not-a-hash"},
            decision_id="decision_3",
        )
    with pytest.raises(ValueError, match="decision_id"):
        suggest_accept_from_evidence(
            _candidate(),
            evidence={"evidence_kind": "commit", "decision_id": "other", "content_hash": "sha256:x"},
            decision_id="decision_3",
        )


def test_suggested_superseded_classification_does_not_change_current_truth():
    result = suggest_superseded_classification(
        _candidate(),
        superseded_by="mem_newer_decision",
        decision_id="decision_3",
        reason="A newer accepted decision replaces this candidate.",
        artifact_id="architecture.md",
        timestamp="2026-06-13T00:01:30+00:00",
    )

    suggested = result["suggested_card"]
    assert result["write_performed"] is False
    assert result["accepted_truth_changed"] is False
    assert result["requires_human_review"] is True
    assert result["feedback_record"] is None
    assert suggested["judgment_state"] == "suggested_superseded"
    assert suggested["approval_state"] == "suggested"
    assert suggested["currentness"] == "superseded"
    assert suggested["superseded_by"] == ["mem_newer_decision"]


def test_needs_review_path_preserves_review_reason_and_feedback():
    result = mark_candidate_needs_review(
        _candidate(),
        reason="conflicting evidence",
        decision_id="decision_3",
        artifact_id="design.md",
        conflict_state="conflict",
        timestamp="2026-06-13T00:02:00+00:00",
    )

    review_card = result["review_card"]
    feedback = result["feedback_record"]
    assert result["write_performed"] is False
    assert review_card["lifecycle_state"] == "needs_review"
    assert review_card["judgment_state"] == "needs_review"
    assert review_card["approval_state"] == "needs_review"
    assert review_card["reason_capsule"]["review_block_reason"] == "conflicting evidence"
    assert feedback["conflict_state"] == "conflict"
    assert feedback["corrected_status"] == "needs_review"


def test_feedback_record_capture_rejects_private_source_material():
    candidate = _candidate()
    candidate["source_refs"] = [{"path": "/Users/example/.codex/transcripts/raw.jsonl"}]

    with pytest.raises(ValueError, match="opaque|forbidden"):
        build_feedback_record(
            candidate=candidate,
            decision_id="decision_3",
            proposed_status="candidate",
            final_status="rejected",
            user_action="reject",
            model_reason="Private source leak.",
            confidence=0.4,
            conflict_state="none",
            timestamp="2026-06-13T00:03:00+00:00",
        )
