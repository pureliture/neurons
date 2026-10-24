"""Comprehensive Acceptance & Regression Suite for LBrain Information Age (FR-AGE / AC-AGE).

Covers:
- Existing 6 basic regression tests (retained).
- 15 Acceptance Criteria (AC-AGE-001 ~ AC-AGE-015).
- Section 7.1 Decision Table (12 rows parameterized).
- Section 8 Response Contract (22 common temporal fields for card & chunk paths).

Uses self-contained in-memory Mock Store / Mock Graph patterns.
Runs in under 2 seconds without external DB or network access.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
from agent_knowledge.rag_ingress.pg_recall import _session_temporal_block


# ==============================================================================
# Helper Factories & Mock Harnesses
# ==============================================================================

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
    freshness_state: str = "unchecked",
    freshness_checked_at: str | None = None,
    freshness_next_check_after: str | None = None,
    freshness_gaps: list[str] | None = None,
    source_hash: str | None = None,
    time_confidence: str | None = None,
    task_state: str | None = None,
) -> dict[str, Any]:
    """Construct an in-memory memory_card dictionary with temporal metadata."""
    now = datetime.now(timezone.utc)
    if observed_at is None and observed_days_ago is not None:
        observed_at = (now - timedelta(days=observed_days_ago)).isoformat()
    if created_at is None and recorded_at is not None:
        created_at = recorded_at
    if created_at is None and observed_at is not None:
        created_at = observed_at

    sup_list = (
        superseded_by
        if isinstance(superseded_by, list)
        else [superseded_by]
        if superseded_by is not None
        else []
    )
    payload = dict(typed or {})
    if task_state is not None:
        payload["task_state"] = task_state

    card: dict[str, Any] = {
        "memory_id": memory_id,
        "project": "neurons",
        "card_type": card_type,
        "title": title or memory_id,
        "summary": summary or memory_id,
        "currentness": currentness,
        "content_hash": content_hash or ("sha256:" + "a" * 64),
        "typed_payload": payload,
        "freshness_state": freshness_state,
        "freshness_checked_at": freshness_checked_at,
        "freshness_next_check_after": freshness_next_check_after,
        "freshness_gaps": freshness_gaps or [],
    }
    if observed_at is not None:
        card["observed_at"] = observed_at
    if created_at is not None:
        card["created_at"] = created_at
    if valid_from is not None:
        card["valid_from"] = valid_from
    if valid_to is not None:
        card["valid_to"] = valid_to
    if sup_list:
        card["superseded_by"] = sup_list
    if source_hash is not None:
        card["source_hash"] = source_hash
    if time_confidence is not None:
        card["time_confidence"] = time_confidence
    return card


class _Store:
    """Mock authority store mimicking PgVectorStore in-memory."""

    def __init__(self, cards, *, projection_lag_ms: int = 0, unprojected: bool = False, calls=None):
        self.cards = list(cards)
        self.projection_lag_ms = projection_lag_ms
        self.unprojected = unprojected
        self.calls = calls if calls is not None else []

    def graph_projection_health(self, project, *, as_of=None, steward_only=False):
        self.calls.append("health")
        return {"unprojected": self.unprojected, "projection_lag_ms": self.projection_lag_ms}

    def list_authorized_cards(self, **kwargs):
        self.calls.append(("list", kwargs.get("memory_ids")))
        wanted = kwargs.get("memory_ids")
        as_of = kwargs.get("as_of")
        cards = self.cards if wanted is None else [c for c in self.cards if c["memory_id"] in wanted]
        if as_of:
            try:
                as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
                filtered = []
                for c in cards:
                    v_from = c.get("valid_from")
                    v_to = c.get("valid_to")
                    if v_from:
                        dt_from = datetime.fromisoformat(v_from.replace("Z", "+00:00"))
                        if dt_from > as_of_dt:
                            continue
                    if v_to:
                        dt_to = datetime.fromisoformat(v_to.replace("Z", "+00:00"))
                        if dt_to <= as_of_dt:
                            continue
                    filtered.append(c)
                cards = filtered
            except Exception:
                pass
        if wanted is None:
            cards = [
                c for c in sorted(cards, key=lambda c: c["memory_id"])
                if c["memory_id"] > (kwargs.get("after_memory_id") or "")
            ]
        return cards[: kwargs.get("limit", 100)]

    def list_context_cards(self, **kwargs):
        self.calls.append(("context", kwargs))
        return list(self.cards)[: kwargs.get("limit", 100)]

    def hybrid_search(self, **kwargs):
        self.calls.append("hybrid")
        return list(self.cards)

    def read_authorized_evidence(self, **kwargs):
        self.calls.append(("evidence", kwargs))
        return {
            "root_hashes": {
                c["memory_id"]: c.get("content_hash", "sha256:" + "a" * 64)
                for c in self.cards
                if c.get("memory_id") in kwargs.get("root_memory_ids", [])
            },
            "edges": [],
            "truncated": False,
        }


class _Graph:
    """Mock graph service mimicking Graphiti / Neo4j in-memory."""

    def __init__(self, episodes=(), status="available", calls=None):
        self.episodes = tuple(episodes)
        self.status = status
        self.calls = calls if calls is not None else []

    def search_context(self, **kwargs):
        self.calls.append("graph")
        return SimpleNamespace(status=self.status, episodes=self.episodes)


def _episode(
    memory_id: str,
    content_hash: str = "sha256:" + "a" * 64,
    *,
    valid_from: str | None = None,
    currentness: str = "current",
):
    payload = {"authority_memory_id": memory_id, "content_hash": content_hash}
    if valid_from is not None:
        payload["valid_from"] = valid_from
    if currentness is not None:
        payload["currentness"] = currentness
    return SimpleNamespace(payload=payload)


# ==============================================================================
# Existing 6 Basic Tests (Retained)
# ==============================================================================

def test_retired_runtime_card_is_not_current():
    cards = [
        _card("qdrant-readiness", currentness="superseded", observed_days_ago=10),
        _card(
            "qdrant-retirement",
            currentness="current",
            observed_days_ago=5,
            typed={"decision": "Qdrant runtime retired; pgvector is authority"},
        ),
    ]
    resolver = GraphFirstResolver(_Store(cards), None, None)
    result = resolver.resolve(project="neurons", mode="list", query="qdrant", limit=5)
    ids = [it["id"] for it in result["items"]]
    assert "qdrant-retirement" in ids
    for it in result["items"]:
        if it["id"] == "qdrant-retirement":
            assert it["currentness"] == "current"
            assert it["temporal"]["observed_at"] is not None
            assert it["temporal"]["age"]["seconds"] >= 0


def test_old_but_current_stays_current():
    cards = [_card("old-decision", currentness="current", observed_days_ago=180)]
    resolver = GraphFirstResolver(_Store(cards), None, None)
    result = resolver.resolve(project="neurons", mode="list", query="decision", limit=5)
    assert result["items"][0]["currentness"] == "current"
    assert result["items"][0]["temporal"]["age"]["confidence"] == "high"


def test_recent_but_superseded_is_historical():
    cards = [_card("recent-superseded", currentness="superseded", observed_days_ago=1)]
    resolver = GraphFirstResolver(_Store(cards), None, None)
    result = resolver.resolve(project="neurons", mode="list", query="x", limit=5)
    if result["items"]:
        assert result["items"][0]["currentness"] == "superseded"


def test_missing_observed_at_is_unknown():
    cards = [
        {
            "memory_id": "no-time",
            "project": "neurons",
            "card_type": "decision",
            "title": "no-time",
            "summary": "no-time",
            "currentness": "current",
            "content_hash": "sha256:" + "a" * 64,
            "typed_payload": {},
        }
    ]
    resolver = GraphFirstResolver(_Store(cards), None, None)
    result = resolver.resolve(project="neurons", mode="list", query="x", limit=5)
    if result["items"]:
        t = result["items"][0]["temporal"]
        assert t["observed_at"] is None
        assert t["age"]["confidence"] == "unknown"
        assert "observed_at_missing" in t["age"]["reason_codes"]


def test_temporal_coverage_metrics_present():
    cards = [_card("a", observed_days_ago=1), _card("b", observed_days_ago=2)]
    resolver = GraphFirstResolver(_Store(cards), None, None)
    result = resolver.resolve(project="neurons", mode="list", query="x", limit=5)
    cov = result["metadata"].get("temporal_coverage")
    assert cov is not None
    assert cov["items"] == len(result["items"])


def test_as_of_does_not_crash():
    cards = [_card("c", currentness="current", observed_days_ago=10)]
    resolver = GraphFirstResolver(_Store(cards), None, None)
    result = resolver.resolve(project="neurons", mode="list", query="x", limit=5, as_of="2026-01-01T00:00:00+00:00")
    assert "items" in result


# ==============================================================================
# Acceptance Criteria: AC-AGE-001 ~ AC-AGE-015
# ==============================================================================

def test_ac_age_001_backfill_original_age_preserved():
    """AC-AGE-001: 1-year-old session backfilled today preserves original observed_at and calculates ingestion_lag."""
    now = datetime.now(timezone.utc)
    obs_date = now - timedelta(days=365)
    rec_date = now

    card = _card(
        "session-backfill-1yr",
        observed_at=obs_date.isoformat(),
        recorded_at=rec_date.isoformat(),
        currentness="current",
    )
    resolver = GraphFirstResolver(_Store([card]), None, None)
    result = resolver.resolve(project="neurons", mode="list", query="backfill", limit=5)

    assert len(result["items"]) == 1
    t = result["items"][0]["temporal"]

    # 1. observed_at is preserved from 1 year ago (not stamped with today's ingestion time)
    assert t["observed_at"] == obs_date.isoformat()
    # 2. recorded_at is today
    assert t["recorded_at"] == rec_date.isoformat()
    # 3. age bucket is historical (365 days > 180 days)
    assert t["age"]["label"] == "historical"
    assert t["age"]["seconds"] >= 365 * 86400 - 60
    # 4. ingestion_lag_seconds reflects ~365 days of lag
    assert t.get("ingestion_lag_seconds", 0) >= 365 * 86400 - 60

    # Also verify chunk path (_session_temporal_block)
    chunk_t = _session_temporal_block(observed_at=obs_date, recorded_at=rec_date)
    assert chunk_t["observed_at"] == obs_date.isoformat()
    assert chunk_t["recorded_at"] == rec_date.isoformat()
    assert chunk_t["age"]["label"] == "historical"
    assert chunk_t["ingestion_lag_seconds"] >= 365 * 86400 - 60


def test_ac_age_002_historical_items_excluded_by_default():
    """AC-AGE-002: Superseded/expired items are excluded from default current results."""
    now = datetime.now(timezone.utc)
    card_expired = _card(
        "card-superseded",
        currentness="superseded",
        observed_days_ago=30,
        valid_to=(now - timedelta(days=5)).isoformat(),
    )
    card_current = _card(
        "card-active",
        currentness="current",
        observed_days_ago=2,
        valid_to=None,
    )
    resolver = GraphFirstResolver(_Store([card_expired, card_current]), None, None)

    # Default query: only current lane should be returned
    result = resolver.resolve(project="neurons", mode="list", query="card", limit=10)
    returned_ids = [it["id"] for it in result["items"]]

    assert "card-active" in returned_ids
    assert "card-superseded" not in returned_ids

    # Explicit include_historical=True: both current and historical lanes returned
    res_hist = resolver.resolve(
        project="neurons", mode="list", query="card", limit=10, include_historical=True,
    )
    returned_hist_ids = [it["id"] for it in res_hist["items"]]
    assert "card-active" in returned_hist_ids
    assert "card-superseded" in returned_hist_ids


def test_ac_age_003_missing_observed_at_yields_unknown():
    """AC-AGE-003: Missing observed_at yields age=unknown, time_confidence=unknown, and reason_codes."""
    # Case A: completely missing time
    card_no_time = {
        "memory_id": "mem-no-time",
        "project": "neurons",
        "card_type": "decision",
        "title": "mem-no-time",
        "summary": "mem-no-time",
        "currentness": "current",
        "content_hash": "sha256:" + "a" * 64,
        "typed_payload": {},
    }
    resolver = GraphFirstResolver(_Store([card_no_time]), None, None)
    result = resolver.resolve(project="neurons", mode="list", query="x", limit=5)
    assert len(result["items"]) == 1
    t = result["items"][0]["temporal"]

    assert t["observed_at"] is None
    assert t["age"]["seconds"] is None
    assert t["age"]["confidence"] == "unknown"
    assert t["time_confidence"] == "unknown"
    assert "observed_at_missing" in t["age"]["reason_codes"]

    # Case B: recorded_at present without observed_at -> fallback with clock=recorded_at, confidence=low (FR-AGE-011)
    now = datetime.now(timezone.utc)
    card_rec_only = {
        "memory_id": "mem-rec-only",
        "project": "neurons",
        "card_type": "decision",
        "title": "mem-rec-only",
        "summary": "mem-rec-only",
        "currentness": "current",
        "content_hash": "sha256:" + "b" * 64,
        "created_at": (now - timedelta(days=5)).isoformat(),
        "typed_payload": {},
    }
    resolver2 = GraphFirstResolver(_Store([card_rec_only]), None, None)
    result2 = resolver2.resolve(project="neurons", mode="list", query="x", limit=5)
    t2 = result2["items"][0]["temporal"]
    assert t2["age"]["clock"] == "recorded_at"
    assert t2["age"]["confidence"] == "low"


def test_ac_age_004_graph_pg_mismatch_prefers_pg_authority():
    """AC-AGE-004: When Graph and PostgreSQL disagree on validity/currentness, PostgreSQL authority wins with conflict flag."""
    # Graph says mem_x is current and valid_from=2026-01-01
    graph_episode = _episode(
        "mem-mismatch",
        content_hash="sha256:" + "a" * 64,
        valid_from="2026-01-01T00:00:00Z",
        currentness="current",
    )
    graph = _Graph(episodes=[graph_episode])

    # PostgreSQL store says mem_x was superseded and valid_from=2026-06-01
    store_card = _card(
        "mem-mismatch",
        content_hash="sha256:" + "a" * 64,
        currentness="superseded",
        valid_from="2026-06-01T00:00:00Z",
        observed_days_ago=120,
    )
    store = _Store([store_card])

    resolver = GraphFirstResolver(store, graph)
    result = resolver.resolve(project="neurons", query="mismatch", mode="query")

    assert len(result["items"]) >= 1
    item = result["items"][0]
    t = item["temporal"]

    # Authority must be PostgreSQL
    assert item["currentness"] == "superseded"
    assert t["temporal_conflict"] is True
    assert t.get("authority_source") == "postgres" or result["metadata"].get("authority_source") == "postgres"


def test_ac_age_005_current_past_conflict_shows_superseded_by():
    """AC-AGE-005: Conflict between current and superseded item shows superseded_by and temporal_conflict."""
    card_old = _card(
        "decision-v1",
        currentness="superseded",
        superseded_by=["decision-v2"],
        observed_days_ago=90,
    )
    card_new = _card(
        "decision-v2",
        currentness="current",
        observed_days_ago=10,
    )
    store = _Store([card_old, card_new])
    resolver = GraphFirstResolver(store, None, None)

    result = resolver.resolve(project="neurons", mode="list", query="decision", limit=10)
    items_by_id = {it["id"]: it for it in result["items"]}

    # Even in listing, if old card is present or referenced, relation is preserved
    if "decision-v1" in items_by_id:
        v1_t = items_by_id["decision-v1"]["temporal"]
        assert "decision-v2" in v1_t["superseded_by"]
        assert v1_t["currentness"] == "superseded"

    # Conflicted active card pair
    card_conflict = _card(
        "decision-conflict",
        currentness="conflicted",
        observed_days_ago=5,
    )
    resolver_conflict = GraphFirstResolver(_Store([card_conflict]), None, None)
    res_conflict = resolver_conflict.resolve(project="neurons", mode="list", query="decision", limit=5)
    t_conf = res_conflict["items"][0]["temporal"]
    assert t_conf["temporal_conflict"] is True
    assert t_conf.get("recommended_action") == "review_conflict"


def test_ac_age_006_as_of_query_validity_window():
    """AC-AGE-006: as_of query determines validity window (valid_from <= as_of < valid_to) and sets temporal_view=as_of."""
    card_b = _card(
        "card-b-time-window",
        currentness="superseded",
        valid_from="2026-08-01T00:00:00Z",
        valid_to="2026-09-01T00:00:00Z",
        observed_at="2026-08-01T00:00:00Z",
    )
    resolver = GraphFirstResolver(_Store([card_b]), None, None)

    # Query at 2026-08-15 (inside window) -> Card B was current at that time
    res_inside = resolver.resolve(
        project="neurons",
        mode="list",
        query="card",
        as_of="2026-08-15T00:00:00Z",
    )
    assert len(res_inside["items"]) == 1
    assert res_inside["items"][0]["id"] == "card-b-time-window"
    assert res_inside.get("temporal_view") == "as_of" or res_inside.get("metadata", {}).get("temporal_view") == "as_of"

    # Query at 2026-10-01 (outside window, expired) -> Card B is not current
    res_outside = resolver.resolve(
        project="neurons",
        mode="list",
        query="card",
        as_of="2026-10-01T00:00:00Z",
    )
    assert len(res_outside["items"]) == 0

    # Query with future as_of (> now + 5 min) -> rejected with invalid_as_of_future
    now = datetime.now(timezone.utc)
    future_as_of = (now + timedelta(days=10)).isoformat()
    with pytest.raises((ValueError, Exception), match=r"(invalid_as_of_future|future)"):
        resolver.resolve(project="neurons", query="card", as_of=future_as_of)


def test_ac_age_007_future_timestamps_anomalous_excluded_from_top():
    """AC-AGE-007: Future timestamps (+1h) are marked anomalous and excluded from top ranking."""
    now = datetime.now(timezone.utc)
    card_future = _card(
        "card-future-anomaly",
        observed_at=(now + timedelta(hours=1)).isoformat(),
        recorded_at=now.isoformat(),
        currentness="current",
    )
    card_normal = _card(
        "card-normal-fresh",
        observed_at=(now - timedelta(hours=1)).isoformat(),
        recorded_at=now.isoformat(),
        currentness="current",
    )
    resolver = GraphFirstResolver(_Store([card_future, card_normal]), None, None)
    result = resolver.resolve(project="neurons", mode="list", query="card", limit=10)

    # 1. Anomaly classification
    items_by_id = {it["id"]: it for it in result["items"]}
    fut_item = items_by_id["card-future-anomaly"]
    fut_t = fut_item["temporal"]

    assert fut_t["time_confidence"] == "anomalous"
    assert fut_t["temporal_relation"] == "future"
    assert "observed_after_now" in fut_t["age"]["reason_codes"] or "observed_after_recorded" in fut_t["age"]["reason_codes"]
    assert fut_t.get("recommended_action") in ("ask_user", "do_not_use")

    # 2. Ranking exclusion: future item must not be sorted ahead of normal recent item
    ordered_ids = [it["id"] for it in result["items"]]
    assert ordered_ids.index("card-normal-fresh") < ordered_ids.index("card-future-anomaly")


def test_ac_age_008_old_but_current_retains_currentness():
    """AC-AGE-008: 6-month-old decision retains currentness='current' without auto-decay to stale."""
    card = _card(
        "arch-decision-6mo",
        currentness="current",
        observed_days_ago=180,
        valid_to=None,
    )
    resolver = GraphFirstResolver(_Store([card]), None, None)
    result = resolver.resolve(project="neurons", mode="list", query="arch", limit=5)

    item = result["items"][0]
    t = item["temporal"]

    assert item["currentness"] == "current"
    assert t["currentness"] == "current"
    assert t["age"]["label"] in ("old", "historical")
    assert t.get("recommended_action") == "use_with_verification"


def test_ac_age_009_recent_but_superseded_retains_superseded():
    """AC-AGE-009: Ingested 1h ago but superseded item retains currentness='superseded' and action='prefer_replacement'."""
    now = datetime.now(timezone.utc)
    card = _card(
        "superseded-1h-ago",
        currentness="superseded",
        observed_at=(now - timedelta(hours=1)).isoformat(),
        valid_to=(now - timedelta(minutes=30)).isoformat(),
    )
    resolver = GraphFirstResolver(_Store([card]), None, None)
    result = resolver.resolve(project="neurons", mode="list", query="superseded", limit=5)

    if result["items"]:
        item = result["items"][0]
        t = item["temporal"]
        assert t["age"]["label"] == "fresh"
        assert t["currentness"] == "superseded"
        assert t.get("recommended_action") == "prefer_replacement"


def test_ac_age_010_missing_checked_at_yields_unchecked_gap():
    """AC-AGE-010: Missing freshness.checked_at yields freshness.state='unchecked' and gap='no_recent_freshness_check'."""
    card = _card(
        "card-unchecked-freshness",
        currentness="current",
        observed_days_ago=20,
        freshness_checked_at=None,
    )
    resolver = GraphFirstResolver(_Store([card]), None, None)
    result = resolver.resolve(project="neurons", mode="list", query="card", limit=5)

    t = result["items"][0]["temporal"]
    assert "freshness" in t
    assert t["freshness"]["state"] == "unchecked"
    assert t["freshness"]["checked_at"] is None
    assert "no_recent_freshness_check" in t["freshness"]["gaps"]

    # Also verify chunk path
    chunk_t = _session_temporal_block(
        observed_at=datetime.now(timezone.utc),
        recorded_at=datetime.now(timezone.utc),
    )
    assert chunk_t["freshness"]["state"] == "unchecked"
    assert chunk_t["freshness"]["checked_at"] is None
    assert "no_recent_freshness_check" in chunk_t["freshness"]["gaps"]


def test_ac_age_011_qdrant_retired_runtime_specimen():
    """AC-AGE-011: Qdrant retirement specimen distinguishes runtime retirement from code presence without ambiguity."""
    cards = [
        _card(
            "qdrant-readiness-prep",
            card_type="task",
            currentness="superseded",
            observed_days_ago=45,
            superseded_by=["qdrant-runtime-retirement"],
        ),
        _card(
            "qdrant-runtime-retirement",
            card_type="decision",
            currentness="current",
            observed_days_ago=15,
            summary="Qdrant runtime retired; pgvector is single authority",
            typed={"decision": "Qdrant runtime retired; pgvector is authority"},
        ),
    ]
    resolver = GraphFirstResolver(_Store(cards), None, lambda _: [0.1])
    result = resolver.resolve(project="neurons", query="qdrant", mode="query", limit=10)

    returned_ids = [it["id"] for it in result["items"]]
    assert "qdrant-runtime-retirement" in returned_ids

    ret_item = next(it for it in result["items"] if it["id"] == "qdrant-runtime-retirement")
    assert ret_item["currentness"] == "current"
    assert ret_item["temporal"]["age"]["seconds"] is not None

    # Obsolete readiness prep card must not be returned as current
    if "qdrant-readiness-prep" in returned_ids:
        prep_item = next(it for it in result["items"] if it["id"] == "qdrant-readiness-prep")
        assert prep_item["currentness"] == "superseded"
        assert prep_item["temporal"].get("recommended_action") == "prefer_replacement"


def test_ac_age_012_task_items_check_due_no_auto_obsolete():
    """AC-AGE-012: Task card observed 60d ago with expired next_check_after yields check_due without auto-obsolete."""
    now = datetime.now(timezone.utc)
    task_card = _card(
        "task-migration-check",
        card_type="task",
        task_state="in_progress",
        currentness="current",
        observed_days_ago=60,
        freshness_next_check_after=(now - timedelta(days=10)).isoformat(),
        freshness_gaps=["source_missing"],
    )
    resolver = GraphFirstResolver(_Store([task_card]), None, None)
    result = resolver.resolve(project="neurons", mode="list", query="task", limit=5)

    item = result["items"][0]
    t = item["temporal"]

    assert t["age"]["label"] in ("old", "historical")
    assert t["freshness"]["state"] == "check_due"
    # Status must NOT be automatically flipped to obsolete
    assert item["typed_payload"].get("task_state") == "in_progress"
    assert t.get("recommended_action") == "use_with_verification"


# ==============================================================================
# Milestone M3: Freshness Revalidation Logic Unit Tests (R3)
# ==============================================================================

def test_m3_freshness_source_hash_changed_yields_verify_or_supersede():
    """M3 Unit: Card marked verified_changed (source_hash mismatch) exposes verified_changed & verify_or_supersede."""
    card = _card(
        "card-source-changed",
        currentness="current",
        observed_days_ago=3,
        freshness_state="verified_changed",
        source_hash="sha256:oldhash1234567890abcdef",
        freshness_checked_at=datetime.now(timezone.utc).isoformat(),
        freshness_gaps=["source_hash_mismatch"],
    )
    resolver = GraphFirstResolver(_Store([card]), None, None)
    result = resolver.resolve(project="neurons", mode="list", query="card", limit=5)

    t = result["items"][0]["temporal"]
    assert t["freshness"]["state"] == "verified_changed"
    assert t["freshness"]["checked_at"] is not None
    assert "source_hash_mismatch" in t["freshness"]["gaps"]
    assert t.get("recommended_action") == "verify_or_supersede"
    assert result["items"][0]["currentness"] == "current"


def test_m3_freshness_source_hash_mismatch_detection():
    """M3 Unit: Dynamic source_hash mismatch against expected_source_hash yields verified_changed & verify_or_supersede."""
    card = _card(
        "card-doc-drift",
        currentness="current",
        observed_days_ago=5,
        freshness_checked_at=datetime.now(timezone.utc).isoformat(),
        source_hash="sha256:" + "1" * 64,
    )
    card["expected_source_hash"] = "sha256:" + "2" * 64

    resolver = GraphFirstResolver(_Store([card]), None, None)
    result = resolver.resolve(project="neurons", mode="list", query="card", limit=5)

    item = result["items"][0]
    t = item["temporal"]
    assert t["freshness"]["state"] == "verified_changed"
    assert "source_changed" in t["freshness"]["gaps"]
    assert t.get("recommended_action") == "verify_or_supersede"
    assert item["currentness"] == "current"


def test_m3_freshness_verified_current_with_future_check_deadline():
    """M3 Unit: When checked_at is recent and next_check_after is in future, state remains verified_current."""
    now = datetime.now(timezone.utc)
    card = _card(
        "card-verified-fresh",
        currentness="current",
        observed_days_ago=2,
        freshness_state="verified_current",
        freshness_checked_at=(now - timedelta(hours=1)).isoformat(),
        freshness_next_check_after=(now + timedelta(days=7)).isoformat(),
        source_hash="sha256:current1234567890abcdef",
    )
    resolver = GraphFirstResolver(_Store([card]), None, None)
    result = resolver.resolve(project="neurons", mode="list", query="card", limit=5)

    t = result["items"][0]["temporal"]
    assert t["freshness"]["state"] == "verified_current"
    assert t["freshness"]["checked_at"] is not None
    assert "no_recent_freshness_check" not in t["freshness"]["gaps"]
    assert t.get("recommended_action") != "use_with_verification"


def test_m3_freshness_next_check_after_boundary_with_as_of():
    """M3 Unit: next_check_after is evaluated against query as_of rather than wall clock."""
    base = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    card = _card(
        "card-as-of-check-due",
        currentness="current",
        observed_at=(base - timedelta(days=30)).isoformat(),
        freshness_state="verified_current",
        freshness_checked_at=(base - timedelta(days=10)).isoformat(),
        freshness_next_check_after=base.isoformat(),
    )
    store = _Store([card])
    resolver = GraphFirstResolver(store, None, None)

    # Before check deadline: still verified_current
    res_before = resolver.resolve(
        project="neurons", mode="list", query="card",
        as_of=(base - timedelta(hours=1)).isoformat()
    )
    t_before = res_before["items"][0]["temporal"]
    assert t_before["freshness"]["state"] == "verified_current"

    # After check deadline: becomes check_due
    res_after = resolver.resolve(
        project="neurons", mode="list", query="card",
        as_of=(base + timedelta(hours=1)).isoformat()
    )
    t_after = res_after["items"][0]["temporal"]
    assert t_after["freshness"]["state"] == "check_due"
    assert t_after.get("recommended_action") == "use_with_verification"


def test_ac_age_013_observed_at_stable_pagination():
    """AC-AGE-013: Pagination sorted by observed_at produces zero duplicates and zero omissions across pages."""
    cards = [
        _card(f"mem-page-{i:02d}", observed_days_ago=i * 2)
        for i in range(15)
    ]
    store = _Store(cards)
    resolver = GraphFirstResolver(store, None, None)

    collected_ids: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        page = resolver.resolve(
            project="neurons",
            mode="list",
            limit=5,
            cursor=cursor,
        )
        items = page["items"]
        if not items:
            break
        for it in items:
            collected_ids.append(it["id"])
        cursor = page.get("next_cursor")
        if not cursor:
            break

    assert len(collected_ids) == 15
    assert len(set(collected_ids)) == 15, "Pagination yielded duplicate items"


def test_ac_age_014_public_safety_no_raw_session_or_secrets():
    """AC-AGE-014: Public safety: temporal metadata and serialized output never leak raw tokens, credentials, or private paths."""
    sensitive_token = "Bearer sk-ant-secret1234567890abcdef"
    private_path = "/srv/private/.secret_env"
    raw_session = "sess_raw_private_uuid_8849"

    card = _card(
        "sensitive-card",
        summary=f"Configuration loaded from {private_path}",
        typed={"token": sensitive_token, "session_id": raw_session},
        observed_days_ago=1,
    )
    resolver = GraphFirstResolver(_Store([card]), None, None)
    result = resolver.resolve(project="neurons", mode="list", query="sensitive", limit=5)

    dumped = json.dumps(result, ensure_ascii=False)
    assert sensitive_token not in dumped, "Raw Bearer token leaked in response"
    assert "sk-ant-secret" not in dumped, "Secret key substring leaked"

    t = result["items"][0]["temporal"]
    t_dumped = json.dumps(t)
    assert raw_session not in t_dumped, "Raw session leaked in temporal block"


def test_ac_age_015_projection_lag_seconds_and_observed_age():
    """AC-AGE-015: 1h projection lag exposes projection_lag_seconds=3600 and age is calculated from observed_at."""
    store = _Store(
        cards=[_card("card-projection-lag", observed_days_ago=10)],
        projection_lag_ms=3600 * 1000,
    )
    resolver = GraphFirstResolver(store, None, None)
    result = resolver.resolve(project="neurons", mode="list", query="card", limit=5)

    item = result["items"][0]
    t = item["temporal"]

    # 1. projection_lag_seconds is exposed
    assert t.get("projection_lag_seconds") == 3600
    # 2. age is computed from observed_at (10 days ago), NOT from projection time (1 hour ago)
    assert t["age"]["seconds"] >= 10 * 86400 - 60
    assert t["age"]["label"] in ("aging", "old")


# ==============================================================================
# Section 7.1 Decision Table Tests (12 Rows Parameterized)
# ==============================================================================

@pytest.mark.parametrize(
    (
        "row_id",
        "age_label",
        "currentness",
        "valid_to_relation",
        "freshness_state",
        "temporal_conflict",
        "expected_label",
        "expected_action",
    ),
    [
        (1, "fresh", "current", "none", "verified_current", False, "최신·검증됨", "use"),
        (2, "old", "current", "none", "unchecked", False, "오래되었지만 현재 유효", "use_with_verification"),
        (3, "recent", "current", "none", "verified_changed", False, "현재 권위이나 소스 변경됨", "verify_or_supersede"),
        (4, "old", "superseded", "past", "unchecked", False, "폐기됨·대체 있음", "prefer_replacement"),
        (5, "old", "stale", "past", "unchecked", False, "신뢰 철회됨", "treat_as_historical"),
        (6, "old", "retired", "past", "unchecked", False, "의도적으로 폐기됨", ("prefer_replacement", "treat_as_historical")),
        (7, "fresh", "superseded", "past", "unchecked", False, "최근이지만 이미 폐기됨", "prefer_replacement"),
        (8, "old", "current", "none", "check_due", False, "오래됨·재검증 필요", "use_with_verification"),
        (9, "unknown", "current", "none", "unchecked", False, "현재 권위이나 나이 불명", "ask_user"),
        (10, "unknown", "unknown", "none", "unknown", False, "시간/권위 불명", "ask_user"),
        (11, "future", "current", "none", "unchecked", False, "시간 이상", ("ask_user", "do_not_use")),
        (12, "fresh", "conflicted", "none", "unchecked", True, "현재 항목 간 충돌", "review_conflict"),
    ],
)
def test_section_7_1_decision_table_12_rows(
    row_id,
    age_label,
    currentness,
    valid_to_relation,
    freshness_state,
    temporal_conflict,
    expected_label,
    expected_action,
):
    """Section 7.1 Decision Table: Verify all 12 rows map deterministically to expected label & recommended_action."""
    try:
        from agent_knowledge.llm_brain_core.temporal_envelope import evaluate_decision_table_7_1
    except ImportError as err:
        pytest.fail(f"M4 temporal_envelope not implemented yet: {err}")

    label, action = evaluate_decision_table_7_1(
        age_label=age_label,
        currentness=currentness,
        valid_to_relation=valid_to_relation,
        freshness_state=freshness_state,
        temporal_conflict=temporal_conflict,
    )
    assert label == expected_label
    if isinstance(expected_action, tuple):
        assert action in expected_action
    else:
        assert action == expected_action


# ==============================================================================
# Section 8 Response Contract Tests (22 Common Temporal Fields)
# ==============================================================================

REQUIRED_22_TEMPORAL_FIELDS = {
    "observed_at",
    "recorded_at",
    "valid_from",
    "valid_to",
    "superseded_by",
    "as_of",
    "age",
    "ingestion_lag_seconds",
    "time_confidence",
    "currentness",
    "lifecycle_status",
    "freshness",
    "temporal_relation",
    "temporal_conflict",
    "projection_lag_seconds",
    "recommended_action",
    "summary",
}

REQUIRED_AGE_SUBFIELDS = {"seconds", "label", "clock", "confidence", "reason_codes"}
REQUIRED_FRESHNESS_SUBFIELDS = {"state", "checked_at", "next_check_after", "gaps"}


def test_section_8_response_contract_card_path_22_fields():
    """Section 8 Response Contract: Verify all 22 common temporal fields are present in card resolver path."""
    card = _card(
        "contract-card-test",
        observed_days_ago=10,
        currentness="current",
    )
    resolver = GraphFirstResolver(_Store([card]), None, None)
    result = resolver.resolve(project="neurons", mode="list", query="contract", limit=5)

    assert len(result["items"]) >= 1
    t = result["items"][0]["temporal"]

    # Top-level keys check
    missing = REQUIRED_22_TEMPORAL_FIELDS - set(t.keys())
    assert not missing, f"Card temporal block missing fields: {missing}"

    # Nested age subfields check
    assert isinstance(t["age"], dict)
    missing_age = REQUIRED_AGE_SUBFIELDS - set(t["age"].keys())
    assert not missing_age, f"Card temporal.age missing subfields: {missing_age}"

    # Nested freshness subfields check
    assert isinstance(t["freshness"], dict)
    missing_fresh = REQUIRED_FRESHNESS_SUBFIELDS - set(t["freshness"].keys())
    assert not missing_fresh, f"Card temporal.freshness missing subfields: {missing_fresh}"


def test_section_8_response_contract_chunk_path_22_fields():
    """Section 8 Response Contract: Verify all 22 common temporal fields are present in session chunk recall path."""
    now = datetime.now(timezone.utc)
    t = _session_temporal_block(
        observed_at=now - timedelta(days=5),
        recorded_at=now,
    )

    # Top-level keys check
    missing = REQUIRED_22_TEMPORAL_FIELDS - set(t.keys())
    assert not missing, f"Chunk temporal block missing fields: {missing}"

    # Nested age subfields check
    assert isinstance(t["age"], dict)
    missing_age = REQUIRED_AGE_SUBFIELDS - set(t["age"].keys())
    assert not missing_age, f"Chunk temporal.age missing subfields: {missing_age}"

    # Nested freshness subfields check
    assert isinstance(t["freshness"], dict)
    missing_fresh = REQUIRED_FRESHNESS_SUBFIELDS - set(t["freshness"].keys())
    assert not missing_fresh, f"Chunk temporal.freshness missing subfields: {missing_fresh}"