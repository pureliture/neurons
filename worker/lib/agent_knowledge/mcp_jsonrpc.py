from __future__ import annotations

import json
import sys
from typing import TextIO

from .knowledge_search_service import KnowledgeSearchService
from .llm_brain_core.models import EvidenceRequest
from .mcp_tools import (
    BRAIN_CONTEXT_RESOLVE_TOOL_NAME,
    BRAIN_DRIFT_EXPLAIN_TOOL_NAME,
    BRAIN_EVIDENCE_GET_TOOL_NAME,
    BRAIN_INCIDENT_SEARCH_TOOL_NAME,
    BRAIN_MEMORY_SEARCH_TOOL_NAME,
    BRAIN_PERSONA_CHECK_TOOL_NAME,
    BRAIN_PERSONA_GET_TOOL_NAME,
    BRAIN_QUERY_TOOL_NAME,
    BRAIN_RESOLVE_TOOL_NAME,
    TOOL_NAME,
    list_tools,
)


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
