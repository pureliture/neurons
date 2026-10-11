"""Bounded, approval-gated runtime for the canonical PG embedding outbox.

Default invocation is planning-only: no DB connection or provider construction.
Activation is an operator/deployment decision, not memory-card auto-approval.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import threading
from collections.abc import Mapping

from ..model_connectors import DEFAULT_EMBEDDING_DIM, DEFAULT_EMBEDDING_MODEL, DEFAULT_EMBEDDING_PROFILE_ID, resolve_embedding_spec
from ..rag_ingress.pg_backfill import validated_pg_embedding_profile
from ..rag_ingress.pg_embedding_privacy import assert_pg_embedding_egress_safe
from ..rag_ingress.qdrant_embedding import build_openai_embedding_provider
from .outbox_worker import OutboxWorker
from .pgvector_store import PgVectorStore


def _positive_int(value: str) -> int:
    result = int(value)
    if not 1 <= result <= 1000:
        raise argparse.ArgumentTypeError("value must be between 1 and 1000")
    return result


def _positive_seconds(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or not 0 < result <= 3600:
        raise argparse.ArgumentTypeError("seconds must be finite and positive, at most 3600")
    return result


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge pg-embedding-outbox")
    parser.add_argument("--execute", action="store_true", help="Enable leased queue writes; requires operator activation")
    parser.add_argument("--allow-model-calls", action="store_true", help="Operator-approved provider use; does not approve any memory card")
    parser.add_argument("--dry-run", action="store_true", help="Configuration plan only; does not inspect or change the queue")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--batch-size", type=_positive_int, default=1)
    parser.add_argument("--max-jobs", type=_positive_int, default=20, help="Attempt ceiling for this process, not a daily/currency budget")
    parser.add_argument("--max-iterations", type=_positive_int, default=60)
    parser.add_argument("--lease-seconds", type=_positive_int, default=180)
    parser.add_argument("--poll-interval", type=_positive_seconds, default=5.0)
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    report = {"schema_version": "lbrain_embedding_consumer.v1", "profile": DEFAULT_EMBEDDING_PROFILE_ID, "attempted": 0, "completed": 0, "failed": 0, "stale": 0, "model_calls": 0}

    def emit(status: str, **fields) -> None:
        print(json.dumps({**report, "status": status, **fields}, sort_keys=True))

    if args.dry_run or not (args.execute and args.allow_model_calls):
        emit("planned" if args.dry_run else "activation_required", network_used=False, mutation_performed=False, max_jobs=args.max_jobs)
        return 0 if args.dry_run else 2

    dsn = env.get("NEURON_LBRAIN_PGVECTOR_DSN", "")
    try:
        spec = resolve_embedding_spec(env)
        timeout = float(env.get("LLM_BRAIN_EMBEDDING_TIMEOUT_SECONDS", "30"))
        if not dsn or not spec.base_url:
            raise ValueError("configuration_required")
        if (spec.model, spec.dim) != (DEFAULT_EMBEDDING_MODEL, DEFAULT_EMBEDDING_DIM):
            raise ValueError("unsupported_embedding_profile")
        batch_bound = min(args.batch_size, args.max_jobs)
        # Every job is leased together but provider calls are serial. Reserve
        # the configured SDK attempts plus write/backoff margin for ALL jobs.
        minimum_lease = (timeout * 3 + 15) * batch_bound
        if not math.isfinite(timeout) or not 0 < timeout <= 30 or args.lease_seconds < minimum_lease:
            raise ValueError("unsafe_provider_timeout_or_lease")
    except (ValueError, TypeError):
        emit("configuration_rejected", network_used=False, mutation_performed=False)
        return 2

    provider = None
    stop = threading.Event()
    previous_sigterm = None
    try:
        provider = build_openai_embedding_provider(environ=env)
        validated_pg_embedding_profile(provider)

        def embed(text: str) -> list[float]:
            assert_pg_embedding_egress_safe(text)
            if len(text.encode("utf-8")) > 32768:
                raise ValueError("embedding_input_budget_exceeded")
            report["model_calls"] += 1
            return provider.embed(text)

        store = PgVectorStore(dsn=dsn)
        worker = OutboxWorker(store=store, embedding_fn=embed, batch_size=min(args.batch_size, args.max_jobs), lease_seconds=args.lease_seconds, poll_interval_seconds=args.poll_interval)
        if threading.current_thread() is threading.main_thread():
            previous_sigterm = signal.signal(signal.SIGTERM, lambda _signum, _frame: stop.set())
        for _ in range(1 if args.once else args.max_iterations):
            remaining = args.max_jobs - report["attempted"]
            if stop.is_set() or remaining <= 0:
                break
            worker.batch_size = min(args.batch_size, remaining)
            # Preserve confirmed progress even if failure recording raises a DB
            # error after an earlier job has already committed successfully.
            try:
                attempted = worker.run_once()
            finally:
                report["attempted"] += worker.last_batch_attempted
                report["completed"] += worker.last_batch_completed
                report["failed"] += worker.last_batch_failed
                report["stale"] += worker.last_batch_stale
            if args.once:
                break
            if attempted == 0:
                stop.wait(args.poll_interval)
        emit("failed" if report["failed"] else ("processed" if report["attempted"] else "idle"), stop_requested=stop.is_set())
        return 1 if report["failed"] else 0
    except KeyboardInterrupt:
        emit("interrupted")
        return 130
    except Exception as exc:
        # Exception text may contain a DSN, credential or provider input.
        emit("consumer_failed", error_code=type(exc).__name__)
        return 1
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
        if provider is not None:
            close = getattr(provider, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
