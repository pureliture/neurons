"""Adversarial stress and invariance test harness for Milestone 3 (R3 Freshness Revalidation).

Challenger: challenger_m3_2
Focus:
1. Task card invariance stress:
   - 60d, 365d, 10y, 100y aged task cards, next_check_after expiration.
   - Verification that task_state="in_progress" is NEVER mutated or flipped to "obsolete".
   - Verification across card_type variants: task, work, work_unit, and typed_payload task_state.
2. source_hash extreme edge cases:
   - None, empty string, whitespace, corrupted hashes, matching, mismatching, fallbacks.
   - State and recommended_action transitions.
3. High volume combinatorial fuzzing (limit <= 20 per request).
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
    card_type: str = "task",
    title: str | None = None,
    summary: str | None = None,
    content_hash: str | None = None,
    typed: dict | None = None,
    freshness_state: str = "unchecked",
    freshness_checked_at: str | None = None,
    freshness_next_check_after: str | None = None,
    freshness_gaps: list[str] | None = None,
    source_hash: str | None = None,
    expected_source_hash: str | None = None,
    latest_source_hash: str | None = None,
    task_state: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    if observed_at is None and observed_days_ago is not None:
        observed_at = (now - timedelta(days=observed_days_ago)).isoformat()
    if created_at is None and recorded_at is not None:
        created_at = recorded_at
    if created_at is None and observed_at is not None:
        created_at = observed_at

    payload = dict(typed or {})
    if task_state is not None:
        payload["task_state"] = task_state

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
    if source_hash is not None:
        card["source_hash"] = source_hash
    if expected_source_hash is not None:
        card["expected_source_hash"] = expected_source_hash
    if latest_source_hash is not None:
        card["latest_source_hash"] = latest_source_hash
    return card


class _MockStore:
    def __init__(self, cards):
        self.cards = list(cards)

    def graph_projection_health(self, project, *, as_of=None, steward_only=False):
        return {"unprojected": False, "projection_lag_ms": 0}

    def list_authorized_cards(self, **kwargs):
        wanted = kwargs.get("memory_ids")
        cards = self.cards if wanted is None else [c for c in self.cards if c["memory_id"] in wanted]
        return cards[: kwargs.get("limit", 100)]

    def list_context_cards(self, **kwargs):
        return self.list_authorized_cards(**kwargs)


# ==============================================================================
# Suite 1: Task Card Invariance Under Extreme Aging and Expiration
# ==============================================================================

@pytest.mark.parametrize(
    "days_ago",
    [60, 61, 90, 180, 365, 730, 3650, 36500],
)
@pytest.mark.parametrize("card_type", ["task", "work", "work_unit"])
def test_task_invariance_extreme_ages(days_ago: int, card_type: str):
    """Stress: Task card aged from 60 days to 100 years NEVER flips task_state to obsolete."""
    now = datetime.now(timezone.utc)
    card = _card(
        f"task-age-{days_ago}-{card_type}",
        card_type=card_type,
        task_state="in_progress",
        currentness="current",
        observed_days_ago=days_ago,
        freshness_checked_at=(now - timedelta(days=days_ago)).isoformat(),
        freshness_next_check_after=(now - timedelta(days=days_ago - 7)).isoformat(),
    )

    resolver = GraphFirstResolver(_MockStore([card]), None, None)

    # 1. Test in list mode
    res_list = resolver.resolve(project="neurons", mode="list", query="task", limit=5)
    item_list = res_list["items"][0]

    assert item_list["typed_payload"]["task_state"] == "in_progress", "task_state mutated in typed_payload!"
    assert item_list["typed_payload"]["task_state"] != "obsolete", "task_state flipped to obsolete!"
    assert item_list["currentness"] == "current", "currentness flipped!"
    assert item_list["temporal"]["freshness"]["state"] == "check_due", "freshness state must transition to check_due"
    assert item_list["temporal"]["recommended_action"] == "use_with_verification"

    # Tasks section check
    assert len(res_list["tasks"]) == 1
    assert res_list["tasks"][0]["task_state"] == "in_progress"
    assert res_list["tasks"][0]["task_state"] != "obsolete"

    # 2. Test in context mode
    res_ctx = resolver.resolve(project="neurons", mode="context", query="task", limit=5)
    assert len(res_ctx["tasks"]) == 1
    assert res_ctx["tasks"][0]["task_state"] == "in_progress"


@pytest.mark.parametrize(
    "expired_delta",
    [
        timedelta(seconds=1),
        timedelta(minutes=5),
        timedelta(hours=1),
        timedelta(days=1),
        timedelta(days=30),
        timedelta(days=365),
        timedelta(days=3650),
    ],
)
def test_task_invariance_expired_next_check_after(expired_delta: timedelta):
    """Stress: Various degrees of expired next_check_after preserve task_state="in_progress"."""
    now = datetime.now(timezone.utc)
    card = _card(
        "task-expired-check",
        card_type="task",
        task_state="in_progress",
        currentness="current",
        observed_days_ago=10,  # under 60 days
        freshness_checked_at=(now - expired_delta - timedelta(days=1)).isoformat(),
        freshness_next_check_after=(now - expired_delta).isoformat(),
    )

    resolver = GraphFirstResolver(_MockStore([card]), None, None)
    res = resolver.resolve(project="neurons", mode="list", query="task", limit=5)
    item = res["items"][0]

    assert item["typed_payload"]["task_state"] == "in_progress"
    assert item["currentness"] == "current"
    assert item["temporal"]["freshness"]["state"] == "check_due"
    assert item["temporal"]["recommended_action"] == "use_with_verification"
    assert res["tasks"][0]["task_state"] == "in_progress"


@pytest.mark.parametrize(
    "custom_state",
    ["in_progress", "blocked", "todo", "review", "waiting_external_approval", "open"],
)
def test_task_invariance_arbitrary_active_states(custom_state: str):
    """Stress: Any active task state is 100% preserved in typed_payload and tasks entry even when check_due."""
    now = datetime.now(timezone.utc)
    card = _card(
        f"task-state-{custom_state}",
        card_type="task",
        task_state=custom_state,
        currentness="current",
        observed_days_ago=120,
        freshness_checked_at=(now - timedelta(days=120)).isoformat(),
        freshness_next_check_after=(now - timedelta(days=60)).isoformat(),
        typed={"task": f"Do work for {custom_state}", "task_state": custom_state},
    )
    resolver = GraphFirstResolver(_MockStore([card]), None, None)
    res = resolver.resolve(project="neurons", mode="list", query="task", limit=5)
    item = res["items"][0]

    assert item["typed_payload"]["task_state"] == custom_state
    assert item["typed_payload"]["task_state"] != "obsolete"
    assert item["currentness"] == "current"
    assert item["temporal"]["freshness"]["state"] == "check_due"
    assert res["tasks"][0]["task_state"] == custom_state
    assert res["tasks"][0]["task_state"] != "obsolete"


def test_task_invariance_detected_via_typed_payload_when_card_type_is_custom():
    """Stress: When card_type is non-standard but typed_payload has task_state, 60d protection applies."""
    now = datetime.now(timezone.utc)
    card = _card(
        "task-custom-card-type",
        card_type="workflow_item",
        task_state="in_progress",
        currentness="current",
        observed_days_ago=75,
        freshness_checked_at=(now - timedelta(days=75)).isoformat(),
    )
    resolver = GraphFirstResolver(_MockStore([card]), None, None)
    res = resolver.resolve(project="neurons", mode="list", query="task", limit=5)
    item = res["items"][0]

    assert item["typed_payload"]["task_state"] == "in_progress"
    assert item["currentness"] == "current"
    assert item["temporal"]["freshness"]["state"] == "check_due"
    assert item["temporal"]["recommended_action"] == "use_with_verification"


def test_task_invariance_when_source_hash_changed_takes_precedence():
    """Stress: When 10-year-old task card has source_hash mismatch, verified_changed takes precedence over check_due."""
    now = datetime.now(timezone.utc)
    card = _card(
        "task-10y-source-changed",
        card_type="task",
        task_state="in_progress",
        currentness="current",
        observed_days_ago=3650,
        freshness_checked_at=now.isoformat(),
        freshness_next_check_after=(now - timedelta(days=3600)).isoformat(),
        source_hash="sha256:" + "1" * 64,
        expected_source_hash="sha256:" + "2" * 64,
    )
    resolver = GraphFirstResolver(_MockStore([card]), None, None)
    res = resolver.resolve(project="neurons", mode="list", query="task", limit=5)
    item = res["items"][0]

    # Task state still strictly preserved
    assert item["typed_payload"]["task_state"] == "in_progress"
    assert item["typed_payload"]["task_state"] != "obsolete"
    assert item["currentness"] == "current"

    # Freshness state must be verified_changed, NOT check_due
    assert item["temporal"]["freshness"]["state"] == "verified_changed"
    assert item["temporal"]["recommended_action"] == "verify_or_supersede"
    assert "source_changed" in item["temporal"]["freshness"]["gaps"]


# ==============================================================================
# Suite 2: source_hash Extreme Edge Cases and Transitions
# ==============================================================================

@pytest.mark.parametrize(
    "src_hash,exp_hash,raw_state,checked,exp_state,exp_action,expect_changed_gap",
    [
        # Case 1: Matching hashes -> verified_current
        (
            "sha256:" + "a" * 64,
            "sha256:" + "a" * 64,
            "verified_current",
            True,
            "verified_current",
            None,
            False,
        ),
        # Case 2: Matching hashes with surrounding whitespace -> stripped and matches
        (
            "   sha256:" + "b" * 64 + " \n",
            "\tsha256:" + "b" * 64 + "  ",
            "verified_current",
            True,
            "verified_current",
            None,
            False,
        ),
        # Case 3: Mismatching valid hashes -> verified_changed
        (
            "sha256:" + "a" * 64,
            "sha256:" + "b" * 64,
            "verified_current",
            True,
            "verified_changed",
            "verify_or_supersede",
            True,
        ),
        # Case 4: Corrupted source_hash vs valid expected -> verified_changed
        (
            "corrupt_hash_#@!$%^",
            "sha256:" + "b" * 64,
            "verified_current",
            True,
            "verified_changed",
            "verify_or_supersede",
            True,
        ),
        # Case 5: Valid source_hash vs corrupted expected -> verified_changed
        (
            "sha256:" + "a" * 64,
            "corrupt_expected_&*(",
            "verified_current",
            True,
            "verified_changed",
            "verify_or_supersede",
            True,
        ),
        # Case 6: Integer hash representations (robust to non-string)
        (
            1234567890,
            "1234567890",
            "verified_current",
            True,
            "verified_current",
            None,
            False,
        ),
        (
            1234567890,
            9876543210,
            "verified_current",
            True,
            "verified_changed",
            "verify_or_supersede",
            True,
        ),
        # Case 7: Both None -> no change detection, remains raw_state or unchecked
        (
            None,
            None,
            "verified_current",
            True,
            "verified_current",
            None,
            False,
        ),
        # Case 8: None source_hash, expected provided -> no crash, no mismatch triggered
        (
            None,
            "sha256:" + "a" * 64,
            "verified_current",
            True,
            "verified_current",
            None,
            False,
        ),
        # Case 9: Source hash provided, expected is None -> no crash, no mismatch triggered
        (
            "sha256:" + "a" * 64,
            None,
            "verified_current",
            True,
            "verified_current",
            None,
            False,
        ),
        # Case 10: Missing checked_at with matching hashes -> unchecked (Rule 1)
        (
            "sha256:" + "a" * 64,
            "sha256:" + "a" * 64,
            "verified_current",
            False,
            "unchecked",
            None,
            False,
        ),
        # Case 11: Missing checked_at with MISMATCHING hashes -> verified_changed takes precedence!
        (
            "sha256:" + "a" * 64,
            "sha256:" + "z" * 64,
            "verified_current",
            False,
            "verified_changed",
            "verify_or_supersede",
            True,
        ),
    ],
)
def test_source_hash_extremes_matrix(
    src_hash, exp_hash, raw_state, checked, exp_state, exp_action, expect_changed_gap
):
    """Stress: Exhaustive matrix of source_hash edge cases."""
    now = datetime.now(timezone.utc)
    card = _card(
        "card-hash-test",
        card_type="decision",
        observed_days_ago=2,
        freshness_state=raw_state,
        freshness_checked_at=now.isoformat() if checked else None,
        source_hash=src_hash,
        expected_source_hash=exp_hash,
    )
    resolver = GraphFirstResolver(_MockStore([card]), None, None)
    res = resolver.resolve(project="neurons", mode="list", query="card", limit=5)
    t = res["items"][0]["temporal"]

    assert t["freshness"]["state"] == exp_state
    if exp_action is not None:
        assert t.get("recommended_action") == exp_action
    else:
        assert t.get("recommended_action") != "verify_or_supersede"

    if expect_changed_gap:
        assert "source_changed" in t["freshness"]["gaps"]
    else:
        assert "source_changed" not in t["freshness"]["gaps"]


def test_source_hash_fallback_locations():
    """Verify latest_source_hash and typed_payload fallbacks operate cleanly."""
    now = datetime.now(timezone.utc)

    # 1. Fallback to latest_source_hash on card root
    card1 = _card(
        "card-latest-hash",
        observed_days_ago=1,
        freshness_checked_at=now.isoformat(),
        source_hash="sha256:alpha",
        latest_source_hash="sha256:beta",
    )
    r1 = GraphFirstResolver(_MockStore([card1]), None, None).resolve(project="neurons", mode="list", query="card")
    assert r1["items"][0]["temporal"]["freshness"]["state"] == "verified_changed"

    # 2. Fallback to expected_source_hash inside typed_payload
    card2 = _card(
        "card-typed-hash",
        observed_days_ago=1,
        freshness_checked_at=now.isoformat(),
        source_hash="sha256:gamma",
        typed={"expected_source_hash": "sha256:delta"},
    )
    r2 = GraphFirstResolver(_MockStore([card2]), None, None).resolve(project="neurons", mode="list", query="card")
    assert r2["items"][0]["temporal"]["freshness"]["state"] == "verified_changed"

    # 3. Source hash inside typed_payload with expected_source_hash on root
    card3 = _card(
        "card-typed-src-hash",
        observed_days_ago=1,
        freshness_checked_at=now.isoformat(),
        expected_source_hash="sha256:epsilon",
        typed={"source_hash": "sha256:zeta"},
    )
    r3 = GraphFirstResolver(_MockStore([card3]), None, None).resolve(project="neurons", mode="list", query="card")
    assert r3["items"][0]["temporal"]["freshness"]["state"] == "verified_changed"


def test_source_hash_preexisting_gaps_triggers():
    """Existing gaps ('source_changed' or 'source_hash_mismatch') trigger verified_changed even without hashes."""
    now = datetime.now(timezone.utc)
    card = _card(
        "card-gaps-test",
        observed_days_ago=1,
        freshness_checked_at=now.isoformat(),
        freshness_gaps=["source_hash_mismatch"],
    )
    r = GraphFirstResolver(_MockStore([card]), None, None).resolve(project="neurons", mode="list", query="card")
    t = r["items"][0]["temporal"]
    assert t["freshness"]["state"] == "verified_changed"
    assert t.get("recommended_action") == "verify_or_supersede"


def test_source_hash_empty_and_whitespace_edge_cases():
    """Verify empty string and whitespace handling for source_hash."""
    now = datetime.now(timezone.utc)
    # 1. Empty string source_hash with expected hash: falsy empty string evaluates as None, no mismatch triggered
    c1 = _card("c1", observed_days_ago=1, freshness_state="verified_current", freshness_checked_at=now.isoformat(), source_hash="", expected_source_hash="sha256:abc")
    r1 = GraphFirstResolver(_MockStore([c1]), None, None).resolve(project="neurons", mode="list", query="c")
    assert r1["items"][0]["temporal"]["freshness"]["state"] == "verified_current"
    assert "source_changed" not in r1["items"][0]["temporal"]["freshness"]["gaps"]

    # 2. Whitespace source_hash with expected hash: stripped != expected -> verified_changed
    c2 = _card("c2", observed_days_ago=1, freshness_state="verified_current", freshness_checked_at=now.isoformat(), source_hash="   ", expected_source_hash="sha256:abc")
    r2 = GraphFirstResolver(_MockStore([c2]), None, None).resolve(project="neurons", mode="list", query="c")
    assert r2["items"][0]["temporal"]["freshness"]["state"] == "verified_changed"
    assert "source_changed" in r2["items"][0]["temporal"]["freshness"]["gaps"]

    # 3. Whitespace vs Whitespace: stripped == stripped ("" == "") -> not changed
    c3 = _card("c3", observed_days_ago=1, freshness_state="verified_current", freshness_checked_at=now.isoformat(), source_hash="   ", expected_source_hash="  \t  ")
    r3 = GraphFirstResolver(_MockStore([c3]), None, None).resolve(project="neurons", mode="list", query="c")
    assert r3["items"][0]["temporal"]["freshness"]["state"] == "verified_current"
    assert "source_changed" not in r3["items"][0]["temporal"]["freshness"]["gaps"]


def test_as_of_boundary_precision():
    """Verify precision around exact second of next_check_after with as_of queries."""
    base = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
    card = _card(
        "card-boundary",
        observed_at=(base - timedelta(days=5)).isoformat(),
        freshness_state="verified_current",
        freshness_checked_at=(base - timedelta(days=2)).isoformat(),
        freshness_next_check_after=base.isoformat(),
    )
    store = _MockStore([card])
    resolver = GraphFirstResolver(store, None, None)

    # 1 second before deadline -> verified_current
    res_before = resolver.resolve(
        project="neurons", mode="list", query="card", as_of=(base - timedelta(seconds=1)).isoformat()
    )
    assert res_before["items"][0]["temporal"]["freshness"]["state"] == "verified_current"

    # Exactly at deadline -> check_due (instant >= next_check_dt)
    res_exact = resolver.resolve(
        project="neurons", mode="list", query="card", as_of=base.isoformat()
    )
    assert res_exact["items"][0]["temporal"]["freshness"]["state"] == "check_due"
    assert res_exact["items"][0]["temporal"]["recommended_action"] == "use_with_verification"

    # 1 second after deadline -> check_due
    res_after = resolver.resolve(
        project="neurons", mode="list", query="card", as_of=(base + timedelta(seconds=1)).isoformat()
    )
    assert res_after["items"][0]["temporal"]["freshness"]["state"] == "check_due"

    # Malformed as_of -> strict boundary validation rejects with error
    with pytest.raises(Exception):
        resolver.resolve(
            project="neurons", mode="list", query="card", as_of="not-a-valid-iso-date"
        )



# ==============================================================================
# Suite 3: High-Volume Fuzzing & Stress Invariance
# ==============================================================================

def test_high_volume_card_invariance_fuzzing():
    """Fuzzing stress: 240 heterogeneous memory cards through resolver in valid batches (limit <= 20)."""
    now = datetime.now(timezone.utc)
    cards = []

    card_types = ["task", "work", "work_unit", "decision", "preference", "status"]
    task_states = ["in_progress", "blocked", "todo", "done", "custom_wip"]
    ages = [0, 1, 15, 59, 60, 61, 365, 3650]

    idx = 0
    for ct in card_types:
        for ts in task_states:
            for age in ages:
                idx += 1
                expired = (age >= 60)
                card = _card(
                    f"fuzz-card-{idx}",
                    card_type=ct,
                    task_state=ts if ct in ("task", "work", "work_unit") else None,
                    currentness="current",
                    observed_days_ago=age,
                    freshness_checked_at=(now - timedelta(days=1)).isoformat() if idx % 2 == 0 else None,
                    freshness_next_check_after=(now - timedelta(days=1)).isoformat() if expired else (now + timedelta(days=10)).isoformat(),
                    source_hash=f"sha256:hash_{idx}",
                    expected_source_hash=f"sha256:hash_{idx + 1}" if idx % 5 == 0 else f"sha256:hash_{idx}",
                )
                cards.append(card)

    batch_size = 20
    for offset in range(0, len(cards), batch_size):
        sub_cards = cards[offset : offset + batch_size]
        sub_store = _MockStore(sub_cards)
        res = GraphFirstResolver(sub_store, None, None).resolve(
            project="neurons", mode="list", query="card", limit=min(batch_size, len(sub_cards))
        )

        for item in res["items"]:
            # Invariant 1: No task state is ever flipped to obsolete
            ts = item.get("typed_payload", {}).get("task_state")
            if ts:
                assert ts != "obsolete", f"Card {item['id']} had task_state flipped to obsolete!"
            # Invariant 2: Currentness never flipped to obsolete
            assert item["currentness"] != "obsolete", f"Card {item['id']} had currentness flipped to obsolete!"

            # Invariant 3: Freshness state is one of allowed enum values
            f_state = item["temporal"]["freshness"]["state"]
            assert f_state in {
                "verified_current",
                "verified_changed",
                "unchecked",
                "check_due",
                "unverifiable",
                "unknown",
            }
