from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from .._util import ensure_public_safe, public_safe_text
from .runtime_readiness import (
    EVIDENCE_PROVENANCE_SCHEMA,
    REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES,
    RUNTIME_READINESS_AGENT_CONTEXT_TOOL,
)

POST_DEPLOY_MCP_CAPTURE_SCHEMA = "source_to_candidate_runtime_post_deploy_mcp_capture.v1"
PROTECTED_OUTPUT_FLAGS = (
    "raw_private_evidence_returned",
    "secret_returned",
    "host_topology_returned",
    "raw_external_ids_returned",
)


def validate_post_deploy_mcp_url(mcp_url: str) -> str:
    safe_url = str(mcp_url or "").strip()
    parsed = urlparse(safe_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("mcp url must be an http(s) endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("mcp url must not include credentials, query, or fragment")
    return safe_url


@asynccontextmanager
async def _default_mcp_session(mcp_url: str) -> AsyncIterator[Any]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(mcp_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            yield session


async def collect_source_to_candidate_post_deploy_mcp_capture(
    *,
    mcp_url: str,
    repository: str = "",
    branch: str = "",
    consumer: str = "codex",
    expected_commit: str = "",
    deployed_identity: Mapping[str, Any] | None = None,
    session_factory: Any = None,
) -> dict[str, Any]:
    """Collect sanitized read-only runtime evidence from a deployed MCP HTTP endpoint."""

    safe_url = validate_post_deploy_mcp_url(mcp_url)
    factory = session_factory or _default_mcp_session
    async with factory(safe_url) as session:
        await session.initialize()
        tool_names = await _collect_tool_names(session)
        plan = await _call_tool_mapping(
            session,
            RUNTIME_READINESS_AGENT_CONTEXT_TOOL,
            {
                "evidence_collection_plan": True,
                "expected_commit": expected_commit,
                "repository": repository,
                "branch": branch,
                "consumer": consumer,
            },
        )
        runtime_packet = await _call_tool_mapping(
            session,
            RUNTIME_READINESS_AGENT_CONTEXT_TOOL,
            {
                "collect_shadow_evidence": True,
                "expected_commit": expected_commit,
                "repository": repository,
                "branch": branch,
                "consumer": consumer,
                "evidence_collection_mode": "post_deploy_read_only_smoke",
                "evidence_collection_network_used": True,
            },
        )
        context_pack = await _call_tool_mapping(
            session,
            "brain_context_resolve",
            {
                "repository": repository,
                "branch": branch,
                "current_files": [],
                "current_request": (
                    "source-to-candidate runtime readiness post-deploy "
                    "agent context product capture"
                ),
                "limit": 8,
                "response_mode": "full",
                "consumer": consumer,
            },
        )
        smokes = [
            _route_smoke_from_call(
                route=route,
                raw=await _call_tool_mapping(
                    session,
                    "brain_objects_query",
                    {
                        "repository": repository,
                        "branch": branch,
                        "query": f"source-to-candidate runtime readiness post-deploy route smoke: {route}",
                        "current_files": [],
                        "route": route,
                        "limit": 5,
                        "response_mode": "full",
                        "consumer": consumer,
                    },
                ),
            )
            for route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES
        ]

    identity = (
        _public_safe_mapping(deployed_identity)
        if isinstance(deployed_identity, Mapping)
        else {
            "contains_expected_commit": False,
            "identity_source": "post_deploy_mcp_capture_missing_deployed_identity",
        }
    )
    provenance = _post_deploy_provenance(runtime_packet)
    capture = {
        "schema_version": POST_DEPLOY_MCP_CAPTURE_SCHEMA,
        "tool_names": tool_names,
        "runtime_readiness_plan": _public_safe_mapping(plan),
        "runtime_collected_packet": _runtime_collected_packet_summary(runtime_packet),
        "agent_context_product": _agent_context_product_from_context_pack(context_pack),
        "brain_objects_query_smokes": smokes,
        "deployed_identity": identity,
        "collection": provenance,
        "evidence_provenance": provenance,
        "production_mutation_performed": _runtime_packet_reports_mutation(runtime_packet),
    }
    projection_join = _live_projection_join_from_runtime_packet(runtime_packet)
    if projection_join:
        capture["projection_join"] = projection_join
    session_project_rollup = _live_session_project_rollup_from_runtime_packet(runtime_packet)
    if session_project_rollup:
        capture["session_project_rollup_runtime"] = session_project_rollup
    preference_artifact_memory = _live_preference_artifact_memory_from_runtime_packet(runtime_packet)
    if preference_artifact_memory:
        capture["preference_artifact_memory"] = preference_artifact_memory
    ensure_public_safe(capture, "SourceToCandidatePostDeployMcpCapture")
    return capture


async def _collect_tool_names(session: Any) -> list[str]:
    tools_result = await session.list_tools()
    tools = getattr(tools_result, "tools", [])
    return [
        public_safe_text(str(getattr(tool, "name", "") or ""), max_chars=120)
        for tool in tools
        if str(getattr(tool, "name", "") or "")
    ]


async def _call_tool_mapping(session: Any, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    try:
        result = await session.call_tool(name, dict(arguments))
    except Exception as exc:  # pragma: no cover - defensive transport guard
        return {
            "collector_error_type": public_safe_text(type(exc).__name__, max_chars=80),
            "collector_call_failed": True,
        }
    if getattr(result, "isError", False) is True:
        return {"collector_call_failed": True, "collector_error_type": "McpToolError"}
    structured = getattr(result, "structuredContent", None)
    return _public_safe_mapping(structured)


def _agent_context_product_from_context_pack(context_pack: Mapping[str, Any]) -> dict[str, Any]:
    pack = _public_safe_mapping(context_pack)
    authority = pack.get("authority") if isinstance(pack.get("authority"), Mapping) else {}
    product = authority.get("agent_context_product")
    if not isinstance(product, Mapping):
        product = pack.get("agent_context_product")
    if isinstance(product, Mapping):
        return _public_safe_mapping(product)
    return {
        "schema_version": "",
        "sections": {},
        "surface_policy": {"mutation_allowed": False},
        "missing_evidence_before_promotion": ["agent_context_product_capture_failed"],
        "tool_hints": [],
        "collector_error_type": public_safe_text(
            str(pack.get("collector_error_type") or "missing_agent_context_product"),
            max_chars=80,
        ),
    }


def _route_smoke_from_call(*, route: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    smoke = _public_safe_mapping(raw)
    if smoke.get("collector_call_failed") is True:
        smoke = {
            "schema_version": "brain_objects_query.v1",
            "route": route,
            "collector_error_type": public_safe_text(
                str(smoke.get("collector_error_type") or "McpToolError"),
                max_chars=80,
            ),
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": route,
                "objects": [],
                "edges": [],
                "evidence": [],
                "recommended_actions": [],
                "lanes": {},
                "gaps": ["collector_route_smoke_failed"],
            },
        }
    smoke["schema_version"] = public_safe_text(
        str(smoke.get("schema_version") or "brain_objects_query.v1"),
        max_chars=80,
    )
    smoke["route"] = public_safe_text(str(smoke.get("route") or route), max_chars=120)
    smoke["production_mutation_performed"] = False
    object_pack = smoke.get("object_pack") if isinstance(smoke.get("object_pack"), Mapping) else {}
    if not object_pack:
        object_pack = {
            "schema_version": "object_pack.v1",
            "route": route,
            "objects": [],
            "edges": [],
            "evidence": [],
            "recommended_actions": [],
            "lanes": {},
            "gaps": ["collector_route_smoke_missing_object_pack"],
        }
    else:
        object_pack = _public_safe_mapping(object_pack)
        object_pack["schema_version"] = public_safe_text(
            str(object_pack.get("schema_version") or "object_pack.v1"),
            max_chars=80,
        )
        object_pack["route"] = public_safe_text(str(object_pack.get("route") or route), max_chars=120)
        object_pack["production_mutation_performed"] = False
    smoke["object_pack"] = object_pack
    ensure_public_safe(smoke, "SourceToCandidatePostDeployMcpRouteSmoke")
    return smoke


def _runtime_collected_packet_summary(packet: Mapping[str, Any]) -> dict[str, Any]:
    safe_packet = _public_safe_mapping(packet)
    collector = safe_packet.get("collector") if isinstance(safe_packet.get("collector"), Mapping) else {}
    provenance = (
        safe_packet.get("evidence_provenance")
        if isinstance(safe_packet.get("evidence_provenance"), Mapping)
        else {}
    )
    projection = (
        safe_packet.get("projection_join")
        if isinstance(safe_packet.get("projection_join"), Mapping)
        else {}
    )
    rollup = (
        safe_packet.get("session_project_rollup_runtime")
        if isinstance(safe_packet.get("session_project_rollup_runtime"), Mapping)
        else {}
    )
    rollup_preview = (
        rollup.get("rollup_preview") if isinstance(rollup.get("rollup_preview"), Mapping) else {}
    )
    object_type_counts = (
        rollup_preview.get("object_type_counts")
        if isinstance(rollup_preview.get("object_type_counts"), Mapping)
        else {}
    )
    preference = (
        safe_packet.get("preference_artifact_memory")
        if isinstance(safe_packet.get("preference_artifact_memory"), Mapping)
        else {}
    )
    preference_pack = (
        preference.get("preference_object_pack")
        if isinstance(preference.get("preference_object_pack"), Mapping)
        else {}
    )
    artifact_check = (
        preference.get("artifact_review_check")
        if isinstance(preference.get("artifact_review_check"), Mapping)
        else {}
    )
    projection_promoted = bool(_live_projection_join_from_runtime_packet(safe_packet))
    session_project_rollup_promoted = bool(
        _live_session_project_rollup_from_runtime_packet(safe_packet)
    )
    preference_artifact_memory_promoted = bool(
        _live_preference_artifact_memory_from_runtime_packet(safe_packet)
    )
    preference_artifact_memory_blockers = _preference_artifact_memory_promotion_blockers(
        safe_packet
    )
    summary = {
        "schema_version": public_safe_text(str(safe_packet.get("schema_version") or ""), max_chars=80),
        "collector_readiness_claim": public_safe_text(str(collector.get("readiness_claim") or ""), max_chars=120),
        "projection_join_present": bool(projection),
        "projection_join_schema": public_safe_text(str(projection.get("schema_version") or ""), max_chars=80),
        "projection_join_edge_count": _safe_int(projection.get("edge_count")),
        "projection_join_promoted_to_live_evidence": projection_promoted,
        "session_project_rollup_present": bool(rollup),
        "session_project_rollup_schema": public_safe_text(
            str(rollup.get("schema_version") or ""),
            max_chars=80,
        ),
        "session_project_rollup_preview_schema": public_safe_text(
            str(rollup_preview.get("schema_version") or ""),
            max_chars=80,
        ),
        "session_project_rollup_device_count": _safe_int(rollup_preview.get("device_count")),
        "session_project_rollup_work_unit_count": _safe_int(object_type_counts.get("WorkUnit")),
        "session_project_rollup_promoted_to_live_evidence": session_project_rollup_promoted,
        "preference_artifact_memory_present": bool(preference),
        "preference_artifact_memory_schema": public_safe_text(
            str(preference.get("schema_version") or ""),
            max_chars=80,
        ),
        "preference_artifact_accepted_preference_count": _safe_int(
            preference_pack.get("accepted_preference_count")
        ),
        "preference_artifact_proposal_preference_count": _safe_int(
            preference_pack.get("proposal_preference_count")
        ),
        "preference_artifact_review_check_status": public_safe_text(
            str(artifact_check.get("status") or ""),
            max_chars=80,
        ),
        "preference_artifact_memory_promoted_to_live_evidence": (
            preference_artifact_memory_promoted
        ),
        "preference_artifact_memory_promotion_blockers": preference_artifact_memory_blockers,
        "evidence_collection_mode": public_safe_text(str(provenance.get("collection_mode") or ""), max_chars=80),
        "evidence_collection_network_used": provenance.get("network_used") is True,
        "production_mutation_performed": safe_packet.get("production_mutation_performed") is True,
    }
    ensure_public_safe(summary, "SourceToCandidatePostDeployRuntimePacketSummary")
    return summary


def _live_projection_join_from_runtime_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    safe_packet = _public_safe_mapping(packet)
    projection = safe_packet.get("projection_join")
    if not isinstance(projection, Mapping):
        return {}
    collector = safe_packet.get("collector") if isinstance(safe_packet.get("collector"), Mapping) else {}
    if str(collector.get("readiness_claim") or "") == "collector_packet_not_live_evidence":
        return {}
    provenance = (
        safe_packet.get("evidence_provenance")
        if isinstance(safe_packet.get("evidence_provenance"), Mapping)
        else {}
    )
    if str(provenance.get("collection_mode") or "") != "post_deploy_read_only_smoke":
        return {}
    if provenance.get("network_used") is not True:
        return {}
    if _runtime_packet_reports_mutation(safe_packet):
        return {}
    if _runtime_packet_reports_protected_output(safe_packet):
        return {}
    if _postcheck_reports_protected_output(projection):
        return {}
    if str(projection.get("status") or "") != "pass":
        return {}
    live_projection = _public_safe_mapping(projection)
    ensure_public_safe(live_projection, "SourceToCandidatePostDeployProjectionJoin")
    return live_projection


def _live_session_project_rollup_from_runtime_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    safe_packet = _public_safe_mapping(packet)
    rollup = safe_packet.get("session_project_rollup_runtime")
    if not isinstance(rollup, Mapping):
        return {}
    collector = safe_packet.get("collector") if isinstance(safe_packet.get("collector"), Mapping) else {}
    if str(collector.get("readiness_claim") or "") == "collector_packet_not_live_evidence":
        return {}
    provenance = (
        safe_packet.get("evidence_provenance")
        if isinstance(safe_packet.get("evidence_provenance"), Mapping)
        else {}
    )
    if str(provenance.get("collection_mode") or "") != "post_deploy_read_only_smoke":
        return {}
    if provenance.get("network_used") is not True:
        return {}
    if _runtime_packet_reports_mutation(safe_packet):
        return {}
    if _runtime_packet_reports_protected_output(safe_packet):
        return {}
    if _postcheck_reports_protected_output(rollup):
        return {}
    live_rollup = _public_safe_mapping(rollup)
    ensure_public_safe(live_rollup, "SourceToCandidatePostDeploySessionProjectRollup")
    return live_rollup


def _live_preference_artifact_memory_from_runtime_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    safe_packet = _public_safe_mapping(packet)
    if _preference_artifact_memory_promotion_blockers(safe_packet):
        return {}
    preference = safe_packet.get("preference_artifact_memory")
    if not isinstance(preference, Mapping):
        return {}
    live_preference = _public_safe_mapping(preference)
    ensure_public_safe(live_preference, "SourceToCandidatePostDeployPreferenceArtifactMemory")
    return live_preference


def _preference_artifact_memory_promotion_blockers(packet: Mapping[str, Any]) -> list[str]:
    safe_packet = _public_safe_mapping(packet)
    preference = safe_packet.get("preference_artifact_memory")
    if not isinstance(preference, Mapping):
        return ["preference_artifact_memory_missing"]
    if str(preference.get("evidence_class") or "") != "runtime_preference_artifact_memory":
        return ["preference_artifact_memory_evidence_class_missing"]
    collector = safe_packet.get("collector") if isinstance(safe_packet.get("collector"), Mapping) else {}
    if str(collector.get("readiness_claim") or "") == "collector_packet_not_live_evidence":
        return ["collector_packet_not_live_evidence"]
    provenance = (
        safe_packet.get("evidence_provenance")
        if isinstance(safe_packet.get("evidence_provenance"), Mapping)
        else {}
    )
    if str(provenance.get("collection_mode") or "") != "post_deploy_read_only_smoke":
        return ["preference_artifact_memory_not_post_deploy_read_only_smoke"]
    if provenance.get("network_used") is not True:
        return ["preference_artifact_memory_network_not_used"]
    if _runtime_packet_reports_mutation(safe_packet):
        return ["preference_artifact_memory_mutation_reported"]
    if _runtime_packet_reports_protected_output(safe_packet):
        return ["preference_artifact_memory_protected_output_reported"]
    if _postcheck_reports_protected_output(preference):
        return ["preference_artifact_memory_postcheck_protected_output"]
    if not _preference_artifact_has_accepted_current_lane(preference):
        return ["preference_artifact_accepted_current_lane_missing"]
    artifact_check = (
        preference.get("artifact_review_check")
        if isinstance(preference.get("artifact_review_check"), Mapping)
        else {}
    )
    if artifact_check.get("raw_artifact_body_returned") is not False:
        return ["preference_artifact_raw_artifact_body_returned"]
    context = (
        preference.get("agent_context_preference_section")
        if isinstance(preference.get("agent_context_preference_section"), Mapping)
        else {}
    )
    lanes = context.get("authority_lanes") if isinstance(context.get("authority_lanes"), list) else []
    safe_lanes = [public_safe_text(str(lane or ""), max_chars=80) for lane in lanes if lane]
    if "accepted_current" not in safe_lanes:
        return ["preference_artifact_agent_context_accepted_current_missing"]
    return []


def _preference_artifact_has_accepted_current_lane(preference: Mapping[str, Any]) -> bool:
    pack = (
        preference.get("preference_object_pack")
        if isinstance(preference.get("preference_object_pack"), Mapping)
        else {}
    )
    lanes = pack.get("lanes") if isinstance(pack.get("lanes"), Mapping) else {}
    accepted = lanes.get("accepted_current") if isinstance(lanes.get("accepted_current"), list) else []
    return any(
        isinstance(obj, Mapping)
        and obj.get("object_type") == "ArtifactPreference"
        and obj.get("authority_lane") == "accepted_current"
        for obj in accepted
    )


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _runtime_packet_reports_mutation(packet: Mapping[str, Any]) -> bool:
    safe_packet = _public_safe_mapping(packet)
    provenance = (
        safe_packet.get("evidence_provenance")
        if isinstance(safe_packet.get("evidence_provenance"), Mapping)
        else {}
    )
    mutation_scope = public_safe_text(str(provenance.get("mutation_scope") or ""), max_chars=80)
    return (
        safe_packet.get("production_mutation_performed") is True
        or safe_packet.get("mutation_performed") is True
        or bool(mutation_scope and mutation_scope != "none")
    )


def _runtime_packet_reports_protected_output(packet: Mapping[str, Any]) -> bool:
    safe_packet = _public_safe_mapping(packet)
    provenance = (
        safe_packet.get("evidence_provenance")
        if isinstance(safe_packet.get("evidence_provenance"), Mapping)
        else {}
    )
    return any(
        safe_packet.get(field) is True or provenance.get(field) is True
        for field in PROTECTED_OUTPUT_FLAGS
    )


def _postcheck_reports_protected_output(evidence: Mapping[str, Any]) -> bool:
    postcheck = evidence.get("postcheck") if isinstance(evidence.get("postcheck"), Mapping) else {}
    if postcheck.get("status") != "validated":
        return True
    return any(postcheck.get(field) is not False for field in PROTECTED_OUTPUT_FLAGS)


def _post_deploy_provenance(runtime_packet: Mapping[str, Any] | None = None) -> dict[str, Any]:
    safe_packet = _public_safe_mapping(runtime_packet or {})
    runtime_provenance = (
        safe_packet.get("evidence_provenance")
        if isinstance(safe_packet.get("evidence_provenance"), Mapping)
        else {}
    )
    mutation_scope = public_safe_text(
        str(runtime_provenance.get("mutation_scope") or "none"),
        max_chars=80,
    )
    return {
        "schema_version": EVIDENCE_PROVENANCE_SCHEMA,
        "collector": "source_to_candidate_post_deploy_mcp_capture",
        "collection_mode": "post_deploy_read_only_smoke",
        "network_used": True,
        "mutation_scope": mutation_scope,
        "raw_private_evidence_returned": runtime_provenance.get("raw_private_evidence_returned") is True,
        "secret_returned": runtime_provenance.get("secret_returned") is True,
        "host_topology_returned": runtime_provenance.get("host_topology_returned") is True,
        "raw_external_ids_returned": runtime_provenance.get("raw_external_ids_returned") is True,
    }


def _public_safe_mapping(value: Any) -> dict[str, Any]:
    safe = dict(value) if isinstance(value, Mapping) else {}
    ensure_public_safe(safe, "SourceToCandidatePostDeployMcpMapping")
    return safe
