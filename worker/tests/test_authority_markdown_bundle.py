from agent_knowledge.llm_brain_core import BrainReadService
from agent_knowledge.llm_brain_core.authority_bundle import (
    build_markdown_authority_bundle,
    check_markdown_authority_bundle_drift,
)

from test_context_authority_pack import _card


def test_markdown_authority_bundle_exports_context_pack_sections():
    service = BrainReadService(
        memory_cards=[
            _card(
                "mem_design",
                "decision",
                "Approved Context Authority design",
                {
                    "decision": "Use neurons brain APIs as the default agent-facing surface.",
                    "authority_ref": "specs/context-authority-roadmap/design.md",
                },
            ),
            _card(
                "mem_worktree",
                "workflow_contract",
                "Use dedicated worktrees before edits",
                {
                    "rule": "Use a dedicated branch/worktree before repository edits.",
                    "applies_to": "code-changing work",
                    "reason": "Repeated repo-safety correction.",
                },
            ),
            _card(
                "mem_language",
                "preference",
                "Korean response preference",
                {
                    "preference": "자연어 응답과 문서는 한국어로 작성한다.",
                    "applies_to": "natural_language_response",
                    "reason": "User global communication preference.",
                },
            ),
        ],
    )
    pack = service.brain_context_resolve(
        repository="neurons",
        branch="codex/context-authority-roadmap",
        current_files=[
            "specs/context-authority-roadmap/design.md",
            ".harnesskit/optimal-response-out/content/context-authority.html",
        ],
        current_request="verify deployed runtime status",
        project="neurons",
    )

    files = build_markdown_authority_bundle(pack)

    assert sorted(files) == [
        "context-authority/documents/8f0721c15046.md",
        "context-authority/documents/e5ada5d392a3.md",
        "context-authority/evidence-gaps/graph_unavailable.md",
        "context-authority/evidence-gaps/runtime_evidence_unverified.md",
        "context-authority/index.md",
        "context-authority/preferences/mem_language.md",
        "context-authority/workflows/mem_worktree.md",
    ]
    assert "schema_version: context_authority_pack.v1" in files["context-authority/index.md"]
    assert 'status: "source_of_truth"' in files["context-authority/documents/e5ada5d392a3.md"]
    assert "confidence: 0.9" in files["context-authority/documents/e5ada5d392a3.md"]
    assert 'evidence_refs:\n  - "mem_design"' in files["context-authority/documents/e5ada5d392a3.md"]
    assert "generated_artifact: true" in files["context-authority/documents/8f0721c15046.md"]
    assert "auto_update_allowed: false" in files["context-authority/workflows/mem_worktree.md"]
    assert 'scope: "natural_language_response"' in files["context-authority/preferences/mem_language.md"]
    assert 'next_action: "verify_against_approved_ubuntu_runtime_surface"' in files[
        "context-authority/evidence-gaps/runtime_evidence_unverified.md"
    ]


def test_markdown_authority_bundle_quotes_yaml_strings_with_special_characters():
    service = BrainReadService(
        memory_cards=[
            _card(
                "mem_rule",
                "workflow_contract",
                "Use quoted YAML",
                {
                    "rule": 'Use key: "value" in examples.',
                    "reason": "Colon: hash # dash - must not break frontmatter.",
                },
            )
        ],
    )
    pack = service.brain_context_resolve(
        repository="neurons",
        branch="codex/context-authority-roadmap",
        current_files=[],
        current_request="export authority bundle",
        project="neurons",
    )

    files = build_markdown_authority_bundle(pack)
    workflow = next(body for path, body in files.items() if path.startswith("context-authority/workflows/"))

    assert 'reason: "Colon: hash # dash - must not break frontmatter."' in workflow
    assert '# Workflow Contract\n\nUse key: "value" in examples.' in workflow


def test_markdown_authority_bundle_drift_check_reports_missing_extra_and_changed_files():
    service = BrainReadService(
        memory_cards=[
            _card(
                "mem_design",
                "decision",
                "Approved Context Authority design",
                {"authority_ref": "specs/context-authority-roadmap/design.md"},
            )
        ],
    )
    pack = service.brain_context_resolve(
        repository="neurons",
        branch="codex/context-authority-roadmap",
        current_files=["specs/context-authority-roadmap/design.md"],
        current_request="export authority bundle",
        project="neurons",
    )
    files = build_markdown_authority_bundle(pack)

    assert check_markdown_authority_bundle_drift(pack, files) == {
        "status": "in_sync",
        "missing": [],
        "extra": [],
        "changed": [],
    }

    drifted = dict(files)
    drifted["context-authority/index.md"] = drifted["context-authority/index.md"] + "\nmanual edit\n"
    drifted["context-authority/extra.md"] = "manual extra"
    del drifted["context-authority/documents/e5ada5d392a3.md"]

    assert check_markdown_authority_bundle_drift(pack, drifted) == {
        "status": "drifted",
        "missing": ["context-authority/documents/e5ada5d392a3.md"],
        "extra": ["context-authority/extra.md"],
        "changed": ["context-authority/index.md"],
    }
