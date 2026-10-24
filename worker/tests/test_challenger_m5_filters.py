"""Adversarial challenger test suite for Milestone M5: R5 MCP Query Parameters & Filters.

Challenger: challenger_m5_1
Empirically stress-tests:
1. min_age > max_age rejection (Pydantic & MCP JSON-RPC)
2. min_age == max_age boundary edge cases
3. Negative values for min_age/max_age rejection
4. Type safety: floating point and string rejection where int expected
5. Invalid enum rejection for age_bucket, currentness, time_confidence
6. Temporal age None / unknown semantics and crash immunity
7. Combinatorial multi-filter evaluation
8. Cursor fingerprinting and pagination stability under filters
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from agent_knowledge.llm_brain_core.graph_first_resolver import (
    GraphFirstResolver,
    _ResolveRequest,
    _card_matches_filters,
)
from agent_knowledge.mcp_jsonrpc import handle_jsonrpc_message
from agent_knowledge.mcp_tools import BRAIN_RESOLVE_TOOL_NAME


class _Store:
    def __init__(self, cards: list[dict[str, Any]]) -> None:
        self.cards = sorted(cards, key=lambda c: str(c.get("memory_id") or ""))

    def graph_projection_health(self, *args, **kwargs):
        return {"unprojected": 0, "projection_lag_ms": 0}

    def list_authorized_cards(self, **kwargs):
        cards = self.cards
        after_id = kwargs.get("after_memory_id")
        if after_id:
            cards = [c for c in cards if str(c.get("memory_id") or "") > str(after_id)]
        return list(cards)[: kwargs.get("limit", 100)]

    def list_context_cards(self, **kwargs):
        return list(self.cards)[: kwargs.get("limit", 100)]

    def hybrid_search(self, **kwargs):
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
    time_confidence: str | None = None,
    typed: dict[str, Any] | None = None,
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
# 1. Stress Test: min_age > max_age Must Be Rejected
# ==============================================================================


@pytest.mark.parametrize(
    ("min_age", "max_age"),
    [
        (10, 5),
        (1, 0),
        (1000000, 1),
        (86400, 3600),
        (999, 998),
    ],
)
def test_adversarial_min_age_greater_than_max_age_pydantic(min_age: int, max_age: int):
    with pytest.raises(ValidationError, match="min_age must be <= max_age"):
        _ResolveRequest(project="neurons", mode="list", min_age=min_age, max_age=max_age)


@pytest.mark.parametrize(
    ("min_age", "max_age"),
    [
        (10, 5),
        (1, 0),
        (1000000, 1),
        (86400, 3600),
    ],
)
def test_adversarial_min_age_greater_than_max_age_jsonrpc(min_age: int, max_age: int):
    mock_service = MagicMock()
    mock_service.brain_memory_resolve.return_value = {"items": []}
    call_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": BRAIN_RESOLVE_TOOL_NAME,
            "arguments": {
                "project": "neurons",
                "mode": "list",
                "min_age": min_age,
                "max_age": max_age,
            },
        },
    }
    resp = handle_jsonrpc_message(call_msg, mock_service, surface="agent")
    assert "error" in resp, "min_age > max_age must yield an error"
    assert resp["error"]["code"] == -32602
    assert "invalid params" in resp["error"]["message"]
    mock_service.brain_memory_resolve.assert_not_called()


# ==============================================================================
# 2. Boundary Edge Case: min_age == max_age
# ==============================================================================


def test_adversarial_min_age_equal_max_age_zero():
    # Boundary: min_age == max_age == 0
    req = _ResolveRequest(project="neurons", mode="list", min_age=0, max_age=0)
    assert req.min_age == 0
    assert req.max_age == 0

    now = datetime.now(timezone.utc)
    card_0s = _card("c-0s", observed_at=now.isoformat())
    card_1s = _card("c-1s", observed_at=(now - timedelta(seconds=1)).isoformat())

    resolver = GraphFirstResolver(_Store([card_0s, card_1s]))
    res = resolver.resolve(project="neurons", mode="list", min_age=0, max_age=0)
    ids = [item["id"] for item in res["items"]]
    assert "c-0s" in ids
    assert "c-1s" not in ids


def test_adversarial_min_age_equal_max_age_positive():
    now = datetime.now(timezone.utc)
    target_sec = 3600
    card_exact = _card("c-exact", observed_at=(now - timedelta(seconds=target_sec)).isoformat())
    card_minus1 = _card("c-minus1", observed_at=(now - timedelta(seconds=target_sec - 1)).isoformat())
    card_plus1 = _card("c-plus1", observed_at=(now - timedelta(seconds=target_sec + 1)).isoformat())

    resolver = GraphFirstResolver(_Store([card_exact, card_minus1, card_plus1]))
    res = resolver.resolve(project="neurons", mode="list", min_age=target_sec, max_age=target_sec)
    ids = [item["id"] for item in res["items"]]
    assert ids == ["c-exact"]


# ==============================================================================
# 3. Stress Test: Negative Values for min_age / max_age Must Be Rejected
# ==============================================================================


@pytest.mark.parametrize(
    "neg_kwargs",
    [
        {"max_age": -1},
        {"max_age": -999999},
        {"min_age": -1},
        {"min_age": -999999},
        {"min_age": -5, "max_age": -2},
        {"min_age": -1, "max_age": 10},
    ],
)
def test_adversarial_negative_age_pydantic_rejected(neg_kwargs: dict[str, Any]):
    with pytest.raises(ValidationError):
        _ResolveRequest(project="neurons", mode="list", **neg_kwargs)


@pytest.mark.parametrize(
    "neg_kwargs",
    [
        {"max_age": -1},
        {"min_age": -1},
        {"min_age": -10, "max_age": 5},
    ],
)
def test_adversarial_negative_age_jsonrpc_rejected(neg_kwargs: dict[str, Any]):
    mock_service = MagicMock()
    mock_service.brain_memory_resolve.return_value = {"items": []}
    call_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": BRAIN_RESOLVE_TOOL_NAME,
            "arguments": {"project": "neurons", "mode": "list", **neg_kwargs},
        },
    }
    resp = handle_jsonrpc_message(call_msg, mock_service, surface="agent")
    assert "error" in resp
    assert resp["error"]["code"] == -32602
    assert "invalid params" in resp["error"]["message"]
    mock_service.brain_memory_resolve.assert_not_called()


# ==============================================================================
# 4. Stress Test: Type Safety — Floats, Strings, Booleans where Int Expected
# ==============================================================================


@pytest.mark.parametrize(
    "invalid_type_kwargs",
    [
        {"max_age": "100"},
        {"min_age": "50"},
        {"max_age": True},
        {"min_age": False},
        {"max_age": 12.5},
        {"min_age": 3.14},
        {"max_age": 10.0},
        {"min_age": 0.0},
    ],
)
def test_adversarial_type_safety_pydantic_rejects_non_ints(invalid_type_kwargs: dict[str, Any]):
    with pytest.raises(ValidationError):
        _ResolveRequest(project="neurons", mode="list", **invalid_type_kwargs)


@pytest.mark.parametrize(
    "invalid_arg",
    [
        {"max_age": "100"},
        {"min_age": "0"},
        {"max_age": True},
        {"min_age": False},
        {"max_age": [10]},
        {"min_age": {"age": 5}},
    ],
)
def test_adversarial_type_safety_jsonrpc_rejects_strings_and_bools(invalid_arg: dict[str, Any]):
    mock_service = MagicMock()
    mock_service.brain_memory_resolve.return_value = {"items": []}
    call_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": BRAIN_RESOLVE_TOOL_NAME,
            "arguments": {"project": "neurons", "mode": "list", **invalid_arg},
        },
    }
    resp = handle_jsonrpc_message(call_msg, mock_service, surface="agent")
    assert "error" in resp
    assert resp["error"]["code"] == -32602


@pytest.mark.parametrize(
    "float_arg",
    [
        {"max_age": 12.5},
        {"min_age": 3.14},
        {"max_age": 10.0},
        {"max_age": float("inf")},
        {"max_age": float("-inf")},
    ],
)
def test_adversarial_type_safety_jsonrpc_float_behavior(float_arg: dict[str, Any]):
    """Empirically document whether MCP JSON-RPC accepts or rejects floats where int expected.

    Per MCP schema ("type": "integer") and mission specification:
    Floating point types where int expected must be rejected with -32602.
    """
    mock_service = MagicMock()
    mock_service.brain_memory_resolve.return_value = {"items": []}
    call_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": BRAIN_RESOLVE_TOOL_NAME,
            "arguments": {"project": "neurons", "mode": "list", **float_arg},
        },
    }
    resp = handle_jsonrpc_message(call_msg, mock_service, surface="agent")
    # This assertion verifies whether floats are rejected with -32602 as required
    assert "error" in resp and resp["error"]["code"] == -32602, (
        f"Float input {float_arg} was NOT rejected with -32602: {resp}"
    )


# ==============================================================================
# 5. Stress Test: Invalid Enum Values Must Be Rejected
# ==============================================================================


@pytest.mark.parametrize(
    "enum_kwargs",
    [
        {"age_bucket": "ancient"},
        {"age_bucket": "FRESH"},
        {"age_bucket": ""},
        {"age_bucket": "fresh "},
        {"currentness": "broken"},
        {"currentness": "CURRENT"},
        {"currentness": "active"},  # historical active is now 'current'
        {"currentness": "deleted"},
        {"time_confidence": "perfect"},
        {"time_confidence": "HIGH"},
        {"time_confidence": "none"},
    ],
)
def test_adversarial_invalid_enums_pydantic_rejected(enum_kwargs: dict[str, Any]):
    with pytest.raises(ValidationError):
        _ResolveRequest(project="neurons", mode="list", **enum_kwargs)


@pytest.mark.parametrize(
    "enum_kwargs",
    [
        {"age_bucket": "ancient"},
        {"age_bucket": "FRESH"},
        {"age_bucket": ""},
        {"currentness": "broken"},
        {"currentness": "CURRENT"},
        {"currentness": "active"},
        {"time_confidence": "perfect"},
        {"time_confidence": "HIGH"},
    ],
)
def test_adversarial_invalid_enums_jsonrpc_rejected(enum_kwargs: dict[str, Any]):
    mock_service = MagicMock()
    mock_service.brain_memory_resolve.return_value = {"items": []}
    call_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": BRAIN_RESOLVE_TOOL_NAME,
            "arguments": {"project": "neurons", "mode": "list", **enum_kwargs},
        },
    }
    resp = handle_jsonrpc_message(call_msg, mock_service, surface="agent")
    assert "error" in resp
    assert resp["error"]["code"] == -32602


# ==============================================================================
# 6. Stress Test: Temporal Age None / Unknown Handling and Crash Immunity
# ==============================================================================


def test_adversarial_card_with_all_temporal_fields_none():
    card_none = _card(
        "c-none",
        observed_at=None,
        recorded_at=None,
        valid_from=None,
        valid_to=None,
        time_confidence=None,
    )
    # 1. Default resolve must not crash and include c-none in current lane
    resolver = GraphFirstResolver(_Store([card_none]))
    res = resolver.resolve(project="neurons", mode="list")
    assert "c-none" in [it["id"] for it in res["items"]]

    # 2. Filtering by max_age must exclude c-none (age is unknown, cannot satisfy <= max_age)
    res_max = resolver.resolve(project="neurons", mode="list", max_age=86400)
    assert "c-none" not in [it["id"] for it in res_max["items"]]

    # 3. Filtering by min_age must exclude c-none (age is unknown, cannot satisfy >= min_age)
    res_min = resolver.resolve(project="neurons", mode="list", min_age=0)
    assert "c-none" not in [it["id"] for it in res_min["items"]]

    # 4. Filtering by age_bucket="unknown" must match c-none
    res_bucket_unk = resolver.resolve(project="neurons", mode="list", age_bucket="unknown")
    assert "c-none" in [it["id"] for it in res_bucket_unk["items"]]

    # 5. Filtering by age_bucket="fresh" must exclude c-none
    res_bucket_fresh = resolver.resolve(project="neurons", mode="list", age_bucket="fresh")
    assert "c-none" not in [it["id"] for it in res_bucket_fresh["items"]]

    # 6. Filtering by time_confidence="unknown" must match c-none
    res_conf_unk = resolver.resolve(project="neurons", mode="list", time_confidence="unknown")
    assert "c-none" in [it["id"] for it in res_conf_unk["items"]]

    # 7. Filtering by time_confidence="high" must exclude c-none
    res_conf_high = resolver.resolve(project="neurons", mode="list", time_confidence="high")
    assert "c-none" not in [it["id"] for it in res_conf_high["items"]]


def test_adversarial_corrupted_timestamps_crash_immunity():
    card_corrupted = _card(
        "c-corrupted",
        observed_at="corrupted-iso-string",
        recorded_at="not-a-date",
        valid_from="xyz",
        valid_to="abc",
    )
    resolver = GraphFirstResolver(_Store([card_corrupted]))
    # Must not crash
    res = resolver.resolve(project="neurons", mode="list")
    assert "c-corrupted" in [it["id"] for it in res["items"]]


def test_adversarial_session_chunks_crash_immunity():
    mock_store = MagicMock()
    mock_store.graph_projection_health.return_value = {"unprojected": 0, "projection_lag_ms": 0}
    mock_store.hybrid_search.return_value = []
    mock_store.list_context_cards.return_value = []
    mock_store.list_authorized_cards.return_value = []
    mock_store.search_session_chunks.return_value = [
        {
            "chunk_id": "chunk-123456",
            "project": "neurons",
            "content_markdown": "# Title\nChunk content without temporal headers",
            "distance": 0.1,
        }
    ]

    resolver = GraphFirstResolver(mock_store, embed_query=lambda q: [0.1] * 10)
    # Query with filters should not crash on chunk cards
    res = resolver.resolve(project="neurons", mode="query", query="Title", max_age=100)
    # Chunk cards have age None, so max_age=100 should safely filter it out
    assert len(res["items"]) == 0

    # Query without age filter returns the chunk card
    res2 = resolver.resolve(project="neurons", mode="query", query="Title")
    assert len(res2["items"]) == 1
    assert res2["items"][0]["id"] == "chunk:chunk-123456"


# ==============================================================================
# 7. Combinatorial Multi-Filter Evaluation
# ==============================================================================


def test_adversarial_multi_filter_combination_all_match():
    # Card that satisfies all 4 filters:
    # - age = 1000s (fresh, between 500 and 2000)
    # - currentness = "current"
    # - time_confidence = "high" (observed_at present)
    now = datetime.now(timezone.utc)
    target = _card(
        "c-match",
        currentness="current",
        observed_at=(now - timedelta(seconds=1000)).isoformat(),
        recorded_at=now.isoformat(),
    )
    # Competitor failing currentness
    c_fail_curr = _card(
        "c-fail-curr",
        currentness="superseded",
        observed_at=(now - timedelta(seconds=1000)).isoformat(),
        recorded_at=now.isoformat(),
    )
    # Competitor failing age_bucket / max_age
    c_fail_age = _card(
        "c-fail-age",
        currentness="current",
        observed_at=(now - timedelta(days=10)).isoformat(),
        recorded_at=now.isoformat(),
    )
    # Competitor failing time_confidence (clock fallback to recorded_at yields low confidence)
    c_fail_conf = _card(
        "c-fail-conf",
        currentness="current",
        observed_at=None,
        recorded_at=(now - timedelta(seconds=1000)).isoformat(),
    )

    resolver = GraphFirstResolver(_Store([target, c_fail_curr, c_fail_age, c_fail_conf]))
    res = resolver.resolve(
        project="neurons",
        mode="list",
        include_historical=True,
        max_age=2000,
        min_age=500,
        age_bucket="fresh",
        currentness="current",
        time_confidence="high",
    )
    ids = [it["id"] for it in res["items"]]
    assert ids == ["c-match"]


def test_adversarial_contradictory_filters_return_empty_cleanly():
    # Caller asks for age_bucket="fresh" (< 86400s) AND min_age=200000 (> 2 days)
    # Impossible to satisfy, must return empty items without crashing
    now = datetime.now(timezone.utc)
    card1 = _card("c1", observed_days_ago=0.5)
    card2 = _card("c2", observed_days_ago=5)

    resolver = GraphFirstResolver(_Store([card1, card2]))
    res = resolver.resolve(
        project="neurons",
        mode="list",
        age_bucket="fresh",
        min_age=200000,
    )
    assert res["items"] == []


def test_adversarial_historical_lane_invariants():
    now = datetime.now(timezone.utc)
    # Invariant 1: Old-but-current (age > 180d, currentness="current") stays in current lane
    c_old_current = _card("c-old-current", currentness="current", observed_days_ago=200)

    # Invariant 2: Superseded card is excluded by default
    c_superseded = _card("c-superseded", currentness="superseded", observed_days_ago=10)

    # Invariant 3: Expired valid_to is excluded by default
    c_expired = _card(
        "c-expired",
        currentness="current",
        observed_days_ago=10,
        valid_to=(now - timedelta(days=1)).isoformat(),
    )

    # Invariant 4: Conflicted card is NEVER excluded even if expired or superseded
    c_conflicted = _card(
        "c-conflicted",
        currentness="superseded",
        observed_days_ago=10,
        temporal_conflict=True,
    )

    resolver = GraphFirstResolver(_Store([c_old_current, c_superseded, c_expired, c_conflicted]))
    res = resolver.resolve(project="neurons", mode="list", include_historical=False)
    ids = [it["id"] for it in res["items"]]

    assert "c-old-current" in ids, "Old-but-current must NOT be excluded by default"
    assert "c-conflicted" in ids, "Conflicted cards must NEVER be excluded by default"
    assert "c-superseded" not in ids, "Superseded cards must be excluded by default"
    assert "c-expired" not in ids, "Expired valid_to cards must be excluded by default"


# ==============================================================================
# 8. Pagination & Keyset Cursor Handling under Filters
# ==============================================================================


def test_adversarial_keyset_pagination_under_filters():
    now = datetime.now(timezone.utc)
    cards = [
        _card(f"c-{i:02d}", observed_at=(now - timedelta(seconds=i * 100)).isoformat())
        for i in range(10)
    ]
    # Filter: max_age=550 seconds -> cards c-00, c-01, c-02, c-03, c-04, c-05 match (6 cards)
    resolver = GraphFirstResolver(_Store(cards))

    # Page 1: limit=2
    p1 = resolver.resolve(project="neurons", mode="list", max_age=550, limit=2)
    assert len(p1["items"]) == 2
    assert [it["id"] for it in p1["items"]] == ["c-00", "c-01"]
    assert p1["next_cursor"] is not None

    # Page 2: with next_cursor
    p2 = resolver.resolve(project="neurons", mode="list", max_age=550, limit=2, cursor=p1["next_cursor"])
    assert len(p2["items"]) == 2
    assert [it["id"] for it in p2["items"]] == ["c-02", "c-03"]
    assert p2["next_cursor"] is not None

    # Page 3: with p2 cursor
    p3 = resolver.resolve(project="neurons", mode="list", max_age=550, limit=2, cursor=p2["next_cursor"])
    assert len(p3["items"]) == 2
    assert [it["id"] for it in p3["items"]] == ["c-04", "c-05"]
    assert p3["next_cursor"] is None, "Last page should have no next_cursor"


def test_adversarial_cursor_tamper_filter_switch_invalidated():
    # If caller gets a cursor with max_age=500, then tries to reuse it with max_age=1000
    now = datetime.now(timezone.utc)
    cards = [
        _card(f"c-{i:02d}", observed_at=(now - timedelta(seconds=i * 10)).isoformat())
        for i in range(5)
    ]
    resolver = GraphFirstResolver(_Store(cards))

    p1 = resolver.resolve(project="neurons", mode="list", max_age=500, limit=2)
    cursor = p1["next_cursor"]
    assert cursor is not None

    # Tampered request: different filter parameters with old cursor
    # GraphFirstResolver detects fingerprint mismatch on list cursor and raises ValueError
    with pytest.raises(ValueError, match="cursor does not match"):
        resolver.resolve(project="neurons", mode="list", max_age=1000, limit=2, cursor=cursor)
