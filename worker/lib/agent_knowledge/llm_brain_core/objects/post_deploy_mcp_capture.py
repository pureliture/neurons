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
    capture = {
        "schema_version": POST_DEPLOY_MCP_CAPTURE_SCHEMA,
        "tool_names": tool_names,
        "runtime_readiness_plan": _public_safe_mapping(plan),
        "agent_context_product": _agent_context_product_from_context_pack(context_pack),
        "brain_objects_query_smokes": smokes,
        "deployed_identity": identity,
        "collection": _post_deploy_provenance(),
        "evidence_provenance": _post_deploy_provenance(),
        "production_mutation_performed": False,
    }
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
    product = authority.get("agent_context_product") if isinstance(authority, Mapping) else None
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


def _post_deploy_provenance() -> dict[str, Any]:
    return {
        "schema_version": EVIDENCE_PROVENANCE_SCHEMA,
        "collector": "source_to_candidate_post_deploy_mcp_capture",
        "collection_mode": "post_deploy_read_only_smoke",
        "network_used": True,
        "mutation_scope": "none",
        "raw_private_evidence_returned": False,
        "secret_returned": False,
        "host_topology_returned": False,
        "raw_external_ids_returned": False,
    }


def _public_safe_mapping(value: Any) -> dict[str, Any]:
    safe = dict(value) if isinstance(value, Mapping) else {}
    ensure_public_safe(safe, "SourceToCandidatePostDeployMcpMapping")
    return safe
