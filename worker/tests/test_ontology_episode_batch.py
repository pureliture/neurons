from __future__ import annotations

from agent_knowledge.llm_brain_core import (
    BrainReadService,
    FakeGraphMemoryAdapter,
    GraphProjectionWorker,
    SourceRefRecord,
    build_ontology_episode_batch,
    build_ontology_episode_batch_report,
)
from agent_knowledge.llm_brain_core.models import SessionMemoryArtifact


def test_ontology_episode_batch_maps_artifacts_cards_and_source_refs():
    artifact = SessionMemoryArtifact.from_summary(
        session_id_hash=_h("session"),
        project="neurons",
        provider="codex",
        summary="Session stopped before graph smoke.",
        source_event_ids=["evt_session"],
        chunk_refs=["src_chunk"],
    )
    source_ref = SourceRefRecord(
        source_ref_id="src_design",
        device_id_hash=_h("device-a"),
        root_id="project-root",
        relative_path_hash=_h("specs/design.md"),
        content_hash=_h("content"),
        mtime="2026-06-19T00:00:00Z",
        size=100,
        sync_policy="metadata_only",
        last_seen_at="2026-06-19T00:00:00Z",
    )
    episodes = build_ontology_episode_batch(
        artifacts=[artifact],
        memory_cards=[
            _card(
                "mem_task",
                "task",
                "Graph smoke task",
                {
                    "task_state": "Run graph smoke",
                    "next_action": "Start Neo4j profile on a Compose host",
                    "status": "open",
                },
            ),
            _card(
                "mem_decision",
                "decision",
                "Graph stays derived",
                {
                    "decision": "Graph stays derived.",
                    "rationale": "Canonical artifacts and MemoryCards remain winners.",
                },
            ),
        ],
        source_refs=[source_ref],
    )

    assert [episode.entity_type for episode in episodes] == ["Session", "Task", "Decision", "SourceRef"]
    assert episodes[0].payload["artifact_id"] == artifact.artifact_id
    assert episodes[-1].payload["relative_path_hash"].startswith("sha256:")
    assert "specs/design.md" not in str(episodes[-1].to_dict())


def test_projection_worker_projects_full_ontology_batch_and_context_uses_graph_task():
    graph = FakeGraphMemoryAdapter()
    worker = GraphProjectionWorker(graph)
    source_ref = SourceRefRecord(
        source_ref_id="src_graph_only",
        device_id_hash=_h("device-a"),
        root_id="project-root",
        relative_path_hash=_h("specs/design.md"),
        content_hash=_h("content"),
        mtime="2026-06-19T00:00:00Z",
        size=100,
        sync_policy="metadata_only",
        last_seen_at="2026-06-19T00:00:00Z",
    )
    report = worker.project_batch(
        artifacts=[
            SessionMemoryArtifact.from_summary(
                session_id_hash=_h("session"),
                project="neurons",
                provider="codex",
                summary="Graph-only context artifact.",
                source_event_ids=["evt_session"],
            )
        ],
        memory_cards=[
            _card(
                "mem_graph_only_task",
                "task",
                "Graph-only task",
                {
                    "task_state": "Restore latest task from graph",
                    "next_action": "Use graph task when canonical card store is empty",
                    "status": "open",
                },
            )
        ],
        source_refs=[source_ref],
    ).to_dict()
    service = BrainReadService(graph_adapter=graph)

    pack = service.brain_context_resolve(
        repository="neurons",
        branch="codex/llm-brain-core-design",
        current_files=[],
        current_request="latest task graph",
        project="neurons",
    ).to_dict()

    assert report["status"] == "succeeded"
    assert report["projected"] == 3
    assert pack["current_task"] == "Restore latest task from graph"
    assert pack["last_stopped_at"] == "Use graph task when canonical card store is empty"
    assert pack["graph_status"]["status"] == "available"


def test_ontology_batch_reports_bad_card_and_projects_valid_items():
    graph = FakeGraphMemoryAdapter()
    worker = GraphProjectionWorker(graph)
    valid = _card(
        "mem_valid_task",
        "task",
        "Valid task",
        {
            "task_state": "Valid task",
            "next_action": "Continue after malformed card",
            "status": "open",
        },
    )
    invalid = {"memory_id": "", "card_type": "task"}

    batch = build_ontology_episode_batch_report(memory_cards=[valid, invalid]).to_dict()
    report = worker.project_batch(memory_cards=[valid, invalid]).to_dict()

    assert len(batch["episodes"]) == 1
    assert batch["failures"][0]["item_type"] == "memory_card"
    assert report["status"] == "partial"
    assert report["attempted"] == 2
    assert report["projected"] == 1
    assert report["failed"] == 1


def test_ontology_batch_reports_non_mapping_memory_card_without_crashing():
    batch = build_ontology_episode_batch_report(memory_cards=[object()]).to_dict()

    assert batch["episodes"] == []
    assert batch["failures"][0]["item_type"] == "memory_card"
    assert batch["failures"][0]["item_id"] == ""
    assert batch["failures"][0]["reason_code"] in {"AttributeError", "TypeError"}


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
        "source_refs": [{"source_ref_id": "src_design", "content_hash": _h("content")}],
        "derived_from": ["evt_card"],
        "typed_payload": typed_payload,
    }


def _h(value):
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()
