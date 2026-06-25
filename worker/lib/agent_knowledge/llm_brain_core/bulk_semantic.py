from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from ._util import PRIVATE_OUTPUT_RE, SECRET_ASSIGNMENT_RE, public_safe_text, short_hash
from .graphiti_adapter import (
    _AsyncLoopRunner,
    _episode_type_json,
    _graphiti_group_id,
    _group_id_for_episode,
    _llm_reasoning_effort_env,
    _parse_datetime,
    _reject_forbidden_gemini_llm_model,
)
from .models import OntologyEpisode

DEFAULT_BULK_SEMANTIC_MAX_SESSIONS_PER_CALL = 5
DEFAULT_BULK_SEMANTIC_MAX_SESSION_CHARS = 1600
DEFAULT_BULK_SEMANTIC_MAX_TOKENS = 4096
DEFAULT_BULK_SEMANTIC_TIMEOUT_SECONDS = 600
DEFAULT_BULK_SEMANTIC_ALLOW_EMPTY_SESSIONS = False
DEFAULT_BULK_SEMANTIC_EMBEDDINGS = True


@dataclass(frozen=True)
class BulkSemanticSessionInput:
    session_key: str
    episode: OntologyEpisode
    text: str


@dataclass(frozen=True)
class BulkSemanticEntity:
    name: str
    type: str = "Concept"
    summary: str = ""


@dataclass(frozen=True)
class BulkSemanticRelation:
    source: str
    target: str
    type: str = "related_to"
    fact: str = ""


@dataclass(frozen=True)
class BulkSemanticSessionResult:
    session_key: str
    entities: tuple[BulkSemanticEntity, ...] = ()
    relations: tuple[BulkSemanticRelation, ...] = ()


@dataclass(frozen=True)
class BulkSemanticExtractionResult:
    sessions: tuple[BulkSemanticSessionResult, ...]

    def by_session_key(self) -> dict[str, BulkSemanticSessionResult]:
        return {item.session_key: item for item in self.sessions}


@dataclass(frozen=True)
class BulkSemanticWriteReport:
    projected: int
    entities_written: int
    relations_written: int


class OpenAICompatibleBulkSemanticExtractor:
    """Batch semantic extractor using a single OpenAI-compatible chat call."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        reasoning_effort: str = "",
        timeout_seconds: int = DEFAULT_BULK_SEMANTIC_TIMEOUT_SECONDS,
        max_tokens: int = DEFAULT_BULK_SEMANTIC_MAX_TOKENS,
        post_fn: Callable[..., str] = None,
    ) -> None:
        _reject_forbidden_gemini_llm_model(model)
        if not base_url:
            raise ValueError("LLM_BRAIN_LLM_BASE_URL is required")
        if not model:
            raise ValueError("LLM_BRAIN_LLM_MODEL is required")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._reasoning_effort = _llm_reasoning_effort_env(reasoning_effort)
        self._timeout_seconds = max(1, int(timeout_seconds))
        self._max_tokens = max(1, int(max_tokens))
        self._post_fn = post_fn or _urllib_post

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "OpenAICompatibleBulkSemanticExtractor":
        env = environ or os.environ
        return cls(
            base_url=env.get("LLM_BRAIN_LLM_BASE_URL", env.get("OPENAI_BASE_URL", "")),
            model=env.get("LLM_BRAIN_LLM_MODEL", env.get("MODEL_NAME", "")),
            api_key=env.get("LLM_BRAIN_LLM_API_KEY", env.get("OPENAI_API_KEY", "")),
            reasoning_effort=env.get("LLM_BRAIN_LLM_REASONING_EFFORT", ""),
            timeout_seconds=_positive_int_env(
                env.get("LLM_BRAIN_BULK_SEMANTIC_TIMEOUT_SECONDS", ""),
                DEFAULT_BULK_SEMANTIC_TIMEOUT_SECONDS,
            ),
            max_tokens=_positive_int_env(
                env.get("LLM_BRAIN_BULK_SEMANTIC_MAX_TOKENS", ""),
                DEFAULT_BULK_SEMANTIC_MAX_TOKENS,
            ),
        )

    def extract(self, batch: list[BulkSemanticSessionInput]) -> BulkSemanticExtractionResult:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": _bulk_semantic_messages(batch),
            "temperature": 0,
            "max_tokens": self._max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "bulk_semantic_extraction",
                    "schema": _BULK_SEMANTIC_JSON_SCHEMA,
                },
            },
        }
        if self._reasoning_effort:
            body["reasoning_effort"] = self._reasoning_effort
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        raw = self._post_fn(
            self._base_url + "/chat/completions",
            headers=headers,
            body=json.dumps(body, ensure_ascii=True),
            timeout=self._timeout_seconds,
        )
        data = json.loads(raw)
        content = str(data["choices"][0]["message"]["content"] or "")
        if not content.strip():
            raise ValueError("bulk semantic extractor returned empty content")
        parsed = _loads_json_object(content)
        return parse_bulk_semantic_result(parsed)


class OpenAICompatibleEmbeddingBatcher:
    """Small OpenAI-compatible batch embedding client for deterministic graph writes."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: int = DEFAULT_BULK_SEMANTIC_TIMEOUT_SECONDS,
        post_fn: Callable[..., str] | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("LLM_BRAIN_EMBEDDING_BASE_URL is required")
        if not model:
            raise ValueError("LLM_BRAIN_EMBEDDING_MODEL is required")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout_seconds = max(1, int(timeout_seconds))
        self._post_fn = post_fn or _urllib_post

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "OpenAICompatibleEmbeddingBatcher | None":
        env = environ or os.environ
        if not _truthy_env(env.get("LLM_BRAIN_BULK_SEMANTIC_EMBEDDINGS", "true")):
            return None
        model = env.get("LLM_BRAIN_EMBEDDING_MODEL", env.get("EMBEDDING_MODEL", ""))
        base_url = env.get("LLM_BRAIN_EMBEDDING_BASE_URL", env.get("OPENAI_BASE_URL", ""))
        if not model or not base_url:
            return None
        return cls(
            base_url=base_url,
            model=model,
            api_key=env.get("LLM_BRAIN_EMBEDDING_API_KEY", env.get("OPENAI_API_KEY", "")),
            timeout_seconds=_positive_int_env(
                env.get("LLM_BRAIN_BULK_SEMANTIC_TIMEOUT_SECONDS", ""),
                DEFAULT_BULK_SEMANTIC_TIMEOUT_SECONDS,
            ),
        )

    def embed_many(self, texts: list[str]) -> list[list[float] | None]:
        if not texts:
            return []
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        raw = self._post_fn(
            self._base_url + "/embeddings",
            headers=headers,
            body=json.dumps({"model": self._model, "input": list(texts)}, ensure_ascii=True),
            timeout=self._timeout_seconds,
        )
        data = json.loads(raw)
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ValueError("embedding response missing data")
        by_index: dict[int, list[float]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                index = int(row.get("index"))
                embedding = [float(item) for item in row.get("embedding") or []]
            except (TypeError, ValueError):
                continue
            by_index[index] = embedding
        return [by_index.get(index) for index in range(len(texts))]


class DeterministicGraphitiSemanticWriter:
    """Persist extracted semantics through Graphiti-compatible nodes and edges."""

    def __init__(
        self,
        driver: Any,
        *,
        embedder: OpenAICompatibleEmbeddingBatcher | None = None,
        runner: _AsyncLoopRunner | None = None,
    ) -> None:
        self._driver = driver
        self._embedder = embedder
        self._runner = runner or _AsyncLoopRunner.get_instance()

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "DeterministicGraphitiSemanticWriter":
        env = environ or os.environ
        from graphiti_core.driver.neo4j_driver import Neo4jDriver

        return cls(
            Neo4jDriver(
                env.get("LLM_BRAIN_NEO4J_URI", env.get("NEO4J_URI", "bolt://localhost:7687")),
                env.get("LLM_BRAIN_NEO4J_USER", env.get("NEO4J_USER", "neo4j")),
                env.get("LLM_BRAIN_NEO4J_PASSWORD", env.get("NEO4J_PASSWORD", "")),
            ),
            embedder=OpenAICompatibleEmbeddingBatcher.from_env(env),
        )

    def write_batch(
        self,
        inputs: list[BulkSemanticSessionInput],
        extraction: BulkSemanticExtractionResult,
        *,
        allow_empty_sessions: bool = DEFAULT_BULK_SEMANTIC_ALLOW_EMPTY_SESSIONS,
    ) -> BulkSemanticWriteReport:
        result_by_key = extraction.by_session_key()
        normalized_results: dict[str, BulkSemanticSessionResult] = {}
        for item in inputs:
            result = result_by_key.get(item.session_key)
            if result is None:
                raise ValueError("bulk semantic result missing session")
            normalized_results[item.session_key] = normalize_session_result(
                result,
                allow_empty=allow_empty_sessions,
            )
        embeddings = self._embed_normalized_results(normalized_results)

        async def _call() -> BulkSemanticWriteReport:
            return await self._write_batch_async(
                inputs,
                normalized_results,
                embeddings,
            )

        return self._runner.run(_call)

    async def _write_batch_async(
        self,
        inputs: list[BulkSemanticSessionInput],
        normalized_results: dict[str, BulkSemanticSessionResult],
        embeddings: dict[tuple[str, str], list[float] | None],
    ) -> BulkSemanticWriteReport:
        from graphiti_core.edges import EntityEdge, EpisodicEdge
        from graphiti_core.nodes import EntityNode, EpisodicNode

        projected = 0
        entity_writes = 0
        relation_writes = 0
        for item in inputs:
            session_result = normalized_results.get(item.session_key)
            if session_result is None:
                raise ValueError("bulk semantic result missing session")
            episode = item.episode
            group_id = _graphiti_group_id(_group_id_for_episode(episode, ""))
            body = json.dumps(episode.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            reference_time = _parse_datetime(episode.reference_time)
            episode_node = EpisodicNode(
                uuid=episode.episode_id,
                name=episode.episode_id,
                group_id=group_id,
                labels=[],
                source=_episode_type_json(),
                content=body,
                source_description=f"llm_brain_core:{episode.entity_type}:{episode.natural_id}",
                created_at=datetime.now(timezone.utc),
                valid_at=reference_time,
            )
            await episode_node.save(self._driver)

            entity_uuid_by_name: dict[str, str] = {}
            for entity in session_result.entities:
                entity_uuid = _entity_uuid(group_id, entity)
                entity_uuid_by_name[_entity_key(entity.name)] = entity_uuid
                entity_node = EntityNode(
                    uuid=entity_uuid,
                    name=entity.name,
                    group_id=group_id,
                    labels=[],
                    summary=entity.summary,
                    name_embedding=embeddings.get(("entity", _entity_key(entity.name))),
                    created_at=datetime.now(timezone.utc),
                    attributes={
                        "semantic_type": entity.type,
                        "extraction_mode": "bulk_semantic",
                    },
                )
                if entity_node.name_embedding:
                    await entity_node.save(self._driver)
                else:
                    await _save_entity_node_without_embedding(self._driver, entity_node)
                await EpisodicEdge(
                    source_node_uuid=episode.episode_id,
                    target_node_uuid=entity_uuid,
                    uuid=_mentions_uuid(episode.episode_id, entity_uuid),
                    group_id=group_id,
                    created_at=datetime.now(timezone.utc),
                ).save(self._driver)
                entity_writes += 1

            for relation in session_result.relations:
                source_uuid = entity_uuid_by_name.get(_entity_key(relation.source))
                target_uuid = entity_uuid_by_name.get(_entity_key(relation.target))
                if not source_uuid or not target_uuid:
                    raise ValueError("bulk semantic relation endpoint missing entity")
                entity_edge = EntityEdge(
                    uuid=_relation_uuid(group_id, relation),
                    group_id=group_id,
                    source_node_uuid=source_uuid,
                    target_node_uuid=target_uuid,
                    name=relation.type,
                    fact=relation.fact,
                    fact_embedding=embeddings.get(("relation", _relation_key(relation))),
                    episodes=[episode.episode_id],
                    created_at=datetime.now(timezone.utc),
                    valid_at=reference_time,
                    reference_time=reference_time,
                    attributes={"extraction_mode": "bulk_semantic"},
                )
                if entity_edge.fact_embedding:
                    await entity_edge.save(self._driver)
                else:
                    await _save_entity_edge_without_embedding(self._driver, entity_edge)
                relation_writes += 1
            projected += 1
        return BulkSemanticWriteReport(
            projected=projected,
            entities_written=entity_writes,
            relations_written=relation_writes,
        )

    def _embed_normalized_results(
        self,
        normalized_results: dict[str, BulkSemanticSessionResult],
    ) -> dict[tuple[str, str], list[float] | None]:
        if self._embedder is None:
            return {}
        ordered: list[tuple[tuple[str, str], str]] = []
        seen: set[tuple[str, str]] = set()
        for result in normalized_results.values():
            for entity in result.entities:
                key = ("entity", _entity_key(entity.name))
                if key not in seen:
                    seen.add(key)
                    ordered.append((key, entity.name))
            for relation in result.relations:
                key = ("relation", _relation_key(relation))
                if key not in seen:
                    seen.add(key)
                    ordered.append((key, relation.fact))
        vectors = self._embedder.embed_many([text for _key, text in ordered])
        return {
            key: vector
            for (key, _text), vector in zip(ordered, vectors, strict=False)
            if vector
        }


def make_bulk_session_input(
    *,
    session_key: str,
    episode: OntologyEpisode,
    max_chars: int = DEFAULT_BULK_SEMANTIC_MAX_SESSION_CHARS,
) -> BulkSemanticSessionInput:
    text = compact_extraction_text(episode.extraction_text or "", max_chars=max_chars)
    if not text:
        text = compact_extraction_text(str(episode.payload.get("summary") or ""), max_chars=max_chars)
    return BulkSemanticSessionInput(session_key=session_key, episode=episode, text=text)


def compact_extraction_text(text: str, *, max_chars: int = DEFAULT_BULK_SEMANTIC_MAX_SESSION_CHARS) -> str:
    bounded = max(200, int(max_chars))
    normalized = "\n".join(line.strip() for line in str(text or "").splitlines() if line.strip())
    if len(normalized) <= bounded:
        return normalized
    marker = "\n[...]\n"
    half = max(1, (bounded - len(marker)) // 2)
    return normalized[:half].rstrip() + marker + normalized[-half:].lstrip()


def parse_bulk_semantic_result(value: Any) -> BulkSemanticExtractionResult:
    if not isinstance(value, dict):
        raise ValueError("bulk semantic response must be an object")
    sessions_raw = value.get("sessions")
    if not isinstance(sessions_raw, list):
        raise ValueError("bulk semantic response must contain sessions")
    sessions: list[BulkSemanticSessionResult] = []
    for raw_session in sessions_raw:
        if not isinstance(raw_session, dict):
            raise ValueError("bulk semantic session must be an object")
        session_key = _required_text(raw_session.get("session_key"), "session_key", max_chars=80)
        entities = tuple(_parse_entity(item) for item in _list_or_empty(raw_session.get("entities")))
        relations = tuple(_parse_relation(item) for item in _list_or_empty(raw_session.get("relations")))
        sessions.append(
            BulkSemanticSessionResult(
                session_key=session_key,
                entities=entities,
                relations=relations,
            )
        )
    return BulkSemanticExtractionResult(tuple(sessions))


def normalize_session_result(
    result: BulkSemanticSessionResult,
    *,
    allow_empty: bool = DEFAULT_BULK_SEMANTIC_ALLOW_EMPTY_SESSIONS,
) -> BulkSemanticSessionResult:
    entities_by_key: dict[str, BulkSemanticEntity] = {
        _entity_key(entity.name): entity
        for entity in result.entities
    }
    normalized_relations: list[BulkSemanticRelation] = []
    for relation in result.relations:
        for name in (relation.source, relation.target):
            key = _entity_key(name)
            if key not in entities_by_key:
                entities_by_key[key] = BulkSemanticEntity(name=name, type="Concept", summary="")
        normalized_relations.append(relation)
    if not allow_empty and not entities_by_key:
        raise ValueError("bulk semantic session produced no entities")
    return BulkSemanticSessionResult(
        session_key=result.session_key,
        entities=tuple(entities_by_key.values()),
        relations=tuple(normalized_relations),
    )


def _bulk_semantic_messages(batch: list[BulkSemanticSessionInput]) -> list[dict[str, str]]:
    sessions = [
        {
            "session_key": item.session_key,
            "provider": str(item.episode.payload.get("provider") or ""),
            "project_ref": "sha256:" + short_hash(str(item.episode.payload.get("project") or "")),
            "text": item.text,
        }
        for item in batch
    ]
    return [
        {
            "role": "system",
            "content": (
                "Extract a compact semantic graph from each session. "
                "Return only one minified JSON object. The first character must be { and the last must be }. "
                "Every input session must appear exactly once in sessions and must contain at least one entity. "
                "Entities should be concrete domain concepts, tools, systems, files, tasks, "
                "decisions, models, services, or incidents. Avoid generic entities such as user, "
                "assistant, session, conversation, or project unless they are the actual subject. "
                "Relations must connect extracted entities and include a short factual statement."
            ),
        },
        {
            "role": "user",
            "content": json.dumps({"sessions": sessions}, ensure_ascii=True, sort_keys=True),
        },
    ]


def _parse_entity(value: Any) -> BulkSemanticEntity:
    if not isinstance(value, dict):
        raise ValueError("bulk semantic entity must be an object")
    name = _required_text(
        value.get("name")
        or value.get("entity_name")
        or value.get("entity")
        or value.get("entity_text"),
        "entity.name",
        max_chars=200,
    )
    entity_type = _safe_text(value.get("type") or value.get("entity_type") or "Concept", max_chars=80)
    summary = _safe_text(value.get("summary") or value.get("description") or "", max_chars=500)
    return BulkSemanticEntity(name=name, type=entity_type or "Concept", summary=summary)


def _parse_relation(value: Any) -> BulkSemanticRelation:
    if not isinstance(value, dict):
        raise ValueError("bulk semantic relation must be an object")
    source = _required_text(
        value.get("source") or value.get("source_entity") or value.get("source_entity_name"),
        "relation.source",
        max_chars=200,
    )
    target = _required_text(
        value.get("target") or value.get("target_entity") or value.get("target_entity_name"),
        "relation.target",
        max_chars=200,
    )
    relation_type = _safe_text(value.get("type") or value.get("relation_type") or "related_to", max_chars=80)
    fact = _required_text(value.get("fact") or value.get("summary") or value.get("description"), "relation.fact")
    return BulkSemanticRelation(
        source=source,
        target=target,
        type=relation_type or "related_to",
        fact=fact,
    )


def _safe_text(value: Any, *, max_chars: int) -> str:
    raw = str(value or "")
    if PRIVATE_OUTPUT_RE.search(raw) or SECRET_ASSIGNMENT_RE.search(raw):
        raise ValueError("bulk semantic output contains private or raw content")
    text = public_safe_text(raw, max_chars=max_chars)
    if PRIVATE_OUTPUT_RE.search(text) or SECRET_ASSIGNMENT_RE.search(text):
        raise ValueError("bulk semantic output contains private or raw content")
    return text


def _required_text(value: Any, field: str, *, max_chars: int = 500) -> str:
    text = _safe_text(value, max_chars=max_chars).strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _list_or_empty(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _entity_key(name: str) -> str:
    return " ".join(str(name or "").lower().split())


def _entity_uuid(group_id: str, entity: BulkSemanticEntity) -> str:
    return f"entity:{short_hash([group_id, _entity_key(entity.name), entity.type.lower()])}"


def _mentions_uuid(episode_id: str, entity_uuid: str) -> str:
    return f"mentions:{short_hash([episode_id, entity_uuid])}"


def _relation_uuid(group_id: str, relation: BulkSemanticRelation) -> str:
    return f"rel:{short_hash([group_id, relation.source.lower(), relation.target.lower(), relation.fact.lower()])}"


def _relation_key(relation: BulkSemanticRelation) -> str:
    return "|".join(
        [
            _entity_key(relation.source),
            _entity_key(relation.target),
            " ".join(relation.fact.lower().split()),
        ]
    )


async def _save_entity_node_without_embedding(driver: Any, node: Any) -> None:
    data = {
        "uuid": node.uuid,
        "name": node.name,
        "group_id": node.group_id,
        "summary": node.summary,
        "created_at": node.created_at,
        **dict(node.attributes or {}),
    }
    await driver.execute_query(
        """
        MERGE (n:Entity {uuid: $entity_data.uuid})
        SET n:Entity
        SET n += $entity_data
        RETURN n.uuid AS uuid
        """,
        entity_data=data,
    )


async def _save_entity_edge_without_embedding(driver: Any, edge: Any) -> None:
    data = {
        "uuid": edge.uuid,
        "source_uuid": edge.source_node_uuid,
        "target_uuid": edge.target_node_uuid,
        "name": edge.name,
        "fact": edge.fact,
        "group_id": edge.group_id,
        "episodes": edge.episodes,
        "created_at": edge.created_at,
        "expired_at": edge.expired_at,
        "valid_at": edge.valid_at,
        "invalid_at": edge.invalid_at,
        "reference_time": edge.reference_time,
        **dict(edge.attributes or {}),
    }
    await driver.execute_query(
        """
        MATCH (source:Entity {uuid: $edge_data.source_uuid})
        MATCH (target:Entity {uuid: $edge_data.target_uuid})
        MERGE (source)-[e:RELATES_TO {uuid: $edge_data.uuid}]->(target)
        SET e += $edge_data
        RETURN e.uuid AS uuid
        """,
        edge_data=data,
    )


def _strip_code_fences(value: str) -> str:
    text = str(value or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _loads_json_object(value: str) -> Any:
    text = _strip_code_fences(value)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _urllib_post(url: str, *, headers: dict, body: str, timeout: int) -> str:
    request = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def _positive_int_env(value: str, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _truthy_env(value: str) -> bool:
    return str(value or "").strip().lower() not in {"", "0", "false", "no", "off"}


_BULK_SEMANTIC_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sessions"],
    "properties": {
        "sessions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["session_key", "entities", "relations"],
                "properties": {
                    "session_key": {"type": "string"},
                    "entities": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["name", "type", "summary"],
                            "properties": {
                                "name": {"type": "string"},
                                "type": {"type": "string"},
                                "summary": {"type": "string"},
                            },
                        },
                    },
                    "relations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["source", "target", "type", "fact"],
                            "properties": {
                                "source": {"type": "string"},
                                "target": {"type": "string"},
                                "type": {"type": "string"},
                                "fact": {"type": "string"},
                            },
                        },
                    },
                },
            },
        }
    },
}
