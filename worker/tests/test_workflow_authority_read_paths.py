from agent_knowledge.llm_brain_core import BrainReadService

from test_context_authority_pack import _card


def test_brain_workflow_read_paths_current_and_explain():
    worktree = _card(
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
    worktree["source_refs"] = [{"source_ref_id": "session:turn-10994", "kind": "session"}]
    review = _card(
        "mem_review",
        "workflow_contract",
        "Run a review gate for high-risk implementation milestones",
        {
            "rule": "Use a review gate for high-risk implementation milestones.",
            "applies_to": "high-risk milestones",
            "reason": "Risky changes need independent review evidence.",
        },
    )
    service = BrainReadService(memory_cards=[worktree, review])

    current = service.brain_workflows_current(
        repository="neurons",
        branch="codex/context-authority-roadmap",
        current_files=[],
        current_request="code-changing work in neurons",
        project="neurons",
    )
    explain = service.brain_workflows_explain(
        rule="Use dedicated branch/worktree before repository edits.",
        repository="neurons",
        branch="codex/context-authority-roadmap",
        current_files=[],
        current_request="explain workflow defaults",
        project="neurons",
    )

    assert [contract["memory_id"] for contract in current["workflow_contracts"]] == [
        "mem_worktree",
        "mem_review",
    ]
    assert current["auto_update_allowed"] is False
    assert explain["workflow_contract"] == {
        "memory_id": "mem_worktree",
        "rule": "Use a dedicated branch/worktree before repository edits.",
        "scope": "code-changing work",
        "reason": "Repeated repo-safety correction.",
        "confidence": 0.9,
        "evidence_refs": ["mem_worktree", "session:turn-10994"],
        "exceptions": ["explicit user override"],
        "auto_update_allowed": False,
    }
