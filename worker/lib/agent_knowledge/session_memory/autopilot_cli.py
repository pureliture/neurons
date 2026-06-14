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
from ..memory_miner import build_ragflow_completion_fn
from .autopilot_loop import run_autopilot_cycle
from .brain_query import run_brain_query_v2
from .brain_read_model import LegacyLedgerBrainReadModel
from .llm_brain_miner import LlmBrainEnvelopeMiner


def mine_live_candidates(
    *,
    ragflow: Any,
    project: str,
    refresh_watermark: str = "live",
    llm_id: str = "",
    max_candidates: int = 5,
    query: str = "conversation chunk",
    limit: int = 200,
) -> list[dict]:
    """Blind mine cycle-ready MemoryCard candidates from transcript-memory (Option B).

    Reads redacted transcript-memory chunks for the project and runs the envelope miner,
    which prompts the RAGFlow chat model to emit 6-type MemoryCard envelopes directly.
    The miner never sees the golden; output is directly consumable by run_autopilot_cycle.
    """
    chunks = ragflow.list_transcript_memory_chunks(project=project, query=query, limit=limit)
    miner = LlmBrainEnvelopeMiner(
        completion_fn=build_ragflow_completion_fn(ragflow, llm_id=llm_id),
        max_candidates=max_candidates,
    )
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
    import os

    parser = argparse.ArgumentParser(prog="neuron-knowledge memory")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--refresh-watermark", required=True)
    parser.add_argument(
        "--candidates-json",
        default="",
        help="path to a JSON array of pre-mined candidates; omit to mine live from RAGFlow",
    )
    # Live-mining options (used when --candidates-json is omitted). This is the Ubuntu
    # brain-server entry a systemd timer/cron invokes.
    parser.add_argument("--ragflow-url", default="")
    parser.add_argument("--token-env", default="")
    parser.add_argument("--policy-proxy-url", default="")
    parser.add_argument("--derived-dataset-id", default="", help="dataset id for supersede candidate recall")
    parser.add_argument("--llm-id", default="")
    args = parser.parse_args(argv)

    ledger = Ledger(args.ledger)
    supersede_detector = None

    if args.candidates_json:
        with open(args.candidates_json, encoding="utf-8") as handle:
            candidates = json.load(handle)
        if not isinstance(candidates, list):
            raise ValueError("--candidates-json must contain a JSON array of candidates")
    else:
        from ..mcp_server import build_ragflow_client
        from .supersede_detector import build_ragflow_judge_fn, build_supersede_detector

        token = os.environ.get(args.token_env, "") if args.token_env else ""
        ragflow = build_ragflow_client(
            ragflow_url=args.ragflow_url, token=token, policy_proxy_url=args.policy_proxy_url
        )
        candidates = mine_live_candidates(
            ragflow=ragflow,
            project=args.project,
            refresh_watermark=args.refresh_watermark,
            llm_id=args.llm_id,
        )
        if args.derived_dataset_id:
            supersede_detector = build_supersede_detector(
                ragflow=ragflow,
                judge_fn=build_ragflow_judge_fn(ragflow, llm_id=args.llm_id),
                dataset_id=args.derived_dataset_id,
                project=args.project,
            )

    result = run_autopilot_command(
        ledger=ledger,
        candidates=candidates,
        project=args.project,
        refresh_watermark=args.refresh_watermark,
        supersede_detector=supersede_detector,
    )
    print(json.dumps(result, sort_keys=True))
    return 0
