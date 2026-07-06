from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TextIO

from .knowledge_search_service import KnowledgeSearchService
from .llm_brain_core.context import project_from_repository
from .llm_brain_core.context_builder import normalize_context_consumer
from .llm_brain_core.models import EvidenceRequest
from .llm_brain_core.objects.knowledge_objects import AuthorityDecision, ReviewProposal, denied_payload
from .llm_brain_core.objects.reference_corpus import build_corpus_ingest_plan
from .mcp_tools import (
    BRAIN_CORPUS_INGEST_PLAN_TOOL_NAME,
    BRAIN_CORPUS_STATUS_TOOL_NAME,
    BRAIN_SOURCE_TO_CANDIDATE_GRAPH_TOOL_NAME,
    BRAIN_CANDIDATE_REVIEW_EDIT_TOOL_NAME,
    BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME,
    BRAIN_SOURCE_TO_CANDIDATE_RUNTIME_READINESS_TOOL_NAME,
    BRAIN_CONTEXT_RESOLVE_TOOL_NAME,
    BRAIN_DRIFT_EXPLAIN_TOOL_NAME,
    BRAIN_EVIDENCE_GET_TOOL_NAME,
    BRAIN_INCIDENT_SEARCH_TOOL_NAME,
    BRAIN_MEMORY_SEARCH_TOOL_NAME,
    BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME,
    BRAIN_OBJECT_EXPLAIN_TOOL_NAME,
    BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME,
    BRAIN_OBJECTS_QUERY_TOOL_NAME,
    BRAIN_PERSONA_CHECK_TOOL_NAME,
    BRAIN_PERSONA_GET_TOOL_NAME,
    BRAIN_QUERY_TOOL_NAME,
    BRAIN_REVIEW_PROPOSALS_TOOL_NAME,
    BRAIN_RESOLVE_TOOL_NAME,
    MEMORY_AUTHORITY_PACK_READ_TOOL_NAME,
    MEMORY_CANDIDATE_APPROVE_TOOL_NAME,
    MEMORY_CANDIDATE_AUTO_ACCEPT_TOOL_NAME,
    MEMORY_CANDIDATE_CREATE_TOOL_NAME,
    MEMORY_CANDIDATE_REJECT_TOOL_NAME,
    MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME,
    MEMORY_STALE_COMMIT_TOOL_NAME,
    MEMORY_STALE_MARK_TOOL_NAME,
    MEMORY_SUPERSEDE_COMMIT_TOOL_NAME,
    MEMORY_SUPERSEDE_PROPOSE_TOOL_NAME,
    STEWARD_RESTRICTED_TOOL_NAMES,
    TOOL_NAME,
    ToolContract,
    list_tools,
    tool_contract_registry,
)
from .public_safe_util import public_safe_text, short_hash
from .session_memory.brain_steward import StewardPermissionError

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
_STEWARD_READ_PROPOSAL_TOOL_NAMES = _STEWARD_TOOL_NAMES - frozenset(STEWARD_RESTRICTED_TOOL_NAMES)

ToolHandler = Callable[[dict, KnowledgeSearchService], dict]
ToolDispatch = Callable[[str, dict, KnowledgeSearchService], dict]
StewardReadProposalDispatch = Callable[[str, dict, object], dict]
StewardRestrictedDispatch = Callable[[str, dict, object], dict]


@dataclass(frozen=True)
class ToolRuntimeContract:
    tool_contract: ToolContract
    handler: ToolHandler

    @property
    def name(self) -> str:
        return self.tool_contract.name

    @property
    def dispatch_owner(self) -> str:
        return self.tool_contract.dispatch_owner

    def to_tool(self) -> dict:
        return self.tool_contract.to_tool()


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
    registry = tool_handler_registry()
    handler = registry.get(tool_name)
    if handler is None:
        raise ValueError(f"unknown tool: {tool_name}")
    return handler(arguments, service)


def tool_runtime_contract_registry() -> dict[str, ToolRuntimeContract]:
    contracts = tool_contract_registry()
    handlers = _tool_handler_candidates()
    _validate_tool_handler_registry(handlers, expected_names=set(contracts))
    return {
        name: ToolRuntimeContract(tool_contract=contract, handler=handlers[name])
        for name, contract in contracts.items()
    }


def tool_handler_registry() -> dict[str, ToolHandler]:
    return {
        name: runtime_contract.handler
        for name, runtime_contract in tool_runtime_contract_registry().items()
    }


def _tool_handler_candidates() -> dict[str, ToolHandler]:
    registry = {
        tool_name: _bind_tool_handler(tool_name, dispatch)
        for tool_name, dispatch in _tool_dispatch_registry().items()
    }
    registry.update(
        {
            tool_name: _bind_read_proposal_steward_handler(tool_name, dispatch)
            for tool_name, dispatch in _steward_read_proposal_dispatch_registry().items()
        }
    )
    registry.update(
        {
            tool_name: _bind_restricted_steward_handler(tool_name, dispatch)
            for tool_name, dispatch in _steward_restricted_dispatch_registry().items()
        }
    )
    return registry


def _tool_dispatch_registry() -> dict[str, ToolDispatch]:
    dispatches: tuple[tuple[str, ToolDispatch], ...] = (
        (BRAIN_CONTEXT_RESOLVE_TOOL_NAME, _dispatch_brain_context_resolve_tool),
        (BRAIN_MEMORY_SEARCH_TOOL_NAME, _dispatch_brain_memory_search_tool),
        (BRAIN_INCIDENT_SEARCH_TOOL_NAME, _dispatch_brain_incident_search_tool),
        (BRAIN_DRIFT_EXPLAIN_TOOL_NAME, _dispatch_brain_drift_explain_tool),
        (BRAIN_PERSONA_GET_TOOL_NAME, _dispatch_brain_persona_get_tool),
        (BRAIN_PERSONA_CHECK_TOOL_NAME, _dispatch_brain_persona_check_tool),
        (BRAIN_EVIDENCE_GET_TOOL_NAME, _dispatch_brain_evidence_get_tool),
        (BRAIN_OBJECTS_QUERY_TOOL_NAME, _dispatch_brain_objects_query_tool),
        (BRAIN_OBJECT_EXPLAIN_TOOL_NAME, _dispatch_brain_object_explain_tool),
        (BRAIN_CORPUS_STATUS_TOOL_NAME, _dispatch_brain_corpus_status_tool),
        (BRAIN_CORPUS_INGEST_PLAN_TOOL_NAME, _dispatch_brain_corpus_ingest_plan_tool),
        (BRAIN_SOURCE_TO_CANDIDATE_GRAPH_TOOL_NAME, _dispatch_brain_source_to_candidate_graph_tool),
        (BRAIN_CANDIDATE_REVIEW_EDIT_TOOL_NAME, _dispatch_brain_candidate_review_edit_tool),
        (BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME, _dispatch_brain_approval_board_decide_tool),
        (
            BRAIN_SOURCE_TO_CANDIDATE_RUNTIME_READINESS_TOOL_NAME,
            _dispatch_brain_source_to_candidate_runtime_readiness_tool,
        ),
        (BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME, _dispatch_brain_object_proposal_create_tool),
        (BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME, _dispatch_brain_object_decision_commit_tool),
        (BRAIN_REVIEW_PROPOSALS_TOOL_NAME, _dispatch_brain_review_proposals_tool),
        (BRAIN_QUERY_TOOL_NAME, _dispatch_brain_query_tool),
        (BRAIN_RESOLVE_TOOL_NAME, _dispatch_brain_resolve_tool),
        (TOOL_NAME, _dispatch_knowledge_search_tool),
    )
    return dict(dispatches)


def _bind_tool_handler(tool_name: str, dispatch: ToolDispatch) -> ToolHandler:
    def handle(arguments: dict, service: KnowledgeSearchService) -> dict:
        return dispatch(tool_name, arguments, service)

    return handle


def _validate_tool_handler_registry(
    registry: dict[str, ToolHandler],
    *,
    expected_names: set[str] | None = None,
) -> None:
    tool_names = expected_names or set(tool_contract_registry())
    handler_names = set(registry)
    missing_handlers = sorted(tool_names - handler_names)
    if missing_handlers:
        raise ValueError(f"MCP tools missing handlers: {missing_handlers}")
    stale_handlers = sorted(handler_names - tool_names)
    if stale_handlers:
        raise ValueError(f"MCP tool handlers are stale: {stale_handlers}")


def _dispatch_brain_context_resolve_tool(tool_name: str, arguments: dict, service: KnowledgeSearchService) -> dict:
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


def _dispatch_brain_memory_search_tool(tool_name: str, arguments: dict, service: KnowledgeSearchService) -> dict:
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


def _dispatch_brain_incident_search_tool(tool_name: str, arguments: dict, service: KnowledgeSearchService) -> dict:
    symptom = _require_non_empty_string(arguments, "symptom", tool_name=tool_name)
    project = _project_arg(arguments)
    result = service.core_brain(project=project).brain_incident_search(
        symptom=symptom,
        project=project,
        limit=_bounded_limit(arguments.get("limit"), default=5, maximum=20),
    )
    return _tool_result(result)


def _dispatch_brain_drift_explain_tool(tool_name: str, arguments: dict, service: KnowledgeSearchService) -> dict:
    subject = _require_non_empty_string(arguments, "subject", tool_name=tool_name)
    project = _project_arg(arguments)
    result = service.core_brain(project=project).brain_drift_explain(
        subject=subject,
        project=project,
    )
    return _tool_result(result)


def _dispatch_brain_persona_get_tool(tool_name: str, arguments: dict, service: KnowledgeSearchService) -> dict:
    _ = tool_name
    project = _project_arg(arguments)
    result = service.core_brain(project=project).brain_persona_get(
        project=project or None,
        scope=str(arguments.get("scope") or "") or None,
    )
    return _tool_result(result)


def _dispatch_brain_persona_check_tool(tool_name: str, arguments: dict, service: KnowledgeSearchService) -> dict:
    plan = _require_non_empty_string(arguments, "plan", tool_name=tool_name)
    project = _project_arg(arguments)
    result = service.core_brain(project=project).brain_persona_check(
        plan=plan,
        project=project or None,
    )
    return _tool_result(result)


def _dispatch_brain_evidence_get_tool(tool_name: str, arguments: dict, service: KnowledgeSearchService) -> dict:
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


def _dispatch_brain_objects_query_tool(tool_name: str, arguments: dict, service: KnowledgeSearchService) -> dict:
    repository = _require_non_empty_string(arguments, "repository", tool_name=tool_name)
    branch = _require_non_empty_string(arguments, "branch", tool_name=tool_name)
    query = _require_non_empty_string(arguments, "query", tool_name=tool_name)
    current_files = arguments.get("current_files") or []
    if not isinstance(current_files, list):
        raise ValueError("current_files must be an array")
    object_types = arguments.get("object_types") or []
    if not isinstance(object_types, list):
        raise ValueError("object_types must be an array")
    project = _project_arg(arguments)
    result = service.brain_objects_query(
        repository=repository,
        branch=branch,
        query=query,
        current_files=[str(item) for item in current_files],
        project=project or None,
        object_types=[str(item) for item in object_types],
        route=str(arguments.get("route") or ""),
        limit=_bounded_limit(arguments.get("limit"), default=20, maximum=50),
        response_mode=_response_mode(arguments),
        consumer=_consumer(arguments),
    )
    return _tool_result(result)


def _dispatch_brain_object_explain_tool(tool_name: str, arguments: dict, service: KnowledgeSearchService) -> dict:
    result = service.brain_object_explain(
        object_id=_require_non_empty_string(arguments, "object_id", tool_name=tool_name),
        include_edges=bool(arguments.get("include_edges", True)),
        include_evidence=bool(arguments.get("include_evidence", True)),
        response_mode=_response_mode(arguments),
    )
    return _tool_result(result)


def _dispatch_brain_corpus_status_tool(tool_name: str, arguments: dict, service: KnowledgeSearchService) -> dict:
    _ = tool_name
    project = _project_arg(arguments)
    result = service.core_brain(project=project).brain_corpus_status(
        corpus_id=str(arguments.get("corpus_id") or ""),
        project=project,
        limit=_bounded_limit(arguments.get("limit"), default=20, maximum=100),
    )
    return _tool_result(result)


def _dispatch_brain_corpus_ingest_plan_tool(tool_name: str, arguments: dict, service: KnowledgeSearchService) -> dict:
    _ = service
    manifest = arguments.get("manifest")
    if not isinstance(manifest, dict):
        manifest = {"corpus_name": str(arguments.get("corpus_name") or "reference-corpus"), "sources": []}
        if arguments.get("manifest_ref"):
            manifest["manifest_ref"] = str(arguments.get("manifest_ref") or "")
            manifest["gaps"] = ["manifest_ref_not_loaded"]
    expected_source_type_counts = arguments.get("expected_source_type_counts")
    result = build_corpus_ingest_plan(
        manifest,
        project=_require_non_empty_string(arguments, "project", tool_name=tool_name),
        storage_mode=str(arguments.get("storage_mode") or "metadata_only"),
        expected_source_count=arguments.get("expected_source_count"),
        expected_source_url_count=arguments.get("expected_source_url_count"),
        expected_manual_text_without_url_count=arguments.get("expected_manual_text_without_url_count"),
        expected_source_type_counts=(
            expected_source_type_counts
            if isinstance(expected_source_type_counts, dict)
            else None
        ),
    )
    return _tool_result(result)


def _dispatch_brain_source_to_candidate_graph_tool(
    tool_name: str,
    arguments: dict,
    service: KnowledgeSearchService,
) -> dict:
    result = service.brain_source_to_candidate_graph(
        project=_require_non_empty_string(arguments, "project", tool_name=tool_name),
        corpus_id=str(arguments.get("corpus_id") or ""),
        target=str(arguments.get("target") or "production"),
        consumer=_consumer(arguments),
        limit=_bounded_limit(arguments.get("limit"), default=20, maximum=100),
    )
    return _tool_result(result)


def _dispatch_brain_candidate_review_edit_tool(
    tool_name: str,
    arguments: dict,
    service: KnowledgeSearchService,
) -> dict:
    pack = arguments.get("pack")
    if not isinstance(pack, dict):
        raise ValueError(f"{tool_name} requires pack object")
    edits = arguments.get("edits")
    if not isinstance(edits, list):
        raise ValueError(f"{tool_name} requires edits array")
    result = service.brain_candidate_review_edit(
        pack=pack,
        edits=[dict(item) for item in edits if isinstance(item, Mapping)],
        reviewer_id=str(arguments.get("reviewer_id") or "unspecified"),
    )
    return _tool_result(result)


def _dispatch_brain_approval_board_decide_tool(
    tool_name: str,
    arguments: dict,
    service: KnowledgeSearchService,
) -> dict:
    pack = arguments.get("pack")
    if not isinstance(pack, dict):
        raise ValueError(f"{tool_name} requires pack object")
    decisions = arguments.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError(f"{tool_name} requires decisions array")
    result = service.brain_approval_board_decide(
        pack=pack,
        decisions=[dict(item) for item in decisions if isinstance(item, Mapping)],
        target=str(arguments.get("target") or "production"),
        reviewer_id=str(arguments.get("reviewer_id") or "unspecified"),
    )
    return _tool_result(result)


def _dispatch_brain_source_to_candidate_runtime_readiness_tool(
    tool_name: str,
    arguments: dict,
    service: KnowledgeSearchService,
) -> dict:
    _ = tool_name
    live_evidence = arguments.get("live_evidence")
    result = service.brain_source_to_candidate_runtime_readiness(
        live_evidence=live_evidence if isinstance(live_evidence, Mapping) else None,
        expected_commit=str(arguments.get("expected_commit") or ""),
    )
    return _tool_result(result)


def _dispatch_brain_object_proposal_create_tool(tool_name: str, arguments: dict, service: KnowledgeSearchService) -> dict:
    ledger_scope = str(arguments.get("ledger_scope") or "production")
    if ledger_scope != "local_test":
        return _tool_result(
            denied_payload(
                tool_name,
                "proposal_write_requires_local_test_ledger_or_later_production_gate",
            )
        )
    if not _local_test_object_authority_writes_allowed(service):
        return _tool_result(
            denied_payload(
                tool_name,
                "local_test_object_authority_write_requires_test_service_gate",
            )
        )
    result = ReviewProposal.from_parts(
        proposal_type=_require_non_empty_string(arguments, "proposal_type", tool_name=tool_name),
        target_object_id=_require_non_empty_string(arguments, "target_object_id", tool_name=tool_name),
        reason=_require_non_empty_string(arguments, "reason", tool_name=tool_name),
        evidence_refs=[str(item) for item in arguments.get("evidence_refs") or []],
        proposer=_steward_proposer(arguments),
    ).to_dict(
        proposal_write_performed=True,
        proposal_write_target="local_test_ledger",
    )
    result["project"] = _project_arg(arguments)
    service.append_object_review_proposal(result)
    return _tool_result(result)


def _dispatch_brain_object_decision_commit_tool(tool_name: str, arguments: dict, service: KnowledgeSearchService) -> dict:
    ledger_scope = str(arguments.get("ledger_scope") or "production")
    if ledger_scope != "local_test":
        return _tool_result(
            denied_payload(
                tool_name,
                "restricted_tool_requires_human_gate",
                extra={"production_promotion_plan": _production_authority_promotion_plan(arguments)},
            )
        )
    if not _local_test_object_authority_writes_allowed(service):
        return _tool_result(
            denied_payload(
                tool_name,
                "local_test_object_authority_write_requires_test_service_gate",
            )
        )
    approved_by = _require_non_empty_string(arguments, "approved_by", tool_name=tool_name)
    decision = AuthorityDecision.from_parts(
        decision_type=_require_non_empty_string(arguments, "decision_type", tool_name=tool_name),
        target_object_id=_require_non_empty_string(arguments, "target_object_id", tool_name=tool_name),
        previous_authority_lane=_require_non_empty_string(arguments, "previous_authority_lane", tool_name=tool_name),
        new_authority_lane=_require_non_empty_string(arguments, "new_authority_lane", tool_name=tool_name),
        approved_by="redacted",
        evidence_refs=[str(item) for item in arguments.get("evidence_refs") or []],
    ).to_dict(authority_write_performed=True, cache_invalidated=True)
    decision["decision_id"] = _require_non_empty_string(arguments, "decision_id", tool_name=tool_name)
    decision["proposal_id"] = _require_non_empty_string(arguments, "proposal_id", tool_name=tool_name)
    decision["project"] = _project_arg(arguments)
    decision["decision_reason"] = public_safe_text(str(arguments.get("decision_reason") or ""), max_chars=512)
    decision["approved_by_hash"] = "sha256:" + short_hash(approved_by, length=24)
    result = service.commit_object_authority_decision(decision)
    return _tool_result(result)


def _local_test_object_authority_writes_allowed(service: KnowledgeSearchService) -> bool:
    return bool(getattr(service, "allow_local_test_object_authority_writes", False)) and not bool(
        getattr(service.ledger, "read_only", True)
    )


def _production_authority_promotion_plan(arguments: Mapping[str, object]) -> dict:
    decision_type = public_safe_text(str(arguments.get("decision_type") or ""), max_chars=120)
    proposal_id = public_safe_text(str(arguments.get("proposal_id") or ""), max_chars=180)
    decision_id = public_safe_text(str(arguments.get("decision_id") or ""), max_chars=180)
    project = public_safe_text(str(arguments.get("project") or ""), max_chars=120)
    return {
        "schema_version": "object_authority_promotion_plan.v1",
        "production_write_state": "closed_without_human_gate",
        "mutation_allowed": False,
        "requested_decision_type": decision_type,
        "requested_proposal_id": proposal_id,
        "requested_decision_id": decision_id,
        "project": project,
        "allowed_object_classes": ["RepoDocument"],
        "allowed_decision_types": [
            "accept_current",
            "commit_stale",
            "commit_supersession",
            "retire",
            "reject_candidate",
        ],
        "reviewer_role": "human_object_authority_reviewer",
        "required_gate_evidence": [
            "configured_deployed_mcp_identity_matches_source",
            "single_object_scope",
            "read_after_write_smoke_plan",
            "rollback_or_supersession_plan",
            "no_raw_private_evidence",
        ],
        "rollback_path": [
            "write_new_authority_decision_preserving_audit_history",
            "demote_prior_object_to_accepted_non_current_or_archive_only",
            "verify_brain_objects_query_read_after_write",
        ],
        "blast_radius": {
            "scope": "single_project_single_object",
            "max_objects_per_decision": 1,
            "requires_project": True,
        },
        "no_mutation_report": {
            "proposal_write_performed": False,
            "authority_write_performed": False,
            "authoritative_memory_changed": False,
        },
    }


def _dispatch_brain_review_proposals_tool(tool_name: str, arguments: dict, service: KnowledgeSearchService) -> dict:
    _ = tool_name
    result = service.object_review_proposals(
        project=_project_arg(arguments),
        limit=_bounded_limit(arguments.get("limit"), default=20, maximum=100),
    )
    return _tool_result(result)


def _dispatch_brain_query_tool(tool_name: str, arguments: dict, service: KnowledgeSearchService) -> dict:
    brain_id = _require_non_empty_string(arguments, "brain_id", tool_name=tool_name)
    query = _require_non_empty_string(arguments, "query", tool_name=tool_name)
    result = service.brain_query(
        brain_id=brain_id,
        query=query,
        limit=_bounded_limit(arguments.get("limit"), default=8, maximum=10),
    )
    return _tool_result(result)


def _dispatch_brain_resolve_tool(tool_name: str, arguments: dict, service: KnowledgeSearchService) -> dict:
    _ = tool_name
    result = service.brain_resolve(query=str(arguments.get("query") or ""))
    return _tool_result(result)


def _dispatch_knowledge_search_tool(tool_name: str, arguments: dict, service: KnowledgeSearchService) -> dict:
    _ = tool_name
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


def _steward_proposer(arguments: dict) -> str:
    """제안 actor 라벨(예: hermes). card subject provider 와 다른 식별 축이며 advisory 다."""
    return normalize_context_consumer(str(arguments.get("proposer") or "unspecified"))


def _dispatch_steward_tool(tool_name: str, arguments: dict, service: KnowledgeSearchService) -> dict:
    handler = steward_read_proposal_handler_registry().get(tool_name)
    if handler is None:
        handler = steward_restricted_handler_registry().get(tool_name)
    if handler is None:
        # 새 restricted tool 이 분기 없이 auto_accept 로직으로 흘러드는 것을 막는다.
        raise ValueError(f"unhandled steward tool: {tool_name}")
    return handler(arguments, service)


def steward_read_proposal_handler_registry() -> dict[str, ToolHandler]:
    registry = _tool_handlers_for_dispatch_owner("brain_steward")
    _validate_read_proposal_steward_handler_registry(registry)
    return registry


def _steward_read_proposal_dispatch_registry() -> dict[str, StewardReadProposalDispatch]:
    dispatches: tuple[tuple[str, StewardReadProposalDispatch], ...] = (
        (MEMORY_AUTHORITY_PACK_READ_TOOL_NAME, _dispatch_steward_authority_pack_read_tool),
        (MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME, _dispatch_steward_review_queue_list_tool),
        (MEMORY_CANDIDATE_CREATE_TOOL_NAME, _dispatch_steward_candidate_create_tool),
        (MEMORY_STALE_MARK_TOOL_NAME, _dispatch_steward_stale_mark_tool),
        (MEMORY_SUPERSEDE_PROPOSE_TOOL_NAME, _dispatch_steward_supersede_propose_tool),
    )
    return dict(dispatches)


def _bind_read_proposal_steward_handler(tool_name: str, dispatch: StewardReadProposalDispatch) -> ToolHandler:
    def handle(arguments: dict, service: KnowledgeSearchService) -> dict:
        result = dispatch(tool_name, arguments, service.brain_steward())
        return _tool_result(result)

    return handle


def _validate_read_proposal_steward_handler_registry(registry: dict[str, ToolHandler]) -> None:
    expected = set(_STEWARD_READ_PROPOSAL_TOOL_NAMES)
    actual = set(registry)
    missing = sorted(expected - actual)
    if missing:
        raise ValueError(f"steward read/proposal tools missing handlers: {missing}")
    stale = sorted(actual - expected)
    if stale:
        raise ValueError(f"steward read/proposal handlers are stale: {stale}")
    overlap = sorted(actual & set(STEWARD_RESTRICTED_TOOL_NAMES))
    if overlap:
        raise ValueError(f"steward read/proposal handlers include restricted tools: {overlap}")


def _dispatch_steward_authority_pack_read_tool(tool_name: str, arguments: dict, steward: object) -> dict:
    project = _require_project_scope(arguments, tool_name=tool_name)
    return steward.authority_pack_read(
        project=project,
        limit=_bounded_limit(arguments.get("limit"), default=8, maximum=50),
    )


def _dispatch_steward_review_queue_list_tool(tool_name: str, arguments: dict, steward: object) -> dict:
    _ = tool_name
    return steward.review_queue_list(
        project=_project_arg(arguments),
        limit=_bounded_limit(arguments.get("limit"), default=20, maximum=100),
    )


def _dispatch_steward_candidate_create_tool(tool_name: str, arguments: dict, steward: object) -> dict:
    _ = tool_name
    return steward.candidate_create(
        source_span=steward.select_source_span(arguments),
        mark_needs_review=bool(arguments.get("mark_needs_review", False)),
        review_reason=str(arguments.get("review_reason") or ""),
        proposer=_steward_proposer(arguments),
    )


def _dispatch_steward_stale_mark_tool(tool_name: str, arguments: dict, steward: object) -> dict:
    return steward.stale_mark(
        memory_id=_require_non_empty_string(arguments, "memory_id", tool_name=tool_name),
        reason=_require_non_empty_string(arguments, "reason", tool_name=tool_name),
        proposer=_steward_proposer(arguments),
    )


def _dispatch_steward_supersede_propose_tool(tool_name: str, arguments: dict, steward: object) -> dict:
    return steward.supersede_propose(
        old_memory_id=_require_non_empty_string(arguments, "old_memory_id", tool_name=tool_name),
        source_span=steward.select_source_span(arguments),
        proposer=_steward_proposer(arguments),
    )


def restricted_steward_handler_registry() -> dict[str, ToolHandler]:
    return steward_restricted_handler_registry()


def steward_restricted_handler_registry() -> dict[str, ToolHandler]:
    registry = _tool_handlers_for_dispatch_owner("brain_steward_restricted")
    _validate_restricted_steward_handler_registry(registry)
    return registry


def _tool_handlers_for_dispatch_owner(dispatch_owner: str) -> dict[str, ToolHandler]:
    return {
        name: runtime_contract.handler
        for name, runtime_contract in tool_runtime_contract_registry().items()
        if runtime_contract.dispatch_owner == dispatch_owner
    }


def _steward_restricted_dispatch_registry() -> dict[str, StewardRestrictedDispatch]:
    dispatches: tuple[tuple[str, StewardRestrictedDispatch], ...] = (
        (MEMORY_CANDIDATE_APPROVE_TOOL_NAME, _dispatch_steward_candidate_approve_tool),
        (MEMORY_CANDIDATE_REJECT_TOOL_NAME, _dispatch_steward_candidate_reject_tool),
        (MEMORY_CANDIDATE_AUTO_ACCEPT_TOOL_NAME, _dispatch_steward_candidate_auto_accept_tool),
        (MEMORY_SUPERSEDE_COMMIT_TOOL_NAME, _dispatch_steward_supersede_commit_tool),
        (MEMORY_STALE_COMMIT_TOOL_NAME, _dispatch_steward_stale_commit_tool),
    )
    return dict(dispatches)


def _bind_restricted_steward_handler(tool_name: str, dispatch: StewardRestrictedDispatch) -> ToolHandler:
    def handle(arguments: dict, service: KnowledgeSearchService) -> dict:
        steward = service.brain_steward()
        # restricted tools: 기본 권한에서는 어떤 write 도 하지 않고 거부한다.
        try:
            result = dispatch(tool_name, arguments, steward)
        except StewardPermissionError:
            # denied 계약은 service 가 소유한다(M5). dispatch 는 그대로 전달만 한다.
            return _tool_result(steward.restricted_denied_payload(tool_name))
        # restricted commit 은 accepted/current authority 를 바꾼다. 같은 세션 card 캐시를
        # 무효화해 이후 core_brain() 읽기가 demote 전 snapshot 을 반환하지 않게 한다(read-after-write).
        service.invalidate_brain_card_cache()
        return _tool_result(result)

    return handle


def _validate_restricted_steward_handler_registry(registry: dict[str, ToolHandler]) -> None:
    expected = set(STEWARD_RESTRICTED_TOOL_NAMES)
    actual = set(registry)
    missing = sorted(expected - actual)
    if missing:
        raise ValueError(f"restricted steward tools missing handlers: {missing}")
    stale = sorted(actual - expected)
    if stale:
        raise ValueError(f"restricted steward handlers are stale: {stale}")


def _dispatch_steward_candidate_approve_tool(tool_name: str, arguments: dict, steward: object) -> dict:
    return steward.candidate_approve(
        candidate_memory_id=_require_non_empty_string(arguments, "candidate_memory_id", tool_name=tool_name),
        approved_by=_require_non_empty_string(arguments, "approved_by", tool_name=tool_name),
        decision_id=_require_non_empty_string(arguments, "decision_id", tool_name=tool_name),
    )


def _dispatch_steward_candidate_reject_tool(tool_name: str, arguments: dict, steward: object) -> dict:
    return steward.candidate_reject(
        candidate_memory_id=_require_non_empty_string(arguments, "candidate_memory_id", tool_name=tool_name),
        rejected_by=_require_non_empty_string(arguments, "rejected_by", tool_name=tool_name),
        decision_id=_require_non_empty_string(arguments, "decision_id", tool_name=tool_name),
        reason=_require_non_empty_string(arguments, "reason", tool_name=tool_name),
    )


def _dispatch_steward_candidate_auto_accept_tool(tool_name: str, arguments: dict, steward: object) -> dict:
    evaluation = arguments.get("evaluation")
    if not isinstance(evaluation, dict):
        raise ValueError("memory_candidate_auto_accept requires an evaluation object")
    return steward.candidate_auto_accept(
        candidate_memory_id=_require_non_empty_string(arguments, "candidate_memory_id", tool_name=tool_name),
        evaluation=evaluation,
        operator_approval_ref=_require_non_empty_string(arguments, "operator_approval_ref", tool_name=tool_name),
    )


def _dispatch_steward_supersede_commit_tool(tool_name: str, arguments: dict, steward: object) -> dict:
    return steward.supersede_commit(
        proposal_memory_id=_require_non_empty_string(arguments, "proposal_memory_id", tool_name=tool_name),
        approved_by=_require_non_empty_string(arguments, "approved_by", tool_name=tool_name),
        decision_id=_require_non_empty_string(arguments, "decision_id", tool_name=tool_name),
    )


def _dispatch_steward_stale_commit_tool(tool_name: str, arguments: dict, steward: object) -> dict:
    return steward.stale_commit(
        proposal_memory_id=_require_non_empty_string(arguments, "proposal_memory_id", tool_name=tool_name),
        approved_by=_require_non_empty_string(arguments, "approved_by", tool_name=tool_name),
        decision_id=_require_non_empty_string(arguments, "decision_id", tool_name=tool_name),
    )


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
