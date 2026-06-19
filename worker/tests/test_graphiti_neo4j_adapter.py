from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

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

    assert result.status == "available"
    assert [episode.natural_id for episode in result.episodes] == ["task:episode-only"]
    assert result.details == ("graphiti_neo4j", "edge_search:RuntimeError")


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
