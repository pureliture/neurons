from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable

from agent_knowledge.model_connectors import resolve_model_connection_config
from agent_knowledge.model_connectors.openai_compatible import (
    normalize_structured_response as _shared_normalize_structured_response,
)

from ._util import public_safe_text, short_hash
from .graph import UpsertEpisodeResult
from .graphiti_backend import build_graphiti_from_config
from .models import GraphMemoryResult, OntologyEpisode

_GRAPHITI_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Default async-call timeouts (seconds). Reads (search/retrieve) are expected to
# return in seconds; writes (entity extraction via the LLM) can take much
# longer. These are split so a slow write does not force every read to wait the
# full upper bound, and both are injectable for tests and tuning.
DEFAULT_GRAPH_READ_TIMEOUT_SECONDS = 30.0
DEFAULT_GRAPH_WRITE_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class GraphitiNeo4jConfig:
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    # Secrets are repr=False so an accidental repr()/traceback/locals dump of the
    # config never leaks credentials (CLAUDE.md: no token/credential in output).
    password: str = field(default="", repr=False)
    default_group_id: str = ""
    llm_provider: str = "openai"
    llm_model: str = ""
    small_model: str = ""
    llm_base_url: str = ""
    llm_api_key: str = field(default="", repr=False)
    embedding_provider: str = "openai"
    embedding_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = field(default="", repr=False)
    embedding_dim: int = 1024
    store_raw_episode_content: bool = True
    extract_entities: bool = False
    fallback_llm_model: str = ""
    fallback_small_model: str = ""
    primary_attempts: int = 1
    fallback_attempts: int = 1
    read_timeout_seconds: float = DEFAULT_GRAPH_READ_TIMEOUT_SECONDS
    write_timeout_seconds: float = DEFAULT_GRAPH_WRITE_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "GraphitiNeo4jConfig":
        env = os.environ if environ is None else environ
        model_config = resolve_model_connection_config(env)
        return cls(
            uri=env.get("LLM_BRAIN_NEO4J_URI", env.get("NEO4J_URI", "bolt://localhost:7687")),
            user=env.get("LLM_BRAIN_NEO4J_USER", env.get("NEO4J_USER", "neo4j")),
            password=env.get("LLM_BRAIN_NEO4J_PASSWORD", env.get("NEO4J_PASSWORD", "")),
            default_group_id=env.get("LLM_BRAIN_GRAPH_GROUP_ID", ""),
            llm_provider=model_config.llm.provider,
            llm_model=model_config.llm.model,
            small_model=model_config.llm.small_model,
            llm_base_url=model_config.llm.base_url,
            llm_api_key=env.get("LLM_BRAIN_LLM_API_KEY", env.get("OPENAI_API_KEY", "")),
            embedding_provider=model_config.embedding.provider,
            embedding_model=model_config.embedding.model,
            embedding_base_url=model_config.embedding.base_url,
            embedding_api_key=env.get("LLM_BRAIN_EMBEDDING_API_KEY", env.get("OPENAI_API_KEY", "")),
            embedding_dim=model_config.embedding.dim,
            store_raw_episode_content=env.get("LLM_BRAIN_GRAPH_STORE_EPISODE_CONTENT", "true").lower()
            not in {"0", "false", "no"},
            extract_entities=env.get("LLM_BRAIN_GRAPH_EXTRACT_ENTITIES", "false").lower() in {"1", "true", "yes"},
            fallback_llm_model=model_config.fallback_llm_model,
            fallback_small_model=model_config.fallback_small_model,
            primary_attempts=model_config.primary_attempts,
            fallback_attempts=model_config.fallback_attempts,
            read_timeout_seconds=_float_env(
                env.get("LLM_BRAIN_GRAPH_READ_TIMEOUT_SECONDS", ""),
                default=DEFAULT_GRAPH_READ_TIMEOUT_SECONDS,
            ),
            write_timeout_seconds=_float_env(
                env.get("LLM_BRAIN_GRAPH_WRITE_TIMEOUT_SECONDS", ""),
                default=DEFAULT_GRAPH_WRITE_TIMEOUT_SECONDS,
            ),
        )


class GraphitiNeo4jGraphMemoryAdapter:
    """Graphiti-backed derived graph adapter.

    The core stays synchronous and backend-neutral. This wrapper runs Graphiti's
    async API behind the `GraphMemoryAdapter` protocol and stores only
    public-safe `OntologyEpisode` JSON, never raw transcripts or raw file paths.
    """

    def __init__(
        self,
        graphiti: Any,
        *,
        fallback_graphiti: Any | None = None,
        default_group_id: str = "",
        extract_entities: bool = False,
        primary_attempts: int = 1,
        fallback_attempts: int = 1,
        episode_exists: Callable[[Any, str], Any] | None = None,
        read_timeout_seconds: float = DEFAULT_GRAPH_READ_TIMEOUT_SECONDS,
        write_timeout_seconds: float = DEFAULT_GRAPH_WRITE_TIMEOUT_SECONDS,
        runner: "_AsyncLoopRunner | None" = None,
    ) -> None:
        self._graphiti = graphiti
        self._fallback_graphiti = fallback_graphiti
        self._default_group_id = default_group_id
        self._extract_entities = extract_entities
        self._primary_attempts = max(1, int(primary_attempts))
        self._fallback_attempts = max(1, int(fallback_attempts))
        # Split read/write timeouts: a read that hangs must not be forced to wait
        # the (longer) write upper bound, and vice versa. Injectable so a unit
        # test can drive the timeout path deterministically with a tiny bound.
        self._read_timeout_seconds = float(read_timeout_seconds)
        self._write_timeout_seconds = float(write_timeout_seconds)
        # `runner` is injectable so a test can supply a non-singleton loop runner
        # without poisoning the shared production singleton.
        self._runner = runner if runner is not None else _AsyncLoopRunner.get_instance()
        self._last_write_details: tuple[str, ...] = ()
        # Existence probe for episode_id MERGE idempotency. Injectable so tests
        # can simulate a pre-existing episode without a live Neo4j. Defaults to
        # Graphiti's EpisodicNode.get_by_uuid (async), which raises when absent.
        self._episode_exists = episode_exists or _default_episode_exists

    @property
    def last_write_details(self) -> tuple[str, ...]:
        return self._last_write_details

    @classmethod
    def from_config(cls, config: GraphitiNeo4jConfig) -> "GraphitiNeo4jGraphMemoryAdapter":
        fallback_graphiti = None
        if config.extract_entities and config.fallback_llm_model:
            fallback_config = replace(
                config,
                llm_model=config.fallback_llm_model,
                small_model=config.fallback_small_model or config.fallback_llm_model,
                fallback_llm_model="",
                fallback_small_model="",
            )
            fallback_graphiti = build_graphiti_from_config(fallback_config)
        return cls(
            build_graphiti_from_config(config),
            fallback_graphiti=fallback_graphiti,
            default_group_id=config.default_group_id,
            extract_entities=config.extract_entities,
            primary_attempts=config.primary_attempts,
            fallback_attempts=config.fallback_attempts,
            read_timeout_seconds=config.read_timeout_seconds,
            write_timeout_seconds=config.write_timeout_seconds,
        )

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "GraphitiNeo4jGraphMemoryAdapter":
        return cls.from_config(GraphitiNeo4jConfig.from_env(environ))

    def upsert_episode(self, episode: OntologyEpisode) -> UpsertEpisodeResult:
        body = json.dumps(episode.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        group_id = _graphiti_group_id(_group_id_for_episode(episode, self._default_group_id))

        async def _call() -> UpsertEpisodeResult:
            if not self._extract_entities:
                from graphiti_core.nodes import EpisodicNode

                # episode_id MERGE idempotency: an episode_id already encodes the
                # content_hash (see OntologyEpisode.from_payload), so a node with
                # the same uuid is the same content. Treat a re-upsert as a
                # `duplicate` to stay symmetric with FakeGraphMemoryAdapter, not a
                # second projected row.
                if await self._episode_exists(self._graphiti.driver, episode.episode_id):
                    self._last_write_details = ("graphiti_neo4j", "duplicate")
                    return "duplicate"
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
                try:
                    await graphiti_episode.save(self._graphiti.driver)
                except Exception as exc:
                    self._last_write_details = (
                        "graphiti_neo4j",
                        f"direct_write_error:{type(exc).__name__}",
                    )
                    raise
                self._last_write_details = ("graphiti_neo4j", "episode_node_direct_write")
                return "inserted"

            self._last_write_details = await self._add_episode_with_fallback(
                name=episode.episode_id,
                episode_body=body,
                source_description=f"llm_brain_core:{episode.entity_type}:{episode.natural_id}",
                reference_time=_parse_datetime(episode.reference_time),
                source=_episode_type_json(),
                group_id=group_id or None,
            )
            return "inserted"

        return self._runner.run(_call, timeout=self._write_timeout_seconds)

    async def _add_episode_with_fallback(self, **kwargs: Any) -> tuple[str, ...]:
        last_error: Exception | None = None
        primary_errors: list[str] = []
        for _ in range(self._primary_attempts):
            try:
                await self._graphiti.add_episode(**kwargs)
                return ("graphiti_neo4j", "primary_write")
            except Exception as exc:
                last_error = exc
                primary_errors.append(f"primary_error:{type(exc).__name__}")
        if self._fallback_graphiti is not None:
            fallback_errors: list[str] = []
            for _ in range(self._fallback_attempts):
                try:
                    await self._fallback_graphiti.add_episode(**kwargs)
                    return (
                        "graphiti_neo4j",
                        "fallback_used",
                        *(primary_errors[-1:] or ()),
                    )
                except Exception as exc:
                    last_error = exc
                    fallback_errors.append(f"fallback_error:{type(exc).__name__}")
            self._last_write_details = (
                "graphiti_neo4j",
                *(primary_errors[-1:] or ()),
                *(fallback_errors[-1:] or ()),
            )
        elif primary_errors:
            self._last_write_details = ("graphiti_neo4j", primary_errors[-1])
        if last_error is not None:
            raise last_error
        return ("graphiti_neo4j", "write_not_attempted")

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

        async def _call() -> tuple[list[Any], list[Any], list[str], bool]:
            details: list[str] = ["graphiti_neo4j"]
            edge_degraded = False
            try:
                edges = await self._graphiti.search(
                    query,
                    group_ids=group_ids,
                    num_results=bounded,
                )
            except Exception as exc:
                edges = []
                edge_degraded = True
                details.append(f"edge_search:{type(exc).__name__}")
            episodes = await self._graphiti.retrieve_episodes(
                reference_time=datetime.now(timezone.utc),
                last_n=max(bounded * 5, bounded),
                group_ids=group_ids,
            )
            return list(edges or []), list(episodes or []), details, edge_degraded

        try:
            edges, episodes, details, edge_degraded = self._runner.run(
                _call, timeout=self._read_timeout_seconds
            )
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
        # Edge (relationship) search failure with surviving episode reads is a
        # partial result, not a healthy 'available' one. Separate it so downstream
        # gates cannot read a false-healthy graph_status.
        if edge_degraded:
            return GraphMemoryResult(
                status="degraded",
                episodes=tuple(converted[:bounded]),
                details=tuple([*details, "graph_edge_degraded"]),
            )
        return GraphMemoryResult(
            status="available",
            episodes=tuple(converted[:bounded]),
            details=tuple(details),
        )

    def get_episodes_by_ids(
        self,
        episode_ids: list[str] | tuple[str, ...],
        *,
        brain_id: str = "",
        entity_types: list[str] | None = None,
    ) -> tuple[OntologyEpisode, ...]:
        wanted = [str(item) for item in episode_ids if str(item or "")]
        if not wanted:
            return ()
        group_id = _graphiti_group_id(brain_id or self._default_group_id)
        wanted_types = set(entity_types or [])

        async def _call() -> list[OntologyEpisode]:
            from graphiti_core.nodes import EpisodicNode

            episodes: list[OntologyEpisode] = []
            for episode_id in wanted:
                try:
                    node = await EpisodicNode.get_by_uuid(self._graphiti.driver, episode_id)
                except Exception:
                    continue
                if group_id and str(getattr(node, "group_id", "") or "") != group_id:
                    continue
                episode = _episode_node_to_ontology(node)
                if episode is None:
                    continue
                if wanted_types and episode.entity_type not in wanted_types:
                    continue
                episodes.append(episode)
            return episodes

        return tuple(self._runner.run(_call, timeout=self._read_timeout_seconds))


async def _default_episode_exists(driver: Any, episode_id: str) -> bool:
    """Return True when an EpisodicNode with ``episode_id`` already exists.

    Used by the production-default (extract_entities=False) path to detect a
    re-upsert of the same episode_id as a `duplicate`. Graphiti's
    ``EpisodicNode.get_by_uuid`` raises ``NodeNotFoundError`` when the node is
    absent; any lookup error is treated as "not present" so a transient read
    failure degrades to an insert attempt rather than masking a real write.
    """

    from graphiti_core.nodes import EpisodicNode

    try:
        node = await EpisodicNode.get_by_uuid(driver, episode_id)
    except Exception:
        return False
    return node is not None


def probe_graphiti_connectivity(adapter: Any) -> None:
    """One-shot connectivity probe for the must-have (required) graph path.

    Calls the underlying Neo4j driver's ``verify_connectivity`` once so a dead
    or unreachable backend fails fast at startup instead of degrading to an empty
    'available' read later. Raises on failure; returns ``None`` on success.

    The probe is kept here (next to the adapter) but injected through
    ``runtime_graph.build_graph_adapter_from_env`` so tests can substitute a
    failing probe without a live Neo4j.
    """

    graphiti = getattr(adapter, "_graphiti", None)
    driver = getattr(graphiti, "driver", None)
    if driver is None:
        raise RuntimeError("graph connectivity probe: no driver available")
    verify = getattr(driver, "verify_connectivity", None)
    if verify is None:
        # Wrapped drivers may expose the neo4j driver one level down.
        inner = getattr(driver, "client", None) or getattr(driver, "_driver", None)
        verify = getattr(inner, "verify_connectivity", None)
    if verify is None:
        raise RuntimeError("graph connectivity probe: driver has no verify_connectivity")
    result = verify()
    if asyncio.iscoroutine(result):
        _AsyncLoopRunner.get_instance().run(
            lambda: result, timeout=DEFAULT_GRAPH_READ_TIMEOUT_SECONDS
        )


def _normalize_structured_response(value: Any, response_model: Any = None) -> Any:
    return _shared_normalize_structured_response(value, response_model)


def _build_graphiti(config: GraphitiNeo4jConfig):
    return build_graphiti_from_config(config)


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

    def __init__(self, *, default_timeout: float = DEFAULT_GRAPH_WRITE_TIMEOUT_SECONDS) -> None:
        self._default_timeout = float(default_timeout)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def run(self, factory: Callable[[], Any], *, timeout: float | None = None) -> Any:
        """Run an async factory on the background loop with a bounded wait.

        `timeout` is the per-call wait in seconds; ``None`` falls back to the
        runner's configured default. A timeout raises
        ``concurrent.futures.TimeoutError`` (the caller decides how to surface
        it: reads degrade to ``status='error'``, writes propagate as a failed
        upsert). The pending coroutine is cancelled so a hung call does not leak
        onto the shared loop.
        """

        async def _invoke():
            return await factory()

        wait = self._default_timeout if timeout is None else float(timeout)
        future = asyncio.run_coroutine_threadsafe(_invoke(), self._loop)
        try:
            return future.result(timeout=wait)
        except FuturesTimeoutError:
            # Stop the orphaned coroutine from running forever on the shared loop.
            future.cancel()
            raise

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def shutdown(self) -> None:
        """Stop the background loop and join its thread (process-exit seam).

        Idempotent and best-effort: a daemon thread would die with the process
        anyway, but an explicit shutdown lets a long-lived host (or a test)
        release the loop deterministically instead of relying on interpreter
        teardown.
        """

        loop = getattr(self, "_loop", None)
        if loop is None or loop.is_closed():
            return
        # Cancel any still-pending coroutines (e.g. a timed-out backend call whose
        # future was cancelled but whose task had not yet unwound) and let the
        # loop drain them before stopping, so teardown does not emit "Task was
        # destroyed but it is pending" noise.
        drain = asyncio.run_coroutine_threadsafe(_drain_pending_tasks(), loop)
        try:
            drain.result(timeout=5)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        thread = getattr(self, "_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        if not loop.is_closed():
            loop.close()
        if _AsyncLoopRunner._instance is self:
            with _AsyncLoopRunner._lock:
                if _AsyncLoopRunner._instance is self:
                    _AsyncLoopRunner._instance = None


async def _drain_pending_tasks() -> None:
    """Cancel and await every other task on the current loop.

    Run on the loop thread during shutdown so timed-out/cancelled coroutines
    unwind cleanly instead of being garbage-collected while still pending.
    """

    current = asyncio.current_task()
    pending = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
    for task in pending:
        task.cancel()
    for task in pending:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


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
    required_fields = (
        "episode_id",
        "event_id",
        "idempotency_key",
        "entity_type",
        "natural_id",
        "lifecycle_state",
        "currentness",
        "content_hash",
    )
    if not isinstance(parsed, dict) or any(not parsed.get(field) for field in required_fields):
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
        observed_at=valid_at or "unknown",
        reference_time=valid_at or "unknown",
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
    return ""


def _terms(value: Any) -> list[str]:
    return [term for term in str(value or "").lower().split() if len(term) >= 3]


def _matches(value: str, terms: list[str]) -> bool:
    return any(term in value for term in terms)


def _float_env(value: str, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
