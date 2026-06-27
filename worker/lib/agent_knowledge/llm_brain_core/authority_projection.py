from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._util import ensure_public_safe, public_safe_text, short_hash
from .models import ContextPack, OntologyEpisode

CONTEXT_AUTHORITY_PROJECTION_VERSION = "context_authority_projection.v1"


def authority_episodes_from_context_pack(pack: ContextPack | Mapping[str, Any]) -> tuple[OntologyEpisode, ...]:
    """Context Authority workbench용 derived graph episode를 만든다.

    반환되는 episode는 projection payload일 뿐 raw truth가 아니다.
    graph 직접 수정도 product authority를 바꾸지 않는다.
    """

    data = pack.to_dict() if isinstance(pack, ContextPack) else dict(pack)
    brain_id = str(data.get("brain_id") or "")
    authority = data.get("authority") if isinstance(data.get("authority"), Mapping) else {}
    event_id = f"context-authority:{short_hash([brain_id, data.get('audit', {})])}"
    episodes = [
        *_document_episodes(authority.get("documents"), brain_id=brain_id, event_id=event_id),
        *_workflow_episodes(authority.get("workflow_contracts"), brain_id=brain_id, event_id=event_id),
        *_preference_episodes(authority.get("preferences"), brain_id=brain_id, event_id=event_id),
        *_gap_episodes(authority.get("evidence_gaps"), brain_id=brain_id, event_id=event_id),
    ]
    ensure_public_safe([episode.to_dict() for episode in episodes], "context_authority_projection")
    return tuple(episodes)


def _document_episodes(value: Any, *, brain_id: str, event_id: str) -> list[OntologyEpisode]:
    episodes: list[OntologyEpisode] = []
    for item in _list_of_mappings(value):
        path = public_safe_text(str(item.get("path") or ""), max_chars=240)
        if not path:
            continue
        payload = {
            "brain_id": brain_id,
            "path": path,
            "status": item.get("status") or "unknown",
            "reason": item.get("reason") or "",
            "confidence": item.get("confidence") or 0,
            "evidence_refs": list(item.get("evidence_refs") or []),
            "evidence_edges": list(item.get("evidence_edges") or []),
            "authority": "derived_authority_graph",
            "projection_version": CONTEXT_AUTHORITY_PROJECTION_VERSION,
            "source_card_id": _source_card_id(item, fallback=f"document:{short_hash(path)}"),
        }
        episodes.append(
            OntologyEpisode.from_payload(
                event_id=event_id,
                entity_type="Document",
                natural_id=f"document:{short_hash(path)}",
                payload=payload,
                source_event_ids=tuple(str(ref) for ref in item.get("evidence_refs") or []),
            )
        )
    return episodes


def _workflow_episodes(value: Any, *, brain_id: str, event_id: str) -> list[OntologyEpisode]:
    episodes: list[OntologyEpisode] = []
    for item in _list_of_mappings(value):
        rule = public_safe_text(str(item.get("rule") or ""), max_chars=360)
        if not rule:
            continue
        payload = {
            "brain_id": brain_id,
            "rule": rule,
            "scope": item.get("scope") or "project",
            "reason": item.get("reason") or "",
            "confidence": item.get("confidence") or 0,
            "evidence_refs": list(item.get("evidence_refs") or []),
            "exceptions": list(item.get("exceptions") or []),
            "auto_update_allowed": bool(item.get("auto_update_allowed", False)),
            "authority": "derived_authority_graph",
            "projection_version": CONTEXT_AUTHORITY_PROJECTION_VERSION,
            "source_card_id": _source_card_id(item, fallback=f"workflow:{short_hash(rule)}"),
        }
        episodes.append(
            OntologyEpisode.from_payload(
                event_id=event_id,
                entity_type="WorkflowContract",
                natural_id=f"workflow:{short_hash(rule)}",
                payload=payload,
                source_event_ids=(str(item.get("memory_id") or ""),),
            )
        )
    return episodes


def _preference_episodes(value: Any, *, brain_id: str, event_id: str) -> list[OntologyEpisode]:
    episodes: list[OntologyEpisode] = []
    for item in _list_of_mappings(value):
        rule = public_safe_text(str(item.get("rule") or ""), max_chars=360)
        if not rule:
            continue
        payload = {
            "brain_id": brain_id,
            "rule": rule,
            "scope": item.get("scope") or "global",
            "reason": item.get("reason") or "",
            "confidence": item.get("confidence") or 0,
            "currentness": item.get("currentness") or "unknown",
            "evidence_refs": list(item.get("evidence_refs") or []),
            "exceptions": list(item.get("exceptions") or []),
            "authority": "derived_authority_graph",
            "projection_version": CONTEXT_AUTHORITY_PROJECTION_VERSION,
            "source_card_id": _source_card_id(item, fallback=f"preference:{short_hash(rule)}"),
        }
        episodes.append(
            OntologyEpisode.from_payload(
                event_id=event_id,
                entity_type="PreferenceRule",
                natural_id=f"preference:{short_hash(rule)}",
                payload=payload,
                source_event_ids=(str(item.get("memory_id") or ""),),
            )
        )
    return episodes


def _gap_episodes(value: Any, *, brain_id: str, event_id: str) -> list[OntologyEpisode]:
    episodes: list[OntologyEpisode] = []
    for item in _list_of_mappings(value):
        code = public_safe_text(str(item.get("code") or ""), max_chars=120)
        if not code:
            continue
        payload = {
            "brain_id": brain_id,
            "code": code,
            "severity": item.get("severity") or "unknown",
            "next_action": item.get("next_action") or "",
            "authority": "derived_authority_graph",
            "projection_version": CONTEXT_AUTHORITY_PROJECTION_VERSION,
            "source_card_id": f"gap:{code}",
        }
        episodes.append(
            OntologyEpisode.from_payload(
                event_id=event_id,
                entity_type="EvidenceGap",
                natural_id=f"evidence-gap:{code}",
                payload=payload,
            )
        )
    return episodes


def _list_of_mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _source_card_id(item: Mapping[str, Any], *, fallback: str) -> str:
    value = item.get("memory_id")
    if value:
        return public_safe_text(str(value), max_chars=160)
    evidence_refs = item.get("evidence_refs") if isinstance(item.get("evidence_refs"), list) else []
    for ref in evidence_refs:
        safe_ref = public_safe_text(str(ref), max_chars=160)
        if safe_ref:
            return safe_ref
    return public_safe_text(fallback, max_chars=160)
