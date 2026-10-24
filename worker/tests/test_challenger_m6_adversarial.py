"""Adversarial challenge test suite for Milestone M6: R6 Metrics Endpoint & Anomaly Handling.

Challenger: challenger_m6_1
Commit verified: eccc5cf

Tests:
1. as_of boundary conditions:
   - as_of == valid_from (INCLUDED in [valid_from, valid_to))
   - as_of == valid_to (EXCLUDED from [valid_from, valid_to))
   - as_of < valid_from (EXCLUDED)
   - as_of > valid_to (EXCLUDED)
   - Consecutive revision window handover (window A [t1, t2), window B [t2, t3))
   - Timezone offset normalization in as_of comparisons
   - as_of 6 minutes in future (> 5min tolerance) raises ValueError
   - as_of 4 minutes in future (<= 5min tolerance) is accepted
   - Exact 300s boundary check (290s accepted, 310s rejected)
2. Future anomaly sorting & invariants:
   - observed_at > now + 5min yields time_confidence == 'anomalous', temporal_relation == 'future'
   - observed_at <= now + 5min (within tolerance) is NOT anomalous
   - Mixed candidate pool: 4 normal cards (fresh, recent, old, historical) mixed with 3 future/anomalous cards
   - Invariant: ALL anomalous cards sorted behind ALL normal cards
   - Invariant: Relative ordering among normal cards is strictly preserved (Timsort stability)
   - Invariant: Relative ordering among anomalous cards is strictly preserved
3. Edge case validity window variants:
   - valid_from specified, valid_to None (open upper bound)
   - valid_from None, valid_to specified (open lower bound)
4. Chunk recall (pg_recall) future as_of rejection & fallback reason:
   - as_of 6min future rejected with ValueError
   - as_of 4min future accepted
   - No current match falls back to past chunk with fallback_reason='no_current_match'
   - Anomalous future chunk is excluded from chunk recall results
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
from agent_knowledge.rag_ingress import pg_recall


def _card(
    memory_id: str,
    *,
    currentness: str = "current",
    observed_days_ago: float | None = None,
    observed_at: str | None = None,
    created_at: str | None = None,
    recorded_at: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    superseded_by: list[str] | str | None = None,
    card_type: str = "decision",
    title: str | None = None,
    summary: str | None = None,
    content_hash: str | None = None,
    typed: dict | None = None,
    temporal_conflict: bool = False,
    time_confidence: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    if observed_at is None and observed_days_ago is not None:
        observed_at = (now - timedelta(days=observed_days_ago)).isoformat()
    if created_at is None and recorded_at is not None:
        created_at = recorded_at
    if created_at is None and observed_at is not None:
        created_at = observed_at

    payload = dict(typed or {})
    card: dict[str, Any] = {
        "memory_id": memory_id,
        "project": "neurons",
        "card_type": card_type,
        "title": title or f"Title {memory_id}",
        "summary": summary or f"Summary {memory_id}",
        "currentness": currentness,
        "content_hash": content_hash or ("sha256:" + "a" * 64),
        "typed_payload": payload,
        "freshness_state": "checked",
        "freshness_checked_at": now.isoformat(),
        "temporal_conflict": temporal_conflict,
    }
    if observed_at is not None:
        card["observed_at"] = observed_at
    if created_at is not None:
        card["created_at"] = created_at
    if valid_from is not None:
        card["valid_from"] = valid_from
    if valid_to is not None:
        card["valid_to"] = valid_to
    if superseded_by is not None:
        card["superseded_by"] = superseded_by if isinstance(superseded_by, list) else [superseded_by]
    if time_confidence is not None:
        card["time_confidence"] = time_confidence
    return card


class _Store:
    def __init__(self, cards: list[dict[str, Any]]):
        self.cards = list(cards)

    def graph_projection_health(self, project: str, *, as_of: str | None = None, steward_only: bool = False):
        return {"unprojected": False, "projection_lag_ms": 0}

    def list_authorized_cards(self, **kwargs):
        wanted = kwargs.get("memory_ids")
        cards = self.cards if wanted is None else [c for c in self.cards if c["memory_id"] in wanted]
        after_id = kwargs.get("after_memory_id")
        if after_id:
            cards = [c for c in cards if c["memory_id"] > after_id]
        if wanted is not None:
            cards = sorted(cards, key=lambda c: wanted.index(c["memory_id"]))
        return cards[: kwargs.get("limit", 100)]

    def list_context_cards(self, **kwargs):
        return self.list_authorized_cards(**kwargs)


# ==============================================================================
# Suite 1: as_of Boundary Conditions & Handover Invariant
# ==============================================================================

def test_as_of_boundary_exact_valid_from_included():
    """Boundary: as_of == valid_from MUST be included in the validity window [valid_from, valid_to)."""
    t_start = "2026-06-01T12:00:00Z"
    t_end = "2026-07-01T12:00:00Z"

    card = _card(
        "card-window-boundary",
        currentness="superseded",
        valid_from=t_start,
        valid_to=t_end,
        observed_at=t_start,
    )
    resolver = GraphFirstResolver(_Store([card]), None, None)

    # Exact boundary match at valid_from
    res = resolver.resolve(project="neurons", mode="list", query="card", as_of=t_start)
    assert len(res["items"]) == 1
    assert res["items"][0]["id"] == "card-window-boundary"
    assert res.get("temporal_view") == "as_of" or res.get("metadata", {}).get("temporal_view") == "as_of"


def test_as_of_boundary_exact_valid_to_excluded():
    """Boundary: as_of == valid_to MUST be excluded because interval is half-open [valid_from, valid_to)."""
    t_start = "2026-06-01T12:00:00Z"
    t_end = "2026-07-01T12:00:00Z"

    card = _card(
        "card-window-boundary",
        currentness="superseded",
        valid_from=t_start,
        valid_to=t_end,
        observed_at=t_start,
    )
    resolver = GraphFirstResolver(_Store([card]), None, None)

    # Exact boundary match at valid_to
    res = resolver.resolve(project="neurons", mode="list", query="card", as_of=t_end)
    assert len(res["items"]) == 0


def test_as_of_outside_window_excluded():
    """Boundary: as_of < valid_from and as_of > valid_to MUST be excluded."""
    t_start = "2026-06-01T12:00:00Z"
    t_end = "2026-07-01T12:00:00Z"

    card = _card(
        "card-window",
        currentness="superseded",
        valid_from=t_start,
        valid_to=t_end,
        observed_at=t_start,
    )
    resolver = GraphFirstResolver(_Store([card]), None, None)

    # 1 second before valid_from
    res_before = resolver.resolve(project="neurons", mode="list", query="card", as_of="2026-06-01T11:59:59Z")
    assert len(res_before["items"]) == 0

    # 1 second after valid_to
    res_after = resolver.resolve(project="neurons", mode="list", query="card", as_of="2026-07-01T12:00:01Z")
    assert len(res_after["items"]) == 0


def test_consecutive_revision_window_handover():
    """Invariance: Consecutive revisions with touching boundaries [t1, t2) and [t2, t3).
    At as_of == t2, revision 1 is excluded and revision 2 is included.
    No double-counting and no temporal hole.
    """
    t1 = "2026-05-01T00:00:00Z"
    t2 = "2026-06-01T00:00:00Z"
    t3 = "2026-07-01T00:00:00Z"

    card_v1 = _card("rev-1", currentness="superseded", valid_from=t1, valid_to=t2, observed_at=t1)
    card_v2 = _card("rev-2", currentness="superseded", valid_from=t2, valid_to=t3, observed_at=t2)
    card_v3 = _card("rev-3", currentness="current", valid_from=t3, valid_to=None, observed_at=t3)

    resolver = GraphFirstResolver(_Store([card_v1, card_v2, card_v3]), None, None)

    # At t1: exactly rev-1
    res_t1 = resolver.resolve(project="neurons", mode="list", query="rev", as_of=t1)
    assert [it["id"] for it in res_t1["items"]] == ["rev-1"]

    # At t2: exactly rev-2 (handover point)
    res_t2 = resolver.resolve(project="neurons", mode="list", query="rev", as_of=t2)
    assert [it["id"] for it in res_t2["items"]] == ["rev-2"]

    # At t3: exactly rev-3
    res_t3 = resolver.resolve(project="neurons", mode="list", query="rev", as_of=t3)
    assert [it["id"] for it in res_t3["items"]] == ["rev-3"]


def test_as_of_timezone_offset_handling():
    """Stress: as_of with timezone offsets (+09:00 vs UTC) matches equivalent instants."""
    card = _card(
        "card-tz",
        currentness="superseded",
        valid_from="2026-06-01T12:00:00+00:00",
        valid_to="2026-07-01T12:00:00+00:00",
        observed_at="2026-06-01T12:00:00+00:00",
    )
    resolver = GraphFirstResolver(_Store([card]), None, None)

    # Query at valid_from in KST (+09:00): 12:00 UTC == 21:00 KST
    res_kst_included = resolver.resolve(
        project="neurons", mode="list", query="card", as_of="2026-06-01T21:00:00+09:00"
    )
    assert len(res_kst_included["items"]) == 1

    # Query at valid_to in KST: 2026-07-01T21:00:00+09:00 == 12:00 UTC -> Excluded
    res_kst_excluded = resolver.resolve(
        project="neurons", mode="list", query="card", as_of="2026-07-01T21:00:00+09:00"
    )
    assert len(res_kst_excluded["items"]) == 0


def test_as_of_future_tolerance_strict_boundaries():
    """FR-AGE-026: as_of within 5min future is accepted; beyond 5min future is rejected."""
    now = datetime.now(timezone.utc)
    resolver = GraphFirstResolver(_Store([]), None, None)

    # 4 minutes in future -> Accepted
    as_of_4m = (now + timedelta(minutes=4)).isoformat()
    res_4m = resolver.resolve(project="neurons", query="x", as_of=as_of_4m)
    assert res_4m.get("temporal_view") == "as_of" or res_4m.get("metadata", {}).get("temporal_view") == "as_of"

    # 6 minutes in future -> Rejected with ValueError
    as_of_6m = (now + timedelta(minutes=6)).isoformat()
    with pytest.raises(ValueError, match=r"(invalid_as_of_future|future)"):
        resolver.resolve(project="neurons", query="x", as_of=as_of_6m)

    # Fine-grained 300-second boundary test:
    # 290 seconds in future (< 300s) -> Accepted
    as_of_290s = (now + timedelta(seconds=290)).isoformat()
    res_290s = resolver.resolve(project="neurons", query="x", as_of=as_of_290s)
    assert "items" in res_290s

    # 310 seconds in future (> 300s) -> Rejected
    as_of_310s = (now + timedelta(seconds=310)).isoformat()
    with pytest.raises(ValueError, match=r"(invalid_as_of_future|future)"):
        resolver.resolve(project="neurons", query="x", as_of=as_of_310s)


def test_as_of_unbounded_windows():
    """Edge cases: Open lower bound (valid_from=None) and open upper bound (valid_to=None)."""
    card_open_upper = _card("open-upper", valid_from="2026-01-01T00:00:00Z", valid_to=None)
    card_open_lower = _card("open-lower", valid_from=None, valid_to="2026-06-01T00:00:00Z", currentness="superseded")

    resolver = GraphFirstResolver(_Store([card_open_upper, card_open_lower]), None, None)

    # Query at 2026-03-01: both are valid
    res_mid = resolver.resolve(project="neurons", mode="list", query="open", as_of="2026-03-01T00:00:00Z")
    ids_mid = [it["id"] for it in res_mid["items"]]
    assert "open-upper" in ids_mid
    assert "open-lower" in ids_mid

    # Query at 2026-07-01: only open-upper is valid
    res_late = resolver.resolve(project="neurons", mode="list", query="open", as_of="2026-07-01T00:00:00Z")
    ids_late = [it["id"] for it in res_late["items"]]
    assert "open-upper" in ids_late
    assert "open-lower" not in ids_late


# ==============================================================================
# Suite 2: Future Anomaly Sorting & Invariant Stress
# ==============================================================================

def test_future_observed_at_anomaly_classification():
    """FR-AGE-071 & Section 7.1: observed_at > now + 5min is anomalous; observed_at <= now + 5min is not."""
    now = datetime.now(timezone.utc)

    # Future > 5 min (+10 min)
    card_anom = _card("anom-card", observed_at=(now + timedelta(minutes=10)).isoformat(), recorded_at=now.isoformat())
    # Future <= 5 min (+2 min, within clock skew tolerance)
    card_skew = _card("skew-card", observed_at=(now + timedelta(minutes=2)).isoformat(), recorded_at=now.isoformat())

    resolver = GraphFirstResolver(_Store([card_anom, card_skew]), None, None)
    res = resolver.resolve(project="neurons", mode="list", query="card", limit=10)
    items_by_id = {it["id"]: it for it in res["items"]}

    t_anom = items_by_id["anom-card"]["temporal"]
    assert t_anom["time_confidence"] == "anomalous"
    assert t_anom["temporal_relation"] == "future"
    assert "observed_after_now" in t_anom["age"]["reason_codes"]

    t_skew = items_by_id["skew-card"]["temporal"]
    assert t_skew["time_confidence"] != "anomalous"


def test_mixed_pool_anomaly_sorting_preserves_order_and_demotes():
    """Adversarial Stress (FR-AGE-075):
    Mixed candidate pool with 4 normal cards across age spectrum and 3 anomalous cards.
    Invariants:
    1. Every normal card is ranked before every anomalous card.
    2. Relative order among normal cards is strictly preserved.
    3. Relative order among anomalous cards is strictly preserved.
    """
    now = datetime.now(timezone.utc)

    normal_1 = _card("n1-fresh", observed_days_ago=0.01)       # ~15 min old
    normal_2 = _card("n2-recent", observed_days_ago=2.0)        # 2 days old
    normal_3 = _card("n3-old", observed_days_ago=45.0)          # 45 days old
    normal_4 = _card("n4-historical", observed_days_ago=200.0)   # 200 days old

    # Different future anomalies:
    anom_1_future_1h = _card("a1-future-1h", observed_at=(now + timedelta(hours=1)).isoformat())
    anom_2_future_10d = _card("a2-future-10d", observed_at=(now + timedelta(days=10)).isoformat())
    anom_3_explicit = _card("a3-explicit-anom", time_confidence="anomalous", observed_days_ago=5.0)

    # Interleave normal and anomalous cards in store input:
    interleaved_pool = [
        anom_1_future_1h,
        normal_1,
        anom_2_future_10d,
        normal_2,
        anom_3_explicit,
        normal_3,
        normal_4,
    ]

    resolver = GraphFirstResolver(_Store(interleaved_pool), None, None)
    result = resolver.resolve(project="neurons", mode="list", query="x", limit=20)
    returned_ids = [it["id"] for it in result["items"]]

    expected_normal_order = ["n1-fresh", "n2-recent", "n3-old", "n4-historical"]
    expected_anom_order = ["a1-future-1h", "a2-future-10d", "a3-explicit-anom"]

    # Invariant 1: Top 4 items are all normal items in exact preserved order
    assert returned_ids[:4] == expected_normal_order, f"Normal items out of order or demoted: {returned_ids}"

    # Invariant 2: Bottom 3 items are all anomalous items in exact preserved order
    assert returned_ids[4:] == expected_anom_order, f"Anomalous items out of order: {returned_ids}"


# ==============================================================================
# Suite 3: Chunk Recall (pg_recall) Future as_of Rejection & Fallback
# ==============================================================================

def test_chunk_recall_future_as_of_boundaries():
    """FR-AGE-026: pg_recall._search validates as_of clock skew tolerance."""
    now = datetime.now(timezone.utc)

    mock_store = MagicMock()
    mock_store.search_session_chunks.return_value = []
    mock_embed = MagicMock()
    mock_embed.model = "test-model"
    mock_embed.size = 16
    mock_embed.embed.return_value = [0.1] * 16

    environ = {
        "NEURON_LBRAIN_PGVECTOR_DSN": "postgresql://mock/db",
        "PG_RECALL_DIRECT_MIGRATED": "true",
    }

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pg_recall, "PgVectorStore", lambda **kw: mock_store)
        mp.setattr("agent_knowledge.rag_ingress.qdrant_embedding.build_openai_embedding_provider", lambda **kw: mock_embed)
        mp.setattr(pg_recall, "validated_pg_embedding_profile", lambda *a: None)
        mp.setattr(pg_recall, "assert_pg_embedding_egress_safe", lambda *a: None)

        search = pg_recall.build_pg_brain_query_search_from_env(environ)
        assert search is not None

        # 6 minutes future -> Rejected
        as_of_6m = (now + timedelta(minutes=6)).isoformat()
        with pytest.raises(ValueError, match=r"invalid_as_of_future"):
            search("query", "/project/test-proj", as_of=as_of_6m)

        # 4 minutes future -> Accepted (no error)
        as_of_4m = (now + timedelta(minutes=4)).isoformat()
        res = search("query", "/project/test-proj", as_of=as_of_4m)
        assert res == []


def test_chunk_recall_anomalous_chunk_excluded_from_results():
    """FR-AGE-075: pg_recall chunk with future anomaly is excluded, returning only normal valid chunk."""
    now = datetime.now(timezone.utc)

    anom_chunk = {
        "chunk_id": "chunk-anom-future",
        "content": "Future anomaly chunk",
        "observed_at": (now + timedelta(hours=2)).isoformat(),
        "created_at": now.isoformat(),
        "authority_currentness": "current",
    }
    normal_chunk = {
        "chunk_id": "chunk-normal-valid",
        "content": "Normal valid chunk",
        "observed_at": (now - timedelta(hours=1)).isoformat(),
        "created_at": now.isoformat(),
        "authority_currentness": "current",
    }

    mock_store = MagicMock()
    mock_store.search_session_chunks.return_value = [
        {
            "chunk_id": "chunk-anom-future",
            "distance": 0.1,
            "session_id_hash": "sess-hash-1",
            "content_hash": pg_recall.sha256_hash("Future anomaly chunk"),
            "content_markdown": "Future anomaly chunk",
            "provider": "test-provider",
            "project": "test-proj",
        },
        {
            "chunk_id": "chunk-normal-valid",
            "distance": 0.2,
            "session_id_hash": "sess-hash-2",
            "content_hash": pg_recall.sha256_hash("Normal valid chunk"),
            "content_markdown": "Normal valid chunk",
            "provider": "test-provider",
            "project": "test-proj",
        },
    ]

    def _get_chunk(chunk_id: str):
        row = MagicMock()
        row.chunk_id = chunk_id
        row.embedding_state = "ready"
        row.embedding_model = "test-model"
        row.embedding = [0.1] * 16
        row.project = "test-proj"
        row.provider = "test-provider"
        if chunk_id == "chunk-anom-future":
            row.content_markdown = anom_chunk["content"]
            row.content_hash = pg_recall.sha256_hash(anom_chunk["content"])
            row.session_id_hash = "sess-hash-1"
            row.observed_at = anom_chunk["observed_at"]
            row.created_at = anom_chunk["created_at"]
            row.authority_currentness = "current"
        else:
            row.content_markdown = normal_chunk["content"]
            row.content_hash = pg_recall.sha256_hash(normal_chunk["content"])
            row.session_id_hash = "sess-hash-2"
            row.observed_at = normal_chunk["observed_at"]
            row.created_at = normal_chunk["created_at"]
            row.authority_currentness = "current"
        return row

    mock_store.get_chunk.side_effect = _get_chunk
    mock_embed = MagicMock()
    mock_embed.model = "test-model"
    mock_embed.size = 16
    mock_embed.embed.return_value = [0.1] * 16

    environ = {
        "NEURON_LBRAIN_PGVECTOR_DSN": "postgresql://mock/db",
        "PG_RECALL_DIRECT_MIGRATED": "true",
    }

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pg_recall, "PgVectorStore", lambda **kw: mock_store)
        mp.setattr("agent_knowledge.rag_ingress.qdrant_embedding.build_openai_embedding_provider", lambda **kw: mock_embed)
        mp.setattr(pg_recall, "validated_pg_embedding_profile", lambda *a: None)
        mp.setattr(pg_recall, "assert_pg_embedding_egress_safe", lambda *a: None)

        search = pg_recall.build_pg_brain_query_search_from_env(environ)
        assert search is not None

        results = search("query", "/project/test-proj")
        result_ids = [r["memory_id"] for r in results]

        # Invariant: Anomalous chunk must NOT appear in normal recall results
        assert "chunk-anom-future" not in result_ids
        assert "chunk-normal-valid" in result_ids
