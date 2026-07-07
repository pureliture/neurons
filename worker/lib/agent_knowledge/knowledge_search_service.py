from __future__ import annotations

import copy
import sqlite3
from collections.abc import Mapping
from typing import Any

from .ledger import Ledger
from .llm_brain_core.document_bridge import RetiredIndexBridgeDocumentBridge
from .llm_brain_core.graph import GraphMemoryAdapter
from .llm_brain_core.ledger_adapter import LedgerSessionMemoryArtifactStore, LedgerSourceRefCatalog
from .llm_brain_core.runtime import build_runtime_brain_service
from .llm_brain_core.objects.extraction_pipeline import run_source_to_candidate_graph_activation_preview
from .llm_brain_core.objects.object_packs import apply_approval_board_decisions, apply_candidate_review_edits
from .llm_brain_core.objects.runtime_readiness import (
    build_source_to_candidate_runtime_collected_shadow_evidence_packet,
    build_source_to_candidate_runtime_evidence_collection_plan,
    build_source_to_candidate_runtime_evidence_packet_template,
    build_source_to_candidate_runtime_post_deploy_capture_packet,
    build_source_to_candidate_runtime_post_deploy_capture_readiness_report,
    build_source_to_candidate_runtime_readiness_report,
    build_source_to_candidate_runtime_shadow_evidence_packet,
    build_source_to_candidate_runtime_shadow_readiness_report,
)
from .memory_read_pipeline import AuthorizedMemoryReader, MemoryReadPipeline, MemorySearchQuery
from .index_client import RetiredIndexBridgeHttpClient
from .public_safe_util import ensure_public_safe, public_safe_text
from .session_memory.brain_query import resolve_brain_ids, run_brain_query_v2
from .session_memory.brain_read_model import LegacyLedgerBrainReadModel, build_semantic_recall


class DisabledRetiredIndexBridgeClient:
    def retrieve(self, *args, **kwargs) -> list[dict]:
        return []

    def search_messages(self, *args, **kwargs) -> dict:
        return {"status_code": 200, "json": {"code": 0, "data": []}}


def build_index_client(
    *,
    index_url: str = "",
    token: str = "",
    policy_proxy_url: str = "",
) -> DisabledRetiredIndexBridgeClient:
    _ = (index_url, token, policy_proxy_url)
    return DisabledRetiredIndexBridgeClient()


class _SessionCardCache:
    """세션 안에서 승인된 MemoryCard를 (project, limit) 단위로 스냅샷한다.

    기존 brain tool 호출은 read model을 매번 다시 만들고 ledger에
    `list_accepted_cards`(승인 카드 전체 reload, limit=100)를 다시 질의했다.
    단일 stdio MCP 세션에서는 승인 카드 집합이 충분히 안정적이므로, 세션 생명주기
    동안 결과를 메모이즈해 반복 호출을 (project, limit)별 ledger read 1회로 줄인다.
    실제 read model을 감싸며 나머지 read path는 그대로 전달하므로 graph 상태,
    evidence policy, 다른 조회 경로는 건드리지 않는다.

    stale 범위: 현재 노출된 brain tool은 모두 read-only이고 세션 내부 write path가
    없으므로 세션 동안 스냅샷은 유효하다. 다른 프로세스(worker/ingestion)가 같은
    ledger에 쓰는 변경은 세션 재시작 전까지 반영되지 않는다. cross-process 또는 TTL
    invalidation은 없다. `invalidate()`는 향후 세션 내부 write path가 생기면 호출할
    명시적 refresh seam이다. production wrapper인 `invalidate_brain_card_cache`는
    아직 production caller가 없고, 현재는 테스트에서만 닿는다.
    """

    def __init__(self, read_model) -> None:
        self._read_model = read_model
        self._cards: dict[tuple[str, int], list[dict]] = {}

    def list_accepted_cards(self, *, project: str, limit: int) -> list[dict]:
        key = (str(project), int(limit))
        cached = self._cards.get(key)
        if cached is None:
            cached = self._read_model.list_accepted_cards(project=project, limit=limit)
            self._cards[key] = cached
        # downstream consumer가 list뿐 아니라 card 내부 dict/list까지 mutate해도
        # 공유 스냅샷이 오염되지 않도록 deep copy를 넘긴다. accepted-card window는
        # 작게 제한되어 있어(limit<=100) ledger read를 줄이는 이득에 비해 비용이 작다.
        return [copy.deepcopy(card) for card in cached]

    def invalidate(self) -> None:
        self._cards.clear()

    def __getattr__(self, name: str):
        # 캐시하지 않는 read-model 메서드는 감싼 모델로 그대로 위임한다.
        return getattr(self._read_model, name)


class KnowledgeSearchService:
    def __init__(
        self,
        *,
        ledger: Ledger,
        retired_index_bridge,
        dataset_ids: list[str],
        allow_private_results: bool = False,
        native_memory_id: str = "",
        graph_adapter: GraphMemoryAdapter | None = None,
        authorized_reader: AuthorizedMemoryReader | None = None,
        read_pipeline: AuthorizedMemoryReader | None = None,
        mirror_search=None,
        allow_restricted_steward: bool = False,
        allow_steward_auto_accept: bool = False,
        allow_local_test_object_authority_writes: bool = False,
        allow_production_object_authority_writes: bool = False,
    ):
        self.ledger = ledger
        self.retired_index_bridge = retired_index_bridge
        self.dataset_ids = dataset_ids
        self.allow_private_results = bool(allow_private_results)
        self.native_memory_id = native_memory_id
        self.graph_adapter = graph_adapter
        # Brain Steward restricted tools 는 기본적으로 막혀 있다. review_commit(approve/reject/
        # supersede_commit/stale_commit)과 가장 위험한 auto_accept 를 별도 flag 로 분리한다.
        # human/manual gate 또는 명시적 test-only path 에서만 연다.
        self.allow_restricted_steward = bool(allow_restricted_steward)
        self.allow_steward_auto_accept = bool(allow_steward_auto_accept)
        self.allow_local_test_object_authority_writes = bool(allow_local_test_object_authority_writes)
        self.allow_production_object_authority_writes = bool(allow_production_object_authority_writes)
        # M8 read cutover: a Qdrant-backed (query, brain_id) -> list[dict] callable
        # that fills brain.query's archive/evidence lanes from the Qdrant searchable
        # mirror. When set it REPLACES the RetiredIndexBridge archive search (which is off in the
        # live MCP anyway). None -> legacy behaviour (RetiredIndexBridge if dataset_ids, else empty).
        self._mirror_search = mirror_search
        self.authorized_reader = authorized_reader or read_pipeline or MemoryReadPipeline(
            ledger=ledger,
            retired_index_bridge=retired_index_bridge,
            dataset_ids=dataset_ids,
            allow_private_results=allow_private_results,
        )
        self.read_pipeline = self.authorized_reader
        # Session-lifetime accepted-card snapshot shared across brain tool calls.
        self._brain_card_cache = _SessionCardCache(LegacyLedgerBrainReadModel(self.ledger))

    def invalidate_brain_card_cache(self) -> None:
        """세션 card snapshot을 비워 다음 brain tool 호출이 ledger를 다시 읽게 한다."""

        self._brain_card_cache.invalidate()

    def brain_steward(self):
        """proposal-only Brain Steward 서비스. restricted 위임은 flag 로만 열린다."""

        from .session_memory.brain_steward import BrainStewardService

        return BrainStewardService(
            self.ledger,
            allow_restricted=self.allow_restricted_steward,
            allow_auto_accept=self.allow_steward_auto_accept,
        )

    def append_object_review_proposal(self, proposal: dict) -> dict:
        stored = dict(proposal)
        ensure_public_safe(stored, "object_review_proposal")
        return self.ledger.upsert_object_review_proposal(stored)

    def commit_object_authority_decision(self, decision: dict) -> dict:
        stored = dict(decision)
        ensure_public_safe(stored, "object_authority_decision")
        committed = self.ledger.commit_object_authority_decision(stored)
        self.invalidate_brain_card_cache()
        return committed

    def object_review_proposals(self, *, project: str = "", limit: int = 20) -> dict:
        bounded = max(1, min(int(limit or 20), 100))
        project_name = public_safe_text(project, max_chars=120)
        items = self.ledger.list_object_review_proposals(project=project_name, limit=bounded)
        response = {
            "schema_version": "brain_review_proposals.v1",
            "project": project_name,
            "count": len(items),
            "items": items,
            "gaps": [] if items else ["review_queue_empty"],
        }
        ensure_public_safe(response, "object_review_proposals")
        return response

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
        result = self.core_brain(project=project or "").brain_objects_query(
            repository=repository,
            branch=branch,
            query=query,
            current_files=current_files,
            project=project or None,
            object_types=object_types or [],
            route=route,
            limit=limit,
            response_mode=response_mode,
            consumer=consumer,
        )
        return self._overlay_object_authority_states(result)

    def brain_source_to_candidate_graph(
        self,
        *,
        project: str,
        corpus_id: str = "",
        target: str = "production",
        consumer: str = "unspecified",
        limit: int = 20,
    ) -> dict[str, Any]:
        safe_target = public_safe_text(str(target or "production"), max_chars=80)
        if safe_target != "local_test":
            result = {
                "schema_version": "object_substrate_cli_denied.v1",
                "status": "denied",
                "reason": "production_source_to_candidate_graph_requires_later_validation_goal",
                "mutation_performed": False,
                "production_mutation_performed": False,
                "ledger_mutation_performed": False,
                "network_used": False,
            }
            ensure_public_safe(result, "brain_source_to_candidate_graph_denied")
            return result
        status = self.ledger.reference_corpus_status(
            project=public_safe_text(project, max_chars=120),
            corpus_id=public_safe_text(corpus_id, max_chars=180),
            limit=limit,
        )
        return run_source_to_candidate_graph_activation_preview(
            corpus_status=status,
            project=project,
            consumer=consumer,
        )

    def brain_candidate_review_edit(
        self,
        *,
        pack: Mapping[str, Any],
        edits: list[Mapping[str, Any]],
        reviewer_id: str = "unspecified",
        target: str = "local_test",
        mutation_mode: str = "no_mutation",
    ) -> dict[str, Any]:
        return apply_candidate_review_edits(
            pack,
            edits=edits,
            reviewer={"id": reviewer_id},
            target_scope=target,
            mutation_mode=mutation_mode,
        )

    def brain_approval_board_decide(
        self,
        *,
        pack: Mapping[str, Any],
        decisions: list[Mapping[str, Any]],
        target: str = "production",
        reviewer_id: str = "unspecified",
    ) -> dict[str, Any]:
        return apply_approval_board_decisions(
            pack,
            decisions=decisions,
            reviewer={"id": reviewer_id},
            ledger_scope=target,
        )

    def brain_source_to_candidate_runtime_readiness(
        self,
        *,
        live_evidence: Mapping[str, Any] | None = None,
        normalize_post_deploy_capture: Mapping[str, Any] | None = None,
        post_deploy_capture: Mapping[str, Any] | None = None,
        normalize_shadow_evidence: Mapping[str, Any] | None = None,
        shadow_evidence: Mapping[str, Any] | None = None,
        expected_commit: str = "",
        evidence_collection_plan: bool = False,
        evidence_packet_template: bool = False,
        collect_shadow_evidence: bool = False,
        repository: str = "",
        branch: str = "",
        consumer: str = "codex",
    ) -> dict[str, Any]:
        if evidence_collection_plan:
            return build_source_to_candidate_runtime_evidence_collection_plan(
                expected_commit=expected_commit,
                repository=repository,
                branch=branch,
                consumer=consumer,
            )
        if evidence_packet_template:
            return build_source_to_candidate_runtime_evidence_packet_template(
                expected_commit=expected_commit,
                repository=repository,
                branch=branch,
                consumer=consumer,
            )
        if collect_shadow_evidence:
            def route_runner(route: str) -> Mapping[str, Any]:
                return self.brain_objects_query(
                    repository=repository,
                    branch=branch,
                    query=f"source-to-candidate runtime readiness route smoke: {route}",
                    current_files=[],
                    route=route,
                    limit=5,
                    response_mode="full",
                    consumer=consumer,
                )

            return build_source_to_candidate_runtime_collected_shadow_evidence_packet(
                expected_commit=expected_commit,
                repository=repository,
                branch=branch,
                consumer=consumer,
                route_runner=route_runner,
            )
        if isinstance(normalize_post_deploy_capture, Mapping):
            return build_source_to_candidate_runtime_post_deploy_capture_packet(
                captured_evidence=normalize_post_deploy_capture,
            )
        if isinstance(post_deploy_capture, Mapping):
            return build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
                captured_evidence=post_deploy_capture,
                expected_commit=expected_commit,
            )
        if isinstance(normalize_shadow_evidence, Mapping):
            return build_source_to_candidate_runtime_shadow_evidence_packet(
                captured_evidence=normalize_shadow_evidence,
            )
        if isinstance(shadow_evidence, Mapping):
            return build_source_to_candidate_runtime_shadow_readiness_report(
                captured_evidence=shadow_evidence,
                expected_commit=expected_commit,
            )
        return build_source_to_candidate_runtime_readiness_report(
            live_evidence=live_evidence,
            expected_commit=expected_commit,
        )

    def _overlay_object_authority_states(self, result: Mapping[str, Any]) -> dict[str, Any]:
        response = copy.deepcopy(dict(result))
        object_pack = response.get("object_pack")
        if not isinstance(object_pack, dict):
            return response
        objects = [dict(obj) for obj in object_pack.get("objects", []) if isinstance(obj, Mapping)]
        object_ids = [str(obj.get("object_id") or "") for obj in objects if obj.get("object_id")]
        states = self.ledger.get_object_authority_states(object_ids) if object_ids else {}
        overlay_count = 0
        for obj in objects:
            object_id = str(obj.get("object_id") or "")
            state = states.get(object_id)
            if not state:
                continue
            _apply_object_authority_state(obj, state)
            overlay_count += 1
        object_pack["objects"] = objects
        if overlay_count:
            object_pack["lanes"] = _rebuild_object_lanes(object_pack, objects)
            object_pack["recommended_actions"] = [
                {"object_id": obj["object_id"], "action": obj["recommended_action"]}
                for obj in objects
                if obj.get("object_id") and obj.get("recommended_action")
            ]
        audit = dict(object_pack.get("audit") or {})
        audit["authority_state_overlay_count"] = overlay_count
        object_pack["audit"] = audit
        ensure_public_safe(response, "brain_objects_query_authority_overlay")
        return response

    def brain_object_explain(
        self,
        *,
        object_id: str,
        include_edges: bool = True,
        include_evidence: bool = True,
        response_mode: str = "full",
    ) -> dict[str, Any]:
        safe_object_id = public_safe_text(object_id, max_chars=180)
        result = self.core_brain().brain_object_explain(
            object_id=safe_object_id,
            include_edges=include_edges,
            include_evidence=include_evidence,
            response_mode=response_mode,
        )
        state = self.ledger.get_object_authority_state(safe_object_id)
        history = self.ledger.list_object_authority_decisions(target_object_id=safe_object_id, limit=20)
        result["decision_history"] = [dict(item) for item in history]
        if state:
            obj = dict(result.get("object") or {})
            obj.setdefault("object_id", safe_object_id)
            obj.setdefault("object_type", _object_type_from_object_id(safe_object_id))
            obj.setdefault("title", safe_object_id)
            obj.setdefault("summary", "Object authority state from ledger decision history.")
            obj.setdefault("lifecycle_status", "observed")
            obj.setdefault("authority_lane", str(state.get("previous_authority_lane") or "candidate"))
            obj.setdefault("verification_state", "unverified")
            obj.setdefault("review_state", "needs_review")
            obj.setdefault("recommended_action", "review")
            _apply_object_authority_state(obj, state)
            result["object"] = obj
            result["authority_state"] = _object_authority_state_view(state)
            gaps = [str(item) for item in result.get("gaps", []) if item]
            if "authority_state_from_ledger_only" not in gaps:
                gaps.append("authority_state_from_ledger_only")
            result["gaps"] = gaps
        ensure_public_safe(result, "brain_object_explain_authority_overlay")
        return result

    def core_brain(self, *, project: str = ""):
        return build_runtime_brain_service(
            project=project,
            artifact_store=LedgerSessionMemoryArtifactStore(self.ledger),
            read_model=self._brain_card_cache,
            source_catalog=LedgerSourceRefCatalog(self.ledger),
            graph_adapter=self.graph_adapter,
            document_bridge=RetiredIndexBridgeDocumentBridge(retired_index_bridge=self.retired_index_bridge, dataset_ids=self.dataset_ids),
            search_mirror_status=self._search_mirror_status(),
            reference_corpus_status_reader=self.ledger.reference_corpus_status,
        )

    def _search_mirror_status(self) -> dict:
        if self._mirror_search is None:
            return {
                "status": "unverified",
                "last_verified_at": "",
                "evidence_ref": "",
                "details": ["mirror_search_not_configured_for_context_authority"],
            }
        return {
            "status": "configured_unverified",
            "last_verified_at": "",
            "evidence_ref": "service:mirror_search_configured",
            "details": ["mirror_search_callable_configured_without_live_probe"],
        }

    def search(
        self,
        query: str,
        *,
        filters: dict | None = None,
        limit: int = 10,
        include_private: bool = False,
    ) -> dict:
        bounded_limit = _knowledge_search_public_limit(limit)
        search_query = MemorySearchQuery(
            query=query,
            filters=filters,
            limit=bounded_limit,
            include_private=include_private,
        )
        response = self.authorized_reader.read(search_query)
        results_dict = []
        for item in response.results:
            item_dict = {
                "knowledge_id": item.knowledge_id,
                "result_type": item.result_type,
                "title": item.title,
                "domain": item.domain,
                "project": item.project,
                "provider": item.provider,
                "summary": item.summary,
                "score": item.score,
                "currentness": item.currentness,
                "provenance": {
                    "authority": "ledger_authorized",
                    "citation_ref": item.knowledge_id,
                },
            }
            if item.conversation_chunk is not None:
                chunk = item.conversation_chunk
                item_dict.update({
                    "chunk_id": chunk.chunk_id,
                    "session_id_hash": chunk.session_id_hash,
                    "turn_range": {
                        "start": chunk.turn_range.start,
                        "end": chunk.turn_range.end,
                    },
                    "snippet": chunk.snippet,
                    "source_status": chunk.source_status,
                    "redaction_version": chunk.redaction_version,
                })
            results_dict.append(item_dict)
        return {"results": results_dict}

    def brain_query(self, *, brain_id: str, query: str, limit: int = 8) -> dict:
        read_model = LegacyLedgerBrainReadModel(self.ledger)
        index_search = self._mirror_search or (
            self._brain_query_index_search if self.dataset_ids else None
        )
        result = run_brain_query_v2(
            read_model=read_model,
            index_search=index_search,
            brain_id=brain_id,
            query=query,
            query_intent="session_context",
            limit=limit,
        )
        if self.native_memory_id:
            semantic = build_semantic_recall(
                ledger=self.ledger,
                retired_index_bridge=self.retired_index_bridge,
                memory_id=self.native_memory_id,
            )
            semantic_failure_type = ""
            try:
                semantic_hits = semantic(query, brain_id)
            except (OSError, RuntimeError, ValueError, KeyError, TypeError, sqlite3.DatabaseError) as exc:
                semantic_hits = []
                semantic_failure_type = type(exc).__name__
            audit = dict(result.get("audit") or {})
            audit["native_memory_bound"] = True
            audit["native_memory_hits"] = len(semantic_hits)
            if semantic_failure_type:
                audit["native_memory_error_type"] = semantic_failure_type
            result["audit"] = audit
        return result

    def _brain_query_index_search(self, query: str, brain_id: str) -> list[dict]:
        from .session_memory.brain_query import project_from_brain_id

        project = project_from_brain_id(brain_id)
        filters = {"project": project} if project else None
        chunks = self.retired_index_bridge.retrieve(query, self.dataset_ids, filters=filters, limit=8)
        results: list[dict] = []
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
            results.append(
                {
                    "result_type": str(chunk.get("result_type") or metadata.get("result_type") or "index_mirror"),
                    "memory_id": str(
                        chunk.get("memory_id")
                        or metadata.get("memory_id")
                        or chunk.get("source_ref")
                        or ""
                    ),
                    "card_type": str(chunk.get("card_type") or metadata.get("card_type") or ""),
                    "summary": str(chunk.get("summary") or ""),
                    "currentness": str(chunk.get("currentness") or metadata.get("currentness") or "unknown"),
                    "score": chunk.get("score"),
                    "content_hash": str(chunk.get("content_hash") or metadata.get("content_hash") or ""),
                }
            )
        return results

    def brain_resolve(self, *, query: str = "") -> dict:
        return resolve_brain_ids(read_model=LegacyLedgerBrainReadModel(self.ledger), query=query)


def _knowledge_search_public_limit(limit: int) -> int:
    return max(1, min(10, int(limit)))


def _apply_object_authority_state(obj: dict[str, Any], state: Mapping[str, Any]) -> None:
    lane = public_safe_text(str(state.get("authority_lane") or obj.get("authority_lane") or ""), max_chars=80)
    decision_type = public_safe_text(str(state.get("decision_type") or ""), max_chars=120)
    obj["authority_lane"] = lane
    obj["lifecycle_status"] = _lifecycle_status_for_authority_state(lane, decision_type, obj)
    obj["review_state"] = _review_state_for_authority_state(lane, obj)
    obj["recommended_action"] = _recommended_action_for_authority_state(lane, decision_type, obj)
    obj["authority_state"] = _object_authority_state_view(state)


def _object_authority_state_view(state: Mapping[str, Any]) -> dict[str, str]:
    return {
        "schema_version": str(state.get("schema_version") or "object_authority_state.v1"),
        "source": "ledger_object_authority_state",
        "decision_id": public_safe_text(str(state.get("decision_id") or ""), max_chars=180),
        "proposal_id": public_safe_text(str(state.get("proposal_id") or ""), max_chars=180),
        "decision_type": public_safe_text(str(state.get("decision_type") or ""), max_chars=120),
        "previous_authority_lane": public_safe_text(str(state.get("previous_authority_lane") or ""), max_chars=80),
        "authority_lane": public_safe_text(str(state.get("authority_lane") or ""), max_chars=80),
        "rollback_of_decision_id": public_safe_text(str(state.get("rollback_of_decision_id") or ""), max_chars=180),
        "supersedes_decision_id": public_safe_text(str(state.get("supersedes_decision_id") or ""), max_chars=180),
        "updated_at": public_safe_text(str(state.get("updated_at") or ""), max_chars=80),
    }


def _lifecycle_status_for_authority_state(lane: str, decision_type: str, obj: Mapping[str, Any]) -> str:
    if lane == "accepted_current":
        return "current"
    if lane == "accepted_non_current":
        if "supersed" in decision_type or "supersess" in decision_type:
            return "superseded"
        if "retir" in decision_type:
            return "retired"
        return "stale"
    if lane == "archive_only":
        return "archived"
    if lane == "rejected":
        return "rejected"
    if lane == "proposal_only":
        return "proposed"
    return public_safe_text(str(obj.get("lifecycle_status") or "observed"), max_chars=80)


def _review_state_for_authority_state(lane: str, obj: Mapping[str, Any]) -> str:
    if lane in {"accepted_current", "accepted_non_current", "archive_only"}:
        return "accepted"
    if lane == "rejected":
        return "rejected"
    if lane in {"candidate", "proposal_only"}:
        return "needs_review"
    return public_safe_text(str(obj.get("review_state") or "not_required"), max_chars=80)


def _recommended_action_for_authority_state(lane: str, decision_type: str, obj: Mapping[str, Any]) -> str:
    if lane == "accepted_current":
        return "keep"
    if lane == "accepted_non_current":
        if "supersed" in decision_type or "supersess" in decision_type:
            return "supersede"
        if "retir" in decision_type:
            return "retire"
        return "archive"
    if lane == "archive_only":
        return "archive"
    if lane == "rejected":
        return "retire"
    return public_safe_text(str(obj.get("recommended_action") or "review"), max_chars=80)


def _rebuild_object_lanes(object_pack: Mapping[str, Any], objects: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    existing_lanes = object_pack.get("lanes") if isinstance(object_pack.get("lanes"), Mapping) else {}
    lanes: dict[str, list[dict[str, Any]]] = {str(lane): [] for lane in existing_lanes}
    for lane in (
        "accepted_current",
        "accepted_non_current",
        "reference_only",
        "proposal_only",
        "archive_only",
        "derived_projection",
        "rejected",
    ):
        lanes.setdefault(lane, [])
    for obj in objects:
        lane = str(obj.get("authority_lane") or "reference_only")
        lanes.setdefault(lane, []).append(obj)
    return lanes


def _object_type_from_object_id(object_id: str) -> str:
    parts = str(object_id or "").split(":")
    if len(parts) >= 3 and parts[0] == "ko" and parts[1]:
        return public_safe_text(parts[1], max_chars=80)
    return "KnowledgeObject"
