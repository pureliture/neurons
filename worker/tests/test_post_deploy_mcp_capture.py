from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from agent_knowledge.cli import main
from agent_knowledge.llm_brain_core.context_builder import object_native_review_tool_hints
from agent_knowledge.llm_brain_core.objects import object_cli
from agent_knowledge.llm_brain_core.objects.post_deploy_mcp_capture import (
    collect_source_to_candidate_post_deploy_mcp_capture,
    validate_post_deploy_mcp_url,
)
from agent_knowledge.llm_brain_core.objects.runtime_readiness import (
    EVIDENCE_PROVENANCE_SCHEMA,
    REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES,
    REQUIRED_AGENT_CONTEXT_SECTIONS,
    REQUIRED_RUNTIME_TOOL_NAMES,
    build_source_to_candidate_runtime_post_deploy_capture_readiness_report,
)


def _fake_agent_context_product(*, consumer: str = "codex") -> dict:
    missing_evidence = ["runtime_evidence_unverified"]
    return {
        "schema_version": "agent_context_product_pack.v1",
        "consumer": consumer,
        "sections": {
            name: {
                "object_count": 1,
                "items": [
                    {
                        "object_id": f"fixture:{name}",
                        "object_type": "MemoryCard",
                        "title": name,
                        "authority_lane": name,
                        "recommended_action": "read",
                    }
                ],
                "authority_lanes": [name],
                "gaps": [],
            }
            for name in REQUIRED_AGENT_CONTEXT_SECTIONS
        },
        "surface_policy": {
            "consumer": consumer,
            "read_only": True,
            "mutation_allowed": False,
            "allowed_actions": ["suggest_change", "run_verification", "request_missing_evidence"],
            "property_omissions": ["raw_body", "raw_source", "private_deploy_value", "secret"],
        },
        "degraded_mode": {"active": True, "gaps": missing_evidence},
        "freshness": {
            "stale_evidence_visible": False,
            "stale_memory_count": 0,
            "no_recent_source": False,
        },
        "missing_evidence_before_promotion": missing_evidence,
        "action_hints": [],
        "tool_hints": object_native_review_tool_hints(missing_evidence),
    }


def _runtime_projection_join_evidence(*, edge_count: int = 2) -> dict:
    return {
        "schema_version": "object_extraction_projection_join_preview.v1",
        "evidence_class": "runtime_projection_join",
        "status": "pass",
        "edge_count": edge_count,
        "production_mutation_performed": False,
        "postcheck": {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
    }


def _runtime_collected_packet(*, live: bool = False) -> dict:
    packet = {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        "projection_join": _runtime_projection_join_evidence(),
        "evidence_provenance": {
            "schema_version": EVIDENCE_PROVENANCE_SCHEMA,
            "collection_mode": "post_deploy_read_only_smoke" if live else "local_test_replay",
            "network_used": live,
            "mutation_scope": "none",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
        "production_mutation_performed": False,
    }
    if not live:
        packet["collector"] = {
            "schema_version": "source_to_candidate_runtime_evidence_collector.v1",
            "readiness_claim": "collector_packet_not_live_evidence",
        }
    return packet


class _FakeMcpSession:
    def __init__(self) -> None:
        self.initialized = False
        self.calls: list[tuple[str, dict]] = []

    async def initialize(self) -> None:
        self.initialized = True

    async def list_tools(self):
        return SimpleNamespace(
            tools=[
                SimpleNamespace(name=name)
                for name in ("brain_context_resolve", *REQUIRED_RUNTIME_TOOL_NAMES)
            ]
        )

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, dict(arguments)))
        if name == "brain_source_to_candidate_runtime_readiness":
            if arguments.get("collect_shadow_evidence") is True:
                return SimpleNamespace(
                    isError=False,
                    structuredContent=_runtime_collected_packet(live=False),
                )
            return SimpleNamespace(
                isError=False,
                structuredContent={
                    "schema_version": "source_to_candidate_runtime_evidence_collection_plan.v1",
                    "collection_mode": "post_deploy_read_only_smoke",
                    "network_used": False,
                    "production_mutation_performed": False,
                },
            )
        if name == "brain_context_resolve":
            return SimpleNamespace(
                isError=False,
                structuredContent={
                    "schema_version": "llm_brain_context_resolve.v1",
                    "authority": {
                        "agent_context_product": _fake_agent_context_product(
                            consumer=arguments.get("consumer", "codex")
                        )
                    },
                    "private_context_not_returned": True,
                },
            )
        route = str(arguments.get("route") or "")
        return SimpleNamespace(
            isError=False,
            structuredContent={
                "schema_version": "brain_objects_query.v1",
                "route": route,
                "object_pack": {
                    "schema_version": "object_pack.v1",
                    "route": route,
                    "objects": [],
                    "edges": [],
                    "evidence": [],
                    "recommended_actions": [],
                    "lanes": {},
                    "gaps": [],
                },
            },
        )


def test_post_deploy_mcp_url_rejects_values_that_can_leak_secrets_or_topology():
    assert validate_post_deploy_mcp_url("https://mcp.example.test/mcp") == "https://mcp.example.test/mcp"
    with pytest.raises(ValueError):
        validate_post_deploy_mcp_url("file:///tmp/mcp")
    with pytest.raises(ValueError):
        validate_post_deploy_mcp_url("https://user:secret@mcp.example.test/mcp")
    with pytest.raises(ValueError):
        validate_post_deploy_mcp_url("https://mcp.example.test/mcp?token=secret")
    with pytest.raises(ValueError):
        validate_post_deploy_mcp_url("https://mcp.example.test/mcp#fragment")


def test_collect_post_deploy_mcp_capture_uses_read_only_mcp_calls_and_sanitizes_output():
    session = _FakeMcpSession()
    seen_urls: list[str] = []

    @asynccontextmanager
    async def _fake_session_factory(mcp_url: str):
        seen_urls.append(mcp_url)
        yield session

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            consumer="codex",
            expected_commit="c2b8548",
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            session_factory=_fake_session_factory,
        )
    )

    assert seen_urls == ["https://mcp.example.test/mcp"]
    assert session.initialized is True
    assert capture["schema_version"] == "source_to_candidate_runtime_post_deploy_mcp_capture.v1"
    assert set(REQUIRED_RUNTIME_TOOL_NAMES).issubset(set(capture["tool_names"]))
    assert capture["production_mutation_performed"] is False
    assert "projection_join" not in capture
    assert capture["runtime_collected_packet"] == {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        "collector_readiness_claim": "collector_packet_not_live_evidence",
        "projection_join_present": True,
        "projection_join_schema": "object_extraction_projection_join_preview.v1",
        "projection_join_edge_count": 2,
        "projection_join_promoted_to_live_evidence": False,
        "evidence_collection_mode": "local_test_replay",
        "evidence_collection_network_used": False,
        "production_mutation_performed": False,
    }
    assert capture["agent_context_product"] == _fake_agent_context_product(consumer="codex")
    assert "private_context_not_returned" not in json.dumps(capture, sort_keys=True)
    assert capture["collection"] == {
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
    assert capture["evidence_provenance"] == capture["collection"]
    assert capture["deployed_identity"]["contains_expected_commit"] is True
    assert {item["route"] for item in capture["brain_objects_query_smokes"]} == set(
        REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES
    )
    assert "mcp.example.test" not in json.dumps(capture, sort_keys=True)

    runtime_readiness_calls = [
        arguments
        for name, arguments in session.calls
        if name == "brain_source_to_candidate_runtime_readiness"
    ]
    assert runtime_readiness_calls == [
        {
            "evidence_collection_plan": True,
            "expected_commit": "c2b8548",
            "repository": "pureliture/neurons",
            "branch": "main",
            "consumer": "codex",
        },
        {
            "collect_shadow_evidence": True,
            "expected_commit": "c2b8548",
            "repository": "pureliture/neurons",
            "branch": "main",
            "consumer": "codex",
            "evidence_collection_mode": "post_deploy_read_only_smoke",
            "evidence_collection_network_used": True,
        }
    ]
    context_calls = [
        arguments
        for name, arguments in session.calls
        if name == "brain_context_resolve"
    ]
    assert context_calls == [
        {
            "repository": "pureliture/neurons",
            "branch": "main",
            "current_files": [],
            "current_request": (
                "source-to-candidate runtime readiness post-deploy "
                "agent context product capture"
            ),
            "limit": 8,
            "response_mode": "full",
            "consumer": "codex",
        }
    ]
    route_calls = [arguments for name, arguments in session.calls if name == "brain_objects_query"]
    assert [arguments["route"] for arguments in route_calls] == list(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES)
    assert all(arguments["response_mode"] == "full" for arguments in route_calls)
    assert all(arguments["consumer"] == "codex" for arguments in route_calls)

    report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=capture,
        expected_commit="c2b8548",
    )
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.agent_context.tool_hints"]["status"] == "validated"
    assert claims["live.agent_context.product_sections"]["status"] == "validated"
    assert claims["live.source_to_candidate.projection_join"]["status"] == "not_validated"
    assert "live_graph_qdrant_projection_join_unproven" in report["gaps"]
    assert report["production_ready"] is False


def test_collect_post_deploy_mcp_capture_promotes_live_projection_join_from_read_only_runtime():
    class _LiveProjectionJoinSession(_FakeMcpSession):
        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_source_to_candidate_runtime_readiness" and arguments.get("collect_shadow_evidence") is True:
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(
                    isError=False,
                    structuredContent=_runtime_collected_packet(live=True),
                )
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _LiveProjectionJoinSession()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            expected_commit="c2b8548",
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            session_factory=_fake_session_factory,
        )
    )

    assert capture["runtime_collected_packet"]["projection_join_promoted_to_live_evidence"] is True
    assert capture["projection_join"]["schema_version"] == "object_extraction_projection_join_preview.v1"
    assert capture["projection_join"]["evidence_class"] == "runtime_projection_join"
    assert capture["projection_join"]["edge_count"] == 2
    assert capture["projection_join"]["production_mutation_performed"] is False

    report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=capture,
        expected_commit="c2b8548",
    )
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert claims["live.source_to_candidate.projection_join"]["status"] == "validated"
    assert "live_graph_qdrant_projection_join_unproven" not in report["gaps"]
    assert report["production_ready"] is False


def test_collect_post_deploy_mcp_capture_keeps_tool_errors_as_public_safe_gaps():
    class _FailingRouteSession(_FakeMcpSession):
        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_objects_query" and arguments.get("route") == "temporal_work_recall":
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(isError=True, structuredContent={})
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _FailingRouteSession()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            session_factory=_fake_session_factory,
        )
    )

    by_route = {item["route"]: item for item in capture["brain_objects_query_smokes"]}
    assert by_route["temporal_work_recall"]["collector_error_type"] == "McpToolError"
    assert by_route["temporal_work_recall"]["object_pack"]["gaps"] == ["collector_route_smoke_failed"]
    assert by_route["temporal_work_recall"]["production_mutation_performed"] is False


def test_collect_post_deploy_mcp_capture_keeps_missing_agent_context_as_public_safe_gap():
    class _MissingContextSession(_FakeMcpSession):
        async def call_tool(self, name: str, arguments: dict):
            if name == "brain_context_resolve":
                self.calls.append((name, dict(arguments)))
                return SimpleNamespace(isError=True, structuredContent={})
            return await super().call_tool(name, arguments)

    @asynccontextmanager
    async def _fake_session_factory(_mcp_url: str):
        yield _MissingContextSession()

    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository="pureliture/neurons",
            branch="main",
            session_factory=_fake_session_factory,
        )
    )

    product = capture["agent_context_product"]
    assert product["schema_version"] == ""
    assert product["surface_policy"]["mutation_allowed"] is False
    assert product["missing_evidence_before_promotion"] == ["agent_context_product_capture_failed"]
    assert product["collector_error_type"] == "McpToolError"


def test_runtime_readiness_cli_collects_post_deploy_mcp_capture(monkeypatch, capsys, tmp_path):
    identity_file = tmp_path / "deployed-identity.json"
    identity_file.write_text(
        json.dumps(
            {
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            }
        ),
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    async def _fake_collect(**kwargs):
        seen.update(kwargs)
        return {
            "schema_version": "source_to_candidate_runtime_post_deploy_mcp_capture.v1",
            "collection": {
                "schema_version": EVIDENCE_PROVENANCE_SCHEMA,
                "collection_mode": "post_deploy_read_only_smoke",
                "network_used": True,
                "mutation_scope": "none",
            },
            "production_mutation_performed": False,
        }

    monkeypatch.setattr(
        object_cli,
        "collect_source_to_candidate_post_deploy_mcp_capture",
        _fake_collect,
    )

    assert (
        main(
            [
                "source-to-candidate-runtime-readiness",
                "--collect-post-deploy-mcp-capture",
                "--mcp-url",
                "https://mcp.example.test/mcp",
                "--repository",
                "pureliture/neurons",
                "--branch",
                "main",
                "--consumer",
                "codex",
                "--expected-commit",
                "c2b8548",
                "--deployed-identity-file",
                str(identity_file),
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == "source_to_candidate_runtime_post_deploy_mcp_capture.v1"
    assert output["collection"]["network_used"] is True
    assert seen["mcp_url"] == "https://mcp.example.test/mcp"
    assert seen["repository"] == "pureliture/neurons"
    assert seen["branch"] == "main"
    assert seen["consumer"] == "codex"
    assert seen["expected_commit"] == "c2b8548"
    assert seen["deployed_identity"] == {
        "contains_expected_commit": True,
        "identity_source": "redacted_artifact_identity_summary",
    }
