"""Neuron-owned session-memory build-state helpers.

This slice owns the read-only shadow-log scan and neuron-local dirty-session
seed primitive. The live build loop remains fail-closed until its server
runtime contract is approved.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from ..ledger import Ledger
from ..ragflow_client import _transcript_memory_meta_for_document

TRANSCRIPT_MEMORY_TARGET_PROFILE = "ragflow-transcript-memory"
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


def seed_dirty_session_memory_from_deliveries(
    deliveries,
    *,
    ragflow,
    ledger: Ledger,
    dataset_ids,
) -> dict:
    """Seed neuron-local dirty session-memory rows behind delivered documents."""
    seen_sessions: set[str] = set()
    new_watermark = ""
    seeded = 0
    for delivery in deliveries or []:
        updated_at = str(delivery.get("updated_at") or "")
        if updated_at > new_watermark:
            new_watermark = updated_at
        document_ref = str(delivery.get("document_ref") or "")
        if not document_ref:
            continue
        meta = _transcript_memory_meta_for_document(ragflow, dataset_ids, document_ref)
        if not meta:
            continue
        meta_type = meta.get("result_type") or meta.get("type") or meta.get("kind")
        if meta_type != "conversation_chunk":
            continue
        session_id_hash = str(meta.get("session_id_hash") or "")
        if not session_id_hash or session_id_hash in seen_sessions:
            continue
        seen_sessions.add(session_id_hash)
        ledger.mark_session_memory_dirty(
            session_id_hash=session_id_hash,
            provider=str(meta.get("provider") or ""),
            project=str(meta.get("project") or ""),
            reason="neuron_shadow_log_delivery",
            source_knowledge_id=str(meta.get("knowledge_id") or document_ref),
        )
        seeded += 1
    return {
        "seeded_sessions": seeded,
        "new_watermark": new_watermark,
        "session_id_hashes": sorted(seen_sessions),
    }


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
    config,
    token: str,
    shadow_db_path: str | Path,
    watermark_path: str | Path,
    transcript_dataset_name: str = "transcript-memory",
    log=None,
) -> dict:
    """One neuron build cycle: seed dirty from the worker shadow log, then build.

    ``config`` must carry ``transcript_read_source="ragflow_read_sot"`` and a
    *neuron-local* ``ledger_path`` (build-state only; the Mac ledger is not
    used). Reuses :class:`DirtySessionMemorySyncRunner` to build + promote the
    seeded sessions from RAGFlow read-SoT. The watermark advances only after a
    successful seed scan so deliveries are processed at-least-once.
    """
    from ..ragflow_client import RagflowHttpClient
    from .dirty_session_memory_sync import DirtySessionMemorySyncRunner

    emit = log or (lambda event: None)
    ragflow = RagflowHttpClient(base_url=config.ragflow_url, bearer_token=token, request_timeout_seconds=30)
    dataset_ids = [
        str(dataset.get("id") or "")
        for dataset in ragflow.list_datasets(name=transcript_dataset_name)
        if dataset.get("id")
    ]
    watermark = read_watermark(watermark_path)
    deliveries = read_recent_transcript_deliveries(shadow_db_path, since_watermark=watermark)
    ledger = Ledger(config.ledger_path)
    seed = seed_dirty_session_memory_from_deliveries(
        deliveries, ragflow=ragflow, ledger=ledger, dataset_ids=dataset_ids
    )
    emit({"event": "neuron_seed", **seed, "scanned": len(deliveries)})
    build = DirtySessionMemorySyncRunner(config=config, token=token, log=emit).run()
    if seed["new_watermark"] and seed["new_watermark"] > watermark:
        write_watermark(watermark_path, seed["new_watermark"])
    return {"seed": seed, "build": build}


def _strip_program(argv: list[str]) -> list[str]:
    if argv and argv[0] == COMMAND:
        return argv[1:]
    return argv


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=COMMAND)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--shadow-db")
    parser.add_argument("--watermark-file")
    parser.add_argument("--target-profile", default=TRANSCRIPT_MEMORY_TARGET_PROFILE)
    parser.add_argument("--limit", type=int, default=500)

    # Legacy live-build arguments are parsed so old invocations fail closed in a
    # structured way. They are not used by the dry-run planner.
    parser.add_argument("--ledger", default="")
    parser.add_argument("--dataset-id", default="")
    parser.add_argument("--dataset-name", default="session-memory")
    parser.add_argument("--transcript-dataset-name", default="transcript-memory")
    parser.add_argument("--ragflow-url", default="")
    parser.add_argument("--token-env", default="")
    parser.add_argument("--runtime-dir", default="")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--max-processed-per-run", type=int, default=25)
    parser.add_argument("--approval", default="")
    return parser


def _print_report(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


def _blocked_report(reason: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "blocked_live_execution",
        "command": COMMAND,
        "reason": reason,
        "mode": "blocked",
        "mutation_performed": False,
        "network_used": False,
        "ragflow_write_performed": False,
        "raw_ids_printed": False,
        "raw_paths_printed": False,
    }


def _has_live_args(args: argparse.Namespace) -> bool:
    return any([args.ledger, args.dataset_id, args.ragflow_url, args.token_env, args.runtime_dir, args.approval])


def _run_dry_run(args: argparse.Namespace) -> int:
    if not args.shadow_db or not args.watermark_file:
        print("--shadow-db and --watermark-file are required for dry-run", file=sys.stderr)
        return 2
    if _has_live_args(args):
        _print_report(_blocked_report("legacy live build arguments are not accepted in dry-run mode"))
        return 1

    watermark = read_watermark(args.watermark_file)
    deliveries = read_recent_transcript_deliveries(
        args.shadow_db,
        since_watermark=watermark,
        target_profile=args.target_profile,
        limit=args.limit,
    )
    new_watermark = max((str(item.get("updated_at") or "") for item in deliveries), default=watermark)
    _print_report(
        {
            "schema_version": SCHEMA_VERSION,
            "status": "dry_run_complete",
            "command": COMMAND,
            "mode": "dry_run",
            "mutation_performed": False,
            "network_used": False,
            "ragflow_write_performed": False,
            "raw_ids_printed": False,
            "raw_paths_printed": False,
            "target_profile": args.target_profile,
            "limit": max(int(args.limit), 1),
            "deliveries_seen": len(deliveries),
            "current_watermark": watermark,
            "planned_new_watermark": new_watermark,
        }
    )
    return 0


def _run_live(args: argparse.Namespace, raw_argv: list[str]) -> int:
    """Approved live build: validate the runtime contract, take a non-blocking
    flock, then seed+build via the vendored :class:`DirtySessionMemorySyncRunner`
    (RAGFlow read-SoT). The flock lives in the build entrypoint so cron
    re-invocations skip instead of piling up -- the prior pileup came from the
    build path bypassing the lock.
    """
    import fcntl
    import os

    from ..ragflow_client import RagflowHttpClient
    from .dirty_session_memory_sync import DirtySessionMemorySyncConfig, resolve_dataset_id
    from .native_memory_sync_approval import ApprovalError, validate_memory_enqueue_approval

    for required in ("ledger", "ragflow_url", "token_env", "runtime_dir", "shadow_db", "watermark_file", "approval"):
        if not getattr(args, required):
            print(f"--{required.replace('_', '-')} is required for live build", file=sys.stderr)
            return 2
    token = os.environ.get(args.token_env)
    if not token:
        print("token env is not set", file=sys.stderr)
        return 2
    # Validate the runtime contract before any network so an unapproved or
    # mismatched invocation fails closed without touching RAGFlow.
    try:
        validate_memory_enqueue_approval(
            args.approval,
            operation=NEURON_SESSION_MEMORY_BUILD_OPERATION,
            command_argv=[COMMAND, *raw_argv],
        )
    except (ApprovalError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    resolver = RagflowHttpClient(base_url=args.ragflow_url, bearer_token=token, request_timeout_seconds=15)
    try:
        dataset_id = resolve_dataset_id(ragflow=resolver, dataset_id=args.dataset_id, dataset_name=args.dataset_name)
    except Exception as exc:  # noqa: BLE001 - resolution failure must fail closed, not crash
        print(f"dataset resolution failed: {exc}", file=sys.stderr)
        return 2

    runtime_dir = Path(args.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock_handle = (runtime_dir / "run.lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _print_report(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "already_running",
                "command": COMMAND,
                "mode": "live",
                "mutation_performed": False,
            }
        )
        return 0

    config = DirtySessionMemorySyncConfig(
        ledger_path=Path(args.ledger),
        dataset_id=dataset_id,
        ragflow_url=args.ragflow_url,
        runtime_dir=runtime_dir,
        batch_size=args.batch_size,
        max_processed_per_run=args.max_processed_per_run,
        transcript_read_source="ragflow_read_sot",
    )
    report = run_neuron_session_memory_build_once(
        config=config,
        token=token,
        shadow_db_path=args.shadow_db,
        watermark_path=args.watermark_file,
    )
    _print_report(
        {
            "schema_version": SCHEMA_VERSION,
            "status": "ok",
            "command": COMMAND,
            "mode": "live",
            "seed": report["seed"],
            "build": report["build"],
        }
    )
    return 0 if report["build"].get("status") == "ok" else 1


def main(argv: list[str] | None = None) -> int:
    raw_argv = _strip_program(list(sys.argv[1:] if argv is None else argv))
    parser = _build_parser()
    args = parser.parse_args(raw_argv)
    if args.dry_run:
        return _run_dry_run(args)
    return _run_live(args, raw_argv)


if __name__ == "__main__":
    raise SystemExit(main())
