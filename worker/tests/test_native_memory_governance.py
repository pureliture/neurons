from __future__ import annotations

from agent_knowledge.session_memory.memory_card import CANDIDATE_TYPES
from agent_knowledge.session_memory.native_memory_governance import (
    HIGH_RISK_CARD_TYPES,
    governance_tier,
    mirror_prerequisite_block_reason,
    mirror_requires_approval,
)


def test_high_risk_types_are_high():
    for card_type in ("user_preference", "project_decision", "risk_or_constraint"):
        assert governance_tier(card_type) == "high"


def test_low_risk_types_are_low():
    for card_type in ("semantic_fact", "procedural_rule", "tool_skill", "unresolved_task"):
        assert governance_tier(card_type) == "low"


def test_unknown_and_empty_type_fail_closed_high():
    assert governance_tier("") == "high"
    assert governance_tier("totally_unknown_type") == "high"


def test_mirror_requires_approval_matches_tier():
    assert mirror_requires_approval("user_preference") is True
    assert mirror_requires_approval("semantic_fact") is True
    assert mirror_requires_approval("") is True


def test_mirror_prerequisites_require_approval_provenance_and_eval():
    assert mirror_prerequisite_block_reason(approved=False, provenance_status="pass", eval_status="pass") == (
        "operator_approval_required"
    )
    assert mirror_prerequisite_block_reason(approved=True, provenance_status="missing", eval_status="pass") == (
        "provenance_required"
    )
    assert mirror_prerequisite_block_reason(approved=True, provenance_status="pass", eval_status="fail") == (
        "eval_required"
    )
    assert mirror_prerequisite_block_reason(approved=True, provenance_status="pass", eval_status="pass") == ""


def test_high_risk_is_subset_of_candidate_types():
    # CANDIDATE_TYPES(miner SoT)가 확장/리네임되면 분류표가 어긋남을 조기 감지.
    assert HIGH_RISK_CARD_TYPES <= set(CANDIDATE_TYPES)


def test_every_candidate_type_classified():
    # 모든 알려진 card_type 은 high 또는 low 로 분류된다(누락 없음).
    for card_type in CANDIDATE_TYPES:
        assert governance_tier(card_type) in {"high", "low"}
