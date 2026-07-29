"""CLI entry for one autopilot curation cycle (the live-schedule entry point).

The testable core ``run_autopilot_command`` runs a cycle over already-mined candidates
and returns a recall snapshot taken through the real product read-path
(``run_brain_query_v2``). ``main`` requires a JSON file of pre-mined candidates; omitting
it fails closed without constructing the retired bridge, a miner, a projector, or a
ledger. ``mine_live_candidates`` remains an explicit legacy helper for callers that
have separately authorized that integration.

AUTOPILOT MODE — DEFAULT CLI BOUNDARY
======================================
GOAL (working goal_state, user-set 2026-06-14):
  On the active neurons-owned project lane, candidate curation -> cycle -> recall must pass
  the golden finish gate: SILENT-LIE == 0 AND false_current == 0, over 3 consecutive
  FINAL-slice cohorts, per-lane F1 >= 0.85.

LEGACY INTEGRATION STATUS:
  The old RetiredIndexBridge live-mining and projection path is not available through
  this CLI. Its compatibility helper remains callable only by an explicit caller that
  owns separate authorization and runtime safeguards.

BOUNDARY:
  The forbidden operations stay HARD-BLOCKED regardless of authorization: memory delete,
  live GC execute, RetiredIndexBridge dataset delete/disable, raw transcript/secret exposure, runtime
  mutation (see FORBIDDEN_AUTO_POLICY_OPERATIONS + CLAUDE.md). Explicit legacy callers
  remain responsible for bounded invocation, redaction, postcheck, rollback, and the
  readiness/conflict tripwire.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Mapping, Sequence

from ..ledger import Ledger
from .autopilot_loop import run_autopilot_cycle
from .brain_query import run_brain_query_v2
from .brain_read_model import LegacyLedgerBrainReadModel
from .extraction_llm import build_vertex_wrapper_completion_fn
from .llm_brain_miner import LlmBrainEnvelopeMiner

# Legacy compatibility marker. It does not enable retired bridge live-mining or
# projection from ``main``; those operations remain outside the default CLI path.
AUTOPILOT_PREAPPROVED = True


RETIRED_BRIDGE_LIVE_MINING_BLOCKED_EXIT = 2


def retired_bridge_live_mining_blocked_report(*, project: str, refresh_watermark: str) -> dict[str, Any]:
    """Return the fail-closed result for the retired default live-mining path."""
    return {
        "schema_version": "llm_brain_autopilot_command.v1",
        "project": project,
        "refresh_watermark": refresh_watermark,
        "status": "blocked_retired_bridge_live_mining",
        "network_used": False,
        "mutation_performed": False,
    }


def mine_live_candidates(
    *,
    retired_index_bridge: Any,
    project: str,
    refresh_watermark: str = "live",
    completion_fn: Any | None = None,
    max_candidates: int = 5,
    source: str = "session-memory",
    provider: str = "",
    limit: int = 200,
) -> list[dict]:
    """Blind mine cycle-ready MemoryCard candidates from the durable SoT (Option B).

    Default source is session-memory — the durable, lossless aggregate of conversations
    (transcript-memory is transient raw chunks GC'd after conversion). Reads docs via RetiredIndexBridge,
    then runs the envelope miner with an instruction-following completion_fn (default: keyless
    vertex-wrapper; the RetiredIndexBridge chat assistant is conversational and won't emit strict JSON).
    The miner never sees the golden; output is directly consumable by run_autopilot_cycle.
    """
    if completion_fn is None:
        completion_fn = build_vertex_wrapper_completion_fn()
    if source == "transcript-memory":
        chunks = retired_index_bridge.list_transcript_memory_chunks(project=project, limit=limit)
    else:
        chunks = retired_index_bridge.list_session_memory_chunks(project=project, provider=provider, limit=limit)
    miner = LlmBrainEnvelopeMiner(completion_fn=completion_fn, max_candidates=max_candidates)
    candidates: list[dict] = []
    for chunk in chunks:
        candidates.extend(miner.mine_chunk(chunk, refresh_watermark=refresh_watermark))
    return candidates


def run_autopilot_command(
    *,
    ledger: Any,
    candidates: Sequence[Mapping[str, Any]],
    project: str,
    refresh_watermark: str,
    supersede_detector: Any | None = None,
    projection_client: Any | None = None,
    timestamp: str | None = None,
) -> dict:
    cycle = run_autopilot_cycle(
        candidates=candidates,
        ledger=ledger,
        refresh_watermark=refresh_watermark,
        supersede_detector=supersede_detector,
        projection_client=projection_client,
        timestamp=timestamp,
    )
    recall = run_brain_query_v2(
        read_model=LegacyLedgerBrainReadModel(ledger),
        brain_id=f"/project/{project}",
        query="현재 진행중인 작업과 최신 결정 알려줘",
        query_intent="current_work",
    )
    return {
        "schema_version": "llm_brain_autopilot_command.v1",
        "project": project,
        "refresh_watermark": refresh_watermark,
        "cycle": {
            "accepted_count": len(cycle["accepted"]),
            "needs_review_count": len(cycle["needs_review"]),
            "superseded_count": len(cycle["superseded"]),
            "projected_count": cycle.get("projected_count", 0),
        },
        "recall": {
            "current_count": len(recall.get("current") or []),
            "accepted_count": len(recall.get("accepted") or []),
            "conflicts_count": len(recall.get("conflicts") or []),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge memory")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--refresh-watermark", required=True)
    parser.add_argument(
        "--candidates-json",
        default="",
        help="required path to a JSON array of pre-mined candidates",
    )
    # Legacy options remain parse-compatible for existing invocations. They never enable
    # retired bridge live-mining or projection from this CLI.
    parser.add_argument("--retired-index-bridge-url", default="")
    parser.add_argument("--retired-index-bridge-token-env", default="")
    parser.add_argument("--policy-proxy-url", default="")
    parser.add_argument("--derived-dataset-id", default="", help="dataset id for supersede candidate recall")
    parser.add_argument("--llm-id", default="")
    # Legacy mining bounds stay accepted for parse compatibility; ``main`` still
    # fails closed before live mining when --candidates-json is absent.
    parser.add_argument("--limit", type=int, default=200, help="max transcript chunks to mine this cycle")
    parser.add_argument("--max-candidates", type=int, default=5, help="max candidates extracted per chunk")
    args = parser.parse_args(argv)

    if not args.candidates_json:
        print(
            json.dumps(
                retired_bridge_live_mining_blocked_report(
                    project=args.project,
                    refresh_watermark=args.refresh_watermark,
                ),
                sort_keys=True,
            )
        )
        return RETIRED_BRIDGE_LIVE_MINING_BLOCKED_EXIT

    with open(args.candidates_json, encoding="utf-8") as handle:
        candidates = json.load(handle)
    if not isinstance(candidates, list):
        raise ValueError("--candidates-json must contain a JSON array of candidates")

    ledger = Ledger(args.ledger)

    result = run_autopilot_command(
        ledger=ledger,
        candidates=candidates,
        project=args.project,
        refresh_watermark=args.refresh_watermark,
    )
    print(json.dumps(result, sort_keys=True))
    return 0
