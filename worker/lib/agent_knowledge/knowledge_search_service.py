from __future__ import annotations

import copy
import sqlite3

from .ledger import Ledger
from .llm_brain_core.document_bridge import RagFlowDocumentBridge
from .llm_brain_core.graph import GraphMemoryAdapter
from .llm_brain_core.ledger_adapter import LedgerSessionMemoryArtifactStore, LedgerSourceRefCatalog
from .llm_brain_core.runtime import build_runtime_brain_service
from .memory_read_pipeline import AuthorizedMemoryReader, MemoryReadPipeline, MemorySearchQuery
from .ragflow_client import RagflowHttpClient
from .session_memory.brain_query import resolve_brain_ids, run_brain_query_v2
from .session_memory.brain_read_model import LegacyLedgerBrainReadModel, build_semantic_recall


class DisabledRagflowClient:
    def retrieve(self, *args, **kwargs) -> list[dict]:
        return []

    def search_messages(self, *args, **kwargs) -> dict:
        return {"status_code": 200, "json": {"code": 0, "data": []}}


def build_ragflow_client(
    *,
    ragflow_url: str = "",
    token: str = "",
    policy_proxy_url: str = "",
) -> RagflowHttpClient | DisabledRagflowClient:
    if policy_proxy_url:
        return RagflowHttpClient(base_url=policy_proxy_url, bearer_token="")
    if ragflow_url and token:
        return RagflowHttpClient(base_url=ragflow_url, bearer_token=token)
    return DisabledRagflowClient()


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
        ragflow,
        dataset_ids: list[str],
        allow_private_results: bool = False,
        native_memory_id: str = "",
        graph_adapter: GraphMemoryAdapter | None = None,
        authorized_reader: AuthorizedMemoryReader | None = None,
        read_pipeline: AuthorizedMemoryReader | None = None,
        mirror_search=None,
        allow_restricted_steward: bool = False,
        allow_steward_auto_accept: bool = False,
    ):
        self.ledger = ledger
        self.ragflow = ragflow
        self.dataset_ids = dataset_ids
        self.allow_private_results = bool(allow_private_results)
        self.native_memory_id = native_memory_id
        self.graph_adapter = graph_adapter
        # Brain Steward restricted tools 는 기본적으로 막혀 있다. review_commit(approve/reject/
        # supersede_commit/stale_commit)과 가장 위험한 auto_accept 를 별도 flag 로 분리한다.
        # human/manual gate 또는 명시적 test-only path 에서만 연다.
        self.allow_restricted_steward = bool(allow_restricted_steward)
        self.allow_steward_auto_accept = bool(allow_steward_auto_accept)
        # M8 read cutover: a Qdrant-backed (query, brain_id) -> list[dict] callable
        # that fills brain.query's archive/evidence lanes from the Qdrant searchable
        # mirror. When set it REPLACES the RAGFlow archive search (which is off in the
        # live MCP anyway). None -> legacy behaviour (RAGFlow if dataset_ids, else empty).
        self._mirror_search = mirror_search
        self.authorized_reader = authorized_reader or read_pipeline or MemoryReadPipeline(
            ledger=ledger,
            ragflow=ragflow,
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

    def core_brain(self, *, project: str = ""):
        return build_runtime_brain_service(
            project=project,
            artifact_store=LedgerSessionMemoryArtifactStore(self.ledger),
            read_model=self._brain_card_cache,
            source_catalog=LedgerSourceRefCatalog(self.ledger),
            graph_adapter=self.graph_adapter,
            document_bridge=RagFlowDocumentBridge(ragflow=self.ragflow, dataset_ids=self.dataset_ids),
            search_mirror_status=self._search_mirror_status(),
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
        ragflow_search = self._mirror_search or (
            self._brain_query_ragflow_search if self.dataset_ids else None
        )
        result = run_brain_query_v2(
            read_model=read_model,
            ragflow_search=ragflow_search,
            brain_id=brain_id,
            query=query,
            query_intent="session_context",
            limit=limit,
        )
        if self.native_memory_id:
            semantic = build_semantic_recall(
                ledger=self.ledger,
                ragflow=self.ragflow,
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

    def _brain_query_ragflow_search(self, query: str, brain_id: str) -> list[dict]:
        from .session_memory.brain_query import project_from_brain_id

        project = project_from_brain_id(brain_id)
        filters = {"project": project} if project else None
        chunks = self.ragflow.retrieve(query, self.dataset_ids, filters=filters, limit=8)
        results: list[dict] = []
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
            results.append(
                {
                    "result_type": str(chunk.get("result_type") or metadata.get("result_type") or "ragflow_mirror"),
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
