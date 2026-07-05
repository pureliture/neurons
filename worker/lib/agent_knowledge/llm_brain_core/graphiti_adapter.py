from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable

from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

from ._util import (
    PRIVATE_OUTPUT_RE,
    SECRET_ASSIGNMENT_RE,
    ensure_public_safe,
    public_safe_text,
    short_hash,
)
from .graph import UpsertEpisodeResult
from .models import GraphMemoryResult, OntologyEpisode
from ..model_connectors.structured_response import (
    existing_fact_idx_values_from_messages as _existing_fact_idx_values_from_messages,
    is_list_annotation as _is_list_annotation,
    normalize_structured_keys as _normalize_structured_keys,
    normalize_structured_response as _normalize_structured_response,
)

_GRAPHITI_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Default async-call timeouts (seconds). Reads (search/retrieve) are expected to
# return in seconds; writes (entity extraction via the LLM) can take much
# longer. These are split so a slow write does not force every read to wait the
# full upper bound, and both are injectable for tests and tuning.
DEFAULT_GRAPH_READ_TIMEOUT_SECONDS = 30.0
DEFAULT_GRAPH_WRITE_TIMEOUT_SECONDS = 300.0
_ALLOWED_LLM_REASONING_EFFORTS = frozenset({"high", "medium", "low", "none"})
_GEMINI_LLM_FORBIDDEN_MESSAGE = (
    "Gemini LLM models are forbidden for Graphiti semantic extraction; "
    "use Gemma-4 MaaS or an Ollama model instead. Gemini embeddings remain allowed."
)

# Generic fallback embedding dimension when neither the env nor a known model
# pins one. Kept as a single source so the dataclass default, from_env default,
# and the model/dim reconciliation in _build_graphiti cannot drift apart.
_DEFAULT_EMBEDDING_DIM = 1024
# Native output dimensions for embedding models whose true dimension differs from
# the generic _DEFAULT_EMBEDDING_DIM. nomic-embed-text (the ollama default) emits
# 768-dim vectors; pairing it with the 1024 default mismatches the index/query
# dimension. Add only models whose native dim is known to be safe to assume.
_KNOWN_EMBEDDING_DIMS = {
    "nomic-embed-text": 768,
}


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
    llm_reasoning_effort: str = ""
    llm_base_url: str = ""
    llm_api_key: str = field(default="", repr=False)
    embedding_provider: str = "openai"
    embedding_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = field(default="", repr=False)
    embedding_dim: int = _DEFAULT_EMBEDDING_DIM
    store_raw_episode_content: bool = True
    extract_entities: bool = False
    force_reextract_entities: bool = False
    fallback_llm_model: str = ""
    fallback_small_model: str = ""
    primary_attempts: int = 1
    fallback_attempts: int = 1
    primary_attempt_timeout_seconds: float = 0.0
    fallback_attempt_timeout_seconds: float = 0.0
    read_timeout_seconds: float = DEFAULT_GRAPH_READ_TIMEOUT_SECONDS
    write_timeout_seconds: float = DEFAULT_GRAPH_WRITE_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "GraphitiNeo4jConfig":
        env = environ or os.environ
        provider = env.get("LLM_BRAIN_GRAPH_LLM_PROVIDER", env.get("GRAPHITI_LLM_PROVIDER", "openai"))
        llm_model = env.get("LLM_BRAIN_LLM_MODEL", env.get("MODEL_NAME", ""))
        small_model = env.get("LLM_BRAIN_SMALL_LLM_MODEL", env.get("SMALL_MODEL_NAME", ""))
        fallback_llm_model = env.get(
            "LLM_BRAIN_LLM_FALLBACK_MODEL",
            env.get("LLM_BRAIN_GRAPH_FALLBACK_LLM_MODEL", ""),
        )
        fallback_small_model = env.get(
            "LLM_BRAIN_SMALL_LLM_FALLBACK_MODEL",
            env.get("LLM_BRAIN_GRAPH_FALLBACK_SMALL_LLM_MODEL", ""),
        )
        for model in (llm_model, small_model, fallback_llm_model, fallback_small_model):
            _reject_forbidden_gemini_llm_model(model)
        return cls(
            uri=env.get("LLM_BRAIN_NEO4J_URI", env.get("NEO4J_URI", "bolt://localhost:7687")),
            user=env.get("LLM_BRAIN_NEO4J_USER", env.get("NEO4J_USER", "neo4j")),
            password=env.get("LLM_BRAIN_NEO4J_PASSWORD", env.get("NEO4J_PASSWORD", "")),
            default_group_id=env.get("LLM_BRAIN_GRAPH_GROUP_ID", ""),
            llm_provider=provider.lower(),
            llm_model=llm_model,
            small_model=small_model,
            llm_reasoning_effort=_llm_reasoning_effort_env(
                env.get("LLM_BRAIN_LLM_REASONING_EFFORT", "")
            ),
            llm_base_url=env.get("LLM_BRAIN_LLM_BASE_URL", env.get("OPENAI_BASE_URL", "")),
            llm_api_key=env.get("LLM_BRAIN_LLM_API_KEY", env.get("OPENAI_API_KEY", "")),
            embedding_provider=env.get("LLM_BRAIN_EMBEDDING_PROVIDER", env.get("EMBEDDING_PROVIDER", "openai")).lower(),
            embedding_model=env.get("LLM_BRAIN_EMBEDDING_MODEL", env.get("EMBEDDING_MODEL", "")),
            embedding_base_url=env.get("LLM_BRAIN_EMBEDDING_BASE_URL", env.get("OPENAI_BASE_URL", "")),
            embedding_api_key=env.get("LLM_BRAIN_EMBEDDING_API_KEY", env.get("OPENAI_API_KEY", "")),
            embedding_dim=_int_env(env.get("LLM_BRAIN_EMBEDDING_DIM", ""), default=_DEFAULT_EMBEDDING_DIM),
            store_raw_episode_content=env.get("LLM_BRAIN_GRAPH_STORE_EPISODE_CONTENT", "true").lower()
            not in {"0", "false", "no"},
            extract_entities=env.get("LLM_BRAIN_GRAPH_EXTRACT_ENTITIES", "false").lower() in {"1", "true", "yes"},
            force_reextract_entities=env.get("LLM_BRAIN_GRAPH_FORCE_REEXTRACT_ENTITIES", "false").lower()
            in {"1", "true", "yes"},
            fallback_llm_model=fallback_llm_model,
            fallback_small_model=fallback_small_model,
            primary_attempts=_positive_int_env(
                env.get("LLM_BRAIN_GRAPH_PRIMARY_ATTEMPTS", ""),
                default=1,
            ),
            fallback_attempts=_positive_int_env(
                env.get("LLM_BRAIN_GRAPH_FALLBACK_ATTEMPTS", ""),
                default=1,
            ),
            primary_attempt_timeout_seconds=_non_negative_float_env(
                env.get("LLM_BRAIN_GRAPH_PRIMARY_ATTEMPT_TIMEOUT_SECONDS", ""),
                default=0.0,
            ),
            fallback_attempt_timeout_seconds=_non_negative_float_env(
                env.get("LLM_BRAIN_GRAPH_FALLBACK_ATTEMPT_TIMEOUT_SECONDS", ""),
                default=0.0,
            ),
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
        force_reextract_entities: bool = False,
        primary_attempts: int = 1,
        fallback_attempts: int = 1,
        primary_attempt_timeout_seconds: float = 0.0,
        fallback_attempt_timeout_seconds: float = 0.0,
        episode_exists: Callable[[Any, str], Any] | None = None,
        entity_extracted: Callable[[Any, str], Any] | None = None,
        read_timeout_seconds: float = DEFAULT_GRAPH_READ_TIMEOUT_SECONDS,
        write_timeout_seconds: float = DEFAULT_GRAPH_WRITE_TIMEOUT_SECONDS,
        runner: "_AsyncLoopRunner | None" = None,
    ) -> None:
        self._graphiti = graphiti
        self._fallback_graphiti = fallback_graphiti
        self._default_group_id = default_group_id
        self._extract_entities = extract_entities
        self._force_reextract_entities = force_reextract_entities
        self._primary_attempts = max(1, int(primary_attempts))
        self._fallback_attempts = max(1, int(fallback_attempts))
        self._primary_attempt_timeout_seconds = max(0.0, float(primary_attempt_timeout_seconds))
        self._fallback_attempt_timeout_seconds = max(0.0, float(fallback_attempt_timeout_seconds))
        # Split read/write timeouts: a read that hangs must not be forced to wait
        # the (longer) write upper bound, and vice versa. Injectable so a unit
        # test can drive the timeout path deterministically with a tiny bound.
        self._read_timeout_seconds = float(read_timeout_seconds)
        self._write_timeout_seconds = float(write_timeout_seconds)
        # `runner` is injectable so a test can supply a non-singleton loop runner
        # without poisoning the shared production singleton.
        self._runner = runner if runner is not None else _AsyncLoopRunner.get_instance()
        # Existence probe for episode_id MERGE idempotency. Injectable so tests
        # can simulate a pre-existing episode without a live Neo4j. Defaults to
        # Graphiti's EpisodicNode.get_by_uuid (async), which raises when absent.
        self._episode_exists = episode_exists or _default_episode_exists
        # Entity-extraction idempotency probe (entity path only). Distinct from
        # _episode_exists: an EpisodicNode can already exist (episodic pass) while
        # NO Entity/RELATES_TO has been extracted yet, so the entity pass must
        # still run. This probe answers "did the entity pass already run for this
        # episode_id" by checking for MENTIONS edges from the episode to any
        # Entity. Injectable for tests; defaults to a MENTIONS-count query.
        self._entity_extracted = entity_extracted or _default_entity_extracted

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
            fallback_graphiti = _build_graphiti(fallback_config)
        return cls(
            _build_graphiti(config),
            fallback_graphiti=fallback_graphiti,
            default_group_id=config.default_group_id,
            extract_entities=config.extract_entities,
            force_reextract_entities=config.force_reextract_entities,
            primary_attempts=config.primary_attempts,
            fallback_attempts=config.fallback_attempts,
            primary_attempt_timeout_seconds=config.primary_attempt_timeout_seconds,
            fallback_attempt_timeout_seconds=config.fallback_attempt_timeout_seconds,
            read_timeout_seconds=config.read_timeout_seconds,
            write_timeout_seconds=config.write_timeout_seconds,
        )

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "GraphitiNeo4jGraphMemoryAdapter":
        return cls.from_config(GraphitiNeo4jConfig.from_env(environ))

    def _build_episodic_node(self, episode: OntologyEpisode, body: str, group_id: str) -> Any:
        """Build the EpisodicNode for ``episode`` keyed on its episode_id.

        Single source of the EpisodicNode shape for BOTH the episodic-only path
        and the entity path's ensure-save. Keying ``uuid`` and ``name`` on
        episode_id (which already encodes the content_hash) makes the node's
        identity MERGE-idempotent: the same content always maps to the same node,
        so the two paths cannot create divergent or duplicate Episodic nodes.
        """

        from graphiti_core.nodes import EpisodicNode

        return EpisodicNode(
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

    def upsert_episode(self, episode: OntologyEpisode) -> UpsertEpisodeResult:
        # Two-body split:
        #   `body`            -> stored EpisodicNode.content (canonical JSON). Recall
        #                        (_episode_node_to_ontology) parses it with json.loads,
        #                        so this MUST stay JSON for recall to keep working.
        #   `extraction_body` -> the entity-pass input handed to add_episode. Real
        #                        redacted prose (conversation chunks / typed-payload
        #                        meaning) extracts far richer entities than a JSON
        #                        metadata blob, which only ever yields generic ones.
        # extraction_body is derived/transient and is NOT in content_hash, so it
        # never changes episode_id (no node-dup explosion).
        body = json.dumps(episode.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        group_id = _graphiti_group_id(_group_id_for_episode(episode, self._default_group_id))

        async def _call() -> UpsertEpisodeResult:
            if not self._extract_entities:
                # episode_id MERGE idempotency: an episode_id already encodes the
                # content_hash (see OntologyEpisode.from_payload), so a node with
                # the same uuid is the same content. Treat a re-upsert as a
                # `duplicate` to stay symmetric with FakeGraphMemoryAdapter, not a
                # second projected row.
                if await self._episode_exists(self._graphiti.driver, episode.episode_id):
                    self._last_write_details = ("graphiti_neo4j", "duplicate")
                    return "duplicate"
                graphiti_episode = self._build_episodic_node(episode, body, group_id)
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

            # Entity-path idempotency guard. Unlike the episodic path, an existing
            # EpisodicNode is NOT enough: the entity pass may not have run yet. We
            # probe specifically for already-extracted Entity/RELATES_TO and treat
            # a hit as `duplicate`, so the entity pass is not re-run (and the LLM
            # not re-billed) for an episode already extracted.
            if (
                not self._force_reextract_entities
                and await self._entity_extracted(self._graphiti.driver, episode.episode_id)
            ):
                return "duplicate"
            extraction_body = _extraction_body_for(episode, body)
            # Ensure the episode_id-keyed EpisodicNode exists BEFORE add_episode.
            # Graphiti's add_episode(uuid=episode_id) does get_by_uuid(uuid) first
            # and then extracts from the returned node's `content`, not directly
            # from the `episode_body` argument. Therefore the entity path must
            # temporarily store the extraction prose on the existing node before
            # calling add_episode; otherwise Graphiti re-reads the canonical JSON
            # body and extracts only generic metadata entities. The `finally`
            # restore below puts recall-safe canonical JSON back on the node.
            await self._build_episodic_node(episode, extraction_body, group_id).save(
                self._graphiti.driver
            )
            # Pass uuid=episode_id so Graphiti reuses the existing EpisodicNode
            # (get_by_uuid, now guaranteed present) instead of minting a fresh
            # random uuid. episode_id encodes the content_hash, so the same
            # content always maps to the same node -- a 2-pass run (episodic then
            # entity) does not create a duplicate EpisodicNode.
            # episode_body is the extraction input (real prose), NOT the stored
            # content. The EpisodicNode whose content is the canonical JSON was
            # already ensure-saved above on {uuid: episode_id}; add_episode reuses
            # that node (uuid=episode_id) and only runs the entity pass over
            # episode_body, so the stored content stays JSON while extraction sees
            # prose. `source` stays json: the ensure-saved content is JSON.
            try:
                results = await self._add_episode_with_fallback(
                    name=episode.episode_id,
                    episode_body=extraction_body,
                    source_description=f"llm_brain_core:{episode.entity_type}:{episode.natural_id}",
                    reference_time=_parse_datetime(episode.reference_time),
                    source=_episode_type_json(),
                    group_id=group_id or None,
                    uuid=episode.episode_id,
                )
            finally:
                await self._build_episodic_node(episode, body, group_id).save(
                    self._graphiti.driver
                )
            # Write-time redaction hard gate. Inputs are already public-safe
            # (OntologyEpisode.__post_init__ -> ensure_public_safe), but the LLM
            # entity extractor synthesizes NEW text (EntityNode.name/summary,
            # RELATES_TO.fact). That synthesized text is not covered by the input
            # invariant, so we postcheck it and HARD FAIL (raise) on any private
            # path / secret-assignment match rather than letting it persist.
            #
            # add_episode has ALREADY written the Entity/RELATES_TO nodes/edges to
            # the graph by the time we get here, so a rejection alone would leave
            # the private/secret text persisted (graph pollution, P1). Delete the
            # just-persisted extracted elements before re-raising so the rejection
            # never survives in the graph.
            try:
                _reject_unsafe_extraction(results)
            except Exception:
                await _delete_extracted_graph_elements(self._graphiti.driver, results)
                raise
            return "inserted"

        return self._runner.run(_call, timeout=self._write_timeout_seconds)

    async def _add_episode_with_fallback(self, **kwargs: Any) -> Any:
        # A per-attempt timeout cancels the local await, but the remote LLM +
        # Neo4j write the cancelled add_episode kicked off may still be running
        # server-side. Re-firing add_episode for the same episode_id would then
        # double-extract. After any attempt that timed out, re-probe whether the
        # entity pass has since landed (MENTIONS edge present) before launching
        # another attempt; if it has, stop and treat it as done. Best-effort: the
        # probe narrows, but cannot fully close, the in-flight race.
        episode_uuid = kwargs.get("uuid")
        last_error: Exception | None = None
        timed_out = False
        for _ in range(self._primary_attempts):
            if timed_out and await self._entity_pass_already_landed(episode_uuid):
                return None
            try:
                return await _add_episode_with_attempt_timeout(
                    self._graphiti,
                    self._primary_attempt_timeout_seconds,
                    **kwargs,
                )
            except asyncio.TimeoutError as exc:
                last_error = exc
                timed_out = True
            except Exception as exc:
                last_error = exc
        if self._fallback_graphiti is not None:
            for _ in range(self._fallback_attempts):
                if timed_out and await self._entity_pass_already_landed(episode_uuid):
                    return None
                try:
                    return await _add_episode_with_attempt_timeout(
                        self._fallback_graphiti,
                        self._fallback_attempt_timeout_seconds,
                        **kwargs,
                    )
                except asyncio.TimeoutError as exc:
                    last_error = exc
                    timed_out = True
                except Exception as exc:
                    last_error = exc
        if last_error is not None:
            raise last_error

    async def _entity_pass_already_landed(self, episode_uuid: Any) -> bool:
        """Whether the entity pass for ``episode_uuid`` is already in the graph.

        Reuses the same MENTIONS probe as the pre-extraction idempotency guard.
        Any probe error degrades to False so a transient read failure leads to a
        retry rather than masking a still-missing extraction.
        """

        if not episode_uuid:
            return False
        try:
            return bool(await self._entity_extracted(self._graphiti.driver, str(episode_uuid)))
        except Exception:
            return False

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


async def _add_episode_with_attempt_timeout(graphiti: Any, timeout_seconds: float, **kwargs: Any) -> Any:
    add_episode = graphiti.add_episode(**kwargs)
    if timeout_seconds > 0:
        return await asyncio.wait_for(add_episode, timeout=timeout_seconds)
    return await add_episode


# MENTIONS is the EpisodicNode -> EntityNode edge Graphiti writes when the entity
# pass extracts entities for an episode. Its presence is the signal that the
# entity pass already ran for this episode_id (as opposed to an episodic-only
# node, which has no MENTIONS edges).
_ENTITY_EXTRACTED_QUERY = (
    "MATCH (e:Episodic {uuid: $uuid})-[:MENTIONS]->(:Entity) RETURN count(*) AS mentions"
)


async def _default_entity_extracted(driver: Any, episode_id: str) -> bool:
    """Return True when the entity pass already extracted entities for ``episode_id``.

    Probes for at least one MENTIONS edge from the episode to an Entity. A lookup
    error degrades to False ("not extracted") so a transient read failure leads
    to an extraction attempt rather than masking a missing entity pass. The
    driver may expose either ``execute_query`` (neo4j async) or a wrapper; we use
    ``execute_query`` which the adapter's other paths also rely on.
    """

    execute_query = getattr(driver, "execute_query", None)
    if execute_query is None:
        return False
    try:
        result = await execute_query(_ENTITY_EXTRACTED_QUERY, uuid=episode_id)
    except Exception:
        return False
    return _mentions_count(result) > 0


def _mentions_count(result: Any) -> int:
    """Extract the MENTIONS count from a neo4j execute_query result.

    neo4j's async ``execute_query`` returns a ``(records, summary, keys)`` tuple;
    each record is a mapping with the aliased ``mentions`` column. Any shape we
    cannot read returns 0 (treated as "not extracted") rather than raising.
    """

    records = result[0] if isinstance(result, (tuple, list)) and result else result
    if not records:
        return 0
    try:
        first = records[0]
    except (IndexError, TypeError, KeyError):
        return 0
    value: Any = None
    if isinstance(first, dict):
        value = first.get("mentions")
    else:
        getter = getattr(first, "get", None)
        if callable(getter):
            value = getter("mentions")
        else:
            try:
                value = first["mentions"]
            except (IndexError, TypeError, KeyError):
                value = None
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


_LOG = logging.getLogger(__name__)


def _extraction_body_for(episode: OntologyEpisode, json_body: str) -> str:
    """Pick the entity-pass extraction input for ``episode``.

    Prefers ``episode.extraction_text`` (real prose: conversation chunks or
    typed-payload meaning). Those sources are ALREADY ingress-redacted at capture
    (redact_public_ingress_text + assert_source_text_clean) / mapping, so the
    LLM-input prose is trusted as-is. We deliberately do NOT re-apply the strict
    public-safe gate to this input: that over-strict re-check rejected legitimate
    technical conversation (paths, ``key=value`` discussed in code) and regressed
    every session to the generic JSON body. The strict gate stays on extraction
    OUTPUT (``_reject_unsafe_extraction``), not on this input.

    When no prose was sourced (e.g. a Session whose CouchDB chunks were not
    materialized), it falls back to the canonical JSON body -- logged at WARNING
    (episode_id/entity_type only, never raw content) -- so a generic-only run is
    visible.
    """

    prose = (episode.extraction_text or "").strip()
    if prose:
        return prose
    _LOG.warning(
        "no extraction prose for episode; entity pass will run on JSON metadata "
        "(generic-only extraction regression) episode_id=%s entity_type=%s",
        episode.episode_id,
        episode.entity_type,
    )
    return json_body


def _reject_unsafe_extraction(results: Any) -> None:
    """Hard-fail when LLM-extracted entity text carries private/secret content.

    Scans EntityNode.name / EntityNode.summary and EntityEdge (RELATES_TO).fact
    on the add_episode results for PRIVATE_OUTPUT_RE / SECRET_ASSIGNMENT_RE. A
    match raises ValueError, which propagates as a failed upsert (projection
    records it as a failure, never a projected/duplicate row). The exception
    message names only the field kind, never the offending text, so the raw
    private/secret value is not re-emitted in a traceback or log.
    """

    if results is None:
        return
    for node in _safe_iter(getattr(results, "nodes", None)):
        _reject_field(getattr(node, "name", ""), "extracted EntityNode.name")
        _reject_field(getattr(node, "summary", ""), "extracted EntityNode.summary")
    for edge in _safe_iter(getattr(results, "edges", None)):
        _reject_field(getattr(edge, "fact", ""), "extracted RELATES_TO.fact")


async def _delete_extracted_graph_elements(driver: Any, results: Any) -> None:
    """Best-effort removal of the entities/edges add_episode just persisted.

    add_episode writes EntityNode/RELATES_TO to the graph BEFORE the write-time
    redaction gate runs, so an unsafe extraction would otherwise leave the
    private/secret text in the graph. When the gate rejects, delete those
    elements (DETACH DELETE on a node also drops its MENTIONS/RELATES_TO edges)
    so the rejection does not persist polluted state. Failures here are
    swallowed: the caller is already re-raising the rejection, and a cleanup
    error must not mask or replace it.
    """

    if results is None:
        return
    for edge in _safe_iter(getattr(results, "edges", None)):
        await _safe_delete_graph_element(edge, driver)
    for node in _safe_iter(getattr(results, "nodes", None)):
        await _safe_delete_graph_element(node, driver)


async def _safe_delete_graph_element(element: Any, driver: Any) -> None:
    delete = getattr(element, "delete", None)
    if not callable(delete):
        return
    try:
        await delete(driver)
    except Exception:
        # Best-effort: a delete failure must not mask the unsafe-extraction raise.
        pass


def _safe_iter(value: Any) -> list[Any]:
    if value is None:
        return []
    try:
        return list(value)
    except TypeError:
        return []


def _reject_field(value: Any, field: str) -> None:
    text = str(value or "")
    if PRIVATE_OUTPUT_RE.search(text) or SECRET_ASSIGNMENT_RE.search(text):
        # Name only the field kind; never echo the matched private/secret text.
        raise ValueError(f"{field} contains private or raw content")


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


# Providers that build Graphiti with our configured OpenAI-compatible client
# instead of Graphiti's built-in default OpenAI client. The documented default
# ``openai`` is included on purpose: routing it through the configured path means
# the episode-only operational default never constructs Graphiti's built-in
# OpenAI LLM/embedder client (which instantiates an OpenAI SDK client and would
# break the zero-LLM guarantee when no key is configured).
_CONFIGURED_LLM_CLIENT_PROVIDERS = frozenset(
    {"openai", "ollama", "openai-compatible", "openai_compatible"}
)
# Non-secret placeholder API key for providers that authenticate out of band
# (e.g. the vertex-wrapper backend uses ADC, so an empty configured key is
# intentional). Never a real credential; a genuinely required key surfaces as an
# auth error at LLM call time, not as a construction-time failure here.
_NON_SECRET_PLACEHOLDER_API_KEY = "not-needed"


def _uses_configured_llm_client(provider: str) -> bool:
    return str(provider or "").strip().lower() in _CONFIGURED_LLM_CLIENT_PROVIDERS


def _placeholder_api_key(provider: str) -> str:
    """Return the non-secret fallback API key for an empty configured key.

    Ollama keeps its conventional ``ollama`` placeholder. Every other configured
    provider (including ADC-backed openai-compatible endpoints such as the
    vertex-wrapper) gets an explicit non-secret placeholder so the client
    constructs cleanly when the key is intentionally empty.
    """

    if str(provider or "").strip().lower() == "ollama":
        return "ollama"
    return _NON_SECRET_PLACEHOLDER_API_KEY


def _resolve_embedding_dim(embedding_model: str, configured_dim: int) -> int:
    """Reconcile the embedding dimension with the resolved model's native one.

    Only steps in when ``configured_dim`` is still the generic module default and
    the model has a known native dimension, so an explicit ``LLM_BRAIN_EMBEDDING_DIM``
    override is always honored while the ollama/nomic-embed-text default stops
    mismatching its native 768-dim output.
    """

    native = _KNOWN_EMBEDDING_DIMS.get(str(embedding_model or "").strip().lower())
    if native is not None and configured_dim == _DEFAULT_EMBEDDING_DIM:
        return native
    return configured_dim


def _build_graphiti(config: GraphitiNeo4jConfig):
    from graphiti_core import Graphiti

    if _uses_configured_llm_client(config.llm_provider):
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
        from graphiti_core.llm_client.config import LLMConfig

        base_url = config.llm_base_url or ("http://localhost:11434/v1" if config.llm_provider == "ollama" else "")
        api_key = config.llm_api_key or _placeholder_api_key(config.llm_provider)
        llm_config = LLMConfig(
            api_key=api_key,
            model=config.llm_model or ("deepseek-r1:7b" if config.llm_provider == "ollama" else None),
            small_model=config.small_model or config.llm_model or ("deepseek-r1:7b" if config.llm_provider == "ollama" else None),
            base_url=base_url or None,
        )
        llm_client = _ReasoningOpenAIGenericClient(
            config=llm_config,
            reasoning_effort=config.llm_reasoning_effort,
        )
        embedding_model = config.embedding_model or (
            "nomic-embed-text" if config.llm_provider == "ollama" else "text-embedding-3-small"
        )
        embedder = OpenAIEmbedder(
            config=OpenAIEmbedderConfig(
                api_key=config.embedding_api_key or api_key,
                embedding_model=embedding_model,
                embedding_dim=_resolve_embedding_dim(embedding_model, config.embedding_dim),
                base_url=config.embedding_base_url or base_url or None,
            )
        )
        return Graphiti(
            config.uri,
            config.user,
            config.password,
            llm_client=llm_client,
            embedder=embedder,
            cross_encoder=OpenAIRerankerClient(client=llm_client.client, config=llm_config),
            store_raw_episode_content=config.store_raw_episode_content,
        )

    return Graphiti(
        config.uri,
        config.user,
        config.password,
        store_raw_episode_content=config.store_raw_episode_content,
    )


# Standard OpenAI chat roles. Any message whose role is outside this set is
# remapped to `user` so its content survives into the request.
_OPENAI_CHAT_ROLES = frozenset({"system", "user", "assistant", "tool", "developer", "function"})


class _ReasoningOpenAIGenericClient(OpenAIGenericClient):
    """OpenAI-compatible Graphiti client with optional per-request reasoning."""

    def __init__(self, *args: Any, reasoning_effort: str = "", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._reasoning_effort = reasoning_effort

    async def _generate_response(self, messages, response_model=None, max_tokens=None, model_size=None):
        import json as _json

        import openai
        from graphiti_core.llm_client.client import DEFAULT_MAX_TOKENS
        from graphiti_core.llm_client.errors import EmptyResponseError, RateLimitError
        from graphiti_core.llm_client.openai_generic_client import DEFAULT_MODEL

        max_tokens_value = DEFAULT_MAX_TOKENS if max_tokens is None else max_tokens
        openai_messages = []
        for message in messages:
            message.content = self._clean_input(message.content)
            # Map every standard OpenAI chat role through instead of silently
            # dropping non-user/system messages: an `assistant` turn (or `tool`/
            # `developer`) carries real conversation context, and omitting it
            # truncated the prompt the model actually saw. Unknown roles degrade
            # to `user` so the content is never lost.
            role = message.role if message.role in _OPENAI_CHAT_ROLES else "user"
            openai_messages.append({"role": role, "content": message.content})
        valid_duplicate_fact_idxs = _existing_fact_idx_values_from_messages(openai_messages)
        request = {
            "model": self.model or DEFAULT_MODEL,
            "messages": openai_messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens_value,
            "response_format": self._build_response_format(response_model),
        }
        if self._reasoning_effort:
            request["reasoning_effort"] = self._reasoning_effort
        try:
            response = await self.client.chat.completions.create(**request)
            result = response.choices[0].message.content or ""
            if not result:
                raise EmptyResponseError("LLM returned an empty response")
            data = _json.loads(self._strip_code_fences(result))
            return _normalize_structured_response(
                data,
                response_model,
                valid_duplicate_fact_idxs=valid_duplicate_fact_idxs,
            )
        except openai.RateLimitError as exc:
            raise RateLimitError from exc
        except Exception as exc:
            _LOG.error("Error in generating LLM response: %s", exc)
            raise


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


def _int_env(value: str, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _positive_int_env(value: str, *, default: int) -> int:
    parsed = _int_env(value, default=default)
    return parsed if parsed > 0 else default


def _float_env(value: str, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _llm_reasoning_effort_env(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return ""
    if normalized not in _ALLOWED_LLM_REASONING_EFFORTS:
        raise ValueError("LLM_BRAIN_LLM_REASONING_EFFORT must be one of high, medium, low, none")
    return normalized


def _reject_forbidden_gemini_llm_model(model: str) -> None:
    if "gemini" in str(model or "").strip().lower():
        raise ValueError(_GEMINI_LLM_FORBIDDEN_MESSAGE)


def _non_negative_float_env(value: str, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default
