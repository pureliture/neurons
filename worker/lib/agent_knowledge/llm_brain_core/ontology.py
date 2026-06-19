from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from ._util import public_safe_text
from .models import OntologyEpisode, SessionMemoryArtifact, SourceRefRecord
from .runtime import episode_from_memory_card


@dataclass(frozen=True)
class OntologyEpisodeBatch:
    episodes: tuple[OntologyEpisode, ...]
    failures: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["episodes"] = [episode.to_dict() for episode in self.episodes]
        data["failures"] = [dict(item) for item in self.failures]
        return data


def episode_from_session_artifact(artifact: SessionMemoryArtifact) -> OntologyEpisode:
    return OntologyEpisode.from_payload(
        event_id=artifact.source_event_ids[0] if artifact.source_event_ids else artifact.artifact_id,
        entity_type="Session",
        natural_id=artifact.session_id_hash.replace(":", "_"),
        payload={
            "artifact_id": artifact.artifact_id,
            "project": artifact.project,
            "provider": artifact.provider,
            "summary": artifact.summary,
            "session_id_hash": artifact.session_id_hash,
            "brain_id": f"/project/{artifact.project}",
        },
        lifecycle_state="accepted",
        currentness="current",
        source_event_ids=artifact.source_event_ids,
        source_ref_ids=artifact.chunk_refs + artifact.tool_evidence_refs,
        observed_at=artifact.created_at,
        reference_time=artifact.created_at,
        ontology_version=artifact.ontology_version,
        extractor_version=artifact.extractor_version,
    )


def episode_from_source_ref(record: SourceRefRecord, *, project: str = "") -> OntologyEpisode:
    lifecycle_state = "deleted" if record.deleted_at else ("revoked" if record.revoked_at else "active")
    currentness = "stale" if record.deleted_at or record.revoked_at else "current"
    payload = {
        "source_ref_id": record.source_ref_id,
        "device_id_hash": record.device_id_hash,
        "root_id": record.root_id,
        "relative_path_hash": record.relative_path_hash,
        "content_hash": record.content_hash,
        "sync_policy": record.sync_policy,
        "permission_scope": record.permission_scope,
        "last_seen_at": record.last_seen_at,
        "deleted_at": record.deleted_at,
        "revoked_at": record.revoked_at,
        "derived_summary": public_safe_text(record.derived_summary, max_chars=512),
    }
    if project:
        payload["project"] = project
        payload["brain_id"] = f"/project/{project}"
    return OntologyEpisode.from_payload(
        event_id=f"evt_source_ref_{record.source_ref_id}",
        entity_type="SourceRef",
        natural_id=record.source_ref_id,
        payload=payload,
        lifecycle_state=lifecycle_state,
        currentness=currentness,
        source_ref_ids=[record.source_ref_id],
        observed_at=record.deleted_at or record.revoked_at or record.last_seen_at,
        reference_time=record.last_seen_at,
        extractor_version="source-ref-runtime.1",
    )


def build_ontology_episode_batch(
    *,
    artifacts: list[SessionMemoryArtifact] | tuple[SessionMemoryArtifact, ...] = (),
    memory_cards: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] = (),
    source_refs: list[SourceRefRecord] | tuple[SourceRefRecord, ...] = (),
    project: str = "",
) -> list[OntologyEpisode]:
    return list(build_ontology_episode_batch_report(
        artifacts=artifacts,
        memory_cards=memory_cards,
        source_refs=source_refs,
        project=project,
    ).episodes)


def build_ontology_episode_batch_report(
    *,
    artifacts: list[SessionMemoryArtifact] | tuple[SessionMemoryArtifact, ...] = (),
    memory_cards: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] = (),
    source_refs: list[SourceRefRecord] | tuple[SourceRefRecord, ...] = (),
    project: str = "",
) -> OntologyEpisodeBatch:
    episodes: list[OntologyEpisode] = []
    failures: list[dict[str, Any]] = []
    for artifact in artifacts:
        try:
            episodes.append(episode_from_session_artifact(artifact))
        except Exception as exc:
            failures.append(_failure("artifact", getattr(artifact, "artifact_id", ""), exc))
    for card in memory_cards:
        try:
            episodes.append(episode_from_memory_card(card))
        except Exception as exc:
            item_id = str(card.get("memory_id") or "") if isinstance(card, Mapping) else ""
            failures.append(_failure("memory_card", item_id, exc))
    for source_ref in source_refs:
        try:
            episodes.append(episode_from_source_ref(source_ref, project=project))
        except Exception as exc:
            failures.append(_failure("source_ref", getattr(source_ref, "source_ref_id", ""), exc))
    return OntologyEpisodeBatch(episodes=tuple(episodes), failures=tuple(failures))


def _failure(item_type: str, item_id: str, exc: Exception) -> dict[str, Any]:
    return {
        "item_type": item_type,
        "item_id": str(item_id or ""),
        "reason_code": type(exc).__name__,
    }
