"""Neuron-owned session-memory build-state helpers.

The legacy build lane read its transcript-memory source-of-truth from the
retired external index bridge and uploaded the regenerated session-memory
document back to it. That backend is retired and its client package is gone,
so the live write path — and the shadow-log seeding that needed the bridge's
document metadata — no longer exist.

What survives is the read-only side: the worker shadow-log scan, the build
watermark, and a plan of the session-memory documents the dirty ledger
sessions would produce. The live builder that replaced this lane is the
CouchDB-native ``couchdb-session-memory-build`` command.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .dirty_session_memory_sync import (
    DirtySessionMemorySyncConfig,
    DirtySessionMemorySyncRunner,
)

TRANSCRIPT_MEMORY_TARGET_PROFILE = "index-transcript-memory"
SCHEMA_VERSION = "agent_knowledge_neuron_session_memory.v1"
COMMAND = "neuron-session-memory-build"
NEURON_SESSION_MEMORY_BUILD_OPERATION = "neuron_session_memory_build"


def read_recent_transcript_deliveries(
    shadow_db_path: str | Path,
    *,
    since_watermark: str = "",
    target_profile: str = TRANSCRIPT_MEMORY_TARGET_PROFILE,
    limit: int = 500,
) -> list[dict]:
    """Read recently delivered transcript-memory rows from the worker shadow log."""
    uri = f"file:{Path(shadow_db_path)}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT document_ref, updated_at
            FROM shadow_ingest_log
            WHERE target_profile = ?
              AND delivered = 1
              AND document_ref != ''
              AND updated_at > ?
            ORDER BY updated_at ASC
            LIMIT ?
            """,
            (target_profile, since_watermark, max(int(limit), 1)),
        ).fetchall()
        return [{"document_ref": str(row["document_ref"]), "updated_at": str(row["updated_at"])} for row in rows]
    finally:
        connection.close()


def read_watermark(path: str | Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def write_watermark(path: str | Path, value: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(str(value), encoding="utf-8")
    tmp.replace(target)


def run_neuron_session_memory_build_once(
    *,
    config: DirtySessionMemorySyncConfig,
    shadow_db_path: str | Path,
    watermark_path: str | Path,
    delivery_limit: int = 500,
    log=None,
) -> dict:
    """One read-only build-plan cycle.

    The retired external index bridge was the only write target of the legacy
    lane, so this cycle plans instead of writing: it scans the worker shadow
    log for delivered transcript documents and plans the session-memory
    document of every actionable dirty ledger session. No network call, no
    index write and no ledger mutation is performed, and the watermark is not
    advanced because nothing was seeded.
    """
    emit = log or (lambda event: None)
    watermark = read_watermark(watermark_path)
    deliveries = read_recent_transcript_deliveries(
        shadow_db_path,
        since_watermark=watermark,
        limit=delivery_limit,
    )
    runner = DirtySessionMemorySyncRunner(config=config, log=emit)
    build = runner.run()
    return {
        "deliveries": {
            "deliveries_seen": len(deliveries),
            "current_watermark": watermark,
            "raw_ids_printed": False,
        },
        "build": build,
    }


def _strip_program(argv: list[str]) -> list[str]:
    if argv and argv[0] == COMMAND:
        return argv[1:]
    return argv


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=COMMAND)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--probe-meta", action="store_true")
    parser.add_argument("--shadow-db")
    parser.add_argument("--watermark-file")
    parser.add_argument("--ledger", default="")
    parser.add_argument("--target-profile", default=TRANSCRIPT_MEMORY_TARGET_PROFILE)
    parser.add_argument("--limit", type=int, default=500)

    # Legacy live-build arguments are parsed so old invocations fail closed in a
    # structured way. They are not used by the read-only planner.
    parser.add_argument("--dataset-id", default="")
    parser.add_argument("--dataset-name", default="session-memory")
    parser.add_argument("--transcript-dataset-name", default="transcript-memory")
    parser.add_argument("--retired-index-bridge-url", default="")
    parser.add_argument("--retired-index-bridge-token-env", default="")
    parser.add_argument("--runtime-dir", default="")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--max-processed-per-run", type=int, default=25)
    parser.add_argument("--approval", default="")
    return parser


def _print_report(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


def _blocked_report(reason: str, *, failed_error_class: str) -> dict:
    """Retired-lane block report.

    Mirrors the fail-closed shape the retired native-memory writer used: the
    reason is stated, and the report never claims a mutation or a network call.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "blocked_live_execution",
        "command": COMMAND,
        "reason": reason,
        "mode": "blocked",
        "mutation_performed": False,
        "network_used": False,
        "index_write_performed": False,
        "raw_ids_printed": False,
        "raw_paths_printed": False,
        "failed_error_class": failed_error_class,
    }


def _has_live_args(args: argparse.Namespace) -> bool:
    return any(
        [
            args.dataset_id,
            args.retired_index_bridge_url,
            args.retired_index_bridge_token_env,
            args.runtime_dir,
            args.approval,
        ]
    )


def _run_dry_run(args: argparse.Namespace) -> int:
    if not args.shadow_db or not args.watermark_file:
        print("--shadow-db and --watermark-file are required for dry-run", file=sys.stderr)
        return 2
    if args.probe_meta:
        _print_report(
            _blocked_report(
                "transcript-memory metadata probing required the retired external index bridge",
                failed_error_class="retired_index_bridge_metadata_probe_not_vendored",
            )
        )
        return 1
    if _has_live_args(args):
        _print_report(
            _blocked_report(
                "legacy live build arguments are not accepted in dry-run mode",
                failed_error_class="legacy_live_build_arguments_not_accepted",
            )
        )
        return 1

    watermark = read_watermark(args.watermark_file)
    deliveries = read_recent_transcript_deliveries(
        args.shadow_db,
        since_watermark=watermark,
        target_profile=args.target_profile,
        limit=args.limit,
    )
    new_watermark = max(
        (str(item.get("updated_at") or "") for item in deliveries),
        default=watermark,
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "dry_run_complete",
        "command": COMMAND,
        "mode": "dry_run",
        "mutation_performed": False,
        "network_used": False,
        "index_write_performed": False,
        "raw_ids_printed": False,
        "raw_paths_printed": False,
        "target_profile": args.target_profile,
        "limit": max(int(args.limit), 1),
        "deliveries_seen": len(deliveries),
        "current_watermark": watermark,
        "planned_new_watermark": new_watermark,
    }
    if args.ledger:
        config = DirtySessionMemorySyncConfig(
            ledger_path=Path(args.ledger),
            batch_size=args.batch_size,
            max_processed_per_run=args.max_processed_per_run,
        )
        report["build"] = run_neuron_session_memory_build_once(
            config=config,
            shadow_db_path=args.shadow_db,
            watermark_path=args.watermark_file,
            delivery_limit=args.limit,
        )["build"]
    _print_report(report)
    return 0


def _run_live(args: argparse.Namespace, raw_argv: list[str]) -> int:
    """Approved live build.

    The retired external index bridge was this lane's only write target, so
    the live path is retired. The approval contract is still validated first so
    an unapproved invocation fails closed with the same structured error as
    before, and the lane then reports that the CouchDB-native builder replaced
    it.
    """
    from .native_memory_sync_approval import ApprovalError, validate_memory_enqueue_approval

    for required in ("ledger", "runtime_dir", "shadow_db", "watermark_file", "approval"):
        if not getattr(args, required):
            print(f"--{required.replace('_', '-')} is required for live build", file=sys.stderr)
            return 2
    try:
        validate_memory_enqueue_approval(
            args.approval,
            operation=NEURON_SESSION_MEMORY_BUILD_OPERATION,
            command_argv=[COMMAND, *raw_argv],
        )
    except (ApprovalError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    _print_report(
        _blocked_report(
            "live session-memory build required the retired external index bridge; "
            "use the CouchDB-native couchdb-session-memory-build command",
            failed_error_class="live_neuron_session_memory_build_not_vendored",
        )
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    raw_argv = _strip_program(list(sys.argv[1:] if argv is None else argv))
    parser = _build_parser()
    args = parser.parse_args(raw_argv)
    if args.dry_run:
        return _run_dry_run(args)
    return _run_live(args, raw_argv)


if __name__ == "__main__":
    raise SystemExit(main())
