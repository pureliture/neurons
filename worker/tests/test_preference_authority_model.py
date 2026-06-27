from agent_knowledge.llm_brain_core.preference_authority import preference_rule_cards_from_memory_cards

from test_context_authority_pack import _card


def test_preference_rule_model_carries_scope_reason_evidence_and_currentness():
    card = _card(
        "mem_korean",
        "preference",
        "Korean response preference",
        {
            "preference": "자연어 응답과 문서는 한국어로 작성한다.",
            "applies_to": "natural_language_response",
            "reason": "User global communication preference.",
            "exceptions": ["code identifiers stay English"],
        },
    )
    card["source_refs"] = [{"source_ref_id": "session:global-policy", "kind": "session"}]

    [preference] = preference_rule_cards_from_memory_cards([card])

    assert preference == {
        "memory_id": "mem_korean",
        "rule": "자연어 응답과 문서는 한국어로 작성한다.",
        "scope": "natural_language_response",
        "reason": "User global communication preference.",
        "confidence": 0.9,
        "currentness": "current",
        "evidence_refs": ["mem_korean", "session:global-policy"],
        "exceptions": ["code identifiers stay English"],
    }


def test_preference_rule_model_does_not_consume_workflow_contracts():
    workflow = _card(
        "mem_workflow",
        "workflow_contract",
        "Use dedicated worktrees before edits",
        {
            "rule": "Use a dedicated branch/worktree before repository edits.",
            "applies_to": "code-changing work",
        },
    )

    assert preference_rule_cards_from_memory_cards([workflow]) == []
