from __future__ import annotations

from agent_knowledge.llm_brain_core import FakeGraphMemoryAdapter, GraphProjectionWorker


def test_graph_projection_worker_projects_memory_cards_to_derived_graph():
    graph = FakeGraphMemoryAdapter()
    worker = GraphProjectionWorker(graph)

    report = worker.project_memory_cards(
        [
            _card(
                "mem_graph_task",
                "task",
                "Project graph adapter",
                {
                    "task_state": "Project graph adapter",
                    "next_action": "Run Graphiti contract smoke",
                    "status": "open",
                },
            ),
            _card(
                "mem_graph_decision",
                "decision",
                "Graph is derived index",
                {
                    "decision": "Graph is a derived index.",
                    "rationale": "Canonical MemoryCards remain the winner.",
                },
            ),
        ]
    ).to_dict()
    search = graph.search_context(
        brain_id="/project/neurons",
        query="Graphiti contract",
        entity_types=["Task"],
        limit=5,
    )

    assert report["status"] == "succeeded"
    assert report["attempted"] == 2
    assert report["projected"] == 2
    assert report["failed"] == 0
    assert [episode.entity_type for episode in search.episodes] == ["Task"]


def test_graph_projection_worker_reports_mapping_failures_without_throwing():
    graph = FakeGraphMemoryAdapter()
    worker = GraphProjectionWorker(graph)

    report = worker.project_memory_cards([{"memory_id": "", "card_type": "task"}]).to_dict()

    assert report["status"] == "failed"
    assert report["attempted"] == 1
    assert report["projected"] == 0
    assert report["failed"] == 1
    assert report["failures"][0]["phase"] == "map"


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
        "approval_state": "approved",
        "currentness": "current",
        "confidence": 0.9,
        "source_refs": [{"source_ref_id": "src_graph_projection", "content_hash": _h("source")}],
        "derived_from": ["evt_graph_projection"],
        "typed_payload": typed_payload,
    }


def _h(value):
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()
