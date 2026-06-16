"""CLI entry for one autopilot curation cycle (the live-schedule entry point).

This un-stubs the ``memory`` server command. The testable core ``run_autopilot_command``
runs a cycle over already-mined candidates and returns a recall snapshot taken through
the real product read-path (run_brain_query_v2). ``main`` either loads candidates from a
JSON file or blind-mines them live from transcript-memory via RAGFlow.

AUTOPILOT MODE — GOAL AND STANDING AUTHORIZATION
================================================
GOAL (working goal_state, user-set 2026-06-14):
  On the active neurons-owned project lane, live blind mine -> cycle -> recall must pass
  the golden finish gate: SILENT-LIE == 0 AND false_current == 0, over 3 consecutive
  FINAL-slice cohorts, per-lane F1 >= 0.85.

STANDING PRE-APPROVAL (user, 2026-06-14):
  All autopilot approval gates are pre-approved and run unattended — candidate auto-accept
  (human_approval path with approved_by='autopilot') and RAGFlow projection writes (the loop
  self-mints the projection approval_record). No per-cycle human confirmation is required.

BOUNDARY (unchanged by the pre-approval — these are NOT covered by it):
  The forbidden operations stay HARD-BLOCKED regardless of authorization: memory delete,
  live GC execute, RAGFlow dataset delete/disable, raw transcript/secret exposure, runtime
  mutation (see FORBIDDEN_AUTO_POLICY_OPERATIONS + CLAUDE.md). Pre-approval removes the
  "ask first" gate, not the safety mechanics: live writes still run bounded with exact argv,
  timeout, redaction, postcheck, and rollback, and the readiness/conflict tripwire still
  freezes + rolls back a poisoned cohort.
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

# Standing pre-approval flag for the autopilot operating mode (user-set 2026-06-14).
# Scope = candidate accept + RAGFlow projection write only. Forbidden ops remain blocked.
AUTOPILOT_PREAPPROVED = True


def mine_live_candidates(
    *,
    ragflow: Any,
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
    (transcript-memory is transient raw chunks GC'd after conversion). Reads docs via RAGFlow,
    then runs the envelope miner with an instruction-following completion_fn (default: keyless
    vertex-wrapper; the RAGFlow chat assistant is conversational and won't emit strict JSON).
    The miner never sees the golden; output is directly consumable by run_autopilot_cycle.
    """
    if completion_fn is None:
        completion_fn = build_vertex_wrapper_completion_fn()
    if source == "transcript-memory":
        chunks = ragflow.list_transcript_memory_chunks(project=project, limit=limit)
    else:
        chunks = ragflow.list_session_memory_chunks(project=project, provider=provider, limit=limit)
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
    # Canary bounds — required even under standing pre-approval (safety mechanic, not a gate).
    parser.add_argument("--limit", type=int, default=200, help="max transcript chunks to mine this cycle")
    parser.add_argument("--max-candidates", type=int, default=5, help="max candidates extracted per chunk")
    args = parser.parse_args(argv)

    ledger = Ledger(args.ledger)
    supersede_detector = None
    projection_client = None

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
            limit=args.limit,
            max_candidates=args.max_candidates,
        )
        if args.derived_dataset_id:
            from .ragflow_projection import RagflowMemoryCardProjectionClient

            supersede_detector = build_supersede_detector(
                ragflow=ragflow,
                judge_fn=build_ragflow_judge_fn(ragflow, llm_id=args.llm_id),
                dataset_id=args.derived_dataset_id,
                project=args.project,
            )
            projection_client = RagflowMemoryCardProjectionClient(
                ragflow=ragflow, dataset_id=args.derived_dataset_id
            )

    result = run_autopilot_command(
        ledger=ledger,
        candidates=candidates,
        project=args.project,
        refresh_watermark=args.refresh_watermark,
        supersede_detector=supersede_detector,
        projection_client=projection_client,
    )
    print(json.dumps(result, sort_keys=True))
    return 0
