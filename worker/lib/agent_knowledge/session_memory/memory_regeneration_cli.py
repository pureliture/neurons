"""Dry-run/fail-closed CLI for server-owned memory regeneration."""

from __future__ import annotations

import argparse
import json
import sys

from ..ledger import Ledger
from .memory_regeneration import (
    DEFAULT_PROJECT_MEMORY_TARGET_PROFILE,
    DEFAULT_SESSION_MEMORY_TARGET_PROFILE,
    LedgerTranscriptMemorySource,
    ProjectMemoryRegenerationRunner,
    SessionMemoryBulkDryRunRunner,
    SessionMemoryRegenerationRunner,
)


def _strip_program(argv: list[str]) -> list[str]:
    if argv and argv[0] == "memory-regeneration":
        return argv[1:]
    return argv


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memory-regeneration")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run")
    run.add_argument("--output", choices=["session-memory", "project-memory"], default="project-memory")
    run.add_argument("--ledger", required=True)
    run.add_argument("--project")
    run.add_argument("--provider")
    run.add_argument("--session-id-hash")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--enqueue", action="store_true")
    run.add_argument("--sync", action="store_true")
    run.add_argument("--ingress-url")
    run.add_argument("--target-profile")
    run.add_argument("--approval")

    build_session = subparsers.add_parser("build-session-memory")
    build_session.add_argument("--ledger", required=True)
    build_session.add_argument("--project", required=True)
    build_session.add_argument("--provider", required=True)
    build_session.add_argument("--session-id-hash")
    build_session.add_argument("--all-sessions", action="store_true")
    build_session.add_argument("--bulk-sample-limit", type=int, default=20)
    build_session.add_argument("--max-sessions", type=int, default=0)
    build_session.add_argument("--sync", action="store_true")
    build_session.add_argument("--dataset-id")
    build_session.add_argument("--retired-index-bridge-url")
    build_session.add_argument("--retired-index-bridge-token-env")
    build_session.add_argument("--approval")

    process_dirty = subparsers.add_parser("process-dirty")
    process_dirty.add_argument("--output", choices=["project-memory"], default="project-memory")
    process_dirty.add_argument("--ledger", required=True)
    process_dirty.add_argument("--limit", type=int, default=10)
    process_dirty.add_argument("--quiet-period-seconds", type=int, default=60)
    process_dirty.add_argument("--dry-run", action="store_true")
    process_dirty.add_argument("--enqueue", action="store_true")
    process_dirty.add_argument("--ingress-url")
    process_dirty.add_argument("--target-profile")
    process_dirty.add_argument("--approval")

    for name in (
        "reconcile-indexed",
        "cleanup-session-memory",
        "disable-session-memory",
        "reset-session-memory-dataset",
        "audit-session-memory-context",
        "promote-indexed",
    ):
        blocked = subparsers.add_parser(name)
        blocked.add_argument("--ledger")
        blocked.add_argument("--approval")

    return parser


def _print_live_blocked(command: str) -> int:
    print(
        json.dumps(
            {
                "status": "blocked_live_execution",
                "command": command,
                "dry_run": False,
                "mutation_performed": False,
                "network_used": False,
                "raw_ids_printed": False,
                "raw_paths_printed": False,
                "failed_error_class": "live_memory_regeneration_cli_not_vendored",
            },
            sort_keys=True,
        )
    )
    return 1


def _open_read_only_ledger(path: str) -> Ledger:
    return Ledger.open_read_only(path)


def _run_memory_regeneration(args: argparse.Namespace) -> int:
    if args.enqueue or args.sync or args.ingress_url or args.approval:
        return _print_live_blocked("run")
    try:
        ledger = _open_read_only_ledger(args.ledger)
        source = LedgerTranscriptMemorySource(ledger)
        if args.output == "project-memory":
            report = ProjectMemoryRegenerationRunner(
                source=source,
                target_profile=args.target_profile or DEFAULT_PROJECT_MEMORY_TARGET_PROFILE,
            ).run(
                project=args.project,
                provider=args.provider,
                session_id_hash=args.session_id_hash,
            )
        else:
            report = SessionMemoryRegenerationRunner(
                source=source,
                target_profile=args.target_profile or DEFAULT_SESSION_MEMORY_TARGET_PROFILE,
            ).run(
                project=args.project,
                provider=args.provider,
                session_id_hash=args.session_id_hash,
            )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


def _run_build_session_memory(args: argparse.Namespace) -> int:
    if args.sync or args.dataset_id or args.retired_index_bridge_url or args.retired_index_bridge_token_env or args.approval:
        return _print_live_blocked("build-session-memory")
    if args.all_sessions and args.session_id_hash:
        print("session-memory build requires either --session-id-hash or --all-sessions, not both", file=sys.stderr)
        return 2
    if not args.all_sessions and not args.session_id_hash:
        print("session-memory build requires --session-id-hash or --all-sessions", file=sys.stderr)
        return 2
    try:
        ledger = _open_read_only_ledger(args.ledger)
        source = LedgerTranscriptMemorySource(ledger, densify_indexed_windows=bool(args.all_sessions))
        if args.all_sessions:
            report = SessionMemoryBulkDryRunRunner(
                source=source,
                sample_limit=args.bulk_sample_limit,
                max_sessions=args.max_sessions,
            ).run(project=args.project, provider=args.provider)
        else:
            report = SessionMemoryRegenerationRunner(source=source).run(
                project=args.project,
                provider=args.provider,
                session_id_hash=args.session_id_hash,
            )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


def _run_process_dirty(args: argparse.Namespace) -> int:
    if args.enqueue or args.ingress_url or args.approval:
        return _print_live_blocked("process-dirty")
    try:
        ledger = _open_read_only_ledger(args.ledger)
        report = ProjectMemoryRegenerationRunner.process_dirty_projects(
            ledger=ledger,
            enqueue=False,
            target_profile=args.target_profile or DEFAULT_PROJECT_MEMORY_TARGET_PROFILE,
            limit=args.limit,
            quiet_period_seconds=args.quiet_period_seconds,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = _strip_program(list(sys.argv[1:] if argv is None else argv))
    parser = _build_parser()
    args = parser.parse_args(raw_argv)
    if args.command == "run":
        return _run_memory_regeneration(args)
    if args.command == "build-session-memory":
        return _run_build_session_memory(args)
    if args.command == "process-dirty":
        return _run_process_dirty(args)
    return _print_live_blocked(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
