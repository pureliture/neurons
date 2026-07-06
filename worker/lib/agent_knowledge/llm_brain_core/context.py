from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Callable

from ._util import ensure_public_safe, public_safe_text
from .artifact_store import InMemorySessionMemoryArtifactStore, SessionMemoryArtifactStore
from .context_builder import (
    ContextPackBuilder,
    NON_CURRENT_AUTHORITY,
    decision_view as _decision_view,
    incident_records as _incident_records,
    persona_view as _persona_view,
    split_incident_lanes as _split_incident_lanes,
)
from .document_bridge import DisabledDocumentBridge, DocumentBridge
from .graph import GraphMemoryAdapter, NullGraphMemoryAdapter
from .models import EvidenceRequest
from .objects.object_packs import build_documentation_cleanup_pack, build_runtime_truth_pack
from .objects.reference_corpus import default_corpus_policy_status
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
        search_mirror_status: Mapping[str, Any] | None = None,
        reference_corpus_status_reader: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.artifact_store = artifact_store or InMemorySessionMemoryArtifactStore()
        self.memory_cards = [dict(card) for card in memory_cards or [] if _is_accepted_card(card)]
        self.graph_adapter = graph_adapter or NullGraphMemoryAdapter()
        self.source_resolver = source_resolver or SourceRefResolver()
        self.document_bridge = document_bridge or DisabledDocumentBridge()
        self.search_mirror_status = dict(search_mirror_status or {})
        self.reference_corpus_status_reader = reference_corpus_status_reader
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
        project_name = project or project_from_repository(repository)
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
            search_mirror_status=self.search_mirror_status,
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

    def brain_objects_query(
        self,
        *,
        repository: str,
        branch: str,
        query: str,
        current_files: list[str],
        project: str | None = None,
        object_types: list[str] | None = None,
        route: str = "",
        limit: int = 20,
        response_mode: str = "full",
        consumer: str = "unspecified",
    ) -> dict[str, Any]:
        project_name = project or project_from_repository(repository)
        selected_route = route or _route_for_query(query)
        pack = self.brain_context_resolve(
            repository=repository,
            branch=branch,
            current_files=current_files,
            current_request=query,
            project=project_name,
            limit=min(max(int(limit or 20), 1), 20),
            consumer=consumer,
        ).to_dict()
        if selected_route == "documentation_cleanup":
            object_pack = build_documentation_cleanup_pack(
                documents=_authority_documents(pack),
                route=selected_route,
                consumer=consumer,
            )
        elif selected_route == "deployment_runtime_truth":
            object_pack = build_runtime_truth_pack(
                pull_request={"id": _query_object_ref(query), "merged": False},
                deployment={"target": "production"},
                live_evidence=None,
            )
            object_pack["audit"] = {
                "request_hash": pack.get("audit", {}).get("request_hash", ""),
                "consumer": consumer,
                "object_pack_route_source": "runtime_truth_pack",
            }
        elif selected_route == "code_style_preference":
            object_pack = _context_authority_object_pack(
                pack,
                route=selected_route,
                pack_names=("preferences", "style"),
                consumer=consumer,
            )
        elif selected_route == "temporal_work_recall":
            object_pack = _context_authority_object_pack(
                pack,
                route=selected_route,
                pack_names=("current_work", "required_verification"),
                consumer=consumer,
            )
        else:
            object_pack = _context_authority_object_pack(
                pack,
                route=selected_route,
                pack_names=(
                    "documentation_cleanup",
                    "reference_corpus",
                    "preferences",
                    "style",
                    "current_work",
                    "required_verification",
                    "do_not_touch_boundaries",
                ),
                consumer=consumer,
            )
        result = {
            "schema_version": "brain_objects_query.v1",
            "route": selected_route,
            "response_mode": response_mode if response_mode in {"full", "compact", "degraded"} else "full",
            "object_pack": _object_pack_view(
                object_pack,
                object_types=[str(item) for item in object_types or []],
                response_mode=response_mode,
            ),
        }
        ensure_public_safe(result, "brain_objects_query")
        return result

    def brain_object_explain(
        self,
        *,
        object_id: str,
        include_edges: bool = True,
        include_evidence: bool = True,
        response_mode: str = "full",
    ) -> dict[str, Any]:
        result = {
            "schema_version": "brain_object_explain.v1",
            "object_id": public_safe_text(object_id, max_chars=180),
            "response_mode": response_mode if response_mode in {"full", "compact", "degraded"} else "full",
            "object": {},
            "edges": [] if include_edges else [],
            "evidence": [] if include_evidence else [],
            "gaps": ["object_store_not_configured"],
        }
        ensure_public_safe(result, "brain_object_explain")
        return result

    def brain_corpus_status(self, *, corpus_id: str = "", project: str = "", limit: int = 20) -> dict[str, Any]:
        if self.reference_corpus_status_reader is not None:
            result = self.reference_corpus_status_reader(corpus_id=corpus_id, project=project, limit=limit)
            ensure_public_safe(result, "brain_corpus_status")
            return result
        result = {
            "schema_version": "brain_corpus_status.v1",
            "corpus_id": public_safe_text(corpus_id, max_chars=180),
            "project": public_safe_text(project, max_chars=120),
            "source_count": 0,
            "storage_modes": {},
            "reference_object_count": 0,
            "freshness_gaps": [],
            "limit": min(max(int(limit or 20), 1), 100),
            **default_corpus_policy_status(),
            "gaps": ["reference_corpus_store_empty"],
        }
        ensure_public_safe(result, "brain_corpus_status")
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
        # 현재-권위가 아닌(stale/superseded/archive_candidate) 카드는 persona fact 에서 제외한다.
        cards = [card for card in cards if str(card.get("currentness") or "") not in NON_CURRENT_AUTHORITY]
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
        # 현재-권위가 아닌 카드는 current fact/conflict 로 보지 않는다(drift 판정은 위에서 전체 카드 기준).
        current_cards = [card for card in cards if str(card.get("currentness") or "") not in NON_CURRENT_AUTHORITY]
        conflicts = [_persona_view(card) for card in current_cards if _persona_conflicts(card, plan)]
        if conflicts:
            status = "possible_conflict"
        result = {
            "status": status,
            "facts": [_persona_view(card) for card in current_cards],
            "conflicts": conflicts,
            "memory_status": {"status": "available", "authority": "canonical_card"},
        }
        ensure_public_safe(result, "brain_persona_check")
        return result

    def brain_evidence_get(self, request: EvidenceRequest) -> dict[str, Any]:
        return self.source_resolver.resolve(request).to_dict()


def project_from_repository(repository: str) -> str:
    value = str(repository or "").rstrip("/")
    if not value:
        return "unknown"
    name = value.split("/")[-1]
    return name.removesuffix(".git") or "unknown"


_project_from_repository = project_from_repository


def _route_for_query(query: str) -> str:
    text = str(query or "").lower()
    if any(
        token in text
        for token in (
            "어제",
            "오늘",
            "세션",
            "작업",
            "뭐 했",
            "무엇 했",
            "yesterday",
            "today",
            "session",
            "work unit",
            "current work",
            "unfinished",
            "handoff",
            "resume",
        )
    ):
        return "temporal_work_recall"
    if "문서" in text or "doc" in text or "stale" in text or "archive" in text:
        return "documentation_cleanup"
    if "merge" in text or "배포" in text or "deploy" in text:
        return "deployment_runtime_truth"
    if "style" in text or "스타일" in text or "preference" in text or "선호" in text:
        return "code_style_preference"
    return "authority_archive_separation"


def _authority_documents(pack: Mapping[str, Any]) -> list[dict[str, Any]]:
    authority = pack.get("authority") if isinstance(pack.get("authority"), Mapping) else {}
    documents = authority.get("documents") if isinstance(authority.get("documents"), list) else []
    return [dict(doc) for doc in documents if isinstance(doc, Mapping)]


def _context_authority_object_pack(
    pack: Mapping[str, Any],
    *,
    route: str,
    pack_names: tuple[str, ...],
    consumer: str,
) -> dict[str, Any]:
    authority = pack.get("authority") if isinstance(pack.get("authority"), Mapping) else {}
    object_packs = authority.get("object_packs") if isinstance(authority.get("object_packs"), Mapping) else {}
    merged = _empty_read_object_pack(route=route, consumer=consumer)
    seen_objects: set[str] = set()
    seen_edges: set[str] = set()
    seen_evidence: set[str] = set()
    seen_actions: set[tuple[str, str]] = set()
    for name in pack_names:
        source = object_packs.get(name) if isinstance(object_packs.get(name), Mapping) else {}
        _merge_object_pack(
            merged,
            source,
            seen_objects=seen_objects,
            seen_edges=seen_edges,
            seen_evidence=seen_evidence,
            seen_actions=seen_actions,
        )
    if not merged["objects"]:
        merged["gaps"].append("context_authority_object_pack_empty")
    merged["confidence"] = {
        "score": 0.75 if merged["objects"] else 0.0,
        "basis": "context_authority_object_packs",
    }
    merged["audit"] = {
        "request_hash": str((pack.get("audit") or {}).get("request_hash") or ""),
        "consumer": consumer,
        "object_pack_route_source": "context_authority_object_packs",
        "source_pack_names": list(pack_names),
    }
    ensure_public_safe(merged, "ContextAuthorityObjectPack")
    return merged


def _empty_read_object_pack(*, route: str, consumer: str) -> dict[str, Any]:
    return {
        "schema_version": "object_pack.v1",
        "route": route,
        "objects": [],
        "edges": [],
        "evidence": [],
        "lanes": {
            "accepted_current": [],
            "accepted_non_current": [],
            "candidate": [],
            "reference_only": [],
            "proposal_only": [],
            "archive_only": [],
            "derived_projection": [],
            "rejected": [],
        },
        "verification": {"runtime_verified": [], "runtime_unverified": [], "unverified": []},
        "recommended_actions": [],
        "confidence": {"score": 0.0, "basis": ""},
        "gaps": [],
        "audit": {"consumer": consumer},
    }


def _merge_object_pack(
    target: dict[str, Any],
    source: Mapping[str, Any],
    *,
    seen_objects: set[str],
    seen_edges: set[str],
    seen_evidence: set[str],
    seen_actions: set[tuple[str, str]],
) -> None:
    for obj in source.get("objects", []) if isinstance(source.get("objects"), list) else []:
        if not isinstance(obj, Mapping):
            continue
        object_id = str(obj.get("object_id") or "")
        if object_id and object_id in seen_objects:
            continue
        if object_id:
            seen_objects.add(object_id)
        safe_obj = dict(obj)
        target["objects"].append(safe_obj)
        lane = str(safe_obj.get("authority_lane") or "reference_only")
        target["lanes"].setdefault(lane, []).append(safe_obj)
    for edge in source.get("edges", []) if isinstance(source.get("edges"), list) else []:
        if not isinstance(edge, Mapping):
            continue
        edge_id = str(edge.get("edge_id") or "")
        if edge_id and edge_id in seen_edges:
            continue
        if edge_id:
            seen_edges.add(edge_id)
        target["edges"].append(dict(edge))
    for evidence in source.get("evidence", []) if isinstance(source.get("evidence"), list) else []:
        if not isinstance(evidence, Mapping):
            continue
        evidence_id = str(evidence.get("evidence_id") or "")
        if evidence_id and evidence_id in seen_evidence:
            continue
        if evidence_id:
            seen_evidence.add(evidence_id)
        target["evidence"].append(dict(evidence))
    verification = source.get("verification") if isinstance(source.get("verification"), Mapping) else {}
    for lane in ("runtime_verified", "runtime_unverified", "unverified"):
        values = verification.get(lane) if isinstance(verification.get(lane), list) else []
        target["verification"].setdefault(lane, []).extend(dict(item) for item in values if isinstance(item, Mapping))
    for action in source.get("recommended_actions", []) if isinstance(source.get("recommended_actions"), list) else []:
        if not isinstance(action, Mapping):
            continue
        key = (str(action.get("object_id") or ""), str(action.get("action") or ""))
        if key in seen_actions:
            continue
        seen_actions.add(key)
        target["recommended_actions"].append(dict(action))
    for gap in source.get("gaps", []) if isinstance(source.get("gaps"), list) else []:
        safe_gap = str(gap or "")
        if safe_gap and safe_gap not in target["gaps"]:
            target["gaps"].append(safe_gap)


def _query_object_ref(query: str) -> str:
    return "query:" + public_safe_text(str(query or "runtime_truth"), max_chars=80)


def _object_pack_view(
    pack: Mapping[str, Any],
    *,
    object_types: list[str],
    response_mode: str,
) -> dict[str, Any]:
    view = dict(pack)
    wanted = {item for item in object_types if item}
    if wanted:
        objects = [
            dict(obj)
            for obj in view.get("objects", [])
            if isinstance(obj, Mapping) and str(obj.get("object_type")) in wanted
        ]
        object_ids = {str(obj.get("object_id")) for obj in objects}
        view["objects"] = objects
        lanes = view.get("lanes") if isinstance(view.get("lanes"), Mapping) else {}
        view["lanes"] = {
            str(lane): [
                dict(obj)
                for obj in lane_objects
                if isinstance(obj, Mapping) and str(obj.get("object_id")) in object_ids
            ]
            for lane, lane_objects in lanes.items()
            if isinstance(lane_objects, list)
        }
        view["recommended_actions"] = [
            dict(action)
            for action in view.get("recommended_actions", [])
            if isinstance(action, Mapping) and str(action.get("object_id")) in object_ids
        ]
    audit = dict(view.get("audit") or {})
    audit["object_type_filter"] = sorted(wanted)
    view["audit"] = audit
    mode = response_mode if response_mode in {"full", "compact", "degraded"} else "full"
    if mode != "full":
        view["objects"] = [_compact_object(obj) for obj in view.get("objects", []) if isinstance(obj, Mapping)]
        lanes = view.get("lanes") if isinstance(view.get("lanes"), Mapping) else {}
        view["lanes"] = {
            str(lane): [_compact_object(obj) for obj in lane_objects if isinstance(obj, Mapping)]
            for lane, lane_objects in lanes.items()
            if isinstance(lane_objects, list)
        }
    view["response_mode"] = mode
    return view


def _compact_object(obj: Mapping[str, Any]) -> dict[str, Any]:
    keep = [
        "object_id",
        "object_type",
        "title",
        "lifecycle_status",
        "authority_lane",
        "verification_state",
        "review_state",
        "recommended_action",
    ]
    return {key: obj[key] for key in keep if key in obj}


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
