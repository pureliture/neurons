from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent_knowledge.llm_brain_core.graph import FakeGraphMemoryAdapter
from agent_knowledge.llm_brain_core.graphiti_adapter import (
    GraphitiNeo4jConfig,
    GraphitiNeo4jGraphMemoryAdapter,
    _datetime_to_iso,
    _episode_node_to_ontology,
)
from agent_knowledge.llm_brain_core.models import OntologyEpisode


def test_graphiti_adapter_upserts_public_safe_json_episode():
    graphiti = _FakeGraphiti()
    adapter = GraphitiNeo4jGraphMemoryAdapter(graphiti, default_group_id="/project/neurons", extract_entities=True)
    episode = _episode("Task", "task:graphiti", {"brain_id": "/project/neurons", "task": "Graphiti adapter"})

    result = adapter.upsert_episode(episode)

    assert result == f"graph:{episode.episode_id}"
    assert graphiti.added[0]["name"] == episode.episode_id
    assert graphiti.added[0]["source"] == "json"
    assert graphiti.added[0]["group_id"].startswith("brain_")
    assert "/" not in graphiti.added[0]["group_id"]
    body = json.loads(graphiti.added[0]["episode_body"])
    assert body["episode_id"] == episode.episode_id
    assert body["entity_type"] == "Task"
    assert body["payload"]["task"] == "Graphiti adapter"
    assert "/Users/" not in graphiti.added[0]["episode_body"]


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
        }
    )

    assert config.uri == "bolt://neo4j:7687"
    assert config.llm_provider == "ollama"
    assert config.llm_model == "llama3.1:70b"
    assert config.embedding_model == "nomic-embed-text"
    assert config.embedding_dim == 768
    assert config.extract_entities is True


def test_graphiti_config_defaults_to_episode_only_storage():
    config = GraphitiNeo4jConfig.from_env({})

    assert config.extract_entities is False


def test_graphiti_adapters_share_single_async_loop_runner():
    first = GraphitiNeo4jGraphMemoryAdapter(_FakeGraphiti())
    second = GraphitiNeo4jGraphMemoryAdapter(_FakeGraphiti())

    assert first._runner is second._runner


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


class _FakeGraphiti:
    def __init__(self) -> None:
        self.added: list[dict] = []
        self.edges: list[SimpleNamespace] = []
        self.episodes: list[SimpleNamespace] = []
        self.search_calls: list[dict] = []
        self.raise_on_search: Exception | None = None

    async def add_episode(self, **kwargs):
        self.added.append(dict(kwargs, source=kwargs["source"].value))
        self.episodes.append(SimpleNamespace(content=kwargs["episode_body"]))
        return SimpleNamespace(uuid=f"graph:{kwargs['name']}")

    async def search(self, query, *, group_ids=None, num_results=10):
        self.search_calls.append({"query": query, "group_ids": group_ids, "num_results": num_results})
        if self.raise_on_search is not None:
            raise self.raise_on_search
        return list(self.edges[:num_results])

    async def retrieve_episodes(self, *, reference_time, last_n=3, group_ids=None):
        _ = reference_time
        _ = group_ids
        return list(self.episodes[-last_n:])


def _episode(entity_type: str, natural_id: str, payload: dict) -> OntologyEpisode:
    return OntologyEpisode.from_payload(
        event_id=f"evt_{natural_id.replace(':', '_')}",
        entity_type=entity_type,
        natural_id=natural_id,
        payload=payload,
        observed_at="2026-06-19T00:00:00+00:00",
        reference_time="2026-06-19T00:00:00+00:00",
    )
