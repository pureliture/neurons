"""native-memory mirror governance policy.

card_type 별 위험 등급은 audit/context 우선순위에만 쓴다. RAGFlow Memory mirror
write는 모든 card_type에서 명시 승인, provenance pass, eval pass가 필요하다.
어휘는 miner 의 memory_card.CANDIDATE_TYPES 를 SoT 로 재사용한다(새 어휘 발명 금지).
RAGFlow search hit 의 message_type(raw/semantic/...)은 별개 어휘이며 tier 계산에 쓰지 않는다.
"""
from __future__ import annotations

# 정본 "profile-changing 은 수동 승인" + "risk/constraint 는 운영 안전 경계" 규정과 정합.
HIGH_RISK_CARD_TYPES = frozenset({"user_preference", "project_decision", "risk_or_constraint"})


def governance_tier(card_type: str) -> str:
    """card_type → 'high' | 'low'. 알 수 없는/빈 type = 'high'(fail-closed)."""
    from .memory_card import CANDIDATE_TYPES

    if card_type in HIGH_RISK_CARD_TYPES:
        return "high"
    if card_type in CANDIDATE_TYPES:  # 명시적으로 알려진 low-risk 만 low
        return "low"
    return "high"  # 미지정/알 수 없는 type fail-closed


def mirror_requires_approval(card_type: str) -> bool:
    governance_tier(card_type)
    return True


def mirror_prerequisite_block_reason(
    *,
    approved: bool,
    provenance_status: str,
    eval_status: str,
) -> str:
    if not approved:
        return "operator_approval_required"
    if provenance_status != "pass":
        return "provenance_required"
    if eval_status != "pass":
        return "eval_required"
    return ""
