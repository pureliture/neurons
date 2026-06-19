from __future__ import annotations

from agent_knowledge.llm_brain_core import FakeGraphMemoryAdapter, GraphProjectionWorker, NullGraphMemoryAdapter


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


def test_graph_projection_worker_treats_duplicate_only_projection_as_success():
    graph = FakeGraphMemoryAdapter()
    worker = GraphProjectionWorker(graph)
    card = _card(
        "mem_graph_duplicate",
        "task",
        "Duplicate graph task",
        {"task_state": "Duplicate graph task"},
    )

    worker.project_memory_cards([card])
    report = worker.project_memory_cards([card]).to_dict()

    assert report["status"] == "succeeded"
    assert report["projected"] == 0
    assert report["duplicates"] == 1
    assert report["failed"] == 0


def test_graph_projection_worker_counts_disabled_graph_as_skipped_not_failed():
    worker = GraphProjectionWorker(NullGraphMemoryAdapter())

    report = worker.project_memory_cards(
        [
            _card(
                "mem_graph_disabled",
                "task",
                "Disabled graph task",
                {"task_state": "Disabled graph task"},
            )
        ]
    ).to_dict()

    # Graph intentionally disabled is a clean skip, never a failure or exit-1.
    assert report["status"] == "succeeded"
    assert report["projected"] == 0
    assert report["skipped_disabled"] == 1
    assert report["failed"] == 0
    assert report["failures"] == []


def test_graph_projection_worker_counts_unavailable_backend_as_failed():
    from agent_knowledge.llm_brain_core import UnavailableGraphMemoryAdapter

    worker = GraphProjectionWorker(UnavailableGraphMemoryAdapter("ConnectionError"))

    report = worker.project_memory_cards(
        [
            _card(
                "mem_graph_unavailable",
                "task",
                "Unavailable graph task",
                {"task_state": "Unavailable graph task"},
            )
        ]
    ).to_dict()

    # A graph that was requested but unreachable is a real failure.
    assert report["status"] == "failed"
    assert report["projected"] == 0
    assert report["skipped_disabled"] == 0
    assert report["failed"] == 1
    assert report["failures"][0]["reason_code"] == "failed"


def test_resume_skips_already_projected_episodes_without_upsert_round_trip():
    # A re-run with the prior run's episode_ids must skip those episodes entirely
    # (no upsert call), counted as skipped_resumed, not duplicate.
    counting = _CountingUpsertAdapter()
    worker = GraphProjectionWorker(counting)
    card = _card(
        "mem_resume",
        "task",
        "Resume graph task",
        {"task_state": "Resume graph task", "next_action": "resume"},
    )

    first = worker.project_memory_cards([card]).to_dict()
    projected_id = first["episode_ids"][0]
    counting.calls = 0

    resumed = worker.project_memory_cards(
        [card], resume_projected_ids={projected_id}
    ).to_dict()

    assert first["projected"] == 1
    assert resumed["skipped_resumed"] == 1
    assert resumed["projected"] == 0
    assert resumed["duplicates"] == 0
    assert resumed["status"] == "succeeded"
    # No upsert round-trip happened on the resumed run.
    assert counting.calls == 0


def test_resume_only_skips_listed_ids_and_projects_the_rest():
    graph = FakeGraphMemoryAdapter()
    worker = GraphProjectionWorker(graph)
    done = _card("mem_done", "task", "Done task", {"task_state": "Done task"})
    new = _card("mem_new", "task", "New task", {"task_state": "New task"})

    seed = worker.project_memory_cards([done]).to_dict()
    done_id = seed["episode_ids"][0]

    report = worker.project_memory_cards(
        [done, new], resume_projected_ids={done_id}
    ).to_dict()

    assert report["skipped_resumed"] == 1
    assert report["projected"] == 1
    assert report["attempted"] == 2


class _CountingUpsertAdapter:
    def __init__(self):
        self.calls = 0

    def upsert_episode(self, episode):
        self.calls += 1
        return "inserted"

    def search_context(self, *, brain_id, query, entity_types=None, limit=10):
        from agent_knowledge.llm_brain_core.models import GraphMemoryResult

        return GraphMemoryResult(status="available")


def test_fake_graph_memory_adapter_filters_by_brain_id():
    graph = FakeGraphMemoryAdapter()
    worker = GraphProjectionWorker(graph)
    worker.project_memory_cards(
        [
            _card("mem_graph_neurons", "task", "Neurons graph task", {"task_state": "Neurons graph task"}),
            _card(
                "mem_graph_other",
                "task",
                "Neurons graph task from other brain",
                {"task_state": "Neurons graph task from other brain"},
                brain_id="/project/other",
                project="other",
            ),
        ]
    )

    result = graph.search_context(brain_id="/project/neurons", query="Neurons graph task", limit=10)

    assert [episode.payload["brain_id"] for episode in result.episodes] == ["/project/neurons"]


def _card(memory_id, card_type, summary, typed_payload, *, brain_id="/project/neurons", project="neurons"):
    return {
        "memory_id": memory_id,
        "brain_id": brain_id,
        "card_type": card_type,
        "scope": "project",
        "project": project,
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
