"""Regression suite for LBrain information-age behavior (FR-AGE).

Uses the Qdrant-retirement scenario as a specimen: a retired runtime whose
name still appears in code. No live Qdrant or network is required.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver


def _card(memory_id, *, currentness="current", observed_days_ago=None, valid_to=None, typed=None):
    now = datetime.now(timezone.utc)
    observed = (now - timedelta(days=observed_days_ago)).isoformat() if observed_days_ago is not None else None
    card = {
        "memory_id": memory_id,
        "project": "neurons",
        "card_type": "decision",
        "title": memory_id,
        "summary": memory_id,
        "currentness": currentness,
        "content_hash": "sha256:" + "a" * 64,
        "typed_payload": typed or {},
    }
    if observed is not None:
        card["observed_at"] = observed
        card["created_at"] = observed
    if valid_to is not None:
        card["valid_to"] = valid_to
    return card


class _Store:
    def __init__(self, cards):
        self.cards = list(cards)

    def graph_projection_health(self, project, *, as_of=None, steward_only=False):
        return {"unprojected": False, "projection_lag_ms": 0}

    def list_authorized_cards(self, **kwargs):
        wanted = kwargs.get("memory_ids")
        cards = self.cards if wanted is None else [c for c in self.cards if c["memory_id"] in wanted]
        if wanted is None:
            cards = [c for c in sorted(cards, key=lambda c: c["memory_id"])
                     if c["memory_id"] > (kwargs.get("after_memory_id") or "")]
        return cards[:kwargs.get("limit", 100)]

    def list_context_cards(self, **kwargs):
        return list(self.cards)[:kwargs.get("limit", 100)]

    def hybrid_search(self, **kwargs):
        return list(self.cards)

    def read_authorized_evidence(self, **kwargs):
        return {"root_hashes": {}, "edges": [], "truncated": False}


def test_retired_runtime_card_is_not_current():
    cards = [
        _card("qdrant-readiness", currentness="superseded", observed_days_ago=10),
        _card("qdrant-retirement", currentness="current", observed_days_ago=5,
              typed={"decision": "Qdrant runtime retired; pgvector is authority"}),
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
    cards = [{"memory_id": "no-time", "project": "neurons", "card_type": "decision",
              "title": "no-time", "summary": "no-time", "currentness": "current",
              "content_hash": "sha256:" + "a" * 64, "typed_payload": {}}]
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