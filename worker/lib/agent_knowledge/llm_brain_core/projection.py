from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .graph import GraphMemoryAdapter
from .models import OntologyEpisode, SessionMemoryArtifact, SourceRefRecord
from .ontology import build_ontology_episode_batch_report
from .runtime import episode_from_memory_card


@dataclass(frozen=True)
class GraphProjectionReport:
    status: str
    attempted: int
    projected: int
    duplicates: int = 0
    skipped_disabled: int = 0
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

    def project_memory_cards(
        self, cards: list[dict[str, Any]], *, project: str = ""
    ) -> GraphProjectionReport:
        episodes = []
        failures: list[dict[str, Any]] = []
        for card in cards:
            try:
                episodes.append(episode_from_memory_card(card, project=project))
            except Exception as exc:
                failures.append(_failure(card, exc, phase="map"))
        report = self.project_episodes(episodes)
        merged_failures = tuple([*failures, *report.failures])
        failed = len(merged_failures)
        projected_or_duplicate = report.projected or report.duplicates
        status = "succeeded" if failed == 0 else ("partial" if projected_or_duplicate else "failed")
        return GraphProjectionReport(
            status=status,
            attempted=len(cards),
            projected=report.projected,
            duplicates=report.duplicates,
            skipped_disabled=report.skipped_disabled,
            failed=failed,
            episode_ids=report.episode_ids,
            failures=merged_failures,
            details=report.details,
        )

    def project_batch(
        self,
        *,
        artifacts: list[SessionMemoryArtifact] | None = None,
        memory_cards: list[dict[str, Any]] | None = None,
        source_refs: list[SourceRefRecord] | None = None,
        project: str = "",
    ) -> GraphProjectionReport:
        batch = build_ontology_episode_batch_report(
            artifacts=artifacts or [],
            memory_cards=memory_cards or [],
            source_refs=source_refs or [],
            project=project,
        )
        report = self.project_episodes(list(batch.episodes))
        failures = tuple([*batch.failures, *report.failures])
        failed = len(failures)
        projected_or_duplicate = report.projected or report.duplicates
        status = "succeeded" if failed == 0 else ("partial" if projected_or_duplicate else "failed")
        return GraphProjectionReport(
            status=status,
            attempted=len(artifacts or []) + len(memory_cards or []) + len(source_refs or []),
            projected=report.projected,
            duplicates=report.duplicates,
            skipped_disabled=report.skipped_disabled,
            failed=failed,
            episode_ids=report.episode_ids,
            failures=failures,
            details=tuple([*report.details, "ontology_batch_projection"]),
        )

    def project_episodes(self, episodes: list[OntologyEpisode]) -> GraphProjectionReport:
        projected = 0
        duplicates = 0
        skipped_disabled = 0
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
            result_text = str(result or "")
            if result_text == "skipped_disabled":
                # Graph intentionally disabled: a no-op, not a projection or a failure.
                skipped_disabled += 1
                continue
            if result_text == "duplicate":
                duplicates += 1
            elif result_text in {"", "unavailable", "failed", "error"}:
                failures.append(
                    {
                        "episode_id": episode.episode_id,
                        "entity_type": episode.entity_type,
                        "reason_code": result_text or "empty_result",
                    }
                )
                continue
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
            skipped_disabled=skipped_disabled,
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
