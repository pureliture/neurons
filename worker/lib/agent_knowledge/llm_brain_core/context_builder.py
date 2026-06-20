from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ._util import ensure_public_safe, public_safe_text, short_hash
from .models import ContextPack, GraphMemoryResult

# Task statuses that mean a task is no longer open work. Kept here with the
# ranking/merge logic so the "is this still unfinished" rule lives in one place.
TERMINAL_TASK_STATUSES = {"done", "resolved", "closed", "cancelled"}


class ContextPackBuilder:
    """Assembles a ContextPack from already-resolved inputs.

    This owns the read-side merge/ranking policy that used to live inline in
    `BrainReadService.brain_context_resolve`. The authority order is fixed:

        canonical MemoryCard  >  canonical SessionMemoryArtifact  >  derived graph

    For each ContextPack field, the canonical card answer wins; the artifact is
    the next fallback; the derived graph is consulted only when neither canonical
    source answered. The builder is pure (no I/O): the service resolves
    artifacts/cards/graph/incidents/bridge first, then hands them here so the
    ranking is testable in isolation and the round-trip/seam logic stays in the
    service.
    """

    def build(
        self,
        *,
        brain_id: str,
        repository: str,
        branch: str,
        current_files: list[str],
        current_request: str,
        artifacts: list[Any],
        cards: list[dict[str, Any]],
        graph_result: GraphMemoryResult,
        incidents: tuple[dict[str, Any], ...],
        bridge_status: dict[str, Any],
        bridge_evidence: tuple[dict[str, Any], ...],
    ) -> ContextPack:
        task_card = select_current_task(cards, current_request)

        # current_task: card > artifact > graph.
        current_task = task_title(task_card)
        if not current_task and artifacts:
            current_task = artifacts[0].summary
        if not current_task:
            current_task = graph_task_title(graph_result)

        # last_stopped_at: card/artifact (via last_stop) > graph.
        last_stopped_at = last_stop(task_card, artifacts)
        if not last_stopped_at:
            last_stopped_at = graph_task_stop(graph_result)

        decisions = tuple(decision_view(card) for card in cards if card.get("card_type") == "decision")
        persona = tuple(persona_view(card) for card in cards if card.get("card_type") == "preference")
        unfinished = tuple(unfinished_items(cards, graph_result))
        source_refs = tuple(merged_source_refs(cards, graph_result))

        gaps: list[str] = []
        if not artifacts and not cards:
            gaps.append("no_canonical_memory")
        if graph_result.status == "degraded":
            # Edge/relationship search failed but episode reads survived: a
            # partial graph, distinct from a fully unavailable one.
            gaps.append("graph_edge_degraded")
        elif graph_result.status != "available":
            gaps.append("graph_unavailable")

        pack = ContextPack(
            brain_id=brain_id,
            current_task=current_task,
            last_stopped_at=last_stopped_at,
            unfinished_items=unfinished,
            relevant_decisions=decisions,
            similar_incidents=incidents,
            persona_constraints=persona,
            source_refs=source_refs,
            memory_status={
                "status": "available",
                "authority": "canonical_artifact_and_card",
                "artifact_count": len(artifacts),
                "card_count": len(cards),
            },
            graph_status={
                "status": graph_result.status,
                "authority": "derived_index",
                "details": list(graph_result.details),
            },
            bridge_status=bridge_status,
            bridge_evidence=bridge_evidence,
            gaps=tuple(gaps),
            audit={
                "request_hash": short_hash([repository, branch, current_files, current_request]),
                "source": "llm_brain_core",
            },
        )
        ensure_public_safe(pack.to_dict(), "ContextPack")
        return pack


def select_current_task(cards: list[dict[str, Any]], request: str) -> dict[str, Any] | None:
    candidates = []
    for card in cards:
        if card.get("card_type") != "task" or card.get("currentness") not in ("current", "unknown", ""):
            continue
        payload = card.get("typed_payload") or {}
        if str(payload.get("status") or "").lower() in TERMINAL_TASK_STATUSES:
            continue
        candidates.append(card)
    if not candidates:
        return None
    terms = _terms(request)
    candidates.sort(
        key=lambda card: (
            _match_score(_card_match_text(card), terms),
            str(card.get("updated_at") or card.get("created_at") or ""),
        ),
        reverse=True,
    )
    return candidates[0]


def _card_match_text(card: Mapping[str, Any]) -> str:
    payload = card.get("typed_payload") if isinstance(card.get("typed_payload"), Mapping) else {}
    return " ".join(
        str(value or "")
        for value in (
            card.get("title"),
            card.get("summary"),
            payload.get("task_state"),
            payload.get("next_action"),
            payload.get("blocker"),
        )
    )


def task_title(card: Mapping[str, Any] | None) -> str:
    if not card:
        return ""
    payload = card.get("typed_payload") or {}
    return public_safe_text(str(payload.get("task_state") or card.get("title") or card.get("summary") or ""), max_chars=240)


def last_stop(task_card: Mapping[str, Any] | None, artifacts: list[Any]) -> str:
    if task_card:
        payload = task_card.get("typed_payload") or {}
        return public_safe_text(str(payload.get("next_action") or payload.get("blocker") or task_card.get("summary") or ""), max_chars=320)
    if artifacts:
        return public_safe_text(artifacts[0].summary, max_chars=320)
    return ""


def graph_task_title(graph: GraphMemoryResult) -> str:
    for episode in graph.episodes:
        if episode.entity_type != "Task":
            continue
        payload = episode.payload
        typed_payload = payload.get("typed_payload") if isinstance(payload.get("typed_payload"), Mapping) else {}
        value = (
            payload.get("task_state")
            or payload.get("task")
            or typed_payload.get("task_state")
            or payload.get("title")
            or payload.get("summary")
        )
        text = public_safe_text(str(value or ""), max_chars=240)
        if text:
            return text
    return ""


def graph_task_stop(graph: GraphMemoryResult) -> str:
    for episode in graph.episodes:
        if episode.entity_type != "Task":
            continue
        payload = episode.payload
        typed_payload = payload.get("typed_payload") if isinstance(payload.get("typed_payload"), Mapping) else {}
        value = (
            payload.get("next_action")
            or payload.get("blocker")
            or typed_payload.get("next_action")
            or typed_payload.get("blocker")
            or payload.get("summary")
        )
        text = public_safe_text(str(value or ""), max_chars=320)
        if text:
            return text
    return ""


def unfinished_items(cards: list[dict[str, Any]], graph: GraphMemoryResult) -> list[str]:
    items: list[str] = []
    for card in cards:
        if card.get("card_type") != "task":
            continue
        payload = card.get("typed_payload") or {}
        status = str(payload.get("status") or "").lower()
        if status in TERMINAL_TASK_STATUSES:
            continue
        for key in ("next_action", "blocker"):
            value = public_safe_text(str(payload.get(key) or ""), max_chars=240)
            if value and value not in items:
                items.append(value)
    for episode in graph.episodes:
        if episode.entity_type != "Task":
            continue
        value = public_safe_text(str(episode.payload.get("next_action") or episode.payload.get("task") or ""), max_chars=240)
        if value and value not in items:
            items.append(value)
    return items


def merged_source_refs(cards: list[dict[str, Any]], graph: GraphMemoryResult) -> list[dict[str, Any]]:
    seen: set[str] = set()
    refs: list[dict[str, Any]] = []
    for card in cards:
        for ref in card.get("source_refs") or []:
            safe = _safe_source_ref(ref)
            ref_id = str(safe.get("source_ref_id") or safe.get("id") or safe.get("value") or "")
            if ref_id and ref_id not in seen:
                refs.append(safe)
                seen.add(ref_id)
    for episode in graph.episodes:
        for ref_id in episode.source_ref_ids:
            if ref_id not in seen:
                refs.append({"source_ref_id": ref_id})
                seen.add(ref_id)
    return refs


def _safe_source_ref(ref: Any) -> dict[str, Any]:
    if isinstance(ref, str):
        return {"source_ref_id": public_safe_text(ref, max_chars=160)}
    if not isinstance(ref, Mapping):
        return {"source_ref_id": "invalid_ref"}
    safe: dict[str, Any] = {}
    for key in ("source_ref_id", "span_ref_id", "content_hash", "device_id_hash", "sync_policy"):
        if key in ref:
            safe[key] = ref[key]
    ensure_public_safe(safe, "source_ref")
    return safe


def decision_view(card: Mapping[str, Any]) -> dict[str, Any]:
    payload = card.get("typed_payload") or {}
    return {
        "memory_id": card.get("memory_id", ""),
        "decision": public_safe_text(str(payload.get("decision") or card.get("summary") or ""), max_chars=360),
        "rationale": public_safe_text(str(payload.get("rationale") or ""), max_chars=360),
        "currentness": card.get("currentness", "unknown"),
        "supersedes": list(card.get("supersedes") or []),
        "superseded_by": list(card.get("superseded_by") or []),
    }


def persona_view(card: Mapping[str, Any]) -> dict[str, Any]:
    payload = card.get("typed_payload") or {}
    return {
        "memory_id": card.get("memory_id", ""),
        "preference": public_safe_text(str(payload.get("preference") or card.get("summary") or ""), max_chars=360),
        "explicitness": payload.get("explicitness", "inferred"),
        "confirmation_status": payload.get("confirmation_status", "unconfirmed"),
        "applies_to": payload.get("applies_to", "global"),
        "currentness": card.get("currentness", "unknown"),
        "confidence": card.get("confidence", 0),
    }


def incident_records(graph: GraphMemoryResult) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for episode in graph.episodes:
        payload = dict(episode.payload)
        incident_id = str(payload.get("incident_id") or payload.get("target_incident_id") or episode.natural_id)
        record = records.setdefault(
            incident_id,
            {
                "incident_id": public_safe_text(incident_id, max_chars=200),
                "symptoms": [],
                "attempts": [],
                "fixes": [],
                "verifications": [],
                "applies": payload.get("applies", True),
                "do_not_apply": bool(payload.get("do_not_apply", False)),
            },
        )
        if episode.entity_type in ("Incident", "Symptom"):
            _append_unique(record["symptoms"], payload.get("symptom") or payload.get("summary") or payload.get("title"))
        elif episode.entity_type == "Attempt":
            _append_unique(record["attempts"], payload.get("attempt") or payload.get("summary"))
        elif episode.entity_type == "Fix":
            _append_unique(record["fixes"], payload.get("fix") or payload.get("summary"))
        elif episode.entity_type == "Verification":
            _append_unique(record["verifications"], payload.get("verification") or payload.get("summary"))
        if payload.get("applies") is False:
            record["applies"] = False
        if payload.get("do_not_apply"):
            record["do_not_apply"] = True
    return list(records.values())


def split_incident_lanes(records: list[dict[str, Any]], *, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reusable: list[dict[str, Any]] = []
    do_not_apply: list[dict[str, Any]] = []
    for item in records:
        if item.get("applies") is False or item.get("do_not_apply"):
            do_not_apply.append(item)
        else:
            reusable.append(item)
    return reusable[:limit], do_not_apply[:limit]


def _append_unique(items: list[str], value: Any) -> None:
    text = public_safe_text(str(value or ""), max_chars=360)
    if text and text not in items:
        items.append(text)


def _terms(value: Any) -> list[str]:
    return [term for term in re.split(r"[^a-zA-Z0-9_가-힣]+", str(value).lower()) if len(term) >= 3]


def _match_score(value: Any, terms: list[str]) -> int:
    text = str(value).lower()
    return sum(1 for term in terms if term in text)
