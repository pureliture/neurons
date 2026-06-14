from __future__ import annotations

import json
import sys
from typing import TextIO

from .ledger import Ledger
from .ragflow_client import RagflowHttpClient
from .session_memory.brain_query import resolve_brain_ids, run_brain_query_v2
from .session_memory.brain_read_model import LegacyLedgerBrainReadModel, build_semantic_recall
from .session_memory.transcript_model import MAX_TRANSCRIPT_SNIPPET_CHARS, redact_and_bound_text

TOOL_NAME = "knowledge.search"
BRAIN_QUERY_TOOL_NAME = "brain.query"
BRAIN_RESOLVE_TOOL_NAME = "brain.resolve"


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
    ]


class KnowledgeSearchService:
    def __init__(
        self,
        *,
        ledger: Ledger,
        ragflow,
        dataset_ids: list[str],
        allow_private_results: bool = False,
        native_memory_id: str = "",
    ):
        self.ledger = ledger
        self.ragflow = ragflow
        self.dataset_ids = dataset_ids
        self.allow_private_results = bool(allow_private_results)
        self.native_memory_id = native_memory_id

    def search(
        self,
        query: str,
        *,
        filters: dict | None = None,
        limit: int = 10,
        include_private: bool = False,
    ) -> dict:
        chunks = self.ragflow.retrieve(query, self.dataset_ids, filters=filters, limit=limit)
        results: list[dict] = []
        private_allowed = bool(include_private and self.allow_private_results)
        for chunk in chunks:
            document_id = str(chunk.get("document_id") or chunk.get("doc_id") or "")
            if not document_id:
                continue
            item = self.ledger.authorize_document(
                document_id,
                filters=filters or {},
                include_private=private_allowed,
            )
            if item is None:
                continue
            result = {
                "knowledge_id": item["knowledge_id"],
                "result_type": item["type"],
                "title": item["title"],
                "domain": item["domain"],
                "project": item["project"],
                "provider": item["provider"],
                "summary": item["summary"],
                "score": chunk.get("score"),
                "currentness": "server_authorized",
                "provenance": {
                    "dataset": chunk.get("kb_id") or chunk.get("dataset_id") or item["ragflow_dataset_id"],
                    "ragflow_document_id": item["ragflow_document_id"],
                },
            }
            if item["type"] == "conversation_chunk":
                conversation_chunk = self.ledger.get_conversation_chunk_by_document(document_id)
                if conversation_chunk is None:
                    continue
                result.update(
                    {
                        "chunk_id": conversation_chunk["chunk_id"],
                        "session_id_hash": conversation_chunk["session_id_hash"],
                        "turn_range": {
                            "start": conversation_chunk["turn_start_index"],
                            "end": conversation_chunk["turn_end_index"],
                        },
                        "snippet": redact_and_bound_text(
                            str(chunk.get("content") or ""),
                            MAX_TRANSCRIPT_SNIPPET_CHARS,
                        ),
                        "source_status": conversation_chunk["source_status"],
                        "redaction_version": conversation_chunk["redaction_version"],
                    }
                )
            results.append(result)
        return {"results": results[: max(1, min(10, int(limit)))]}

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
        return _error(request_id, -32602, str(exc))
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
    if tool_name == BRAIN_QUERY_TOOL_NAME:
        raw_limit = arguments.get("limit", 8)
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, (int, float)):
            raw_limit = 8
        result = service.brain_query(
            brain_id=str(arguments.get("brain_id") or ""),
            query=str(arguments.get("query") or ""),
            limit=int(raw_limit),
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
        limit=int(arguments.get("limit", 10)),
        include_private=bool(arguments.get("include_private", False)),
    )
    return _tool_result(result)


def _tool_result(result: dict) -> dict:
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return {"content": [{"type": "text", "text": text}], "structuredContent": result}


def _success(request_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
