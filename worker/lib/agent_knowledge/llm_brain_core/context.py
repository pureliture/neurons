from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Callable

from ._util import ensure_public_safe, hash_payload, public_safe_text
from .artifact_store import InMemorySessionMemoryArtifactStore, SessionMemoryArtifactStore
from .context_builder import (
    ContextPackBuilder,
    NON_CURRENT_AUTHORITY,
    artifact_field,
    artifact_summary,
    decision_view as _decision_view,
    incident_records as _incident_records,
    persona_view as _persona_view,
    split_incident_lanes as _split_incident_lanes,
    task_title,
    is_current_nonterminal,
)
from .document_bridge import DisabledDocumentBridge, DocumentBridge
from .graph import GraphMemoryAdapter, NullGraphMemoryAdapter
from .models import EvidenceRequest, GraphMemoryResult
from .objects.knowledge_objects import KnowledgeObjectEnvelope
from .objects.object_packs import (
    build_code_change_impact_pack,
    build_documentation_cleanup_pack,
    build_runtime_truth_pack,
)
from .objects.reference_corpus import default_corpus_policy_status
from .source_ref import SourceRefResolver
from .temporal import (
    TemporalSelector,
    TemporalSelectorError,
    validate_explicit_temporal_selector,
    parse_temporal_selector,
)

if TYPE_CHECKING:
    # `brain_context_resolve` is annotated `-> ContextPack`. With
    # `from __future__ import annotations` the annotation is lazy, so the symbol
    # is only needed by static type checkers, not at runtime.
    from .models import ContextPack


ACCEPTED_LIFECYCLE_STATES = {"accepted", "human_accepted", "auto_accepted"}
ACCEPTED_APPROVAL_STATES = {"approved", "auto_accepted"}
_SYNTHETIC_CANARY_PROVIDER = "lbrain-temporal-canary"


def _is_synthetic_canary_artifact(artifact: Any) -> bool:
    """Keep additive projection probes outside every user recall lane."""

    return (
        str(artifact_field(artifact, "provider") or "").strip().casefold()
        == _SYNTHETIC_CANARY_PROVIDER
    )


def _recall_safe_artifacts(artifacts: list[Any]) -> list[Any]:
    return [artifact for artifact in artifacts if not _is_synthetic_canary_artifact(artifact)]


def _is_synthetic_canary_episode(episode: Any) -> bool:
    payload = getattr(episode, "payload", None)
    if not isinstance(payload, Mapping):
        return True
    source_providers = payload.get("source_providers", ())
    if not isinstance(source_providers, (list, tuple, set, frozenset)):
        source_providers = (source_providers,)
    return any(
        str(provider or "").strip().casefold() == _SYNTHETIC_CANARY_PROVIDER
        for provider in (payload.get("provider"), *source_providers)
    )


def _recall_safe_graph_result(graph_result: GraphMemoryResult) -> GraphMemoryResult:
    episodes = tuple(
        episode
        for episode in graph_result.episodes
        if not _is_synthetic_canary_episode(episode)
    )
    if len(episodes) == len(graph_result.episodes):
        return graph_result
    return GraphMemoryResult(
        status=graph_result.status,
        episodes=episodes,
        details=graph_result.details,
    )


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
        artifacts = _recall_safe_artifacts(
            self.artifact_store.list_recent(project=project_name, limit=limit)
        )
        cards = _project_cards(self.memory_cards, project_name)
        query = " ".join([repository, branch, current_request, " ".join(current_files)])
        graph_result = _recall_safe_graph_result(
            self.graph_adapter.search_context(
                brain_id=brain_id,
                query=query,
                entity_types=["Task", "Decision", "Incident", "PersonaFact", "File", "SourceRef"],
                limit=limit,
            )
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
        graph = _recall_safe_graph_result(
            self.graph_adapter.search_context(
                brain_id=f"/project/{project}",
                query=query,
                entity_types=card_types,
                limit=bounded,
            )
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
        as_of: str = "",
        date_from: str = "",
        date_to: str = "",
    ) -> dict[str, Any]:
        project_name = project or project_from_repository(repository)
        validate_explicit_temporal_selector(
            as_of=as_of,
            date_from=date_from,
            date_to=date_to,
            route=route,
        )
        has_explicit_temporal_selector = bool(as_of or date_from or date_to)
        query_temporal_selector = (
            parse_temporal_selector(query=query)
            if not has_explicit_temporal_selector
            else None
        )
        if (
            query_temporal_selector is not None
            and route
            and route != "temporal_work_recall"
        ):
            raise TemporalSelectorError(
                "query temporal selectors require route temporal_work_recall"
            )
        selected_route = (
            "temporal_work_recall"
            if has_explicit_temporal_selector or query_temporal_selector is not None
            else route or _route_for_query(query)
        )
        route_source = (
            "temporal_selector"
            if has_explicit_temporal_selector and not route
            else "explicit"
            if route
            else "query_temporal_selector"
            if query_temporal_selector is not None
            else "inferred"
        )
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
        elif selected_route == "code_change_impact":
            object_pack = build_code_change_impact_pack(
                current_files=current_files,
                route=selected_route,
                consumer=consumer,
            )
        elif selected_route == "html_visualization_preference":
            object_pack = _html_visualization_preference_object_pack(
                pack,
                route=selected_route,
                consumer=consumer,
            )
        elif selected_route == "code_style_preference":
            object_pack = _context_authority_object_pack(
                pack,
                route=selected_route,
                pack_names=("preferences", "style"),
                consumer=consumer,
            )
        elif selected_route == "temporal_work_recall":
            selector = parse_temporal_selector(
                as_of=as_of,
                date_from=date_from,
                date_to=date_to,
                query=query,
            )
            selector_bounds = selector.to_audit_dict() if selector is not None else {}
            list_revisions = getattr(
                self.artifact_store,
                "list_observed_interval_revisions",
                None,
            )
            if not callable(list_revisions):
                # Compatibility for external adapters that have not adopted the
                # all-revisions seam yet. Built-in stores always provide it.
                list_revisions = self.artifact_store.list_observed_interval
            artifacts = _recall_safe_artifacts(
                list_revisions(
                    project=project_name,
                    observed_at_start=str(selector_bounds.get("start") or ""),
                    observed_at_end=str(selector_bounds.get("end") or ""),
                    limit=10000,
                )
                if selector is not None
                else []
            )
            object_pack = _temporal_work_object_pack(
                cards=_project_cards(self.memory_cards, project_name),
                artifacts=artifacts,
                selector=selector,
                project=project_name,
                query=query,
                route=selected_route,
                consumer=consumer,
                limit=min(max(int(limit or 20), 1), 50),
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
                route_source=route_source,
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
        graph = _recall_safe_graph_result(
            self.graph_adapter.search_context(
                brain_id=f"/project/{project}",
                query=symptom,
                entity_types=["Incident", "Symptom", "Attempt", "Fix", "Verification"],
                limit=max(limit * 4, 10),
            )
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
        graph = _recall_safe_graph_result(
            self.graph_adapter.search_context(
                brain_id=f"/project/{project}",
                query=incident_id,
                entity_types=["Incident", "Symptom", "Hypothesis", "Attempt", "Fix", "Verification"],
                limit=20,
            )
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
    if _is_code_change_impact_query(text):
        return "code_change_impact"
    if _is_html_visualization_preference_query(text):
        return "html_visualization_preference"
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


def _is_code_change_impact_query(text: str) -> bool:
    if "code change impact" in text:
        return True
    file_terms = ("파일", "file", "current file", "current_files", "repo path", "source path")
    change_terms = ("바꾸", "변경", "수정", "고치", "change", "edit", "touch", "modify")
    impact_terms = ("영향", "impact", "테스트", "test", "런타임", "runtime", "검증", "verify")
    return (
        any(token in text for token in file_terms)
        and any(token in text for token in change_terms)
        and any(token in text for token in impact_terms)
    )


def _is_html_visualization_preference_query(text: str) -> bool:
    visual_medium_terms = (
        "html",
        "visualization",
        "visualisation",
        "visual",
        "시각화",
    )
    preference_terms = ("preference", "선호", "review", "리뷰", "기준", "평가")
    return any(term in text for term in visual_medium_terms) and any(term in text for term in preference_terms)


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


def _temporal_work_object_pack(
    *,
    cards: list[dict[str, Any]],
    artifacts: list[Any],
    selector: TemporalSelector | None,
    project: str,
    query: str,
    route: str,
    consumer: str,
    limit: int,
) -> dict[str, Any]:
    """Return only WorkUnits backed by matching observed/event time evidence.

    Temporal recall intentionally does not merge ``current_work`` or
    ``required_verification`` packs. Those packs describe now; using them as a
    historical fallback is the correctness bug this route must fail closed on.
    """

    result = _empty_read_object_pack(route=route, consumer=consumer)
    if selector is None:
        result["gaps"].append("temporal_selector_required")
        result["confidence"] = {"score": 0.0, "basis": "temporal_evidence_absent"}
        result["audit"].update(
            {
                "object_pack_route_source": "temporal_evidence_filter",
                "temporal_candidate_count": 0,
                "temporal_missing_time_count": 0,
            }
        )
        return result

    relevance_terms = _temporal_relevance_terms(query, project=project)
    candidates: list[dict[str, Any]] = []
    non_authoritative_card_count = 0
    for card in cards:
        if str(card.get("card_type") or "").casefold() not in {"task", "work", "work_unit"}:
            continue
        typed_payload = card.get("typed_payload") if isinstance(card.get("typed_payload"), Mapping) else {}
        lane = _temporal_task_card_authority_lane(card, typed_payload)
        if not lane:
            non_authoritative_card_count += 1
            continue
        candidates.append(
            {
                "natural_key": str(card.get("memory_id") or card.get("title") or ""),
                "title": task_title(card),
                "content_hash": str(card.get("content_hash") or card.get("card_hash") or ""),
                "observed_at_start": str(card.get("observed_at_start") or card.get("observed_at") or ""),
                "observed_at_end": str(card.get("observed_at_end") or card.get("observed_at") or ""),
                "authority_lane": lane,
                "lifecycle_status": "current" if lane == "accepted_current" else "observed",
                "verification_state": "freshness_checked" if lane == "accepted_current" else "unverified",
                "review_state": "accepted",
                "confidence_score": 0.9 if lane == "accepted_current" else 0.55,
                "recommended_action": "resume_work" if lane == "accepted_current" else "",
                "source_kind": "memory_card",
                "source_object_type": "MemoryCard:task",
                # The object envelope has its own deterministic object_id. Keep
                # the canonical card identifier as a distinct provenance field
                # so the public brain.query compatibility adapter never swaps
                # its stable memory_id for a WorkUnit id.
                "source_memory_id": str(card.get("memory_id") or ""),
                "source_revision": str(card.get("source_revision") or card.get("content_hash") or ""),
                "relevance_text": " ".join(
                    str(value or "")
                    for value in (
                        card.get("title"),
                        card.get("summary"),
                        typed_payload.get("task_state"),
                        typed_payload.get("next_action"),
                    )
                ),
            }
        )
    for artifact in artifacts:
        revision_temporal_evidence = str(
            artifact_field(artifact, "revision_temporal_evidence") or "legacy"
        )
        artifact_observed_start = str(
            artifact_field(artifact, "revision_observed_at_start")
            if revision_temporal_evidence == "bounded"
            else artifact_field(artifact, "observed_at_start")
            or ""
        )
        artifact_observed_end = str(
            artifact_field(artifact, "revision_observed_at_end")
            if revision_temporal_evidence == "bounded"
            else artifact_field(artifact, "observed_at_end")
            or ""
        )
        candidates.append(
            {
                "natural_key": str(artifact_field(artifact, "artifact_id") or artifact_summary(artifact)),
                "title": artifact_summary(artifact),
                "content_hash": str(artifact_field(artifact, "content_hash") or ""),
                "observed_at_start": artifact_observed_start,
                "observed_at_end": artifact_observed_end,
                "observed_intervals": tuple(
                    tuple(interval)
                    for interval in (
                        artifact_field(artifact, "revision_observed_intervals")
                        or ()
                    )
                    if isinstance(interval, (list, tuple)) and len(interval) == 2
                ),
                "temporal_term_bindings": tuple(
                    (
                        str(binding[0]),
                        str(binding[1]),
                        tuple(binding[2]),
                    )
                    for binding in (
                        artifact_field(
                            artifact, "revision_temporal_term_bindings"
                        )
                        or ()
                    )
                    if isinstance(binding, (list, tuple))
                    and len(binding) == 3
                    and isinstance(binding[2], (list, tuple))
                ),
                "temporal_evidence_kind": revision_temporal_evidence,
                "confidence_score": (
                    0.7 if revision_temporal_evidence == "bounded" else 0.0
                ),
                "authority_lane": "reference_only",
                "source_kind": "session_memory_artifact",
                "source_object_type": "SessionMemoryArtifact",
                "source_revision": str(artifact_field(artifact, "source_revision") or ""),
                "relevance_text": artifact_summary(artifact),
                "relevance_hashes": tuple(
                    artifact_field(artifact, "search_term_hashes") or ()
                ),
                # Internal-only grouping/currentness fields. They are consumed
                # before KnowledgeObjectEnvelope construction and never emitted.
                "session_group_key": str(
                    artifact_field(artifact, "session_id_hash") or ""
                ),
                "artifact_currentness": (
                    int(artifact_field(artifact, "materialization_revision") or 0),
                    str(artifact_field(artifact, "materialized_at") or ""),
                    str(artifact_field(artifact, "source_revision") or ""),
                    str(artifact_field(artifact, "created_at") or ""),
                    str(artifact_field(artifact, "artifact_id") or ""),
                ),
            }
        )

    missing_time_count = 0
    irrelevant_count = 0
    matched: list[dict[str, Any]] = []
    for candidate in candidates:
        if (
            candidate.get("source_kind") == "session_memory_artifact"
            and candidate.get("temporal_evidence_kind") != "bounded"
        ):
            # Cumulative/legacy session bounds cannot prove which revision was
            # observed at the requested event time. Keep temporal recall closed
            # until the bounded metadata rebuild supplies revision evidence.
            missing_time_count += 1
            continue
        observed_intervals = tuple(candidate.get("observed_intervals") or ())
        if observed_intervals:
            matching_intervals = [
                (str(interval[0]), str(interval[1]))
                for interval in observed_intervals
                if selector.matches(
                    observed_at_start=str(interval[0]),
                    observed_at_end=str(interval[1]),
                )
            ]
            if not matching_intervals:
                continue
            temporal_term_bindings = tuple(
                candidate.get("temporal_term_bindings") or ()
            )
            if candidate.get("source_kind") == "session_memory_artifact":
                if temporal_term_bindings:
                    matching_bindings = [
                        binding
                        for binding in temporal_term_bindings
                        if selector.matches(
                            observed_at_start=str(binding[0]),
                            observed_at_end=str(binding[1]),
                        )
                    ]
                    if not matching_bindings:
                        continue
                    candidate = {
                        **candidate,
                        "relevance_hashes": tuple(
                            sorted(
                                {
                                    str(term_hash)
                                    for binding in matching_bindings
                                    for term_hash in binding[2]
                                }
                            )
                        ),
                    }
                elif len(observed_intervals) != 1:
                    # Old multi-interval artifacts cannot prove which term
                    # belongs to which interval.  They remain readable through
                    # non-temporal lanes but fail closed here until rebuilt.
                    missing_time_count += 1
                    continue
            selected_interval = max(matching_intervals)
            candidate = {
                **candidate,
                "observed_at_start": selected_interval[0],
                "observed_at_end": selected_interval[1],
            }
        observed_start = str(candidate.get("observed_at_start") or "")
        observed_end = str(candidate.get("observed_at_end") or "")
        if not observed_start and not observed_end:
            missing_time_count += 1
            continue
        if not observed_intervals and not selector.matches(
            observed_at_start=observed_start,
            observed_at_end=observed_end,
        ):
            continue
        if relevance_terms and not _temporal_candidate_is_relevant(
            str(candidate.get("relevance_text") or ""),
            relevance_terms,
            tuple(candidate.get("relevance_hashes") or ()),
        ):
            irrelevant_count += 1
            continue
        matched.append(candidate)
    # Multiple bounded revisions of one session can overlap the same date. Pick
    # the latest revision only *after* semantic relevance is known; collapsing in
    # the store first lets a newer unrelated revision hide an older relevant one.
    latest_relevant_by_session: dict[str, dict[str, Any]] = {}
    ungrouped: list[dict[str, Any]] = []
    for candidate in matched:
        session_group_key = str(candidate.get("session_group_key") or "")
        if not session_group_key:
            ungrouped.append(candidate)
            continue
        existing = latest_relevant_by_session.get(session_group_key)
        if existing is None or tuple(candidate.get("artifact_currentness") or ()) > tuple(
            existing.get("artifact_currentness") or ()
        ):
            latest_relevant_by_session[session_group_key] = candidate
    matched = [*ungrouped, *latest_relevant_by_session.values()]
    matched.sort(
        key=lambda item: (
            str(item.get("observed_at_start") or item.get("observed_at_end") or ""),
            str(item.get("natural_key") or ""),
        ),
        reverse=True,
    )

    for candidate in matched[: max(1, int(limit))]:
        lane = str(candidate["authority_lane"])
        content_hash = str(candidate.get("content_hash") or "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", content_hash):
            content_hash = hash_payload(
                [
                    "temporal_work_recall",
                    candidate.get("natural_key"),
                    candidate.get("observed_at_start"),
                    candidate.get("observed_at_end"),
                ]
            )
        observed_at = str(candidate.get("observed_at_start") or candidate.get("observed_at_end") or "")
        obj = KnowledgeObjectEnvelope.from_parts(
            object_type="WorkUnit",
            natural_key=str(candidate.get("natural_key") or candidate.get("title") or ""),
            scope={"project": project},
            title=str(candidate.get("title") or ""),
            summary=str(candidate.get("title") or ""),
            lifecycle_status=str(candidate.get("lifecycle_status") or "observed"),
            authority_lane=lane,
            verification_state=str(candidate.get("verification_state") or "unverified"),
            review_state=str(candidate.get("review_state") or "not_required"),
            content_hash=content_hash,
            observed_at=observed_at,
            confidence={
                "score": float(candidate.get("confidence_score") or 0.0),
                "basis": "matching_temporal_evidence",
            },
            recommended_action=str(candidate.get("recommended_action") or ""),
            freshness={"status": "event_time_matched"},
            payload={
                "source_kind": candidate.get("source_kind"),
                "source_object_type": candidate.get("source_object_type"),
                "source_memory_id": candidate.get("source_memory_id"),
                "observed_at_start": candidate.get("observed_at_start"),
                "observed_at_end": candidate.get("observed_at_end"),
                "source_revision": candidate.get("source_revision"),
            },
        ).to_dict()
        obj["valid_from"] = str(candidate.get("observed_at_start") or "")
        obj["valid_to"] = str(candidate.get("observed_at_end") or "")
        result["objects"].append(obj)
        result["lanes"][lane].append(obj)
        action = str(candidate.get("recommended_action") or "")
        if action:
            result["recommended_actions"].append({"object_id": obj["object_id"], "action": action})

    if not result["objects"]:
        if irrelevant_count:
            result["gaps"].append("temporal_evidence_no_relevant_match")
        else:
            result["gaps"].append(
                "temporal_evidence_missing" if missing_time_count else "temporal_evidence_no_match"
            )
        result["confidence"] = {"score": 0.0, "basis": "temporal_evidence_absent_or_mismatched"}
    else:
        if (
            result["lanes"]["accepted_non_current"]
            and not result["lanes"]["accepted_current"]
            and not result["lanes"]["reference_only"]
        ):
            result["gaps"].append("temporal_current_authority_missing")
        result["confidence"] = {
            "score": min(float(obj.get("confidence", {}).get("score") or 0.0) for obj in result["objects"]),
            "basis": "matching_observed_event_time",
        }
    result["audit"].update(
        {
            "object_pack_route_source": "temporal_evidence_filter",
            "temporal_selector": selector.to_audit_dict(),
            "temporal_candidate_count": len(candidates),
            "temporal_match_count": len(result["objects"]),
            "temporal_missing_time_count": missing_time_count,
            "temporal_irrelevant_count": irrelevant_count,
            "temporal_relevance_term_count": len(relevance_terms),
            "temporal_non_authoritative_card_count": non_authoritative_card_count,
        }
    )
    ensure_public_safe(result, "TemporalWorkObjectPack")
    return result


_TEMPORAL_QUERY_GENERIC_TERMS = frozenset(
    {
        "am",
        "are",
        "as",
        "be",
        "been",
        "being",
        "can",
        "could",
        "date",
        "did",
        "do",
        "does",
        "from",
        "had",
        "has",
        "have",
        "history",
        "how",
        "on",
        "is",
        "may",
        "might",
        "must",
        "recall",
        "repo",
        "repository",
        "result",
        "session",
        "shall",
        "should",
        "temporal",
        "the",
        "to",
        "today",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "whose",
        "why",
        "will",
        "work",
        "would",
        "yesterday",
        "그날",
        "결과",
        "기억",
        "날짜",
        "당시",
        "무엇",
        "봐야",
        "세션",
        "시간",
        "어제",
        "오늘",
        "작업",
        "재개",
        "재개하려면",
        "했나",
        "했다",
        "했어",
        "해야",
        "해",
        "회상",
        "was",
        "were",
    }
)


def _temporal_relevance_terms(query: str, *, project: str) -> set[str]:
    without_dates = re.sub(
        r"(?<!\d)\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[Zz]|[+-]\d{2}:?\d{2})?)?(?!\d)",
        " ",
        str(query or ""),
    )
    project_terms = {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9가-힣_-]+", str(project or ""))
        if len(token) > 1
    }
    return {
        token
        for token in (
            _temporal_query_term(value)
            for value in re.findall(r"[A-Za-z0-9가-힣_-]+", without_dates)
            if len(value) > 1
        )
        if token
        if token not in _TEMPORAL_QUERY_GENERIC_TERMS
        and token not in project_terms
        and not token.isdigit()
    }


def _temporal_query_term(value: str) -> str:
    token = value.casefold()
    for suffix in ("으로", "에서", "에게", "한테", "까지", "부터", "처럼", "보다", "로"):
        if token.endswith(suffix) and len(token) > len(suffix) + 1:
            return token[: -len(suffix)]
    for suffix in ("ment", "ing", "ed"):
        if token.endswith(suffix) and len(token) > len(suffix) + 3:
            return token[: -len(suffix)]
    return token


def _temporal_candidate_is_relevant(
    text: str,
    terms: set[str],
    term_hashes: tuple[str, ...] = (),
) -> bool:
    candidate_terms = {
        token
        for token in (
            _temporal_query_term(value)
            for value in re.findall(r"[A-Za-z0-9가-힣_-]+", str(text or ""))
            if len(value) > 1
        )
        if token
    }
    hashed = set(term_hashes)
    matched_count = sum(
        1 for term in terms if term in candidate_terms or hash_payload(term) in hashed
    )
    if len(terms) <= 1:
        return matched_count == len(terms)
    required_count = max(2, (len(terms) * 3 + 4) // 5)
    return matched_count >= required_count


def _html_visualization_preference_object_pack(
    pack: Mapping[str, Any],
    *,
    route: str,
    consumer: str,
) -> dict[str, Any]:
    merged = _context_authority_object_pack(
        pack,
        route=route,
        pack_names=("preferences", "style"),
        consumer=consumer,
    )
    relevant_ids = {
        str(obj.get("object_id") or "")
        for obj in merged.get("objects", [])
        if isinstance(obj, Mapping) and _is_html_visualization_preference_object(obj)
    }
    merged["objects"] = [
        dict(obj)
        for obj in merged.get("objects", [])
        if isinstance(obj, Mapping) and str(obj.get("object_id") or "") in relevant_ids
    ]
    lanes = merged.get("lanes") if isinstance(merged.get("lanes"), Mapping) else {}
    merged["lanes"] = {
        str(lane): [
            dict(obj)
            for obj in lane_objects
            if isinstance(obj, Mapping) and str(obj.get("object_id") or "") in relevant_ids
        ]
        for lane, lane_objects in lanes.items()
        if isinstance(lane_objects, list)
    }
    merged["recommended_actions"] = [
        dict(action)
        for action in merged.get("recommended_actions", [])
        if isinstance(action, Mapping) and str(action.get("object_id") or "") in relevant_ids
    ]
    merged["audit"] = {
        **dict(merged.get("audit") or {}),
        "object_pack_route_source": "html_visualization_preference_pack",
    }
    if not relevant_ids:
        merged["gaps"] = [
            gap
            for gap in merged.get("gaps", [])
            if str(gap or "") != "context_authority_object_pack_empty"
        ]
        for gap in ("accepted_html_preference_missing", "visualization_preference_missing"):
            if gap not in merged["gaps"]:
                merged["gaps"].append(gap)
    merged["confidence"] = {
        "score": 0.74 if relevant_ids else 0.0,
        "basis": "html_visualization_preference_route",
    }
    ensure_public_safe(merged, "HtmlVisualizationPreferenceObjectPack")
    return merged


def _is_html_visualization_preference_object(obj: Mapping[str, Any]) -> bool:
    payload = obj.get("payload") if isinstance(obj.get("payload"), Mapping) else {}
    text = " ".join(
        [
            str(obj.get("title") or ""),
            str(obj.get("summary") or ""),
            str(payload.get("scope") or ""),
            str(payload.get("applies_to") or ""),
        ]
    ).lower()
    return any(
        marker in text
        for marker in (
            "html",
            "review artifact",
            "visualization",
            "visualisation",
            "visual artifact",
            "시각화",
        )
    )


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
    route_source: str,
) -> dict[str, Any]:
    view = dict(pack)
    wanted = {item for item in object_types if item}
    if wanted:
        pre_filter_objects = [
            obj for obj in view.get("objects", []) if isinstance(obj, Mapping)
        ]
        objects = [
            dict(obj)
            for obj in pre_filter_objects
            if str(obj.get("object_type")) in wanted
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
        if (
            str(view.get("route") or "") == "temporal_work_recall"
            and pre_filter_objects
            and not objects
        ):
            gaps = [str(gap) for gap in view.get("gaps", []) if str(gap or "")]
            filter_gap = "temporal_object_type_filter_no_matching_evidence"
            if filter_gap not in gaps:
                gaps.append(filter_gap)
            view["gaps"] = gaps
            view["confidence"] = {
                "score": 0.0,
                "basis": "temporal_object_type_filter_no_match",
            }
            temporal_audit = dict(view.get("audit") or {})
            temporal_audit["temporal_pre_filter_match_count"] = len(pre_filter_objects)
            temporal_audit["temporal_match_count"] = 0
            view["audit"] = temporal_audit
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
    view["route_trace"] = _route_trace_for_view(view, route_source=route_source)
    return view


def _route_trace_for_view(view: Mapping[str, Any], *, route_source: str) -> dict[str, Any]:
    missing_evidence = _missing_evidence_gaps(view.get("gaps"))
    trace = {
        "schema_version": "object_query_route_trace.v1",
        "route": public_safe_text(str(view.get("route") or ""), max_chars=120),
        "route_source": "explicit" if route_source == "explicit" else "inferred",
        "selected_source_lanes": _selected_source_lanes(view.get("lanes")),
        "confidence": dict(view.get("confidence") or {}),
        "stop_reason": _route_stop_reason(view, missing_evidence=missing_evidence),
        "missing_evidence": missing_evidence,
    }
    ensure_public_safe(trace, "ObjectQueryRouteTrace")
    return trace


def _selected_source_lanes(lanes: Any) -> list[str]:
    if not isinstance(lanes, Mapping):
        return []
    selected = [
        public_safe_text(str(lane), max_chars=80)
        for lane, lane_objects in lanes.items()
        if isinstance(lane_objects, list) and lane_objects
    ]
    return sorted(lane for lane in selected if lane)


def _missing_evidence_gaps(gaps: Any) -> list[str]:
    if not isinstance(gaps, list):
        return []
    markers = ("evidence", "unverified", "freshness", "missing")
    missing: list[str] = []
    for gap in gaps:
        text = public_safe_text(str(gap or ""), max_chars=180)
        if text and any(marker in text for marker in markers) and text not in missing:
            missing.append(text)
    return missing


def _route_stop_reason(view: Mapping[str, Any], *, missing_evidence: list[str]) -> str:
    if missing_evidence:
        return "missing_evidence_gap_returned"
    objects = view.get("objects") if isinstance(view.get("objects"), list) else []
    actions = view.get("recommended_actions") if isinstance(view.get("recommended_actions"), list) else []
    gaps = view.get("gaps") if isinstance(view.get("gaps"), list) else []
    if objects or actions:
        return "returned_object_pack"
    if gaps:
        return "gap_only_response"
    return "empty_object_pack"


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


def _temporal_task_card_authority_lane(
    card: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> str:
    if not _is_accepted_card(card):
        return ""
    currentness = str(card.get("currentness") or "").casefold()
    if currentness == "current" and is_current_nonterminal(card, payload):
        return "accepted_current"
    if currentness in NON_CURRENT_AUTHORITY or (
        currentness == "current" and not is_current_nonterminal(card, payload)
    ):
        return "accepted_non_current"
    return ""


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
