"""Retired compatibility CLI for legacy session-memory private sync.

The old command performed live RetiredIndexBridge writes. In neurons we only keep a
read-only dry-run planner until the server-side live sync lane has an approved
runtime contract.
"""

from __future__ import annotations

import argparse
import json
import sys

from ..ledger import Ledger
from .memory_regeneration import LedgerTranscriptMemorySource, SessionMemoryRegenerationRunner

SCHEMA_VERSION = "agent_knowledge_session_memory_private_sync_cli.v1"
COMMAND = "session-memory-private-sync"
LIVE_SYNC_REPLACEMENT = [
    "memory-regeneration build-session-memory --ledger <ledger> --project <project> --provider <provider> --session-id-hash <hash>",
    "memory-regeneration build-session-memory --ledger <ledger> --project <project> --provider <provider> --all-sessions",
]


def _strip_program(argv: list[str]) -> list[str]:
    if argv and argv[0] == COMMAND:
        return argv[1:]
    return argv


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=COMMAND)
    parser.add_argument("--ledger")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--quiet-period-seconds", type=int, default=60)

    # Legacy live-sync arguments are accepted so old argv can fail closed with a
    # structured report instead of falling through to an unknown-arg error.
    parser.add_argument("--dataset-id", default="")
    parser.add_argument("--dataset-name", default="")
    parser.add_argument("--retired-index-bridge-url", default="")
    parser.add_argument("--retired-index-bridge-token-env", default="")
    parser.add_argument("--runtime-dir", default="")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--max-processed-per-run", type=int, default=25)
    parser.add_argument("--max-session-attempts", type=int, default=2)
    parser.add_argument("--retry-backoff-seconds", default="60,180")
    parser.add_argument("--poll-attempts", type=int, default=60)
    parser.add_argument("--poll-interval-seconds", type=float, default=1.0)
    parser.add_argument("--transcript-read-source", choices=["ledger", "index_read_sot"], default="ledger")
    parser.add_argument("--approval", default="")
    return parser


def _print_report(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


def _blocked_report(*, status: str = "blocked_live_execution", reason: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "command": COMMAND,
        "reason": reason,
        "mode": "blocked",
        "mutation_performed": False,
        "network_used": False,
        "index_write_performed": False,
        "raw_ids_printed": False,
        "raw_paths_printed": False,
        "replacement_commands": LIVE_SYNC_REPLACEMENT,
    }


def _has_live_args(args: argparse.Namespace) -> bool:
    return any(
        [
            args.dataset_id,
            args.dataset_name,
            args.retired_index_bridge_url,
            args.retired_index_bridge_token_env,
            args.approval,
            args.transcript_read_source != "ledger",
        ]
    )


def _fragment(value: str) -> str:
    return str(value or "").split(":", 1)[-1][:12]


def _candidate_report(row: dict, runner: SessionMemoryRegenerationRunner) -> dict:
    base = {
        "provider": str(row.get("provider") or ""),
        "project": str(row.get("project") or ""),
        "session_id_fragment": _fragment(str(row.get("session_id_hash") or "")),
        "dirty_status": str(row.get("status") or ""),
        "dirty_reason": str(row.get("reason") or ""),
    }
    try:
        report = runner.run(
            project=base["project"],
            provider=base["provider"],
            session_id_hash=str(row.get("session_id_hash") or ""),
        )
    except ValueError as exc:
        return {
            **base,
            "candidate_status": "planning_failed",
            "error_class": exc.__class__.__name__,
            "error": str(exc)[:200],
            "sessions_seen": 0,
            "memory_documents_planned": 0,
            "skipped_session_count": 0,
        }
    return {
        **base,
        "candidate_status": "planned",
        "sessions_seen": int(report.get("sessions_seen") or 0),
        "memory_documents_planned": int(report.get("memory_documents_planned") or 0),
        "skipped_session_count": len(report.get("skipped_sessions") or []),
    }


def _run_dry_run(args: argparse.Namespace) -> int:
    if not args.ledger:
        print("--ledger is required for dry-run", file=sys.stderr)
        return 2
    if _has_live_args(args):
        _print_report(_blocked_report(reason="legacy live sync arguments are not accepted in dry-run mode"))
        return 1

    limit = args.limit if args.limit is not None else args.max_processed_per_run
    ledger = Ledger.open_read_only(args.ledger)
    rows = ledger.list_dirty_session_memory(
        limit=limit,
        quiet_period_seconds=args.quiet_period_seconds,
    )
    runner = SessionMemoryRegenerationRunner(source=LedgerTranscriptMemorySource(ledger))
    candidates = [_candidate_report(row, runner) for row in rows]
    _print_report(
        {
            "schema_version": SCHEMA_VERSION,
            "status": "dry_run_complete",
            "command": COMMAND,
            "mode": "dry_run",
            "mutation_performed": False,
            "network_used": False,
            "index_write_performed": False,
            "raw_ids_printed": False,
            "raw_paths_printed": False,
            "limit": max(int(limit), 1),
            "quiet_period_seconds": max(int(args.quiet_period_seconds), 0),
            "dirty_sessions_seen": len(rows),
            "sessions_seen": sum(item["sessions_seen"] for item in candidates),
            "memory_documents_planned": sum(item["memory_documents_planned"] for item in candidates),
            "candidate_reports": candidates,
            "replacement_commands": LIVE_SYNC_REPLACEMENT,
        }
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = _strip_program(list(sys.argv[1:] if argv is None else argv))
    parser = _build_parser()
    args = parser.parse_args(raw_argv)
    if args.dry_run:
        return _run_dry_run(args)
    _print_report(
        _blocked_report(
            status="blocked_retired_legacy_entrypoint",
            reason="live dirty session-memory sync is not vendored into neurons without an approved server-side runtime contract",
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
