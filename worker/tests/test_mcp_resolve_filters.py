from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from agent_knowledge.llm_brain_core.graph_first_resolver import (
    GraphFirstResolver,
    _ResolveRequest,
)
from agent_knowledge.mcp_jsonrpc import handle_jsonrpc_message
from agent_knowledge.mcp_tools import BRAIN_RESOLVE_TOOL_NAME, list_public_agent_tools


class _Store:
    def __init__(self, cards: list[dict[str, Any]]) -> None:
        self.cards = cards
        self.calls: list[Any] = []

    def graph_projection_health(self, *args, **kwargs):
        return {"unprojected": 0, "projection_lag_ms": 0}

    def list_authorized_cards(self, **kwargs):
        self.calls.append(("list", kwargs))
        cards = self.cards
        after_id = kwargs.get("after_memory_id")
        if after_id:
            cards = [c for c in cards if c.get("memory_id", "") > after_id]
        return cards[: kwargs.get("limit", 100)]

    def list_context_cards(self, **kwargs):
        self.calls.append(("context", kwargs))
        return list(self.cards)[: kwargs.get("limit", 100)]

    def hybrid_search(self, **kwargs):
        self.calls.append(("hybrid", kwargs))
        return list(self.cards)


def _card(
    memory_id: str,
    *,
    currentness: str = "current",
    observed_at: str | None = None,
    observed_days_ago: float | None = None,
    recorded_at: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    temporal_conflict: bool = False,
    card_type: str = "decision",
    title: str | None = None,
    summary: str | None = None,
    typed: dict[str, Any] | None = None,
    time_confidence: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    if observed_days_ago is not None:
        obs = (now - timedelta(days=observed_days_ago)).isoformat()
    else:
        obs = observed_at
    if recorded_at is None and observed_at is None and observed_days_ago is None:
        rec = None
    else:
        rec = recorded_at or now.isoformat()
    return {
        "memory_id": memory_id,
        "id": memory_id,
        "project": "neurons",
        "card_type": card_type,
        "title": title or memory_id,
        "summary": summary or f"Summary for {memory_id}",
        "typed_payload": typed or {"decision": f"Decision {memory_id}"},
        "currentness": currentness,
        "content_hash": "sha256:" + "a" * 64,
        "observed_at": obs,
        "recorded_at": rec,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "temporal_conflict": temporal_conflict,
        "time_confidence": time_confidence,
    }


# ==============================================================================
# 1. Schema & Tool Contract Tests
# ==============================================================================


def test_brain_resolve_mcp_schema_properties():
    tools = list_public_agent_tools()
    resolve_tool = next((t for t in tools if t["name"] == BRAIN_RESOLVE_TOOL_NAME), None)
    assert resolve_tool is not None, "brain.resolve must be in public agent tools"

    schema = resolve_tool["inputSchema"]
    assert schema["additionalProperties"] is False
    props = schema["properties"]

    # Verify all 6 M5 parameters exist in schema
    assert "include_historical" in props
    assert props["include_historical"]["type"] == "boolean"
    assert props["include_historical"]["default"] is False

    assert "max_age" in props
    assert props["max_age"]["type"] == "integer"
    assert props["max_age"]["minimum"] == 0

    assert "min_age" in props
    assert props["min_age"]["type"] == "integer"
    assert props["min_age"]["minimum"] == 0

    assert "age_bucket" in props
    assert props["age_bucket"]["type"] == "string"
    assert set(props["age_bucket"]["enum"]) == {
        "fresh", "recent", "aging", "old", "historical", "unknown",
    }

    assert "currentness" in props
    assert props["currentness"]["type"] == "string"
    assert set(props["currentness"]["enum"]) == {
        "current", "superseded", "stale", "retired", "conflicted", "unknown",
    }

    assert "time_confidence" in props
    assert props["time_confidence"]["type"] == "string"
    assert set(props["time_confidence"]["enum"]) == {
        "high", "medium", "low", "anomalous", "unknown",
    }


# ==============================================================================
# 2. _ResolveRequest Pydantic Validation Tests
# ==============================================================================


def test_resolve_request_valid_filters():
    req = _ResolveRequest(
        project="neurons",
        mode="list",
        include_historical=True,
        max_age=86400,
        min_age=3600,
        age_bucket="fresh",
        currentness="current",
        time_confidence="high",
    )
    assert req.include_historical is True
    assert req.max_age == 86400
    assert req.min_age == 3600
    assert req.age_bucket == "fresh"
    assert req.currentness == "current"
    assert req.time_confidence == "high"


def test_resolve_request_rejects_negative_max_age():
    with pytest.raises(ValidationError):
        _ResolveRequest(project="neurons", mode="list", max_age=-1)


def test_resolve_request_rejects_negative_min_age():
    with pytest.raises(ValidationError):
        _ResolveRequest(project="neurons", mode="list", min_age=-10)


def test_resolve_request_rejects_min_greater_than_max():
    with pytest.raises(ValidationError, match="min_age must be <= max_age"):
        _ResolveRequest(project="neurons", mode="list", min_age=100, max_age=50)


def test_resolve_request_allows_equal_min_and_max():
    req = _ResolveRequest(project="neurons", mode="list", min_age=100, max_age=100)
    assert req.min_age == 100
    assert req.max_age == 100


def test_resolve_request_rejects_invalid_enums():
    with pytest.raises(ValidationError):
        _ResolveRequest(project="neurons", mode="list", age_bucket="ancient")

    with pytest.raises(ValidationError):
        _ResolveRequest(project="neurons", mode="list", currentness="invalid_state")

    with pytest.raises(ValidationError):
        _ResolveRequest(project="neurons", mode="list", time_confidence="super_confident")


def test_resolve_request_coerces_include_historical():
    req1 = _ResolveRequest(project="neurons", mode="list")
    assert req1.include_historical is False

    req2 = _ResolveRequest(project="neurons", mode="list", include_historical=None)
    assert req2.include_historical is False


# ==============================================================================
# 3. MCP JSON-RPC Dispatch & Error Handling (-32602)
# ==============================================================================


def test_mcp_dispatch_passes_valid_filters():
    mock_service = MagicMock()
    mock_service.brain_memory_resolve.return_value = {
        "items": [],
        "metadata": {},
    }

    call_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": BRAIN_RESOLVE_TOOL_NAME,
            "arguments": {
                "project": "neurons",
                "mode": "list",
                "include_historical": True,
                "max_age": 86400,
                "min_age": 3600,
                "age_bucket": "fresh",
                "currentness": "current",
                "time_confidence": "high",
            },
        },
    }

    resp = handle_jsonrpc_message(call_msg, mock_service, surface="agent")
    assert "result" in resp
    assert "error" not in resp

    mock_service.brain_memory_resolve.assert_called_once_with(
        project="neurons",
        query="",
        mode="list",
        response_mode="minimal",
        as_of="",
        limit=5,
        cursor=None,
        include_historical=True,
        max_age=86400,
        min_age=3600,
        age_bucket="fresh",
        currentness="current",
        time_confidence="high",
    )


@pytest.mark.parametrize(
    "invalid_args",
    [
        {"max_age": -1},
        {"min_age": -5},
        {"max_age": "not_an_int"},
        {"min_age": True},  # boolean passed where int expected
        {"min_age": 100, "max_age": 50},
        {"age_bucket": "ancient"},
        {"currentness": "broken"},
        {"time_confidence": "perfect"},
        {"include_historical": "yes"},  # string passed where bool expected
    ],
)
def test_mcp_dispatch_invalid_params_yield_32602(invalid_args):
    mock_service = MagicMock()
    call_msg = {
        "jsonrpc": "2.0",
        "id": 42,
        "method": "tools/call",
        "params": {
            "name": BRAIN_RESOLVE_TOOL_NAME,
            "arguments": {"project": "neurons", "mode": "list", **invalid_args},
        },
    }

    resp = handle_jsonrpc_message(call_msg, mock_service, surface="agent")
    assert "error" in resp
    assert resp["error"]["code"] == -32602
    assert "invalid params" in resp["error"]["message"]
    mock_service.brain_memory_resolve.assert_not_called()


# ==============================================================================
# 4. GraphFirstResolver Filter Implementation Tests
# ==============================================================================


def test_resolver_historical_exclusion_and_inclusion():
    now = datetime.now(timezone.utc)
    cards = [
        _card("c-active", currentness="current", observed_days_ago=1),
        _card("c-superseded", currentness="superseded", observed_days_ago=10),
        _card("c-retired", currentness="retired", observed_days_ago=20),
        _card(
            "c-expired",
            currentness="current",
            observed_days_ago=30,
            valid_to=(now - timedelta(days=1)).isoformat(),
        ),
    ]
    resolver = GraphFirstResolver(_Store(cards))

    # 1. Default (include_historical=False) -> only active current card
    res_default = resolver.resolve(project="neurons", mode="list", limit=10)
    ids_default = [it["id"] for it in res_default["items"]]
    assert ids_default == ["c-active"]

    # 2. Explicit include_historical=True -> all 4 cards
    res_all = resolver.resolve(
        project="neurons", mode="list", include_historical=True, limit=10,
    )
    ids_all = [it["id"] for it in res_all["items"]]
    assert set(ids_all) == {"c-active", "c-superseded", "c-retired", "c-expired"}


def test_resolver_filter_by_currentness():
    cards = [
        _card("c-curr", currentness="current"),
        _card("c-sup", currentness="superseded"),
        _card("c-ret", currentness="retired"),
    ]
    resolver = GraphFirstResolver(_Store(cards))

    # Explicit currentness="current"
    res1 = resolver.resolve(project="neurons", mode="list", currentness="current", limit=10)
    assert [it["id"] for it in res1["items"]] == ["c-curr"]

    # Explicit historical currentness overrides default exclusion
    res2 = resolver.resolve(project="neurons", mode="list", currentness="superseded", limit=10)
    assert [it["id"] for it in res2["items"]] == ["c-sup"]

    res3 = resolver.resolve(project="neurons", mode="list", currentness="retired", limit=10)
    assert [it["id"] for it in res3["items"]] == ["c-ret"]


def test_resolver_filter_by_age_bucket():
    now = datetime.now(timezone.utc)
    cards = [
        _card("c-fresh", observed_at=(now - timedelta(hours=2)).isoformat()),
        _card("c-recent", observed_at=(now - timedelta(days=3)).isoformat()),
        _card("c-aging", observed_at=(now - timedelta(days=15)).isoformat()),
        _card("c-old", observed_at=(now - timedelta(days=60)).isoformat()),
        _card("c-historical", observed_at=(now - timedelta(days=200)).isoformat()),
    ]
    resolver = GraphFirstResolver(_Store(cards))

    res_fresh = resolver.resolve(project="neurons", mode="list", age_bucket="fresh", limit=10)
    assert [it["id"] for it in res_fresh["items"]] == ["c-fresh"]

    res_recent = resolver.resolve(project="neurons", mode="list", age_bucket="recent", limit=10)
    assert [it["id"] for it in res_recent["items"]] == ["c-recent"]

    res_aging = resolver.resolve(project="neurons", mode="list", age_bucket="aging", limit=10)
    assert [it["id"] for it in res_aging["items"]] == ["c-aging"]

    res_old = resolver.resolve(project="neurons", mode="list", age_bucket="old", limit=10)
    assert [it["id"] for it in res_old["items"]] == ["c-old"]

    res_hist = resolver.resolve(project="neurons", mode="list", age_bucket="historical", limit=10)
    assert [it["id"] for it in res_hist["items"]] == ["c-historical"]


def test_resolver_filter_by_min_and_max_age():
    now = datetime.now(timezone.utc)
    cards = [
        _card("c-1h", observed_at=(now - timedelta(seconds=3600)).isoformat()),
        _card("c-6h", observed_at=(now - timedelta(seconds=21600)).isoformat()),
        _card("c-24h", observed_at=(now - timedelta(seconds=86400)).isoformat()),
        _card("c-no-time", observed_at=None, recorded_at=None),
    ]
    resolver = GraphFirstResolver(_Store(cards))

    # max_age = 7200 (2h) -> only c-1h
    res_max = resolver.resolve(project="neurons", mode="list", max_age=7200, limit=10)
    assert [it["id"] for it in res_max["items"]] == ["c-1h"]

    # min_age = 7200 (2h) -> c-6h, c-24h (c-no-time excluded)
    res_min = resolver.resolve(project="neurons", mode="list", min_age=7200, limit=10)
    assert [it["id"] for it in res_min["items"]] == ["c-6h", "c-24h"]

    # min_age = 5000, max_age = 30000 -> c-6h (21600s)
    res_range = resolver.resolve(
        project="neurons", mode="list", min_age=5000, max_age=30000, limit=10,
    )
    assert [it["id"] for it in res_range["items"]] == ["c-6h"]


def test_resolver_filter_by_time_confidence():
    now = datetime.now(timezone.utc)
    cards = [
        _card("c-high", observed_at=now.isoformat()),
        _card("c-low", observed_at=None, recorded_at=now.isoformat()),  # fallback to recorded_at yields confidence="low"
    ]
    resolver = GraphFirstResolver(_Store(cards))

    res_high = resolver.resolve(project="neurons", mode="list", time_confidence="high", limit=10)
    assert [it["id"] for it in res_high["items"]] == ["c-high"]

    res_low = resolver.resolve(project="neurons", mode="list", time_confidence="low", limit=10)
    assert [it["id"] for it in res_low["items"]] == ["c-low"]


def test_resolver_combined_multi_filters():
    now = datetime.now(timezone.utc)
    cards = [
        _card("c-match", currentness="current", observed_at=(now - timedelta(hours=1)).isoformat()),
        _card("c-wrong-curr", currentness="superseded", observed_at=(now - timedelta(hours=1)).isoformat()),
        _card("c-wrong-age", currentness="current", observed_at=(now - timedelta(days=10)).isoformat()),
    ]
    resolver = GraphFirstResolver(_Store(cards))

    res = resolver.resolve(
        project="neurons",
        mode="list",
        currentness="current",
        age_bucket="fresh",
        max_age=7200,
        include_historical=True,
        limit=10,
    )
    assert [it["id"] for it in res["items"]] == ["c-match"]


def test_resolver_conflict_preservation_under_default_query():
    cards = [
        _card("c-conflict-1", currentness="conflicted", observed_days_ago=50),
        _card("c-conflict-2", currentness="superseded", temporal_conflict=True, observed_days_ago=50),
        _card("c-normal-superseded", currentness="superseded", temporal_conflict=False, observed_days_ago=50),
    ]
    resolver = GraphFirstResolver(_Store(cards))

    res = resolver.resolve(project="neurons", mode="list", limit=10)
    ids = [it["id"] for it in res["items"]]
    assert "c-conflict-1" in ids
    assert "c-conflict-2" in ids
    assert "c-normal-superseded" not in ids


def test_resolver_invariant_old_but_current_retained_by_default():
    card_old_current = _card(
        "c-old-current",
        currentness="current",
        observed_days_ago=180,
        valid_to=None,
    )
    resolver = GraphFirstResolver(_Store([card_old_current]))

    res = resolver.resolve(project="neurons", mode="list", limit=10)
    assert len(res["items"]) == 1
    assert res["items"][0]["id"] == "c-old-current"
    assert res["items"][0]["temporal"]["age"]["label"] in ("old", "historical")
    assert res["items"][0]["currentness"] == "current"
