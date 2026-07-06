from agent_knowledge.llm_brain_core.object_packs import (
    apply_approval_board_decisions,
    apply_candidate_review_edits,
    build_agent_context_object_packs,
    build_candidate_graph_review_pack,
    build_documentation_cleanup_pack,
    build_runtime_truth_pack,
    route_spec_for,
)
from agent_knowledge.llm_brain_core.knowledge_objects import EvidenceRef, KnowledgeEdge, KnowledgeObjectEnvelope


def test_documentation_cleanup_pack_separates_lanes_and_actions():
    pack = build_documentation_cleanup_pack(
        documents=[
            {
                "path": "README.md",
                "status": "source_of_truth",
                "reason": "approved_markdown_source",
                "confidence": 0.9,
                "evidence_refs": ["mem_readme"],
            },
            {
                "path": "docs/old.md",
                "status": "archive_candidate",
                "reason": "stale_or_superseded_memory_card",
                "confidence": 0.7,
                "evidence_refs": ["mem_old"],
            },
        ],
        route="documentation_cleanup",
    )

    assert pack["route"] == "documentation_cleanup"
    assert pack["lanes"]["accepted_current"][0]["payload"]["path_ref"] == "README.md"
    assert pack["lanes"]["proposal_only"][0]["payload"]["path_ref"] == "docs/old.md"
    assert pack["recommended_actions"] == [
        {"object_id": pack["lanes"]["accepted_current"][0]["object_id"], "action": "keep"},
        {"object_id": pack["lanes"]["proposal_only"][0]["object_id"], "action": "review_archive"},
    ]
    assert "review_archive" in pack["route_spec"]["recommended_action_vocabulary"]
    assert "locator_view" in pack["evidence"][0]
    assert pack["gaps"] == []


def test_documentation_cleanup_pack_reports_empty_current_lane():
    pack = build_documentation_cleanup_pack(documents=[], route="documentation_cleanup")

    assert pack["lanes"]["accepted_current"] == []
    assert "accepted_current documents empty" in pack["gaps"]
    assert "review_proposals_needed" in pack["gaps"]


def test_documentation_cleanup_pack_skips_empty_evidence_refs():
    pack = build_documentation_cleanup_pack(
        documents=[
            {
                "path": "README.md",
                "status": "source_of_truth",
                "evidence_refs": [None, "", "mem_readme"],
            },
        ],
        route="documentation_cleanup",
    )

    assert [evidence["evidence_id"] for evidence in pack["evidence"]] == ["mem_readme"]


def test_runtime_truth_pack_keeps_merge_and_deploy_verification_separate():
    pack = build_runtime_truth_pack(
        pull_request={"id": "pr:1", "merged": True},
        deployment=None,
        live_evidence=None,
    )

    assert pack["route"] == "deployment_runtime_truth"
    assert pack["verification"]["runtime_verified"] == []
    assert pack["verification"]["runtime_unverified"]
    assert pack["lanes"]["candidate"][0]["object_type"] == "PullRequest"
    assert "runtime_evidence_unverified" in pack["gaps"]


def test_runtime_truth_pack_requires_typed_runtime_verified_evidence():
    untyped = build_runtime_truth_pack(
        pull_request={"id": "pr:1", "merged": True},
        deployment={"target": "stable"},
        live_evidence={"status": "healthy"},
    )
    typed = build_runtime_truth_pack(
        pull_request={"id": "pr:1", "merged": True},
        deployment={"target": "stable"},
        live_evidence={"verification_state": "runtime_verified", "evidence_id": "ev:runtime:stable"},
    )

    assert untyped["verification"]["runtime_verified"] == []
    assert "runtime_evidence_unverified" in untyped["gaps"]
    assert typed["verification"]["runtime_verified"][0]["evidence_id"] == "ev:runtime:stable"


def test_candidate_graph_review_pack_exposes_editable_review_surface_without_authority_write():
    evidence = EvidenceRef.from_parts(
        evidence_type="source_hash",
        authority_lane="reference_only",
        verification_state="source_hash_verified",
        locator={"kind": "relative_repo_path", "value": "docs/source.md"},
        content_hash="sha256:" + "1" * 64,
        summary="Source material hash.",
    )
    obj = KnowledgeObjectEnvelope.from_parts(
        object_type="RepoDocument",
        natural_key="docs/source.md",
        scope={"project": "neurons"},
        title="Draft source doc",
        summary="AI extracted document claim.",
        lifecycle_status="proposed",
        authority_lane="candidate",
        verification_state="source_hash_verified",
        review_state="needs_review",
        content_hash="sha256:" + "2" * 64,
        evidence_refs=[evidence.evidence_id],
        confidence={"score": 0.64, "basis": "ai_extraction"},
        recommended_action="review",
        payload={"path_ref": "docs/source.md"},
    ).to_dict()
    edge = KnowledgeEdge.from_parts(
        edge_type="requires_evidence",
        from_object_id=obj["object_id"],
        to_object_id=obj["object_id"],
        evidence_refs=[evidence.evidence_id],
        lifecycle_status="proposed",
        authority_lane="candidate",
        verification_state="unverified",
    ).to_dict()

    pack = build_candidate_graph_review_pack(
        objects=[obj],
        edges=[edge],
        evidence=[evidence.to_view()],
        extractor="fixture_ai_extractor",
        reviewer_actions=["promote", "reject", "hold", "request_more_evidence"],
    )

    assert pack["route"] == "candidate_graph_review"
    assert pack["production_mutation_performed"] is False
    assert pack["authority_write_performed"] is False
    assert pack["authoritative_memory_changed"] is False
    assert pack["minimal_edit_surface"]["supported"] is True
    assert pack["lanes"]["candidate"][0]["object_id"] == obj["object_id"]
    assert pack["approval_board"][0]["object_id"] == obj["object_id"]
    assert pack["approval_board"][0]["editable"] is True
    assert pack["approval_board"][0]["allowed_actions"] == [
        "promote",
        "reject",
        "hold",
        "request_more_evidence",
    ]
    assert pack["minimal_edit_surface"]["supported_edit_actions"] == [
        "update_object",
        "update_edge",
        "update_evidence",
        "add_edge",
        "remove_edge",
        "add_evidence",
        "remove_evidence",
    ]
    assert pack["approval_board"][0]["evidence_refs"] == [evidence.evidence_id]
    assert pack["candidate_graph_hash"].startswith("sha256:")
    assert "accepted_current objects empty" in pack["gaps"]


def test_candidate_review_edits_change_candidate_state_without_promoting_authority():
    evidence = EvidenceRef.from_parts(
        evidence_type="source_hash",
        authority_lane="reference_only",
        verification_state="source_hash_verified",
        locator={"kind": "relative_repo_path", "value": "docs/source.md"},
        content_hash="sha256:" + "4" * 64,
        summary="Source material hash.",
    )
    obj = KnowledgeObjectEnvelope.from_parts(
        object_type="RepoDocument",
        natural_key="docs/source.md",
        scope={"project": "neurons"},
        title="Draft source doc",
        summary="AI extracted document claim.",
        lifecycle_status="proposed",
        authority_lane="candidate",
        verification_state="unverified",
        review_state="needs_review",
        content_hash="sha256:" + "3" * 64,
        evidence_refs=[evidence.evidence_id],
        recommended_action="review",
        payload={"path_ref": "docs/source.md"},
    ).to_dict()
    edge = KnowledgeEdge.from_parts(
        edge_type="requires_evidence",
        from_object_id=obj["object_id"],
        to_object_id=obj["object_id"],
        evidence_refs=[evidence.evidence_id],
        lifecycle_status="proposed",
        authority_lane="candidate",
        verification_state="unverified",
    ).to_dict()
    pack = build_candidate_graph_review_pack(
        objects=[obj],
        edges=[edge],
        evidence=[evidence.to_view()],
        extractor="fixture_ai_extractor",
    )

    result = apply_candidate_review_edits(
        pack,
        edits=[
            {
                "action": "update_object",
                "object_id": obj["object_id"],
                "fields": {
                    "title": "Reviewed source doc",
                    "summary": "Human corrected candidate claim.",
                    "recommended_action": "request_more_evidence",
                    "authority_lane": "accepted_current",
                },
            },
            {
                "action": "update_edge",
                "edge_id": edge["edge_id"],
                "fields": {
                    "edge_type": "supersedes",
                    "authority_lane": "accepted_current",
                },
            },
            {
                "action": "update_evidence",
                "evidence_id": evidence.evidence_id,
                "fields": {
                    "summary": "Human corrected evidence summary.",
                    "authority_lane": "accepted_current",
                },
            },
        ],
        reviewer={"id": "human-reviewer"},
    )

    updated = result["updated_pack"]["objects"][0]
    updated_edge = result["updated_pack"]["edges"][0]
    updated_evidence = result["updated_pack"]["evidence"][0]
    assert result["schema_version"] == "candidate_review_edit_result.v1"
    assert result["candidate_state_changed"] is True
    assert result["authority_write_performed"] is False
    assert result["authoritative_memory_changed"] is False
    assert result["original_extraction_preserved"] is True
    assert result["rejected_edits"] == [
        {
            "action": "update_object",
            "object_id": obj["object_id"],
            "field": "authority_lane",
            "reason": "authority_field_requires_approval_board_decision",
        },
        {
            "action": "update_edge",
            "edge_id": edge["edge_id"],
            "field": "authority_lane",
            "reason": "authority_field_requires_approval_board_decision",
        },
        {
            "action": "update_evidence",
            "evidence_id": evidence.evidence_id,
            "field": "authority_lane",
            "reason": "authority_field_requires_approval_board_decision",
        }
    ]
    assert updated["title"] == "Reviewed source doc"
    assert updated["summary"] == "Human corrected candidate claim."
    assert updated["recommended_action"] == "request_more_evidence"
    assert updated["authority_lane"] == "candidate"
    assert updated["review_state"] == "needs_review"
    assert result["updated_pack"]["approval_board"][0]["title"] == "Reviewed source doc"
    assert result["updated_pack"]["recommended_actions"] == [
        {"object_id": obj["object_id"], "action": "request_more_evidence"}
    ]
    assert updated_edge["edge_type"] == "supersedes"
    assert updated_edge["authority_lane"] == "candidate"
    assert updated_edge["edge_id"] != edge["edge_id"]
    assert updated_evidence["summary"] == "Human corrected evidence summary."
    assert updated_evidence["authority_lane"] == "reference_only"
    assert result["updated_pack"]["candidate_graph_hash"] != pack["candidate_graph_hash"]


def test_candidate_review_edits_add_and_remove_edges_and_evidence_without_authority_write():
    original_evidence = EvidenceRef.from_parts(
        evidence_type="source_hash",
        authority_lane="reference_only",
        verification_state="source_hash_verified",
        locator={"kind": "relative_repo_path", "value": "docs/source.md"},
        content_hash="sha256:" + "7" * 64,
        summary="Original source material hash.",
    )
    added_evidence = EvidenceRef.from_parts(
        evidence_type="source_hash",
        authority_lane="reference_only",
        verification_state="source_hash_verified",
        locator={"kind": "relative_repo_path", "value": "docs/reviewed-source.md"},
        content_hash="sha256:" + "8" * 64,
        summary="Reviewer attached replacement evidence.",
    )
    obj = KnowledgeObjectEnvelope.from_parts(
        object_type="RepoDocument",
        natural_key="docs/source.md",
        scope={"project": "neurons"},
        title="Draft source doc",
        summary="AI extracted document claim.",
        lifecycle_status="proposed",
        authority_lane="candidate",
        verification_state="unverified",
        review_state="needs_review",
        content_hash="sha256:" + "9" * 64,
        evidence_refs=[original_evidence.evidence_id],
        recommended_action="review",
        payload={"path_ref": "docs/source.md"},
    ).to_dict()
    original_edge = KnowledgeEdge.from_parts(
        edge_type="requires_evidence",
        from_object_id=obj["object_id"],
        to_object_id=obj["object_id"],
        evidence_refs=[original_evidence.evidence_id],
        lifecycle_status="proposed",
        authority_lane="candidate",
        verification_state="unverified",
    ).to_dict()
    pack = build_candidate_graph_review_pack(
        objects=[obj],
        edges=[original_edge],
        evidence=[original_evidence.to_view()],
        extractor="fixture_ai_extractor",
    )

    result = apply_candidate_review_edits(
        pack,
        edits=[
            {
                "action": "add_evidence",
                "attach_to_object_id": obj["object_id"],
                "fields": {
                    "evidence_type": "source_hash",
                    "locator": {"kind": "relative_repo_path", "value": "docs/reviewed-source.md"},
                    "content_hash": "sha256:" + "8" * 64,
                    "summary": "Reviewer attached replacement evidence.",
                },
            },
            {
                "action": "add_edge",
                "fields": {
                    "edge_type": "supports",
                    "from_object_id": obj["object_id"],
                    "to_object_id": obj["object_id"],
                    "evidence_refs": [added_evidence.evidence_id],
                },
            },
            {"action": "remove_edge", "edge_id": original_edge["edge_id"]},
            {"action": "remove_evidence", "evidence_id": original_evidence.evidence_id},
        ],
        reviewer={"id": "human-reviewer"},
    )

    updated_pack = result["updated_pack"]
    updated_obj = updated_pack["objects"][0]
    assert result["permission"] == "allowed"
    assert result["reason"] == "candidate_review_edit_no_mutation_preview"
    assert result["target_scope"] == "local_test"
    assert result["mutation_mode"] == "no_mutation"
    assert result["candidate_state_changed"] is True
    assert result["authority_write_performed"] is False
    assert result["authoritative_memory_changed"] is False
    assert result["production_mutation_performed"] is False
    assert result["rejected_edits"] == []
    assert [item["action"] for item in result["accepted_edits"]] == [
        "add_evidence",
        "add_edge",
        "remove_edge",
        "remove_evidence",
    ]
    assert [item["evidence_id"] for item in updated_pack["evidence"]] == [added_evidence.evidence_id]
    assert updated_obj["evidence_refs"] == [added_evidence.evidence_id]
    assert len(updated_pack["edges"]) == 1
    assert updated_pack["edges"][0]["edge_type"] == "supports"
    assert updated_pack["edges"][0]["evidence_refs"] == [added_evidence.evidence_id]
    assert updated_obj["edge_refs"] == [updated_pack["edges"][0]["edge_id"]]
    assert updated_pack["approval_board"][0]["evidence_refs"] == [added_evidence.evidence_id]
    assert updated_pack["candidate_graph_hash"] != pack["candidate_graph_hash"]


def test_candidate_review_edits_reject_non_candidate_authority_lanes():
    evidence = EvidenceRef.from_parts(
        evidence_type="source_hash",
        authority_lane="reference_only",
        verification_state="source_hash_verified",
        locator={"kind": "relative_repo_path", "value": "docs/source.md"},
        content_hash="sha256:" + "a" * 64,
        summary="Accepted source material hash.",
    )
    accepted_obj = KnowledgeObjectEnvelope.from_parts(
        object_type="RepoDocument",
        natural_key="docs/source.md",
        scope={"project": "neurons"},
        title="Accepted source doc",
        summary="Current accepted claim.",
        lifecycle_status="current",
        authority_lane="accepted_current",
        verification_state="source_hash_verified",
        review_state="accepted",
        content_hash="sha256:" + "b" * 64,
        evidence_refs=[evidence.evidence_id],
        recommended_action="keep",
        payload={"path_ref": "docs/source.md"},
    ).to_dict()
    reference_edge = KnowledgeEdge.from_parts(
        edge_type="references",
        from_object_id=accepted_obj["object_id"],
        to_object_id=accepted_obj["object_id"],
        evidence_refs=[evidence.evidence_id],
        lifecycle_status="observed",
        authority_lane="reference_only",
        verification_state="unverified",
    ).to_dict()
    pack = build_candidate_graph_review_pack(
        objects=[accepted_obj],
        edges=[reference_edge],
        evidence=[evidence.to_view()],
        extractor="fixture_ai_extractor",
    )

    result = apply_candidate_review_edits(
        pack,
        edits=[
            {
                "action": "update_object",
                "object_id": accepted_obj["object_id"],
                "fields": {"summary": "Should not change accepted authority."},
            },
            {
                "action": "add_edge",
                "fields": {
                    "edge_type": "supports",
                    "from_object_id": accepted_obj["object_id"],
                    "to_object_id": accepted_obj["object_id"],
                    "evidence_refs": [evidence.evidence_id],
                },
            },
            {
                "action": "update_edge",
                "edge_id": reference_edge["edge_id"],
                "fields": {"edge_type": "supersedes"},
            },
            {
                "action": "update_evidence",
                "evidence_id": evidence.evidence_id,
                "fields": {"summary": "Should not change accepted evidence usage."},
            },
            {"action": "remove_evidence", "evidence_id": evidence.evidence_id},
        ],
        reviewer={"id": "human-reviewer"},
    )

    assert result["candidate_state_changed"] is False
    assert result["permission"] == "allowed"
    assert result["authority_write_performed"] is False
    assert result["authoritative_memory_changed"] is False
    assert result["production_mutation_performed"] is False
    assert result["updated_pack"]["objects"][0]["summary"] == "Current accepted claim."
    assert result["updated_pack"]["edges"][0]["edge_type"] == "references"
    assert result["updated_pack"]["evidence"][0]["summary"] == "Accepted source material hash."
    assert [item["reason"] for item in result["rejected_edits"]] == [
        "candidate_review_edit_requires_candidate_lane",
        "candidate_review_edit_requires_candidate_lane",
        "candidate_review_edit_requires_candidate_lane",
        "candidate_evidence_used_by_non_candidate_authority",
        "candidate_evidence_used_by_non_candidate_authority",
    ]


def test_candidate_review_edits_deny_mutation_mode_before_editing_candidate_pack():
    evidence = EvidenceRef.from_parts(
        evidence_type="source_hash",
        authority_lane="reference_only",
        verification_state="source_hash_verified",
        locator={"kind": "relative_repo_path", "value": "docs/source.md"},
        content_hash="sha256:" + "c" * 64,
        summary="Source material hash.",
    )
    obj = KnowledgeObjectEnvelope.from_parts(
        object_type="RepoDocument",
        natural_key="docs/source.md",
        scope={"project": "neurons"},
        title="Draft source doc",
        summary="AI extracted document claim.",
        lifecycle_status="proposed",
        authority_lane="candidate",
        verification_state="unverified",
        review_state="needs_review",
        content_hash="sha256:" + "d" * 64,
        evidence_refs=[evidence.evidence_id],
        recommended_action="review",
        payload={"path_ref": "docs/source.md"},
    ).to_dict()
    pack = build_candidate_graph_review_pack(
        objects=[obj],
        edges=[],
        evidence=[evidence.to_view()],
        extractor="fixture_ai_extractor",
    )

    result = apply_candidate_review_edits(
        pack,
        edits=[
            {
                "action": "update_object",
                "object_id": obj["object_id"],
                "fields": {"summary": "Should not be applied."},
            }
        ],
        reviewer={"id": "human-reviewer"},
        target_scope="production",
        mutation_mode="write",
    )

    assert result["permission"] == "denied"
    assert result["reason"] == "candidate_review_edit_mutation_mode_not_supported"
    assert result["target_scope"] == "production"
    assert result["mutation_mode"] == "write"
    assert result["candidate_state_changed"] is False
    assert result["authority_write_performed"] is False
    assert result["authoritative_memory_changed"] is False
    assert result["production_mutation_performed"] is False
    assert result["updated_pack"]["objects"][0]["summary"] == "AI extracted document claim."
    assert result["rejected_edits"] == [
        {
            "action": "update_object",
            "reason": "candidate_review_edit_mutation_mode_not_supported",
        }
    ]


def test_approval_board_promotes_candidate_in_local_test_without_production_mutation():
    evidence = EvidenceRef.from_parts(
        evidence_type="source_hash",
        authority_lane="reference_only",
        verification_state="source_hash_verified",
        locator={"kind": "relative_repo_path", "value": "docs/source.md"},
        content_hash="sha256:" + "5" * 64,
        summary="Source material hash.",
    )
    obj = KnowledgeObjectEnvelope.from_parts(
        object_type="RepoDocument",
        natural_key="docs/source.md",
        scope={"project": "neurons"},
        title="Reviewed source doc",
        summary="Human reviewed candidate claim.",
        lifecycle_status="proposed",
        authority_lane="candidate",
        verification_state="source_hash_verified",
        review_state="needs_review",
        content_hash="sha256:" + "6" * 64,
        evidence_refs=[evidence.evidence_id],
        confidence={"score": 0.91, "basis": "reviewed_candidate"},
        recommended_action="promote",
        payload={"path_ref": "docs/source.md"},
    ).to_dict()
    pack = build_candidate_graph_review_pack(
        objects=[obj],
        edges=[],
        evidence=[evidence.to_view()],
        extractor="fixture_ai_extractor",
    )

    result = apply_approval_board_decisions(
        pack,
        decisions=[
            {
                "action": "promote",
                "object_id": obj["object_id"],
                "reason": "Reviewer accepted the candidate as current authority.",
                "approved_by": "human-reviewer",
            }
        ],
        reviewer={"id": "human-reviewer"},
        ledger_scope="local_test",
    )

    promoted = result["updated_pack"]["objects"][0]
    assert result["schema_version"] == "approval_board_decision_result.v1"
    assert result["ledger_scope"] == "local_test"
    assert result["production_mutation_performed"] is False
    assert result["authority_write_performed"] is True
    assert result["authority_write_scope"] == "local_test"
    assert result["authoritative_memory_changed"] is True
    assert result["decision_count"] == 1
    assert result["decisions"][0]["decision_type"] == "accept_current"
    assert result["decisions"][0]["previous_authority_lane"] == "candidate"
    assert result["decisions"][0]["new_authority_lane"] == "accepted_current"
    assert promoted["authority_lane"] == "accepted_current"
    assert promoted["lifecycle_status"] == "current"
    assert promoted["review_state"] == "accepted"
    assert promoted["recommended_action"] == "keep"
    assert result["updated_pack"]["lanes"]["accepted_current"][0]["object_id"] == obj["object_id"]
    assert result["updated_pack"]["lanes"]["candidate"] == []
    assert result["updated_pack"]["approval_board"][0]["editable"] is False
    assert result["updated_pack"]["candidate_graph_hash"] != pack["candidate_graph_hash"]


def test_approval_board_decision_denies_production_scope_without_mutation():
    obj = KnowledgeObjectEnvelope.from_parts(
        object_type="RepoDocument",
        natural_key="docs/source.md",
        scope={"project": "neurons"},
        title="Reviewed source doc",
        summary="Human reviewed candidate claim.",
        lifecycle_status="proposed",
        authority_lane="candidate",
        verification_state="source_hash_verified",
        review_state="needs_review",
        content_hash="sha256:" + "7" * 64,
        evidence_refs=["ev:source:production-deny"],
        confidence={"score": 0.91, "basis": "reviewed_candidate"},
        recommended_action="promote",
        payload={"path_ref": "docs/source.md"},
    ).to_dict()
    pack = build_candidate_graph_review_pack(
        objects=[obj],
        edges=[],
        evidence=[],
        extractor="fixture_ai_extractor",
    )

    result = apply_approval_board_decisions(
        pack,
        decisions=[
            {
                "action": "promote",
                "object_id": obj["object_id"],
                "reason": "Reviewer accepted the candidate as current authority.",
                "approved_by": "human-reviewer",
            }
        ],
        reviewer={"id": "human-reviewer"},
        ledger_scope="production",
    )

    assert result["permission"] == "denied"
    assert result["reason"] == "production_approval_gate_required"
    assert result["production_mutation_performed"] is False
    assert result["authority_write_performed"] is False
    assert result["authoritative_memory_changed"] is False
    assert result["updated_pack"]["candidate_graph_hash"] == pack["candidate_graph_hash"]
    assert result["updated_pack"]["objects"][0]["authority_lane"] == "candidate"
    assert result["promotion_plan"]["required_gate_evidence"] == [
        "human_approval",
        "audit_trail",
        "rollback_or_supersession_path",
        "scoped_object_classes",
    ]


def test_route_spec_is_declarative():
    spec = route_spec_for("documentation_cleanup")

    assert spec["required_object_types"] == ["RepoDocument"]
    assert "accepted_current" in spec["allowed_authority_lanes"]
    assert "includes_recommended_action" in spec["eval_assertions"]

    review_spec = route_spec_for("candidate_graph_review")
    assert "candidate" in review_spec["allowed_authority_lanes"]
    assert "edge_type" in review_spec["editable_edge_fields"]
    assert "summary" in review_spec["editable_evidence_fields"]
    assert "add_evidence" in review_spec["supported_edit_actions"]
    assert "remove_edge" in review_spec["supported_edit_actions"]
    assert "reviewer_edit_does_not_mutate_authority" in review_spec["eval_assertions"]
    assert "approval_board_decision_promotes_authority" in review_spec["eval_assertions"]


def test_agent_context_object_packs_include_fr11_sections():
    packs = build_agent_context_object_packs(
        documents=[],
        preferences=[],
        style_profile={"claims": []},
        current_work=[],
        required_verification=["cd worker && uv run pytest -q"],
        guardrails=["do_not_touch_production_ledger"],
    )

    assert set(packs) == {
        "documentation_cleanup",
        "reference_corpus",
        "preferences",
        "style",
        "current_work",
        "required_verification",
        "do_not_touch_boundaries",
    }
    assert packs["required_verification"]["objects"][0]["title"] == "cd worker && uv run pytest -q"
    assert packs["do_not_touch_boundaries"]["objects"][0]["title"] == "do_not_touch_production_ledger"
