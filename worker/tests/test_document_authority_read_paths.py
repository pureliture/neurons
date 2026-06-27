from agent_knowledge.llm_brain_core import BrainReadService

from test_context_authority_pack import _card


def test_brain_document_read_paths_current_explain_and_archive_candidates():
    active = _card(
        "mem_design",
        "decision",
        "Approved design source",
        {
            "decision": "The design source is approved.",
            "authority_ref": "specs/context-authority-roadmap/design.md",
        },
    )
    stale = _card(
        "mem_old",
        "decision",
        "Old generated report",
        {
            "decision": "The old report is superseded.",
            "authority_ref": "docs/old-context-report.md",
        },
    )
    stale["currentness"] = "stale"
    service = BrainReadService(memory_cards=[active, stale])

    current = service.brain_docs_current(
        repository="neurons",
        branch="codex/context-authority-roadmap",
        current_files=["specs/context-authority-roadmap/design.md", "docs/old-context-report.md"],
        current_request="inspect current docs",
        project="neurons",
    )
    explain = service.brain_docs_explain(
        document_path="specs/context-authority-roadmap/design.md",
        repository="neurons",
        branch="codex/context-authority-roadmap",
        current_files=[],
        current_request="explain design doc",
        project="neurons",
    )
    archive = service.brain_docs_archive_candidates(
        repository="neurons",
        branch="codex/context-authority-roadmap",
        current_files=["docs/old-context-report.md"],
        current_request="archive stale docs",
        project="neurons",
    )

    assert [doc["path"] for doc in current["documents"]] == ["specs/context-authority-roadmap/design.md"]
    assert explain["document"]["status"] == "source_of_truth"
    assert explain["document"]["evidence_edges"][0]["evidence_type"] == "memory_card"
    assert archive["documents"] == [
        {
            "path": "docs/old-context-report.md",
            "status": "archive_candidate",
            "reason": "stale_or_superseded_memory_card",
            "confidence": 0.9,
            "evidence_refs": ["mem_old"],
            "evidence_edges": [
                {
                    "document_path": "docs/old-context-report.md",
                    "evidence_type": "memory_card",
                    "evidence_ref": "mem_old",
                    "relation": "supports_status",
                    "confidence": 0.9,
                }
            ],
            "archive_proposal_only": True,
        }
    ]
