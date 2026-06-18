from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .graph import GraphMemoryAdapter
from .models import OntologyEpisode
from .runtime import episode_from_memory_card


@dataclass(frozen=True)
class GraphProjectionReport:
    status: str
    attempted: int
    projected: int
    duplicates: int = 0
    failed: int = 0
    episode_ids: tuple[str, ...] = ()
    failures: tuple[dict[str, Any], ...] = ()
    details: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["episode_ids"] = list(self.episode_ids)
        data["failures"] = [dict(item) for item in self.failures]
        data["details"] = list(self.details)
        return data


class GraphProjectionWorker:
    """Project canonical MemoryCards into a derived graph adapter."""

    def __init__(self, graph_adapter: GraphMemoryAdapter) -> None:
        self._graph_adapter = graph_adapter

    def project_memory_cards(self, cards: list[dict[str, Any]]) -> GraphProjectionReport:
        episodes = []
        failures: list[dict[str, Any]] = []
        for card in cards:
            try:
                episodes.append(episode_from_memory_card(card))
            except Exception as exc:
                failures.append(_failure(card, exc, phase="map"))
        report = self.project_episodes(episodes)
        merged_failures = tuple([*failures, *report.failures])
        failed = len(merged_failures)
        status = "succeeded" if failed == 0 else ("partial" if report.projected else "failed")
        return GraphProjectionReport(
            status=status,
            attempted=len(cards),
            projected=report.projected,
            duplicates=report.duplicates,
            failed=failed,
            episode_ids=report.episode_ids,
            failures=merged_failures,
            details=report.details,
        )

    def project_episodes(self, episodes: list[OntologyEpisode]) -> GraphProjectionReport:
        projected = 0
        duplicates = 0
        failures: list[dict[str, Any]] = []
        episode_ids: list[str] = []
        for episode in episodes:
            try:
                result = self._graph_adapter.upsert_episode(episode)
            except Exception as exc:
                failures.append(
                    {
                        "episode_id": episode.episode_id,
                        "entity_type": episode.entity_type,
                        "reason_code": type(exc).__name__,
                    }
                )
                continue
            if result == "duplicate":
                duplicates += 1
            else:
                projected += 1
            episode_ids.append(episode.episode_id)
        failed = len(failures)
        status = "succeeded" if failed == 0 else ("partial" if projected or duplicates else "failed")
        return GraphProjectionReport(
            status=status,
            attempted=len(episodes),
            projected=projected,
            duplicates=duplicates,
            failed=failed,
            episode_ids=tuple(episode_ids),
            failures=tuple(failures),
            details=("derived_graph_projection",),
        )


def _failure(card: dict[str, Any], exc: Exception, *, phase: str) -> dict[str, Any]:
    return {
        "memory_id": str(card.get("memory_id") or ""),
        "card_type": str(card.get("card_type") or ""),
        "phase": phase,
        "reason_code": type(exc).__name__,
    }
