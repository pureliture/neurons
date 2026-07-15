from __future__ import annotations

from types import SimpleNamespace

from agent_knowledge.llm_brain_core.context_builder import (
    ContextPackBuilder,
    build_agent_context_product_pack,
    needs_runtime_evidence,
)
from agent_knowledge.llm_brain_core.models import GraphMemoryResult, OntologyEpisode


def _graph_task_episode(task_state: str, next_action: str) -> OntologyEpisode:
    return OntologyEpisode.from_payload(
        event_id="evt_graph_task",
        entity_type="Task",
        natural_id="task:graph",
        payload={
            "brain_id": "/project/neurons",
            "task_state": task_state,
            "next_action": next_action,
        },
        observed_at="2026-06-19T00:00:00+00:00",
    )


def _available(*episodes: OntologyEpisode) -> GraphMemoryResult:
    return GraphMemoryResult(status="available", episodes=tuple(episodes))


def _task_card(memory_id: str, task_state: str, next_action: str) -> dict:
    return {
        "memory_id": memory_id,
        "card_type": "task",
        "project": "neurons",
        "title": task_state,
        "summary": task_state,
        "currentness": "current",
        "typed_payload": {"task_state": task_state, "next_action": next_action, "status": "open"},
    }


def _artifact_preference_card(*, memory_id: str, project: str, rule: str) -> dict:
    suffix = memory_id.replace("_", "-")
    return {
        "memory_id": memory_id,
        "card_type": "preference",
        "project": project,
        "title": rule,
        "summary": rule,
        "currentness": "current",
        "lifecycle_state": "accepted",
        "approval_state": "approved",
        "content_hash": "sha256:" + "c" * 64,
        "confidence": 0.95,
        "typed_payload": {
            "preference": rule,
            "applies_to": "html review artifact",
            "source_object_type": "ArtifactPreference",
            "target_object_id": f"ko:ArtifactPreference:{suffix}",
            "source_content_hash": "sha256:" + "a" * 64,
            "authority_proposal_id": f"proposal:{suffix}",
            "authority_decision_id": f"decision:{suffix}",
        },
    }


def test_builder_prefers_card_over_artifact_and_graph_for_current_task():
    # card > artifact > graph: the card task wins even when an artifact and a
    # graph task are also present.
    builder = ContextPackBuilder()
    artifact = SimpleNamespace(summary="Artifact summary should not win over card")
    graph = _available(_graph_task_episode("Graph task should not win", "graph next"))

    pack = builder.build(
        brain_id="/project/neurons",
        repository="neurons",
        branch="main",
        current_files=[],
        current_request="task",
        artifacts=[artifact],
        cards=[_task_card("mem_a", "Card task wins", "Card next action")],
        graph_result=graph,
        incidents=(),
        bridge_status={"status": "disabled", "authority": "bridge", "details": []},
        bridge_evidence=(),
    ).to_dict()

    assert pack["current_task"] == "Card task wins"
    assert pack["last_stopped_at"] == "Card next action"


def test_builder_falls_back_to_artifact_then_graph_when_no_card_task():
    builder = ContextPackBuilder()
    artifact = SimpleNamespace(summary="Artifact summary becomes current task")
    graph = _available(_graph_task_episode("Graph task fallback", "Graph next action"))

    # No cards: artifact summary becomes current_task; graph supplies last_stop
    # only when the artifact does not.
    pack_with_artifact = builder.build(
        brain_id="/project/neurons",
        repository="neurons",
        branch="main",
        current_files=[],
        current_request="task",
        artifacts=[artifact],
        cards=[],
        graph_result=graph,
        incidents=(),
        bridge_status={"status": "disabled", "authority": "bridge", "details": []},
        bridge_evidence=(),
    ).to_dict()

    assert pack_with_artifact["current_task"] == "Artifact summary becomes current task"
    assert pack_with_artifact["last_stopped_at"] == "Artifact summary becomes current task"

    # No cards and no artifacts: the derived graph fills both, and the
    # no_canonical_memory gap is flagged.
    pack_graph_only = builder.build(
        brain_id="/project/neurons",
        repository="neurons",
        branch="main",
        current_files=[],
        current_request="task",
        artifacts=[],
        cards=[],
        graph_result=graph,
        incidents=(),
        bridge_status={"status": "disabled", "authority": "bridge", "details": []},
        bridge_evidence=(),
    ).to_dict()

    assert pack_graph_only["current_task"] == "Graph task fallback"
    assert pack_graph_only["last_stopped_at"] == "Graph next action"
    assert "no_canonical_memory" in pack_graph_only["gaps"]


def test_builder_projects_one_ranked_current_work_candidate_into_agent_context():
    builder = ContextPackBuilder()
    artifact = SimpleNamespace(
        artifact_id="session-memory:artifact-current-work",
        project="neurons",
        summary="Resume the latest canonical session work",
        content_hash="sha256:" + "b" * 64,
        created_at="2026-07-15T00:00:00+00:00",
    )
    graph = _available(_graph_task_episode("Graph work", "Resume graph work"))

    card_pack = builder.build(
        brain_id="/project/neurons",
        repository="neurons",
        branch="main",
        current_files=[],
        current_request="resume P9",
        artifacts=[artifact],
        cards=[_task_card("mem_work", "Accepted card work", "Resume card work")],
        graph_result=graph,
        incidents=(),
        bridge_status={"status": "disabled", "authority": "bridge", "details": []},
        bridge_evidence=(),
        consumer="codex",
    ).to_dict()
    artifact_pack = builder.build(
        brain_id="/project/neurons",
        repository="neurons",
        branch="main",
        current_files=[],
        current_request="resume P9",
        artifacts=[artifact],
        cards=[],
        graph_result=graph,
        incidents=(),
        bridge_status={"status": "disabled", "authority": "bridge", "details": []},
        bridge_evidence=(),
        consumer="codex",
    ).to_dict()
    graph_pack = builder.build(
        brain_id="/project/neurons",
        repository="neurons",
        branch="main",
        current_files=[],
        current_request="resume P9",
        artifacts=[],
        cards=[],
        graph_result=graph,
        incidents=(),
        bridge_status={"status": "disabled", "authority": "bridge", "details": []},
        bridge_evidence=(),
        consumer="codex",
    ).to_dict()

    card_work = card_pack["authority"]["agent_context_product"]["sections"]["active_work"]
    artifact_work = artifact_pack["authority"]["agent_context_product"]["sections"]["active_work"]
    graph_work = graph_pack["authority"]["agent_context_product"]["sections"]["active_work"]
    assert [(item["title"], item["authority_lane"]) for item in card_work["items"]] == [
        ("Accepted card work", "accepted_current")
    ]
    assert [(item["title"], item["authority_lane"]) for item in artifact_work["items"]] == [
        ("Resume the latest canonical session work", "reference_only")
    ]
    assert [(item["title"], item["authority_lane"]) for item in graph_work["items"]] == [
        ("Graph work", "derived_projection")
    ]


def test_builder_excludes_terminal_and_non_current_tasks_from_active_work():
    terminal_graph = OntologyEpisode.from_payload(
        event_id="evt_terminal_graph_task",
        entity_type="Task",
        natural_id="task:terminal-graph",
        payload={
            "brain_id": "/project/neurons",
            "task_state": "Completed graph task",
            "next_action": "Do not resume",
            "status": "completed",
        },
        observed_at="2026-07-15T00:00:00+00:00",
    )
    retired_card = _task_card("mem_retired", "Retired card task", "Do not resume")
    retired_card["currentness"] = "retired"

    pack = ContextPackBuilder().build(
        brain_id="/project/neurons",
        repository="neurons",
        branch="main",
        current_files=[],
        current_request="resume P9",
        artifacts=[],
        cards=[retired_card],
        graph_result=_available(terminal_graph),
        incidents=(),
        bridge_status={"status": "disabled", "authority": "bridge", "details": []},
        bridge_evidence=(),
        consumer="codex",
    ).to_dict()

    active_work = pack["authority"]["agent_context_product"]["sections"]["active_work"]
    assert active_work["object_count"] == 0
    assert pack["current_task"] == ""


def test_builder_exposes_project_accepted_artifact_preference_without_keyword_match():
    local = _artifact_preference_card(
        memory_id="mem_local_html_preference",
        project="neurons",
        rule="Prefer dense evidence-first HTML review artifacts.",
    )
    other = _artifact_preference_card(
        memory_id="mem_other_html_preference",
        project="other-project",
        rule="Cross-project preference must stay hidden.",
    )

    pack = ContextPackBuilder().build(
        brain_id="/project/neurons",
        repository="neurons",
        branch="main",
        current_files=[],
        current_request="P9 startup context",
        artifacts=[],
        cards=[local, other],
        graph_result=GraphMemoryResult(status="available"),
        incidents=(),
        bridge_status={"status": "disabled", "authority": "bridge", "details": []},
        bridge_evidence=(),
        consumer="codex",
    ).to_dict()

    product = pack["authority"]["agent_context_product"]
    authority = product["sections"]["current_authority"]
    style = product["sections"]["style_preference"]
    assert authority["object_count"] == 1
    assert authority["authority_lanes"] == ["accepted_current"]
    assert all(item["authority_lane"] == "accepted_current" for item in authority["items"])
    assert [item["title"] for item in style["items"]] == [
        "Prefer dense evidence-first HTML review artifacts."
    ]
    assert style["authority_lanes"] == ["accepted_current"]
    assert style["items"][0]["payload"]["applies_to"] == "html review artifact"
    assert style["items"][0]["payload"]["applies_to_current_request"] is False


def test_builder_flags_graph_edge_degraded_distinct_from_unavailable():
    builder = ContextPackBuilder()

    degraded = builder.build(
        brain_id="/project/neurons",
        repository="neurons",
        branch="main",
        current_files=[],
        current_request="task",
        artifacts=[SimpleNamespace(summary="art")],
        cards=[],
        graph_result=GraphMemoryResult(status="degraded", details=("graph_edge_degraded",)),
        incidents=(),
        bridge_status={"status": "disabled", "authority": "bridge", "details": []},
        bridge_evidence=(),
    ).to_dict()
    unavailable = builder.build(
        brain_id="/project/neurons",
        repository="neurons",
        branch="main",
        current_files=[],
        current_request="task",
        artifacts=[SimpleNamespace(summary="art")],
        cards=[],
        graph_result=GraphMemoryResult(status="error", details=("boom",)),
        incidents=(),
        bridge_status={"status": "disabled", "authority": "bridge", "details": []},
        bridge_evidence=(),
    ).to_dict()

    assert "graph_edge_degraded" in degraded["gaps"]
    assert "graph_unavailable" not in degraded["gaps"]
    assert "graph_unavailable" in unavailable["gaps"]
    assert "graph_edge_degraded" not in unavailable["gaps"]


def test_builder_adds_consumer_specific_compact_agent_context_pack_with_safe_action_hints():
    builder = ContextPackBuilder()
    cards = [
        _task_card("mem_task", "Continue P9 agent context productization", "Run focused context pack tests"),
        {
            "memory_id": "mem_pref",
            "card_type": "preference",
            "summary": "Use concise Korean status updates.",
            "currentness": "current",
            "confidence": 0.9,
            "typed_payload": {
                "preference": "Use concise Korean status updates.",
                "applies_to": "communication",
            },
            "source_refs": [{"source_ref_id": "session:accepted-pref"}],
        },
        {
            "memory_id": "mem_stale",
            "card_type": "decision",
            "summary": "Old deployment claim.",
            "currentness": "stale",
            "typed_payload": {"decision": "Old deployment claim."},
        },
    ]

    for consumer in ("codex", "claude-code", "gemini", "hermes"):
        pack = builder.build(
            brain_id="/project/neurons",
            repository="neurons",
            branch="main",
            current_files=["docs/specs/roadmap.md"],
            current_request="이 PR merge됐어? 배포도 됐어?",
            artifacts=[],
            cards=cards,
            graph_result=GraphMemoryResult(status="degraded", details=("edge index unavailable",)),
            incidents=(),
            bridge_status={"status": "disabled", "authority": "bridge", "details": []},
            bridge_evidence=(),
            consumer=consumer,
        ).to_dict()

        product = pack["authority"]["agent_context_product"]
        assert product["schema_version"] == "agent_context_product_pack.v1"
        assert product["consumer"] == consumer
        assert set(product["sections"]) == {
            "current_authority",
            "reference_objects",
            "style_preference",
            "active_work",
            "guardrails",
            "required_verification",
        }
        assert product["degraded_mode"]["active"] is True
        assert "runtime_evidence_unverified" in product["degraded_mode"]["gaps"]
        assert product["freshness"]["stale_evidence_visible"] is True
        assert product["freshness"]["stale_memory_count"] == 1
        assert "runtime_evidence_unverified" in product["missing_evidence_before_promotion"]
        assert product["surface_policy"]["property_omissions"] == [
            "raw_body",
            "raw_source",
            "private_deploy_value",
            "secret",
        ]
        assert product["surface_policy"]["mutation_allowed"] is False
        assert product["sections"]["reference_objects"]["object_count"] >= 1
        expected_blockers = ["runtime_evidence_unverified"]
        assert product["action_hints"] == [
            {
                "action": "request_missing_evidence",
                "suggest_allowed": True,
                "execute_allowed": False,
                "blocked_by": expected_blockers,
            },
            {
                "action": "promote_authority",
                "suggest_allowed": True,
                "execute_allowed": False,
                "blocked_by": ["approved_scope_required", *expected_blockers],
            },
        ]
        tool_hints = {item["tool"]: item for item in product["tool_hints"]}
        assert set(tool_hints) == {
            "brain_objects_query",
            "brain_source_to_candidate_graph",
            "brain_candidate_review_edit",
            "brain_approval_board_decide",
            "brain_source_to_candidate_runtime_readiness",
        }
        assert tool_hints["brain_objects_query"]["suggest_allowed"] is True
        assert tool_hints["brain_objects_query"]["execute_allowed"] is False
        assert tool_hints["brain_objects_query"]["production_mutation_allowed"] is False
        assert tool_hints["brain_objects_query"]["safe_targets"] == ["read_only_object_pack"]
        assert tool_hints["brain_source_to_candidate_graph"]["suggest_allowed"] is True
        assert tool_hints["brain_source_to_candidate_graph"]["execute_allowed"] is False
        assert tool_hints["brain_source_to_candidate_graph"]["production_mutation_allowed"] is False
        assert tool_hints["brain_source_to_candidate_graph"]["blocked_targets"] == ["production"]
        assert tool_hints["brain_candidate_review_edit"]["execute_allowed"] is False
        assert "accepted_current_authority" in tool_hints["brain_candidate_review_edit"]["blocked_targets"]
        assert tool_hints["brain_approval_board_decide"]["blocked_by"] == [
            "approved_scope_required",
            *expected_blockers,
        ]
        assert tool_hints["brain_approval_board_decide"]["production_mutation_allowed"] is False
        readiness_hint = tool_hints["brain_source_to_candidate_runtime_readiness"]
        assert readiness_hint["execute_allowed"] is False
        assert readiness_hint["production_mutation_allowed"] is False
        assert readiness_hint["safe_targets"] == ["sanitized_evidence_packet"]
        assert "raw_private_runtime_evidence" in readiness_hint["blocked_targets"]


def test_builder_marks_empty_required_agent_context_sections_as_actionable_gaps():
    pack = ContextPackBuilder().build(
        brain_id="/project/neurons",
        repository="neurons",
        branch="main",
        current_files=["docs/specs/roadmap.md"],
        current_request="P9 agent context status",
        artifacts=[],
        cards=[],
        graph_result=GraphMemoryResult(status="available"),
        incidents=(),
        bridge_status={"status": "disabled", "authority": "bridge", "details": []},
        bridge_evidence=(),
        consumer="codex",
    ).to_dict()

    product = pack["authority"]["agent_context_product"]
    style_section = product["sections"]["style_preference"]
    active_work_section = product["sections"]["active_work"]
    section_gaps = [
        "agent_context_style_preference_missing",
        "agent_context_active_work_missing",
    ]

    assert style_section["object_count"] == 0
    assert active_work_section["object_count"] == 0
    assert "agent_context_style_preference_missing" in style_section["gaps"]
    assert "agent_context_active_work_missing" in active_work_section["gaps"]
    assert all(gap in product["degraded_mode"]["gaps"] for gap in section_gaps)
    assert all(gap in product["missing_evidence_before_promotion"] for gap in section_gaps)
    request_hint = next(
        item for item in product["action_hints"] if item["action"] == "request_missing_evidence"
    )
    assert all(gap in request_hint["blocked_by"] for gap in section_gaps)
    assert product["surface_policy"]["mutation_allowed"] is False


def test_builder_filters_stale_preference_from_compact_style_guidance():
    builder = ContextPackBuilder()
    cards = [
        {
            "memory_id": "mem_current_pref",
            "card_type": "preference",
            "summary": "Use concise Korean status updates.",
            "currentness": "current",
            "confidence": 0.9,
            "typed_payload": {
                "preference": "Use concise Korean status updates.",
                "applies_to": "communication",
            },
        },
        {
            "memory_id": "mem_stale_pref",
            "card_type": "preference",
            "summary": "Use stale verbose status updates.",
            "currentness": "stale",
            "confidence": 0.99,
            "typed_payload": {
                "preference": "Use stale verbose status updates.",
                "applies_to": "communication",
            },
        },
    ]

    pack = builder.build(
        brain_id="/project/neurons",
        repository="neurons",
        branch="main",
        current_files=["docs/specs/roadmap.md"],
        current_request="status update",
        artifacts=[],
        cards=cards,
        graph_result=GraphMemoryResult(status="available"),
        incidents=(),
        bridge_status={"status": "disabled", "authority": "bridge", "details": []},
        bridge_evidence=(),
        consumer="codex",
    ).to_dict()

    style_items = pack["authority"]["agent_context_product"]["sections"]["style_preference"]["items"]
    titles = [item["title"] for item in style_items]
    assert "Use concise Korean status updates." in titles
    assert "Use stale verbose status updates." not in titles


def test_agent_context_product_pack_defensively_compacts_dynamic_object_packs():
    product = build_agent_context_product_pack(
        consumer="codex",
        block={
            "object_packs": {
                "preferences": {
                    "lanes": None,
                    "gaps": None,
                    "objects": [
                        None,
                        "not-an-object",
                        {
                            "object_id": "obj:pref",
                            "object_type": "ArtifactPreference",
                            "title": "Current preference",
                            "authority_lane": "accepted_current",
                            "recommended_action": "apply_preference",
                            "scope": {"project": "neurons"},
                            "content_hash": "sha256:" + "a" * 64,
                            "payload": {
                                "target_object_id": "ko:ArtifactPreference:html-review-density",
                                "memory_id": "mem_artifact_preference",
                                "card_content_hash": "sha256:" + "c" * 64,
                                "authority_proposal_id": "proposal:p7-html-review-density",
                                "authority_decision_id": "decision:p7-html-review-density",
                                "project": "neurons",
                                "source_content_hash": "sha256:" + "a" * 64,
                            },
                        },
                    ],
                }
            }
        },
        gaps=[],
        cards=[],
    )

    section = product["sections"]["style_preference"]
    assert section["object_count"] == 1
    assert section["items"][0]["title"] == "Current preference"
    assert section["items"][0]["payload"] == {
        "target_object_id": "ko:ArtifactPreference:html-review-density",
        "memory_id": "mem_artifact_preference",
        "card_content_hash": "sha256:" + "c" * 64,
        "authority_proposal_id": "proposal:p7-html-review-density",
        "authority_decision_id": "decision:p7-html-review-density",
        "project": "neurons",
        "source_content_hash": "sha256:" + "a" * 64,
    }
    assert section["gaps"] == []


def test_agent_context_product_pack_discloses_reference_only_current_authority_gap():
    product = build_agent_context_product_pack(
        consumer="codex",
        block={
            "object_packs": {
                "documentation_cleanup": {
                    "lanes": {
                        "accepted_current": [],
                        "reference_only": [{"object_id": "obj:doc"}],
                    },
                    "gaps": ["accepted_current documents empty"],
                    "objects": [
                        {
                            "object_id": "obj:doc",
                            "object_type": "RepoDocument",
                            "title": "docs/specs/roadmap.md",
                            "authority_lane": "reference_only",
                            "recommended_action": "review",
                        },
                    ],
                },
            },
        },
        gaps=[],
        cards=[],
    )

    section = product["sections"]["current_authority"]
    assert section["object_count"] == 0
    assert section["authority_lanes"] == []
    assert section["items"] == []
    assert "agent_context_current_authority_accepted_current_missing" in section["gaps"]
    assert "agent_context_current_authority_accepted_current_missing" in product["degraded_mode"]["gaps"]
    assert (
        "agent_context_current_authority_accepted_current_missing"
        in product["missing_evidence_before_promotion"]
    )
    assert (
        "agent_context_current_authority_accepted_current_missing"
        in product["action_hints"][1]["blocked_by"]
    )


def test_agent_context_product_pack_discloses_reference_only_style_preference_gap():
    product = build_agent_context_product_pack(
        consumer="codex",
        block={
            "object_packs": {
                "documentation_cleanup": {
                    "lanes": {
                        "accepted_current": [{"object_id": "obj:doc"}],
                    },
                    "objects": [
                        {
                            "object_id": "obj:doc",
                            "object_type": "RepoDocument",
                            "title": "README.md",
                            "authority_lane": "accepted_current",
                            "recommended_action": "keep",
                        },
                    ],
                },
                "style": {
                    "lanes": {
                        "reference_only": [{"object_id": "obj:style"}],
                    },
                    "objects": [
                        {
                            "object_id": "obj:style",
                            "object_type": "StyleRule",
                            "title": "Repository prefers compact summaries.",
                            "authority_lane": "reference_only",
                            "recommended_action": "review",
                        },
                    ],
                },
            },
        },
        gaps=[],
        cards=[],
    )

    section = product["sections"]["style_preference"]
    assert section["object_count"] == 0
    assert section["authority_lanes"] == []
    assert section["items"] == []
    assert section["suggestion_object_count"] == 1
    assert section["suggestion_authority_lanes"] == ["reference_only"]
    assert section["suggestion_items"][0]["title"] == "Repository prefers compact summaries."
    assert "agent_context_style_preference_missing" in section["gaps"]
    assert "agent_context_style_preference_missing" in product["degraded_mode"]["gaps"]
    assert (
        "agent_context_style_preference_missing"
        in product["missing_evidence_before_promotion"]
    )


def test_needs_runtime_evidence_handles_missing_current_request():
    assert needs_runtime_evidence(None, ["deploy/status.md"]) is True
