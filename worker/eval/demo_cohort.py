"""Demo cohort runner (EVAL LANE) — proves loop + grader end-to-end on the real golden.

NOT a blind grade: candidates here are hand-built to match a couple of golden subjects so
the demote path and the silent-lie metric can be exercised without the (deferred) live
transcript->LLM mining provider. A real grade replaces these hand-built candidates with
blind-mined ones. Run: uv run python eval/demo_cohort.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.memory_miner import build_memory_card_candidate_from_source_span
from agent_knowledge.session_memory.autopilot_loop import run_autopilot_cycle
from agent_knowledge.session_memory.brain_query import run_brain_query_v2
from agent_knowledge.session_memory.brain_read_model import LegacyLedgerBrainReadModel

from golden_grader import grade_recall_against_golden, load_golden

PROJECT = "neurons"


def _candidate(summary: str, sid: str):
    span = {
        "source_ref": {"source_id": f"src_{sid}"},
        "span_ref": {"span_id": f"span_{sid}"},
        "content_hash": f"sha256:{sid}",
        "brain_id": f"/project/{PROJECT}",
        "card_type": "decision",
        "scope": "project",
        "project": PROJECT,
        "provider": "codex",
        "title": sid,
        "redacted_summary": summary,
        "typed_payload": {
            "decision": summary,
            "rationale": "session decision",
            "alternatives": [],
            "consequence": "",
            "authority_ref": "session",
        },
        "confidence": 0.95,
        "confidence_basis": "operator-approved",
    }
    return build_memory_card_candidate_from_source_span(span, refresh_watermark="demo")


def main() -> int:
    golden = load_golden(str(Path(__file__).parent / "golden" / "neurons.golden.draft.json"))
    by_key = {}
    for e in golden:
        by_key.setdefault((e["subject_key"], e["expected_lane"]), e)

    cur = by_key[("authority-model", "current")]["canonical_statement"]
    sup = by_key[("ragflow-as-brain", "superseded_conflict")]["canonical_statement"]

    with tempfile.TemporaryDirectory() as tmp:
        ledger = Ledger(Path(tmp) / "ledger.sqlite")

        # 1) the now-dead claim was once accepted as current
        old = run_autopilot_cycle(
            candidates=[_candidate(sup, "old")], ledger=ledger, refresh_watermark="w1"
        )["accepted"][0]
        # 2) the current truth supersedes it -> old demoted out of current AND accepted
        run_autopilot_cycle(
            candidates=[_candidate(cur, "new")],
            ledger=ledger,
            refresh_watermark="w2",
            supersede_detector=lambda c, _l: old,
        )

        recall = run_brain_query_v2(
            read_model=LegacyLedgerBrainReadModel(ledger),
            brain_id=f"/project/{PROJECT}",
            query="현재 권위 모델",
            query_intent="current_work",
        )
        scorecard = grade_recall_against_golden(recall=recall, golden=golden)

    print(json.dumps({
        "golden_entries": len(golden),
        "lanes": {"current_in_recall": len(recall["current"]), "accepted_in_recall": len(recall["accepted"])},
        "scorecard": {k: scorecard[k] for k in ("false_current_count", "silent_lie_rate", "current_lane_recall", "silent_lies")},
        "note": "demo: hand-fed candidates (not blind mining). Proves demote keeps the dead claim out (silent_lie target 0).",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
