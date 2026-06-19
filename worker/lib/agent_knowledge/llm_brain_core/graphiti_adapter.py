from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from ._util import public_safe_text, short_hash
from .models import GraphMemoryResult, OntologyEpisode

_GRAPHITI_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class GraphitiNeo4jConfig:
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: str = ""
    default_group_id: str = ""
    llm_provider: str = "openai"
    llm_model: str = ""
    small_model: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    embedding_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_dim: int = 1024
    store_raw_episode_content: bool = True
    extract_entities: bool = False

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "GraphitiNeo4jConfig":
        env = environ or os.environ
        provider = env.get("LLM_BRAIN_GRAPH_LLM_PROVIDER", env.get("GRAPHITI_LLM_PROVIDER", "openai"))
        return cls(
            uri=env.get("LLM_BRAIN_NEO4J_URI", env.get("NEO4J_URI", "bolt://localhost:7687")),
            user=env.get("LLM_BRAIN_NEO4J_USER", env.get("NEO4J_USER", "neo4j")),
            password=env.get("LLM_BRAIN_NEO4J_PASSWORD", env.get("NEO4J_PASSWORD", "")),
            default_group_id=env.get("LLM_BRAIN_GRAPH_GROUP_ID", ""),
            llm_provider=provider.lower(),
            llm_model=env.get("LLM_BRAIN_LLM_MODEL", env.get("MODEL_NAME", "")),
            small_model=env.get("LLM_BRAIN_SMALL_LLM_MODEL", env.get("SMALL_MODEL_NAME", "")),
            llm_base_url=env.get("LLM_BRAIN_LLM_BASE_URL", env.get("OPENAI_BASE_URL", "")),
            llm_api_key=env.get("LLM_BRAIN_LLM_API_KEY", env.get("OPENAI_API_KEY", "")),
            embedding_model=env.get("LLM_BRAIN_EMBEDDING_MODEL", env.get("EMBEDDING_MODEL", "")),
            embedding_base_url=env.get("LLM_BRAIN_EMBEDDING_BASE_URL", env.get("OPENAI_BASE_URL", "")),
            embedding_api_key=env.get("LLM_BRAIN_EMBEDDING_API_KEY", env.get("OPENAI_API_KEY", "")),
            embedding_dim=_int_env(env.get("LLM_BRAIN_EMBEDDING_DIM", ""), default=1024),
            store_raw_episode_content=env.get("LLM_BRAIN_GRAPH_STORE_EPISODE_CONTENT", "true").lower()
            not in {"0", "false", "no"},
            extract_entities=env.get("LLM_BRAIN_GRAPH_EXTRACT_ENTITIES", "false").lower() in {"1", "true", "yes"},
        )


class GraphitiNeo4jGraphMemoryAdapter:
    """Graphiti-backed derived graph adapter.

    The core stays synchronous and backend-neutral. This wrapper runs Graphiti's
    async API behind the `GraphMemoryAdapter` protocol and stores only
    public-safe `OntologyEpisode` JSON, never raw transcripts or raw file paths.
    """

    def __init__(self, graphiti: Any, *, default_group_id: str = "", extract_entities: bool = False) -> None:
        self._graphiti = graphiti
        self._default_group_id = default_group_id
        self._extract_entities = extract_entities
        self._runner = _AsyncLoopRunner.get_instance()

    @classmethod
    def from_config(cls, config: GraphitiNeo4jConfig) -> "GraphitiNeo4jGraphMemoryAdapter":
        return cls(
            _build_graphiti(config),
            default_group_id=config.default_group_id,
            extract_entities=config.extract_entities,
        )

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "GraphitiNeo4jGraphMemoryAdapter":
        return cls.from_config(GraphitiNeo4jConfig.from_env(environ))

    def upsert_episode(self, episode: OntologyEpisode) -> str:
        body = json.dumps(episode.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        group_id = _graphiti_group_id(_group_id_for_episode(episode, self._default_group_id))

        async def _call():
            if not self._extract_entities:
                from graphiti_core.nodes import EpisodicNode

                graphiti_episode = EpisodicNode(
                    uuid=episode.episode_id,
                    name=episode.episode_id,
                    group_id=group_id or _graphiti_group_id("llm_brain_default"),
                    labels=[],
                    source=_episode_type_json(),
                    content=body,
                    source_description=f"llm_brain_core:{episode.entity_type}:{episode.natural_id}",
                    created_at=datetime.now(timezone.utc),
                    valid_at=_parse_datetime(episode.reference_time),
                )
                await graphiti_episode.save(self._graphiti.driver)
                return graphiti_episode

            return await self._graphiti.add_episode(
                name=episode.episode_id,
                episode_body=body,
                source_description=f"llm_brain_core:{episode.entity_type}:{episode.natural_id}",
                reference_time=_parse_datetime(episode.reference_time),
                source=_episode_type_json(),
                group_id=group_id or None,
            )

        result = self._runner.run(_call)
        graph_uuid = getattr(result, "uuid", "") or getattr(getattr(result, "episode", None), "uuid", "")
        return public_safe_text(str(graph_uuid or episode.episode_id), max_chars=240)

    def search_context(
        self,
        *,
        brain_id: str,
        query: str,
        entity_types: list[str] | None = None,
        limit: int = 10,
    ) -> GraphMemoryResult:
        bounded = max(1, min(int(limit), 100))
        group_id = _graphiti_group_id(brain_id or self._default_group_id)
        group_ids = [group_id] if group_id else None

        async def _call() -> tuple[list[Any], list[Any]]:
            edges = await self._graphiti.search(
                query,
                group_ids=group_ids,
                num_results=bounded,
            )
            episodes = await self._graphiti.retrieve_episodes(
                reference_time=datetime.now(timezone.utc),
                last_n=max(bounded * 5, bounded),
                group_ids=group_ids,
            )
            return list(edges or []), list(episodes or [])

        try:
            edges, episodes = self._runner.run(_call)
        except Exception as exc:
            return GraphMemoryResult(status="error", details=(type(exc).__name__,))

        wanted = set(entity_types or [])
        terms = _terms(query)
        converted: list[OntologyEpisode] = []
        for episode_node in episodes:
            episode = _episode_node_to_ontology(episode_node)
            if episode is None:
                continue
            if wanted and episode.entity_type not in wanted:
                continue
            if terms and not _matches(episode.search_text(), terms):
                continue
            converted.append(episode)
        for edge in edges:
            episode = _edge_to_ontology(edge)
            if wanted and episode.entity_type not in wanted:
                continue
            converted.append(episode)

        converted.sort(key=lambda item: (item.observed_at, item.episode_id), reverse=True)
        return GraphMemoryResult(
            status="available",
            episodes=tuple(converted[:bounded]),
            details=("graphiti_neo4j",),
        )


def _build_graphiti(config: GraphitiNeo4jConfig):
    from graphiti_core import Graphiti

    if config.llm_provider in {"ollama", "openai-compatible", "openai_compatible"}:
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
        from graphiti_core.llm_client.config import LLMConfig
        from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

        base_url = config.llm_base_url or ("http://localhost:11434/v1" if config.llm_provider == "ollama" else "")
        api_key = config.llm_api_key or ("ollama" if config.llm_provider == "ollama" else "")
        llm_config = LLMConfig(
            api_key=api_key,
            model=config.llm_model or ("deepseek-r1:7b" if config.llm_provider == "ollama" else None),
            small_model=config.small_model or config.llm_model or ("deepseek-r1:7b" if config.llm_provider == "ollama" else None),
            base_url=base_url or None,
        )
        llm_client = OpenAIGenericClient(config=llm_config)
        embedder = OpenAIEmbedder(
            config=OpenAIEmbedderConfig(
                api_key=config.embedding_api_key or api_key,
                embedding_model=config.embedding_model or ("nomic-embed-text" if config.llm_provider == "ollama" else "text-embedding-3-small"),
                embedding_dim=config.embedding_dim,
                base_url=config.embedding_base_url or base_url or None,
            )
        )
        return Graphiti(
            config.uri,
            config.user,
            config.password,
            llm_client=llm_client,
            embedder=embedder,
            cross_encoder=OpenAIRerankerClient(client=llm_client, config=llm_config),
            store_raw_episode_content=config.store_raw_episode_content,
        )

    return Graphiti(
        config.uri,
        config.user,
        config.password,
        store_raw_episode_content=config.store_raw_episode_content,
    )


class _AsyncLoopRunner:
    _instance: _AsyncLoopRunner | None = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> _AsyncLoopRunner:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def run(self, factory: Callable[[], Any]) -> Any:
        async def _invoke():
            return await factory()

        future = asyncio.run_coroutine_threadsafe(_invoke(), self._loop)
        return future.result(timeout=300)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()


def _episode_type_json():
    from graphiti_core.nodes import EpisodeType

    return EpisodeType.json


def _episode_node_to_ontology(node: Any) -> OntologyEpisode | None:
    content = getattr(node, "content", "")
    if not isinstance(content, str) or not content.strip():
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or "episode_id" not in parsed:
        return None
    return OntologyEpisode(
        episode_id=str(parsed["episode_id"]),
        event_id=str(parsed["event_id"]),
        idempotency_key=str(parsed["idempotency_key"]),
        entity_type=str(parsed["entity_type"]),
        natural_id=str(parsed["natural_id"]),
        lifecycle_state=str(parsed["lifecycle_state"]),
        currentness=str(parsed["currentness"]),
        source_event_ids=tuple(parsed.get("source_event_ids") or ()),
        source_ref_ids=tuple(parsed.get("source_ref_ids") or ()),
        valid_from=str(parsed.get("valid_from") or ""),
        valid_to=str(parsed.get("valid_to") or ""),
        observed_at=str(parsed.get("observed_at") or ""),
        reference_time=str(parsed.get("reference_time") or ""),
        content_hash=str(parsed["content_hash"]),
        ontology_version=str(parsed.get("ontology_version") or "1.0.0"),
        extractor_version=str(parsed.get("extractor_version") or "0.1.0"),
        payload=dict(parsed.get("payload") or {}),
        relations=tuple(dict(relation) for relation in parsed.get("relations") or ()),
    )


def _edge_to_ontology(edge: Any) -> OntologyEpisode:
    edge_uuid = public_safe_text(str(getattr(edge, "uuid", "") or getattr(edge, "name", "") or ""), max_chars=200)
    fact = public_safe_text(str(getattr(edge, "fact", "") or getattr(edge, "name", "") or ""), max_chars=1024)
    valid_at = _datetime_to_iso(getattr(edge, "valid_at", None) or getattr(edge, "reference_time", None))
    invalid_at = _datetime_to_iso(getattr(edge, "invalid_at", None) or getattr(edge, "expired_at", None))
    payload = {
        "graphiti_edge_uuid": edge_uuid,
        "fact": fact,
        "source_node_uuid": public_safe_text(str(getattr(edge, "source_node_uuid", "") or ""), max_chars=200),
        "target_node_uuid": public_safe_text(str(getattr(edge, "target_node_uuid", "") or ""), max_chars=200),
    }
    return OntologyEpisode.from_payload(
        event_id=f"evt:graphiti:{short_hash([edge_uuid, fact])}",
        entity_type="GraphFact",
        natural_id=f"graphiti-fact:{short_hash([edge_uuid, fact])}",
        payload=payload,
        valid_from=valid_at,
        valid_to=invalid_at,
        observed_at=valid_at,
        reference_time=valid_at,
        extractor_version="graphiti-edge.1",
    )


def _group_id_for_episode(episode: OntologyEpisode, default_group_id: str) -> str:
    payload_brain_id = episode.payload.get("brain_id")
    if payload_brain_id:
        return public_safe_text(str(payload_brain_id), max_chars=200)
    return default_group_id


def _graphiti_group_id(value: str) -> str:
    text = public_safe_text(str(value or ""), max_chars=200)
    if not text:
        return ""
    if _GRAPHITI_GROUP_ID_RE.fullmatch(text):
        return text
    return f"brain_{short_hash(text)}"


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _datetime_to_iso(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return datetime.now(timezone.utc).isoformat()


def _terms(value: Any) -> list[str]:
    return [term for term in str(value or "").lower().split() if len(term) >= 3]


def _matches(value: str, terms: list[str]) -> bool:
    return any(term in value for term in terms)


def _int_env(value: str, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
