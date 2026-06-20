from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from ._util import (
    list_or_empty,
    public_safe_text,
    require_non_empty,
    short_hash,
    utc_now_iso,
)
from .models import OntologyEpisode, SessionMemoryArtifact, SourceRefRecord


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


def episode_from_memory_card(card: Mapping[str, Any], *, project: str = "") -> OntologyEpisode:
    card_project = str(card.get("project") or "")
    resolved_project = str(project or card_project)
    payload = {
        "memory_id": str(card.get("memory_id") or ""),
        "brain_id": str(card.get("brain_id") or ""),
        "project": resolved_project,
        "card_type": str(card.get("card_type") or ""),
        "title": public_safe_text(str(card.get("title") or ""), max_chars=240),
        "summary": public_safe_text(str(card.get("summary") or ""), max_chars=512),
        # typed_payload free text is operator-authored and can carry general PII
        # or private paths. The graph body is serialized straight from this
        # payload, so redact every nested string through the public-safe filter
        # (the same redaction title/summary already get) before it reaches the
        # derived index, instead of trusting raw card text.
        "typed_payload": _public_safe_typed_payload(card.get("typed_payload")),
    }
    memory_id = require_non_empty(payload["memory_id"], "memory_id")
    card_type = require_non_empty(payload["card_type"], "card_type")
    # brain_id is the graph group key. A missing brain_id silently breaks
    # group_ids scoping, so derive it from the project (the canonical group key
    # `/project/<project>`) and fail fast instead of projecting an ungrouped
    # episode. When the caller passes `project`, the card's own brain_id (if any)
    # must agree with the project-derived key; a mismatch is a scoping hazard and
    # fails fast rather than silently grouping under the wrong brain.
    payload["brain_id"] = _resolve_card_brain_id(
        card_brain_id=payload["brain_id"],
        project=project,
        resolved_project=resolved_project,
    )
    natural_id = f"{card_type}:{memory_id}"
    entity_type = _entity_type_for_card(payload["card_type"])
    return OntologyEpisode.from_payload(
        event_id=f"evt:{short_hash([natural_id, card.get('content_hash', '')])}",
        entity_type=entity_type,
        natural_id=natural_id,
        payload=payload,
        lifecycle_state=str(card.get("lifecycle_state") or "accepted"),
        currentness=str(card.get("currentness") or "unknown"),
        source_event_ids=tuple(str(ref) for ref in list_or_empty(card.get("derived_from"))),
        source_ref_ids=tuple(_source_ref_ids(card)),
        observed_at=str(card.get("approved_at") or card.get("accepted_at") or card.get("updated_at") or utc_now_iso()),
        ontology_version=str(card.get("ontology_version") or "1.0.0"),
        extractor_version="memory-card-runtime.1",
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
            episodes.append(episode_from_memory_card(card, project=project))
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


def _resolve_card_brain_id(*, card_brain_id: str, project: str, resolved_project: str) -> str:
    """Resolve the graph group key (`brain_id`) for a memory-card episode.

    - When a `project` is supplied, the canonical group key is
      `/project/<project>`. If the card already carries a `brain_id`, it must
      match; a mismatch is a scoping hazard (the card would group under a
      different brain than its project) and fails fast.
    - When no `project` is supplied, fall back to the card's own `brain_id`
      (project-derived if absent), and require a non-empty result so an
      ungrouped episode is never projected.
    """

    project_brain_id = f"/project/{project}" if project else ""
    if project_brain_id:
        if card_brain_id and card_brain_id != project_brain_id:
            raise ValueError(
                f"memory card brain_id does not match project group key: {card_brain_id!r} != {project_brain_id!r}"
            )
        return project_brain_id
    fallback = card_brain_id or (f"/project/{resolved_project}" if resolved_project else "")
    return require_non_empty(fallback, "brain_id")


def _public_safe_typed_payload(value: Any, *, max_chars: int = 2048) -> Any:
    """Recursively redact free-text inside a card's typed_payload.

    Every string (dict value, list item, or nested) is passed through
    `public_safe_text` so general PII / private paths in operator-authored card
    fields never reach the graph body. Dict keys, booleans, and numbers are
    preserved as-is so structured filtering still works downstream. The recursion
    is depth-bounded as a cheap guard against pathological/cyclic-shaped input.
    """

    return _redact_value(value, max_chars=max_chars, depth=0)


def _redact_value(value: Any, *, max_chars: int, depth: int) -> Any:
    if depth > 12:
        return public_safe_text(str(value), max_chars=max_chars)
    if isinstance(value, str):
        return public_safe_text(value, max_chars=max_chars)
    if isinstance(value, Mapping):
        return {
            str(key): _redact_value(item, max_chars=max_chars, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, max_chars=max_chars, depth=depth + 1) for item in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    # Unknown/opaque types: stringify then redact so nothing raw slips through.
    return public_safe_text(str(value), max_chars=max_chars)


def _entity_type_for_card(card_type: str) -> str:
    return {
        "decision": "Decision",
        "task": "Task",
        "drift": "Drift",
        "preference": "PersonaFact",
        "evidence": "Evidence",
        "status": "Status",
    }.get(card_type, "MemoryCard")


def _source_ref_ids(card: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    for ref in card.get("source_refs") or ():
        if isinstance(ref, str):
            ids.append(ref)
        elif isinstance(ref, Mapping):
            value = ref.get("source_ref_id") or ref.get("source_id")
            if value:
                ids.append(str(value))
    return ids
