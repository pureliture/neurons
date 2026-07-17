from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_knowledge.mcp_jsonrpc import dispatch_tool_call
from agent_knowledge.mcp_tools import list_tools
from agent_knowledge.knowledge_search_service import (
    DisabledRetiredIndexBridgeClient,
    KnowledgeSearchService,
)
from agent_knowledge.ledger import Ledger


def test_mcp_runtime_build_identity_contract_is_fixed_and_read_only():
    tools = {tool["name"]: tool for tool in list_tools()}

    contract = tools["brain_runtime_build_identity"]

    assert contract["inputSchema"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert "read-only" in contract["description"]


def test_mcp_runtime_build_identity_returns_only_fixed_public_safe_projection():
    expected = {
        "schema_version": "brain_runtime_build_identity.v1",
        "source_commit": "a" * 40,
        "build_content_manifest_hash": "sha256:" + "b" * 64,
        "production_mutation_performed": False,
        "protected_values_returned": False,
        "raw_private_evidence_returned": False,
        "secret_returned": False,
        "host_topology_returned": False,
        "raw_external_ids_returned": False,
    }

    class BuildIdentityService:
        def brain_runtime_build_identity(self):
            return expected

    result = dispatch_tool_call(
        {"name": "brain_runtime_build_identity", "arguments": {}},
        BuildIdentityService(),
    )

    assert result["structuredContent"] == expected


def test_mcp_runtime_build_identity_rejects_all_arguments():
    class BuildIdentityService:
        def brain_runtime_build_identity(self):
            raise AssertionError("dispatcher must reject before service call")

    with pytest.raises(ValueError, match="runtime build identity takes no arguments"):
        dispatch_tool_call(
            {
                "name": "brain_runtime_build_identity",
                "arguments": {"unexpected": "must-fail"},
            },
            BuildIdentityService(),
        )


def test_packaged_runtime_build_identity_returns_fixed_projection(tmp_path):
    from agent_knowledge.runtime_build_identity import load_runtime_build_identity

    identity_path = tmp_path / "build-identity.json"
    identity_path.write_text(
        json.dumps(
            {
                "schema_version": "brain_runtime_build_identity.v1",
                "source_commit": "a" * 40,
                "build_content_manifest_hash": "sha256:" + "b" * 64,
            }
        ),
        encoding="utf-8",
    )

    identity = load_runtime_build_identity(identity_path)

    assert identity == {
        "schema_version": "brain_runtime_build_identity.v1",
        "source_commit": "a" * 40,
        "build_content_manifest_hash": "sha256:" + "b" * 64,
        "production_mutation_performed": False,
        "protected_values_returned": False,
        "raw_private_evidence_returned": False,
        "secret_returned": False,
        "host_topology_returned": False,
        "raw_external_ids_returned": False,
    }


def test_knowledge_service_reads_runtime_identity_through_narrow_dependency(tmp_path):
    expected = {
        "schema_version": "brain_runtime_build_identity.v1",
        "source_commit": "a" * 40,
        "build_content_manifest_hash": "sha256:" + "b" * 64,
        "production_mutation_performed": False,
        "protected_values_returned": False,
        "raw_private_evidence_returned": False,
        "secret_returned": False,
        "host_topology_returned": False,
        "raw_external_ids_returned": False,
    }
    service = KnowledgeSearchService(
        ledger=Ledger(tmp_path / "ledger.sqlite"),
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        runtime_build_identity_reader=lambda: expected,
    )

    result = service.brain_runtime_build_identity()

    assert result == expected


@pytest.mark.parametrize(
    "unsafe_override",
    [
        {"unexpected_field": "must-fail"},
        {"production_mutation_performed": True},
        {"secret_returned": True},
    ],
)
def test_knowledge_service_rejects_non_fixed_runtime_identity_projection(
    tmp_path,
    unsafe_override,
):
    identity = {
        "schema_version": "brain_runtime_build_identity.v1",
        "source_commit": "a" * 40,
        "build_content_manifest_hash": "sha256:" + "b" * 64,
        "production_mutation_performed": False,
        "protected_values_returned": False,
        "raw_private_evidence_returned": False,
        "secret_returned": False,
        "host_topology_returned": False,
        "raw_external_ids_returned": False,
    }
    identity.update(unsafe_override)
    service = KnowledgeSearchService(
        ledger=Ledger(tmp_path / "ledger.sqlite"),
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        runtime_build_identity_reader=lambda: identity,
    )

    with pytest.raises(ValueError, match="runtime build identity projection"):
        service.brain_runtime_build_identity()


def test_build_identity_writer_binds_full_commit_to_installed_content(tmp_path):
    from agent_knowledge.runtime_build_identity import write_runtime_build_identity

    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")
    output_path = tmp_path / "build-identity.json"

    first = write_runtime_build_identity(
        source_commit="a" * 40,
        content_root=tmp_path,
        output_path=output_path,
    )
    (tmp_path / "lib" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    second = write_runtime_build_identity(
        source_commit="a" * 40,
        content_root=tmp_path,
        output_path=output_path,
    )

    assert first["source_commit"] == "a" * 40
    assert first["build_content_manifest_hash"].startswith("sha256:")
    assert first["build_content_manifest_hash"] != second["build_content_manifest_hash"]
    assert set(json.loads(output_path.read_text(encoding="utf-8"))) == {
        "schema_version",
        "source_commit",
        "build_content_manifest_hash",
    }


def test_build_identity_requires_root_pyproject(tmp_path):
    from agent_knowledge.runtime_build_identity import (
        RuntimeBuildIdentityError,
        write_runtime_build_identity,
    )

    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

    with pytest.raises(RuntimeBuildIdentityError, match="root pyproject is missing"):
        write_runtime_build_identity(
            source_commit="a" * 40,
            content_root=tmp_path,
            output_path=tmp_path / "build-identity.json",
        )


def test_local_compose_source_sentinel_cannot_claim_deployable_identity(tmp_path):
    from agent_knowledge.runtime_build_identity import (
        RuntimeBuildIdentityError,
        load_runtime_build_identity,
        write_runtime_build_identity,
    )

    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'fixture'\n",
        encoding="utf-8",
    )
    output_path = tmp_path / "build-identity.json"

    identity = write_runtime_build_identity(
        source_commit="0" * 40,
        content_root=tmp_path,
        output_path=output_path,
    )

    assert identity["source_commit"] == "0" * 40
    with pytest.raises(
        RuntimeBuildIdentityError,
        match="packaged runtime build identity commit is invalid",
    ):
        load_runtime_build_identity(output_path)


def test_build_identity_ignores_generated_python_and_install_cache(tmp_path):
    from agent_knowledge.runtime_build_identity import write_runtime_build_identity

    (tmp_path / "lib" / "package").mkdir(parents=True)
    (tmp_path / "lib" / "package" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")
    output_path = tmp_path / "build-identity.json"
    clean = write_runtime_build_identity(
        source_commit="a" * 40,
        content_root=tmp_path,
        output_path=output_path,
    )

    (tmp_path / "lib" / "package" / "__pycache__").mkdir()
    (tmp_path / "lib" / "package" / "__pycache__" / "module.pyc").write_bytes(b"generated")
    (tmp_path / "lib" / "fixture.egg-info").mkdir()
    (tmp_path / "lib" / "fixture.egg-info" / "PKG-INFO").write_text(
        "generated metadata",
        encoding="utf-8",
    )
    with_cache = write_runtime_build_identity(
        source_commit="a" * 40,
        content_root=tmp_path,
        output_path=output_path,
    )

    assert with_cache["build_content_manifest_hash"] == clean["build_content_manifest_hash"]


def test_docker_context_excludes_generated_runtime_cache():
    dockerignore = (Path(__file__).parents[1] / ".dockerignore").read_text(encoding="utf-8")

    assert "**/__pycache__" in dockerignore
    assert "**/*.pyc" in dockerignore
    assert "**/*.egg-info" in dockerignore


def test_mcp_http_image_embeds_immutable_oci_and_packaged_identity_contract():
    dockerfile = (Path(__file__).parents[1] / "Dockerfile.mcp-http").read_text(
        encoding="utf-8"
    )

    assert "ARG NEURONS_SOURCE_COMMIT" in dockerfile
    assert "org.opencontainers.image.revision=${NEURONS_SOURCE_COMMIT}" in dockerfile
    assert (
        'org.opencontainers.image.source="https://github.com/pureliture/neurons"'
        in dockerfile
    )
    assert "python -m agent_knowledge.runtime_build_identity" in dockerfile
    assert "--source-commit ${NEURONS_SOURCE_COMMIT}" in dockerfile
    assert "--output /app/build-identity.json" in dockerfile
