from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent_knowledge import mcp_jsonrpc, mcp_server, mcp_tools
from agent_knowledge.session_memory.brain_steward import BrainStewardService, StewardPermissionError


READ_TOOL_NAME = "memory_review_bundle_read"
COMMIT_TOOL_NAME = "memory_review_bundle_commit"
_EXACT_IDENTITY_PATTERN = r"^\S(?:[\s\S]*\S)?(?![\s\S])"

_READ_ARGUMENTS = {
    "project": "project-alpha",
    "brain_id": "/project/project-alpha",
    "scope": "project",
    "member_ids": ["member-two", "member-one"],
}
_COMMIT_ARGUMENTS = {
    **_READ_ARGUMENTS,
    "snapshot_token": "opaque-snapshot",
    "action": "approve",
    "actor": "unit-test",
    "decision_id": "decision-bundle",
}


class _FakeSteward:
    def __init__(self, *, deny: bool = False, failure: Exception | None = None) -> None:
        self.deny = deny
        self.failure = failure
        self.calls: list[tuple[str, dict]] = []
        self.writes: list[dict] = []
        self.read_result = {"schema_version": "brain_steward_review_bundle.v1", "snapshot_token": "opaque-snapshot"}
        self.commit_result = {"schema_version": "brain_steward_review_bundle_commit.v1", "write_performed": True}

    def review_bundle_read(self, *, project: str, brain_id: str, scope: str, member_ids: list[str]) -> dict:
        self.calls.append((READ_TOOL_NAME, {
            "project": project, "brain_id": brain_id, "scope": scope, "member_ids": member_ids,
        }))
        if self.failure:
            raise self.failure
        return self.read_result

    def review_bundle_commit(self, *, project: str, brain_id: str, scope: str, member_ids: list[str],
                             snapshot_token: str, action: str, actor: str, decision_id: str, reason: str = "") -> dict:
        arguments = {
            "project": project, "brain_id": brain_id, "scope": scope, "member_ids": member_ids,
            "snapshot_token": snapshot_token, "action": action, "actor": actor,
            "decision_id": decision_id, "reason": reason,
        }
        self.calls.append((COMMIT_TOOL_NAME, arguments))
        if self.deny:
            raise StewardPermissionError("restricted review commit disabled")
        if self.failure:
            raise self.failure
        self.writes.append(arguments)
        return self.commit_result

    def restricted_denied_payload(self, tool_name: str) -> dict:
        return {
            "schema_version": "brain_steward_restricted_denied.v1", "tool": tool_name,
            "permission": "denied", "reason": "restricted_tool_requires_human_gate",
            "write_performed": False, "authoritative_memory_changed": False,
        }


class _FakeService:
    def __init__(self, steward: _FakeSteward) -> None:
        self.steward = steward
        self.events: list[str] = []

    def brain_steward(self) -> _FakeSteward:
        self.events.append("brain_steward")
        return self.steward

    def invalidate_brain_card_cache(self) -> None:
        self.events.append("invalidate_brain_card_cache")


def _admin_call(tool_name: str, arguments: object, service: _FakeService) -> dict:
    return mcp_jsonrpc.handle_admin_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": tool_name, "arguments": arguments,
        }},
        service,
        auth_token=mcp_jsonrpc.ADMIN_AUTH_IDENTITY,
    )


def test_review_bundle_read_is_registered_only_on_admin_with_explicit_scope_and_members():
    contracts = mcp_tools.admin_tool_contract_registry()

    assert READ_TOOL_NAME in contracts
    assert getattr(mcp_tools, "MEMORY_REVIEW_BUNDLE_READ_TOOL_NAME", None) == READ_TOOL_NAME
    assert getattr(mcp_server, "MEMORY_REVIEW_BUNDLE_READ_TOOL_NAME", None) == READ_TOOL_NAME
    assert "MEMORY_REVIEW_BUNDLE_READ_TOOL_NAME" in mcp_server.__all__
    assert READ_TOOL_NAME in mcp_tools.ADMIN_TOOL_NAMES
    assert mcp_tools.PUBLIC_AGENT_TOOL_NAMES == frozenset({"brain.resolve", "memory_candidate_create"})
    assert READ_TOOL_NAME not in mcp_tools.public_tool_contract_registry()
    assert READ_TOOL_NAME not in mcp_tools.STEWARD_RESTRICTED_TOOL_NAMES

    contract = contracts[READ_TOOL_NAME]
    assert contract.dispatch_owner == "brain_steward"
    schema = contract.input_schema
    assert schema["type"] == "object"
    assert schema["required"] == ["project", "brain_id", "scope", "member_ids"]
    assert set(schema["properties"]) == set(schema["required"])
    assert schema["additionalProperties"] is False
    for field in ("project", "brain_id", "scope"):
        assert schema["properties"][field]["type"] == "string"
        assert schema["properties"][field]["minLength"] == 1
    assert schema["properties"]["member_ids"] == {
        "type": "array",
        "minItems": 1,
        "maxItems": 20,
        "uniqueItems": True,
        "items": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": _EXACT_IDENTITY_PATTERN},
    }


@pytest.mark.parametrize("member_ids", [["member-one"], [f"member-{index}" for index in reversed(range(20))]])
def test_review_bundle_read_dispatches_exact_arguments_without_invalidating_cache(member_ids: list[str]):
    steward = _FakeSteward()
    service = _FakeService(steward)
    arguments = {**_READ_ARGUMENTS, "member_ids": member_ids}

    response = _admin_call(READ_TOOL_NAME, arguments, service)

    assert "error" not in response
    assert response["result"]["structuredContent"] == steward.read_result
    assert json.loads(response["result"]["content"][0]["text"]) == steward.read_result
    assert steward.calls == [(READ_TOOL_NAME, arguments)]
    assert service.events == ["brain_steward"]


_INVALID_READ_ARGUMENTS = [
    pytest.param(["not-an-object"], id="arguments-array"),
    pytest.param(True, id="arguments-bool"),
    pytest.param(7, id="arguments-number"),
    pytest.param("not-an-object", id="arguments-string"),
    *[
        pytest.param({key: value for key, value in _READ_ARGUMENTS.items() if key != field}, id=f"missing-{field}")
        for field in _READ_ARGUMENTS
    ],
    *[
        pytest.param({**_READ_ARGUMENTS, field: value}, id=f"{field}-{label}")
        for field in ("project", "brain_id", "scope")
        for label, value in (("null", None), ("number", 3), ("bool", False), ("array", []), ("empty", ""), ("blank", " \t\n"))
    ],
    *[
        pytest.param({**_READ_ARGUMENTS, "member_ids": value}, id=f"members-{label}")
        for label, value in (
            ("null", None), ("string", "member-one"), ("tuple", ("member-one",)),
            ("empty", []), ("over-limit", [f"member-{index}" for index in range(21)]),
            ("duplicate", ["member-one", "member-one"]), ("empty-id", [""]),
            ("blank-id", [" \t\n"]), ("long-id", ["m" * 65]),
            ("number-id", [3]), ("bool-id", [True]), ("null-id", [None]),
            ("object-id", [{"memory_id": "member-one"}]), ("nested-id", [["member-one"]]),
        )
    ],
    pytest.param({**_READ_ARGUMENTS, "repository": "project-alpha"}, id="extra-repository"),
    pytest.param({**_READ_ARGUMENTS, "limit": 20}, id="extra-limit"),
    pytest.param({**_READ_ARGUMENTS, "action": "approve"}, id="extra-action"),
    pytest.param({key: value for key, value in {**_READ_ARGUMENTS, "repository": "project-alpha"}.items() if key != "project"}, id="repository-is-not-project"),
]


@pytest.mark.parametrize("arguments", _INVALID_READ_ARGUMENTS)
def test_review_bundle_read_strictly_rejects_invalid_input_before_service_method(arguments: object):
    steward = _FakeSteward()
    service = _FakeService(steward)

    response = _admin_call(READ_TOOL_NAME, arguments, service)

    assert response["error"] == {"code": -32602, "message": "invalid params: ValueError"}
    assert steward.calls == []
    assert "invalidate_brain_card_cache" not in service.events

    handler = mcp_jsonrpc.steward_read_proposal_handler_registry()[READ_TOOL_NAME]
    with pytest.raises(ValueError):
        handler(arguments, service)
    assert steward.calls == []


def test_review_bundle_commit_is_admin_only_restricted_with_bounded_manual_decision_schema():
    contracts = mcp_tools.admin_tool_contract_registry()

    assert COMMIT_TOOL_NAME in contracts
    assert getattr(mcp_tools, "MEMORY_REVIEW_BUNDLE_COMMIT_TOOL_NAME", None) == COMMIT_TOOL_NAME
    assert getattr(mcp_server, "MEMORY_REVIEW_BUNDLE_COMMIT_TOOL_NAME", None) == COMMIT_TOOL_NAME
    assert "MEMORY_REVIEW_BUNDLE_COMMIT_TOOL_NAME" in mcp_server.__all__
    assert COMMIT_TOOL_NAME in mcp_tools.ADMIN_TOOL_NAMES
    assert COMMIT_TOOL_NAME in mcp_tools.STEWARD_RESTRICTED_TOOL_NAMES
    assert COMMIT_TOOL_NAME not in mcp_tools.public_tool_contract_registry()
    assert mcp_tools.PUBLIC_AGENT_TOOL_NAMES == frozenset({"brain.resolve", "memory_candidate_create"})

    contract = contracts[COMMIT_TOOL_NAME]
    assert contract.dispatch_owner == "brain_steward_restricted"
    schema = contract.input_schema
    assert schema["type"] == "object"
    assert schema["required"] == ["project", "brain_id", "scope", "member_ids", "snapshot_token", "action", "actor", "decision_id"]
    assert set(schema["properties"]) == set(schema["required"]) | {"reason"}
    assert schema["additionalProperties"] is False
    for field in _READ_ARGUMENTS:
        assert schema["properties"][field] == contracts[READ_TOOL_NAME].input_schema["properties"][field]
    assert schema["properties"]["action"] == {"type": "string", "enum": ["approve", "reject"]}
    for field, maximum in (("snapshot_token", 8192), ("actor", 128), ("decision_id", 256)):
        pattern = r"\S" if field == "snapshot_token" else _EXACT_IDENTITY_PATTERN
        assert schema["properties"][field] == {"type": "string", "minLength": 1, "maxLength": maximum, "pattern": pattern}
    assert schema["properties"]["reason"] == {"type": "string", "maxLength": 2000, "default": ""}
    assert schema["if"] == {"properties": {"action": {"const": "reject"}}, "required": ["action"]}
    assert schema["then"] == {"required": ["reason"], "properties": {"reason": {"minLength": 1, "pattern": r"\S"}}}


@pytest.mark.parametrize("action,extra", [("approve", {}), ("reject", {"reason": "policy mismatch"})])
def test_review_bundle_commit_dispatches_manual_decision_then_invalidates_cache_once(action: str, extra: dict):
    steward = _FakeSteward()
    service = _FakeService(steward)
    arguments = {**_COMMIT_ARGUMENTS, "action": action, **extra}

    response = _admin_call(COMMIT_TOOL_NAME, arguments, service)

    assert "error" not in response
    assert response["result"]["structuredContent"] == steward.commit_result
    assert json.loads(response["result"]["content"][0]["text"]) == steward.commit_result
    expected_arguments = {"reason": "", **arguments}
    assert steward.calls == [(COMMIT_TOOL_NAME, expected_arguments)]
    assert steward.writes == [expected_arguments]
    assert service.events == ["brain_steward", "invalidate_brain_card_cache"]


@pytest.mark.parametrize("action,extra", [("approve", {}), ("reject", {"reason": "policy mismatch"})])
def test_review_bundle_commit_denial_preserves_existing_payload_without_writes_or_cache_invalidation(action: str, extra: dict):
    steward = _FakeSteward(deny=True)
    service = _FakeService(steward)
    arguments = {**_COMMIT_ARGUMENTS, "action": action, **extra}

    response = _admin_call(COMMIT_TOOL_NAME, arguments, service)

    assert "error" not in response
    assert response["result"]["structuredContent"] == steward.restricted_denied_payload(COMMIT_TOOL_NAME)
    assert steward.writes == []
    assert service.events == ["brain_steward"]


_INVALID_COMMIT_ARGUMENTS = [
    pytest.param(["not-an-object"], id="arguments-array"),
    pytest.param(True, id="arguments-bool"),
    pytest.param(7, id="arguments-number"),
    pytest.param("not-an-object", id="arguments-string"),
    *[
        pytest.param({key: value for key, value in _COMMIT_ARGUMENTS.items() if key != field}, id=f"missing-{field}")
        for field in _COMMIT_ARGUMENTS
    ],
    *[
        pytest.param({**_COMMIT_ARGUMENTS, field: value}, id=f"{field}-{label}")
        for field in ("project", "brain_id", "scope", "snapshot_token", "action", "actor", "decision_id")
        for label, value in (("null", None), ("number", 3), ("bool", False), ("array", []), ("empty", ""), ("blank", " \t\n"))
    ],
    *[
        pytest.param({**_COMMIT_ARGUMENTS, "member_ids": value}, id=f"members-{label}")
        for label, value in (
            ("null", None), ("string", "member-one"), ("tuple", ("member-one",)),
            ("empty", []), ("over-limit", [f"member-{index}" for index in range(21)]),
            ("duplicate", ["member-one", "member-one"]), ("empty-id", [""]),
            ("blank-id", [" \t\n"]), ("long-id", ["m" * 65]),
            ("number-id", [3]), ("bool-id", [True]), ("null-id", [None]),
            ("object-id", [{"memory_id": "member-one"}]), ("nested-id", [["member-one"]]),
        )
    ],
    *[
        pytest.param({**_COMMIT_ARGUMENTS, "action": action}, id=f"action-{label}")
        for label, action in (("auto-accept", "auto_accept"), ("supersede", "supersede"), ("uppercase", "APPROVE"), ("padded", " approve "))
    ],
    *[
        pytest.param({**_COMMIT_ARGUMENTS, field: "x" * size}, id=f"over-limit-{field}")
        for field, size in (("snapshot_token", 8193), ("actor", 129), ("decision_id", 257))
    ],
    *[
        pytest.param({**_COMMIT_ARGUMENTS, "reason": reason}, id=f"reason-{label}")
        for label, reason in (("null", None), ("number", 3), ("bool", False), ("array", []), ("object", {}), ("long", "r" * 2001))
    ],
    pytest.param({**_COMMIT_ARGUMENTS, "action": "reject"}, id="reject-missing-reason"),
    pytest.param({**_COMMIT_ARGUMENTS, "action": "reject", "reason": ""}, id="reject-empty-reason"),
    pytest.param({**_COMMIT_ARGUMENTS, "action": "reject", "reason": " \t\n"}, id="reject-blank-reason"),
    *[
        pytest.param({**_COMMIT_ARGUMENTS, field: value}, id=f"extra-{field}")
        for field, value in (("repository", "project-alpha"), ("limit", 20), ("evaluation", {}), ("operator_approval_ref", "approval"), ("allow_restricted", True), ("allow_auto_accept", True))
    ],
    pytest.param({key: value for key, value in {**_COMMIT_ARGUMENTS, "repository": "project-alpha"}.items() if key != "project"}, id="repository-is-not-project"),
]


@pytest.mark.parametrize("arguments", _INVALID_COMMIT_ARGUMENTS)
def test_review_bundle_commit_strictly_rejects_invalid_input_before_service_method(arguments: object):
    steward = _FakeSteward()
    service = _FakeService(steward)

    response = _admin_call(COMMIT_TOOL_NAME, arguments, service)

    assert response["error"] == {"code": -32602, "message": "invalid params: ValueError"}
    assert steward.calls == []
    assert steward.writes == []
    assert "invalidate_brain_card_cache" not in service.events

    handler = mcp_jsonrpc.steward_restricted_handler_registry()[COMMIT_TOOL_NAME]
    with pytest.raises(ValueError):
        handler(arguments, service)
    assert steward.calls == []
    assert steward.writes == []


@pytest.mark.parametrize("tool_name", [READ_TOOL_NAME, COMMIT_TOOL_NAME])
def test_review_bundle_dispatch_preserves_scope_and_maximum_bounded_strings(tool_name: str):
    steward = _FakeSteward()
    service = _FakeService(steward)
    arguments = {
        "project": "explicit-project",
        "brain_id": "/brain/explicit-context",
        "scope": "workspace",
        "member_ids": ["m" * 64],
    }
    if tool_name == COMMIT_TOOL_NAME:
        arguments.update({"snapshot_token": "s" * 8192, "action": "reject", "actor": "a" * 128,
                          "decision_id": "d" * 256, "reason": "r" * 2000})

    response = _admin_call(tool_name, arguments, service)

    assert "error" not in response
    assert steward.calls == [(tool_name, arguments)]


@pytest.mark.parametrize("tool_name", [READ_TOOL_NAME, COMMIT_TOOL_NAME])
def test_review_bundle_scope_schema_matches_exact_bounded_service_identities(tool_name: str):
    schema = mcp_tools.tool_contract_registry()[tool_name].input_schema

    for field, maximum in (("project", 64), ("brain_id", 256), ("scope", 64)):
        assert schema["properties"][field] == {
            "type": "string", "minLength": 1, "maxLength": maximum, "pattern": _EXACT_IDENTITY_PATTERN,
        }


@pytest.mark.parametrize("tool_name", [READ_TOOL_NAME, COMMIT_TOOL_NAME])
@pytest.mark.parametrize("field,value", [
    ("project", "p" * 65), ("brain_id", "b" * 257), ("scope", "s" * 65),
    ("project", " project-alpha"), ("brain_id", "/project/project-alpha "), ("scope", "project\n"),
    ("member_ids", [" member-one"]), ("member_ids", ["member-one "]), ("member_ids", ["member-one\n"]),
])
def test_review_bundle_rejects_nonexact_or_oversized_scope_and_member_identities(tool_name: str, field: str, value: object):
    steward = _FakeSteward()
    service = _FakeService(steward)
    base = _READ_ARGUMENTS if tool_name == READ_TOOL_NAME else _COMMIT_ARGUMENTS

    response = _admin_call(tool_name, {**base, field: value}, service)

    assert response["error"] == {"code": -32602, "message": "invalid params: ValueError"}
    assert steward.calls == []
    assert steward.writes == []
    assert "invalidate_brain_card_cache" not in service.events


@pytest.mark.parametrize("field,value", [("actor", " unit-test"), ("actor", "unit-test\n"), ("decision_id", " decision-bundle"), ("decision_id", "decision-bundle\n")])
def test_review_bundle_commit_rejects_nonexact_decision_identities(field: str, value: str):
    steward = _FakeSteward()
    service = _FakeService(steward)

    response = _admin_call(COMMIT_TOOL_NAME, {**_COMMIT_ARGUMENTS, field: value}, service)

    assert response["error"] == {"code": -32602, "message": "invalid params: ValueError"}
    assert steward.calls == []
    assert steward.writes == []
    assert "invalidate_brain_card_cache" not in service.events


@pytest.mark.parametrize("tool_name", [READ_TOOL_NAME, COMMIT_TOOL_NAME])
def test_review_bundle_public_surface_denies_before_any_service_access(tool_name: str):
    service = _FakeService(_FakeSteward())
    arguments = _READ_ARGUMENTS if tool_name == READ_TOOL_NAME else _COMMIT_ARGUMENTS
    params = {"name": tool_name, "arguments": arguments}

    response = mcp_jsonrpc.handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}, service,
    )

    assert response["error"] == {"code": -32601, "message": f"unknown tool: {tool_name}"}
    with pytest.raises(ValueError, match="unknown tool"):
        mcp_jsonrpc.dispatch_tool_call(params, service, surface="agent")
    assert service.events == []


@pytest.mark.parametrize("auto_accept", [False, True])
def test_admin_identity_or_auto_accept_capability_does_not_grant_bundle_review_commit(auto_accept: bool):
    class ForbiddenStore:
        def __getattr__(self, name: str):
            raise AssertionError(f"denied commit must not access store method {name}")

    steward = BrainStewardService(
        SimpleNamespace(read_only=False), pgvector_store=ForbiddenStore(), allow_auto_accept=auto_accept,
    )
    service = _FakeService(steward)

    response = _admin_call(COMMIT_TOOL_NAME, _COMMIT_ARGUMENTS, service)

    assert "error" not in response
    assert response["result"]["structuredContent"] == {
        "schema_version": "brain_steward_restricted_denied.v1", "tool": COMMIT_TOOL_NAME,
        "permission": "denied", "reason": "restricted_tool_requires_human_gate",
        "write_performed": False, "authoritative_memory_changed": False,
    }
    assert service.events == ["brain_steward"]


@pytest.mark.parametrize("tool_name", [READ_TOOL_NAME, COMMIT_TOOL_NAME])
@pytest.mark.parametrize("failure", [ValueError("private-snapshot-must-not-escape"), RuntimeError("private-actor-must-not-escape")])
def test_review_bundle_service_failure_is_redacted_without_cache_invalidation(tool_name: str, failure: Exception):
    steward = _FakeSteward(failure=failure)
    service = _FakeService(steward)
    arguments = _READ_ARGUMENTS if tool_name == READ_TOOL_NAME else _COMMIT_ARGUMENTS

    response = _admin_call(tool_name, arguments, service)

    expected = {"code": -32602, "message": "invalid params: ValueError"} if isinstance(failure, ValueError) else {"code": -32603, "message": "internal error"}
    assert response["error"] == expected
    assert steward.writes == []
    assert service.events == ["brain_steward"]


def test_http_sdk_retains_review_bundle_admin_schemas_and_exact_public_allowlist():
    pytest.importorskip("mcp")
    pytest.importorskip("starlette")
    from agent_knowledge import mcp_http_server

    admin_tools = {tool.name: tool for tool in mcp_http_server._to_sdk_tools(surface="admin")}
    contracts = mcp_tools.admin_tool_contract_registry()
    for tool_name in (READ_TOOL_NAME, COMMIT_TOOL_NAME):
        assert admin_tools[tool_name].inputSchema == contracts[tool_name].input_schema
    assert {tool.name for tool in mcp_http_server._to_sdk_tools(surface="agent")} == {
        "brain.resolve", "memory_candidate_create",
    }


@pytest.mark.parametrize("tool_name,arguments", [
    (READ_TOOL_NAME, {**_READ_ARGUMENTS, "repository": "project-alpha"}),
    (READ_TOOL_NAME, {**_READ_ARGUMENTS, "member_ids": ["duplicate", "duplicate"]}),
    (COMMIT_TOOL_NAME, {**_COMMIT_ARGUMENTS, "actor": "a" * 129}),
    (COMMIT_TOOL_NAME, {**_COMMIT_ARGUMENTS, "action": "reject"}),
])
def test_http_adapter_uses_strict_bundle_dispatch_without_sdk_schema_validation(tool_name: str, arguments: dict):
    pytest.importorskip("mcp")
    pytest.importorskip("starlette")
    from agent_knowledge import mcp_http_server
    steward = _FakeSteward()
    service = _FakeService(steward)

    result = asyncio.run(mcp_http_server._dispatch_call_tool(
        service, tool_name, arguments, surface="admin", auth_token=mcp_jsonrpc.ADMIN_AUTH_IDENTITY,
    ))

    assert result.isError is True
    assert result.structuredContent == {"error_code": -32602, "error_type": "ValueError"}
    assert result.content[0].text == "invalid params: ValueError"
    assert steward.calls == []
    assert steward.writes == []
    assert "invalidate_brain_card_cache" not in service.events
