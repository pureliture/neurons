from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from agent_knowledge.cli import main
from agent_knowledge.llm_brain_core.objects import object_cli
from agent_knowledge.llm_brain_core.objects.post_deploy_mcp_capture import (
    collect_source_to_candidate_post_deploy_mcp_capture,
    validate_post_deploy_mcp_url,
)
from agent_knowledge.llm_brain_core.objects.runtime_readiness import (
    EVIDENCE_PROVENANCE_SCHEMA,
    REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES,
    REQUIRED_RUNTIME_TOOL_NAMES,
)


class _FakeMcpSession:
    def __init__(self) -> None:
        self.initialized = False
        self.calls: list[tuple[str, dict]] = []

    async def initialize(self) -> None:
        self.initialized = True

    async def list_tools(self):
        return SimpleNamespace(
            tools=[SimpleNamespace(name=name) for name in REQUIRED_RUNTIME_TOOL_NAMES]
        )

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, dict(arguments)))
        if name == "brain_source_to_candidate_runtime_readiness":
            return SimpleNamespace(
                isError=False,
                structuredContent={
                    "schema_version": "source_to_candidate_runtime_evidence_collection_plan.v1",
                    "collection_mode": "post_deploy_read_only_smoke",
                    "network_used": False,
                    "production_mutation_performed": False,
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
    assert set(capture["tool_names"]) == set(REQUIRED_RUNTIME_TOOL_NAMES)
    assert capture["production_mutation_performed"] is False
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
        }
    ]
    route_calls = [arguments for name, arguments in session.calls if name == "brain_objects_query"]
    assert [arguments["route"] for arguments in route_calls] == list(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES)
    assert all(arguments["response_mode"] == "full" for arguments in route_calls)
    assert all(arguments["consumer"] == "codex" for arguments in route_calls)


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
