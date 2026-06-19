from __future__ import annotations

from typing import Literal, Protocol

from .models import GraphMemoryResult, OntologyEpisode


# Typed upsert outcomes. `skipped_disabled` separates a graph-disabled
# NullAdapter no-op from real `failed` writes so projection exit codes do not
# treat "graph off" as a failure.
UpsertEpisodeResult = Literal["inserted", "duplicate", "skipped_disabled", "failed"]


class GraphMemoryAdapter(Protocol):
    def upsert_episode(self, episode: OntologyEpisode) -> UpsertEpisodeResult: ...

    def search_context(
        self,
        *,
        brain_id: str,
        query: str,
        entity_types: list[str] | None = None,
        limit: int = 10,
    ) -> GraphMemoryResult: ...


class NullGraphMemoryAdapter:
    def upsert_episode(self, episode: OntologyEpisode) -> UpsertEpisodeResult:
        # Graph is intentionally disabled: this is a no-op skip, not a failure.
        return "skipped_disabled"

    def search_context(
        self,
        *,
        brain_id: str,
        query: str,
        entity_types: list[str] | None = None,
        limit: int = 10,
    ) -> GraphMemoryResult:
        return GraphMemoryResult(status="unavailable", details=("graph_disabled",))


class UnavailableGraphMemoryAdapter:
    """Graph adapter placeholder that preserves read-path safety with diagnostics."""

    def __init__(self, reason: str) -> None:
        self._reason = str(reason or "graph_unavailable")

    def upsert_episode(self, episode: OntologyEpisode) -> UpsertEpisodeResult:
        # Graph was requested but the backend could not be initialized: real failure.
        return "failed"

    def search_context(
        self,
        *,
        brain_id: str,
        query: str,
        entity_types: list[str] | None = None,
        limit: int = 10,
    ) -> GraphMemoryResult:
        return GraphMemoryResult(status="error", details=(self._reason,))


class FakeGraphMemoryAdapter:
    """Small in-memory adapter for contract and context tests.

    This intentionally mirrors the production-default Graphiti behavior so tests
    catch grouping regressions without a live Neo4j:

    - `extract_entities=False` path stores episodes keyed on `episode_id` with a
      MERGE (re-upserting the same `episode_id` is a `duplicate`, not a second row).
    - reads are scoped by a derived `group_id` (from the episode `brain_id`
      payload), the same way Graphiti filters by `group_ids`. A `search_context`
      with a `brain_id` that maps to a different group never returns the episode.
    """

    def __init__(
        self,
        episodes: list[OntologyEpisode] | None = None,
        *,
        default_group_id: str = "",
    ) -> None:
        self._episodes: dict[str, OntologyEpisode] = {}
        self._group_ids: dict[str, str] = {}
        self._default_group_id = str(default_group_id or "")
        for episode in episodes or []:
            self.upsert_episode(episode)

    def upsert_episode(self, episode: OntologyEpisode) -> UpsertEpisodeResult:
        prior = self._episodes.get(episode.episode_id)
        if prior is not None:
            if prior.content_hash != episode.content_hash:
                raise ValueError("episode id collision with different content_hash")
            return "duplicate"
        self._episodes[episode.episode_id] = episode
        self._group_ids[episode.episode_id] = _group_id_for_episode(episode, self._default_group_id)
        return "inserted"

    def search_context(
        self,
        *,
        brain_id: str,
        query: str,
        entity_types: list[str] | None = None,
        limit: int = 10,
    ) -> GraphMemoryResult:
        bounded = max(1, min(int(limit), 100))
        wanted = set(entity_types or [])
        query_terms = [term for term in str(query or "").lower().split() if term]
        target_group = _normalize_group_id(brain_id or self._default_group_id)
        matches: list[OntologyEpisode] = []
        related_ids: set[str] = set()
        for episode in self._episodes.values():
            if self._episode_group(episode) != target_group:
                continue
            if wanted and episode.entity_type not in wanted:
                continue
            text = episode.search_text()
            if not query_terms or any(term in text for term in query_terms):
                matches.append(episode)
                related_id = _related_incident_id(episode)
                if related_id:
                    related_ids.add(related_id)
        if related_ids:
            seen = {episode.episode_id for episode in matches}
            for episode in self._episodes.values():
                if self._episode_group(episode) != target_group:
                    continue
                if episode.episode_id in seen:
                    continue
                if wanted and episode.entity_type not in wanted:
                    continue
                if _related_incident_id(episode) in related_ids:
                    matches.append(episode)
                    seen.add(episode.episode_id)
        matches.sort(key=lambda episode: (episode.observed_at, episode.episode_id), reverse=True)
        return GraphMemoryResult(status="available", episodes=tuple(matches[:bounded]))

    def _episode_group(self, episode: OntologyEpisode) -> str:
        stored = self._group_ids.get(episode.episode_id)
        if stored is None:
            stored = _group_id_for_episode(episode, self._default_group_id)
        return _normalize_group_id(stored)


def _related_incident_id(episode: OntologyEpisode) -> str:
    value = episode.payload.get("incident_id") or episode.payload.get("target_incident_id")
    return str(value or "")


def _episode_brain_id(episode: OntologyEpisode) -> str:
    return str(episode.payload.get("brain_id") or "")


def _group_id_for_episode(episode: OntologyEpisode, default_group_id: str) -> str:
    return _episode_brain_id(episode) or str(default_group_id or "")


def _normalize_group_id(value: str) -> str:
    return str(value or "")
