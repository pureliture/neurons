from __future__ import annotations

from agent_knowledge.ledger import Ledger
from agent_knowledge.memory_miner import build_memory_card_candidate_from_source_span
from agent_knowledge.session_memory.autopilot_loop import run_autopilot_cycle


PROJECT = "neurons"


def _candidate(**overrides):
    span = {
        "source_ref": {"source_id": "src"},
        "span_ref": {"span_id": "span"},
        "content_hash": "sha256:x",
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


def test_cycle_accepts_clean_and_routes_blocked_to_review(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")

    clean = _candidate()
    blocked = _candidate(
        source_ref={"source_id": "src2"},
        span_ref={"span_id": "span2"},
        content_hash="sha256:y",
    )
    blocked["conflicts"] = [{"memory_id": "other", "reason": "contradicts"}]

    result = run_autopilot_cycle(
        candidates=[clean, blocked], ledger=ledger, refresh_watermark="wm"
    )

    assert [c["memory_id"] for c in result["accepted"]] == [clean["memory_id"]]
    assert [c["memory_id"] for c in result["needs_review"]] == [blocked["memory_id"]]

    stored_current = ledger.list_llm_brain_memory_cards(accepted_only=True, current_only=True)
    assert [c["memory_id"] for c in stored_current] == [clean["memory_id"]]


def test_cycle_projects_accepted_and_superseded_cards_to_mirror(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")

    old_card = run_autopilot_cycle(
        candidates=[_candidate()], ledger=ledger, refresh_watermark="w1"
    )["accepted"][0]

    calls: list[str] = []

    class FakeProjectionClient:
        def upsert_memory_card(self, payload, *, idempotency_key):
            calls.append(payload["memory_id"])
            return {"document_id": "doc_" + payload["memory_id"][:6]}

    new_candidate = _candidate(
        source_ref={"source_id": "src_new"},
        span_ref={"span_id": "span_new"},
        content_hash="sha256:new",
        redacted_summary="Auth now uses OAuth.",
    )
    result = run_autopilot_cycle(
        candidates=[new_candidate],
        ledger=ledger,
        refresh_watermark="w2",
        supersede_detector=lambda c, _l: old_card,
        projection_client=FakeProjectionClient(),
    )

    new_id = result["accepted"][0]["memory_id"]
    # both the new current card and the demoted old card are projected to the mirror
    assert new_id in calls
    assert old_card["memory_id"] in calls
    assert result["projected_count"] == 2


def test_cycle_skips_projection_when_no_client(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    result = run_autopilot_cycle(candidates=[_candidate()], ledger=ledger, refresh_watermark="wm")
    assert result["projected_count"] == 0


def test_cycle_supersedes_when_detector_returns_old_card(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")

    old_card = run_autopilot_cycle(
        candidates=[_candidate()], ledger=ledger, refresh_watermark="w1"
    )["accepted"][0]

    new_candidate = _candidate(
        source_ref={"source_id": "src_new"},
        span_ref={"span_id": "span_new"},
        content_hash="sha256:new",
        redacted_summary="Auth now uses OAuth.",
    )

    def detector(candidate, _ledger):
        return old_card

    result = run_autopilot_cycle(
        candidates=[new_candidate],
        ledger=ledger,
        refresh_watermark="w2",
        supersede_detector=detector,
    )

    new_card = result["accepted"][0]
    assert [c["memory_id"] for c in result["superseded"]] == [old_card["memory_id"]]
    assert ledger.get_llm_brain_memory_card(old_card["memory_id"])["currentness"] == "superseded"

    stored_current = ledger.list_llm_brain_memory_cards(accepted_only=True, current_only=True)
    assert [c["memory_id"] for c in stored_current] == [new_card["memory_id"]]
