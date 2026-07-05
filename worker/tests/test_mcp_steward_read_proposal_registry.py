from __future__ import annotations

import ast
import inspect

import pytest

from agent_knowledge import mcp_jsonrpc
from agent_knowledge.mcp_tools import (
    MEMORY_AUTHORITY_PACK_READ_TOOL_NAME,
    MEMORY_CANDIDATE_CREATE_TOOL_NAME,
    MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME,
    MEMORY_STALE_MARK_TOOL_NAME,
    MEMORY_SUPERSEDE_PROPOSE_TOOL_NAME,
    STEWARD_RESTRICTED_TOOL_NAMES,
)

_READ_PROPOSAL_STEWARD_TOOL_NAMES = (
    MEMORY_AUTHORITY_PACK_READ_TOOL_NAME,
    MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME,
    MEMORY_CANDIDATE_CREATE_TOOL_NAME,
    MEMORY_STALE_MARK_TOOL_NAME,
    MEMORY_SUPERSEDE_PROPOSE_TOOL_NAME,
)
_SOURCE_SPAN_PROPOSAL_TOOL_NAMES = frozenset(
    {
        MEMORY_CANDIDATE_CREATE_TOOL_NAME,
        MEMORY_SUPERSEDE_PROPOSE_TOOL_NAME,
    }
)

_READ_PROPOSAL_TOOL_ARGS = {
    MEMORY_AUTHORITY_PACK_READ_TOOL_NAME: {
        "project": "project-alpha",
        "limit": 8,
    },
    MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME: {
        "project": "project-alpha",
        "limit": 10,
    },
    MEMORY_CANDIDATE_CREATE_TOOL_NAME: {
        "review_reason": "unit-test reason",
        "mark_needs_review": True,
        "proposer": "codex",
    },
    MEMORY_STALE_MARK_TOOL_NAME: {
        "memory_id": "memory-stale-id",
        "reason": "unit test stale",
        "proposer": "codex",
    },
    MEMORY_SUPERSEDE_PROPOSE_TOOL_NAME: {
        "old_memory_id": "proposal-old-id",
        "proposer": "codex",
    },
}


class _FakeSteward:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.selected_spans: list[dict] = []

    def authority_pack_read(self, **kwargs: str | int) -> dict:
        self.calls.append((MEMORY_AUTHORITY_PACK_READ_TOOL_NAME, dict(kwargs)))
        return {"tool": MEMORY_AUTHORITY_PACK_READ_TOOL_NAME}

    def review_queue_list(self, **kwargs: str | int) -> dict:
        self.calls.append((MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME, dict(kwargs)))
        return {"tool": MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME}

    def candidate_create(self, **kwargs: object) -> dict:
        self.calls.append((MEMORY_CANDIDATE_CREATE_TOOL_NAME, dict(kwargs)))
        return {"tool": MEMORY_CANDIDATE_CREATE_TOOL_NAME}

    def stale_mark(self, **kwargs: object) -> dict:
        self.calls.append((MEMORY_STALE_MARK_TOOL_NAME, dict(kwargs)))
        return {"tool": MEMORY_STALE_MARK_TOOL_NAME}

    def supersede_propose(self, **kwargs: object) -> dict:
        self.calls.append((MEMORY_SUPERSEDE_PROPOSE_TOOL_NAME, dict(kwargs)))
        return {"tool": MEMORY_SUPERSEDE_PROPOSE_TOOL_NAME}

    def select_source_span(self, arguments: dict) -> dict:
        span = {"source_ref": {"source_id": "fake"}, "span_ref": {"span_id": "fake"}, "content_hash": "sha256:fake"}
        self.calls.append(("select_source_span", dict(arguments)))
        self.selected_spans.append(span)
        return span


class _FakeService:
    def __init__(self, steward: _FakeSteward) -> None:
        self.steward = steward
        self.events: list[str] = []

    def brain_steward(self) -> _FakeSteward:
        self.events.append("brain_steward")
        return self.steward

    def invalidate_brain_card_cache(self) -> None:
        self.events.append("invalidate_brain_card_cache")


def _read_steward_read_proposal_handler_registry() -> dict:
    accessor = getattr(mcp_jsonrpc, "steward_read_proposal_handler_registry", None)
    if not callable(accessor):
        pytest.fail("mcp_jsonrpc should expose steward_read_proposal_handler_registry()")
    handlers = accessor()
    if not isinstance(handlers, dict):
        pytest.fail(
            f"steward_read_proposal_handler_registry returned {type(handlers)!r}; expected dict"
        )
    return handlers


def _assert_single_steward_call(steward: _FakeSteward, tool_name: str) -> None:
    assert steward.calls == [(tool_name, _READ_PROPOSAL_TOOL_ARGS[tool_name])]


def _assert_source_span_proposal_call(steward: _FakeSteward, tool_name: str) -> None:
    assert len(steward.calls) == 2
    select_call, proposal_call = steward.calls
    source_span = proposal_call[1]["source_span"]

    assert select_call == ("select_source_span", _READ_PROPOSAL_TOOL_ARGS[tool_name])
    assert proposal_call[0] == tool_name
    assert isinstance(source_span, dict)
    assert steward.selected_spans == [source_span]
    assert proposal_call[1] == dict(_READ_PROPOSAL_TOOL_ARGS[tool_name], source_span=source_span)


def test_steward_read_proposal_registry_exposes_contracted_entrypoint():
    _read_steward_read_proposal_handler_registry()


def test_steward_read_proposal_registry_key_set_matches_contract():
    registry = _read_steward_read_proposal_handler_registry()
    assert set(registry) == set(_READ_PROPOSAL_STEWARD_TOOL_NAMES)


def test_steward_read_proposal_registry_excludes_restricted_names():
    registry = _read_steward_read_proposal_handler_registry()
    assert set(registry).isdisjoint(set(STEWARD_RESTRICTED_TOOL_NAMES))


@pytest.mark.parametrize("tool_name", _READ_PROPOSAL_STEWARD_TOOL_NAMES)
def test_steward_read_proposal_handlers_are_callables(tool_name: str):
    registry = _read_steward_read_proposal_handler_registry()
    assert callable(registry[tool_name])


@pytest.mark.parametrize("tool_name", _READ_PROPOSAL_STEWARD_TOOL_NAMES)
def test_read_proposal_handlers_call_steward_methods_and_do_not_invalidate_cache(tool_name: str):
    steward = _FakeSteward()
    service = _FakeService(steward)
    registry = _read_steward_read_proposal_handler_registry()

    handler = registry[tool_name]
    result = handler(_READ_PROPOSAL_TOOL_ARGS[tool_name], service)

    assert result["structuredContent"] == {"tool": tool_name}
    assert service.events == ["brain_steward"]
    assert "invalidate_brain_card_cache" not in service.events

    if tool_name in _SOURCE_SPAN_PROPOSAL_TOOL_NAMES:
        _assert_source_span_proposal_call(steward, tool_name)
    else:
        _assert_single_steward_call(steward, tool_name)


def test_steward_dispatcher_uses_read_proposal_and_restricted_registry_paths_not_direct_if_chain():
    source = inspect.getsource(mcp_jsonrpc._dispatch_steward_tool)
    tree = ast.parse(source)
    direct_tool_name_comparisons: list[str] = []
    read_proposal_names = set(_READ_PROPOSAL_STEWARD_TOOL_NAMES)

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1 or not isinstance(node.ops[0], ast.Eq):
                continue
            left = node.left
            right = node.comparators[0]
            if isinstance(left, ast.Name) and left.id == "tool_name" and isinstance(right, ast.Constant) and isinstance(right.value, str):
                if right.value in read_proposal_names or right.value in STEWARD_RESTRICTED_TOOL_NAMES:
                    direct_tool_name_comparisons.append(right.value)
            if isinstance(left, ast.Constant) and isinstance(left.value, str) and isinstance(right, ast.Name) and right.id == "tool_name":
                if left.value in read_proposal_names or left.value in STEWARD_RESTRICTED_TOOL_NAMES:
                    direct_tool_name_comparisons.append(left.value)

    assert not direct_tool_name_comparisons, (
        "steward dispatch should route through registries instead of direct tool-name compares; "
        f"found direct checks for {sorted(set(direct_tool_name_comparisons))}"
    )
