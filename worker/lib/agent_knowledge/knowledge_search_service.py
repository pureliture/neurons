from __future__ import annotations

import copy

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
    """Per-session snapshot of accepted MemoryCards by (project, limit).

    Each brain tool call used to rebuild the read model and re-run
    `list_accepted_cards` (full accepted-card reload, limit=100) against the
    ledger. Within a single stdio MCP session the accepted-card set is stable
    enough to snapshot, so this memoizes the result for the session lifetime and
    collapses repeated tool calls onto one ledger read per (project, limit). It
    wraps the real read model and forwards everything else unchanged, so graph
    status, evidence policy, and other read paths are untouched.

    Staleness scope: every exposed brain tool is read-only and there is currently
    no in-session write path, so the snapshot stays correct for the session. A
    write to the same ledger by another process (worker/ingestion) is NOT
    reflected until the session restarts -- there is no cross-process or TTL
    invalidation. `invalidate()` exists as the explicit refresh seam to call once
    an in-session write path is added; its production wrapper
    `invalidate_brain_card_cache` has no production caller yet, so today the seam
    is reached only via tests.
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
        # Hand out deep copies so a downstream consumer mutating not just its
        # list but any nested dict/list inside a card cannot corrupt the shared
        # snapshot. The accepted-card window is bounded (limit<=100), so the
        # deepcopy cost is negligible against the ledger read it replaces.
        return [copy.deepcopy(card) for card in cached]

    def invalidate(self) -> None:
        self._cards.clear()

    def __getattr__(self, name: str):
        # Forward any non-cached read-model method (get_card_meta,
        # list_recent_cards, list_project_card_counts, ...) to the wrapped model.
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
    ):
        self.ledger = ledger
        self.ragflow = ragflow
        self.dataset_ids = dataset_ids
        self.allow_private_results = bool(allow_private_results)
        self.native_memory_id = native_memory_id
        self.graph_adapter = graph_adapter
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
        """Refresh seam: drop the session card snapshot so the next brain tool
        call re-reads accepted cards from the ledger."""

        self._brain_card_cache.invalidate()

    def core_brain(self, *, project: str = ""):
        return build_runtime_brain_service(
            project=project,
            artifact_store=LedgerSessionMemoryArtifactStore(self.ledger),
            read_model=self._brain_card_cache,
            source_catalog=LedgerSourceRefCatalog(self.ledger),
            graph_adapter=self.graph_adapter,
            document_bridge=RagFlowDocumentBridge(ragflow=self.ragflow, dataset_ids=self.dataset_ids),
        )

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
                    "dataset": item.provenance.dataset,
                    "ragflow_document_id": item.provenance.ragflow_document_id,
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
        ragflow_search = self._brain_query_ragflow_search if self.dataset_ids else None
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
            try:
                semantic_hits = semantic(query, brain_id)
            except Exception:
                semantic_hits = []
            audit = dict(result.get("audit") or {})
            audit["native_memory_bound"] = True
            audit["native_memory_hits"] = len(semantic_hits)
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
                        or chunk.get("document_id")
                        or chunk.get("doc_id")
                        or ""
                    ),
                    "card_type": str(chunk.get("card_type") or metadata.get("card_type") or ""),
                    "summary": str(chunk.get("summary") or chunk.get("content") or ""),
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
