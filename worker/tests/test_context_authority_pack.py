from agent_knowledge.llm_brain_core import BrainReadService


def test_context_pack_exposes_m1_authority_sections_and_guardrails():
    service = BrainReadService(
        memory_cards=[
            _card(
                "mem_task",
                "task",
                "Implement Context Authority Pack",
                {
                    "task_state": "Implement Context Authority Pack",
                    "next_action": "Add authority sections to brain_context_resolve",
                    "status": "open",
                },
            ),
            _card(
                "mem_design",
                "decision",
                "Approved Context Authority design",
                {
                    "decision": "Use neurons brain APIs as the default agent-facing surface.",
                    "rationale": "Neo4j is the workbench, not raw product authority.",
                    "authority_ref": "specs/context-authority-roadmap/design.md",
                },
            ),
            _card(
                "mem_workflow",
                "workflow_contract",
                "Use dedicated worktrees before edits",
                {
                    "rule": "Use a dedicated branch/worktree before repository edits.",
                    "applies_to": "code-changing work",
                    "exceptions": ["explicit user override"],
                },
            ),
            _card(
                "mem_pref",
                "preference",
                "Korean response preference",
                {
                    "preference": "자연어 응답과 문서는 한국어로 작성한다.",
                    "applies_to": "natural_language_response",
                },
            ),
        ],
    )

    pack = service.brain_context_resolve(
        repository="/Users/example/Projects/neurons",
        branch="codex/context-authority-roadmap",
        current_files=["specs/context-authority-roadmap/design.md"],
        current_request="start M1 Context Authority implementation",
        project="neurons",
    ).to_dict()

    authority = pack["authority"]
    assert authority["schema_version"] == "context_authority_pack.v1"
    assert authority["documents"] == [
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
        }
    ]
    assert authority["workflow_contracts"][0]["rule"] == "Use a dedicated branch/worktree before repository edits."
    assert authority["preferences"][0]["rule"] == "자연어 응답과 문서는 한국어로 작성한다."
    assert authority["projection"]["neo4j"]["authority"] == "derived_authority_graph"
    assert authority["object_substrate_status"]["status"] == "degraded"
    assert authority["object_substrate_status"]["authority"] == "model_available_object_store_not_configured"
    assert "object_store_not_configured" in authority["object_substrate_status"]["gaps"]
    assert authority["search_mirror"]["qdrant_docling"] == {
        "status": "unverified",
        "authority": "searchable_document_mirror",
        "canonical_memory": False,
        "product_use": "candidate_only_requires_document_authority_join",
        "requires_document_authority_join": True,
        "degraded_if_unavailable": True,
        "last_verified_at": "",
        "evidence_ref": "",
        "details": [],
    }
    assert "graph_unavailable" in [gap["code"] for gap in authority["evidence_gaps"]]
    assert "agents_use_brain_context_resolve" in authority["boundary_guardrails"]


def test_context_pack_flags_runtime_claims_without_ubuntu_evidence():
    service = BrainReadService(
        memory_cards=[
            _card(
                "mem_runtime",
                "decision",
                "Runtime evidence must be verified",
                {
                    "decision": "Verify neurons runtime claims against ops-host before trusting them.",
                    "authority_ref": "specs/context-authority-roadmap/design.md",
                },
            )
        ],
    )

    pack = service.brain_context_resolve(
        repository="neurons",
        branch="codex/context-authority-roadmap",
        current_files=["compose.yaml"],
        current_request="check deployed Ubuntu runtime status",
        project="neurons",
    ).to_dict()

    gap_codes = [gap["code"] for gap in pack["authority"]["evidence_gaps"]]
    assert "runtime_evidence_unverified" in gap_codes


def test_context_pack_builds_document_authority_from_current_file_inventory_without_cards():
    service = BrainReadService()

    pack = service.brain_context_resolve(
        repository="neurons",
        branch="codex/context-authority-roadmap",
        current_files=[
            "specs/context-authority-roadmap/design.md",
            ".harnesskit/optimal-response-out/content/context-authority.html",
        ],
        current_request="start context authority work",
        project="neurons",
    ).to_dict()

    assert pack["authority"]["documents"] == [
        {
            "path": "specs/context-authority-roadmap/design.md",
            "status": "active",
            "reason": "inventory_markdown_candidate",
            "confidence": 0.5,
            "evidence_refs": ["file_inventory:specs/context-authority-roadmap/design.md"],
            "evidence_edges": [
                {
                    "document_path": "specs/context-authority-roadmap/design.md",
                    "evidence_type": "file_inventory",
                    "evidence_ref": "file_inventory:specs/context-authority-roadmap/design.md",
                    "relation": "supports_status",
                    "confidence": 0.5,
                }
            ],
            "archive_proposal_only": True,
        },
        {
            "path": ".harnesskit/optimal-response-out/content/context-authority.html",
            "status": "generated_companion",
            "reason": "html_preview_or_generated_companion",
            "confidence": 0.5,
            "evidence_refs": ["file_inventory:.harnesskit/optimal-response-out/content/context-authority.html"],
            "evidence_edges": [
                {
                    "document_path": ".harnesskit/optimal-response-out/content/context-authority.html",
                    "evidence_type": "file_inventory",
                    "evidence_ref": "file_inventory:.harnesskit/optimal-response-out/content/context-authority.html",
                    "relation": "supports_status",
                    "confidence": 0.5,
                }
            ],
            "archive_proposal_only": True,
        },
    ]


def test_context_pack_applies_only_relevant_preferences_for_current_request():
    service = BrainReadService(
        memory_cards=[
            _card(
                "mem_language",
                "preference",
                "Korean response preference",
                {
                    "preference": "자연어 응답과 문서는 한국어로 작성한다.",
                    "applies_to": "natural_language_response",
                },
            ),
            _card(
                "mem_runtime",
                "preference",
                "Runtime proof preference",
                {
                    "preference": "Verify runtime truth against approved host evidence before trusting claims.",
                    "applies_to": "runtime proof",
                },
            ),
        ],
    )

    ordinary = service.brain_context_resolve(
        repository="neurons",
        branch="codex/context-authority-roadmap",
        current_files=["worker/lib/agent_knowledge/llm_brain_core/context.py"],
        current_request="add workflow authority read path",
        project="neurons",
    ).to_dict()
    runtime = service.brain_context_resolve(
        repository="neurons",
        branch="codex/context-authority-roadmap",
        current_files=["compose.yaml"],
        current_request="verify deployed runtime status",
        project="neurons",
    ).to_dict()

    assert [item["memory_id"] for item in ordinary["authority"]["preferences"]] == ["mem_language"]
    assert [item["memory_id"] for item in runtime["authority"]["preferences"]] == ["mem_language", "mem_runtime"]


def test_context_pack_uses_injected_search_mirror_status_without_claiming_authority():
    service = BrainReadService(
        search_mirror_status={
            "status": "configured_unverified",
            "last_verified_at": "",
            "evidence_ref": "service:mirror_search_configured",
            "details": ["mirror_search_callable_configured_without_live_probe"],
        }
    )

    pack = service.brain_context_resolve(
        repository="neurons",
        branch="codex/context-authority-roadmap",
        current_files=[],
        current_request="resolve docs",
        project="neurons",
    ).to_dict()

    mirror = pack["authority"]["search_mirror"]["qdrant_docling"]
    assert mirror["status"] == "configured_unverified"
    assert mirror["evidence_ref"] == "service:mirror_search_configured"
    assert mirror["requires_document_authority_join"] is True
    assert mirror["canonical_memory"] is False


def test_context_pack_flags_named_runtime_system_claims_without_evidence():
    service = BrainReadService()

    pack = service.brain_context_resolve(
        repository="neurons",
        branch="codex/context-authority-roadmap",
        current_files=["docs/graphiti-canary.md"],
        current_request="Graphiti canary passed and Neo4j projection is healthy",
        project="neurons",
    ).to_dict()

    assert "runtime_evidence_unverified" in pack["gaps"]


def test_context_pack_runtime_gap_uses_token_matching_not_substrings():
    service = BrainReadService()

    pack = service.brain_context_resolve(
        repository="neurons",
        branch="codex/context-authority-roadmap",
        current_files=["docs/delivery-healthcare-copy.md"],
        current_request="deliver healthcare copy edits",
        project="neurons",
    ).to_dict()

    assert "runtime_evidence_unverified" not in pack["gaps"]


def test_context_pack_response_modes_preserve_required_status_fields():
    service = BrainReadService()
    pack = service.brain_context_resolve(
        repository="neurons",
        branch="codex/context-authority-roadmap",
        current_files=["compose.yaml"],
        current_request="verify deployed runtime status",
        project="neurons",
    )

    full = pack.to_dict()
    compact = pack.to_dict(mode="compact")
    degraded = pack.to_dict(mode="degraded")

    assert "relevant_decisions" in full
    assert compact["response_mode"] == "compact"
    assert compact["schema_version"] == full["schema_version"]
    assert compact["memory_status"] == full["memory_status"]
    assert compact["graph_status"] == full["graph_status"]
    assert compact["bridge_status"] == full["bridge_status"]
    assert compact["authority"]["schema_version"] == "context_authority_pack.v1"
    assert compact["authority"]["projection"]["neo4j"]["status"] == full["graph_status"]["status"]
    assert "relevant_decisions" not in compact
    assert degraded["response_mode"] == "degraded"
    assert degraded["status"] == "degraded"
    assert "runtime_evidence_unverified" in degraded["gaps"]
    assert "source_refs" in degraded["omitted_sections"]


def _card(memory_id, card_type, summary, typed_payload):
    return {
        "memory_id": memory_id,
        "brain_id": "/project/neurons",
        "card_type": card_type,
        "scope": "project",
        "project": "neurons",
        "provider": "codex",
        "title": summary,
        "summary": summary,
        "render_text": summary,
        "lifecycle_state": "accepted",
        "judgment_state": "none",
        "status": "active",
        "approval_state": "approved",
        "governance_tier": "medium",
        "freshness": "current",
        "currentness": "current",
        "confidence": 0.9,
        "confidence_basis": "fixture",
        "source_refs": [],
        "evidence_refs": [],
        "evidence_hashes": ["sha256:" + memory_id],
        "derived_from": [],
        "supersedes": [],
        "superseded_by": [],
        "conflicts": [],
        "active_until": "",
        "typed_payload": typed_payload,
    }
