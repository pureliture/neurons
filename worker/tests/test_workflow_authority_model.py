from agent_knowledge.llm_brain_core.workflow_authority import (
    skill_evolution_cards_from_memory_cards,
    workflow_contract_cards_from_memory_cards,
    workflow_default_cards_from_memory_cards,
)

from test_context_authority_pack import _card


def test_workflow_contract_model_carries_scope_reason_evidence_and_exceptions():
    card = _card(
        "mem_worktree",
        "workflow_contract",
        "Use dedicated worktrees before edits",
        {
            "rule": "Use a dedicated branch/worktree before repository edits.",
            "applies_to": "code-changing work",
            "reason": "Repeated repo-safety correction.",
            "exceptions": ["explicit user override"],
        },
    )
    card["source_refs"] = [{"source_ref_id": "session:turn-10994", "kind": "session"}]

    [contract] = workflow_contract_cards_from_memory_cards([card])

    assert contract == {
        "memory_id": "mem_worktree",
        "rule": "Use a dedicated branch/worktree before repository edits.",
        "scope": "code-changing work",
        "reason": "Repeated repo-safety correction.",
        "confidence": 0.9,
        "evidence_refs": ["mem_worktree", "session:turn-10994"],
        "exceptions": ["explicit user override"],
        "auto_update_allowed": False,
    }


def test_workflow_default_model_is_evidence_backed_and_read_only():
    card = _card(
        "mem_agentic",
        "workflow_default",
        "Use agentic execution after approved design",
        {
            "default": "After approved design.md, use agentic-execution as one long-running goal.",
            "applies_to": "approved implementation plans",
            "reason": "Repeated successful handoff pattern.",
            "exceptions": ["SoT change required"],
        },
    )
    card["source_refs"] = [{"source_ref_id": "skill:agentic-execution", "kind": "skill"}]

    [default] = workflow_default_cards_from_memory_cards([card])

    assert default == {
        "memory_id": "mem_agentic",
        "default": "After approved design.md, use agentic-execution as one long-running goal.",
        "scope": "approved implementation plans",
        "reason": "Repeated successful handoff pattern.",
        "confidence": 0.9,
        "evidence_refs": ["mem_agentic", "skill:agentic-execution"],
        "exceptions": ["SoT change required"],
        "auto_update_allowed": False,
    }
    assert workflow_contract_cards_from_memory_cards([card]) == []


def test_workflow_contract_model_excludes_stale_or_superseded_cards():
    stale = _card(
        "mem_old_workflow",
        "workflow_contract",
        "Old workflow contract",
        {"rule": "Use the retired workflow.", "applies_to": "code-changing work"},
    )
    stale["currentness"] = "superseded"

    assert workflow_contract_cards_from_memory_cards([stale]) == []


def test_skill_evolution_model_captures_evidence_without_proposal_loop():
    card = _card(
        "mem_skill",
        "skill_evolution",
        "agentic-execution should be suggested after design approval",
        {
            "skill_name": "agentic-execution",
            "change_summary": "Suggest after approved design.md for long-running implementation.",
            "reason": "Prior sessions used this as the default long-loop handoff.",
        },
    )
    card["source_refs"] = [{"source_ref_id": "session:turn-10994", "kind": "session"}]

    [evolution] = skill_evolution_cards_from_memory_cards([card])

    assert evolution == {
        "memory_id": "mem_skill",
        "skill_name": "agentic-execution",
        "change_summary": "Suggest after approved design.md for long-running implementation.",
        "reason": "Prior sessions used this as the default long-loop handoff.",
        "confidence": 0.9,
        "evidence_refs": ["mem_skill", "session:turn-10994"],
        "auto_update_allowed": False,
    }
