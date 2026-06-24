from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import sys
from typing import TextIO

from .ledger import Ledger
from .llm_brain_core.document_bridge import RagFlowDocumentBridge
from .llm_brain_core.graph import GraphMemoryAdapter
from .llm_brain_core.ledger_adapter import LedgerSessionMemoryArtifactStore, LedgerSourceRefCatalog
from .llm_brain_core.models import EvidenceRequest
from .llm_brain_core.runtime import build_runtime_brain_service
from .ragflow_client import RagflowHttpClient
from .session_memory.brain_query import resolve_brain_ids, run_brain_query_v2
from .session_memory.brain_read_model import LegacyLedgerBrainReadModel, build_semantic_recall
from .session_memory.transcript_model import MAX_TRANSCRIPT_SNIPPET_CHARS, redact_and_bound_text

TOOL_NAME = "knowledge.search"
BRAIN_QUERY_TOOL_NAME = "brain.query"
BRAIN_RESOLVE_TOOL_NAME = "brain.resolve"
BRAIN_CONTEXT_RESOLVE_TOOL_NAME = "brain_context_resolve"
BRAIN_MEMORY_SEARCH_TOOL_NAME = "brain_memory_search"
BRAIN_INCIDENT_SEARCH_TOOL_NAME = "brain_incident_search"
BRAIN_DRIFT_EXPLAIN_TOOL_NAME = "brain_drift_explain"
BRAIN_PERSONA_GET_TOOL_NAME = "brain_persona_get"
BRAIN_PERSONA_CHECK_TOOL_NAME = "brain_persona_check"
BRAIN_EVIDENCE_GET_TOOL_NAME = "brain_evidence_get"


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


def list_tools() -> list[dict]:
    return [
        {
            "name": TOOL_NAME,
            "description": "Search server-owned RAGFlow-backed neuron knowledge.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "filters": {"type": "object", "additionalProperties": {"type": "string"}},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 10},
                    "include_private": {"type": "boolean", "default": False},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_QUERY_TOOL_NAME,
            "description": "use brain: query accepted/current neuron memory by brain_id.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "brain_id": {"type": "string"},
                    "query": {"type": "string"},
                    "mode": {"type": "string", "enum": ["latest"], "default": "latest"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 8},
                },
                "required": ["brain_id", "query"],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_RESOLVE_TOOL_NAME,
            "description": "Resolve available /project/<project> brain_id candidates.",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_CONTEXT_RESOLVE_TOOL_NAME,
            "description": "Resolve the current LLM-Brain ContextPack from canonical artifacts/cards plus derived graph status.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repository": {"type": "string"},
                    "branch": {"type": "string"},
                    "current_files": {"type": "array", "items": {"type": "string"}, "default": []},
                    "current_request": {"type": "string"},
                    "project": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
                },
                "required": ["repository", "branch", "current_request"],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_MEMORY_SEARCH_TOOL_NAME,
            "description": "Search accepted/current LLM-Brain memory with derived graph results labeled separately.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "project": {"type": "string"},
                    "card_types": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
                },
                "required": ["query", "project"],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_INCIDENT_SEARCH_TOOL_NAME,
            "description": "Search prior incidents, attempts, fixes, verifications, and do-not-apply cases.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symptom": {"type": "string"},
                    "project": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                },
                "required": ["symptom", "project"],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_DRIFT_EXPLAIN_TOOL_NAME,
            "description": "Explain design, persona, or project assumption drift from canonical memory cards.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "project": {"type": "string"},
                },
                "required": ["subject", "project"],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_PERSONA_GET_TOOL_NAME,
            "description": "Return persona facts from accepted/current LLM-Brain memory.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "scope": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_PERSONA_CHECK_TOOL_NAME,
            "description": "Check a plan against accepted persona facts and return aligned/conflict/drift status.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "plan": {"type": "string"},
                    "project": {"type": "string"},
                },
                "required": ["plan"],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_EVIDENCE_GET_TOOL_NAME,
            "description": "Resolve a SourceRef/SpanRef through the LLM-Brain evidence policy without exposing raw private paths.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source_ref_id": {"type": "string"},
                    "requesting_device_id_hash": {"type": "string"},
                    "span_ref_id": {"type": "string"},
                    "approval_ref": {"type": "string"},
                    "expected_content_hash": {"type": "string"},
                    "max_bytes": {"type": "integer", "minimum": 1, "maximum": 65536, "default": 4096},
                    "redaction_profile": {"type": "string", "default": "public_safe"},
                },
                "required": ["source_ref_id", "requesting_device_id_hash"],
                "additionalProperties": False,
            },
        },
    ]


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


@dataclass(frozen=True)
class TurnRange:
    start: int
    end: int


@dataclass(frozen=True)
class ConversationChunkDetails:
    chunk_id: str
    session_id_hash: str
    turn_range: TurnRange
    snippet: str
    source_status: str
    redaction_version: int | str


@dataclass(frozen=True)
class MemoryProvenance:
    dataset: str
    ragflow_document_id: str


@dataclass(frozen=True)
class MemorySearchResultItem:
    knowledge_id: str
    result_type: str
    title: str
    domain: str
    project: str
    provider: str
    summary: str
    score: float | None
    currentness: str
    provenance: MemoryProvenance
    conversation_chunk: ConversationChunkDetails | None = None


@dataclass(frozen=True)
class MemorySearchResponse:
    results: list[MemorySearchResultItem]


@dataclass(frozen=True)
class MemorySearchQuery:
    query: str
    filters: dict | None = None
    limit: int = 10
    include_private: bool = False


class MemoryReadPipeline:
    def __init__(
        self,
        *,
        ledger: Ledger,
        ragflow,
        dataset_ids: list[str],
        allow_private_results: bool = False,
    ):
        self.ledger = ledger
        self.ragflow = ragflow
        self.dataset_ids = dataset_ids
        self.allow_private_results = bool(allow_private_results)

    def read(self, query: MemorySearchQuery) -> MemorySearchResponse:
        chunks = self.ragflow.retrieve(
            query.query,
            self.dataset_ids,
            filters=query.filters,
            limit=query.limit,
        )
        results: list[MemorySearchResultItem] = []
        private_allowed = bool(query.include_private and self.allow_private_results)
        for chunk in chunks:
            document_id = str(chunk.get("document_id") or chunk.get("doc_id") or "")
            if not document_id:
                continue
            item = self.ledger.authorize_document(
                document_id,
                filters=query.filters or {},
                include_private=private_allowed,
            )
            if item is None:
                continue
            provenance = MemoryProvenance(
                dataset=str(chunk.get("kb_id") or chunk.get("dataset_id") or item["ragflow_dataset_id"]),
                ragflow_document_id=item["ragflow_document_id"],
            )
            conversation_chunk_details = None
            if item["type"] == "conversation_chunk":
                conversation_chunk = self.ledger.get_conversation_chunk_by_document(document_id)
                if conversation_chunk is None:
                    continue
                conversation_chunk_details = ConversationChunkDetails(
                    chunk_id=conversation_chunk["chunk_id"],
                    session_id_hash=conversation_chunk["session_id_hash"],
                    turn_range=TurnRange(
                        start=conversation_chunk["turn_start_index"],
                        end=conversation_chunk["turn_end_index"],
                    ),
                    snippet=redact_and_bound_text(
                        str(chunk.get("content") or ""),
                        MAX_TRANSCRIPT_SNIPPET_CHARS,
                    ),
                    source_status=conversation_chunk["source_status"],
                    redaction_version=conversation_chunk["redaction_version"],
                )
            result_item = MemorySearchResultItem(
                knowledge_id=item["knowledge_id"],
                result_type=item["type"],
                title=item["title"],
                domain=item["domain"],
                project=item["project"],
                provider=item["provider"],
                summary=item["summary"],
                score=chunk.get("score"),
                currentness="server_authorized",
                provenance=provenance,
                conversation_chunk=conversation_chunk_details,
            )
            results.append(result_item)
        sliced_results = results[: max(1, int(query.limit))]
        return MemorySearchResponse(results=sliced_results)


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
        read_pipeline: MemoryReadPipeline | None = None,
    ):
        self.ledger = ledger
        self.ragflow = ragflow
        self.dataset_ids = dataset_ids
        self.allow_private_results = bool(allow_private_results)
        self.native_memory_id = native_memory_id
        self.graph_adapter = graph_adapter
        self.read_pipeline = read_pipeline or MemoryReadPipeline(
            ledger=ledger,
            ragflow=ragflow,
            dataset_ids=dataset_ids,
            allow_private_results=allow_private_results,
        )
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
        search_query = MemorySearchQuery(
            query=query,
            filters=filters,
            limit=limit,
            include_private=include_private,
        )
        response = self.read_pipeline.read(search_query)
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


def handle_jsonrpc_message(message: dict, service: KnowledgeSearchService) -> dict | None:
    request_id = message.get("id")
    method = message.get("method")
    try:
        if method == "initialize":
            return _success(
                request_id,
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "neurons", "version": "0.1.0"},
                },
            )
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return _success(request_id, {"tools": list_tools()})
        if method == "tools/call":
            return _success(request_id, _call_tool(message.get("params") or {}, service))
        return _error(request_id, -32601, f"method not found: {method}")
    except (TypeError, ValueError) as exc:
        # Never echo the raw exception message: it can carry caller-supplied
        # argument values or private context. Surface only a static message plus
        # the exception type name, mirroring the brain_context_resolve redaction.
        return _error(request_id, -32602, f"invalid params: {type(exc).__name__}")
    except Exception:
        return _error(request_id, -32603, "internal error")


def run_stdio_server(
    service: KnowledgeSearchService,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            message = json.loads(stripped)
        except json.JSONDecodeError:
            response = _error(None, -32700, "parse error")
        else:
            response = handle_jsonrpc_message(message, service)
        if response is None:
            continue
        stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        stdout.flush()


def _call_tool(params: dict, service: KnowledgeSearchService) -> dict:
    tool_name = params.get("name")
    arguments = params.get("arguments") or {}
    if tool_name == BRAIN_CONTEXT_RESOLVE_TOOL_NAME:
        current_files = arguments.get("current_files") or []
        if not isinstance(current_files, list):
            raise ValueError("current_files must be an array")
        project = _project_arg(arguments)
        result = service.core_brain(project=project).brain_context_resolve(
            repository=str(arguments.get("repository") or ""),
            branch=str(arguments.get("branch") or ""),
            current_files=[str(item) for item in current_files],
            current_request=str(arguments.get("current_request") or ""),
            project=project or None,
            limit=_bounded_limit(arguments.get("limit"), default=8, maximum=20),
        ).to_dict()
        return _tool_result(result)
    if tool_name == BRAIN_MEMORY_SEARCH_TOOL_NAME:
        card_types = arguments.get("card_types")
        if card_types is not None and not isinstance(card_types, list):
            raise ValueError("card_types must be an array")
        project = _project_arg(arguments)
        result = service.core_brain(project=project).brain_memory_search(
            query=str(arguments.get("query") or ""),
            project=project,
            card_types=[str(item) for item in card_types] if isinstance(card_types, list) else None,
            limit=_bounded_limit(arguments.get("limit"), default=8, maximum=20),
        )
        return _tool_result(result)
    if tool_name == BRAIN_INCIDENT_SEARCH_TOOL_NAME:
        project = _project_arg(arguments)
        result = service.core_brain(project=project).brain_incident_search(
            symptom=str(arguments.get("symptom") or ""),
            project=project,
            limit=_bounded_limit(arguments.get("limit"), default=5, maximum=20),
        )
        return _tool_result(result)
    if tool_name == BRAIN_DRIFT_EXPLAIN_TOOL_NAME:
        project = _project_arg(arguments)
        result = service.core_brain(project=project).brain_drift_explain(
            subject=str(arguments.get("subject") or ""),
            project=project,
        )
        return _tool_result(result)
    if tool_name == BRAIN_PERSONA_GET_TOOL_NAME:
        project = _project_arg(arguments)
        result = service.core_brain(project=project).brain_persona_get(
            project=project or None,
            scope=str(arguments.get("scope") or "") or None,
        )
        return _tool_result(result)
    if tool_name == BRAIN_PERSONA_CHECK_TOOL_NAME:
        project = _project_arg(arguments)
        result = service.core_brain(project=project).brain_persona_check(
            plan=str(arguments.get("plan") or ""),
            project=project or None,
        )
        return _tool_result(result)
    if tool_name == BRAIN_EVIDENCE_GET_TOOL_NAME:
        result = service.core_brain().brain_evidence_get(
            EvidenceRequest(
                source_ref_id=str(arguments.get("source_ref_id") or ""),
                requesting_device_id_hash=str(arguments.get("requesting_device_id_hash") or ""),
                span_ref_id=str(arguments.get("span_ref_id") or ""),
                approval_ref=str(arguments.get("approval_ref") or ""),
                expected_content_hash=str(arguments.get("expected_content_hash") or ""),
                max_bytes=_bounded_limit(arguments.get("max_bytes"), default=4096, maximum=65536),
                redaction_profile=str(arguments.get("redaction_profile") or "public_safe"),
            )
        )
        return _tool_result(result)
    if tool_name == BRAIN_QUERY_TOOL_NAME:
        result = service.brain_query(
            brain_id=str(arguments.get("brain_id") or ""),
            query=str(arguments.get("query") or ""),
            limit=_bounded_limit(arguments.get("limit"), default=8, maximum=10),
        )
        return _tool_result(result)
    if tool_name == BRAIN_RESOLVE_TOOL_NAME:
        result = service.brain_resolve(query=str(arguments.get("query") or ""))
        return _tool_result(result)
    if tool_name != TOOL_NAME:
        raise ValueError(f"unknown tool: {tool_name}")
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("knowledge.search requires a non-empty query")
    filters = arguments.get("filters") or {}
    if not isinstance(filters, dict):
        raise ValueError("filters must be an object")
    result = service.search(
        query,
        filters=filters,
        limit=_knowledge_search_limit(arguments),
        include_private=bool(arguments.get("include_private", False)),
    )
    return _tool_result(result)


def _bounded_limit(value, *, default: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(1, min(maximum, int(value)))


def _knowledge_search_limit(arguments: dict) -> int:
    return max(1, min(10, int(arguments.get("limit", 10))))


def _project_arg(arguments: dict) -> str:
    explicit = str(arguments.get("project") or "").strip()
    if explicit:
        return explicit
    repository = str(arguments.get("repository") or "").strip().rstrip("/\\")
    if not repository:
        return ""
    return repository.replace("\\", "/").split("/")[-1]


def _tool_result(result: dict) -> dict:
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return {"content": [{"type": "text", "text": text}], "structuredContent": result}


def _success(request_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
