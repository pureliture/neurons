from __future__ import annotations

from types import SimpleNamespace

from agent_knowledge.llm_brain_core.context_builder import ContextPackBuilder
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
