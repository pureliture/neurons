from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .._util import ensure_public_safe, public_safe_text
from .golden_query_eval import build_source_to_authority_quality_gate_report

REQUIRED_REVIEW_TOOL_NAMES = (
    "brain_objects_query",
    "brain_source_to_candidate_graph",
    "brain_candidate_review_edit",
    "brain_approval_board_decide",
)
REQUIRED_RUNTIME_TOOL_NAMES = (
    *REQUIRED_REVIEW_TOOL_NAMES,
    "brain_source_to_candidate_runtime_readiness",
)
REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES = (
    "authority_archive_separation",
    "code_style_preference",
    "temporal_work_recall",
    "deployment_runtime_truth",
)
REQUIRED_AGENT_CONTEXT_SECTIONS = (
    "style_preference",
    "active_work",
    "required_verification",
)
PRODUCTION_DENIAL_CLAIMS = (
    ("live.production.source_to_candidate_denial", "brain_source_to_candidate_graph"),
    ("live.production.approval_board_denial", "brain_approval_board_decide"),
    ("live.production.object_proposal_denial", "brain_object_proposal_create"),
    ("live.production.object_decision_denial", "brain_object_decision_commit"),
)
OBJECT_AUTHORITY_PRODUCTION_GATE_TOOLS = (
    "brain_object_proposal_create",
    "brain_object_decision_commit",
)
OBJECT_AUTHORITY_PRODUCTION_RUNTIME_FLAG = "--allow-object-authority-production-writes"
PERMISSION_SENSITIVE_AGENT_CONTEXT_TOOLS = ("brain_approval_board_decide",)


def build_source_to_candidate_runtime_readiness_report(
    *,
    live_evidence: Mapping[str, Any] | None = None,
    expected_commit: str = "",
) -> dict[str, Any]:
    evidence = live_evidence if isinstance(live_evidence, Mapping) else {}
    local_gate = build_source_to_authority_quality_gate_report()
    claims = [
        _local_product_surface_claim(local_gate),
        _live_tools_claim(evidence),
        _live_agent_context_tool_hints_claim(evidence),
        _live_agent_context_product_sections_claim(evidence),
        _live_brain_objects_query_route_smokes_claim(evidence),
        _live_deployed_identity_claim(evidence, expected_commit=expected_commit),
        _live_object_authority_production_gate_policy_claim(evidence),
        *[
            _production_denial_claim(evidence, claim_id=claim_id, tool_name=tool_name)
            for claim_id, tool_name in PRODUCTION_DENIAL_CLAIMS
        ],
    ]
    gaps = _dedupe(
        gap
        for claim in claims
        for gap in claim.get("gaps", [])
        if isinstance(gap, str) and gap
    )
    failed = [claim["claim_id"] for claim in claims if claim["status"] == "failed"]
    report = {
        "schema_version": "source_to_candidate_runtime_readiness.v1",
        "status": "FAIL" if failed else ("PASS_WITH_GAPS" if gaps else "PASS"),
        "claims": claims,
        "failed_claims": failed,
        "gaps": gaps,
        "expected_commit": public_safe_text(str(expected_commit or ""), max_chars=80),
        "live_evidence_provided": bool(evidence),
        "production_mutation_performed": any(_claim_reports_mutation(claim) for claim in claims),
        "network_used": False,
        "local_gate_status": local_gate["status"],
        "release_quality_gate": "not_green" if gaps else "green",
    }
    ensure_public_safe(report, "SourceToCandidateRuntimeReadiness")
    return report


def _local_product_surface_claim(local_gate: Mapping[str, Any]) -> dict[str, Any]:
    checks = local_gate.get("product_surface_checks")
    product_checks = checks if isinstance(checks, list) else []
    failed = [
        str(item.get("id") or "")
        for item in product_checks
        if isinstance(item, Mapping) and item.get("result") != "PASS"
    ]
    return {
        "claim_id": "local.product_surface_checks",
        "evidence_class": "local_test",
        "status": "failed" if failed else "validated",
        "result": "FAIL" if failed else "PASS",
        "covered_tools": list(REQUIRED_RUNTIME_TOOL_NAMES),
        "gaps": ["local_product_surface_checks_failed"] if failed else [],
        "failed_checks": failed,
    }


def _live_tools_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    tool_names = set(_string_list(evidence.get("tool_names")))
    missing = [name for name in REQUIRED_RUNTIME_TOOL_NAMES if name not in tool_names]
    return {
        "claim_id": "live.mcp.review_tools_loaded",
        "evidence_class": "runtime_read_path",
        "status": "not_validated" if missing else "validated",
        "required_tools": list(REQUIRED_RUNTIME_TOOL_NAMES),
        "missing_tools": missing,
        "gaps": ["live_mcp_review_tools_unverified"] if missing else [],
    }


def _live_agent_context_tool_hints_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    tool_hints = _agent_context_tool_hints(evidence)
    hints_by_tool = {
        str(item.get("tool") or ""): item
        for item in tool_hints
        if isinstance(item, Mapping) and str(item.get("tool") or "")
    }
    hinted_tools = set(hints_by_tool)
    missing = [name for name in REQUIRED_RUNTIME_TOOL_NAMES if name not in hinted_tools]
    safety_failures = [
        failure
        for name in REQUIRED_RUNTIME_TOOL_NAMES
        if name in hints_by_tool
        for failure in _agent_context_tool_hint_safety_failures(name, hints_by_tool[name])
    ]
    base = {
        "claim_id": "live.agent_context.tool_hints",
        "evidence_class": "runtime_read_path",
        "required_tools": list(REQUIRED_RUNTIME_TOOL_NAMES),
        "missing_tools": missing,
        "unsafe_tool_hints": safety_failures,
    }
    if safety_failures:
        return {
            **base,
            "status": "failed",
            "gaps": [*safety_failures, *(["live_agent_context_tool_hints_unverified"] if missing else [])],
        }
    return {
        **base,
        "status": "not_validated" if missing else "validated",
        "gaps": ["live_agent_context_tool_hints_unverified"] if missing else [],
    }


def _live_agent_context_product_sections_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    product = _agent_context_product(evidence)
    sections = product.get("sections") if isinstance(product.get("sections"), Mapping) else {}
    missing = [
        name
        for name in REQUIRED_AGENT_CONTEXT_SECTIONS
        if _section_object_count(sections.get(name)) < 1
    ]
    mutation_allowed = (
        product.get("surface_policy") if isinstance(product.get("surface_policy"), Mapping) else {}
    ).get("mutation_allowed")
    base = {
        "claim_id": "live.agent_context.product_sections",
        "evidence_class": "runtime_read_path",
        "required_sections": list(REQUIRED_AGENT_CONTEXT_SECTIONS),
        "missing_sections": missing,
        "mutation_allowed": bool(mutation_allowed),
    }
    if bool(mutation_allowed):
        return {
            **base,
            "status": "failed",
            "gaps": ["live_agent_context_mutation_allowed"],
        }
    return {
        **base,
        "status": "not_validated" if missing else "validated",
        "gaps": ["live_agent_context_product_sections_unverified"] if missing else [],
    }


def _live_brain_objects_query_route_smokes_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    smokes = evidence.get("brain_objects_query_smokes")
    smoke_items = [dict(item) for item in smokes if isinstance(item, Mapping)] if isinstance(smokes, list) else []
    by_route = {
        str(item.get("route") or (item.get("object_pack") or {}).get("route") or ""): item
        for item in smoke_items
    }
    missing = [route for route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES if route not in by_route]
    failures = [
        failure
        for route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES
        if route in by_route
        for failure in _brain_objects_query_smoke_failures(route, by_route[route])
    ]
    base = {
        "claim_id": "live.brain_objects_query.route_smokes",
        "evidence_class": "runtime_read_path",
        "required_routes": list(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES),
        "validated_routes": sorted(route for route in by_route if route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES),
        "missing_routes": missing,
        "production_mutation_performed": _object_query_smokes_report_mutation(smoke_items),
    }
    if failures:
        return {
            **base,
            "status": "failed",
            "gaps": failures,
        }
    return {
        **base,
        "status": "not_validated" if missing else "validated",
        "gaps": ["live_brain_objects_query_route_smokes_unverified"] if missing else [],
    }


def _live_deployed_identity_claim(evidence: Mapping[str, Any], *, expected_commit: str) -> dict[str, Any]:
    identity = evidence.get("deployed_identity")
    identity = identity if isinstance(identity, Mapping) else {}
    contains_expected = identity.get("contains_expected_commit") is True
    gap = "" if contains_expected else "live_deployed_identity_unverified"
    return {
        "claim_id": "live.deployed_identity.includes_expected_commit",
        "evidence_class": "runtime_artifact_identity",
        "status": "validated" if contains_expected else "not_validated",
        "expected_commit": public_safe_text(str(expected_commit or ""), max_chars=80),
        "identity_source": public_safe_text(str(identity.get("identity_source") or ""), max_chars=160),
        "gaps": [gap] if gap else [],
    }


def _live_object_authority_production_gate_policy_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    tool_schemas = evidence.get("tool_schemas")
    tool_schemas = tool_schemas if isinstance(tool_schemas, Mapping) else {}
    runtime_gate = evidence.get("production_authority_gate")
    runtime_gate = runtime_gate if isinstance(runtime_gate, Mapping) else {}
    missing_schemas = [
        tool_name
        for tool_name in OBJECT_AUTHORITY_PRODUCTION_GATE_TOOLS
        if not _tool_schema_has_production_gate(tool_schemas.get(tool_name))
    ]
    has_runtime_policy = bool(runtime_gate)
    base = {
        "claim_id": "live.production.object_authority_gate_policy",
        "evidence_class": "runtime_safety_gate",
        "tools": list(OBJECT_AUTHORITY_PRODUCTION_GATE_TOOLS),
        "missing_gate_schemas": missing_schemas,
        "runtime_flag": public_safe_text(str(runtime_gate.get("runtime_flag") or ""), max_chars=120),
        "default_enabled": bool(runtime_gate.get("default_enabled")),
        "per_call_gate_required": runtime_gate.get("per_call_gate_required") is True,
        "production_mutation_performed": runtime_gate.get("production_mutation_performed") is True,
    }
    missing_evidence = []
    if missing_schemas:
        missing_evidence.extend(f"{tool_name}_production_gate_schema_missing" for tool_name in missing_schemas)
    if not has_runtime_policy:
        missing_evidence.append("object_authority_production_runtime_policy_unverified")
    runtime_failures = (
        _object_authority_runtime_gate_policy_failures(runtime_gate) if has_runtime_policy else []
    )
    if not missing_schemas and has_runtime_policy:
        if runtime_failures:
            return {
                **base,
                "status": "failed",
                "gaps": runtime_failures,
            }
        return {
            **base,
            "status": "validated",
            "gaps": [],
        }
    if runtime_failures:
        missing_evidence.extend(runtime_failures)
    if tool_schemas or runtime_gate:
        return {
            **base,
            "status": "failed",
            "gaps": missing_evidence,
        }
    return {
        **base,
        "status": "not_validated",
        "gaps": ["live_object_authority_gate_policy_unverified"],
    }


def _tool_schema_has_production_gate(schema: Any) -> bool:
    if not isinstance(schema, Mapping):
        return False
    input_schema = schema.get("inputSchema") if isinstance(schema.get("inputSchema"), Mapping) else schema
    properties = input_schema.get("properties") if isinstance(input_schema.get("properties"), Mapping) else {}
    gate = properties.get("production_gate")
    if not isinstance(gate, Mapping):
        return False
    gate_properties = gate.get("properties") if isinstance(gate.get("properties"), Mapping) else {}
    required = {
        "approved",
        "approval_ref",
        "scope",
        "project",
        "max_objects",
        "configured_deployed_mcp_identity_matches_source",
        "read_after_write_smoke_plan",
        "rollback_or_supersession_plan",
        "no_raw_private_evidence",
    }
    return required.issubset(set(str(key) for key in gate_properties))


def _object_authority_runtime_gate_policy_failures(runtime_gate: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if str(runtime_gate.get("runtime_flag") or "") != OBJECT_AUTHORITY_PRODUCTION_RUNTIME_FLAG:
        failures.append("object_authority_production_runtime_flag_unverified")
    if runtime_gate.get("default_enabled") is True:
        failures.append("object_authority_production_runtime_default_enabled")
    if runtime_gate.get("per_call_gate_required") is not True:
        failures.append("object_authority_production_per_call_gate_not_required")
    if runtime_gate.get("production_mutation_performed") is True:
        failures.append("unexpected_production_mutation")
    return failures


def _production_denial_claim(
    evidence: Mapping[str, Any],
    *,
    claim_id: str,
    tool_name: str,
) -> dict[str, Any]:
    denials = evidence.get("production_denials")
    denials = denials if isinstance(denials, Mapping) else {}
    payload = denials.get(tool_name)
    if not isinstance(payload, Mapping):
        return {
            "claim_id": claim_id,
            "evidence_class": "runtime_safety_denial",
            "tool": tool_name,
            "status": "not_validated",
            "production_mutation_performed": False,
            "gaps": [f"{tool_name}_production_denial_unverified"],
        }
    mutation_performed = (
        payload.get("production_mutation_performed") is True
        or payload.get("mutation_performed") is True
        or payload.get("authority_write_performed") is True
        or payload.get("proposal_write_performed") is True
        or payload.get("decision_write_performed") is True
    )
    denied = str(payload.get("status") or payload.get("permission") or "").lower() == "denied"
    if mutation_performed or not denied:
        return {
            "claim_id": claim_id,
            "evidence_class": "runtime_safety_denial",
            "tool": tool_name,
            "status": "failed",
            "production_mutation_performed": bool(mutation_performed),
            "gaps": ["unexpected_production_mutation"],
        }
    return {
        "claim_id": claim_id,
        "evidence_class": "runtime_safety_denial",
        "tool": tool_name,
        "status": "denied_as_expected",
        "production_mutation_performed": False,
        "gaps": [],
    }


def _agent_context_tool_hints(evidence: Mapping[str, Any]) -> list[Any]:
    product = _agent_context_product(evidence)
    hints = product.get("tool_hints") if isinstance(product, Mapping) else []
    return list(hints) if isinstance(hints, list) else []


def _agent_context_tool_hint_safety_failures(tool_name: str, hint: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if hint.get("suggest_allowed") is not True:
        failures.append(f"{tool_name}_tool_hint_suggest_not_allowed")
    if hint.get("execute_allowed") is not False:
        failures.append(f"{tool_name}_tool_hint_execute_allowed")
    if hint.get("production_mutation_allowed") is not False:
        failures.append(f"{tool_name}_tool_hint_production_mutation_allowed")
    if not _string_list(hint.get("safe_targets")):
        failures.append(f"{tool_name}_tool_hint_safe_targets_missing")
    if tool_name in PERMISSION_SENSITIVE_AGENT_CONTEXT_TOOLS and "approved_scope_required" not in _string_list(
        hint.get("blocked_by")
    ):
        failures.append(f"{tool_name}_tool_hint_approved_scope_blocker_missing")
    return failures


def _agent_context_product(evidence: Mapping[str, Any]) -> Mapping[str, Any]:
    product = evidence.get("agent_context_product")
    if isinstance(product, Mapping):
        return product
    context_pack = evidence.get("context_pack")
    authority = context_pack.get("authority") if isinstance(context_pack, Mapping) else {}
    product = authority.get("agent_context_product") if isinstance(authority, Mapping) else {}
    return product if isinstance(product, Mapping) else {}


def _section_object_count(section: Any) -> int:
    if not isinstance(section, Mapping):
        return 0
    try:
        return int(section.get("object_count") or 0)
    except (TypeError, ValueError):
        return 0


def _brain_objects_query_smoke_failures(route: str, smoke: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    object_pack = smoke.get("object_pack") if isinstance(smoke.get("object_pack"), Mapping) else {}
    gaps = [str(gap) for gap in object_pack.get("gaps", []) if str(gap or "")]
    if smoke.get("schema_version") != "brain_objects_query.v1":
        failures.append(f"brain_objects_query_schema_mismatch:{route}")
    if object_pack.get("schema_version") != "object_pack.v1":
        failures.append(f"brain_objects_query_object_pack_schema_mismatch:{route}")
    if str(smoke.get("route") or object_pack.get("route") or "") != route:
        failures.append(f"brain_objects_query_route_mismatch:{route}")
    if "object_pack_route_not_implemented" in gaps:
        failures.append(f"brain_objects_query_route_unimplemented:{route}")
    if bool(smoke.get("production_mutation_performed")) or bool(smoke.get("mutation_performed")):
        failures.append(f"brain_objects_query_mutation_performed:{route}")
    if not isinstance(object_pack.get("recommended_actions"), list):
        failures.append(f"brain_objects_query_recommended_actions_missing:{route}")
    if not isinstance(object_pack.get("lanes"), Mapping):
        failures.append(f"brain_objects_query_lanes_missing:{route}")
    return failures


def _object_query_smokes_report_mutation(smoke_items: list[Mapping[str, Any]]) -> bool:
    return any(
        bool(item.get("production_mutation_performed")) or bool(item.get("mutation_performed"))
        for item in smoke_items
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [public_safe_text(str(item), max_chars=160) for item in value if str(item or "")]


def _claim_reports_mutation(claim: Mapping[str, Any]) -> bool:
    return claim.get("production_mutation_performed") is True


def _dedupe(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
