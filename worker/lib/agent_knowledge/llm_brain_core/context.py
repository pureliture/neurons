from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ._util import ensure_public_safe, public_safe_text, short_hash
from .artifact_store import InMemorySessionMemoryArtifactStore, SessionMemoryArtifactStore
from .document_bridge import DisabledDocumentBridge, DocumentBridge
from .graph import GraphMemoryAdapter, NullGraphMemoryAdapter
from .models import ContextPack, EvidenceRequest, GraphMemoryResult
from .source_ref import SourceRefResolver


ACCEPTED_LIFECYCLE_STATES = {"accepted", "human_accepted", "auto_accepted"}
ACCEPTED_APPROVAL_STATES = {"approved", "auto_accepted"}
TERMINAL_TASK_STATUSES = {"done", "resolved", "closed", "cancelled"}


class BrainReadService:
    """Read-side service for ContextPack, incident, drift, persona and evidence."""

    def __init__(
        self,
        *,
        artifact_store: SessionMemoryArtifactStore | None = None,
        memory_cards: list[Mapping[str, Any]] | None = None,
        graph_adapter: GraphMemoryAdapter | None = None,
        source_resolver: SourceRefResolver | None = None,
        document_bridge: DocumentBridge | None = None,
    ) -> None:
        self.artifact_store = artifact_store or InMemorySessionMemoryArtifactStore()
        self.memory_cards = [dict(card) for card in memory_cards or [] if _is_accepted_card(card)]
        self.graph_adapter = graph_adapter or NullGraphMemoryAdapter()
        self.source_resolver = source_resolver or SourceRefResolver()
        self.document_bridge = document_bridge or DisabledDocumentBridge()

    def brain_context_resolve(
        self,
        *,
        repository: str,
        branch: str,
        current_files: list[str],
        current_request: str,
        project: str | None = None,
        limit: int = 8,
    ) -> ContextPack:
        project_name = project or _project_from_repository(repository)
        brain_id = f"/project/{project_name}"
        artifacts = self.artifact_store.list_recent(project=project_name, limit=limit)
        cards = _project_cards(self.memory_cards, project_name)
        query = " ".join([repository, branch, current_request, " ".join(current_files)])
        graph_result = self.graph_adapter.search_context(
            brain_id=brain_id,
            query=query,
            entity_types=["Task", "Decision", "Incident", "PersonaFact", "File"],
            limit=limit,
        )
        bridge_result = self.document_bridge.search_documents(
            query=current_request,
            project=project_name,
            limit=limit,
        )
        task_card = _select_current_task(cards, current_request)
        current_task = _task_title(task_card)
        if not current_task and artifacts:
            current_task = artifacts[0].summary
        last_stopped_at = _last_stop(task_card, artifacts)
        decisions = tuple(_decision_view(card) for card in cards if card.get("card_type") == "decision")
        incidents = tuple(self.brain_incident_search(symptom=current_request, project=project_name, limit=3)["reusable_fixes"])
        persona = tuple(_persona_view(card) for card in cards if card.get("card_type") == "preference")
        unfinished_items = tuple(_unfinished_items(cards, graph_result))
        source_refs = tuple(_source_refs(cards, graph_result))
        gaps: list[str] = []
        if not artifacts and not cards:
            gaps.append("no_canonical_memory")
        if graph_result.status != "available":
            gaps.append("graph_unavailable")
        pack = ContextPack(
            brain_id=brain_id,
            current_task=current_task,
            last_stopped_at=last_stopped_at,
            unfinished_items=unfinished_items,
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
            bridge_status={
                "status": bridge_result.status,
                "authority": bridge_result.authority,
                "details": list(bridge_result.details),
            },
            bridge_evidence=bridge_result.evidence,
            gaps=tuple(gaps),
            audit={
                "request_hash": short_hash([repository, branch, current_files, current_request]),
                "source": "llm_brain_core",
            },
        )
        ensure_public_safe(pack.to_dict(), "ContextPack")
        return pack

    def brain_memory_search(
        self,
        *,
        query: str,
        project: str,
        card_types: list[str] | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 100))
        wanted = set(card_types or [])
        terms = _terms(query)
        cards = [
            _card_view(card)
            for card in _project_cards(self.memory_cards, project)
            if (not wanted or str(card.get("card_type")) in wanted) and _matches_terms(card, terms)
        ][:bounded]
        graph = self.graph_adapter.search_context(
            brain_id=f"/project/{project}",
            query=query,
            entity_types=card_types,
            limit=bounded,
        )
        result = {
            "memory_status": {"status": "available", "authority": "canonical_card", "count": len(cards)},
            "graph_status": {"status": graph.status, "authority": "derived_index"},
            "results": cards,
            "graph_results": [episode.to_dict() for episode in graph.episodes],
        }
        ensure_public_safe(result, "brain_memory_search")
        return result

    def brain_incident_search(self, *, symptom: str, project: str, limit: int = 5) -> dict[str, Any]:
        graph = self.graph_adapter.search_context(
            brain_id=f"/project/{project}",
            query=symptom,
            entity_types=["Incident", "Symptom", "Attempt", "Fix", "Verification"],
            limit=max(limit * 4, 10),
        )
        reusable: list[dict[str, Any]] = []
        do_not_apply: list[dict[str, Any]] = []
        grouped = _incident_records(graph)
        for item in grouped:
            if item.get("applies") is False or item.get("do_not_apply"):
                do_not_apply.append(item)
            else:
                reusable.append(item)
        result = {
            "query": public_safe_text(symptom, max_chars=512),
            "reusable_fixes": reusable[:limit],
            "do_not_apply": do_not_apply[:limit],
            "graph_status": {"status": graph.status, "authority": "derived_index"},
        }
        ensure_public_safe(result, "brain_incident_search")
        return result

    def brain_incident_replay(self, *, incident_id: str, project: str) -> dict[str, Any]:
        graph = self.graph_adapter.search_context(
            brain_id=f"/project/{project}",
            query=incident_id,
            entity_types=["Incident", "Symptom", "Hypothesis", "Attempt", "Fix", "Verification"],
            limit=20,
        )
        timeline = sorted(
            [episode.to_dict() for episode in graph.episodes],
            key=lambda item: (item.get("observed_at", ""), item.get("episode_id", "")),
        )
        result = {
            "incident_id": public_safe_text(incident_id, max_chars=160),
            "timeline": timeline,
            "graph_status": {"status": graph.status, "authority": "derived_index"},
        }
        ensure_public_safe(result, "brain_incident_replay")
        return result

    def brain_drift_explain(self, *, subject: str, project: str) -> dict[str, Any]:
        cards = _project_cards(self.memory_cards, project)
        subject_terms = _terms(subject)
        drift_cards = [
            _card_view(card)
            for card in cards
            if card.get("card_type") == "drift" and _matches_terms(card.get("typed_payload", {}), subject_terms)
        ]
        decisions = [
            card for card in cards if card.get("card_type") == "decision" and _matches_terms(card, subject_terms)
        ]
        prior = [_decision_view(card) for card in decisions if card.get("currentness") == "superseded"]
        current = [_decision_view(card) for card in decisions if card.get("currentness") == "current"]
        status = "explained" if prior or current or drift_cards else "insufficient_evidence"
        result = {
            "subject": public_safe_text(subject, max_chars=240),
            "status": status,
            "prior_decisions": prior,
            "current_decisions": current,
            "drift_events": drift_cards,
            "memory_status": {"status": "available", "authority": "canonical_card"},
        }
        ensure_public_safe(result, "brain_drift_explain")
        return result

    def brain_persona_get(self, *, project: str | None = None, scope: str | None = None) -> dict[str, Any]:
        cards = [card for card in self.memory_cards if card.get("card_type") == "preference"]
        if project:
            cards = [card for card in cards if str(card.get("project") or "") in ("", project)]
        if scope:
            cards = [card for card in cards if str(card.get("scope") or "") in ("", scope, "global")]
        facts = [_persona_view(card) for card in cards]
        result = {"facts": facts, "memory_status": {"status": "available", "authority": "canonical_card"}}
        ensure_public_safe(result, "brain_persona_get")
        return result

    def brain_persona_check(self, *, plan: str, project: str | None = None) -> dict[str, Any]:
        cards = [
            card
            for card in self.memory_cards
            if card.get("card_type") == "preference" and (project is None or str(card.get("project") or "") in ("", project))
        ]
        if not cards:
            return {
                "status": "insufficient_evidence",
                "facts": [],
                "conflicts": [],
                "memory_status": {"status": "available", "authority": "canonical_card"},
            }
        drift = [card for card in cards if card.get("currentness") in {"superseded", "conflicted"}]
        if drift:
            status = "persona_drift"
        else:
            status = "aligned"
        conflicts = [_persona_view(card) for card in cards if _persona_conflicts(card, plan)]
        if conflicts:
            status = "possible_conflict"
        result = {
            "status": status,
            "facts": [_persona_view(card) for card in cards],
            "conflicts": conflicts,
            "memory_status": {"status": "available", "authority": "canonical_card"},
        }
        ensure_public_safe(result, "brain_persona_check")
        return result

    def brain_evidence_get(self, request: EvidenceRequest) -> dict[str, Any]:
        return self.source_resolver.resolve(request).to_dict()


def _project_from_repository(repository: str) -> str:
    value = str(repository or "").rstrip("/")
    if not value:
        return "unknown"
    name = value.split("/")[-1]
    return name.removesuffix(".git") or "unknown"


def _is_accepted_card(card: Mapping[str, Any]) -> bool:
    if not isinstance(card, Mapping):
        return False
    lifecycle = str(card.get("lifecycle_state") or "")
    approval = str(card.get("approval_state") or "")
    if lifecycle:
        return lifecycle in ACCEPTED_LIFECYCLE_STATES and approval in ACCEPTED_APPROVAL_STATES
    return str(card.get("state") or "") == "active"


def _project_cards(cards: list[dict[str, Any]], project: str) -> list[dict[str, Any]]:
    return [card for card in cards if str(card.get("project") or "") == project]


def _select_current_task(cards: list[dict[str, Any]], request: str) -> dict[str, Any] | None:
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
    candidates.sort(key=lambda card: (_match_score(card, terms), str(card.get("updated_at") or card.get("created_at") or "")), reverse=True)
    return candidates[0]


def _task_title(card: Mapping[str, Any] | None) -> str:
    if not card:
        return ""
    payload = card.get("typed_payload") or {}
    return public_safe_text(str(payload.get("task_state") or card.get("title") or card.get("summary") or ""), max_chars=240)


def _last_stop(task_card: Mapping[str, Any] | None, artifacts: list[Any]) -> str:
    if task_card:
        payload = task_card.get("typed_payload") or {}
        return public_safe_text(str(payload.get("next_action") or payload.get("blocker") or task_card.get("summary") or ""), max_chars=320)
    if artifacts:
        return public_safe_text(artifacts[0].summary, max_chars=320)
    return ""


def _unfinished_items(cards: list[dict[str, Any]], graph: GraphMemoryResult) -> list[str]:
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


def _source_refs(cards: list[dict[str, Any]], graph: GraphMemoryResult) -> list[dict[str, Any]]:
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


def _card_view(card: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(card.get("typed_payload") or {})
    view = {
        "memory_id": card.get("memory_id", ""),
        "card_type": card.get("card_type", ""),
        "title": public_safe_text(str(card.get("title") or ""), max_chars=240),
        "summary": public_safe_text(str(card.get("summary") or ""), max_chars=512),
        "currentness": card.get("currentness", "unknown"),
        "confidence": card.get("confidence", 0),
        "typed_payload": payload,
    }
    ensure_public_safe(view, "card_view")
    return view


def _decision_view(card: Mapping[str, Any]) -> dict[str, Any]:
    payload = card.get("typed_payload") or {}
    return {
        "memory_id": card.get("memory_id", ""),
        "decision": public_safe_text(str(payload.get("decision") or card.get("summary") or ""), max_chars=360),
        "rationale": public_safe_text(str(payload.get("rationale") or ""), max_chars=360),
        "currentness": card.get("currentness", "unknown"),
        "supersedes": list(card.get("supersedes") or []),
        "superseded_by": list(card.get("superseded_by") or []),
    }


def _persona_view(card: Mapping[str, Any]) -> dict[str, Any]:
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


def _incident_records(graph: GraphMemoryResult) -> list[dict[str, Any]]:
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


def _append_unique(items: list[str], value: Any) -> None:
    text = public_safe_text(str(value or ""), max_chars=360)
    if text and text not in items:
        items.append(text)


def _persona_conflicts(card: Mapping[str, Any], plan: str) -> bool:
    text = " ".join(
        [
            str(card.get("summary") or ""),
            str((card.get("typed_payload") or {}).get("preference") or ""),
        ]
    ).lower()
    plan_text = str(plan or "").lower()
    if "architecture" in text and "before code" in text:
        return any(marker in plan_text for marker in ("code first", "implementation first", "implement before design"))
    if "avoid" in text and any(term in plan_text for term in _terms(text.replace("avoid", ""))):
        return True
    if "local first" in text and "cloud only" in plan_text:
        return True
    return False


def _terms(value: Any) -> list[str]:
    return [term for term in re.split(r"[^a-zA-Z0-9_가-힣]+", str(value).lower()) if len(term) >= 3]


def _matches_terms(value: Any, terms: list[str]) -> bool:
    if not terms:
        return True
    text = str(value).lower()
    return any(term in text for term in terms)


def _match_score(value: Any, terms: list[str]) -> int:
    text = str(value).lower()
    return sum(1 for term in terms if term in text)
