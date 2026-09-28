from __future__ import annotations

import pytest

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.memory_miner import build_memory_card_candidate_from_source_span
from agent_knowledge.session_memory.autopilot_loop import run_autopilot_cycle


PROJECT = "neurons"


def _candidate(**overrides):
    span = {
        "source_ref": {"source_id": "src"},
        "span_ref": {"span_id": "span"},
        "content_hash": "sha256:2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",
        "brain_id": f"/project/{PROJECT}",
        "card_type": "task",
        "scope": "project",
        "project": PROJECT,
        "provider": "codex",
        "title": "auth approach",
        "redacted_summary": "Auth uses JWT.",
        "typed_payload": {
            "task_state": "active",
            "next_action": "ship login",
            "blocker": None,
            "owner_hint": "codex",
            "status": "active",
        },
        "confidence": 0.92,
        "confidence_basis": "operator-approved",
    }
    span.update(overrides)
    return build_memory_card_candidate_from_source_span(span, refresh_watermark="wm")


def test_cycle_without_pg_store_refuses_before_sqlite_or_projection(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    candidate = _candidate()
    with pytest.raises(ValueError, match="PostgreSQL steward store is required"):
        run_autopilot_cycle(candidates=[candidate], ledger=ledger, refresh_watermark="wm")
    assert ledger.list_llm_brain_memory_cards() == []


class _StewardStore:
    def __init__(self):
        self.cards = {}
        self.decisions = []

    def put_steward_proposal(self, card):
        stored = dict(card)
        self.cards[stored["memory_id"]] = stored
        return stored

    def get_steward_card(self, memory_id):
        return self.cards.get(memory_id)

    def list_steward_cards(self, *, project, accepted_only, limit, current_only=False):
        return [card for card in self.cards.values()
                if card["project"] == project and card.get("approval_state") == "approved"
                and (not current_only or card["currentness"] == "current")][:limit]

    def list_steward_project_counts(self):
        return [("neurons", len(self.cards))]

    def steward_decision(self, *, decision_id, memory_id, content_hash, action, actor,
                         target_memory_id="", transform):
        assert self.cards[memory_id]["content_hash"] == content_hash
        candidate = self.cards[memory_id]
        target = self.cards.get(target_memory_id) if target_memory_id else None
        changed, demoted = transform(candidate, target)
        self.cards[memory_id] = changed
        if demoted is not None:
            self.cards[target_memory_id] = demoted
        self.decisions.append((decision_id, action, memory_id, target_memory_id))
        return {"card": changed, "target": demoted}


def test_cycle_pg_persists_proposals_and_approves_without_sqlite(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    store = _StewardStore()
    clean = _candidate()
    blocked = _candidate(source_ref={"source_id": "s2"}, span_ref={"span_id": "p2"},
                         content_hash="sha256:d4735e3a265e16eee03f59718b9b5d03019c07d8b6c51f90da3a666eec13ab35")
    blocked["conflicts"] = [{"memory_id": "other", "reason": "contradicts"}]
    result = run_autopilot_cycle(candidates=[clean, blocked], ledger=ledger,
                                 refresh_watermark="wm", pgvector_store=store)
    assert store.cards[clean["memory_id"]]["approval_state"] == "approved"
    assert store.cards[blocked["memory_id"]]["lifecycle_state"] == "needs_review"
    assert result["accepted"][0]["memory_id"] == clean["memory_id"]
    assert result["needs_review"][0]["memory_id"] == blocked["memory_id"]
    assert store.decisions[0][1] == "approve"
    assert ledger.list_llm_brain_memory_cards() == []


def test_cycle_pg_supersedes_current_pg_card_without_sqlite(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    store = _StewardStore()
    old = run_autopilot_cycle(candidates=[_candidate()], ledger=ledger,
                              refresh_watermark="old", pgvector_store=store)["accepted"][0]
    new = _candidate(source_ref={"source_id": "new"}, span_ref={"span_id": "new"},
                     content_hash="sha256:11507a0e2f5e69d5dfa40a62a1bd7b6ee57e6bcd85c67c9b8431b36fff21c437")
    result = run_autopilot_cycle(candidates=[new], ledger=ledger, refresh_watermark="new",
                                 pgvector_store=store, supersede_detector=lambda card, pg: old)
    assert result["accepted"][0]["memory_id"] == new["memory_id"]
    assert store.cards[old["memory_id"]]["currentness"] == "superseded"
    assert store.decisions[-1][1] == "supersede"
    assert store.decisions[-1][3] == old["memory_id"]
    assert ledger.list_llm_brain_memory_cards() == []


def test_cycle_pg_rejects_colliding_proposal_before_approval(tmp_path):
    store = _StewardStore()
    candidate = _candidate()
    conflicting = dict(candidate, conflicts=[{"memory_id": "other", "reason": "contradicts"}])
    store.cards[candidate["memory_id"]] = conflicting
    store.put_steward_proposal = lambda card: conflicting
    with pytest.raises(ValueError, match="PostgreSQL steward proposal differs"):
        run_autopilot_cycle(candidates=[candidate], ledger=Ledger(tmp_path / "ledger.sqlite"),
                            pgvector_store=store, refresh_watermark="wm")
    assert store.decisions == []


def test_cycle_pg_rejects_unknown_supersede_target_before_proposal(tmp_path):
    store = _StewardStore()
    candidate = _candidate()
    with pytest.raises(ValueError, match="unknown PostgreSQL supersede target"):
        run_autopilot_cycle(candidates=[candidate], ledger=Ledger(tmp_path / "ledger.sqlite"),
                            pgvector_store=store, refresh_watermark="wm",
                            supersede_detector=lambda card, pg: dict(card, memory_id="not-in-pg"))
    assert store.cards == {}


def test_cycle_pg_projection_fails_before_any_write(tmp_path):
    store = _StewardStore()
    with pytest.raises(ValueError, match="projection is unsupported"):
        run_autopilot_cycle(candidates=[_candidate()], ledger=Ledger(tmp_path / "ledger.sqlite"),
                            refresh_watermark="wm", pgvector_store=store, projection_client=object())
    assert store.cards == {}


def test_cycle_without_pg_store_rejects_mixed_candidates(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    with pytest.raises(ValueError, match="PostgreSQL steward store is required"):
        run_autopilot_cycle(candidates=[_candidate(), _candidate()], ledger=ledger,
                            refresh_watermark="wm")
    assert ledger.list_llm_brain_memory_cards() == []


def test_cycle_projects_accepted_and_superseded_cards_to_mirror(tmp_path):
    # Retired projection path must stay blocked before either store is mutated.
    store = _StewardStore()
    with pytest.raises(ValueError, match="projection is unsupported"):
        run_autopilot_cycle(candidates=[_candidate()], ledger=Ledger(tmp_path / "ledger.sqlite"),
                            pgvector_store=store, refresh_watermark="wm", projection_client=object())
    assert store.cards == {}




def test_cycle_skips_projection_when_no_client(tmp_path):
    store = _StewardStore()
    result = run_autopilot_cycle(candidates=[_candidate()], ledger=Ledger(tmp_path / "ledger.sqlite"),
                                 pgvector_store=store, refresh_watermark="wm")
    assert result["projected_count"] == 0


def test_cycle_supersedes_when_detector_returns_old_card(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    store = _StewardStore()
    old_card = run_autopilot_cycle(candidates=[_candidate()], ledger=ledger,
                                   pgvector_store=store, refresh_watermark="w1")["accepted"][0]
    new_candidate = _candidate(
        source_ref={"source_id": "src_new"},
        span_ref={"span_id": "span_new"},
        content_hash="sha256:11507a0e2f5e69d5dfa40a62a1bd7b6ee57e6bcd85c67c9b8431b36fff21c437",
        redacted_summary="Auth now uses OAuth.",
    )
    result = run_autopilot_cycle(candidates=[new_candidate], ledger=ledger,
                                 pgvector_store=store, refresh_watermark="w2",
                                 supersede_detector=lambda candidate, pg: old_card)
    assert [c["memory_id"] for c in result["superseded"]] == [old_card["memory_id"]]
    assert store.cards[old_card["memory_id"]]["currentness"] == "superseded"
    assert ledger.list_llm_brain_memory_cards() == []
