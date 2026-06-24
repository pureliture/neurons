from __future__ import annotations

import json

from agent_knowledge.llm_brain_core import (
    BrainReadService,
    FakeGraphMemoryAdapter,
    InMemoryHybridTextMirror,
    MetadataFirstHybridGraphAdapter,
    metadata_first_episode,
)
from agent_knowledge.llm_brain_core.models import OntologyEpisode


def test_metadata_first_episode_strips_free_text_from_graph_payload():
    episode = _task_episode()

    metadata_episode = metadata_first_episode(episode)
    body = json.dumps(metadata_episode.to_dict(), sort_keys=True)

    assert metadata_episode.episode_id == episode.episode_id
    assert metadata_episode.payload["metadata_first"] is True
    assert metadata_episode.payload["memory_id"] == "mem_hybrid_task"
    assert metadata_episode.payload["brain_id"] == "/project/neurons"
    assert metadata_episode.payload["scope"] == "project"
    assert metadata_episode.payload["source_payload_hash"].startswith("sha256:")
    assert metadata_episode.payload["source_text_hash"].startswith("sha256:")
    assert "Restart Neo4j graph projection" not in body
    assert "Check metadata-first hybrid recall" not in body
    assert "typed_payload" not in metadata_episode.payload


def test_hybrid_adapter_stores_metadata_in_graph_and_joins_text_mirror_on_search():
    graph = FakeGraphMemoryAdapter()
    mirror = InMemoryHybridTextMirror()
    adapter = MetadataFirstHybridGraphAdapter(graph, text_mirror=mirror)

    assert adapter.upsert_episode(_task_episode()) == "inserted"
    stored = graph.search_context(
        brain_id="/project/neurons",
        query="",
        entity_types=["Task"],
        limit=5,
    ).episodes[0]
    stored_body = json.dumps(stored.to_dict(), sort_keys=True)

    assert stored.payload["metadata_first"] is True
    assert "Restart Neo4j graph projection" not in stored_body

    result = adapter.search_context(
        brain_id="/project/neurons",
        query="Restart Neo4j graph projection",
        entity_types=["Task"],
        limit=5,
    )

    assert result.status == "available"
    assert result.episodes[0].payload["task_state"] == "Restart Neo4j graph projection"
    assert result.episodes[0].payload["typed_payload"]["next_action"] == "Check metadata-first hybrid recall"
    assert "metadata_first_hybrid" in result.details
    assert "text_mirror_hits:1" in result.details

    exact = adapter.get_episodes_by_ids([_task_episode().episode_id], brain_id="/project/neurons")
    assert exact[0].payload["metadata_first"] is True


def test_contextpack_can_restore_task_from_metadata_first_hybrid_graph():
    adapter = MetadataFirstHybridGraphAdapter(
        FakeGraphMemoryAdapter(),
        text_mirror=InMemoryHybridTextMirror(),
    )
    adapter.upsert_episode(_task_episode())
    service = BrainReadService(graph_adapter=adapter)

    pack = service.brain_context_resolve(
        repository="/Users/example/Projects/neurons",
        branch="codex/metadata-first-hybrid-graph",
        current_files=[],
        current_request="Restart Neo4j graph projection",
        project="neurons",
    ).to_dict()

    assert pack["current_task"] == "Restart Neo4j graph projection"
    assert pack["last_stopped_at"] == "Check metadata-first hybrid recall"
    assert pack["graph_status"]["status"] == "available"
    assert "metadata_first_hybrid" in pack["graph_status"]["details"]
    assert "/Users/" not in json.dumps(pack, sort_keys=True)


def test_hybrid_text_mirror_uses_project_brain_id_fallback():
    graph = FakeGraphMemoryAdapter()
    mirror = InMemoryHybridTextMirror()
    adapter = MetadataFirstHybridGraphAdapter(graph, text_mirror=mirror)
    episode = _task_episode()
    payload = dict(episode.payload)
    payload["brain_id"] = ""
    project_only = OntologyEpisode(
        episode_id=episode.episode_id,
        event_id=episode.event_id,
        idempotency_key=episode.idempotency_key,
        entity_type=episode.entity_type,
        natural_id=episode.natural_id,
        lifecycle_state=episode.lifecycle_state,
        currentness=episode.currentness,
        source_event_ids=episode.source_event_ids,
        source_ref_ids=episode.source_ref_ids,
        valid_from=episode.valid_from,
        valid_to=episode.valid_to,
        observed_at=episode.observed_at,
        reference_time=episode.reference_time,
        content_hash=episode.content_hash,
        ontology_version=episode.ontology_version,
        extractor_version=episode.extractor_version,
        payload=payload,
        relations=episode.relations,
    )

    adapter.upsert_episode(project_only)
    result = adapter.search_context(
        brain_id="/project/neurons",
        query="Restart Neo4j graph projection",
        entity_types=["Task"],
        limit=5,
    )

    assert [item.episode_id for item in result.episodes] == [episode.episode_id]


def test_hybrid_join_uses_episode_id_lookup_not_bounded_metadata_pool():
    graph = FakeGraphMemoryAdapter()
    mirror = InMemoryHybridTextMirror()
    adapter = MetadataFirstHybridGraphAdapter(graph, text_mirror=mirror)
    target = _episode_with(
        "target_old",
        title="AncientNeedle metadata-first task",
        next_action="Recover old mirror hit",
        observed_at="2026-06-22T00:00:00+00:00",
    )
    adapter.upsert_episode(target)
    for index in range(150):
        adapter.upsert_episode(
            _episode_with(
                f"newer_{index:03d}",
                title=f"Newer filler task {index}",
                next_action="Filler",
                observed_at=f"2026-06-22T01:{index % 60:02d}:00+00:00",
            )
        )

    result = adapter.search_context(
        brain_id="/project/neurons",
        query="AncientNeedle",
        entity_types=["Task"],
        limit=5,
    )

    assert result.status == "available"
    assert [item.episode_id for item in result.episodes] == [target.episode_id]
    assert "metadata_exact_join" in result.details
    assert not any(item.startswith("metadata_join_missing") for item in result.details)


def _task_episode() -> OntologyEpisode:
    return _episode_with(
        "hybrid_task",
        title="Restart Neo4j graph projection",
        next_action="Check metadata-first hybrid recall",
        observed_at="2026-06-22T00:00:00+00:00",
    )


def _episode_with(key: str, *, title: str, next_action: str, observed_at: str) -> OntologyEpisode:
    return OntologyEpisode.from_payload(
        event_id=f"evt_{key}",
        entity_type="Task",
        natural_id=f"task:mem_{key}",
        payload={
            "brain_id": "/project/neurons",
            "project": "neurons",
            "provider": "codex",
            "memory_id": f"mem_{key}",
            "card_type": "task",
            "scope": "project",
            "title": title,
            "summary": f"{title} with metadata-first storage.",
            "typed_payload": {
                "task_state": title,
                "next_action": next_action,
                "status": "open",
            },
        },
        source_event_ids=[f"evt_{key}"],
        source_ref_ids=[f"src_{key}"],
        observed_at=observed_at,
    )
