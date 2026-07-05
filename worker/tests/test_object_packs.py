from agent_knowledge.llm_brain_core.object_packs import (
    build_agent_context_object_packs,
    build_documentation_cleanup_pack,
    build_runtime_truth_pack,
    route_spec_for,
)


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


def test_route_spec_is_declarative():
    spec = route_spec_for("documentation_cleanup")

    assert spec["required_object_types"] == ["RepoDocument"]
    assert "accepted_current" in spec["allowed_authority_lanes"]
    assert "includes_recommended_action" in spec["eval_assertions"]


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
