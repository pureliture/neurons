from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ._util import ensure_public_safe, public_safe_text
from .artifact_store import InMemorySessionMemoryArtifactStore, SessionMemoryArtifactStore
from .context_builder import (
    ContextPackBuilder,
    decision_view as _decision_view,
    incident_records as _incident_records,
    persona_view as _persona_view,
    split_incident_lanes as _split_incident_lanes,
)
from .document_bridge import DisabledDocumentBridge, DocumentBridge
from .graph import GraphMemoryAdapter, NullGraphMemoryAdapter
from .models import EvidenceRequest
from .source_ref import SourceRefResolver

if TYPE_CHECKING:
    # `brain_context_resolve` is annotated `-> ContextPack`. With
    # `from __future__ import annotations` the annotation is lazy, so the symbol
    # is only needed by static type checkers, not at runtime.
    from .models import ContextPack


ACCEPTED_LIFECYCLE_STATES = {"accepted", "human_accepted", "auto_accepted"}
ACCEPTED_APPROVAL_STATES = {"approved", "auto_accepted"}


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
        # The merge/ranking policy (card > artifact > graph) lives in the builder;
        # the service stays the I/O + seam layer.
        self._context_builder = ContextPackBuilder()

    def brain_context_resolve(
        self,
        *,
        repository: str,
        branch: str,
        current_files: list[str],
        current_request: str,
        project: str | None = None,
        limit: int = 8,
        consumer: str = "unspecified",
    ) -> ContextPack:
        project_name = project or _project_from_repository(repository)
        brain_id = f"/project/{project_name}"
        artifacts = self.artifact_store.list_recent(project=project_name, limit=limit)
        cards = _project_cards(self.memory_cards, project_name)
        query = " ".join([repository, branch, current_request, " ".join(current_files)])
        graph_result = self.graph_adapter.search_context(
            brain_id=brain_id,
            query=query,
            entity_types=["Task", "Decision", "Incident", "PersonaFact", "File", "SourceRef"],
            limit=limit,
        )
        bridge_result = self.document_bridge.search_documents(
            query=current_request,
            project=project_name,
            limit=limit,
        )
        incidents = tuple(
            self.brain_incident_search(symptom=current_request, project=project_name, limit=3)["reusable_fixes"]
        )
        return self._context_builder.build(
            brain_id=brain_id,
            repository=repository,
            branch=branch,
            current_files=current_files,
            current_request=current_request,
            artifacts=artifacts,
            cards=cards,
            graph_result=graph_result,
            incidents=incidents,
            bridge_status={
                "status": bridge_result.status,
                "authority": bridge_result.authority,
                "details": list(bridge_result.details),
            },
            bridge_evidence=bridge_result.evidence,
            consumer=consumer,
        )

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

    def brain_docs_current(
        self,
        *,
        repository: str,
        branch: str,
        current_files: list[str],
        current_request: str,
        project: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        pack = self.brain_context_resolve(
            repository=repository,
            branch=branch,
            current_files=current_files,
            current_request=current_request,
            project=project,
            limit=limit,
        ).to_dict()
        documents = [
            doc
            for doc in _authority_documents(pack)
            if str(doc.get("status") or "") not in {"archive_candidate", "historical", "superseded", "stale"}
        ]
        result = {
            "documents": documents,
            "memory_status": pack.get("memory_status", {}),
            "graph_status": pack.get("graph_status", {}),
        }
        ensure_public_safe(result, "brain_docs_current")
        return result

    def brain_docs_explain(
        self,
        *,
        document_path: str,
        repository: str,
        branch: str,
        current_files: list[str],
        current_request: str,
        project: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        files = list(current_files or [])
        if document_path not in files:
            files.append(document_path)
        pack = self.brain_context_resolve(
            repository=repository,
            branch=branch,
            current_files=files,
            current_request=current_request,
            project=project,
            limit=limit,
        ).to_dict()
        document = next((doc for doc in _authority_documents(pack) if doc.get("path") == document_path), None)
        result = {
            "document": document or {},
            "memory_status": pack.get("memory_status", {}),
            "graph_status": pack.get("graph_status", {}),
        }
        ensure_public_safe(result, "brain_docs_explain")
        return result

    def brain_docs_archive_candidates(
        self,
        *,
        repository: str,
        branch: str,
        current_files: list[str],
        current_request: str,
        project: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        pack = self.brain_context_resolve(
            repository=repository,
            branch=branch,
            current_files=current_files,
            current_request=current_request,
            project=project,
            limit=limit,
        ).to_dict()
        documents = [doc for doc in _authority_documents(pack) if doc.get("status") == "archive_candidate"]
        result = {
            "documents": documents,
            "archive_proposal_only": True,
            "memory_status": pack.get("memory_status", {}),
            "graph_status": pack.get("graph_status", {}),
        }
        ensure_public_safe(result, "brain_docs_archive_candidates")
        return result

    def brain_workflows_current(
        self,
        *,
        repository: str,
        branch: str,
        current_files: list[str],
        current_request: str,
        project: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        pack = self.brain_context_resolve(
            repository=repository,
            branch=branch,
            current_files=current_files,
            current_request=current_request,
            project=project,
            limit=limit,
        ).to_dict()
        contracts = _authority_workflow_contracts(pack)
        result = {
            "workflow_contracts": contracts,
            "auto_update_allowed": bool(contracts)
            and all(bool(contract.get("auto_update_allowed")) for contract in contracts),
            "memory_status": pack.get("memory_status", {}),
            "graph_status": pack.get("graph_status", {}),
        }
        ensure_public_safe(result, "brain_workflows_current")
        return result

    def brain_workflows_explain(
        self,
        *,
        rule: str,
        repository: str,
        branch: str,
        current_files: list[str],
        current_request: str,
        project: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        pack = self.brain_context_resolve(
            repository=repository,
            branch=branch,
            current_files=current_files,
            current_request=current_request,
            project=project,
            limit=limit,
        ).to_dict()
        target = public_safe_text(rule, max_chars=360)
        contract = next(
            (
                item
                for item in _authority_workflow_contracts(pack)
                if _workflow_rule_matches(str(item.get("rule") or ""), target)
            ),
            None,
        )
        result = {
            "workflow_contract": contract or {},
            "memory_status": pack.get("memory_status", {}),
            "graph_status": pack.get("graph_status", {}),
        }
        ensure_public_safe(result, "brain_workflows_explain")
        return result

    def brain_incident_search(self, *, symptom: str, project: str, limit: int = 5) -> dict[str, Any]:
        graph = self.graph_adapter.search_context(
            brain_id=f"/project/{project}",
            query=symptom,
            entity_types=["Incident", "Symptom", "Attempt", "Fix", "Verification"],
            limit=max(limit * 4, 10),
        )
        reusable, do_not_apply = _split_incident_lanes(_incident_records(graph), limit=limit)
        result = {
            "query": public_safe_text(symptom, max_chars=512),
            "reusable_fixes": reusable,
            "do_not_apply": do_not_apply,
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


def _authority_documents(pack: Mapping[str, Any]) -> list[dict[str, Any]]:
    authority = pack.get("authority") if isinstance(pack.get("authority"), Mapping) else {}
    documents = authority.get("documents") if isinstance(authority.get("documents"), list) else []
    return [dict(doc) for doc in documents if isinstance(doc, Mapping)]


def _authority_workflow_contracts(pack: Mapping[str, Any]) -> list[dict[str, Any]]:
    authority = pack.get("authority") if isinstance(pack.get("authority"), Mapping) else {}
    contracts = authority.get("workflow_contracts") if isinstance(authority.get("workflow_contracts"), list) else []
    return [dict(contract) for contract in contracts if isinstance(contract, Mapping)]


def _workflow_rule_matches(rule: str, target: str) -> bool:
    candidate = _normalize_workflow_rule(rule)
    wanted = _normalize_workflow_rule(target)
    if not candidate or not wanted:
        return False
    if candidate == wanted or wanted in candidate:
        return True
    candidate_terms = set(_terms(candidate))
    wanted_terms = set(_terms(wanted))
    return bool(wanted_terms and wanted_terms.issubset(candidate_terms))


def _normalize_workflow_rule(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_가-힣]+", " ", value).strip().casefold()


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
