"""CLI entry for one autopilot curation cycle (the live-schedule entry point).

This un-stubs the ``memory`` server command. The testable core ``run_autopilot_command``
runs a cycle over already-mined candidates and returns a recall snapshot taken through
the real product read-path (run_brain_query_v2). ``main`` loads candidates from a JSON
file so a scheduler/operator can drive cycles without a live LLM in this process; the
transcript-memory LLM mining provider is a separate live integration that produces the
candidates JSON.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Mapping, Sequence

from ..ledger import Ledger
from .autopilot_loop import run_autopilot_cycle
from .brain_query import run_brain_query_v2
from .brain_read_model import LegacyLedgerBrainReadModel


def run_autopilot_command(
    *,
    ledger: Any,
    candidates: Sequence[Mapping[str, Any]],
    project: str,
    refresh_watermark: str,
    supersede_detector: Any | None = None,
    timestamp: str | None = None,
) -> dict:
    cycle = run_autopilot_cycle(
        candidates=candidates,
        ledger=ledger,
        refresh_watermark=refresh_watermark,
        supersede_detector=supersede_detector,
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
        required=True,
        help="path to a JSON array of already-mined MemoryCard candidates",
    )
    args = parser.parse_args(argv)

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
