from __future__ import annotations

from pathlib import Path

from agent_knowledge.ledger import Ledger
from agent_knowledge.memory_miner import build_memory_card_candidate_from_source_span
from agent_knowledge.session_memory.autopilot_loop import run_autopilot_cycle
from agent_knowledge.session_memory.brain_query import run_brain_query_v2
from agent_knowledge.session_memory.brain_read_model import LegacyLedgerBrainReadModel

from golden_grader import grade_recall_against_golden


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


def test_grader_scores_zero_silent_lie_when_superseded_card_is_demoted(tmp_path):
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
    run_autopilot_cycle(
        candidates=[new_candidate],
        ledger=ledger,
        refresh_watermark="w2",
        supersede_detector=lambda c, _l: old_card,
    )

    recall = run_brain_query_v2(
        read_model=LegacyLedgerBrainReadModel(ledger),
        brain_id=f"/project/{PROJECT}",
        query="현재 인증 방식",
        query_intent="current_work",
    )
    golden = [
        {"subject_key": "auth", "expected_lane": "current", "canonical_statement": "Auth now uses OAuth.", "must_not_appear_in": []},
        {"subject_key": "auth-old", "expected_lane": "superseded_conflict", "canonical_statement": "Auth uses JWT.", "must_not_appear_in": ["current", "accepted"]},
    ]

    scorecard = grade_recall_against_golden(recall=recall, golden=golden)
    assert scorecard["false_current_count"] == 0
    assert scorecard["silent_lie_rate"] == 0.0
    assert scorecard["current_lane_recall"] == 1.0


def test_grader_catches_silent_lie_when_superseded_leaks_into_current():
    recall = {
        "current": [{"memory_id": "m1", "summary": "Auth uses JWT."}],
        "accepted": [{"memory_id": "m1", "summary": "Auth uses JWT."}],
        "archive": [],
        "conflicts": [],
    }
    golden = [
        {"subject_key": "auth-old", "expected_lane": "superseded_conflict", "canonical_statement": "Auth uses JWT.", "must_not_appear_in": ["current", "accepted"]},
    ]

    scorecard = grade_recall_against_golden(recall=recall, golden=golden)
    assert scorecard["false_current_count"] > 0
    assert scorecard["silent_lie_rate"] > 0.0


def test_no_product_module_imports_the_golden_grader():
    pkg = Path(__file__).resolve().parents[1] / "lib" / "agent_knowledge"
    offenders = []
    for path in pkg.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "golden_grader" in text or "golden/" in text:
            offenders.append(str(path.relative_to(pkg)))
    assert offenders == [], f"product modules must not couple to the golden: {offenders}"
