from __future__ import annotations

import ast
import inspect

import pytest

from agent_knowledge import mcp_jsonrpc
from agent_knowledge.mcp_tools import (
    MEMORY_CANDIDATE_APPROVE_TOOL_NAME,
    MEMORY_CANDIDATE_AUTO_ACCEPT_TOOL_NAME,
    MEMORY_CANDIDATE_REJECT_TOOL_NAME,
    MEMORY_STALE_COMMIT_TOOL_NAME,
    MEMORY_SUPERSEDE_COMMIT_TOOL_NAME,
    STEWARD_RESTRICTED_TOOL_NAMES,
)
from agent_knowledge.session_memory.brain_steward import StewardPermissionError


_RESTRICTED_TOOL_ARGS = {
    MEMORY_CANDIDATE_APPROVE_TOOL_NAME: {
        "candidate_memory_id": "candidate-approve-id",
        "approved_by": "unit-test",
        "decision_id": "decision-approve",
    },
    MEMORY_CANDIDATE_REJECT_TOOL_NAME: {
        "candidate_memory_id": "candidate-reject-id",
        "rejected_by": "unit-test",
        "decision_id": "decision-reject",
        "reason": "policy mismatch",
    },
    MEMORY_CANDIDATE_AUTO_ACCEPT_TOOL_NAME: {
        "candidate_memory_id": "candidate-auto-id",
        "evaluation": {"score": 0.99},
        "operator_approval_ref": "approval-ref",
    },
    MEMORY_SUPERSEDE_COMMIT_TOOL_NAME: {
        "proposal_memory_id": "proposal-supersede-id",
        "approved_by": "unit-test",
        "decision_id": "decision-supersede",
    },
    MEMORY_STALE_COMMIT_TOOL_NAME: {
        "proposal_memory_id": "proposal-stale-id",
        "approved_by": "unit-test",
        "decision_id": "decision-stale",
    },
}

_RESTRICTED_STEWARD_HANDLER_REGISTRY_CANDIDATES = (
    "restricted_steward_handler_registry",
    "steward_restricted_handler_registry",
)


def _resolve_restricted_steward_handler_registry():
    for candidate in _RESTRICTED_STEWARD_HANDLER_REGISTRY_CANDIDATES:
        accessor = getattr(mcp_jsonrpc, candidate, None)
        if callable(accessor):
            return candidate, accessor
    pytest.fail(
        "mcp_jsonrpc should expose either restricted_steward_handler_registry() or "
        "steward_restricted_handler_registry()."
    )


def _read_restricted_steward_handler_registry():
    _, accessor = _resolve_restricted_steward_handler_registry()
    handlers = accessor()
    if not isinstance(handlers, dict):
        pytest.fail(
            f"restricted steward registry accessor returned {type(handlers)!r}; expected dict"
        )
    return handlers


def _assert_denied_payload(payload: dict, tool_name: str) -> None:
    assert payload["schema_version"] == "brain_steward_restricted_denied.v1"
    assert payload["tool"] == tool_name
    assert payload["permission"] == "denied"
    assert payload["reason"] == "restricted_tool_requires_human_gate"
    assert payload["write_performed"] is False
    assert payload["authoritative_memory_changed"] is False


class _FakeSteward:
    def __init__(self, *, deny: bool) -> None:
        self.deny = deny
        self.calls: list[tuple[str, dict]] = []

    def restricted_denied_payload(self, tool_name: str) -> dict:
        return {
            "schema_version": "brain_steward_restricted_denied.v1",
            "tool": tool_name,
            "permission": "denied",
            "reason": "restricted_tool_requires_human_gate",
            "write_performed": False,
            "authoritative_memory_changed": False,
        }

    def _deny_or_result(self, tool_name: str, kwargs: dict) -> dict:
        self.calls.append((tool_name, dict(kwargs)))
        if self.deny:
            raise StewardPermissionError(f"denied: {tool_name}")
        return {
            "schema_version": "brain_steward_restricted_ok.v1",
            "tool": tool_name,
            "write_performed": True,
        }

    def candidate_approve(self, **kwargs: str) -> dict:
        return self._deny_or_result(MEMORY_CANDIDATE_APPROVE_TOOL_NAME, kwargs)

    def candidate_reject(self, **kwargs: str) -> dict:
        return self._deny_or_result(MEMORY_CANDIDATE_REJECT_TOOL_NAME, kwargs)

    def candidate_auto_accept(self, **kwargs: str) -> dict:
        return self._deny_or_result(MEMORY_CANDIDATE_AUTO_ACCEPT_TOOL_NAME, kwargs)

    def supersede_commit(self, **kwargs: str) -> dict:
        return self._deny_or_result(MEMORY_SUPERSEDE_COMMIT_TOOL_NAME, kwargs)

    def stale_commit(self, **kwargs: str) -> dict:
        return self._deny_or_result(MEMORY_STALE_COMMIT_TOOL_NAME, kwargs)


class _FakeService:
    def __init__(self, steward: _FakeSteward) -> None:
        self.steward = steward
        self.events: list[str] = []

    def brain_steward(self):
        self.events.append("brain_steward")
        return self.steward

    def invalidate_brain_card_cache(self):
        self.events.append("invalidate_brain_card_cache")


def test_restricted_steward_registry_exposes_contracted_entrypoint():
    _read_restricted_steward_handler_registry()


def test_restricted_steward_registry_key_set_matches_contract():
    registry = _read_restricted_steward_handler_registry()
    assert set(registry) == set(STEWARD_RESTRICTED_TOOL_NAMES)


@pytest.mark.parametrize(
    "tool_name",
    STEWARD_RESTRICTED_TOOL_NAMES,
)
def test_restricted_steward_handlers_are_registered_as_callables(tool_name):
    registry = _read_restricted_steward_handler_registry()
    assert callable(registry[tool_name])


@pytest.mark.parametrize(
    "tool_name",
    STEWARD_RESTRICTED_TOOL_NAMES,
)
def test_restricted_handlers_convert_permission_error_to_denied_payload_and_do_not_invalidate_cache(tool_name):
    steward = _FakeSteward(deny=True)
    service = _FakeService(steward)
    registry = _read_restricted_steward_handler_registry()
    handler = registry[tool_name]

    result = handler(_RESTRICTED_TOOL_ARGS[tool_name], service)

    assert result["structuredContent"] == steward.restricted_denied_payload(tool_name)
    _assert_denied_payload(result["structuredContent"], tool_name)
    assert service.events == ["brain_steward"]
    assert steward.calls == [(tool_name, {**_RESTRICTED_TOOL_ARGS[tool_name]})]


@pytest.mark.parametrize(
    "tool_name",
    STEWARD_RESTRICTED_TOOL_NAMES,
)
def test_restricted_handler_success_path_calls_steward_then_invalidates_cache_once(tool_name):
    steward = _FakeSteward(deny=False)
    service = _FakeService(steward)
    registry = _read_restricted_steward_handler_registry()
    handler = registry[tool_name]

    result = handler(_RESTRICTED_TOOL_ARGS[tool_name], service)

    assert result["structuredContent"]["tool"] == tool_name
    assert result["structuredContent"]["schema_version"] == "brain_steward_restricted_ok.v1"
    assert steward.calls == [(tool_name, {**_RESTRICTED_TOOL_ARGS[tool_name]})]
    assert service.events == ["brain_steward", "invalidate_brain_card_cache"]


def test_steward_restricted_path_cannot_keep_long_if_elif_chain_in_dispatcher():
    source = inspect.getsource(mcp_jsonrpc._dispatch_steward_tool)
    tree = ast.parse(source)
    comparisons: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], ast.Eq):
            continue

        left = node.left
        right = node.comparators[0]
        candidates: list[str] = []
        if isinstance(left, ast.Name) and left.id == "tool_name" and isinstance(right, ast.Constant) and isinstance(right.value, str):
            candidates.append(right.value)
        if isinstance(left, ast.Constant) and isinstance(left.value, str) and isinstance(right, ast.Name) and right.id == "tool_name":
            candidates.append(left.value)

        for name in candidates:
            if name in STEWARD_RESTRICTED_TOOL_NAMES:
                comparisons.append(name)

    assert not comparisons, (
        "restricted tool dispatch should be table-driven via steward registry seam, "
        f"but found direct comparisons: {sorted(set(comparisons))}"
    )
