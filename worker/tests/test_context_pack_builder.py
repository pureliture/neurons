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
        assert product["action_hints"] == [
            {
                "action": "request_missing_evidence",
                "suggest_allowed": True,
                "execute_allowed": False,
                "blocked_by": ["runtime_evidence_unverified"],
            },
            {
                "action": "promote_authority",
                "suggest_allowed": True,
                "execute_allowed": False,
                "blocked_by": ["approved_scope_required", "runtime_evidence_unverified"],
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
            "runtime_evidence_unverified",
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
    assert section["gaps"] == []


def test_needs_runtime_evidence_handles_missing_current_request():
    assert needs_runtime_evidence(None, ["deploy/status.md"]) is True
