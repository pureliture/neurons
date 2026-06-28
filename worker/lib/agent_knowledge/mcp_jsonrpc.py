from __future__ import annotations

import json
import sys
from typing import TextIO

from .knowledge_search_service import KnowledgeSearchService
from .llm_brain_core.context import project_from_repository
from .llm_brain_core.context_builder import normalize_context_consumer
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
    MEMORY_AUTHORITY_PACK_READ_TOOL_NAME,
    MEMORY_CANDIDATE_APPROVE_TOOL_NAME,
    MEMORY_CANDIDATE_AUTO_ACCEPT_TOOL_NAME,
    MEMORY_CANDIDATE_CREATE_TOOL_NAME,
    MEMORY_CANDIDATE_REJECT_TOOL_NAME,
    MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME,
    MEMORY_STALE_MARK_TOOL_NAME,
    MEMORY_SUPERSEDE_PROPOSE_TOOL_NAME,
    STEWARD_RESTRICTED_TOOL_NAMES,
    TOOL_NAME,
    list_tools,
)
from .session_memory.brain_steward import StewardPermissionError

# candidate / supersede proposal 이 받는 redacted source_span 입력 키.
# dispatch 가 arguments 에서 이 키만 골라 steward 로 넘긴다.
_STEWARD_SOURCE_SPAN_KEYS = (
    "card_type",
    "project",
    "provider",
    "scope",
    "title",
    "redacted_summary",
    "summary",
    "typed_payload",
    "content_hash",
    "source_ref",
    "span_ref",
    "confidence",
    "confidence_basis",
    "governance_tier",
)
_STEWARD_TOOL_NAMES = frozenset(
    {
        MEMORY_AUTHORITY_PACK_READ_TOOL_NAME,
        MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME,
        MEMORY_CANDIDATE_CREATE_TOOL_NAME,
        MEMORY_STALE_MARK_TOOL_NAME,
        MEMORY_SUPERSEDE_PROPOSE_TOOL_NAME,
        *STEWARD_RESTRICTED_TOOL_NAMES,
    }
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
            return _success(request_id, dispatch_tool_call(message.get("params") or {}, service))
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


def dispatch_tool_call(params: dict, service: KnowledgeSearchService) -> dict:
    tool_name = params.get("name")
    arguments = params.get("arguments") or {}
    if tool_name == BRAIN_CONTEXT_RESOLVE_TOOL_NAME:
        repository = _require_non_empty_string(arguments, "repository", tool_name=tool_name)
        branch = _require_non_empty_string(arguments, "branch", tool_name=tool_name)
        current_request = _require_non_empty_string(arguments, "current_request", tool_name=tool_name)
        current_files = arguments.get("current_files") or []
        if not isinstance(current_files, list):
            raise ValueError("current_files must be an array")
        project = _project_arg(arguments)
        result = service.core_brain(project=project).brain_context_resolve(
            repository=repository,
            branch=branch,
            current_files=[str(item) for item in current_files],
            current_request=current_request,
            project=project or None,
            limit=_bounded_limit(arguments.get("limit"), default=8, maximum=20),
            consumer=_consumer(arguments),
        ).to_dict(mode=_response_mode(arguments))
        return _tool_result(result)
    if tool_name == BRAIN_MEMORY_SEARCH_TOOL_NAME:
        query = _require_non_empty_string(arguments, "query", tool_name=tool_name)
        card_types = arguments.get("card_types")
        if card_types is not None and not isinstance(card_types, list):
            raise ValueError("card_types must be an array")
        project = _require_project_scope(arguments, tool_name=tool_name)
        result = service.core_brain(project=project).brain_memory_search(
            query=query,
            project=project,
            card_types=[str(item) for item in card_types] if isinstance(card_types, list) else None,
            limit=_bounded_limit(arguments.get("limit"), default=8, maximum=20),
        )
        return _tool_result(result)
    if tool_name == BRAIN_INCIDENT_SEARCH_TOOL_NAME:
        symptom = _require_non_empty_string(arguments, "symptom", tool_name=tool_name)
        project = _project_arg(arguments)
        result = service.core_brain(project=project).brain_incident_search(
            symptom=symptom,
            project=project,
            limit=_bounded_limit(arguments.get("limit"), default=5, maximum=20),
        )
        return _tool_result(result)
    if tool_name == BRAIN_DRIFT_EXPLAIN_TOOL_NAME:
        subject = _require_non_empty_string(arguments, "subject", tool_name=tool_name)
        project = _project_arg(arguments)
        result = service.core_brain(project=project).brain_drift_explain(
            subject=subject,
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
        plan = _require_non_empty_string(arguments, "plan", tool_name=tool_name)
        project = _project_arg(arguments)
        result = service.core_brain(project=project).brain_persona_check(
            plan=plan,
            project=project or None,
        )
        return _tool_result(result)
    if tool_name == BRAIN_EVIDENCE_GET_TOOL_NAME:
        source_ref_id = _require_non_empty_string(arguments, "source_ref_id", tool_name=tool_name)
        requesting_device_id_hash = _require_non_empty_string(
            arguments,
            "requesting_device_id_hash",
            tool_name=tool_name,
        )
        result = service.core_brain().brain_evidence_get(
            EvidenceRequest(
                source_ref_id=source_ref_id,
                requesting_device_id_hash=requesting_device_id_hash,
                span_ref_id=str(arguments.get("span_ref_id") or ""),
                approval_ref=str(arguments.get("approval_ref") or ""),
                expected_content_hash=str(arguments.get("expected_content_hash") or ""),
                max_bytes=_bounded_limit(arguments.get("max_bytes"), default=4096, maximum=65536),
                redaction_profile=str(arguments.get("redaction_profile") or "public_safe"),
            )
        )
        return _tool_result(result)
    if tool_name == BRAIN_QUERY_TOOL_NAME:
        brain_id = _require_non_empty_string(arguments, "brain_id", tool_name=tool_name)
        query = _require_non_empty_string(arguments, "query", tool_name=tool_name)
        result = service.brain_query(
            brain_id=brain_id,
            query=query,
            limit=_bounded_limit(arguments.get("limit"), default=8, maximum=10),
        )
        return _tool_result(result)
    if tool_name == BRAIN_RESOLVE_TOOL_NAME:
        result = service.brain_resolve(query=str(arguments.get("query") or ""))
        return _tool_result(result)
    if tool_name in _STEWARD_TOOL_NAMES:
        return _dispatch_steward_tool(tool_name, arguments, service)
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


def _call_tool(params: dict, service: KnowledgeSearchService) -> dict:
    return dispatch_tool_call(params, service)


def _steward_source_span(arguments: dict) -> dict:
    return {key: arguments[key] for key in _STEWARD_SOURCE_SPAN_KEYS if key in arguments}


def _dispatch_steward_tool(tool_name: str, arguments: dict, service: KnowledgeSearchService) -> dict:
    steward = service.brain_steward()
    if tool_name == MEMORY_AUTHORITY_PACK_READ_TOOL_NAME:
        project = _require_project_scope(arguments, tool_name=tool_name)
        result = steward.authority_pack_read(
            project=project,
            limit=_bounded_limit(arguments.get("limit"), default=8, maximum=50),
        )
        return _tool_result(result)
    if tool_name == MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME:
        result = steward.review_queue_list(
            project=_project_arg(arguments),
            limit=_bounded_limit(arguments.get("limit"), default=20, maximum=100),
        )
        return _tool_result(result)
    if tool_name == MEMORY_CANDIDATE_CREATE_TOOL_NAME:
        result = steward.candidate_create(
            source_span=_steward_source_span(arguments),
            mark_needs_review=bool(arguments.get("mark_needs_review", False)),
            review_reason=str(arguments.get("review_reason") or ""),
        )
        return _tool_result(result)
    if tool_name == MEMORY_STALE_MARK_TOOL_NAME:
        result = steward.stale_mark(
            memory_id=_require_non_empty_string(arguments, "memory_id", tool_name=tool_name),
            reason=_require_non_empty_string(arguments, "reason", tool_name=tool_name),
        )
        return _tool_result(result)
    if tool_name == MEMORY_SUPERSEDE_PROPOSE_TOOL_NAME:
        result = steward.supersede_propose(
            old_memory_id=_require_non_empty_string(arguments, "old_memory_id", tool_name=tool_name),
            source_span=_steward_source_span(arguments),
        )
        return _tool_result(result)
    # restricted tools: 기본 권한에서는 어떤 write 도 하지 않고 거부한다.
    try:
        if tool_name == MEMORY_CANDIDATE_APPROVE_TOOL_NAME:
            result = steward.candidate_approve(
                candidate_memory_id=_require_non_empty_string(arguments, "candidate_memory_id", tool_name=tool_name),
                approved_by=_require_non_empty_string(arguments, "approved_by", tool_name=tool_name),
                decision_id=_require_non_empty_string(arguments, "decision_id", tool_name=tool_name),
            )
        elif tool_name == MEMORY_CANDIDATE_REJECT_TOOL_NAME:
            result = steward.candidate_reject(
                candidate_memory_id=_require_non_empty_string(arguments, "candidate_memory_id", tool_name=tool_name),
                rejected_by=_require_non_empty_string(arguments, "rejected_by", tool_name=tool_name),
                decision_id=_require_non_empty_string(arguments, "decision_id", tool_name=tool_name),
                reason=_require_non_empty_string(arguments, "reason", tool_name=tool_name),
            )
        elif tool_name == MEMORY_CANDIDATE_AUTO_ACCEPT_TOOL_NAME:
            evaluation = arguments.get("evaluation")
            if not isinstance(evaluation, dict):
                raise ValueError("memory_candidate_auto_accept requires an evaluation object")
            result = steward.candidate_auto_accept(
                candidate_memory_id=_require_non_empty_string(arguments, "candidate_memory_id", tool_name=tool_name),
                evaluation=evaluation,
                operator_approval_ref=_require_non_empty_string(arguments, "operator_approval_ref", tool_name=tool_name),
            )
        else:
            # 새 restricted tool 이 분기 없이 auto_accept 로직으로 흘러드는 것을 막는다.
            raise ValueError(f"unhandled steward tool: {tool_name}")
    except StewardPermissionError:
        return _tool_result(
            {
                "schema_version": "brain_steward_restricted_denied.v1",
                "tool": tool_name,
                "permission": "denied",
                "reason": "restricted_tool_requires_human_gate",
                "write_performed": False,
                "authoritative_memory_changed": False,
            }
        )
    return _tool_result(result)


def _bounded_limit(value, *, default: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(1, min(maximum, int(value)))


def _response_mode(arguments: dict) -> str:
    mode = str(arguments.get("response_mode") or "full")
    if mode not in {"full", "compact", "degraded"}:
        raise ValueError("response_mode must be full, compact, or degraded")
    return mode


def _consumer(arguments: dict) -> str:
    return normalize_context_consumer(str(arguments.get("consumer") or "unspecified"))


def _knowledge_search_limit(arguments: dict) -> int:
    return max(1, min(10, int(arguments.get("limit", 10))))


def _require_non_empty_string(arguments: dict, key: str, *, tool_name: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{tool_name} requires a non-empty {key}")
    return value


def _require_project_scope(arguments: dict, *, tool_name: str) -> str:
    project = _project_arg(arguments)
    if not project:
        raise ValueError(f"{tool_name} requires project or repository")
    return project


def _project_arg(arguments: dict) -> str:
    explicit = str(arguments.get("project") or "").strip()
    if explicit:
        return explicit
    repository = str(arguments.get("repository") or "").strip().rstrip("/\\")
    if not repository:
        return ""
    project = project_from_repository(repository.replace("\\", "/"))
    return "" if project == "unknown" else project


def _tool_result(result: dict) -> dict:
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return {"content": [{"type": "text", "text": text}], "structuredContent": result}


def _success(request_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
