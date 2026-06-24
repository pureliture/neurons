from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ._util import ensure_public_safe, hash_payload, public_safe_text, stable_json
from .graph import GraphMemoryAdapter, UpsertEpisodeResult
from .models import GraphMemoryResult, OntologyEpisode


METADATA_FIRST_HYBRID_GRAPH_SCHEMA = "llm_brain_metadata_first_hybrid_graph.v1"

_METADATA_EXACT_KEYS = {
    "artifact_id",
    "brain_id",
    "card_type",
    "content_hash",
    "currentness",
    "deleted_at",
    "device_id_hash",
    "document_kind",
    "event_id",
    "idempotency_key",
    "last_seen_at",
    "lifecycle_state",
    "memory_id",
    "permission_scope",
    "privacy_class",
    "project",
    "provider",
    "revoked_at",
    "root_id",
    "scope",
    "session_id_hash",
    "source_ref_id",
    "status",
    "sync_policy",
    "target_profile",
}
_METADATA_SUFFIXES = (
    "_at",
    "_count",
    "_hash",
    "_hashes",
    "_id",
    "_ids",
    "_ref",
    "_refs",
    "_version",
)
_TEXT_HINT_KEYS = (
    "task_state",
    "next_action",
    "blocker",
    "task",
    "decision",
    "rationale",
    "symptom",
    "attempt",
    "fix",
    "verification",
    "preference",
    "summary",
    "title",
    "status",
)


@dataclass(frozen=True)
class HybridTextMirrorHit:
    """text mirror 검색 결과의 episode join 후보.

    `episode_id`는 graph metadata episode에 다시 join할 키다. `score`는
    mirror backend가 산출한 상대 점수이고, `payload_hints`는 ContextPack
    복원을 위해 graph payload에 덧붙일 bounded public-safe 힌트다.
    """

    episode_id: str
    score: float = 0.0
    payload_hints: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ensure_public_safe(self.to_dict(), "HybridTextMirrorHit")

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "score": self.score,
            "payload_hints": dict(self.payload_hints),
        }


class HybridTextMirror(Protocol):
    """metadata-first graph 뒤에 붙는 검색용 text mirror 계약."""

    def upsert_episode_text(self, episode: OntologyEpisode, *, text: str) -> UpsertEpisodeResult:
        """episode의 public-safe 검색 text를 mirror에 idempotent upsert한다."""
        ...

    def search_episode_hits(
        self,
        *,
        brain_id: str,
        query: str,
        entity_types: list[str] | None = None,
        limit: int = 10,
    ) -> tuple[HybridTextMirrorHit, ...]:
        """brain/entity scope와 query로 episode join 후보를 반환한다."""
        ...


class InMemoryHybridTextMirror:
    """테스트와 local contract smoke용 deterministic text mirror."""

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Any]] = {}

    def upsert_episode_text(self, episode: OntologyEpisode, *, text: str) -> UpsertEpisodeResult:
        safe_text = _hybrid_search_text(episode, override=text)
        text_hash = hash_payload({"text": safe_text})
        prior = self._entries.get(episode.episode_id)
        if prior is not None:
            if prior["text_hash"] != text_hash:
                raise ValueError("episode text mirror collision with different text_hash")
            return "duplicate"
        self._entries[episode.episode_id] = {
            "episode_id": episode.episode_id,
            "brain_id": _episode_brain_id(episode),
            "entity_type": episode.entity_type,
            "observed_at": episode.observed_at,
            "text": safe_text,
            "text_hash": text_hash,
            "payload_hints": _payload_hints_for_episode(episode),
        }
        return "inserted"

    def search_episode_hits(
        self,
        *,
        brain_id: str,
        query: str,
        entity_types: list[str] | None = None,
        limit: int = 10,
    ) -> tuple[HybridTextMirrorHit, ...]:
        bounded = max(1, min(int(limit), 100))
        wanted = set(entity_types or [])
        terms = _terms(query)
        hits: list[tuple[dict[str, Any], int]] = []
        for entry in self._entries.values():
            if brain_id and entry["brain_id"] != brain_id:
                continue
            if wanted and entry["entity_type"] not in wanted:
                continue
            score = _match_score(entry["text"], terms)
            if terms and score <= 0:
                continue
            hits.append((entry, score))
        hits.sort(key=lambda item: (item[1], item[0]["observed_at"], item[0]["episode_id"]), reverse=True)
        return tuple(
            HybridTextMirrorHit(
                episode_id=str(entry["episode_id"]),
                score=float(score),
                payload_hints=dict(entry["payload_hints"]),
            )
            for entry, score in hits[:bounded]
        )


class MetadataFirstHybridGraphAdapter:
    """graph에는 metadata를 먼저 저장하고 recall에는 선택적 text mirror를 쓴다.

    감싼 graph adapter에는 opaque id, hash, lifecycle field, scope key만 남긴
    metadata-first ``OntologyEpisode``를 전달한다. Free text는
    ``HybridTextMirror``에 색인하고 read 결과에 public-safe hint로 다시 join한다.
    canonical authority는 두 파생 계층 밖에 남긴다.
    """

    def __init__(
        self,
        graph_adapter: GraphMemoryAdapter,
        *,
        text_mirror: HybridTextMirror | None = None,
        mirror_required: bool = False,
    ) -> None:
        self._graph_adapter = graph_adapter
        self._text_mirror = text_mirror
        self._mirror_required = bool(mirror_required)

    def upsert_episode(self, episode: OntologyEpisode) -> UpsertEpisodeResult:
        graph_result = self._graph_adapter.upsert_episode(metadata_first_episode(episode))
        if _is_failed_result(graph_result) or self._text_mirror is None:
            return graph_result
        try:
            mirror_result = self._text_mirror.upsert_episode_text(
                episode,
                text=_hybrid_search_text(episode),
            )
        except Exception:
            if self._mirror_required:
                return "failed"
            return graph_result
        if self._mirror_required and _is_failed_result(mirror_result):
            return "failed"
        return graph_result

    def search_context(
        self,
        *,
        brain_id: str,
        query: str,
        entity_types: list[str] | None = None,
        limit: int = 10,
    ) -> GraphMemoryResult:
        bounded = max(1, min(int(limit), 100))
        if self._text_mirror is None:
            result = self._graph_adapter.search_context(
                brain_id=brain_id,
                query=query,
                entity_types=entity_types,
                limit=bounded,
            )
            return _with_details(result, "metadata_first_graph_only")

        try:
            hits = self._text_mirror.search_episode_hits(
                brain_id=brain_id,
                query=query,
                entity_types=entity_types,
                limit=bounded,
            )
        except Exception as exc:
            fallback = self._graph_adapter.search_context(
                brain_id=brain_id,
                query=query,
                entity_types=entity_types,
                limit=bounded,
            )
            status = "degraded" if fallback.status == "available" else fallback.status
            return GraphMemoryResult(
                status=status,
                episodes=fallback.episodes,
                details=tuple([*fallback.details, f"text_mirror:{type(exc).__name__}", "metadata_first_hybrid_degraded"]),
            )

        if not hits:
            fallback = self._graph_adapter.search_context(
                brain_id=brain_id,
                query=query,
                entity_types=entity_types,
                limit=bounded,
            )
            return _with_details(fallback, "metadata_first_hybrid", "text_mirror_hits:0")

        episode_ids = tuple(hit.episode_id for hit in hits)
        metadata_episodes, metadata_status, metadata_details = _metadata_episodes_by_ids(
            self._graph_adapter,
            episode_ids,
            brain_id=brain_id,
            entity_types=entity_types,
            fallback_limit=max(bounded, min(100, max(len(hits) * 4, bounded * 4))),
        )
        by_id = {episode.episode_id: episode for episode in metadata_episodes}
        episodes: list[OntologyEpisode] = []
        missing = 0
        for hit in hits:
            episode = by_id.get(hit.episode_id)
            if episode is None:
                missing += 1
                continue
            episodes.append(_episode_with_hints(episode, hit.payload_hints))
        details = [
            *metadata_details,
            "metadata_first_hybrid",
            f"text_mirror_hits:{len(hits)}",
        ]
        if missing:
            details.append(f"metadata_join_missing:{missing}")
        status = metadata_status
        if missing:
            status = "degraded" if metadata_status == "available" else metadata_status
        return GraphMemoryResult(
            status=status,
            episodes=tuple(episodes[:bounded]),
            details=tuple(details),
        )

    def get_episodes_by_ids(
        self,
        episode_ids: list[str] | tuple[str, ...],
        *,
        brain_id: str = "",
        entity_types: list[str] | None = None,
    ) -> tuple[OntologyEpisode, ...]:
        return self._graph_adapter.get_episodes_by_ids(
            episode_ids,
            brain_id=brain_id,
            entity_types=entity_types,
        )


def metadata_first_episode(episode: OntologyEpisode) -> OntologyEpisode:
    metadata_payload = _metadata_payload(episode.payload)
    metadata_payload.update(
        {
            "metadata_first": True,
            "metadata_schema_version": METADATA_FIRST_HYBRID_GRAPH_SCHEMA,
            "source_payload_hash": hash_payload(episode.payload),
            "source_text_hash": hash_payload({"text": _hybrid_search_text(episode)}),
        }
    )
    metadata_payload["graph_payload_hash"] = hash_payload(metadata_payload)
    ensure_public_safe(metadata_payload, "metadata_first_payload")
    return OntologyEpisode(
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
        payload=metadata_payload,
        relations=_metadata_relations(episode.relations),
    )


def _metadata_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key, value in payload.items():
        key_text = str(key)
        if _is_metadata_key(key_text):
            metadata[key_text] = _metadata_value(value)
    if not metadata.get("brain_id") and payload.get("project"):
        metadata["brain_id"] = f"/project/{payload.get('project')}"
    return metadata


def _episode_brain_id(episode: OntologyEpisode) -> str:
    brain_id = str(episode.payload.get("brain_id") or "")
    if brain_id:
        return brain_id
    project = str(episode.payload.get("project") or "")
    return f"/project/{project}" if project else ""


def _metadata_episodes_by_ids(
    graph_adapter: GraphMemoryAdapter,
    episode_ids: tuple[str, ...],
    *,
    brain_id: str,
    entity_types: list[str] | None,
    fallback_limit: int,
) -> tuple[tuple[OntologyEpisode, ...], str, tuple[str, ...]]:
    getter = getattr(graph_adapter, "get_episodes_by_ids", None)
    if callable(getter):
        try:
            episodes = tuple(
                getter(
                    list(episode_ids),
                    brain_id=brain_id,
                    entity_types=entity_types,
                )
            )
            return episodes, "available", ("metadata_exact_join",)
        except Exception as exc:
            return (), "degraded", (f"metadata_exact_join:{type(exc).__name__}",)

    fallback = graph_adapter.search_context(
        brain_id=brain_id,
        query="",
        entity_types=entity_types,
        limit=fallback_limit,
    )
    return fallback.episodes, fallback.status, tuple([*fallback.details, "metadata_pool_join"])


def _metadata_relations(relations: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    safe_relations: list[dict[str, Any]] = []
    for relation in relations:
        metadata = _metadata_payload(relation)
        metadata["source_relation_hash"] = hash_payload(relation)
        safe_relations.append(metadata)
    return tuple(safe_relations)


def _metadata_value(value: Any) -> Any:
    if isinstance(value, str):
        return public_safe_text(value, max_chars=512)
    if isinstance(value, Mapping):
        return _metadata_payload(value)
    if isinstance(value, (list, tuple)):
        return [_metadata_value(item) for item in value if _is_metadata_scalar(item)]
    if _is_metadata_scalar(value):
        return value
    return public_safe_text(str(value), max_chars=256)


def _is_metadata_key(key: str) -> bool:
    return key in _METADATA_EXACT_KEYS or any(key.endswith(suffix) for suffix in _METADATA_SUFFIXES)


def _is_metadata_scalar(value: Any) -> bool:
    return isinstance(value, (str, bool, int, float)) or value is None


def _payload_hints_for_episode(episode: OntologyEpisode) -> dict[str, Any]:
    hints: dict[str, Any] = {}
    payload = episode.payload
    typed_payload = payload.get("typed_payload") if isinstance(payload.get("typed_payload"), Mapping) else {}
    for key in _TEXT_HINT_KEYS:
        if key in payload:
            hints[key] = _hint_value(payload.get(key))
        if key in typed_payload:
            hints[key] = _hint_value(typed_payload.get(key))
    if typed_payload:
        nested = {
            key: _hint_value(value)
            for key, value in typed_payload.items()
            if key in _TEXT_HINT_KEYS or _is_metadata_key(str(key))
        }
        if nested:
            hints["typed_payload"] = nested
    ensure_public_safe(hints, "hybrid_payload_hints")
    return hints


def _hint_value(value: Any) -> Any:
    if isinstance(value, str):
        return public_safe_text(value, max_chars=512)
    if isinstance(value, Mapping):
        return {str(key): _hint_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_hint_value(item) for item in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return public_safe_text(str(value), max_chars=512)


def _hybrid_search_text(episode: OntologyEpisode, *, override: str | None = None) -> str:
    if override is not None:
        text = public_safe_text(str(override), max_chars=8192)
        ensure_public_safe(text, "hybrid_search_text")
        return text
    parts: list[str] = [episode.entity_type, episode.natural_id]
    _collect_text(episode.payload, parts)
    for relation in episode.relations:
        _collect_text(relation, parts)
    text = public_safe_text(" ".join(parts), max_chars=8192)
    ensure_public_safe(text, "hybrid_search_text")
    return text


def _collect_text(value: Any, parts: list[str]) -> None:
    if isinstance(value, str):
        parts.append(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            _collect_text(item, parts)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_text(item, parts)


def _episode_with_hints(episode: OntologyEpisode, hints: Mapping[str, Any]) -> OntologyEpisode:
    payload = dict(episode.payload)
    for key, value in hints.items():
        payload.setdefault(str(key), value)
    payload["hybrid_text_mirror"] = {
        "status": "joined",
        "authority": "derived_text_mirror",
    }
    ensure_public_safe(payload, "metadata_first_augmented_payload")
    return OntologyEpisode(
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


def _with_details(result: GraphMemoryResult, *details: str) -> GraphMemoryResult:
    return GraphMemoryResult(
        status=result.status,
        episodes=result.episodes,
        details=tuple([*result.details, *details]),
    )


def _is_failed_result(result: str) -> bool:
    return str(result or "") in {"", "failed", "error", "unavailable"}


def _terms(value: Any) -> list[str]:
    return [term for term in re.split(r"[^a-zA-Z0-9_가-힣]+", str(value or "").lower()) if term]


def _match_score(value: Any, terms: list[str]) -> int:
    if not terms:
        return 1
    text = stable_json(value).lower()
    return sum(1 for term in terms if term in text)


__all__ = [
    "HybridTextMirror",
    "HybridTextMirrorHit",
    "InMemoryHybridTextMirror",
    "METADATA_FIRST_HYBRID_GRAPH_SCHEMA",
    "MetadataFirstHybridGraphAdapter",
    "metadata_first_episode",
]
