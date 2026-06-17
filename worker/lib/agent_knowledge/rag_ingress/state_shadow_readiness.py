"""Read-only shadow readiness checks for the M6 state DB gate.

This module deliberately opens existing SQLite files in immutable read-only mode.
It must not instantiate ``RAGIngressStateDB`` because that path creates or
migrates schema.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


EXPECTED_STATE_TABLES = (
    "inbox_events",
    "commands",
    "domain_records",
    "command_results",
    "delivery_jobs",
)

OPEN_DELIVERY_STATUSES = {"pending", "executing", "failed_retryable", "replayable"}
BLOCKING_DELIVERY_STATUSES = {"failed_retryable", "quarantined", "replayable", "stale"}
KNOWN_QUEUE_BUCKETS = {
    "acked",
    "dead-letter",
    "deadLetter",
    "failed",
    "in-flight",
    "inFlight",
    "pending",
    "processing",
    "quarantine",
    "redelivered",
    "retry",
}
OPEN_QUEUE_BUCKETS = {"pending", "processing", "in-flight", "inFlight", "redelivered", "retry"}
BLOCKING_QUEUE_BUCKETS = {"dead-letter", "deadLetter", "failed", "quarantine", "other", "root"}

EXTERNAL_GATES = (
    "mark_done_packet",
    "backend_not_done_packet",
    "replay_quarantine_packet",
    "duplicate_dedupe_quarantine_packet",
    "operator_exact_argv_approval",
    "production_18080_component_identity_review",
)

LEGACY_PARITY_DISPOSITION_SCHEMA = "agent_knowledge_rag_ingress_legacy_parity_disposition.v1"


def build_state_shadow_readiness_report(
    *,
    state_db_path: Path | str,
    legacy_ledger_path: Path | str,
    queue_root: Path | str,
    dry_run: bool,
    redact_paths: bool,
    max_runtime_seconds: float = 300.0,
    repo_root: Path | str | None = None,
    soak_state: dict | None = None,
    legacy_disposition: dict | None = None,
    now_iso: str | None = None,
) -> dict:
    """Build a no-mutation M6 shadow-readiness report.

    The report intentionally contains only booleans, counts, status classes, and
    a digest of normalized parity counts. It never includes raw filesystem paths
    or row identifiers.

    ``soak_state`` carries the prior run's soak aggregate
    (``consecutive_green_runs`` / ``soak_window_start``) so a parity-soak loop runner
    can accumulate continuous-green evidence across runs: a green run increments the
    counter and preserves the window start; any blocked run resets both to zero. The
    runner that threads this state between runs is M6.5 operator scope; this function
    only computes the next soak aggregate from the prior one.
    """

    if not dry_run:
        raise ValueError("shadow-readiness requires --dry-run")
    if not redact_paths:
        raise ValueError("shadow-readiness requires --redact-paths")
    if max_runtime_seconds <= 0:
        raise ValueError("max-runtime-seconds must be positive")

    started = time.monotonic()
    deadline = started + max_runtime_seconds
    blockers: list[dict] = []
    state_db = _inspect_state_db(Path(state_db_path), blockers, repo_root=repo_root)
    _check_deadline(deadline, blockers)
    ledger = _inspect_legacy_ledger(Path(legacy_ledger_path), blockers)
    _check_deadline(deadline, blockers)
    source_queue = _inspect_source_queue(Path(queue_root), blockers, deadline=deadline)
    _append_source_queue_blockers(source_queue, blockers)

    parity_summary = _build_parity_summary(
        legacy_queued_count=ledger.get("queued_ingress_count"),
        shadow_unconsumed_inbox_count=state_db.get("unconsumed_inbox_count"),
        source_queue_open_work_count=source_queue.get("open_work_json_file_count"),
        delivery_status_counts=state_db.get("delivery_status_counts", {}),
        inbox_accept_outcome_counts=state_db.get("inbox_accept_outcome_counts", {}),
    )
    legacy_disposition_summary = _evaluate_legacy_disposition(
        legacy_disposition,
        parity_summary=parity_summary,
    )
    parity_summary["legacy_shadow_mismatch_dispositioned"] = bool(
        legacy_disposition_summary.get("accepted")
    )
    parity_summary["legacy_disposition_status"] = legacy_disposition_summary["status"]
    _append_parity_blockers(parity_summary, blockers)
    _append_state_status_blockers(state_db, blockers)

    blocking_codes = {str(blocker.get("code") or "") for blocker in blockers}
    elapsed_ms = int((time.monotonic() - started) * 1000)
    shadow_status = (
        "shadow_readiness_blocked"
        if blockers
        else "shadow_ready_pending_external_gates"
    )

    green_this_run = not blockers
    prior_soak = soak_state or {}
    prior_consecutive = int(prior_soak.get("consecutive_green_runs") or 0)
    prior_window_start = str(prior_soak.get("soak_window_start") or "")
    if green_this_run:
        consecutive_green_runs = prior_consecutive + 1
        soak_window_start = prior_window_start or (now_iso or datetime.now(timezone.utc).isoformat())
    else:
        consecutive_green_runs = 0
        soak_window_start = ""
    soak = {
        "green_this_run": green_this_run,
        "consecutive_green_runs": consecutive_green_runs,
        "soak_window_start": soak_window_start,
    }

    return {
        "schema_version": "agent_knowledge_rag_ingress_state_shadow_readiness.v1",
        "dry_run": True,
        "redacted_paths": True,
        "network_used": False,
        "mutation_performed": False,
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
        "elapsed_ms": elapsed_ms,
        "max_runtime_seconds": max_runtime_seconds,
        "current_authority": "legacy_file_queue_and_legacy_ledger",
        "production_authority_status": "NO-GO",
        "cutover_status": "cutover_blocked",
        "shadow_readiness_status": shadow_status,
        "soak": soak,
        "state_db_candidate": state_db,
        "legacy_ledger": ledger,
        "source_queue": source_queue,
        "parity_summary": parity_summary,
        "legacy_disposition": legacy_disposition_summary,
        "external_gates": [
            {
                "gate": gate,
                "status": "not_evaluated_by_this_command",
                "required_before_production_authority": True,
            }
            for gate in EXTERNAL_GATES
        ],
        "blockers": blockers,
        "blocking_codes": sorted(code for code in blocking_codes if code),
    }


def _inspect_state_db(path: Path, blockers: list[dict], *, repo_root: Path | str | None) -> dict:
    parent = path.parent
    path_exists = path.exists()
    parent_exists = parent.exists()
    parent_private = _is_private_directory(parent)
    parent_is_symlink = parent.is_symlink()
    file_is_symlink = path.is_symlink()
    outside_repo = _is_outside_repo(path, repo_root)

    if not path_exists:
        _block(blockers, "state_db_candidate_missing")
    if parent_is_symlink:
        _block(blockers, "state_db_parent_symlink")
    if file_is_symlink:
        _block(blockers, "state_db_file_symlink")
    if parent_exists and not parent_private:
        _block(blockers, "state_db_parent_not_private")
    if not outside_repo:
        _block(blockers, "state_db_under_repo")

    table_exists = {name: False for name in EXPECTED_STATE_TABLES}
    table_counts: dict[str, int | None] = {name: None for name in EXPECTED_STATE_TABLES}
    delivery_status_counts: dict[str, int] = {}
    inbox_accept_outcome_counts: dict[str, int] = {}
    expired_lease_count = 0
    unconsumed_inbox_count: int | None = None
    read_only_open = False

    if path_exists:
        try:
            with _connect_sqlite_immutable(path) as connection:
                read_only_open = True
                for table in EXPECTED_STATE_TABLES:
                    exists = _table_exists(connection, table)
                    table_exists[table] = exists
                    if exists:
                        table_counts[table] = _count_rows(connection, table)
                if table_exists["inbox_events"]:
                    unconsumed_inbox_count = _count_where(
                        connection,
                        "inbox_events",
                        "COALESCE(consumed_by_command_id, '') = ''",
                    )
                    inbox_accept_outcome_counts = _group_counts(connection, "inbox_events", "accept_outcome")
                if table_exists["delivery_jobs"]:
                    delivery_status_counts = _group_counts(connection, "delivery_jobs", "status")
                    expired_lease_count = _expired_delivery_lease_count(connection)
        except sqlite3.Error:
            _block(blockers, "state_db_read_only_open_failed")

    missing_tables = [name for name, exists in table_exists.items() if not exists]
    for table in missing_tables:
        _block(blockers, "state_db_schema_missing_table", table=table)

    return {
        "path_supplied": True,
        "exists": path_exists,
        "parent_exists": parent_exists,
        "parent_private": parent_private,
        "parent_is_symlink": parent_is_symlink,
        "file_is_symlink": file_is_symlink,
        "outside_repo": outside_repo,
        "read_only_open": read_only_open,
        "schema_valid": read_only_open and not missing_tables,
        "tables": {
            name: {"exists": table_exists[name], "count": table_counts[name]}
            for name in EXPECTED_STATE_TABLES
        },
        "missing_tables": missing_tables,
        "unconsumed_inbox_count": unconsumed_inbox_count,
        "delivery_status_counts": delivery_status_counts,
        "inbox_accept_outcome_counts": inbox_accept_outcome_counts,
        "expired_lease_count": expired_lease_count,
    }


def _inspect_legacy_ledger(path: Path, blockers: list[dict]) -> dict:
    exists = path.exists()
    parent_private = _is_private_directory(path.parent)
    parent_is_symlink = path.parent.is_symlink()
    read_only_open = False
    schema_valid = False
    queued_ingress_count: int | None = None

    if not exists:
        _block(blockers, "legacy_ledger_missing")
    if parent_is_symlink:
        _block(blockers, "legacy_ledger_parent_symlink")
    if path.parent.exists() and not parent_private:
        _block(blockers, "legacy_ledger_parent_not_private")

    if exists:
        try:
            with _connect_sqlite_immutable(path) as connection:
                read_only_open = True
                schema_valid = _table_exists(connection, "knowledge_items")
                if schema_valid and _table_has_columns(
                    connection,
                    "knowledge_items",
                    ("status", "ingress_job_id"),
                ):
                    queued_ingress_count = _count_where(
                        connection,
                        "knowledge_items",
                        "status = 'queued' AND COALESCE(ingress_job_id, '') != ''",
                    )
                else:
                    schema_valid = False
        except sqlite3.Error:
            _block(blockers, "legacy_ledger_read_only_open_failed")

    if exists and not schema_valid:
        _block(blockers, "legacy_ledger_schema_unavailable")

    return {
        "path_supplied": True,
        "exists": exists,
        "parent_private": parent_private,
        "parent_is_symlink": parent_is_symlink,
        "read_only_open": read_only_open,
        "schema_valid": schema_valid,
        "queued_ingress_count": queued_ingress_count,
    }


def _inspect_source_queue(path: Path, blockers: list[dict], *, deadline: float) -> dict:
    exists = path.exists()
    is_symlink = path.is_symlink()
    status_counts: dict[str, int] = {}
    json_file_count = 0
    open_work_json_file_count = 0
    scan_complete = True

    if not exists:
        _block(blockers, "source_queue_root_missing")
    if is_symlink:
        _block(blockers, "source_queue_root_symlink")

    if exists and not is_symlink:
        try:
            for candidate in sorted(path.rglob("*.json")):
                if time.monotonic() > deadline:
                    scan_complete = False
                    _block(blockers, "max_runtime_exceeded")
                    break
                if not candidate.is_file():
                    continue
                json_file_count += 1
                try:
                    relative_parts = candidate.relative_to(path).parts
                except (IndexError, ValueError):
                    relative_parts = ()
                first_part = relative_parts[0] if len(relative_parts) > 1 else "root"
                status = first_part if first_part in KNOWN_QUEUE_BUCKETS else "other"
                status_counts[status] = status_counts.get(status, 0) + 1
                if status in OPEN_QUEUE_BUCKETS:
                    open_work_json_file_count += 1
        except OSError:
            scan_complete = False
            _block(blockers, "source_queue_scan_failed")

    return {
        "path_supplied": True,
        "exists": exists,
        "is_symlink": is_symlink,
        "json_file_count": json_file_count,
        "open_work_json_file_count": open_work_json_file_count,
        "status_counts": dict(sorted(status_counts.items())),
        "scan_complete": scan_complete,
    }


def _build_parity_summary(
    *,
    legacy_queued_count: object,
    shadow_unconsumed_inbox_count: object,
    source_queue_open_work_count: object,
    delivery_status_counts: object,
    inbox_accept_outcome_counts: object,
) -> dict:
    delivery_counts = {
        str(key): int(value)
        for key, value in dict(delivery_status_counts or {}).items()
    }
    accept_counts = {
        str(key): int(value)
        for key, value in dict(inbox_accept_outcome_counts or {}).items()
    }
    legacy_count = _int_or_none(legacy_queued_count)
    shadow_count = _int_or_none(shadow_unconsumed_inbox_count)
    queue_open_count = _int_or_none(source_queue_open_work_count)
    open_delivery_count = sum(delivery_counts.get(status, 0) for status in OPEN_DELIVERY_STATUSES)
    shadow_open_work_count = (shadow_count or 0) + open_delivery_count if shadow_count is not None else None
    queued_match = legacy_count == shadow_count if legacy_count is not None and shadow_count is not None else None
    queue_match = (
        queue_open_count == shadow_open_work_count
        if queue_open_count is not None and shadow_open_work_count is not None
        else None
    )
    summary = {
        "legacy_queued_count": legacy_count,
        "shadow_unconsumed_inbox_count": shadow_count,
        "source_queue_open_work_count": queue_open_count,
        "open_delivery_count": open_delivery_count,
        "shadow_open_work_count": shadow_open_work_count,
        "legacy_queued_equals_shadow_unconsumed_inbox": queued_match,
        "source_queue_represented_by_shadow_open_work": queue_match,
        "blocking_delivery_status_counts": {
            status: delivery_counts.get(status, 0)
            for status in sorted(BLOCKING_DELIVERY_STATUSES)
            if delivery_counts.get(status, 0)
        },
        "duplicate_inbox_count": accept_counts.get("duplicate", 0),
        "conflict_inbox_count": accept_counts.get("conflict", 0),
    }
    summary["digest"] = _digest(summary)
    return summary


def _append_parity_blockers(summary: dict, blockers: list[dict]) -> None:
    if summary["legacy_queued_equals_shadow_unconsumed_inbox"] is None:
        _block(blockers, "legacy_shadow_queued_parity_unavailable")
    elif (
        not summary["legacy_queued_equals_shadow_unconsumed_inbox"]
        and not summary.get("legacy_shadow_mismatch_dispositioned")
    ):
        _block(blockers, "legacy_shadow_queued_count_mismatch")
    if summary.get("legacy_disposition_status") == "invalid":
        _block(blockers, "legacy_disposition_packet_invalid")

    if summary["source_queue_represented_by_shadow_open_work"] is None:
        _block(blockers, "queue_shadow_open_work_parity_unavailable")
    elif not summary["source_queue_represented_by_shadow_open_work"]:
        _block(blockers, "queue_shadow_open_work_mismatch")

    if summary["duplicate_inbox_count"]:
        _block(blockers, "duplicate_bucket_requires_dedupe_packet")
    if summary["conflict_inbox_count"]:
        _block(blockers, "conflict_bucket_requires_quarantine_packet")
    for status, count in summary["blocking_delivery_status_counts"].items():
        if count:
            _block(blockers, "blocking_delivery_status_unresolved", status=status)


def _append_state_status_blockers(state_db: dict, blockers: list[dict]) -> None:
    if int(state_db.get("expired_lease_count") or 0):
        _block(blockers, "expired_local_lease_requires_reconcile")


def _append_source_queue_blockers(source_queue: dict, blockers: list[dict]) -> None:
    for status, count in dict(source_queue.get("status_counts") or {}).items():
        if status in BLOCKING_QUEUE_BUCKETS and int(count or 0):
            _block(blockers, "blocking_source_queue_bucket_unresolved", status=status)


def _evaluate_legacy_disposition(disposition: dict | None, *, parity_summary: dict) -> dict:
    if disposition is None:
        return {
            "supplied": False,
            "accepted": False,
            "status": "not_supplied",
            "reason": "",
            "schema_valid": False,
            "parity_digest_matches": False,
            "raw_paths_printed": False,
            "raw_ids_printed": False,
            "raw_content_printed": False,
        }
    if not isinstance(disposition, dict):
        return _invalid_legacy_disposition("packet_not_object")

    if disposition.get("schema_version") != LEGACY_PARITY_DISPOSITION_SCHEMA:
        return _invalid_legacy_disposition("schema_version_mismatch")
    if disposition.get("disposition_status") != "accepted":
        return _invalid_legacy_disposition("disposition_status_not_accepted")
    if disposition.get("redacted_paths") is not True:
        return _invalid_legacy_disposition("redacted_paths_required")
    if disposition.get("raw_paths_printed") is not False:
        return _invalid_legacy_disposition("raw_paths_flag_required_false")
    if disposition.get("raw_ids_printed") is not False:
        return _invalid_legacy_disposition("raw_ids_flag_required_false")
    if disposition.get("raw_content_printed") is not False:
        return _invalid_legacy_disposition("raw_content_flag_required_false")

    target = disposition.get("target") or {}
    expected_digest = str(target.get("parity_digest") or "")
    if not expected_digest or expected_digest != parity_summary.get("digest"):
        return _invalid_legacy_disposition("parity_digest_mismatch")

    expected_counts = {
        "legacy_queued_count": parity_summary.get("legacy_queued_count"),
        "shadow_unconsumed_inbox_count": parity_summary.get("shadow_unconsumed_inbox_count"),
        "source_queue_open_work_count": parity_summary.get("source_queue_open_work_count"),
        "shadow_open_work_count": parity_summary.get("shadow_open_work_count"),
    }
    for key, expected in expected_counts.items():
        if _int_or_none(target.get(key)) != expected:
            return _invalid_legacy_disposition(f"{key}_mismatch")

    return {
        "supplied": True,
        "accepted": True,
        "status": "accepted",
        "reason": str(disposition.get("reason") or ""),
        "schema_valid": True,
        "parity_digest_matches": True,
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
    }


def _invalid_legacy_disposition(reason: str) -> dict:
    return {
        "supplied": True,
        "accepted": False,
        "status": "invalid",
        "reason": reason,
        "schema_valid": False,
        "parity_digest_matches": False,
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
    }


def _connect_sqlite_immutable(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve(strict=False)
    connection = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON;")
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _table_has_columns(connection: sqlite3.Connection, table: str, columns: Iterable[str]) -> bool:
    existing = {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    return set(columns).issubset(existing)


def _count_rows(connection: sqlite3.Connection, table: str) -> int:
    row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row else 0


def _count_where(connection: sqlite3.Connection, table: str, where: str) -> int:
    row = connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()
    return int(row[0]) if row else 0


def _group_counts(connection: sqlite3.Connection, table: str, column: str) -> dict[str, int]:
    rows = connection.execute(
        f"""
        SELECT COALESCE({column}, '') AS bucket, COUNT(*) AS count
        FROM {table}
        GROUP BY COALESCE({column}, '')
        """
    ).fetchall()
    return {str(row["bucket"]): int(row["count"]) for row in rows}


def _expired_delivery_lease_count(connection: sqlite3.Connection) -> int:
    now = datetime.now(timezone.utc).isoformat()
    row = connection.execute(
        """
        SELECT COUNT(*)
        FROM delivery_jobs
        WHERE COALESCE(lease_owner, '') != ''
          AND COALESCE(lease_until, '') != ''
          AND lease_until < ?
          AND status NOT IN ('succeeded', 'quarantined')
        """,
        (now,),
    ).fetchone()
    return int(row[0]) if row else 0


def _is_private_directory(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    try:
        return (path.stat().st_mode & 0o077) == 0
    except OSError:
        return False


def _is_outside_repo(path: Path, repo_root: Path | str | None) -> bool:
    root = Path(repo_root).resolve(strict=False) if repo_root is not None else _default_repo_root()
    candidate = path.resolve(strict=False)
    try:
        candidate.relative_to(root)
        return False
    except ValueError:
        return True


def _default_repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "AGENTS.md").exists() and (parent / "capabilities").exists():
            return parent
    return Path.cwd().resolve(strict=False)


def _check_deadline(deadline: float, blockers: list[dict]) -> None:
    if time.monotonic() > deadline:
        _block(blockers, "max_runtime_exceeded")


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _digest(payload: dict) -> str:
    normalized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def _block(blockers: list[dict], code: str, **extra: object) -> None:
    blocker = {"code": code, "severity": "blocking"}
    blocker.update(extra)
    blockers.append(blocker)
