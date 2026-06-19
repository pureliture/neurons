from __future__ import annotations

from typing import Protocol

from .models import GraphMemoryResult, OntologyEpisode


class GraphMemoryAdapter(Protocol):
    def upsert_episode(self, episode: OntologyEpisode) -> str: ...

    def search_context(
        self,
        *,
        brain_id: str,
        query: str,
        entity_types: list[str] | None = None,
        limit: int = 10,
    ) -> GraphMemoryResult: ...


class NullGraphMemoryAdapter:
    def upsert_episode(self, episode: OntologyEpisode) -> str:
        return "unavailable"

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

    def upsert_episode(self, episode: OntologyEpisode) -> str:
        return "error"

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
    """Small in-memory adapter for contract and context tests."""

    def __init__(self, episodes: list[OntologyEpisode] | None = None) -> None:
        self._episodes: dict[str, OntologyEpisode] = {}
        for episode in episodes or []:
            self.upsert_episode(episode)

    def upsert_episode(self, episode: OntologyEpisode) -> str:
        prior = self._episodes.get(episode.episode_id)
        if prior is not None:
            if prior.content_hash != episode.content_hash:
                raise ValueError("episode id collision with different content_hash")
            return "duplicate"
        self._episodes[episode.episode_id] = episode
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
        matches: list[OntologyEpisode] = []
        related_ids: set[str] = set()
        for episode in self._episodes.values():
            if _episode_brain_id(episode) != brain_id:
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
                if _episode_brain_id(episode) != brain_id:
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


def _related_incident_id(episode: OntologyEpisode) -> str:
    value = episode.payload.get("incident_id") or episode.payload.get("target_incident_id")
    return str(value or "")


def _episode_brain_id(episode: OntologyEpisode) -> str:
    return str(episode.payload.get("brain_id") or "")
