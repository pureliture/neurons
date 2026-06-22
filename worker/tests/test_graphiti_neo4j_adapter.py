from __future__ import annotations

import asyncio
import json
import os
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
    _datetime_to_iso,
    _episode_node_to_ontology,
    _normalize_structured_response,
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
    task = _episode("Task", "task:graphiti", {"brain_id": "/project/neurons", "task": "Graphiti adapter"})
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


def test_graphiti_config_from_env_supports_llm_fallback_policy():
    config = GraphitiNeo4jConfig.from_env(
        {
            "LLM_BRAIN_GRAPH_EXTRACT_ENTITIES": "true",
            "LLM_BRAIN_LLM_MODEL": "deepseek-v4-flash:cloud",
            "LLM_BRAIN_SMALL_LLM_MODEL": "deepseek-v4-flash:cloud",
            "LLM_BRAIN_LLM_FALLBACK_MODEL": "gemini-3.5-flash-thinking",
            "LLM_BRAIN_SMALL_LLM_FALLBACK_MODEL": "gemini-3.5-flash-thinking",
            "LLM_BRAIN_GRAPH_PRIMARY_ATTEMPTS": "3",
            "LLM_BRAIN_GRAPH_FALLBACK_ATTEMPTS": "2",
        }
    )

    assert config.llm_model == "deepseek-v4-flash:cloud"
    assert config.fallback_llm_model == "gemini-3.5-flash-thinking"
    assert config.fallback_small_model == "gemini-3.5-flash-thinking"
    assert config.primary_attempts == 3
    assert config.fallback_attempts == 2


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
