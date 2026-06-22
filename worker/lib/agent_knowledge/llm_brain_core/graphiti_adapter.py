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

from ._util import (
    PRIVATE_OUTPUT_RE,
    SECRET_ASSIGNMENT_RE,
    ensure_public_safe,
    public_safe_text,
    short_hash,
)
from .graph import UpsertEpisodeResult
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
    embedding_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = field(default="", repr=False)
    embedding_dim: int = 1024
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
            force_reextract_entities=env.get("LLM_BRAIN_GRAPH_FORCE_REEXTRACT_ENTITIES", "false").lower()
            in {"1", "true", "yes"},
            fallback_llm_model=env.get(
                "LLM_BRAIN_LLM_FALLBACK_MODEL",
                env.get("LLM_BRAIN_GRAPH_FALLBACK_LLM_MODEL", ""),
            ),
            fallback_small_model=env.get(
                "LLM_BRAIN_SMALL_LLM_FALLBACK_MODEL",
                env.get("LLM_BRAIN_GRAPH_FALLBACK_SMALL_LLM_MODEL", ""),
            ),
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
        extraction_body = _extraction_body_for(episode, body)
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
            _reject_unsafe_extraction(results)
            return "inserted"

        return self._runner.run(_call, timeout=self._write_timeout_seconds)

    async def _add_episode_with_fallback(self, **kwargs: Any) -> Any:
        last_error: Exception | None = None
        for _ in range(self._primary_attempts):
            try:
                return await _add_episode_with_attempt_timeout(
                    self._graphiti,
                    self._primary_attempt_timeout_seconds,
                    **kwargs,
                )
            except Exception as exc:
                last_error = exc
        if self._fallback_graphiti is not None:
            for _ in range(self._fallback_attempts):
                try:
                    return await _add_episode_with_attempt_timeout(
                        self._fallback_graphiti,
                        self._fallback_attempt_timeout_seconds,
                        **kwargs,
                    )
                except Exception as exc:
                    last_error = exc
        if last_error is not None:
            raise last_error

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


# gemini-3.5-flash-thinking, served through the vertex-wrapper under graphiti's
# NON-strict json_schema response_format (openai_generic_client deliberately omits
# "strict": true because raw model_json_schema() violates OpenAI's strict subset),
# does not bind to the exact schema property names. It emits each extracted-entity
# item key as ``entity_name`` instead of graphiti's required ``name`` field, so
# ExtractedEntities.model_validate() fails for EVERY item ("Field required ... name")
# and the entity pass yields 0 entities -- the live 0/3-entity stall. Normalize the
# known deviation back to the schema field name before graphiti validates. Keyed on
# the EXACT key ``entity_name`` so the edge model's ``source_entity_name`` /
# ``target_entity_name`` (different keys) are untouched, and an already-correct
# ``name`` is never clobbered.
_STRUCTURED_KEY_ALIASES = {
    "entity_name": "name",
    "entity": "name",
    "entity_value": "name",
    "entity_text": "name",
}


def _normalize_structured_keys(value: Any) -> Any:
    """Recursively rename known gemini structured-output key aliases to graphiti's.

    Pure transform over the parsed JSON (dict / list / scalar). For each dict it
    renames an alias key to its canonical name only when the canonical key is not
    already present, so a correct field is never overwritten. Extra keys gemini
    adds (e.g. ``entity_type_name``) are left intact; pydantic ignores them.
    """

    if isinstance(value, list):
        return [_normalize_structured_keys(item) for item in value]
    if isinstance(value, dict):
        result = {key: _normalize_structured_keys(val) for key, val in value.items()}
        for alias, canonical in _STRUCTURED_KEY_ALIASES.items():
            if alias in result and canonical not in result:
                result[canonical] = result.pop(alias)
        return result
    return value


def _normalize_structured_response(value: Any, response_model: Any = None) -> Any:
    normalized = _normalize_structured_keys(value)
    if response_model is None:
        return normalized
    fields = getattr(response_model, "model_fields", {}) or {}
    if isinstance(normalized, list):
        list_field_names = [
            name for name, field in fields.items()
            if str(getattr(field, "annotation", "")).startswith("list[")
        ]
        if len(list_field_names) == 1:
            normalized = {list_field_names[0]: normalized}
    if isinstance(normalized, dict):
        return _normalize_response_model_payload(normalized, fields)
    return normalized


def _normalize_response_model_payload(payload: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    for field_name in fields:
        items = result.get(field_name)
        if not isinstance(items, list):
            continue
        normalized_items = []
        for item in items:
            if isinstance(item, dict):
                normalized_items.append(_normalize_response_model_item(item, field_name))
            else:
                normalized_items.append(item)
        result[field_name] = normalized_items
    return result


def _normalize_response_model_item(item: dict[str, Any], field_name: str) -> dict[str, Any]:
    result = dict(item)
    if field_name == "extracted_entities" and "name" in result and "entity_type_id" not in result:
        result["entity_type_id"] = 0
    if isinstance(result.get("episode_indices"), list):
        indices: list[int] = []
        for value in result["episode_indices"]:
            try:
                indices.append(int(value))
            except (TypeError, ValueError):
                indices.append(0)
        result["episode_indices"] = indices
    return result


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
        class _NeuronStructuredClient(OpenAIGenericClient):
            """OpenAIGenericClient that repairs gemini-via-wrapper structured-output
            field-name deviations before graphiti's pydantic validation.

            gemini emits ``entity_name`` where graphiti's ExtractedEntities requires
            ``name`` (non-strict json_schema, see _normalize_structured_keys), which
            otherwise drops every extracted entity. We normalize the parsed dict in
            the single chokepoint both the direct and retry paths funnel through.
            """

            async def _generate_response(self, *args, **kwargs):
                response_model = kwargs.get("response_model")
                if response_model is None and len(args) >= 2:
                    response_model = args[1]
                data = await super()._generate_response(*args, **kwargs)
                return _normalize_structured_response(data, response_model)

        llm_client = _NeuronStructuredClient(config=llm_config)
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


def _non_negative_float_env(value: str, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default
