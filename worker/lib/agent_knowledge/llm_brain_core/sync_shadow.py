from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .event_replay import BrainEventReplayStore
from .graph import GraphMemoryAdapter
from .models import BrainEventEnvelope, OntologyEpisode
from .projection import GraphProjectionReport, GraphProjectionWorker
from .runtime import episode_from_memory_card


@dataclass(frozen=True)
class CentralShadowRebuildReport:
    status: str
    replay: dict[str, Any]
    projection: dict[str, Any]
    episode_ids: tuple[str, ...]
    conversion_failures: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["episode_ids"] = list(self.episode_ids)
        data["conversion_failures"] = [dict(item) for item in self.conversion_failures]
        return data


class CentralBrainShadowRebuilder:
    """Deterministically rebuild central derived graph state from BrainEvents."""

    def __init__(self, graph_adapter: GraphMemoryAdapter) -> None:
        self._projection = GraphProjectionWorker(graph_adapter)

    def rebuild(self, events: list[BrainEventEnvelope]) -> CentralShadowRebuildReport:
        replay_report = BrainEventReplayStore().apply(events).to_dict()
        episodes: list[OntologyEpisode] = []
        failures: list[dict[str, Any]] = []
        for payload in replay_report["current_payloads"]:
            try:
                episodes.append(_episode_from_current_payload(payload))
            except Exception as exc:
                failures.append(
                    {
                        "target_id": str(payload.get("target_id") or payload.get("memory_id") or payload.get("episode_id") or ""),
                        "kind": str(payload.get("kind") or ""),
                        "reason_code": type(exc).__name__,
                    }
                )
        projection = self._projection.project_episodes(episodes)
        status = _status(replay_report, projection, failures)
        return CentralShadowRebuildReport(
            status=status,
            replay=replay_report,
            projection=projection.to_dict(),
            episode_ids=tuple(episode.episode_id for episode in episodes),
            conversion_failures=tuple(failures),
        )


def _episode_from_current_payload(payload: dict[str, Any]) -> OntologyEpisode:
    if isinstance(payload.get("ontology_episode"), dict):
        return _episode_from_dict(dict(payload["ontology_episode"]))
    if "episode_id" in payload and "entity_type" in payload and "content_hash" in payload:
        return _episode_from_dict(payload)
    if "memory_id" in payload and "card_type" in payload:
        return episode_from_memory_card(payload)
    if "source_ref_id" in payload:
        return OntologyEpisode.from_payload(
            event_id=str(payload.get("event_id") or f"evt_source_ref_{payload['source_ref_id']}"),
            entity_type="SourceRef",
            natural_id=str(payload["source_ref_id"]),
            payload={
                "source_ref_id": str(payload["source_ref_id"]),
                "device_id_hash": str(payload.get("device_id_hash") or ""),
                "root_id": str(payload.get("root_id") or ""),
                "sync_policy": str(payload.get("sync_policy") or ""),
                "content_hash": str(payload.get("content_hash") or ""),
            },
            source_ref_ids=[str(payload["source_ref_id"])],
            observed_at=str(payload.get("observed_at") or ""),
            reference_time=str(payload.get("occurred_at") or payload.get("observed_at") or ""),
        )
    raise ValueError("unsupported current payload for central shadow rebuild")


def _episode_from_dict(parsed: dict[str, Any]) -> OntologyEpisode:
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


def _status(
    replay_report: dict[str, Any],
    projection: GraphProjectionReport,
    conversion_failures: list[dict[str, Any]],
) -> str:
    if replay_report.get("quarantined") or conversion_failures or projection.failed:
        if projection.projected or projection.duplicates:
            return "partial"
        return "quarantined"
    return "succeeded"
