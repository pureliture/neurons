"""Live gate measurement (EVAL LANE): vertex blind mine -> accept -> embedding-graded gate.

No projection (gate reads ledger). Uses vertex extraction + vertex embedding join so the
score reflects semantic coverage of the golden by what is actually mineable from the corpus.
Run: RAGFLOW_API_KEY=... uv run python eval/gate_measure.py
"""

from __future__ import annotations

import json
import os
import sys

from agent_knowledge.ledger import Ledger
from agent_knowledge.mcp_server import build_ragflow_client
from agent_knowledge.session_memory.autopilot_cli import mine_live_candidates
from agent_knowledge.session_memory.autopilot_loop import run_autopilot_cycle
from agent_knowledge.session_memory.brain_query import run_brain_query_v2
from agent_knowledge.session_memory.brain_read_model import LegacyLedgerBrainReadModel
from agent_knowledge.session_memory.extraction_llm import build_vertex_embedding_fn

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from golden_grader import build_cosine_match_fn, grade_recall_against_golden, load_golden

PROJECT = os.environ.get("GATE_PROJECT", "neurons")
LIMIT = int(os.environ.get("GATE_LIMIT", "15"))
THRESHOLD = float(os.environ.get("GATE_THRESHOLD", "0.78"))


def main() -> int:
    rf = build_ragflow_client(
        ragflow_url="http://127.0.0.1:19380", token=os.environ["RAGFLOW_API_KEY"], policy_proxy_url=""
    )
    ledger = Ledger(os.path.expanduser("~/.autopilot-canary/gate.sqlite"))

    candidates = mine_live_candidates(ragflow=rf, project=PROJECT, limit=LIMIT, max_candidates=2)
    run_autopilot_cycle(candidates=candidates, ledger=ledger, refresh_watermark="gate-measure")
    recall = run_brain_query_v2(
        read_model=LegacyLedgerBrainReadModel(ledger),
        brain_id=f"/project/{PROJECT}",
        query="current decisions and status",
        query_intent="current_work",
        limit=50,
    )
    golden = load_golden(os.path.join(os.path.dirname(__file__), "golden", "neurons.golden.draft.json"))
    match = build_cosine_match_fn(build_vertex_embedding_fn(), threshold=THRESHOLD)
    sc = grade_recall_against_golden(recall=recall, golden=golden, match_fn=match)

    print(json.dumps({
        "mined_cards": len(candidates),
        "recall_current": len(recall.get("current") or []),
        "gate": {k: sc[k] for k in ("false_current_count", "silent_lie_rate", "current_lane_recall")},
        "mined_summaries": [str(c.get("summary") or "")[:60] for c in candidates],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
