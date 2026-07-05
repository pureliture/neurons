from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ._util import ensure_public_safe, public_safe_text, short_hash
from .document_authority import document_authority_cards_from_memory_cards
from .models import ContextPack, GraphMemoryResult
from .objects.object_packs import build_agent_context_object_packs
from .preference_authority import preference_rule_cards_from_memory_cards
from .repo_style_profile import repo_style_profile_from_memory_cards
from .workflow_authority import workflow_contract_cards_from_memory_cards

# Task statuses that mean a task is no longer open work. Kept here with the
# ranking/merge logic so the "is this still unfinished" rule lives in one place.
# 더 이상 현재-권위(current authority)가 아닌 currentness. persona/context-pack 의 현재
# 사실(fact) 소비자는 이 상태의 카드를 제외한다. (drift_explain 같은 history 소비자는 별도.)
NON_CURRENT_AUTHORITY = frozenset({"stale", "superseded", "archive_candidate"})

TERMINAL_TASK_STATUSES = {"done", "resolved", "closed", "cancelled"}
CONTEXT_AUTHORITY_CONSUMERS = {"unspecified", "codex", "claude-code", "gemini", "hermes"}
SEARCH_MIRROR_STATUSES = {"unverified", "configured_unverified", "available", "degraded", "unavailable"}


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
        consumer: str = "unspecified",
        search_mirror_status: Mapping[str, Any] | None = None,
    ) -> ContextPack:
        safe_consumer = normalize_context_consumer(consumer)
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
        persona = tuple(
            persona_view(card)
            for card in cards
            if card.get("card_type") == "preference"
            and str(card.get("currentness") or "") not in NON_CURRENT_AUTHORITY
        )
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
        if needs_runtime_evidence(current_request, current_files):
            gaps.append("runtime_evidence_unverified")
        authority = authority_block(
            cards,
            graph_result=graph_result,
            gaps=gaps,
            current_files=current_files,
            current_request=current_request,
            consumer=safe_consumer,
            search_mirror_status=search_mirror_status,
        )

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
            authority=authority,
            bridge_evidence=bridge_evidence,
            gaps=tuple(gaps),
            audit={
                "request_hash": short_hash([repository, branch, current_files, current_request]),
                "source": "llm_brain_core",
                "consumer": safe_consumer,
            },
        )
        ensure_public_safe(pack.to_dict(), "ContextPack")
        return pack


def authority_block(
    cards: list[dict[str, Any]],
    *,
    graph_result: GraphMemoryResult,
    gaps: list[str],
    current_files: list[str] | tuple[str, ...] = (),
    current_request: str = "",
    consumer: str = "unspecified",
    search_mirror_status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    safe_consumer = normalize_context_consumer(consumer)
    block = {
        "schema_version": "context_authority_pack.v1",
        "documents": document_authority_cards(cards, current_files=current_files),
        "workflow_contracts": workflow_contract_cards(cards),
        "preferences": preference_rule_cards(cards, current_request=current_request, current_files=current_files),
        "evidence_gaps": evidence_gap_cards(gaps),
        "boundary_guardrails": [
            "agents_use_brain_context_resolve",
            "neo4j_is_derived_authority_workbench",
            "graphiti_is_projection_path",
            "qdrant_is_document_mirror",
            "dendrite_is_evidence_sensor",
            "retired_document_bridge_not_context_authority_dependency",
        ],
        "consumer_contract": {
            "consumer": safe_consumer,
            "read_only": True,
            "mutation_allowed": False,
            "default_agent_api": "brain_context_resolve",
        },
        "projection": {
            "neo4j": {
                "status": graph_result.status,
                "authority": "derived_authority_graph",
                "default_agent_api": "brain_context_resolve",
                "details": list(graph_result.details),
            }
        },
        "search_mirror": {
            "qdrant_docling": search_mirror_status_block(search_mirror_status)
        },
    }
    block["object_packs"] = build_agent_context_object_packs(
        documents=block["documents"],
        preferences=block["preferences"],
        style_profile=repo_style_profile_from_memory_cards(cards, repository=""),
        current_work=unfinished_items(cards, graph_result),
        required_verification=["cd worker && uv run pytest -q"],
        guardrails=block["boundary_guardrails"],
    )
    block["object_substrate_status"] = {
        "status": "degraded",
        "authority": "model_available_object_store_not_configured",
        "gaps": sorted({*gaps, "object_store_not_configured"}),
    }
    ensure_public_safe(block, "context_authority")
    return block


def search_mirror_status_block(status: Mapping[str, Any] | None = None) -> dict[str, Any]:
    provided = status if isinstance(status, Mapping) else {}
    raw_status = str(provided.get("status") or "unverified").lower()
    safe_status = raw_status if raw_status in SEARCH_MIRROR_STATUSES else "unverified"
    raw_details = provided.get("details")
    details = raw_details if isinstance(raw_details, (list, tuple)) else ()
    block = {
        "status": safe_status,
        "authority": "searchable_document_mirror",
        "canonical_memory": False,
        "product_use": "candidate_only_requires_document_authority_join",
        "requires_document_authority_join": True,
        "degraded_if_unavailable": True,
        "last_verified_at": public_safe_text(str(provided.get("last_verified_at") or ""), max_chars=80),
        "evidence_ref": public_safe_text(str(provided.get("evidence_ref") or ""), max_chars=160),
        "details": [public_safe_text(str(item or ""), max_chars=160) for item in details if str(item or "")],
    }
    ensure_public_safe(block, "search_mirror_status")
    return block


def normalize_context_consumer(consumer: str) -> str:
    safe = public_safe_text(str(consumer or "unspecified"), max_chars=80).lower()
    if safe not in CONTEXT_AUTHORITY_CONSUMERS:
        raise ValueError("consumer must be unspecified, codex, claude-code, gemini, or hermes")
    return safe


def document_authority_cards(
    cards: list[dict[str, Any]],
    *,
    current_files: list[str] | tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    return document_authority_cards_from_memory_cards(cards, inventory_paths=current_files)


def workflow_contract_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return workflow_contract_cards_from_memory_cards(cards)


def preference_rule_cards(
    cards: list[dict[str, Any]],
    *,
    current_request: str = "",
    current_files: list[str] | tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    return preference_rule_cards_from_memory_cards(
        cards,
        current_request=current_request,
        current_files=current_files,
    )


def evidence_gap_cards(gaps: list[str]) -> list[dict[str, Any]]:
    actions = {
        "no_canonical_memory": "add_or_accept_authority_cards",
        "graph_unavailable": "verify_neo4j_projection_or_mark_workbench_degraded",
        "graph_edge_degraded": "inspect_graphiti_neo4j_edge_search",
        "runtime_evidence_unverified": "verify_against_approved_ubuntu_runtime_surface",
    }
    return [
        {
            "code": gap,
            "severity": "medium" if gap == "no_canonical_memory" else "low",
            "next_action": actions.get(gap, "inspect_context_authority_evidence"),
        }
        for gap in gaps
    ]


def needs_runtime_evidence(current_request: str, current_files: list[str]) -> bool:
    request_text = str(current_request or "").lower()
    text = " ".join([current_request, *current_files]).lower()
    terms = (
        "runtime",
        "deploy",
        "deployed",
        "ubuntu",
        "compose",
        "container",
        "production",
        "k3s",
        "cluster",
        "canary",
        "health",
        "graphiti",
        "neo4j",
        "qdrant",
        "rag" + "flow",
        "live",
    )
    tokens = set(re.findall(r"[a-z0-9]+", text))
    if any(term in tokens for term in terms):
        return True
    request_tokens = set(re.findall(r"[a-z0-9]+", request_text))
    return "worker" in request_tokens and bool(request_tokens & {"running", "status", "healthy", "deployed"})


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
