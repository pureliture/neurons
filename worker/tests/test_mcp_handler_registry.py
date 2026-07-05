from __future__ import annotations

import os
from dataclasses import asdict, is_dataclass
from pathlib import Path

import pytest

from agent_knowledge import mcp_jsonrpc
from agent_knowledge.ledger import Ledger
from agent_knowledge.mcp_jsonrpc import dispatch_tool_call
from agent_knowledge.mcp_server import DisabledRetiredIndexBridgeClient, KnowledgeSearchService
from agent_knowledge.mcp_tools import TOOL_NAME, list_tools, tool_contract_registry, tool_names


def _service(tmp_path: Path) -> KnowledgeSearchService:
    private = tmp_path / "private"
    private.mkdir(parents=True, exist_ok=True)
    os.chmod(private, 0o700)
    return KnowledgeSearchService(
        ledger=Ledger(private / "ledger.sqlite"),
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
    )


def _resolve_tool_handler_registry():
    for name in (
        "tool_handler_registry",
        "_tool_handler_registry",
        "get_tool_handler_registry",
        "build_tool_handler_registry",
    ):
        value = getattr(mcp_jsonrpc, name, None)
        if callable(value):
            return name, value, "callable"

    for name in (
        "TOOL_HANDLER_REGISTRY",
        "_TOOL_HANDLER_REGISTRY",
        "tool_handler_registry_map",
        "_tool_handler_registry_map",
    ):
        value = getattr(mcp_jsonrpc, name, None)
        if isinstance(value, dict):
            return name, value, "mapping"

    pytest.fail(
        "mcp_jsonrpc must expose a tool-handler registry."
        " Please add tool_handler_registry() (or a registry-backed equivalent)"
    )


def _read_handler_registry():
    name, value, kind = _resolve_tool_handler_registry()
    if kind == "callable":
        registry = value()
    else:
        registry = value
    if not isinstance(registry, dict):
        pytest.fail(f"{name} must resolve to a dict, got {type(registry)!r}")
    return name, kind, registry


def _read_runtime_contract_registry():
    accessor = getattr(mcp_jsonrpc, "tool_runtime_contract_registry", None)
    if not callable(accessor):
        pytest.fail(
            "mcp_jsonrpc must expose tool_runtime_contract_registry() so public schema, "
            "dispatch owner, and handler callable share one internal definition"
        )
    registry = accessor()
    if not isinstance(registry, dict):
        pytest.fail(
            f"tool_runtime_contract_registry() must resolve to a dict, got {type(registry)!r}"
        )
    return registry


def _contract_payload(contract):
    if is_dataclass(contract):
        return asdict(contract)
    return getattr(contract, "__dict__", {})


def test_tool_handler_registry_matches_public_contracts():
    _, _, registry = _read_handler_registry()
    contracts = tool_contract_registry()

    assert set(registry) == set(tool_names())
    assert set(registry) == set(contracts)
    for handler in registry.values():
        assert callable(handler), f"Handler for tool is not callable: {handler}"

    for name, contract in contracts.items():
        public = contract.to_tool()
        assert "dispatch_owner" not in public
        assert "handler" not in public
        payload = _contract_payload(contract)
        assert "handler" not in payload
        for value in payload.values():
            assert not callable(value), f"Contract for {name} exposes callable object"


def test_tool_runtime_contract_registry_unifies_schema_owner_and_handler_callable():
    runtime_registry = _read_runtime_contract_registry()
    public_contracts = tool_contract_registry()

    assert set(runtime_registry) == set(public_contracts)
    for name, runtime_contract in runtime_registry.items():
        public_contract = getattr(runtime_contract, "tool_contract", None)
        handler = getattr(runtime_contract, "handler", None)
        runtime_tool = runtime_contract.to_tool()

        assert public_contract == public_contracts[name]
        assert getattr(runtime_contract, "name", None) == name
        assert (
            getattr(runtime_contract, "dispatch_owner", None)
            == public_contracts[name].dispatch_owner
        )
        assert callable(handler), f"Runtime contract for {name} must own its handler callable"

        assert runtime_tool == public_contracts[name].to_tool()
        assert "dispatch_owner" not in runtime_tool
        assert "handler" not in runtime_tool


def test_handler_registries_are_derived_from_runtime_contract_registry(monkeypatch):
    def fake_handler(_arguments, _service):
        return {"structuredContent": {"ok": True}}

    class _FakeRuntimeContract:
        def __init__(self, dispatch_owner: str) -> None:
            self.dispatch_owner = dispatch_owner
            self.handler = fake_handler

    public_contracts = tool_contract_registry()
    fake_runtime_registry = {
        name: _FakeRuntimeContract(contract.dispatch_owner)
        for name, contract in public_contracts.items()
    }

    def expected_handlers(dispatch_owner: str) -> dict:
        return {
            name: fake_handler
            for name, contract in public_contracts.items()
            if contract.dispatch_owner == dispatch_owner
        }

    monkeypatch.setattr(
        mcp_jsonrpc,
        "tool_runtime_contract_registry",
        lambda: fake_runtime_registry,
    )

    assert mcp_jsonrpc.tool_handler_registry() == {
        name: fake_handler for name in fake_runtime_registry
    }
    assert mcp_jsonrpc.steward_read_proposal_handler_registry() == expected_handlers(
        "brain_steward"
    )
    assert mcp_jsonrpc.steward_restricted_handler_registry() == expected_handlers(
        "brain_steward_restricted"
    )


def test_list_tools_exposes_public_contract_shape_only(tmp_path: Path):
    del tmp_path
    for tool in list_tools():
        assert "dispatch_owner" not in tool
        assert "handler" not in tool


def test_dispatch_tool_call_preserves_behavior_through_registry_path_for_readonly_tool(tmp_path: Path, monkeypatch):
    service = _service(tmp_path)
    params = {"name": TOOL_NAME, "arguments": {"query": "hello"}}

    baseline = dispatch_tool_call(params, service)
    assert baseline.get("structuredContent", {}).get("results") == []

    registry_name, registry_kind, registry = _read_handler_registry()
    if TOOL_NAME not in registry:
        pytest.skip(f"{TOOL_NAME} is not in handler registry")

    replacement = {**registry}

    def _registry_call(_params, _service):
        return {"__via_registry__": True}

    replacement[TOOL_NAME] = _registry_call

    if registry_kind == "callable":
        monkeypatch.setattr(mcp_jsonrpc, registry_name, lambda: replacement)
    else:
        monkeypatch.setattr(mcp_jsonrpc, registry_name, replacement)

    delegated = dispatch_tool_call(params, service)
    delegated_payload = delegated.get("structuredContent", delegated)
    assert delegated_payload == {"__via_registry__": True}
    assert delegated_payload != baseline.get("structuredContent", baseline)
