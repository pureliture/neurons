"""Adversarial stress and invariance test harness for Milestone 5 (Historical Lane Separation & Conflict Preservation).

Challenger: challenger_m5_2
Commit: 5d51799

Focus:
1. Historical lane exclusion stress:
   - currentness in ("superseded", "retired", "stale") excluded under include_historical=False
   - expired items (valid_to <= as_of) excluded under include_historical=False
   - boundary checks: valid_to == as_of, timezone offset comparisons, calendar date as_of
2. Inclusion on request stress:
   - include_historical=True returns all historical items alongside current items
   - caller-explicit currentness ("superseded", "retired", "stale") returns historical items even when include_historical=False
   - currentness="current" does not return expired items
3. Old-but-current invariant stress (FR-AGE-015, AC-AGE-008):
   - Items with currentness="current" aged 180d, 365d, 2y, 10y, 100y are NEVER excluded by default query
   - Preservation across card types and freshness states
4. Conflict preservation stress (FR-AGE-028, AC-AGE-004):
   - Items with temporal_conflict=True or currentness="conflicted" are NEVER excluded by default query
   - Even when currentness is superseded/retired/stale or valid_to is expired
   - Graph vs PG authority mismatch preserves conflicted item
   - Cross-card scope/subject conflict and superseded pair conflict
5. Filter interactions and edge cases:
   - age_bucket="historical" vs include_historical=False
   - min_age / max_age ranges
   - Pagination and cursor integrity with filtered sets
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
import pytest

from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver


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
    temporal_conflict: bool = False,
    authority_source: str | None = None,
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
        "freshness_state": freshness_state,
        "freshness_checked_at": freshness_checked_at,
        "freshness_next_check_after": freshness_next_check_after,
        "freshness_gaps": list(freshness_gaps or []),
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
    if authority_source is not None:
        card["authority_source"] = authority_source
    if time_confidence is not None:
        card["time_confidence"] = time_confidence
    return card


class _MockStore:
    def __init__(self, cards: list[dict[str, Any]], *, projection_lag_ms: int = 0):
        self.cards = list(cards)
        self.projection_lag_ms = projection_lag_ms

    def graph_projection_health(self, project: str, *, as_of: str | None = None, steward_only: bool = False):
        return {"unprojected": False, "projection_lag_ms": self.projection_lag_ms}

    def list_authorized_cards(self, **kwargs):
        wanted = kwargs.get("memory_ids")
        cards = self.cards if wanted is None else [c for c in self.cards if c["memory_id"] in wanted]
        after_id = kwargs.get("after_memory_id")
        if after_id:
            cards = [c for c in cards if c["memory_id"] > after_id]
        cards = sorted(cards, key=lambda c: c["memory_id"])
        return cards[: kwargs.get("limit", 100)]

    def list_context_cards(self, **kwargs):
        return self.list_authorized_cards(**kwargs)


# ==============================================================================
# Suite 1: Historical Lane Exclusion Stress
# ==============================================================================

@pytest.mark.parametrize("status", ["superseded", "retired", "stale"])
@pytest.mark.parametrize("mode", ["list", "context", "query"])
def test_historical_status_excluded_by_default(status: str, mode: str):
    """Stress: Any card with currentness in ('superseded', 'retired', 'stale') is excluded by default across all modes."""
    c_hist = _card(f"card-hist-{status}", currentness=status, observed_days_ago=5)
    c_curr = _card("card-current", currentness="current", observed_days_ago=1)

    graph = None
    if mode == "query":
        from types import SimpleNamespace
        episodes = [
            SimpleNamespace(payload={"authority_memory_id": f"card-hist-{status}", "content_hash": "sha256:" + "a" * 64}),
            SimpleNamespace(payload={"authority_memory_id": "card-current", "content_hash": "sha256:" + "a" * 64}),
        ]
        class _MockGraph:
            def search_context(self, **kwargs):
                return SimpleNamespace(status="available", episodes=episodes)
        graph = _MockGraph()

    resolver = GraphFirstResolver(_MockStore([c_hist, c_curr]), graph, None)
    res = resolver.resolve(project="neurons", mode=mode, query="card", limit=10)

    if mode == "context":
        returned_ids = [it["id"] for it in res.get("items", [])]
    else:
        returned_ids = [it["id"] for it in res.get("items", [])]

    assert "card-current" in returned_ids, "Active card was not returned!"
    assert f"card-hist-{status}" not in returned_ids, f"Historical card with status {status} leaked into default query!"


@pytest.mark.parametrize(
    "valid_to_delta,should_exclude",
    [
        (timedelta(seconds=-10), True),    # Expired 10s ago -> excluded
        (timedelta(days=-1), True),        # Expired 1 day ago -> excluded
        (timedelta(days=-365), True),      # Expired 1 year ago -> excluded
        (timedelta(seconds=0), True),      # Exactly at expiration boundary (valid_to <= as_of) -> excluded
        (timedelta(seconds=10), False),    # Valid for 10 more seconds -> included
        (timedelta(days=1), False),        # Valid for 1 more day -> included
        (timedelta(days=365), False),      # Valid for 1 more year -> included
    ],
)
def test_valid_to_boundary_exclusion(valid_to_delta: timedelta, should_exclude: bool):
    """Stress: Expiration check (valid_to <= as_of) strictly excludes cards at or past boundary."""
    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    as_of = now.isoformat()
    valid_to = (now + valid_to_delta).isoformat()

    c = _card("card-validity-test", currentness="current", observed_days_ago=10, valid_to=valid_to)
    resolver = GraphFirstResolver(_MockStore([c]), None, None)
    res = resolver.resolve(project="neurons", mode="list", query="card", as_of=as_of, limit=5)

    returned_ids = [it["id"] for it in res["items"]]
    if should_exclude:
        assert "card-validity-test" not in returned_ids, f"Expired card ({valid_to_delta}) should have been excluded!"
    else:
        assert "card-validity-test" in returned_ids, f"Valid card ({valid_to_delta}) should have been included!"


def test_timezone_aware_expiration_comparison():
    """Stress: valid_to with non-UTC offset (e.g. +09:00 KST) compared against UTC as_of correctly."""
    # 2026-10-24 15:00:00 +09:00 == 2026-10-24 06:00:00 UTC
    kst_tz = timezone(timedelta(hours=9))
    v_to_kst = datetime(2026, 10, 24, 15, 0, 0, tzinfo=kst_tz).isoformat()

    card = _card("card-kst-valid", currentness="current", observed_days_ago=1, valid_to=v_to_kst)
    resolver = GraphFirstResolver(_MockStore([card]), None, None)

    # 1. as_of is 05:59:00 UTC (1 min BEFORE expiration in KST) -> MUST be included
    res_before = resolver.resolve(
        project="neurons", mode="list", query="card", as_of="2026-10-24T05:59:00Z"
    )
    assert "card-kst-valid" in [it["id"] for it in res_before["items"]]

    # 2. as_of is 06:01:00 UTC (1 min AFTER expiration in KST) -> MUST be excluded
    res_after = resolver.resolve(
        project="neurons", mode="list", query="card", as_of="2026-10-24T06:01:00Z"
    )
    assert "card-kst-valid" not in [it["id"] for it in res_after["items"]]


def test_mixed_candidate_pool_exact_lane_partitioning():
    """Stress: A pool of 20 mixed cards: exactly current unexpired cards returned under default query."""
    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    as_of = now.isoformat()

    cards = []
    expected_ids = set()

    for i in range(5):
        cid = f"card-current-valid-{i}"
        cards.append(_card(cid, currentness="current", observed_days_ago=i + 1, valid_to=(now + timedelta(days=10)).isoformat()))
        expected_ids.add(cid)

    for i in range(5):
        cid = f"card-current-expired-{i}"
        cards.append(_card(cid, currentness="current", observed_days_ago=i + 10, valid_to=(now - timedelta(days=i + 1)).isoformat()))

    for i in range(3):
        cid = f"card-superseded-{i}"
        cards.append(_card(cid, currentness="superseded", observed_days_ago=i + 5))

    for i in range(3):
        cid = f"card-retired-{i}"
        cards.append(_card(cid, currentness="retired", observed_days_ago=i + 5))

    for i in range(4):
        cid = f"card-stale-{i}"
        cards.append(_card(cid, currentness="stale", observed_days_ago=i + 5))

    resolver = GraphFirstResolver(_MockStore(cards), None, None)
    res = resolver.resolve(project="neurons", mode="list", query="card", as_of=as_of, limit=20)
    returned_ids = {it["id"] for it in res["items"]}

    assert returned_ids == expected_ids, f"Lane partitioning failed! Diff: {returned_ids.symmetric_difference(expected_ids)}"


# ==============================================================================
# Suite 2: Inclusion on Request Stress
# ==============================================================================

def test_include_historical_true_returns_all_categories():
    """Stress: include_historical=True returns superseded, retired, stale, expired, and current items."""
    now = datetime.now(timezone.utc)
    cards = [
        _card("c-current", currentness="current", observed_days_ago=1),
        _card("c-superseded", currentness="superseded", observed_days_ago=2),
        _card("c-retired", currentness="retired", observed_days_ago=3),
        _card("c-stale", currentness="stale", observed_days_ago=4),
        _card("c-expired", currentness="current", observed_days_ago=5, valid_to=(now - timedelta(days=1)).isoformat()),
    ]

    resolver = GraphFirstResolver(_MockStore(cards), None, None)

    # 1. include_historical=False -> only c-current
    res_default = resolver.resolve(project="neurons", mode="list", query="c", limit=10, include_historical=False)
    ids_default = [it["id"] for it in res_default["items"]]
    assert ids_default == ["c-current"]

    # 2. include_historical=True -> all 5 returned
    res_hist = resolver.resolve(project="neurons", mode="list", query="c", limit=10, include_historical=True)
    ids_hist = set(it["id"] for it in res_hist["items"])
    assert ids_hist == {"c-current", "c-superseded", "c-retired", "c-stale", "c-expired"}


@pytest.mark.parametrize("hist_status", ["superseded", "retired", "stale"])
def test_caller_explicit_historical_status_bypasses_default_exclusion(hist_status: str):
    """Stress: Explicit currentness=X allows historical items of that status through even when include_historical=False."""
    c_target = _card(f"c-{hist_status}", currentness=hist_status, observed_days_ago=5)
    c_other_hist = _card("c-other-hist", currentness="retired" if hist_status != "retired" else "stale", observed_days_ago=5)
    c_current = _card("c-current", currentness="current", observed_days_ago=1)

    resolver = GraphFirstResolver(_MockStore([c_target, c_other_hist, c_current]), None, None)

    # Query with explicit currentness=hist_status, include_historical=False
    res = resolver.resolve(
        project="neurons", mode="list", query="c", currentness=hist_status, include_historical=False, limit=10
    )
    returned_ids = [it["id"] for it in res["items"]]

    assert f"c-{hist_status}" in returned_ids, f"Explicit currentness={hist_status} was wrongly excluded!"
    assert "c-current" not in returned_ids, "Current card should not match explicit filter"
    assert "c-other-hist" not in returned_ids, "Other historical status should not match explicit filter"


def test_caller_explicit_currentness_current_never_returns_expired():
    """Stress: Explicit currentness='current' with include_historical=False does NOT return expired items."""
    now = datetime.now(timezone.utc)
    c_valid = _card("c-valid", currentness="current", observed_days_ago=2, valid_to=None)
    c_expired = _card("c-expired", currentness="current", observed_days_ago=20, valid_to=(now - timedelta(days=2)).isoformat())

    resolver = GraphFirstResolver(_MockStore([c_valid, c_expired]), None, None)
    res = resolver.resolve(
        project="neurons", mode="list", query="c", currentness="current", include_historical=False, limit=10
    )
    returned_ids = [it["id"] for it in res["items"]]

    assert "c-valid" in returned_ids
    assert "c-expired" not in returned_ids, "Expired card leaked when currentness='current' was explicitly requested!"


# ==============================================================================
# Suite 3: Old-But-Current Invariant Stress (FR-AGE-015, AC-AGE-008)
# ==============================================================================

@pytest.mark.parametrize(
    "days_ago",
    [180, 181, 240, 365, 730, 1825, 3650, 36500],
)
@pytest.mark.parametrize("card_type", ["decision", "preference", "status", "task", "work"])
def test_old_but_current_invariant_extreme_ages(days_ago: int, card_type: str):
    """Stress (FR-AGE-015, AC-AGE-008): Cards with currentness='current' aged 6 months to 100 years are NEVER excluded by default query."""
    card = _card(
        f"arch-{card_type}-{days_ago}d",
        card_type=card_type,
        currentness="current",
        observed_days_ago=days_ago,
        valid_to=None,
    )
    resolver = GraphFirstResolver(_MockStore([card]), None, None)

    # 1. List mode
    res_list = resolver.resolve(project="neurons", mode="list", query="arch", limit=5)
    assert len(res_list["items"]) == 1, f"Old-but-current {card_type} ({days_ago}d) was wrongly excluded in list mode!"
    item = res_list["items"][0]
    assert item["currentness"] == "current"
    assert item["temporal"]["currentness"] == "current"
    assert item["temporal"]["age"]["label"] in ("old", "historical")

    # 2. Context mode
    res_ctx = resolver.resolve(project="neurons", mode="context", query="arch", limit=5)
    section_map = {
        "decision": "decisions",
        "preference": "preferences",
        "status": "statuses",
        "task": "tasks",
        "work": "tasks",
    }
    sec_name = section_map[card_type]
    assert len(res_ctx[sec_name]) == 1, f"Old-but-current {card_type} ({days_ago}d) missing in context {sec_name}!"
    assert res_ctx["items"][0]["currentness"] == "current"


@pytest.mark.parametrize("f_state", ["unchecked", "check_due", "verified_current", "verified_changed"])
def test_old_but_current_retained_regardless_of_freshness_state(f_state: str):
    """Stress: Even when freshness is check_due or verified_changed, old-but-current card remains in current lane."""
    card = _card(
        f"card-freshness-{f_state}",
        currentness="current",
        observed_days_ago=200,
        freshness_state=f_state,
        freshness_gaps=["no_recent_freshness_check"] if f_state == "unchecked" else [],
    )
    resolver = GraphFirstResolver(_MockStore([card]), None, None)
    res = resolver.resolve(project="neurons", mode="list", query="card", limit=5)

    assert len(res["items"]) == 1
    assert res["items"][0]["id"] == f"card-freshness-{f_state}"
    assert res["items"][0]["currentness"] == "current"


# ==============================================================================
# Suite 4: Conflict Preservation Stress (FR-AGE-028, AC-AGE-004)
# ==============================================================================

@pytest.mark.parametrize("status", ["superseded", "retired", "stale"])
def test_conflict_preservation_superseded_retired_stale_never_excluded(status: str):
    """Stress (FR-AGE-028): Any card with temporal_conflict=True is NEVER excluded by default query, even if superseded/retired/stale."""
    c_conflicted = _card(
        f"card-conflict-{status}",
        currentness=status,
        observed_days_ago=10,
        temporal_conflict=True,
    )
    resolver = GraphFirstResolver(_MockStore([c_conflicted]), None, None)
    res = resolver.resolve(project="neurons", mode="list", query="card", limit=5)

    returned_ids = [it["id"] for it in res["items"]]
    assert f"card-conflict-{status}" in returned_ids, f"Conflicted {status} card was wrongly excluded by default query!"
    item = res["items"][0]
    assert item["temporal"]["temporal_conflict"] is True


def test_conflict_preservation_expired_item_never_excluded():
    """Stress (FR-AGE-028): Expired card with temporal_conflict=True is NEVER excluded by default query."""
    now = datetime.now(timezone.utc)
    c_expired_conflicted = _card(
        "card-expired-conflict",
        currentness="current",
        observed_days_ago=30,
        valid_to=(now - timedelta(days=10)).isoformat(),
        temporal_conflict=True,
    )
    resolver = GraphFirstResolver(_MockStore([c_expired_conflicted]), None, None)
    res = resolver.resolve(project="neurons", mode="list", query="card", limit=5)

    returned_ids = [it["id"] for it in res["items"]]
    assert "card-expired-conflict" in returned_ids, "Expired conflicted card was wrongly excluded!"
    assert res["items"][0]["temporal"]["temporal_conflict"] is True


def test_conflict_preservation_currentness_conflicted():
    """Stress: Card with currentness='conflicted' is never excluded by default query."""
    card = _card("card-explicit-conflicted", currentness="conflicted", observed_days_ago=15)
    resolver = GraphFirstResolver(_MockStore([card]), None, None)
    res = resolver.resolve(project="neurons", mode="list", query="card", limit=5)

    returned_ids = [it["id"] for it in res["items"]]
    assert "card-explicit-conflicted" in returned_ids
    assert res["items"][0]["temporal"]["temporal_conflict"] is True
    assert res["items"][0]["temporal"]["recommended_action"] == "review_conflict"


def test_conflict_preservation_cross_card_conflict_detected_and_preserved():
    """Stress: Two cards with same subject key detected as conflicting; neither is excluded."""
    c1 = _card(
        "card-a-conflict",
        currentness="current",
        observed_days_ago=2,
        typed={"scope": "auth_token_format", "detail": "JWT format"},
    )
    c2 = _card(
        "card-b-conflict",
        currentness="current",
        observed_days_ago=1,
        typed={"scope": "auth_token_format", "detail": "Opaque bearer token"},
    )
    resolver = GraphFirstResolver(_MockStore([c1, c2]), None, None)
    res = resolver.resolve(project="neurons", mode="list", query="card", limit=10)

    returned_ids = [it["id"] for it in res["items"]]
    assert "card-a-conflict" in returned_ids
    assert "card-b-conflict" in returned_ids
    assert all(it["temporal"]["temporal_conflict"] is True for it in res["items"])


def test_conflict_preservation_superseded_pair_both_preserved():
    """Stress: When card A supersedes card B and both are active, both get temporal_conflict=True and both are returned."""
    c_a = _card(
        "card-a",
        currentness="current",
        observed_days_ago=2,
        superseded_by=["card-b"],
    )
    c_b = _card(
        "card-b",
        currentness="current",
        observed_days_ago=1,
    )
    resolver = GraphFirstResolver(_MockStore([c_a, c_b]), None, None)
    res = resolver.resolve(project="neurons", mode="list", query="card", limit=10)

    returned_ids = [it["id"] for it in res["items"]]
    assert "card-a" in returned_ids
    assert "card-b" in returned_ids
    assert all(it["temporal"]["temporal_conflict"] is True for it in res["items"])


# ==============================================================================
# Suite 5: Filter Interactions & Combinatorial Stress
# ==============================================================================

def test_age_bucket_historical_does_not_conflate_with_historical_lane():
    """Stress: age_bucket='historical' filters by chronological age (>180d), NOT historical lane exclusion."""
    now = datetime.now(timezone.utc)
    c_old_current = _card(
        "c-old-current",
        currentness="current",
        observed_days_ago=200,  # age_bucket = "historical"
        valid_to=None,
    )
    c_fresh_current = _card(
        "c-fresh-current",
        currentness="current",
        observed_days_ago=0.5,  # age_bucket = "fresh"
        valid_to=None,
    )
    c_old_superseded = _card(
        "c-old-superseded",
        currentness="superseded",
        observed_days_ago=200,  # age_bucket = "historical", but in historical lane
    )

    resolver = GraphFirstResolver(_MockStore([c_old_current, c_fresh_current, c_old_superseded]), None, None)

    # 1. Query age_bucket="historical" with include_historical=False (default)
    # MUST return c-old-current, but EXCLUDE c-old-superseded and c-fresh-current!
    res_default = resolver.resolve(
        project="neurons", mode="list", query="c", age_bucket="historical", include_historical=False, limit=10
    )
    ids_default = [it["id"] for it in res_default["items"]]
    assert ids_default == ["c-old-current"], f"Expected only c-old-current, got: {ids_default}"

    # 2. Query age_bucket="historical" with include_historical=True
    # Returns both c-old-current and c-old-superseded
    res_hist = resolver.resolve(
        project="neurons", mode="list", query="c", age_bucket="historical", include_historical=True, limit=10
    )
    ids_hist = set(it["id"] for it in res_hist["items"])
    assert ids_hist == {"c-old-current", "c-old-superseded"}


def test_pagination_consistency_all_current_cards():
    """Stress: Keyset pagination operates cleanly over current candidate set without drift or duplicate."""
    cards = [
        _card(f"card-{i:02d}", currentness="current", observed_days_ago=i + 1)
        for i in range(15)
    ]
    resolver = GraphFirstResolver(_MockStore(cards), None, None)

    collected_ids = []
    cursor = None
    for _ in range(10):
        res = resolver.resolve(
            project="neurons", mode="list", query="card", limit=4, cursor=cursor, include_historical=False
        )
        items = res["items"]
        if not items:
            break
        for it in items:
            collected_ids.append(it["id"])
        cursor = res.get("next_cursor")
        if not cursor:
            break

    assert len(collected_ids) == 15
    assert len(set(collected_ids)) == 15, "Duplicates or missing items in pagination!"


def test_pagination_consistency_with_include_historical_all_cards():
    """Stress: Keyset pagination with include_historical=True returns all 30 cards across multiple pages."""
    cards = [
        _card(
            f"card-{i:02d}",
            currentness="current" if i % 2 == 0 else "superseded",
            observed_days_ago=i + 1,
        )
        for i in range(30)
    ]
    resolver = GraphFirstResolver(_MockStore(cards), None, None)

    collected_ids = []
    cursor = None
    for _ in range(15):
        res = resolver.resolve(
            project="neurons", mode="list", query="card", limit=4, cursor=cursor, include_historical=True
        )
        items = res["items"]
        if not items:
            break
        for it in items:
            collected_ids.append(it["id"])
        cursor = res.get("next_cursor")
        if not cursor:
            break

    assert len(collected_ids) == 30
    assert len(set(collected_ids)) == 30, "Duplicates or missing items in historical pagination!"
