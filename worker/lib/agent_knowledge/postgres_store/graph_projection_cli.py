"""PG Graph Projection Outbox Worker CLI (M7 steady-state consumer).

Polls graph_projection_outbox using FOR UPDATE SKIP LOCKED leases,
maps each payload to an OntologyEpisode, and projects it through
the Graphiti adapter seam.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any

from .outbox_worker import GraphProjectionWorker
from .pgvector_store import PgVectorStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neuron-knowledge pg-graph-project",
        description="Steady-state graph projection outbox worker for PostgreSQL memory cards.",
    )
    parser.add_argument(
        "--pg-dsn",
        default=os.environ.get("NEURON_LBRAIN_PGVECTOR_DSN", ""),
        help="PostgreSQL DSN (or set NEURON_LBRAIN_PGVECTOR_DSN env)",
    )
    parser.add_argument(
        "--enable-graph",
        action="store_true",
        help="Enable graph backend (best-effort, degrades to unavailable on failure)",
    )
    parser.add_argument(
        "--graph-required",
        action="store_true",
        help="Graph backend must initialize and pass connectivity probe (fail-fast)",
    )
    parser.add_argument(
        "--worker-id",
        default=None,
        help="Worker ID for lease claiming (default: auto-generated)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Max jobs per batch (default: 10)",
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=60,
        help="Lease duration in seconds (default: 60)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="Poll interval in seconds when idle (default: 0.5)",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Max poll iterations (default: infinite loop)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single batch and exit (for cron mode)",
    )
    args = parser.parse_args(argv)

    if not args.pg_dsn:
        print(
            "error: --pg-dsn or NEURON_LBRAIN_PGVECTOR_DSN env is required",
            file=sys.stderr,
        )
        return 2

    try:
        store = PgVectorStore(dsn=args.pg_dsn)
    except Exception as exc:
        print(f"error: failed to initialize PG store: {exc}", file=sys.stderr)
        return 1

    try:
        from ..llm_brain_core.runtime_graph import build_graph_adapter_from_env

        graph_adapter = build_graph_adapter_from_env(
            enable_flag=True if args.enable_graph else None,
            required_flag=bool(args.graph_required),
        )
    except Exception as exc:
        print(f"error: failed to build graph adapter: {exc}", file=sys.stderr)
        return 1

    worker = GraphProjectionWorker(
        store=store,
        graph_adapter=graph_adapter,
        worker_id=args.worker_id,
        batch_size=args.batch_size,
        lease_seconds=args.lease_seconds,
        poll_interval_seconds=args.poll_interval,
    )

    if args.once:
        processed = worker.run_once()
        print(f"processed {processed} graph projection job(s)")
        return 0

    try:
        worker.run_loop(max_iterations=args.max_iterations)
    except KeyboardInterrupt:
        worker.stop()
        print("interrupted, shutting down", file=sys.stderr)
        return 130

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
