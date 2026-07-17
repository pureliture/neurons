from __future__ import annotations

import asyncio
import json
import os
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent_knowledge.llm_brain_core.graph import FakeGraphMemoryAdapter
from agent_knowledge.llm_brain_core.graphiti_adapter import (
    DEFAULT_GRAPH_READ_TIMEOUT_SECONDS,
    DEFAULT_GRAPH_WRITE_TIMEOUT_SECONDS,
    GraphitiNeo4jConfig,
    GraphitiNeo4jGraphMemoryAdapter,
    _AsyncLoopRunner,
    _graphiti_group_id,
    _MAX_EDGE_PROVENANCE_LOOKUPS_IN_FLIGHT,
    _is_list_annotation,
    _placeholder_api_key,
    _ReasoningOpenAIGenericClient,
    _resolve_embedding_dim,
    _datetime_to_iso,
    _episode_node_to_ontology,
    _existing_fact_idx_values_from_messages,
    _normalize_structured_response,
    _uses_configured_llm_client,
)
from agent_knowledge.llm_brain_core.models import OntologyEpisode


def test_graphiti_adapter_upserts_public_safe_json_episode():
    graphiti = _FakeGraphiti()
    adapter = GraphitiNeo4jGraphMemoryAdapter(graphiti, default_group_id="/project/neurons", extract_entities=True)
    episode = _episode("Task", "task:graphiti", {"brain_id": "/project/neurons", "task": "Graphiti adapter"})

    result = adapter.upsert_episode(episode)

    # Typed UpsertEpisodeResult (symmetric with FakeGraphMemoryAdapter), not a
    # raw graph uuid string that projection would always count as `projected`.
    assert result == "inserted"
    assert graphiti.added[0]["name"] == episode.episode_id
    assert graphiti.added[0]["source"] == "json"
    assert graphiti.added[0]["group_id"].startswith("brain_")
    assert "/" not in graphiti.added[0]["group_id"]
    body = json.loads(graphiti.added[0]["episode_body"])
    assert body["episode_id"] == episode.episode_id
    assert body["entity_type"] == "Task"
    assert body["payload"]["task"] == "Graphiti adapter"
    assert "/Users/" not in graphiti.added[0]["episode_body"]


def test_graphiti_adapter_default_path_inserts_then_reports_duplicate_on_reupsert():
    # Production default (extract_entities=False) MERGEs on episode_id. The first
    # upsert saves a node; a second upsert of the same episode_id is a duplicate,
    # symmetric with FakeGraphMemoryAdapter -- not a second `inserted` row.
    graphiti = _FakeGraphiti()
    seen: set[str] = set()

    async def _episode_exists(driver, episode_id):
        _ = driver
        return episode_id in seen

    adapter = GraphitiNeo4jGraphMemoryAdapter(
        graphiti,
        default_group_id="/project/neurons",
        episode_exists=_episode_exists,
    )
    episode = _episode("Task", "task:dup", {"brain_id": "/project/neurons", "task": "merge"})

    first = adapter.upsert_episode(episode)
    seen.add(episode.episode_id)
    second = adapter.upsert_episode(episode)

    assert first == "inserted"
    assert second == "duplicate"
    # The duplicate short-circuits before any second node save attempt.
    assert graphiti.saved_uuids == [episode.episode_id]


def test_graphiti_adapter_default_path_does_not_build_entity_extraction_body(monkeypatch):
    graphiti = _FakeGraphiti()

    def _explode(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("episodic-only path must not prepare entity extraction input")

    monkeypatch.setattr("agent_knowledge.llm_brain_core.graphiti_adapter._extraction_body_for", _explode)
    adapter = GraphitiNeo4jGraphMemoryAdapter(graphiti, default_group_id="/project/neurons")
    episode = _episode("Task", "task:metadata-first", {"brain_id": "/project/neurons", "task": "metadata-first"})

    assert adapter.upsert_episode(episode) == "inserted"
    assert graphiti.saved_uuids == [episode.episode_id]


def test_entity_path_ensures_episode_node_before_add_episode_when_absent():
    # Scenario (a): entity mode + episode_id node ABSENT. The live bug:
    # add_episode(uuid=episode_id) calls graphiti get_by_uuid first, which raises
    # NodeNotFoundError when the node was never written (entity pass run as a
    # separate CLI invocation). The fix ensure-saves the episode_id node first,
    # so add_episode finds it and reports 'inserted' -- no NodeNotFoundError.
    graphiti = _FakeGraphiti()

    async def _not_extracted(driver, episode_id):
        _ = (driver, episode_id)
        return False

    adapter = GraphitiNeo4jGraphMemoryAdapter(
        graphiti,
        default_group_id="/project/neurons",
        extract_entities=True,
        entity_extracted=_not_extracted,
    )
    episode = _episode("Task", "task:reextract", {"brain_id": "/project/neurons", "task": "reextract"})

    result = adapter.upsert_episode(episode)

    assert result == "inserted"
    # The entity path writes the episode_id node before add_episode, then restores
    # canonical JSON afterward. Both writes target the same episode_id node.
    assert graphiti.saved_uuids == [episode.episode_id, episode.episode_id]
    assert graphiti.added[0]["uuid"] == episode.episode_id
    assert graphiti.added[0]["name"] == episode.episode_id


def test_both_paths_build_identical_episode_node_via_shared_helper():
    # Scenario (b): both the episodic-only path and the entity-path ensure-save
    # build the EpisodicNode through the SAME _build_episodic_node helper, so the
    # MERGE key/shape is identical. Identical (uuid, name, content, source,
    # source_description, group_id) means a 2-pass run (episodic then entity)
    # MERGEs onto the same node -- no duplicate, no divergent Episodic node.
    graphiti = _FakeGraphiti()
    adapter = GraphitiNeo4jGraphMemoryAdapter(
        graphiti,
        default_group_id="/project/neurons",
        extract_entities=True,
    )
    episode = _episode("Task", "task:exists", {"brain_id": "/project/neurons", "task": "exists"})
    body = json.dumps(episode.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    group_id = adapter._build_episodic_node(episode, body, "brain_x").group_id  # noqa: SLF001

    node_a = adapter._build_episodic_node(episode, body, group_id)  # noqa: SLF001
    node_b = adapter._build_episodic_node(episode, body, group_id)  # noqa: SLF001

    for attr in ("uuid", "name", "content", "source", "source_description", "group_id"):
        assert getattr(node_a, attr) == getattr(node_b, attr)
    # MERGE key is episode_id; the same content always maps to the same node.
    assert node_a.uuid == episode.episode_id
    assert node_a.name == episode.episode_id
    assert node_a.content == body


def test_entity_path_ensure_save_and_restore_write_same_episode_id_node():
    # Scenario (b) companion: the entity-path writes the extraction prose and
    # final JSON restore to the same episode_id node with the normalized brain_
    # group_id, then add_episode succeeds. No NodeNotFoundError, no duplicate.
    graphiti = _FakeGraphiti()

    async def _not_extracted(driver, episode_id):
        _ = (driver, episode_id)
        return False

    adapter = GraphitiNeo4jGraphMemoryAdapter(
        graphiti,
        default_group_id="/project/neurons",
        extract_entities=True,
        entity_extracted=_not_extracted,
    )
    episode = _episode("Task", "task:exists", {"brain_id": "/project/neurons", "task": "exists"})

    result = adapter.upsert_episode(episode)

    assert result == "inserted"
    assert graphiti.saved_uuids == [episode.episode_id, episode.episode_id]
    entity_group_id = graphiti.added[0]["group_id"]
    assert entity_group_id.startswith("brain_")
    assert "/" not in entity_group_id


def test_entity_path_short_circuits_to_duplicate_without_add_episode():
    # Scenario (c): MENTIONS>0 (entity pass already ran). The entity-extracted
    # guard short-circuits to 'duplicate' BEFORE any ensure-save or add_episode,
    # so the LLM is not re-billed and no extra node is written.
    graphiti = _FakeGraphiti()

    async def _already_extracted(driver, episode_id):
        _ = (driver, episode_id)
        return True

    adapter = GraphitiNeo4jGraphMemoryAdapter(
        graphiti,
        default_group_id="/project/neurons",
        extract_entities=True,
        entity_extracted=_already_extracted,
    )
    episode = _episode("Task", "task:dup-entity", {"brain_id": "/project/neurons", "task": "dup"})

    result = adapter.upsert_episode(episode)

    assert result == "duplicate"
    # No ensure-save and no add_episode call: the duplicate short-circuits first.
    assert graphiti.saved_uuids == []
    assert graphiti.added == []


def test_entity_path_force_reextract_bypasses_entity_extracted_guard():
    # --reextract-entities must bypass the adapter-level MENTIONS guard as well
    # as the durable projection resume guard, so an already-extracted episode can
    # be measured/rebuilt intentionally during bounded live validation.
    graphiti = _FakeGraphiti()

    async def _already_extracted(driver, episode_id):
        _ = (driver, episode_id)
        return True

    adapter = GraphitiNeo4jGraphMemoryAdapter(
        graphiti,
        default_group_id="/project/neurons",
        extract_entities=True,
        force_reextract_entities=True,
        entity_extracted=_already_extracted,
    )
    episode = _episode("Task", "task:force-entity", {"brain_id": "/project/neurons", "task": "force"})

    result = adapter.upsert_episode(episode)

    assert result == "inserted"
    assert graphiti.saved_uuids == [episode.episode_id, episode.episode_id]
    assert graphiti.added[0]["uuid"] == episode.episode_id


def test_entity_path_hard_fails_on_private_extracted_text():
    # Scenario (d): the LLM entity extractor synthesizes private/secret text.
    # _reject_unsafe_extraction must HARD FAIL (ValueError) so it never persists,
    # and the message names only the field kind (no raw private value echoed).
    graphiti = _FakeGraphiti()

    async def _not_extracted(driver, episode_id):
        _ = (driver, episode_id)
        return False

    async def _add_episode_with_private(**kwargs):
        # Ensure-save ran first, so the node exists; simulate add_episode
        # succeeding but returning a node whose summary leaks a private path.
        return SimpleNamespace(
            nodes=[SimpleNamespace(name="Entity", summary="see /Users/secret/notes")],
            edges=[],
        )

    graphiti.add_episode = _add_episode_with_private  # type: ignore[method-assign]
    adapter = GraphitiNeo4jGraphMemoryAdapter(
        graphiti,
        default_group_id="/project/neurons",
        extract_entities=True,
        entity_extracted=_not_extracted,
    )
    episode = _episode("Task", "task:leak", {"brain_id": "/project/neurons", "task": "leak"})

    with pytest.raises(ValueError) as excinfo:
        adapter.upsert_episode(episode)

    message = str(excinfo.value)
    assert "EntityNode.summary" in message
    # The raw private path must never be echoed in the exception text.
    assert "/Users/secret/notes" not in message


def test_episodic_default_path_still_saves_single_node_after_helper_extraction():
    # Scenario (e) sibling: the episodic-only path now builds its node through the
    # shared _build_episodic_node helper. Behavior must be preserved: a single
    # save keyed on episode_id, no add_episode call, no NodeNotFoundError.
    graphiti = _FakeGraphiti()
    adapter = GraphitiNeo4jGraphMemoryAdapter(graphiti, default_group_id="/project/neurons")
    episode = _episode("Task", "task:episodic-helper", {"brain_id": "/project/neurons", "task": "episodic"})

    result = adapter.upsert_episode(episode)

    assert result == "inserted"
    assert graphiti.saved_uuids == [episode.episode_id]
    assert graphiti.added == []


def test_graphiti_adapter_search_rehydrates_domain_episode_and_graph_fact():
    graphiti = _FakeGraphiti()
    task = _episode(
        "Task",
        "task:graphiti",
        {"brain_id": "/project/neurons", "provider": "codex", "task": "Graphiti adapter"},
    )
    graphiti.episodes.append(
        SimpleNamespace(
            content=json.dumps(task.to_dict(), ensure_ascii=True, sort_keys=True),
            valid_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
        )
    )
    graphiti.edges.append(
        SimpleNamespace(
            uuid="edge-1",
            name="RELATES_TO",
            fact="Graphiti adapter stores ontology episodes.",
            valid_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
            invalid_at=None,
            source_node_uuid="node-a",
            target_node_uuid="node-b",
            episodes=[task.episode_id],
        )
    )
    adapter = GraphitiNeo4jGraphMemoryAdapter(graphiti, default_group_id="/project/neurons")

    typed = adapter.search_context(
        brain_id="/project/neurons",
        query="Graphiti adapter",
        entity_types=["Task"],
        limit=5,
    )
    all_results = adapter.search_context(
        brain_id="/project/neurons",
        query="Graphiti adapter",
        entity_types=None,
        limit=5,
    )

    assert typed.status == "available"
    assert [episode.entity_type for episode in typed.episodes] == ["Task"]
    assert {episode.entity_type for episode in all_results.episodes} == {"Task", "GraphFact"}
    assert graphiti.search_calls[0]["group_ids"][0].startswith("brain_")
    assert "/" not in graphiti.search_calls[0]["group_ids"][0]


def test_graphiti_adapter_fails_closed_for_unresolved_or_canary_edge_provenance():
    """Entity edges must inherit trusted source provider provenance before recall."""
    graphiti = _FakeGraphiti()
    normal = _episode(
        "Task",
        "task:normal-edge-source",
        {"brain_id": "/project/neurons", "provider": "codex", "task": "Normal graph source"},
    )
    canary = _episode(
        "Task",
        "task:canary-edge-source",
        {
            "brain_id": "/project/neurons",
            "provider": "lbrain-temporal-canary",
            "task": "Synthetic graph source",
        },
    )
    alternate_provider = _episode(
        "Task",
        "task:alternate-provider-edge-source",
        {"brain_id": "/project/neurons", "provider": "claude", "task": "Alternate graph source"},
    )
    other_scope = _episode(
        "Task",
        "task:other-scope-edge-source",
        {"brain_id": "/project/other", "provider": "codex", "task": "Other graph source"},
    )
    providerless = _episode(
        "Task",
        "task:providerless-edge-source",
        {"brain_id": "/project/neurons", "task": "Unbound graph source"},
    )
    graphiti.episodes.extend(
        [
            SimpleNamespace(content=json.dumps(normal.to_dict(), ensure_ascii=True, sort_keys=True)),
            SimpleNamespace(content=json.dumps(canary.to_dict(), ensure_ascii=True, sort_keys=True)),
            SimpleNamespace(content=json.dumps(alternate_provider.to_dict(), ensure_ascii=True, sort_keys=True)),
            SimpleNamespace(content=json.dumps(other_scope.to_dict(), ensure_ascii=True, sort_keys=True)),
            SimpleNamespace(content=json.dumps(providerless.to_dict(), ensure_ascii=True, sort_keys=True)),
        ]
    )
    graphiti.edges.extend(
        [
            SimpleNamespace(
                uuid="edge-normal-provenance",
                name="RELATES_TO",
                fact="Normal edge provenance is retained.",
                valid_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
                invalid_at=None,
                source_node_uuid="node-normal-a",
                target_node_uuid="node-normal-b",
                episodes=[normal.episode_id],
            ),
            SimpleNamespace(
                uuid="edge-canary-provenance",
                name="RELATES_TO",
                fact="Synthetic edge provenance must not reach recall.",
                valid_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
                invalid_at=None,
                source_node_uuid="node-canary-a",
                target_node_uuid="node-canary-b",
                episodes=[canary.episode_id],
            ),
            SimpleNamespace(
                uuid="edge-mixed-provenance",
                name="RELATES_TO",
                fact="Mixed edge provenance must not reach recall.",
                valid_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
                invalid_at=None,
                source_node_uuid="node-mixed-a",
                target_node_uuid="node-mixed-b",
                episodes=[normal.episode_id, canary.episode_id],
            ),
            SimpleNamespace(
                uuid="edge-mixed-normal-provider-provenance",
                name="RELATES_TO",
                fact="Mixed normal providers must not reach recall.",
                valid_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
                invalid_at=None,
                source_node_uuid="node-mixed-normal-a",
                target_node_uuid="node-mixed-normal-b",
                episodes=[normal.episode_id, alternate_provider.episode_id],
            ),
            SimpleNamespace(
                uuid="edge-cross-scope-provenance",
                name="RELATES_TO",
                fact="Cross-scope edge provenance must not reach recall.",
                valid_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
                invalid_at=None,
                source_node_uuid="node-other-a",
                target_node_uuid="node-other-b",
                episodes=[other_scope.episode_id],
            ),
            SimpleNamespace(
                uuid="edge-providerless-provenance",
                name="RELATES_TO",
                fact="Providerless edge provenance must not reach recall.",
                valid_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
                invalid_at=None,
                source_node_uuid="node-providerless-a",
                target_node_uuid="node-providerless-b",
                episodes=[providerless.episode_id],
            ),
            SimpleNamespace(
                uuid="edge-unresolved-provenance",
                name="RELATES_TO",
                fact="Unresolved edge provenance must not reach recall.",
                valid_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
                invalid_at=None,
                source_node_uuid="node-missing-a",
                target_node_uuid="node-missing-b",
                episodes=["missing-source-episode"],
            ),
            SimpleNamespace(
                uuid="edge-empty-provenance",
                name="RELATES_TO",
                fact="Empty edge provenance must not reach recall.",
                valid_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
                invalid_at=None,
                source_node_uuid="node-empty-a",
                target_node_uuid="node-empty-b",
                episodes=[],
            ),
        ]
    )
    adapter = GraphitiNeo4jGraphMemoryAdapter(graphiti, default_group_id="/project/neurons")

    result = adapter.search_context(
        brain_id="/project/neurons",
        query="edge provenance",
        entity_types=None,
        limit=10,
    )

    graph_facts = [episode for episode in result.episodes if episode.entity_type == "GraphFact"]
    assert len(graph_facts) == 1
    assert graph_facts[0].payload["provider"] == "codex"
    assert graph_facts[0].payload["source_providers"] == ["codex"]
    assert result.status == "degraded"
    assert "edge_provenance_unresolved" in result.details


def test_graphiti_adapter_resolves_edge_provenance_within_single_read_deadline():
    """A missing provenance lookup must not start a second full read timeout."""

    class _RecordingRunner:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def run(self, coroutine_factory, *, timeout):  # noqa: ANN001
            self.timeouts.append(float(timeout))
            return asyncio.run(coroutine_factory())

    graphiti = _FakeGraphiti()
    graphiti.edges.append(
        SimpleNamespace(
            uuid="edge-missing-deadline",
            name="RELATES_TO",
            fact="Missing provenance must not extend the read deadline.",
            valid_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
            invalid_at=None,
            source_node_uuid="node-missing-a",
            target_node_uuid="node-missing-b",
            episodes=["missing-source-episode"],
        )
    )
    runner = _RecordingRunner()
    adapter = GraphitiNeo4jGraphMemoryAdapter(
        graphiti,
        default_group_id="/project/neurons",
        read_timeout_seconds=0.5,
        runner=runner,
    )

    result = adapter.search_context(
        brain_id="/project/neurons",
        query="missing provenance",
        entity_types=None,
        limit=10,
    )

    assert result.status == "degraded"
    assert not [episode for episode in result.episodes if episode.entity_type == "GraphFact"]
    assert runner.timeouts == [0.5]


def test_graphiti_adapter_hydrates_recent_window_miss_and_rejects_scope_mismatch(monkeypatch):
    """Exact hydration keeps normal edges while dropping sources outside the graph scope."""
    from graphiti_core.nodes import EpisodicNode

    graphiti = _FakeGraphiti()
    normal = _episode(
        "Task",
        "task:hydrated-edge-source",
        {"brain_id": "/project/neurons", "provider": "codex", "task": "Hydrated graph source"},
    )
    other_scope = _episode(
        "Task",
        "task:hydrated-other-scope-source",
        {"brain_id": "/project/other", "provider": "codex", "task": "Other graph source"},
    )
    legacy_other_scope = _episode(
        "Task",
        "task:hydrated-legacy-other-scope-source",
        {"provider": "codex", "task": "Legacy other graph source"},
    )
    expected_group_id = _graphiti_group_id("/project/neurons")
    nodes = {
        normal.episode_id: SimpleNamespace(
            content=json.dumps(normal.to_dict(), ensure_ascii=True, sort_keys=True),
            group_id=expected_group_id,
        ),
        other_scope.episode_id: SimpleNamespace(
            content=json.dumps(other_scope.to_dict(), ensure_ascii=True, sort_keys=True),
            group_id=_graphiti_group_id("/project/other"),
        ),
        legacy_other_scope.episode_id: SimpleNamespace(
            content=json.dumps(legacy_other_scope.to_dict(), ensure_ascii=True, sort_keys=True),
            group_id=_graphiti_group_id("/project/other"),
        ),
    }

    async def _get_by_uuid(_driver, episode_id):
        return nodes[episode_id]

    monkeypatch.setattr(EpisodicNode, "get_by_uuid", staticmethod(_get_by_uuid))
    graphiti.edges.extend(
        [
            SimpleNamespace(
                uuid="edge-hydrated-normal",
                name="RELATES_TO",
                fact="Hydrated normal provenance is retained.",
                valid_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
                invalid_at=None,
                source_node_uuid="node-normal-a",
                target_node_uuid="node-normal-b",
                episodes=[normal.episode_id],
            ),
            SimpleNamespace(
                uuid="edge-hydrated-other-scope",
                name="RELATES_TO",
                fact="Hydrated cross-scope provenance is rejected.",
                valid_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
                invalid_at=None,
                source_node_uuid="node-other-a",
                target_node_uuid="node-other-b",
                episodes=[other_scope.episode_id],
            ),
            SimpleNamespace(
                uuid="edge-hydrated-legacy-other-scope",
                name="RELATES_TO",
                fact="Hydrated legacy cross-scope provenance is rejected.",
                valid_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
                invalid_at=None,
                source_node_uuid="node-legacy-other-a",
                target_node_uuid="node-legacy-other-b",
                episodes=[legacy_other_scope.episode_id],
            ),
        ]
    )
    adapter = GraphitiNeo4jGraphMemoryAdapter(graphiti, default_group_id="/project/neurons")

    result = adapter.search_context(
        brain_id="/project/neurons",
        query="hydrated provenance",
        entity_types=None,
        limit=10,
    )

    graph_facts = [episode for episode in result.episodes if episode.entity_type == "GraphFact"]
    assert [episode.payload["source_providers"] for episode in graph_facts] == [["codex"]]
    assert result.status == "degraded"
    assert "edge_provenance_unresolved" in result.details


def test_graphiti_adapter_hydrates_missing_edge_sources_concurrently_within_deadline(monkeypatch):
    """Independent exact sources must share one bounded read window fairly."""
    from graphiti_core.nodes import EpisodicNode

    graphiti = _FakeGraphiti()
    source_count = _MAX_EDGE_PROVENANCE_LOOKUPS_IN_FLIGHT + 1
    expected_group_id = _graphiti_group_id("/project/neurons")
    sources = [
        _episode(
            "Task",
            f"task:concurrent-edge-source-{index}",
            {
                "brain_id": "/project/neurons",
                "provider": "codex",
                "task": f"Concurrent source {index}",
            },
        )
        for index in range(source_count)
    ]
    nodes = {
        source.episode_id: SimpleNamespace(
            content=json.dumps(source.to_dict(), ensure_ascii=True, sort_keys=True),
            group_id=expected_group_id,
        )
        for source in sources
    }
    active = 0
    peak_active = 0
    capacity_started = asyncio.Event()

    async def _get_by_uuid(_driver, episode_id):
        nonlocal active, peak_active
        active += 1
        peak_active = max(peak_active, active)
        if active == _MAX_EDGE_PROVENANCE_LOOKUPS_IN_FLIGHT:
            capacity_started.set()
        try:
            await capacity_started.wait()
            await asyncio.sleep(0)
            return nodes[episode_id]
        finally:
            active -= 1

    monkeypatch.setattr(EpisodicNode, "get_by_uuid", staticmethod(_get_by_uuid))
    graphiti.edges.extend(
        [
            SimpleNamespace(
                uuid=f"edge-concurrent-{index}",
                name="RELATES_TO",
                fact=f"Concurrent provenance {index} is retained.",
                valid_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
                invalid_at=None,
                source_node_uuid=f"node-concurrent-{index}-a",
                target_node_uuid=f"node-concurrent-{index}-b",
                episodes=[source.episode_id],
            )
            for index, source in enumerate(sources)
        ]
    )
    runner = _AsyncLoopRunner(default_timeout=1)
    adapter = GraphitiNeo4jGraphMemoryAdapter(
        graphiti,
        default_group_id="/project/neurons",
        read_timeout_seconds=0.2,
        runner=runner,
    )
    try:
        result = adapter.search_context(
            brain_id="/project/neurons",
            query="concurrent provenance",
            entity_types=None,
            limit=10,
        )
    finally:
        runner.shutdown()

    graph_facts = [episode for episode in result.episodes if episode.entity_type == "GraphFact"]
    assert result.status == "available"
    assert len(graph_facts) == source_count
    assert peak_active == _MAX_EDGE_PROVENANCE_LOOKUPS_IN_FLIGHT


def test_graphiti_adapter_bounds_slow_provenance_hydration_to_read_deadline(monkeypatch):
    """A slow exact lookup degrades without blocking independent normal evidence."""
    from graphiti_core.nodes import EpisodicNode

    graphiti = _FakeGraphiti()
    normal = _episode(
        "Task",
        "task:fast-source-during-slow-hydration",
        {"brain_id": "/project/neurons", "provider": "codex", "task": "Fast graph source"},
    )
    expected_group_id = _graphiti_group_id("/project/neurons")
    graphiti.edges.extend(
        [
            SimpleNamespace(
                uuid="edge-slow-provenance",
                name="RELATES_TO",
                fact="Slow provenance must not extend the graph read.",
                valid_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
                invalid_at=None,
                source_node_uuid="node-slow-a",
                target_node_uuid="node-slow-b",
                episodes=["slow-source-episode"],
            ),
            SimpleNamespace(
                uuid="edge-fast-provenance",
                name="RELATES_TO",
                fact="Fast provenance remains available.",
                valid_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
                invalid_at=None,
                source_node_uuid="node-fast-a",
                target_node_uuid="node-fast-b",
                episodes=[normal.episode_id],
            ),
        ]
    )

    async def _slow_search(query, *, group_ids=None, num_results=10):
        _ = (query, group_ids, num_results)
        await asyncio.sleep(0.12)
        return list(graphiti.edges)

    async def _slow_get_by_uuid(_driver, episode_id):
        if episode_id == normal.episode_id:
            return SimpleNamespace(
                content=json.dumps(normal.to_dict(), ensure_ascii=True, sort_keys=True),
                group_id=expected_group_id,
            )
        await asyncio.sleep(1)
        raise AssertionError("read timeout must cancel slow provenance lookup")

    graphiti.search = _slow_search  # type: ignore[method-assign]
    monkeypatch.setattr(EpisodicNode, "get_by_uuid", staticmethod(_slow_get_by_uuid))
    runner = _AsyncLoopRunner(default_timeout=1)
    adapter = GraphitiNeo4jGraphMemoryAdapter(
        graphiti,
        default_group_id="/project/neurons",
        read_timeout_seconds=0.2,
        runner=runner,
    )
    started = time.monotonic()
    try:
        result = adapter.search_context(
            brain_id="/project/neurons",
            query="slow provenance",
            entity_types=None,
            limit=10,
        )
    finally:
        runner.shutdown()
    elapsed = time.monotonic() - started

    assert elapsed < 0.28
    assert result.status == "degraded"
    graph_facts = [episode for episode in result.episodes if episode.entity_type == "GraphFact"]
    assert [episode.payload["provider"] for episode in graph_facts] == ["codex"]


def test_graphiti_adapter_keeps_episode_retrieval_when_edge_index_is_missing():
    graphiti = _FakeGraphiti()
    graphiti.raise_on_search = RuntimeError("missing edge index")
    task = _episode("Task", "task:episode-only", {"brain_id": "/project/neurons", "task": "Episode-only search"})
    graphiti.episodes.append(
        SimpleNamespace(
            content=json.dumps(task.to_dict(), ensure_ascii=True, sort_keys=True),
            valid_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
        )
    )
    adapter = GraphitiNeo4jGraphMemoryAdapter(graphiti, default_group_id="/project/neurons")

    result = adapter.search_context(
        brain_id="/project/neurons",
        query="Episode-only search",
        entity_types=["Task"],
        limit=5,
    )

    # Edge index missing but episodes survive: this is a degraded read, not a
    # healthy 'available' one, and it carries an explicit degrade signal.
    assert result.status == "degraded"
    assert [episode.natural_id for episode in result.episodes] == ["task:episode-only"]
    assert result.details == ("graphiti_neo4j", "edge_search:RuntimeError", "graph_edge_degraded")


def test_graphiti_config_from_env_supports_ollama_openai_compatible_defaults():
    config = GraphitiNeo4jConfig.from_env(
        {
            "LLM_BRAIN_NEO4J_URI": "bolt://neo4j:7687",
            "LLM_BRAIN_NEO4J_USER": "neo4j",
            "LLM_BRAIN_NEO4J_PASSWORD": "secret",
            "LLM_BRAIN_GRAPH_LLM_PROVIDER": "ollama",
            "LLM_BRAIN_LLM_MODEL": "llama3.1:70b",
            "LLM_BRAIN_EMBEDDING_MODEL": "nomic-embed-text",
            "LLM_BRAIN_EMBEDDING_DIM": "768",
            "LLM_BRAIN_GRAPH_EXTRACT_ENTITIES": "true",
            "LLM_BRAIN_GRAPH_FORCE_REEXTRACT_ENTITIES": "true",
        }
    )

    assert config.uri == "bolt://neo4j:7687"
    assert config.llm_provider == "ollama"
    assert config.llm_model == "llama3.1:70b"
    assert config.embedding_model == "nomic-embed-text"
    assert config.embedding_dim == 768
    assert config.extract_entities is True
    assert config.force_reextract_entities is True


def test_graphiti_config_reads_llm_reasoning_effort_from_env():
    config = GraphitiNeo4jConfig.from_env(
        {
            "LLM_BRAIN_GRAPH_LLM_PROVIDER": "openai-compatible",
            "LLM_BRAIN_LLM_MODEL": "ollama:qwen3.5:cloud",
            "LLM_BRAIN_LLM_REASONING_EFFORT": "medium",
        }
    )

    assert config.llm_reasoning_effort == "medium"


def test_graphiti_config_rejects_invalid_llm_reasoning_effort():
    with pytest.raises(ValueError, match="LLM_BRAIN_LLM_REASONING_EFFORT"):
        GraphitiNeo4jConfig.from_env({"LLM_BRAIN_LLM_REASONING_EFFORT": "extreme"})


def test_graphiti_config_rejects_gemini_llm_models_for_cost_guard():
    with pytest.raises(ValueError, match="Gemini LLM models are forbidden"):
        GraphitiNeo4jConfig.from_env({"LLM_BRAIN_LLM_MODEL": "gemini-3.5-flash-thinking"})


def test_graphiti_config_rejects_legacy_gemini_llm_models_for_cost_guard():
    with pytest.raises(ValueError, match="Gemini LLM models are forbidden"):
        GraphitiNeo4jConfig.from_env({"MODEL_NAME": "Gemini-2.5-Pro"})


def test_graphiti_config_rejects_gemini_small_llm_models_for_cost_guard():
    with pytest.raises(ValueError, match="Gemini LLM models are forbidden"):
        GraphitiNeo4jConfig.from_env({"LLM_BRAIN_SMALL_LLM_MODEL": "gemini-2.5-flash"})


def test_graphiti_config_rejects_gemini_fallback_llm_models_for_cost_guard():
    with pytest.raises(ValueError, match="Gemini LLM models are forbidden"):
        GraphitiNeo4jConfig.from_env({"LLM_BRAIN_LLM_FALLBACK_MODEL": "gemini-2.5-flash"})


def test_graphiti_config_allows_gemma4_maas_and_gemini_embedding():
    config = GraphitiNeo4jConfig.from_env(
        {
            "LLM_BRAIN_LLM_MODEL": "gemma-4-26b-a4b-it-maas",
            "LLM_BRAIN_SMALL_LLM_MODEL": "gemma-4-26b-a4b-it-maas",
            "LLM_BRAIN_EMBEDDING_MODEL": "gemini-embedding-2",
        }
    )

    assert config.llm_model == "gemma-4-26b-a4b-it-maas"
    assert config.small_model == "gemma-4-26b-a4b-it-maas"
    assert config.embedding_model == "gemini-embedding-2"


def test_graphiti_config_from_env_supports_llm_fallback_policy():
    config = GraphitiNeo4jConfig.from_env(
        {
            "LLM_BRAIN_GRAPH_EXTRACT_ENTITIES": "true",
            "LLM_BRAIN_LLM_MODEL": "ollama:qwen3.5:cloud",
            "LLM_BRAIN_SMALL_LLM_MODEL": "ollama:qwen3.5:cloud",
            "LLM_BRAIN_LLM_FALLBACK_MODEL": "ollama:gemma4:31b-cloud",
            "LLM_BRAIN_SMALL_LLM_FALLBACK_MODEL": "ollama:gemma4:31b-cloud",
            "LLM_BRAIN_GRAPH_PRIMARY_ATTEMPTS": "3",
            "LLM_BRAIN_GRAPH_FALLBACK_ATTEMPTS": "2",
            "LLM_BRAIN_GRAPH_PRIMARY_ATTEMPT_TIMEOUT_SECONDS": "12.5",
            "LLM_BRAIN_GRAPH_FALLBACK_ATTEMPT_TIMEOUT_SECONDS": "45",
        }
    )

    assert config.llm_model == "ollama:qwen3.5:cloud"
    assert config.fallback_llm_model == "ollama:gemma4:31b-cloud"
    assert config.fallback_small_model == "ollama:gemma4:31b-cloud"
    assert config.primary_attempts == 3
    assert config.fallback_attempts == 2
    assert config.primary_attempt_timeout_seconds == 12.5
    assert config.fallback_attempt_timeout_seconds == 45.0


@pytest.mark.anyio
async def test_reasoning_openai_generic_client_passes_reasoning_effort_to_chat_completion():
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.prompts.models import Message

    captured: dict[str, object] = {}

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"ok": true}'),
                    )
                ]
            )

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=_FakeCompletions(),
        )
    )
    client = _ReasoningOpenAIGenericClient(
        config=LLMConfig(api_key="unit-test", model="ollama:qwen3.5:cloud"),
        client=fake_client,
        reasoning_effort="medium",
    )

    result = await client.generate_response(
        [
            Message(role="system", content="Return JSON."),
            Message(role="user", content="Say ok."),
        ]
    )

    assert result == {"ok": True}
    assert captured["model"] == "ollama:qwen3.5:cloud"
    assert captured["reasoning_effort"] == "medium"


@pytest.mark.anyio
async def test_reasoning_openai_generic_client_omits_reasoning_effort_when_unset():
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.prompts.models import Message

    captured: dict[str, object] = {}

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"ok": true}'),
                    )
                ]
            )

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))
    client = _ReasoningOpenAIGenericClient(
        config=LLMConfig(api_key="unit-test", model="ollama:qwen3.5:cloud"),
        client=fake_client,
    )

    result = await client.generate_response([Message(role="user", content="Say ok.")])

    assert result == {"ok": True}
    assert "reasoning_effort" not in captured


@pytest.mark.anyio
async def test_reasoning_openai_generic_client_maps_assistant_and_unknown_roles():
    # #4: standard roles beyond user/system (assistant) must survive instead of
    # being silently dropped, and an unrecognized role degrades to 'user' so its
    # content is never lost.
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.prompts.models import Message

    captured: dict[str, object] = {}

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))]
            )

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))
    client = _ReasoningOpenAIGenericClient(
        config=LLMConfig(api_key="unit-test", model="ollama:qwen3.5:cloud"),
        client=fake_client,
    )

    await client.generate_response(
        [
            Message(role="system", content="sys"),
            Message(role="assistant", content="prior answer"),
            Message(role="user", content="now"),
            Message(role="reviewer", content="unknown role payload"),
        ]
    )

    roles = [message["role"] for message in captured["messages"]]
    assert roles == ["system", "assistant", "user", "user"]
    # No message was dropped: every input turn produced a request message.
    assert len(captured["messages"]) == 4


def test_graphiti_config_defaults_to_episode_only_storage():
    config = GraphitiNeo4jConfig.from_env({})

    assert config.extract_entities is False
    assert config.force_reextract_entities is False


def test_structured_response_normalizes_entity_name_alias():
    from graphiti_core.prompts.extract_nodes import ExtractedEntities

    normalized = _normalize_structured_response(
        {
            "extracted_entities": [
                {"entity_name": "Project Atlas", "entity_type_id": 0, "episode_indices": [0]}
            ]
        },
        ExtractedEntities,
    )

    assert normalized == {
        "extracted_entities": [
            {"name": "Project Atlas", "entity_type_id": 0, "episode_indices": [0]}
        ]
    }


def test_structured_response_wraps_single_list_for_graphiti_model():
    from graphiti_core.prompts.extract_nodes import ExtractedEntities

    normalized = _normalize_structured_response(
        [{"entity_text": "Neo4j", "episode_indices": [0]}],
        ExtractedEntities,
    )

    assert normalized == {
        "extracted_entities": [{"name": "Neo4j", "entity_type_id": 0, "episode_indices": [0]}]
    }


def test_structured_response_normalizes_episode_indices():
    from graphiti_core.prompts.extract_edges import ExtractedEdges

    normalized = _normalize_structured_response(
        {
            "edges": [
                {
                    "source_entity_name": "Project Atlas",
                    "target_entity_name": "Neo4j",
                    "relation_type": "DEPENDS_ON",
                    "episode_indices": ["episode:abc", "1"],
                }
            ]
        },
        ExtractedEdges,
    )

    assert normalized["edges"][0]["episode_indices"] == [0, 1]


def test_structured_response_drops_invalid_duplicate_fact_idxs_only():
    from graphiti_core.prompts.dedupe_edges import EdgeDuplicate

    normalized = _normalize_structured_response(
        {
            "duplicate_facts": [0, "2", 6, -1, "bad"],
            "contradicted_facts": [0, 2, 6],
        },
        EdgeDuplicate,
        valid_duplicate_fact_idxs={0, 2},
    )

    assert normalized["duplicate_facts"] == [0, 2]
    assert normalized["contradicted_facts"] == [0, 2, 6]


def test_existing_fact_idx_values_parse_only_existing_facts_block():
    from graphiti_core.prompts.models import Message

    messages = [
        Message(
            role="user",
            content="""
<EXISTING FACTS>
[{'idx': 0, 'fact': 'kept'}, {'idx': 2, 'fact': 'kept'}]
</EXISTING FACTS>
<FACT INVALIDATION CANDIDATES>
[{'idx': 6, 'fact': 'not a duplicate candidate'}]
</FACT INVALIDATION CANDIDATES>
""",
        )
    ]

    assert _existing_fact_idx_values_from_messages(messages) == {0, 2}


def test_graphiti_adapter_retries_primary_then_fallback_for_entity_extraction():
    primary = _FailingGraphiti(RuntimeError("primary failed"))
    fallback = _FakeGraphiti()
    fallback.driver = primary.driver
    adapter = GraphitiNeo4jGraphMemoryAdapter(
        primary,
        fallback_graphiti=fallback,
        extract_entities=True,
        primary_attempts=3,
        fallback_attempts=1,
    )
    episode = _episode("Task", "task:fallback", {"brain_id": "/project/neurons", "task": "fallback"})

    result = adapter.upsert_episode(episode)

    assert result == "inserted"
    assert len(primary.added) == 3
    assert len(fallback.added) == 1
    assert fallback.added[0]["name"] == episode.episode_id


def test_graphiti_adapter_bounds_primary_attempt_timeout_before_fallback():
    primary = _SlowAddGraphiti(delay_seconds=1.0)
    fallback = _FakeGraphiti()
    fallback.driver = primary.driver
    adapter = GraphitiNeo4jGraphMemoryAdapter(
        primary,
        fallback_graphiti=fallback,
        extract_entities=True,
        primary_attempts=2,
        fallback_attempts=1,
        primary_attempt_timeout_seconds=0.01,
    )
    episode = _episode("Task", "task:timeout-fallback", {"brain_id": "/project/neurons", "task": "fallback"})

    result = adapter.upsert_episode(episode)

    assert result == "inserted"
    assert len(primary.added) == 2
    assert len(fallback.added) == 1


def test_graphiti_adapter_raises_after_primary_attempts_without_fallback():
    primary = _FailingGraphiti(RuntimeError("primary failed"))
    adapter = GraphitiNeo4jGraphMemoryAdapter(
        primary,
        extract_entities=True,
        primary_attempts=2,
    )
    episode = _episode("Task", "task:no-fallback", {"brain_id": "/project/neurons", "task": "no fallback"})

    with pytest.raises(RuntimeError, match="primary failed"):
        adapter.upsert_episode(episode)

    assert len(primary.added) == 2


def test_graphiti_adapters_share_single_async_loop_runner():
    first = GraphitiNeo4jGraphMemoryAdapter(_FakeGraphiti())
    second = GraphitiNeo4jGraphMemoryAdapter(_FakeGraphiti())

    assert first._runner is second._runner


def test_async_loop_runner_times_out_and_cancels_pending_call():
    # Deterministic timeout path: a coroutine that never finishes within the
    # tiny bound must raise a TimeoutError and have its future cancelled, so a
    # hung backend call does not block the shared loop forever.
    runner = _AsyncLoopRunner(default_timeout=5.0)
    started = asyncio.Event()

    async def _never_returns():
        started.set()
        await asyncio.sleep(60)

    try:
        with pytest.raises(FuturesTimeoutError):
            runner.run(_never_returns, timeout=0.05)
    finally:
        runner.shutdown()


def test_async_loop_runner_uses_default_timeout_when_unspecified():
    runner = _AsyncLoopRunner(default_timeout=0.05)

    async def _slow():
        await asyncio.sleep(60)

    try:
        with pytest.raises(FuturesTimeoutError):
            runner.run(_slow)
    finally:
        runner.shutdown()


def test_async_loop_runner_shutdown_is_idempotent():
    runner = _AsyncLoopRunner(default_timeout=1.0)
    runner.shutdown()
    # A second shutdown must not raise even though the loop is already closed.
    runner.shutdown()


def test_search_context_read_timeout_degrades_to_error_status():
    # A read that exceeds the read timeout must surface as status='error', never
    # as a false 'available' empty read.
    runner = _AsyncLoopRunner(default_timeout=5.0)
    graphiti = _SlowGraphiti(delay=60)
    adapter = GraphitiNeo4jGraphMemoryAdapter(
        graphiti,
        default_group_id="/project/neurons",
        read_timeout_seconds=0.05,
        runner=runner,
    )
    try:
        result = adapter.search_context(brain_id="/project/neurons", query="slow", limit=5)
    finally:
        runner.shutdown()

    assert result.status == "error"
    assert result.details == ("TimeoutError",)


def test_upsert_episode_write_timeout_propagates_as_timeout_error():
    runner = _AsyncLoopRunner(default_timeout=5.0)
    graphiti = _SlowGraphiti(delay=60)
    adapter = GraphitiNeo4jGraphMemoryAdapter(
        graphiti,
        default_group_id="/project/neurons",
        extract_entities=True,
        write_timeout_seconds=0.05,
        runner=runner,
    )
    episode = _episode("Task", "task:slow-write", {"brain_id": "/project/neurons", "task": "slow"})
    try:
        with pytest.raises(FuturesTimeoutError):
            adapter.upsert_episode(episode)
    finally:
        runner.shutdown()


def test_graphiti_config_reads_split_read_write_timeouts_from_env():
    config = GraphitiNeo4jConfig.from_env(
        {
            "LLM_BRAIN_GRAPH_READ_TIMEOUT_SECONDS": "12.5",
            "LLM_BRAIN_GRAPH_WRITE_TIMEOUT_SECONDS": "600",
        }
    )

    assert config.read_timeout_seconds == 12.5
    assert config.write_timeout_seconds == 600.0


def test_graphiti_config_falls_back_to_default_timeouts_on_bad_env():
    config = GraphitiNeo4jConfig.from_env(
        {
            "LLM_BRAIN_GRAPH_READ_TIMEOUT_SECONDS": "not-a-number",
            "LLM_BRAIN_GRAPH_WRITE_TIMEOUT_SECONDS": "-1",
        }
    )

    assert config.read_timeout_seconds == DEFAULT_GRAPH_READ_TIMEOUT_SECONDS
    assert config.write_timeout_seconds == DEFAULT_GRAPH_WRITE_TIMEOUT_SECONDS


def test_graphiti_episode_rehydration_rejects_missing_required_fields():
    malformed = SimpleNamespace(content=json.dumps({"episode_id": "episode:partial"}))

    assert _episode_node_to_ontology(malformed) is None


def test_graphiti_datetime_conversion_does_not_fabricate_missing_times():
    assert _datetime_to_iso(None) == ""
    assert _datetime_to_iso("not-a-datetime") == ""


def test_resolve_embedding_dim_matches_nomic_native_dim_by_default():
    # #1: nomic-embed-text (the ollama default) is natively 768-dim; the generic
    # 1024 default must not be paired with it (index/query dimension mismatch).
    assert _resolve_embedding_dim("nomic-embed-text", 1024) == 768
    # An explicit non-default dim is always honored.
    assert _resolve_embedding_dim("nomic-embed-text", 384) == 384
    # Unknown / openai default models keep the configured dim untouched.
    assert _resolve_embedding_dim("text-embedding-3-small", 1024) == 1024


def test_is_list_annotation_recognizes_builtin_and_typing_list():
    # #3: both the builtin generic ('list[...]') and 'typing.List[...]' spellings
    # must be recognized as list-typed annotations.
    assert _is_list_annotation("list[str]") is True
    assert _is_list_annotation("typing.List[dict]") is True
    assert _is_list_annotation("int") is False
    assert _is_list_annotation(None) is False


def test_structured_response_wraps_single_typing_list_field():
    # #3: a response model declaring its single list field with typing.List must
    # still get a bare list wrapped under that field name.
    from typing import List

    from pydantic import BaseModel

    class _LegacyListModel(BaseModel):
        items: List[dict]

    normalized = _normalize_structured_response([{"entity_name": "Neo4j"}], _LegacyListModel)

    assert normalized == {"items": [{"name": "Neo4j"}]}


def test_default_openai_provider_uses_configured_client_path():
    # #7/#28: the documented default provider 'openai' must route through the
    # configured OpenAI-compatible client path, so episode-only operation never
    # constructs Graphiti's built-in default OpenAI client (zero-LLM guarantee).
    assert _uses_configured_llm_client("openai") is True
    assert _uses_configured_llm_client("OpenAI-Compatible") is True
    assert _uses_configured_llm_client("ollama") is True
    assert _uses_configured_llm_client("mock") is False


def test_placeholder_api_key_is_non_secret_for_adc_backends():
    # #27: ADC-backed openai-compatible endpoints (vertex-wrapper) authenticate
    # out of band; an empty key falls back to a non-secret placeholder, never a
    # real credential.
    assert _placeholder_api_key("ollama") == "ollama"
    placeholder = _placeholder_api_key("openai-compatible")
    assert placeholder and "sk-" not in placeholder
    assert _placeholder_api_key("openai") == placeholder


def test_entity_path_rolls_back_persisted_extraction_on_unsafe_text():
    # #6/#19: add_episode persists Entity/RELATES_TO BEFORE the redaction gate
    # runs. When the gate rejects unsafe synthesized text, the just-persisted
    # elements must be deleted (edge first, then node) so the private/secret
    # content does not survive in the graph.
    graphiti = _FakeGraphiti()
    deleted: list[str] = []

    async def _delete_node(driver):
        _ = driver
        deleted.append("node")

    async def _delete_edge(driver):
        _ = driver
        deleted.append("edge")

    async def _not_extracted(driver, episode_id):
        _ = (driver, episode_id)
        return False

    async def _add_episode_with_private(**kwargs):
        return SimpleNamespace(
            nodes=[SimpleNamespace(name="Entity", summary="see /Users/secret/notes", delete=_delete_node)],
            edges=[SimpleNamespace(fact="benign", delete=_delete_edge)],
        )

    graphiti.add_episode = _add_episode_with_private  # type: ignore[method-assign]
    adapter = GraphitiNeo4jGraphMemoryAdapter(
        graphiti,
        default_group_id="/project/neurons",
        extract_entities=True,
        entity_extracted=_not_extracted,
    )
    episode = _episode("Task", "task:rollback", {"brain_id": "/project/neurons", "task": "rollback"})

    with pytest.raises(ValueError):
        adapter.upsert_episode(episode)

    assert deleted == ["edge", "node"]


def test_entity_path_skips_retry_after_timeout_when_extraction_already_landed():
    # #20 idempotency: a per-attempt timeout cancels the local await, but the
    # remote add_episode may still complete. Before re-firing for the same
    # episode_id, the adapter re-probes the entity pass; once it has landed, it
    # stops instead of double-extracting.
    graphiti = _SlowAddGraphiti(delay_seconds=1.0)
    probe_calls = {"n": 0}

    async def _landed_after_attempt(driver, episode_id):
        _ = (driver, episode_id)
        probe_calls["n"] += 1
        # False at the pre-extraction guard (no attempt yet); True once the first
        # attempt has run (and timed out), so the retry is skipped.
        return len(graphiti.added) >= 1

    adapter = GraphitiNeo4jGraphMemoryAdapter(
        graphiti,
        default_group_id="/project/neurons",
        extract_entities=True,
        primary_attempts=3,
        primary_attempt_timeout_seconds=0.01,
        entity_extracted=_landed_after_attempt,
    )
    episode = _episode("Task", "task:timeout-idem", {"brain_id": "/project/neurons", "task": "idem"})

    result = adapter.upsert_episode(episode)

    assert result == "inserted"
    # Exactly one add_episode fired: the post-timeout probe stopped the retry.
    assert len(graphiti.added) == 1
    # Probe ran at the pre-extraction guard and again after the timeout.
    assert probe_calls["n"] == 2


def test_fake_adapter_simulates_episode_id_merge_on_reupsert():
    # Production default (extract_entities=False) MERGEs on episode_id: a second
    # upsert of the same episode is a duplicate, not a new row.
    adapter = FakeGraphMemoryAdapter(default_group_id="/project/neurons")
    episode = _episode("Task", "task:merge", {"brain_id": "/project/neurons", "task": "merge"})

    assert adapter.upsert_episode(episode) == "inserted"
    assert adapter.upsert_episode(episode) == "duplicate"

    result = adapter.search_context(brain_id="/project/neurons", query="merge", limit=10)
    assert [ep.natural_id for ep in result.episodes] == ["task:merge"]


def test_fake_adapter_scopes_reads_by_group_id_like_graphiti_group_ids():
    # group_ids filter must be honest: an episode in one brain group is never
    # returned for a different brain_id, even when default_group_id is set.
    adapter = FakeGraphMemoryAdapter(default_group_id="/project/neurons")
    adapter.upsert_episode(_episode("Task", "task:neurons", {"brain_id": "/project/neurons", "task": "shared"}))
    adapter.upsert_episode(_episode("Task", "task:other", {"brain_id": "/project/other", "task": "shared"}))

    neurons = adapter.search_context(brain_id="/project/neurons", query="shared", limit=10)
    other = adapter.search_context(brain_id="/project/other", query="shared", limit=10)
    missing = adapter.search_context(brain_id="/project/absent", query="shared", limit=10)

    assert [ep.natural_id for ep in neurons.episodes] == ["task:neurons"]
    assert [ep.natural_id for ep in other.episodes] == ["task:other"]
    assert missing.episodes == ()


def test_fake_adapter_uses_default_group_id_when_episode_brain_id_missing():
    # An episode without a payload brain_id falls back to default_group_id, the
    # same fallback the real adapter applies for group_id derivation.
    adapter = FakeGraphMemoryAdapter(default_group_id="/project/neurons")
    adapter.upsert_episode(_episode("Task", "task:fallback", {"task": "fallback only"}))

    in_default = adapter.search_context(brain_id="/project/neurons", query="fallback", limit=10)
    in_other = adapter.search_context(brain_id="/project/other", query="fallback", limit=10)

    assert [ep.natural_id for ep in in_default.episodes] == ["task:fallback"]
    assert in_other.episodes == ()


@pytest.mark.skipif(
    not os.environ.get("LLM_BRAIN_NEO4J_URI") and not os.environ.get("NEO4J_URI"),
    reason="requires a live Neo4j (set NEO4J_URI / LLM_BRAIN_NEO4J_URI to run)",
)
def test_graphiti_neo4j_round_trip_against_live_backend():
    """Live Neo4j round-trip: upsert one episode and read it back.

    Skipped unless NEO4J_URI / LLM_BRAIN_NEO4J_URI is set. This is the seam for
    the manual live E2E gate documented in the runbook; CI without a backend
    skips it rather than connecting.
    """

    adapter = GraphitiNeo4jGraphMemoryAdapter.from_env()
    natural_id = f"task:live-roundtrip-{os.getpid()}"
    episode = _episode(
        "Task",
        natural_id,
        {"brain_id": "/project/neurons", "task": "live round-trip smoke"},
    )

    result = adapter.upsert_episode(episode)
    assert result and result != "failed"

    search = adapter.search_context(
        brain_id="/project/neurons",
        query="live round-trip smoke",
        entity_types=["Task"],
        limit=10,
    )
    assert search.status in {"available", "degraded"}


class _FakeDriver:
    """Minimal async driver stand-in for EpisodicNode.save() in tests.

    EpisodicNode.save() issues a single execute_query MERGE; record the saved
    uuid so the default-path upsert test can assert exactly one node was written.
    The set of saved uuids also backs a get_by_uuid simulation so the entity
    path's NodeNotFoundError (node absent) vs. success (node present) can be
    reproduced and verified without a live Neo4j.
    """

    provider = None
    graph_operations_interface = None

    def __init__(self) -> None:
        self.saved_uuids: list[str] = []

    async def execute_query(self, query, **params):
        # Record only WRITE queries (MERGE/save). graphiti reads pass routing_='r'
        # (e.g. EpisodicNode.get_by_uuid); recording those would wrongly count a
        # read probe as a node save. Saves omit routing_, so gate on its absence.
        if "routing_" not in params:
            uuid = params.get("uuid") or params.get("episode_uuid")
            if uuid:
                self.saved_uuids.append(str(uuid))
        return ([], None, None)

    def has_node(self, uuid: str) -> bool:
        return str(uuid) in self.saved_uuids


class _FakeGraphiti:
    def __init__(self) -> None:
        self.added: list[dict] = []
        self.edges: list[SimpleNamespace] = []
        self.episodes: list[SimpleNamespace] = []
        self.search_calls: list[dict] = []
        self.raise_on_search: Exception | None = None
        self.driver = _FakeDriver()

    @property
    def saved_uuids(self) -> list[str]:
        return self.driver.saved_uuids

    async def add_episode(self, **kwargs):
        # Mirror graphiti_core add_episode's "Get or create episode": when a uuid
        # is supplied it does get_by_uuid(uuid) FIRST, which raises
        # NodeNotFoundError if that node was never saved. This reproduces the
        # M2 reextract bug -- the entity pass passes uuid=episode_id, and unless
        # the node exists, add_episode fails before any entity is extracted.
        uuid = kwargs.get("uuid")
        if uuid is not None and not self.driver.has_node(uuid):
            from graphiti_core.errors import NodeNotFoundError

            raise NodeNotFoundError(uuid)
        self.added.append(dict(kwargs, source=kwargs["source"].value))
        self.episodes.append(SimpleNamespace(content=kwargs["episode_body"]))
        return SimpleNamespace(uuid=f"graph:{kwargs['name']}", nodes=[], edges=[])

    async def search(self, query, *, group_ids=None, num_results=10):
        self.search_calls.append({"query": query, "group_ids": group_ids, "num_results": num_results})
        if self.raise_on_search is not None:
            raise self.raise_on_search
        return list(self.edges[:num_results])

    async def retrieve_episodes(self, *, reference_time, last_n=3, group_ids=None):
        _ = reference_time
        _ = group_ids
        return list(self.episodes[-last_n:])


class _FailingGraphiti(_FakeGraphiti):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    async def add_episode(self, **kwargs):
        self.added.append(dict(kwargs, source=kwargs["source"].value))
        raise self._exc


class _SlowAddGraphiti(_FakeGraphiti):
    def __init__(self, *, delay_seconds: float) -> None:
        super().__init__()
        self._delay_seconds = delay_seconds

    async def add_episode(self, **kwargs):
        self.added.append(dict(kwargs, source=kwargs["source"].value))
        await asyncio.sleep(self._delay_seconds)
        return None


class _SlowGraphiti:
    """Graphiti stand-in whose async calls sleep past the configured timeout."""

    def __init__(self, *, delay: float) -> None:
        self._delay = float(delay)
        self.driver = _FakeDriver()

    async def add_episode(self, **kwargs):
        await asyncio.sleep(self._delay)
        return SimpleNamespace(uuid="never")

    async def search(self, query, *, group_ids=None, num_results=10):
        await asyncio.sleep(self._delay)
        return []

    async def retrieve_episodes(self, *, reference_time, last_n=3, group_ids=None):
        await asyncio.sleep(self._delay)
        return []


def _episode(entity_type: str, natural_id: str, payload: dict) -> OntologyEpisode:
    return OntologyEpisode.from_payload(
        event_id=f"evt_{natural_id.replace(':', '_')}",
        entity_type=entity_type,
        natural_id=natural_id,
        payload=payload,
        observed_at="2026-06-19T00:00:00+00:00",
        reference_time="2026-06-19T00:00:00+00:00",
    )


def test_bulk_semantic_entity_uuid_is_name_based_and_type_independent():
    from agent_knowledge.llm_brain_core import bulk_semantic as bs

    same_name_other_type = bs._entity_uuid(
        "g", bs.BulkSemanticEntity(name="Vertex Wrapper", type="Tool", summary="")
    )
    placeholder_type = bs._entity_uuid(
        "g", bs.BulkSemanticEntity(name="vertex   wrapper", type="Concept", summary="")
    )
    # Same normalized name must collapse to one UUID regardless of extracted type,
    # matching the name-based relation endpoint lookup.
    assert same_name_other_type == placeholder_type
    other_name = bs._entity_uuid(
        "g", bs.BulkSemanticEntity(name="Other", type="Tool", summary="")
    )
    assert same_name_other_type != other_name


def test_urllib_post_endpoint_allowlist_blocks_ssrf():
    from agent_knowledge.llm_brain_core import bulk_semantic as bs

    # Loopback / internal endpoints stay valid.
    bs._validate_endpoint_url("http://127.0.0.1:8930/v1/chat/completions")
    bs._validate_endpoint_url("https://vertex-wrapper/v1/embeddings")

    for blocked in (
        "file:///etc/passwd",
        "gopher://internal/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http:///no-host/path",
    ):
        with pytest.raises(ValueError):
            bs._validate_endpoint_url(blocked)

    # An explicit env allowlist further restricts permitted hosts.
    allow = bs._endpoint_allowed_hosts({"LLM_BRAIN_ENDPOINT_ALLOWED_HOSTS": "api.internal"})
    bs._validate_endpoint_url("https://api.internal/v1/chat/completions", allowed_hosts=allow)
    with pytest.raises(ValueError):
        bs._validate_endpoint_url("https://evil.example.com/v1", allowed_hosts=allow)

    # _urllib_post rejects a poisoned base URL before any network call.
    with pytest.raises(ValueError):
        bs._urllib_post(
            "file:///etc/passwd",
            headers={"Authorization": "Bearer secret"},
            body="{}",
            timeout=1,
        )
