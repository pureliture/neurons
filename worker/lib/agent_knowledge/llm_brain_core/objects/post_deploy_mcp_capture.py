from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import unquote, urlparse

from .._util import ensure_public_safe, hash_payload, public_safe_text, require_sha256
from .artifact_preference_evaluator import (
    ARTIFACT_PREFERENCE_COLLECTOR_ATTESTATION_SCHEMA,
    ARTIFACT_PREFERENCE_EVALUATOR_TOOL,
    artifact_preference_application_receipt_is_valid,
    validate_artifact_descriptor,
)
from .authority_policy import knowledge_object_class_from_id
from .runtime_readiness import (
    EVIDENCE_PROVENANCE_SCHEMA,
    REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES,
    RUNTIME_READINESS_AGENT_CONTEXT_TOOL,
    _mint_collector_attested_evidence,
)

POST_DEPLOY_MCP_CAPTURE_SCHEMA = "source_to_candidate_runtime_post_deploy_mcp_capture.v1"
PROTECTED_OUTPUT_FLAGS = (
    "raw_private_evidence_returned",
    "secret_returned",
    "host_topology_returned",
    "raw_external_ids_returned",
)
_FORBIDDEN_RUNTIME_INPUT_KEYS = frozenset(
    {
        "api_key",
        "body",
        "dataset_id",
        "document_id",
        "endpoint_url",
        "host",
        "hostname",
        "host_topology",
        "ip_address",
        "password",
        "private",
        "private_path",
        "raw",
        "raw_body",
        "raw_content",
        "raw_source",
        "raw_text",
        "secret",
        "token",
    }
)
_FORBIDDEN_RUNTIME_INPUT_COMPACT_KEYS = frozenset(
    key.replace("_", "") for key in _FORBIDDEN_RUNTIME_INPUT_KEYS
)
_RAW_EXTERNAL_REF_MARKERS = (
    "dataset:",
    "dataset_id:",
    "document:",
    "document_id:",
    "ragflow_dataset:",
    "ragflow_document:",
)
_ARTIFACT_REF_SUFFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_RAW_EXTERNAL_REF_SUFFIX_RE = re.compile(
    r"^(?:ragflow[._-])?(?:dataset|document)(?:[._-]|$)",
    re.IGNORECASE,
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
    project: str = "",
    consumer: str = "codex",
    expected_commit: str = "",
    deployed_identity: Mapping[str, Any] | None = None,
    artifact_descriptor: Mapping[str, Any] | None = None,
    session_factory: Any = None,
) -> dict[str, Any]:
    """Collect sanitized read-only runtime evidence from a deployed MCP HTTP endpoint."""

    safe_url = validate_post_deploy_mcp_url(mcp_url)
    validated_artifact_descriptor = (
        validate_artifact_descriptor(artifact_descriptor)
        if artifact_descriptor is not None
        else None
    )
    explicit_project = str(project or "").strip()
    safe_project = public_safe_text(explicit_project, max_chars=120) or "neurons"
    project_source = "explicit" if explicit_project else "collector_default"
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
                "project": safe_project,
                "consumer": consumer,
            },
        )
        runtime_read_arguments = {
            "collect_shadow_evidence": True,
            "expected_commit": expected_commit,
            "repository": repository,
            "branch": branch,
            "project": safe_project,
            "consumer": consumer,
            "evidence_collection_mode": "post_deploy_read_only_smoke",
            "evidence_collection_network_used": True,
        }
        runtime_packet = await _call_tool_mapping(
            session,
            RUNTIME_READINESS_AGENT_CONTEXT_TOOL,
            runtime_read_arguments,
        )
        context_pack = await _call_tool_mapping(
            session,
            "brain_context_resolve",
            {
                "repository": repository,
                "branch": branch,
                "project": safe_project,
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
                        "project": safe_project,
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
        direct_receipt: dict[str, Any] = {}
        post_evaluator_runtime_packet = runtime_packet
        if (
            validated_artifact_descriptor is not None
            and project_source == "explicit"
            and ARTIFACT_PREFERENCE_EVALUATOR_TOOL in tool_names
        ):
            direct_receipt = await _call_tool_untrusted_mapping(
                session,
                ARTIFACT_PREFERENCE_EVALUATOR_TOOL,
                {
                    "repository": repository,
                    "branch": branch,
                    "project": safe_project,
                    **validated_artifact_descriptor,
                    "consumer": "post_deploy_mcp_capture",
                },
            )
            post_evaluator_runtime_packet = await _call_tool_mapping(
                session,
                RUNTIME_READINESS_AGENT_CONTEXT_TOOL,
                runtime_read_arguments,
            )

    identity = _deployed_identity_view(deployed_identity)
    try:
        _reject_forbidden_runtime_input_keys(runtime_packet)
        _reject_forbidden_runtime_input_keys(post_evaluator_runtime_packet)
        initial_attested_runtime_packet = _attest_preference_artifact_memory(
            runtime_packet,
            deployed_identity=identity,
            expected_commit=expected_commit,
            streamable_http_network_capture=True,
        )
        attested_runtime_packet = _attest_preference_artifact_memory(
            post_evaluator_runtime_packet,
            deployed_identity=identity,
            expected_commit=expected_commit,
            streamable_http_network_capture=True,
        )
        if validated_artifact_descriptor is not None and project_source == "explicit":
            attested_runtime_packet, accepted_receipt = _attach_direct_application_receipt(
                attested_runtime_packet,
                prior_packet=initial_attested_runtime_packet,
                direct_receipt=direct_receipt,
                artifact_descriptor=validated_artifact_descriptor,
                repository=repository,
                branch=branch,
                project=safe_project,
            )
        else:
            accepted_receipt = {}
    except ValueError:
        attested_runtime_packet = _forbidden_runtime_packet_failure(runtime_packet)
        accepted_receipt = {}
    provenance = _post_deploy_provenance(attested_runtime_packet)
    capture = {
        "schema_version": POST_DEPLOY_MCP_CAPTURE_SCHEMA,
        "tool_names": tool_names,
        "runtime_readiness_plan": _remote_mapping_or_failure(plan),
        "runtime_collected_packet": _runtime_collected_packet_summary(attested_runtime_packet),
        "agent_context_product": _agent_context_product_from_context_pack(context_pack),
        "brain_objects_query_smokes": smokes,
        "deployed_identity": identity,
        "project_scope": {
            "project": safe_project,
            "source": project_source,
            "repository_inference_used": False,
        },
        "collection": provenance,
        "evidence_provenance": provenance,
        "production_mutation_performed": _runtime_packet_reports_mutation(attested_runtime_packet),
    }
    projection_join = _live_projection_join_from_runtime_packet(attested_runtime_packet)
    if projection_join:
        capture["projection_join"] = projection_join
    session_project_rollup = _live_session_project_rollup_from_runtime_packet(attested_runtime_packet)
    if session_project_rollup:
        capture["session_project_rollup_runtime"] = session_project_rollup
    preference_artifact_memory = _live_preference_artifact_memory_from_runtime_packet(attested_runtime_packet)
    if preference_artifact_memory:
        capture["preference_artifact_memory"] = preference_artifact_memory
    if accepted_receipt:
        capture["artifact_preference_application_receipt"] = accepted_receipt
    ensure_public_safe(capture, "SourceToCandidatePostDeployMcpCapture")
    if accepted_receipt:
        return _mint_collector_attested_evidence(capture)
    return capture


def _attach_direct_application_receipt(
    packet: Mapping[str, Any],
    *,
    prior_packet: Mapping[str, Any],
    direct_receipt: Mapping[str, Any],
    artifact_descriptor: Mapping[str, Any],
    repository: str,
    branch: str,
    project: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        _reject_forbidden_runtime_input_keys(direct_receipt)
    except ValueError:
        return _public_safe_mapping(packet), {}
    if not artifact_preference_application_receipt_is_valid(direct_receipt):
        return _public_safe_mapping(packet), {}
    safe_packet = _public_safe_mapping(packet)
    safe_prior_packet = _public_safe_mapping(prior_packet)
    safe_receipt = _public_safe_mapping(direct_receipt)
    preference = (
        safe_packet.get("preference_artifact_memory")
        if isinstance(safe_packet.get("preference_artifact_memory"), Mapping)
        else {}
    )
    alignment = (
        preference.get("read_surface_alignment")
        if isinstance(preference.get("read_surface_alignment"), Mapping)
        else {}
    )
    preference_binding = safe_receipt.get("preference_binding")
    artifact_binding = safe_receipt.get("artifact_binding")
    expected_artifact_binding = {
        "repository_hash": hash_payload(repository),
        "branch_hash": hash_payload(branch),
        "artifact_type": artifact_descriptor.get("artifact_type"),
        "artifact_fingerprint": artifact_descriptor.get("artifact_fingerprint"),
        "summary_hash": hash_payload(artifact_descriptor.get("summary")),
        "metrics_hash": hash_payload(artifact_descriptor.get("metrics")),
        "evidence_refs_hash": hash_payload(artifact_descriptor.get("evidence_refs")),
    }
    if (
        not preference
        or not _preference_runtime_read_stable(
            safe_prior_packet,
            safe_packet,
        )
        or not isinstance(preference_binding, Mapping)
        or not isinstance(artifact_binding, Mapping)
        or str(preference_binding.get("project") or "") != project
        or str(preference_binding.get("project") or "")
        != str(alignment.get("project") or "")
        or str(preference_binding.get("target_object_id") or "")
        != str(alignment.get("target_object_id") or "")
        or str(preference_binding.get("memory_id") or "")
        != str(alignment.get("memory_id") or "")
        or str(preference_binding.get("card_content_hash") or "")
        != str(alignment.get("card_content_hash") or "")
        or str(preference_binding.get("source_content_hash") or "")
        != str(alignment.get("source_content_hash") or "")
        or str(preference_binding.get("proposal_id") or "")
        != str(alignment.get("authority_proposal_id") or "")
        or str(preference_binding.get("decision_id") or "")
        != str(alignment.get("authority_decision_id") or "")
        or dict(artifact_binding) != expected_artifact_binding
    ):
        return safe_packet, {}
    safe_preference = dict(preference)
    safe_preference["artifact_consumer_evidence"] = safe_receipt
    safe_preference["artifact_review_check"] = _recalculated_artifact_review_check(
        preference,
        artifact_descriptor=artifact_descriptor,
        target_object_id=str(alignment.get("target_object_id") or ""),
    )
    safe_preference["gaps"] = [
        gap
        for gap in _public_safe_string_list(safe_preference.get("gaps"), max_chars=160)
        if gap != "artifact_consumer_evidence_missing"
    ]
    safe_preference["attestation_provenance"] = {
        "schema_version": ARTIFACT_PREFERENCE_COLLECTOR_ATTESTATION_SCHEMA,
        "collector": "source_to_candidate_post_deploy_mcp_capture",
        "transport": "streamable_http",
        "named_tool": ARTIFACT_PREFERENCE_EVALUATOR_TOOL,
        "receipt_hash": str(safe_receipt.get("receipt_hash") or ""),
        "read_surface_recheck": "validated",
    }
    safe_packet["preference_artifact_memory"] = safe_preference
    ensure_public_safe(safe_packet, "DirectArtifactPreferenceApplicationReceiptPacket")
    return safe_packet, safe_receipt


def _preference_runtime_read_stable(
    prior_packet: Mapping[str, Any],
    current_packet: Mapping[str, Any],
) -> bool:
    prior = prior_packet.get("preference_artifact_memory")
    current = current_packet.get("preference_artifact_memory")
    if not isinstance(prior, Mapping) or not isinstance(current, Mapping):
        return False
    prior_alignment = (
        prior.get("read_surface_alignment")
        if isinstance(prior.get("read_surface_alignment"), Mapping)
        else {}
    )
    current_alignment = (
        current.get("read_surface_alignment")
        if isinstance(current.get("read_surface_alignment"), Mapping)
        else {}
    )
    target_object_id = str(current_alignment.get("target_object_id") or "")
    return (
        bool(target_object_id)
        and dict(prior_alignment) == dict(current_alignment)
        and _preference_surface_continuity_matches(
            prior,
            target_object_id,
            prior_alignment,
        )
        and _preference_surface_continuity_matches(
            current,
            target_object_id,
            current_alignment,
        )
    )


def _recalculated_artifact_review_check(
    preference: Mapping[str, Any],
    *,
    artifact_descriptor: Mapping[str, Any],
    target_object_id: str,
) -> dict[str, Any]:
    original = _preference_artifact_review_check_view(
        preference.get("artifact_review_check")
    )
    original.update(
        {
            "schema_version": "artifact_review_preference_check.v1",
            "status": "pass",
            "ui_required": False,
            "artifact_type": public_safe_text(
                str(artifact_descriptor.get("artifact_type") or ""),
                max_chars=80,
            ),
            "artifact_summary": "operator_supplied_public_safe_descriptor",
            "matched_preference_object_ids": [target_object_id],
            "failures": [],
            "raw_artifact_body_returned": False,
        }
    )
    return original


async def _collect_tool_names(session: Any) -> list[str]:
    tools_result = await session.list_tools()
    tools = getattr(tools_result, "tools", [])
    return [
        public_safe_text(str(getattr(tool, "name", "") or ""), max_chars=120)
        for tool in tools
        if str(getattr(tool, "name", "") or "")
    ]


async def _call_tool_untrusted_mapping(
    session: Any,
    name: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
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
    return dict(structured) if isinstance(structured, Mapping) else {}


async def _call_tool_mapping(session: Any, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    return _public_safe_mapping(
        await _call_tool_untrusted_mapping(session, name, arguments)
    )


def _agent_context_product_from_context_pack(context_pack: Mapping[str, Any]) -> dict[str, Any]:
    pack = _remote_mapping_or_failure(context_pack)
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
    smoke = _remote_mapping_or_failure(raw)
    if smoke.get("collector_call_failed") is True:
        forbidden = smoke.get("collector_forbidden_input") is True
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
                "gaps": [
                    "collector_route_smoke_forbidden" if forbidden else "collector_route_smoke_failed"
                ],
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


def _attest_preference_artifact_memory(
    packet: Mapping[str, Any],
    *,
    deployed_identity: Mapping[str, Any],
    expected_commit: str,
    streamable_http_network_capture: bool,
) -> dict[str, Any]:
    attested_packet = _public_safe_mapping(packet)
    preference = attested_packet.get("preference_artifact_memory")
    if not isinstance(preference, Mapping):
        return attested_packet
    safe_preference = _remote_preference_artifact_memory_view(preference)
    blockers: list[str] = []
    if str(safe_preference.get("attestation_state") or "") != "unattested_runtime_read":
        blockers.append("preference_artifact_memory_unattested_runtime_read_missing")
    if (
        not expected_commit
        or deployed_identity.get("contains_expected_commit") is not True
        or not str(deployed_identity.get("identity_source") or "")
    ):
        blockers.append("deployed_identity_expected_commit_unverified")
    if not streamable_http_network_capture:
        blockers.append("post_deploy_streamable_http_network_capture_missing")
    if blockers:
        attested_packet["preference_artifact_memory"] = safe_preference
        attested_packet["preference_artifact_memory_attestation_blockers"] = blockers
        return attested_packet
    safe_preference.update(
        {
            "attestation_state": "attested_post_deploy_streamable_http",
            "evidence_class": "runtime_preference_artifact_memory",
            "evidence_source": "actual_live_read_surfaces",
        }
    )
    attested_packet["preference_artifact_memory"] = safe_preference
    attested_packet["preference_artifact_memory_attestation_blockers"] = []
    return attested_packet


def _remote_preference_artifact_memory_view(value: Mapping[str, Any]) -> dict[str, Any]:
    preference_pack = value.get("preference_object_pack")
    html_smoke = value.get("html_visualization_route_smoke")
    context = value.get("agent_context_preference_section")
    alignment = value.get("read_surface_alignment")
    artifact_check = value.get("artifact_review_check")
    postcheck = value.get("postcheck")
    view = {
        "schema_version": public_safe_text(str(value.get("schema_version") or ""), max_chars=80),
        "attestation_state": public_safe_text(str(value.get("attestation_state") or ""), max_chars=80),
        "read_surface_alignment": _preference_alignment_view(alignment),
        "preference_object_pack": _preference_object_pack_view(preference_pack),
        "html_visualization_route_smoke": _preference_html_route_view(html_smoke),
        "agent_context_preference_section": _preference_context_section_view(context),
        "artifact_review_check": _preference_artifact_review_check_view(artifact_check),
        "gaps": _public_safe_string_list(value.get("gaps"), max_chars=160),
        "postcheck": _protected_output_postcheck_view(postcheck),
        "production_mutation_performed": value.get("production_mutation_performed") is True,
    }
    ensure_public_safe(view, "SourceToCandidatePostDeployPreferenceArtifactMemoryView")
    return view


def _preference_alignment_view(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    return {
        "status": public_safe_text(str(raw.get("status") or ""), max_chars=80),
        "target_object_id": public_safe_text(str(raw.get("target_object_id") or ""), max_chars=180),
        "memory_id": public_safe_text(str(raw.get("memory_id") or ""), max_chars=180),
        "card_content_hash": public_safe_text(
            str(raw.get("card_content_hash") or ""),
            max_chars=80,
        ),
        "authority_proposal_id": public_safe_text(
            str(raw.get("authority_proposal_id") or ""),
            max_chars=180,
        ),
        "project": public_safe_text(str(raw.get("project") or ""), max_chars=120),
        "source_content_hash": public_safe_text(
            str(raw.get("source_content_hash") or ""),
            max_chars=80,
        ),
        "authority_decision_id": public_safe_text(
            str(raw.get("authority_decision_id") or ""),
            max_chars=180,
        ),
        "code_style_preference_object_ids": _public_safe_string_list(
            raw.get("code_style_preference_object_ids"),
            max_chars=180,
        ),
        "html_visualization_preference_object_ids": _public_safe_string_list(
            raw.get("html_visualization_preference_object_ids"),
            max_chars=180,
        ),
        "style_preference_context_object_ids": _public_safe_string_list(
            raw.get("style_preference_context_object_ids"),
            max_chars=180,
        ),
    }


def _preference_object_pack_view(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    lanes = raw.get("lanes") if isinstance(raw.get("lanes"), Mapping) else {}
    accepted = _preference_object_views(lanes.get("accepted_current"), required_lane="accepted_current")
    proposals = _preference_object_views(lanes.get("proposal_only"), required_lane="proposal_only")
    return {
        "schema_version": public_safe_text(str(raw.get("schema_version") or ""), max_chars=80),
        "route": public_safe_text(str(raw.get("route") or ""), max_chars=80),
        "accepted_preference_count": len(accepted),
        "proposal_preference_count": len(proposals),
        "objects": [*accepted, *proposals],
        "lanes": {"accepted_current": accepted, "proposal_only": proposals},
        "recommended_actions": _preference_action_views(raw.get("recommended_actions")),
        "gaps": _public_safe_string_list(raw.get("gaps"), max_chars=160),
        "production_mutation_performed": raw.get("production_mutation_performed") is True,
    }


def _preference_html_route_view(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    pack = raw.get("object_pack") if isinstance(raw.get("object_pack"), Mapping) else {}
    lanes = pack.get("lanes") if isinstance(pack.get("lanes"), Mapping) else {}
    accepted = _preference_object_views(lanes.get("accepted_current"), required_lane="accepted_current")
    return {
        "schema_version": public_safe_text(str(raw.get("schema_version") or ""), max_chars=80),
        "route": public_safe_text(str(raw.get("route") or ""), max_chars=80),
        "production_mutation_performed": raw.get("production_mutation_performed") is True,
        "object_pack": {
            "schema_version": public_safe_text(str(pack.get("schema_version") or ""), max_chars=80),
            "route": public_safe_text(str(pack.get("route") or ""), max_chars=80),
            "objects": accepted,
            "lanes": {"accepted_current": accepted},
            "recommended_actions": _preference_action_views(pack.get("recommended_actions")),
            "gaps": _public_safe_string_list(pack.get("gaps"), max_chars=160),
        },
    }


def _preference_context_section_view(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    items = _preference_object_views(raw.get("items"), required_lane="accepted_current")
    surface_policy = raw.get("surface_policy") if isinstance(raw.get("surface_policy"), Mapping) else {}
    return {
        "schema_version": public_safe_text(str(raw.get("schema_version") or ""), max_chars=80),
        "section": "style_preference",
        "object_count": len(items),
        "accepted_preference_count": len(items),
        "authority_lanes": _public_safe_string_list(raw.get("authority_lanes"), max_chars=80),
        "items": items,
        "surface_policy": {"mutation_allowed": surface_policy.get("mutation_allowed") is True},
    }


def _preference_object_views(value: Any, *, required_lane: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    views: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
        scope = item.get("scope") if isinstance(item.get("scope"), Mapping) else {}
        lane = public_safe_text(str(item.get("authority_lane") or ""), max_chars=80)
        object_id = public_safe_text(str(item.get("object_id") or ""), max_chars=180)
        object_type = public_safe_text(str(item.get("object_type") or ""), max_chars=80)
        if lane != required_lane or object_type != "ArtifactPreference" or not object_id:
            continue
        if knowledge_object_class_from_id(object_id) != "ArtifactPreference":
            continue
        views.append(
            {
                "object_id": object_id,
                "object_type": object_type,
                "authority_lane": lane,
                "memory_id": public_safe_text(
                    str(item.get("memory_id") or payload.get("memory_id") or ""),
                    max_chars=180,
                ),
                "card_content_hash": public_safe_text(
                    str(
                        item.get("card_content_hash")
                        or payload.get("card_content_hash")
                        or ""
                    ),
                    max_chars=80,
                ),
                "authority_proposal_id": public_safe_text(
                    str(
                        item.get("authority_proposal_id")
                        or payload.get("authority_proposal_id")
                        or ""
                    ),
                    max_chars=180,
                ),
                "project": public_safe_text(
                    str(item.get("project") or scope.get("project") or payload.get("project") or ""),
                    max_chars=120,
                ),
                "content_hash": public_safe_text(
                    str(item.get("content_hash") or payload.get("source_content_hash") or ""),
                    max_chars=80,
                ),
                "source_content_hash": public_safe_text(
                    str(
                        item.get("source_content_hash")
                        or payload.get("source_content_hash")
                        or item.get("content_hash")
                        or ""
                    ),
                    max_chars=80,
                ),
                "authority_decision_id": public_safe_text(
                    str(item.get("authority_decision_id") or payload.get("authority_decision_id") or ""),
                    max_chars=180,
                ),
            }
        )
    return views


def _preference_action_views(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [
        {
            "object_id": public_safe_text(str(item.get("object_id") or ""), max_chars=180),
            "action": public_safe_text(str(item.get("action") or ""), max_chars=80),
        }
        for item in value
        if isinstance(item, Mapping) and item.get("object_id") and item.get("action")
    ]


def _preference_artifact_review_check_view(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    metrics = raw.get("artifact_metrics") if isinstance(raw.get("artifact_metrics"), Mapping) else {}
    return {
        "schema_version": public_safe_text(str(raw.get("schema_version") or ""), max_chars=80),
        "status": public_safe_text(str(raw.get("status") or ""), max_chars=80),
        "ui_required": raw.get("ui_required") is True,
        "artifact_type": public_safe_text(str(raw.get("artifact_type") or ""), max_chars=80),
        "artifact_summary": public_safe_text(str(raw.get("artifact_summary") or ""), max_chars=360),
        "artifact_metrics": {
            "finding_count": _safe_int(metrics.get("finding_count")),
            "evidence_ref_count": _safe_int(metrics.get("evidence_ref_count")),
            "word_count": _safe_int(metrics.get("word_count")),
        },
        "matched_preference_object_ids": _public_safe_string_list(
            raw.get("matched_preference_object_ids"),
            max_chars=180,
        ),
        "failures": _public_safe_string_list(raw.get("failures"), max_chars=160),
        "raw_artifact_body_returned": raw.get("raw_artifact_body_returned")
        if isinstance(raw.get("raw_artifact_body_returned"), bool)
        else None,
    }


def _protected_output_postcheck_view(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    return {
        "status": public_safe_text(str(raw.get("status") or ""), max_chars=80),
        **{
            field: raw.get(field) if isinstance(raw.get(field), bool) else None
            for field in PROTECTED_OUTPUT_FLAGS
        },
    }


def _public_safe_string_list(value: Any, *, max_chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        public_safe_text(str(item), max_chars=max_chars)
        for item in value
        if isinstance(item, str) and item
    ]


def _preference_artifact_memory_promotion_blockers(packet: Mapping[str, Any]) -> list[str]:
    safe_packet = _public_safe_mapping(packet)
    preference = safe_packet.get("preference_artifact_memory")
    if not isinstance(preference, Mapping):
        return ["preference_artifact_memory_missing"]
    attestation_blockers = safe_packet.get("preference_artifact_memory_attestation_blockers")
    if (
        isinstance(attestation_blockers, list)
        and attestation_blockers
        and attestation_blockers[0] == "preference_artifact_runtime_input_forbidden"
    ):
        return [public_safe_text(str(attestation_blockers[0] or ""), max_chars=120)]
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
    if artifact_check.get("status") != "pass":
        return ["preference_artifact_review_check_failed"]
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
    if isinstance(attestation_blockers, list) and attestation_blockers:
        return [public_safe_text(str(attestation_blockers[0] or ""), max_chars=120)]
    if not _artifact_consumer_evidence_valid(preference):
        return ["preference_artifact_consumer_evidence_missing"]
    if str(preference.get("attestation_state") or "") != "attested_post_deploy_streamable_http":
        return ["preference_artifact_memory_post_deploy_attestation_missing"]
    if str(preference.get("evidence_class") or "") != "runtime_preference_artifact_memory":
        return ["preference_artifact_memory_evidence_class_missing"]
    if str(preference.get("evidence_source") or "") != "actual_live_read_surfaces":
        return ["preference_artifact_memory_not_actual_live_read_surfaces"]
    alignment = (
        preference.get("read_surface_alignment")
        if isinstance(preference.get("read_surface_alignment"), Mapping)
        else {}
    )
    target_object_id = public_safe_text(str(alignment.get("target_object_id") or ""), max_chars=180)
    aligned_lists = [
        alignment.get("code_style_preference_object_ids"),
        alignment.get("html_visualization_preference_object_ids"),
        alignment.get("style_preference_context_object_ids"),
    ]
    if (
        alignment.get("status") != "validated"
        or not target_object_id
        or any(not isinstance(items, list) or target_object_id not in items for items in aligned_lists)
        or not _preference_surface_continuity_matches(preference, target_object_id, alignment)
    ):
        return ["preference_artifact_memory_read_surface_alignment_failed"]
    return []


def _artifact_consumer_evidence_valid(preference: Mapping[str, Any]) -> bool:
    consumer = (
        preference.get("artifact_consumer_evidence")
        if isinstance(preference.get("artifact_consumer_evidence"), Mapping)
        else {}
    )
    return artifact_preference_application_receipt_is_valid(consumer)


def _artifact_ref_is_public_safe(value: Any, *, required_prefix: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    normalized = value.casefold()
    decoded = _fully_unquote(value)
    suffix = value[len(required_prefix) :] if normalized.startswith(required_prefix) else ""
    return (
        decoded == value
        and bool(suffix)
        and _ARTIFACT_REF_SUFFIX_RE.fullmatch(suffix) is not None
        and _RAW_EXTERNAL_REF_SUFFIX_RE.match(suffix) is None
        and not any(marker in normalized for marker in _RAW_EXTERNAL_REF_MARKERS)
    )


def _preference_surface_continuity_matches(
    preference: Mapping[str, Any],
    target_object_id: str,
    alignment: Mapping[str, Any],
) -> bool:
    if knowledge_object_class_from_id(target_object_id) != "ArtifactPreference":
        return False
    pack = preference.get("preference_object_pack") if isinstance(
        preference.get("preference_object_pack"), Mapping
    ) else {}
    html_smoke = preference.get("html_visualization_route_smoke") if isinstance(
        preference.get("html_visualization_route_smoke"), Mapping
    ) else {}
    html_pack = html_smoke.get("object_pack") if isinstance(html_smoke.get("object_pack"), Mapping) else {}
    context = preference.get("agent_context_preference_section") if isinstance(
        preference.get("agent_context_preference_section"), Mapping
    ) else {}
    surfaces = [
        pack.get("lanes", {}).get("accepted_current", []) if isinstance(pack.get("lanes"), Mapping) else [],
        html_pack.get("lanes", {}).get("accepted_current", [])
        if isinstance(html_pack.get("lanes"), Mapping)
        else [],
        context.get("items", []) if isinstance(context.get("items"), list) else [],
    ]
    continuity: list[tuple[str, str, str, str, str, str]] = []
    for items in surfaces:
        obj = next(
            (
                item
                for item in items
                if isinstance(item, Mapping) and str(item.get("object_id") or "") == target_object_id
            ),
            None,
        )
        if obj is None:
            return False
        scope = obj.get("scope") if isinstance(obj.get("scope"), Mapping) else {}
        payload = obj.get("payload") if isinstance(obj.get("payload"), Mapping) else {}
        continuity.append(
            (
                public_safe_text(
                    str(obj.get("memory_id") or payload.get("memory_id") or ""),
                    max_chars=180,
                ),
                public_safe_text(
                    str(
                        obj.get("card_content_hash")
                        or payload.get("card_content_hash")
                        or ""
                    ),
                    max_chars=80,
                ),
                public_safe_text(
                    str(
                        obj.get("authority_proposal_id")
                        or payload.get("authority_proposal_id")
                        or ""
                    ),
                    max_chars=180,
                ),
                public_safe_text(
                    str(obj.get("authority_decision_id") or payload.get("authority_decision_id") or ""),
                    max_chars=180,
                ),
                public_safe_text(
                    str(obj.get("project") or scope.get("project") or payload.get("project") or ""),
                    max_chars=120,
                ),
                public_safe_text(
                    str(
                        obj.get("source_content_hash")
                        or obj.get("content_hash")
                        or payload.get("source_content_hash")
                        or ""
                    ),
                    max_chars=80,
                ),
            )
        )
    expected = (
        public_safe_text(str(alignment.get("memory_id") or ""), max_chars=180),
        public_safe_text(str(alignment.get("card_content_hash") or ""), max_chars=80),
        public_safe_text(str(alignment.get("authority_proposal_id") or ""), max_chars=180),
        public_safe_text(str(alignment.get("authority_decision_id") or ""), max_chars=180),
        public_safe_text(str(alignment.get("project") or ""), max_chars=120),
        public_safe_text(str(alignment.get("source_content_hash") or ""), max_chars=80),
    )
    return (
        expected != ("", "", "", "", "", "")
        and len(set(continuity)) == 1
        and continuity[0] == expected
    )


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


def _remote_mapping_or_failure(value: Any) -> dict[str, Any]:
    try:
        _reject_forbidden_runtime_input_keys(value)
    except ValueError:
        return {
            "collector_call_failed": True,
            "collector_error_type": "ForbiddenRuntimeEvidenceField",
            "collector_forbidden_input": True,
        }
    return _public_safe_mapping(value)


def _deployed_identity_view(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {
            "contains_expected_commit": False,
            "identity_source": "post_deploy_mcp_capture_missing_deployed_identity",
        }
    try:
        _reject_forbidden_runtime_input_keys(value)
    except ValueError:
        return {
            "contains_expected_commit": False,
            "identity_source": "post_deploy_mcp_capture_forbidden_deployed_identity",
        }
    return {
        "contains_expected_commit": value.get("contains_expected_commit") is True,
        "identity_source": public_safe_text(str(value.get("identity_source") or ""), max_chars=160),
    }


def _reject_forbidden_runtime_input_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = _normalized_sensitive_key(key)
            compact = normalized.replace("_", "")
            if (
                normalized in _FORBIDDEN_RUNTIME_INPUT_KEYS
                or compact in _FORBIDDEN_RUNTIME_INPUT_COMPACT_KEYS
                or (
                    normalized.endswith("s")
                    and (
                        normalized[:-1] in _FORBIDDEN_RUNTIME_INPUT_KEYS
                        or compact[:-1] in _FORBIDDEN_RUNTIME_INPUT_COMPACT_KEYS
                    )
                )
            ):
                raise ValueError("runtime evidence contains a forbidden field")
            _reject_forbidden_runtime_input_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_forbidden_runtime_input_keys(child)


def _normalized_sensitive_key(value: Any) -> str:
    decoded = _fully_unquote(str(value).strip())
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", decoded)
    return re.sub(r"[^A-Za-z0-9]+", "_", snake).strip("_").casefold()


def _fully_unquote(value: str) -> str:
    decoded = value
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def _forbidden_runtime_packet_failure(packet: Mapping[str, Any]) -> dict[str, Any]:
    provenance = packet.get("evidence_provenance") if isinstance(packet.get("evidence_provenance"), Mapping) else {}
    collector = packet.get("collector") if isinstance(packet.get("collector"), Mapping) else {}
    preference = (
        packet.get("preference_artifact_memory")
        if isinstance(packet.get("preference_artifact_memory"), Mapping)
        else {}
    )
    return {
        "schema_version": public_safe_text(str(packet.get("schema_version") or ""), max_chars=80),
        "collector": {
            "readiness_claim": public_safe_text(
                str(collector.get("readiness_claim") or ""),
                max_chars=120,
            )
        },
        "evidence_provenance": {
            "collection_mode": public_safe_text(
                str(provenance.get("collection_mode") or ""),
                max_chars=80,
            ),
            "network_used": provenance.get("network_used") is True,
            "mutation_scope": public_safe_text(
                str(provenance.get("mutation_scope") or "none"),
                max_chars=80,
            ),
        },
        "preference_artifact_memory": {
            "schema_version": public_safe_text(
                str(preference.get("schema_version") or ""),
                max_chars=80,
            ),
            "collector_error_type": "ForbiddenRuntimeEvidenceField",
            "postcheck": {"status": "failed"},
        },
        "preference_artifact_memory_attestation_blockers": [
            "preference_artifact_runtime_input_forbidden"
        ],
        "production_mutation_performed": packet.get("production_mutation_performed") is True,
    }
