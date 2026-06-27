from agent_knowledge.llm_brain_core.document_authority import document_authority_cards_from_memory_cards

from test_context_authority_pack import _card


def test_document_authority_model_distinguishes_markdown_source_and_html_companion():
    cards = [
        _card(
            "mem_design",
            "decision",
            "Approved design source",
            {
                "decision": "The design source is approved.",
                "authority_ref": "specs/context-authority-roadmap/design.md",
            },
        ),
        _card(
            "mem_preview",
            "decision",
            "Generated architecture preview",
            {
                "decision": "The HTML architecture view is generated.",
                "authority_ref": ".harnesskit/optimal-response-out/content/context-authority.html",
            },
        ),
    ]

    docs = document_authority_cards_from_memory_cards(cards)

    assert docs == [
        {
            "path": "specs/context-authority-roadmap/design.md",
            "status": "source_of_truth",
            "reason": "approved_markdown_source",
            "confidence": 0.9,
            "evidence_refs": ["mem_design"],
            "evidence_edges": [
                {
                    "document_path": "specs/context-authority-roadmap/design.md",
                    "evidence_type": "memory_card",
                    "evidence_ref": "mem_design",
                    "relation": "supports_status",
                    "confidence": 0.9,
                }
            ],
            "archive_proposal_only": True,
        },
        {
            "path": ".harnesskit/optimal-response-out/content/context-authority.html",
            "status": "generated_companion",
            "reason": "html_preview_or_generated_companion",
            "confidence": 0.9,
            "evidence_refs": ["mem_preview"],
            "evidence_edges": [
                {
                    "document_path": ".harnesskit/optimal-response-out/content/context-authority.html",
                    "evidence_type": "memory_card",
                    "evidence_ref": "mem_preview",
                    "relation": "supports_status",
                    "confidence": 0.9,
                }
            ],
            "archive_proposal_only": True,
        },
    ]


def test_document_authority_edges_capture_session_commit_pr_and_live_evidence():
    card = _card(
        "mem_design",
        "decision",
        "Approved design source",
        {
            "decision": "The design source is approved.",
            "authority_ref": "specs/context-authority-roadmap/design.md",
        },
    )
    card["source_refs"] = [{"source_ref_id": "session:turn-10994", "kind": "session"}]
    card["evidence_refs"] = [
        {"kind": "commit", "id": "commit:a1b2c3"},
        {"kind": "pull_request", "id": "pr:27"},
        {"kind": "live", "id": "runtime:ubuntu-smoke"},
    ]

    [doc] = document_authority_cards_from_memory_cards([card])

    assert doc["evidence_refs"] == [
        "mem_design",
        "session:turn-10994",
        "commit:a1b2c3",
        "pr:27",
        "runtime:ubuntu-smoke",
    ]
    assert doc["evidence_edges"] == [
        {
            "document_path": "specs/context-authority-roadmap/design.md",
            "evidence_type": "memory_card",
            "evidence_ref": "mem_design",
            "relation": "supports_status",
            "confidence": 0.9,
        },
        {
            "document_path": "specs/context-authority-roadmap/design.md",
            "evidence_type": "session",
            "evidence_ref": "session:turn-10994",
            "relation": "supports_status",
            "confidence": 0.9,
        },
        {
            "document_path": "specs/context-authority-roadmap/design.md",
            "evidence_type": "commit",
            "evidence_ref": "commit:a1b2c3",
            "relation": "supports_status",
            "confidence": 0.9,
        },
        {
            "document_path": "specs/context-authority-roadmap/design.md",
            "evidence_type": "pull_request",
            "evidence_ref": "pr:27",
            "relation": "supports_status",
            "confidence": 0.9,
        },
        {
            "document_path": "specs/context-authority-roadmap/design.md",
            "evidence_type": "live",
            "evidence_ref": "runtime:ubuntu-smoke",
            "relation": "supports_status",
            "confidence": 0.9,
        },
    ]


def test_document_authority_memory_card_evidence_wins_over_inventory_duplicate():
    card = _card(
        "mem_design",
        "decision",
        "Approved design source",
        {
            "decision": "The design source is approved.",
            "authority_ref": "specs/context-authority-roadmap/design.md",
        },
    )

    [doc] = document_authority_cards_from_memory_cards(
        [card],
        inventory_paths=["specs/context-authority-roadmap/design.md"],
    )

    assert doc["confidence"] == 0.9
    assert doc["evidence_refs"] == ["mem_design"]
