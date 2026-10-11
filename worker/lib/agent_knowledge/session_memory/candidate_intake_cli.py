"""Bounded, activation-gated session-artifact -> steward-candidate producer.

The default invocation plans without touching a database. Producing a candidate
requires an explicit operator decision; it never approves a memory card, never
accepts a proposal, and never reads raw transcript bodies.
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from typing import Any

from ..postgres_store.pgvector_store import PgVectorStore
from ..session_memory.brain_steward import BrainStewardService
from ..session_memory.llm_brain_miner import LlmBrainEnvelopeMiner


def _positive_int(value: str) -> int:
    result = int(value)
    if not 1 <= result <= 1000:
        raise argparse.ArgumentTypeError("value must be between 1 and 1000")
    return result


def load_artifact_source(source_json: str) -> list[Mapping[str, Any]]:
    """Load redacted session artifacts from a JSON file the caller prepared.

    Raw transcripts are never read here. The file must contain a JSON array of
    objects that already carry redacted summaries and opaque source locators.
    """
    with open(source_json, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError("source file must contain a JSON array")
    for index, item in enumerate(payload):
        if not isinstance(item, Mapping):
            raise ValueError(f"source item {index} must be an object")
    return [dict(item) for item in payload]


def build_intake_runtime(*, dsn: str, ledger_path: str) -> BrainStewardService:
    """Construct the production producer from explicit activation inputs."""
    from ..ledger import Ledger

    return BrainStewardService(
        Ledger.open_read_only(ledger_path), pgvector_store=PgVectorStore(dsn=dsn)
    )


def propose_from_artifacts(
    *,
    steward: Any,
    artifacts: list[Mapping[str, Any]],
    project: str,
    refresh_watermark: str,
    completion_fn: Any,
    max_candidates: int = 5,
) -> dict[str, Any]:
    """Turn redacted artifacts into pending steward candidates.

    Every produced candidate is stored only in a non-authoritative review
    state. Nothing here approves a card, accepts a proposal, or replaces an
    existing authority, and a model failure never counts as a candidate.
    """
    miner = LlmBrainEnvelopeMiner(completion_fn=completion_fn, max_candidates=max_candidates)
    produced: list[dict[str, Any]] = []
    model_calls = 0
    for artifact in artifacts:
        model_calls += 1
        try:
            candidates = miner.mine_chunk(artifact, refresh_watermark=refresh_watermark)
        except Exception:
            # A model failure produces no candidate and never raises from the loop.
            continue
        for candidate in candidates:
            stored = steward.candidate_create(source_span=candidate, mark_needs_review=True)
            produced.append(
                {
                    "memory_id": stored["memory_id"],
                    "card_type": stored["card_type"],
                    "approval_state": stored["approval_state"],
                    "lifecycle_state": stored["lifecycle_state"],
                }
            )
    return {
        "project": project,
        "examined_count": len(artifacts),
        "candidate_count": len(produced),
        "candidates": produced,
        "model_calls": model_calls,
        "network_used": bool(artifacts),
        "mutation_performed": bool(produced),
    }


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge pg-memory-propose")
    parser.add_argument("--project", required=True)
    parser.add_argument("--source-json", required=True, help="JSON array of redacted session artifacts")
    parser.add_argument("--ledger-path", default="")
    parser.add_argument("--refresh-watermark", default="session-refresh")
    parser.add_argument("--max-candidates", type=_positive_int, default=5)
    parser.add_argument("--execute", action="store_true", help="Requires operator activation and explicit --allow-model-calls")
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Report the plan without connecting to anything")
    args = parser.parse_args(argv)
    env = environ if environ is not None else __import__("os").environ

    def emit(status: str, **fields) -> None:
        print(json.dumps({
            "schema_version": "lbrain_memory_propose.v1",
            "project": args.project, "status": status, **fields,
        }, sort_keys=True))

    if args.dry_run or not (args.execute and args.allow_model_calls):
        emit("planned" if args.dry_run else "activation_required", network_used=False, mutation_performed=False)
        return 0 if args.dry_run else 2

    if not env.get("NEURON_LBRAIN_PGVECTOR_DSN", "") or not args.ledger_path:
        emit("configuration_rejected", network_used=False, mutation_performed=False)
        return 2

    from ..ledger import Ledger

    try:
        steward = build_intake_runtime(
            dsn=env["NEURON_LBRAIN_PGVECTOR_DSN"], ledger_path=args.ledger_path
        )
        artifacts = load_artifact_source(args.source_json)
    except Exception as exc:
        emit("configuration_rejected", error_code=type(exc).__name__, network_used=False, mutation_performed=False)
        return 2

    from ..session_memory.extraction_llm import build_vertex_wrapper_completion_fn

    try:
        completion_fn = build_vertex_wrapper_completion_fn()
        result = propose_from_artifacts(
            steward=steward,
            artifacts=artifacts,
            project=args.project,
            refresh_watermark=args.refresh_watermark,
            completion_fn=completion_fn,
            max_candidates=args.max_candidates,
        )
    except Exception as exc:
        emit("produce_failed", error_code=type(exc).__name__, network_used=False, mutation_performed=False)
        return 1
    emit("produced", **result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
