"""CLI wrapper for the LLM-brain eval loop."""

from __future__ import annotations

import argparse
import hashlib
import json

from agent_knowledge.ledger import Ledger

from .eval_loop import run_enabled_eval_queries
from .semantic_ranker import build_embedding_semantic_ranker


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_stdout_payload(result: dict) -> dict:
    """Return aggregate-only CLI output safe for Kubernetes Job logs."""

    payload = dict(result)
    metrics = dict(payload.get("metrics") or {})
    metrics.pop("per_query", None)
    failures = payload.pop("failures", [])
    run_id = str(payload.pop("run_id", ""))
    payload["metrics"] = metrics
    payload["failure_count"] = len(failures) if isinstance(failures, list) else 0
    if run_id:
        payload["run_id_hash"] = _sha256_text(run_id)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge eval")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--project", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--run-id", default="")
    parser.add_argument(
        "--retain-runs",
        type=int,
        default=0,
        help="after execute, keep only the latest N eval_runs for the project/provider; 0 disables pruning",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="append eval_runs and retrieval_audit rows; omit for dry-run",
    )
    parser.add_argument(
        "--semantic-rank",
        action="store_true",
        help="use the configured embedding endpoint to vector-rank accepted MemoryCards before eval scoring",
    )
    args = parser.parse_args(argv)

    if args.execute:
        # Attach to an existing runtime ledger without running schema bootstrap/migrations.
        ledger = Ledger(args.ledger, initialize_schema=False)
    else:
        ledger = Ledger.open_read_only(args.ledger)
    semantic_ranker = build_embedding_semantic_ranker() if args.semantic_rank else None
    try:
        result = run_enabled_eval_queries(
            ledger=ledger,
            project=args.project or None,
            provider=args.provider or None,
            limit=args.limit or None,
            execute=bool(args.execute),
            run_id=args.run_id or None,
            retain_runs=args.retain_runs,
            semantic_ranker=semantic_ranker,
        )
    finally:
        close = getattr(semantic_ranker, "close", None)
        if callable(close):
            close()
    print(json.dumps(_safe_stdout_payload(result), ensure_ascii=False, sort_keys=True))
    # Evaluation quality failure is persisted as eval_runs.status=fail; process
    # failure should mean the loop could not run/store its bounded evidence.
    return 0 if result["status"] in {"dry_run", "pass", "fail", "no_queries"} else 1
