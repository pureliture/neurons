"""SQLite state foundation for server-owned RAG ingress command transactions.

This database stays separate from client ledgers and records the command, inbox,
domain projection, delivery-outbox, and replay payload contracts.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .idempotency import IdempotencyDecision, IdempotencyOutcome, classify_idempotency


SQLITE_BUSY_TIMEOUT_MS = 60000


class StateDBError(RuntimeError):
    pass


class InjectedTransactionFailure(StateDBError):
    pass


class ValidationRejected(StateDBError):
    def __init__(self, *, error_class: str, decision: str = "validation_failed"):
        super().__init__(error_class)
        self.error_class = error_class
        self.decision = decision


class StaleOwnerRejected(StateDBError):
    pass


@dataclass(frozen=True)
class DeliveryJobSpec:
    job_id: str
    target_profile: str
    document_kind: str
    idempotency_key: str
    payload_hash: str
    status: str = "pending"
    next_retry_at: str = ""


@dataclass(frozen=True)
class DomainRecordSpec:
    domain_record_id: str
    domain_kind: str
    lifecycle_status: str
    resource_id_hash: str = ""
    session_id_hash: str = ""
    payload_hash: str = ""
    payload_ref: str = ""
    projection: Mapping[str, object] | None = None


@dataclass(frozen=True)
class CommandResultSpec:
    decision: str
    result_payload_ref: str = ""
    domain_versions_written: Mapping[str, object] | None = None
    error_class: str = ""


@dataclass(frozen=True)
class CommandTransactionResult:
    command_id: str
    status: str
    delivery_job_ids: tuple[str, ...]


class ClosingSqliteConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return result


class RAGIngressStateDB:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._prepare_parent_directory()
        self._initialize()
        for candidate in self.path.parent.glob(f"{self.path.name}*"):
            try:
                os.chmod(candidate, 0o600)
            except OSError:
                pass

    def _prepare_parent_directory(self) -> None:
        parent = self.path.parent
        if parent.is_symlink():
            raise ValueError("state db parent must not be a symlink")
        existed = parent.exists()
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not existed:
            os.chmod(parent, 0o700)
            return
        if parent.stat().st_mode & 0o077:
            raise ValueError("state db parent must be private")

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
            factory=ClosingSqliteConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS};")
        connection.execute("PRAGMA foreign_keys=ON;")
        connection.execute("PRAGMA synchronous=NORMAL;")
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL;")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS inbox_events (
                    event_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    source TEXT DEFAULT '',
                    kind TEXT DEFAULT '',
                    target_profile TEXT DEFAULT '',
                    payload_ref TEXT DEFAULT '',
                    received_at TEXT NOT NULL,
                    persisted_at TEXT NOT NULL,
                    accept_outcome TEXT NOT NULL,
                    consumed_by_command_id TEXT DEFAULT '',
                    consumed_at TEXT DEFAULT '',
                    outcome_reason TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_inbox_events_idempotency_key
                    ON inbox_events(idempotency_key, created_at);

                CREATE TABLE IF NOT EXISTS commands (
                    command_id TEXT PRIMARY KEY,
                    command_type TEXT NOT NULL,
                    resource_id_hash TEXT DEFAULT '',
                    session_id_hash TEXT DEFAULT '',
                    idempotency_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    input_epoch TEXT DEFAULT '',
                    status TEXT NOT NULL,
                    lease_owner TEXT DEFAULT '',
                    lease_until TEXT DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error_class TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_commands_idempotency_key
                    ON commands(idempotency_key);
                CREATE INDEX IF NOT EXISTS idx_commands_status_lease
                    ON commands(status, lease_until);

                CREATE TABLE IF NOT EXISTS command_results (
                    result_id TEXT PRIMARY KEY,
                    command_id TEXT NOT NULL REFERENCES commands(command_id) ON DELETE RESTRICT,
                    decision TEXT NOT NULL,
                    result_payload_ref TEXT DEFAULT '',
                    domain_versions_written TEXT DEFAULT '{}',
                    error_class TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_command_results_command_id
                    ON command_results(command_id, created_at);

                CREATE TABLE IF NOT EXISTS domain_records (
                    domain_record_id TEXT PRIMARY KEY,
                    command_id TEXT NOT NULL REFERENCES commands(command_id) ON DELETE RESTRICT,
                    resource_id_hash TEXT DEFAULT '',
                    session_id_hash TEXT DEFAULT '',
                    domain_kind TEXT NOT NULL,
                    lifecycle_status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload_hash TEXT DEFAULT '',
                    payload_ref TEXT DEFAULT '',
                    projection_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_domain_records_resource_kind_version
                    ON domain_records(resource_id_hash, domain_kind, version);
                CREATE INDEX IF NOT EXISTS idx_domain_records_command_id
                    ON domain_records(command_id);

                CREATE TABLE IF NOT EXISTS delivery_jobs (
                    job_id TEXT PRIMARY KEY,
                    command_id TEXT NOT NULL REFERENCES commands(command_id) ON DELETE RESTRICT,
                    idempotency_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    target_profile TEXT NOT NULL,
                    document_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT DEFAULT '',
                    lease_owner TEXT DEFAULT '',
                    lease_until TEXT DEFAULT '',
                    ragflow_dataset_id TEXT DEFAULT '',
                    ragflow_document_id TEXT DEFAULT '',
                    ragflow_run TEXT DEFAULT '',
                    last_error_class TEXT DEFAULT '',
                    last_reconciled_at TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_jobs_idempotency_key
                    ON delivery_jobs(idempotency_key);
                CREATE INDEX IF NOT EXISTS idx_delivery_jobs_status_lease
                    ON delivery_jobs(status, lease_until);

                -- M8.1: redacted wire payload persistence so a pending delivery_jobs
                -- row can recover the document it must deliver (delivery_jobs itself
                -- stays hash-only). payload_json is the already-redacted
                -- rag_ingress_enqueue.v1 request body, keyed by the same
                -- idempotency_key as commands/delivery_jobs.
                CREATE TABLE IF NOT EXISTS delivery_payloads (
                    idempotency_key TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                """
            )

    def command_transaction(self) -> "CommandTransaction":
        return CommandTransaction(self)

    def record_inbox_shadow(
        self,
        payload: Mapping[str, object],
        *,
        payload_ref: str = "",
        event_id: str = "",
        now: datetime | None = None,
    ) -> IdempotencyDecision:
        idempotency_key = str(payload.get("idempotencyKey") or payload.get("idempotency_key") or "")
        payload_hash = str(payload.get("contentHash") or payload.get("payload_hash") or "")
        if not idempotency_key:
            raise ValueError("idempotencyKey is required")
        if not payload_hash:
            raise ValueError("contentHash is required")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM inbox_events
                WHERE idempotency_key = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (idempotency_key,),
            ).fetchone()
            decision = classify_idempotency(
                _row_to_dict(existing) if existing else None,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
            )
            stamp = _iso(now)
            connection.execute(
                """
                INSERT INTO inbox_events (
                    event_id, idempotency_key, payload_hash, source, kind, target_profile,
                    payload_ref, received_at, persisted_at, accept_outcome, outcome_reason,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id or _new_id("inbox"),
                    idempotency_key,
                    payload_hash,
                    _json_dumps(payload.get("source") or {}),
                    str(payload.get("kind") or ""),
                    str(payload.get("targetProfile") or payload.get("target_profile") or ""),
                    payload_ref,
                    stamp,
                    stamp,
                    decision.outcome,
                    decision.reason,
                    stamp,
                    stamp,
                ),
            )
            return decision

    def create_command(
        self,
        *,
        command_id: str,
        command_type: str,
        idempotency_key: str,
        payload_hash: str,
        resource_id_hash: str = "",
        session_id_hash: str = "",
        input_epoch: str = "",
        status: str = "pending",
        now: datetime | None = None,
    ) -> None:
        stamp = _iso(now)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO commands (
                    command_id, command_type, resource_id_hash, session_id_hash,
                    idempotency_key, payload_hash, input_epoch, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command_id,
                    command_type,
                    resource_id_hash,
                    session_id_hash,
                    idempotency_key,
                    payload_hash,
                    input_epoch,
                    status,
                    stamp,
                    stamp,
                ),
            )

    def claim_command(
        self,
        command_id: str,
        *,
        lease_owner: str,
        now: datetime | None = None,
        lease_seconds: int = 60,
        max_attempts: int = 3,
    ) -> bool:
        stamp_dt = now or _utc_now()
        stamp = _iso(stamp_dt)
        lease_until = _iso(stamp_dt + timedelta(seconds=lease_seconds))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM commands WHERE command_id = ?", (command_id,)).fetchone()
            if row is None:
                raise KeyError(command_id)
            row_dict = _row_to_dict(row)
            if _lease_is_live(row_dict, stamp_dt) and row_dict.get("lease_owner") not in {"", lease_owner}:
                return False
            attempt_count = int(row_dict.get("attempt_count") or 0) + 1
            if attempt_count > max_attempts:
                connection.execute(
                    """
                    UPDATE commands
                    SET status = 'quarantined', attempt_count = ?, last_error_class = ?,
                        updated_at = ?
                    WHERE command_id = ?
                    """,
                    (attempt_count, "lease_attempt_limit", stamp, command_id),
                )
                return False
            connection.execute(
                """
                UPDATE commands
                SET status = 'claimed', lease_owner = ?, lease_until = ?,
                    attempt_count = ?, updated_at = ?
                WHERE command_id = ?
                """,
                (lease_owner, lease_until, attempt_count, stamp, command_id),
            )
            return True

    def complete_claimed_command(
        self,
        command_id: str,
        *,
        lease_owner: str,
        now: datetime | None = None,
    ) -> bool:
        stamp_dt = now or _utc_now()
        stamp = _iso(stamp_dt)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM commands WHERE command_id = ?", (command_id,)).fetchone()
            if row is None:
                raise KeyError(command_id)
            row_dict = _row_to_dict(row)
            if row_dict.get("lease_owner") != lease_owner or not _lease_is_live(row_dict, stamp_dt):
                connection.execute(
                    """
                    UPDATE commands
                    SET last_error_class = ?, updated_at = ?
                    WHERE command_id = ?
                    """,
                    ("stale_owner_rejected", stamp, command_id),
                )
                return False
            connection.execute(
                "UPDATE commands SET status = 'completed', updated_at = ? WHERE command_id = ?",
                (stamp, command_id),
            )
            return True

    def create_delivery_job(
        self,
        *,
        job_id: str,
        command_id: str,
        idempotency_key: str,
        payload_hash: str,
        target_profile: str,
        document_kind: str,
        status: str = "pending",
        now: datetime | None = None,
    ) -> None:
        spec = DeliveryJobSpec(
            job_id=job_id,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            target_profile=target_profile,
            document_kind=document_kind,
            status=status,
        )
        stamp = _iso(now)
        with self.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM commands WHERE command_id = ?",
                (command_id,),
            ).fetchone() is None:
                raise StateDBError("delivery job must reference a committed command")
            existing = _find_delivery_job_by_idempotency_key(connection, spec.idempotency_key)
            decision = classify_idempotency(
                existing,
                idempotency_key=spec.idempotency_key,
                payload_hash=spec.payload_hash,
            )
            if decision.outcome == IdempotencyOutcome.DUPLICATE:
                return
            if decision.outcome == IdempotencyOutcome.CONFLICT:
                raise StateDBError("delivery idempotency conflict")
            _insert_delivery_job(connection, spec, stamp, command_id=command_id)

    def claim_delivery_job(
        self,
        job_id: str,
        *,
        lease_owner: str,
        now: datetime | None = None,
        lease_seconds: int = 60,
        max_attempts: int = 3,
    ) -> bool:
        stamp_dt = now or _utc_now()
        stamp = _iso(stamp_dt)
        lease_until = _iso(stamp_dt + timedelta(seconds=lease_seconds))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM delivery_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            row_dict = _row_to_dict(row)
            if _lease_is_live(row_dict, stamp_dt) and row_dict.get("lease_owner") not in {"", lease_owner}:
                return False
            attempt_count = int(row_dict.get("attempt_count") or 0)
            if attempt_count >= max_attempts:
                connection.execute(
                    """
                    UPDATE delivery_jobs
                    SET status = 'quarantined', last_error_class = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    ("lease_attempt_limit", stamp, job_id),
                )
                return False
            connection.execute(
                """
                UPDATE delivery_jobs
                SET status = 'claimed', lease_owner = ?, lease_until = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (lease_owner, lease_until, stamp, job_id),
            )
            return True

    def record_replayable_attempt(
        self,
        job_id: str,
        *,
        now: datetime | None = None,
        max_attempts: int = 3,
        next_retry_seconds: int = 60,
    ) -> str:
        stamp_dt = now or _utc_now()
        stamp = _iso(stamp_dt)
        next_retry_at = _iso(stamp_dt + timedelta(seconds=next_retry_seconds))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM delivery_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            attempt_count = int(row["attempt_count"] or 0) + 1
            if attempt_count >= max_attempts:
                status = "quarantined"
                last_error_class = "replay_attempt_limit"
                next_retry_at = ""
            else:
                status = IdempotencyOutcome.REPLAYABLE
                last_error_class = "remote_outcome_uncertain"
            connection.execute(
                """
                UPDATE delivery_jobs
                SET status = ?, attempt_count = ?, next_retry_at = ?,
                    last_error_class = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (status, attempt_count, next_retry_at, last_error_class, stamp, job_id),
            )
            return status

    def mark_delivery_executing(
        self,
        job_id: str,
        *,
        lease_owner: str,
        now: datetime | None = None,
    ) -> bool:
        stamp_dt = now or _utc_now()
        stamp = _iso(stamp_dt)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM delivery_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            row_dict = _row_to_dict(row)
            if row_dict.get("lease_owner") != lease_owner or not _lease_is_live(row_dict, stamp_dt):
                connection.execute(
                    """
                    UPDATE delivery_jobs
                    SET last_error_class = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    ("stale_owner_rejected", stamp, job_id),
                )
                return False
            connection.execute(
                "UPDATE delivery_jobs SET status = 'executing', updated_at = ? WHERE job_id = ?",
                (stamp, job_id),
            )
            return True

    def record_delivery_evidence(
        self,
        job_id: str,
        *,
        status: str,
        dataset_ref: str = "",
        document_ref: str = "",
        run: str = "",
        last_error_class: str = "",
        observed_at: datetime | None = None,
    ) -> None:
        stamp = _iso(observed_at)
        if status not in {"succeeded", "failed_retryable", "quarantined", "replayable"}:
            raise ValueError("unsupported delivery evidence status")
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE delivery_jobs
                SET status = ?, ragflow_dataset_id = ?, ragflow_document_id = ?,
                    ragflow_run = ?, last_error_class = ?, last_reconciled_at = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (status, dataset_ref, document_ref, run, last_error_class, stamp, stamp, job_id),
            )

    def complete_delivery_with_evidence(
        self,
        job_id: str,
        *,
        lease_owner: str,
        status: str,
        dataset_ref: str = "",
        document_ref: str = "",
        run: str = "",
        last_error_class: str = "",
        observed_at: datetime | None = None,
        now: datetime | None = None,
    ) -> bool:
        stamp_dt = now or _utc_now()
        stamp = _iso(stamp_dt)
        evidence_stamp = _iso(observed_at or stamp_dt)
        if status not in {"succeeded", "failed_retryable"}:
            raise ValueError("unsupported delivery completion status")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM delivery_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            row_dict = _row_to_dict(row)
            if row_dict.get("lease_owner") != lease_owner or not _lease_is_live(row_dict, stamp_dt):
                connection.execute(
                    """
                    UPDATE delivery_jobs
                    SET last_error_class = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    ("stale_owner_rejected", stamp, job_id),
                )
                return False
            connection.execute(
                """
                UPDATE delivery_jobs
                SET status = ?, ragflow_dataset_id = ?, ragflow_document_id = ?,
                    ragflow_run = ?, last_error_class = ?, last_reconciled_at = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (status, dataset_ref, document_ref, run, last_error_class, evidence_stamp, stamp, job_id),
            )
            return True

    def record_failed_retryable_attempt(
        self,
        job_id: str,
        *,
        run: str = "",
        dataset_ref: str = "",
        document_ref: str = "",
        error_class: str = "async_parse_failed",
        lease_owner: str = "",
        observed_at: datetime | None = None,
        now: datetime | None = None,
        max_attempts: int = 3,
        next_retry_seconds: int = 60,
    ) -> str:
        stamp_dt = now or _utc_now()
        stamp = _iso(stamp_dt)
        evidence_stamp = _iso(observed_at or stamp_dt)
        next_retry_at = _iso(stamp_dt + timedelta(seconds=next_retry_seconds))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM delivery_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            row_dict = _row_to_dict(row)
            if lease_owner and (
                row_dict.get("lease_owner") != lease_owner or not _lease_is_live(row_dict, stamp_dt)
            ):
                connection.execute(
                    """
                    UPDATE delivery_jobs
                    SET last_error_class = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    ("stale_owner_rejected", stamp, job_id),
                )
                return "stale_owner_rejected"
            attempt_count = int(row["attempt_count"] or 0) + 1
            if attempt_count >= max_attempts:
                status = "quarantined"
                next_retry_at = ""
            else:
                status = "failed_retryable"
            connection.execute(
                """
                UPDATE delivery_jobs
                SET status = ?, attempt_count = ?, next_retry_at = ?,
                    ragflow_dataset_id = ?, ragflow_document_id = ?, ragflow_run = ?,
                    last_error_class = ?, last_reconciled_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    status,
                    attempt_count,
                    next_retry_at,
                    dataset_ref,
                    document_ref,
                    run,
                    error_class,
                    evidence_stamp,
                    stamp,
                    job_id,
                ),
            )
            return status

    def consume_inbox_with_command(
        self,
        *,
        inbox_event_id: str,
        command_id: str,
        command_type: str,
        idempotency_key: str,
        payload_hash: str,
        result: CommandResultSpec,
        domain_records: Iterable[DomainRecordSpec],
        delivery_jobs: Iterable[DeliveryJobSpec],
        resource_id_hash: str = "",
        session_id_hash: str = "",
        input_epoch: str = "",
        now: datetime | None = None,
        inject_failure_at: str = "",
    ) -> CommandTransactionResult:
        return self.command_transaction().execute(
            command_id=command_id,
            command_type=command_type,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            result=result,
            domain_records=domain_records,
            delivery_jobs=delivery_jobs,
            inbox_event_id=inbox_event_id,
            resource_id_hash=resource_id_hash,
            session_id_hash=session_id_hash,
            input_epoch=input_epoch,
            now=now,
            inject_failure_at=inject_failure_at,
        )

    def get_delivery_job(self, job_id: str) -> dict | None:
        return self.get_row("delivery_jobs", "job_id", job_id)

    def list_delivery_jobs(self, *, status: str, limit: int = 50) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM delivery_jobs
                WHERE status = ?
                ORDER BY created_at ASC, rowid ASC
                LIMIT ?
                """,
                (status, int(limit)),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def record_delivery_payload(
        self,
        payload: Mapping[str, object],
        *,
        now: datetime | None = None,
    ) -> str:
        """Persist the redacted wire payload for later delivery; returns an outcome.

        Idempotent and fail-closed: re-recording the same idempotency_key with the
        SAME contentHash is ``already_present``; with a DIFFERENT contentHash it is a
        genuine ``conflict`` and the stored payload is NOT overwritten.
        """
        idempotency_key = str(payload.get("idempotencyKey") or "")
        payload_hash = str(payload.get("contentHash") or "")
        if not idempotency_key:
            raise ValueError("idempotencyKey is required")
        if not payload_hash:
            raise ValueError("contentHash is required")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_hash FROM delivery_payloads WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) != payload_hash:
                    return "conflict"
                return "already_present"
            connection.execute(
                """
                INSERT INTO delivery_payloads (
                    idempotency_key, payload_hash, payload_json, recorded_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    payload_hash,
                    _json_dumps(payload),
                    _iso(now),
                ),
            )
            return "recorded"

    def get_delivery_payload(self, idempotency_key: str) -> dict | None:
        row = self.get_row("delivery_payloads", "idempotency_key", idempotency_key)
        if row is None:
            return None
        try:
            payload = json.loads(str(row.get("payload_json") or ""))
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def get_domain_record(self, domain_record_id: str) -> dict | None:
        return self.get_row("domain_records", "domain_record_id", domain_record_id)

    def get_row(self, table: str, key_column: str, key_value: str) -> dict | None:
        _assert_safe_identifier(table)
        _assert_safe_identifier(key_column)
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT * FROM {table} WHERE {key_column} = ?",
                (key_value,),
            ).fetchone()
        return _row_to_dict(row) if row else None

    def list_rows(self, table: str) -> list[dict]:
        _assert_safe_identifier(table)
        with self.connect() as connection:
            rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        return [_row_to_dict(row) for row in rows]

    def scalar(self, sql: str, params: Iterable[object] = ()) -> object:
        with self.connect() as connection:
            row = connection.execute(sql, tuple(params)).fetchone()
        return row[0] if row else None


class CommandTransaction:
    def __init__(self, state_db: RAGIngressStateDB):
        self._state_db = state_db
        self._connection: sqlite3.Connection | None = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("transaction connection is available only while executing")
        return self._connection

    def execute(
        self,
        *,
        command_id: str,
        command_type: str,
        idempotency_key: str,
        payload_hash: str,
        result: CommandResultSpec,
        domain_records: Iterable[DomainRecordSpec] = (),
        delivery_jobs: Iterable[DeliveryJobSpec],
        validate: Callable[["CommandTransaction"], None] | None = None,
        mutate: Callable[["CommandTransaction"], Mapping[str, object] | None] | None = None,
        inbox_event_id: str = "",
        resource_id_hash: str = "",
        session_id_hash: str = "",
        input_epoch: str = "",
        lease_owner: str = "command-transaction",
        lease_seconds: int = 60,
        now: datetime | None = None,
        inject_failure_at: str = "",
    ) -> CommandTransactionResult:
        stamp_dt = now or _utc_now()
        stamp = _iso(stamp_dt)
        lease_until = _iso(stamp_dt + timedelta(seconds=lease_seconds))
        domain_specs = tuple(domain_records)
        job_specs = tuple(delivery_jobs)
        with self._state_db.connect() as connection:
            self._connection = connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO commands (
                        command_id, command_type, resource_id_hash, session_id_hash,
                        idempotency_key, payload_hash, input_epoch, status,
                        lease_owner, lease_until, attempt_count, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'claimed', ?, ?, 1, ?, ?)
                    """,
                    (
                        command_id,
                        command_type,
                        resource_id_hash,
                        session_id_hash,
                        idempotency_key,
                        payload_hash,
                        input_epoch,
                        lease_owner,
                        lease_until,
                        stamp,
                        stamp,
                    ),
                )
                if inbox_event_id:
                    _consume_inbox_event(connection, inbox_event_id, command_id, stamp)
                _maybe_fail(inject_failure_at, "claim")

                try:
                    if validate is not None:
                        validate(self)
                except ValidationRejected as exc:
                    connection.execute(
                        """
                        UPDATE commands
                        SET status = 'validation_failed', last_error_class = ?, updated_at = ?
                        WHERE command_id = ?
                        """,
                        (exc.error_class, stamp, command_id),
                    )
                    _insert_command_result(
                        connection,
                        command_id=command_id,
                        result=CommandResultSpec(decision=exc.decision, error_class=exc.error_class),
                        stamp=stamp,
                    )
                    connection.commit()
                    return CommandTransactionResult(command_id, "validation_failed", ())
                _maybe_fail(inject_failure_at, "validate")

                connection.execute(
                    "UPDATE commands SET status = 'validated', updated_at = ? WHERE command_id = ?",
                    (stamp, command_id),
                )
                delivery_plan = _plan_delivery_jobs(connection, job_specs)
                if delivery_plan.conflict is not None:
                    connection.execute(
                        """
                        UPDATE commands
                        SET status = 'quarantined', last_error_class = ?, updated_at = ?
                        WHERE command_id = ?
                        """,
                        ("idempotency_conflict", stamp, command_id),
                    )
                    _insert_command_result(
                        connection,
                        command_id=command_id,
                        result=CommandResultSpec(decision="conflict", error_class="idempotency_conflict"),
                        stamp=stamp,
                    )
                    connection.commit()
                    return CommandTransactionResult(command_id, "quarantined", ())
                if job_specs and not delivery_plan.to_insert:
                    _insert_command_result(
                        connection,
                        command_id=command_id,
                        result=CommandResultSpec(decision="duplicate"),
                        stamp=stamp,
                    )
                    connection.execute(
                        "UPDATE commands SET status = 'completed', updated_at = ? WHERE command_id = ?",
                        (stamp, command_id),
                    )
                    connection.commit()
                    return CommandTransactionResult(
                        command_id,
                        "completed",
                        tuple(delivery_plan.duplicate_job_ids),
                    )
                domain_versions = mutate(self) if mutate is not None else None
                _maybe_fail(inject_failure_at, "mutate")

                inserted_versions = []
                for domain_record in domain_specs:
                    inserted_versions.append(
                        _insert_domain_record(connection, domain_record, stamp, command_id=command_id)
                    )
                _maybe_fail(inject_failure_at, "domain")

                final_result = result
                if (domain_versions is not None or inserted_versions) and result.domain_versions_written is None:
                    final_result = CommandResultSpec(
                        decision=result.decision,
                        result_payload_ref=result.result_payload_ref,
                        domain_versions_written=domain_versions or {"domain_records": inserted_versions},
                        error_class=result.error_class,
                    )
                _insert_command_result(connection, command_id=command_id, result=final_result, stamp=stamp)
                _maybe_fail(inject_failure_at, "result")

                inserted_job_ids: list[str] = []
                for job in delivery_plan.to_insert:
                    _insert_delivery_job(connection, job, stamp, command_id=command_id)
                    inserted_job_ids.append(job.job_id)
                _maybe_fail(inject_failure_at, "jobs")

                connection.execute(
                    "UPDATE commands SET status = 'completed', updated_at = ? WHERE command_id = ?",
                    (stamp, command_id),
                )
                _maybe_fail(inject_failure_at, "commit")
                connection.commit()
                return CommandTransactionResult(
                    command_id=command_id,
                    status="completed",
                    delivery_job_ids=tuple(delivery_plan.duplicate_job_ids + inserted_job_ids),
                )
            except Exception:
                connection.rollback()
                raise
            finally:
                self._connection = None


def _consume_inbox_event(connection: sqlite3.Connection, event_id: str, command_id: str, stamp: str) -> None:
    cursor = connection.execute(
        """
        UPDATE inbox_events
        SET consumed_by_command_id = ?, consumed_at = ?, updated_at = ?
        WHERE event_id = ? AND consumed_by_command_id = ''
        """,
        (command_id, stamp, stamp, event_id),
    )
    if cursor.rowcount != 1:
        raise StateDBError("inbox event is missing or already consumed")


def _insert_command_result(
    connection: sqlite3.Connection,
    *,
    command_id: str,
    result: CommandResultSpec,
    stamp: str,
) -> None:
    connection.execute(
        """
        INSERT INTO command_results (
            result_id, command_id, decision, result_payload_ref,
            domain_versions_written, error_class, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _new_id("cmdres"),
            command_id,
            result.decision,
            result.result_payload_ref,
            _json_dumps(result.domain_versions_written or {}),
            result.error_class,
            stamp,
        ),
    )


def _insert_delivery_job(
    connection: sqlite3.Connection,
    job: DeliveryJobSpec,
    stamp: str,
    *,
    command_id: str,
) -> None:
    connection.execute(
        """
        INSERT INTO delivery_jobs (
            job_id, command_id, idempotency_key, payload_hash, target_profile,
            document_kind, status, next_retry_at, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job.job_id,
            command_id,
            job.idempotency_key,
            job.payload_hash,
            job.target_profile,
            job.document_kind,
            job.status,
            job.next_retry_at,
            stamp,
            stamp,
        ),
    )


def _insert_domain_record(
    connection: sqlite3.Connection,
    domain_record: DomainRecordSpec,
    stamp: str,
    *,
    command_id: str,
) -> dict:
    version = _next_domain_record_version(
        connection,
        resource_id_hash=domain_record.resource_id_hash,
        domain_kind=domain_record.domain_kind,
    )
    connection.execute(
        """
        INSERT INTO domain_records (
            domain_record_id, command_id, resource_id_hash, session_id_hash,
            domain_kind, lifecycle_status, version, payload_hash, payload_ref,
            projection_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            domain_record.domain_record_id,
            command_id,
            domain_record.resource_id_hash,
            domain_record.session_id_hash,
            domain_record.domain_kind,
            domain_record.lifecycle_status,
            version,
            domain_record.payload_hash,
            domain_record.payload_ref,
            _json_dumps(domain_record.projection or {}),
            stamp,
            stamp,
        ),
    )
    return {
        "domain_record_id": domain_record.domain_record_id,
        "domain_kind": domain_record.domain_kind,
        "version": version,
    }


def _next_domain_record_version(
    connection: sqlite3.Connection,
    *,
    resource_id_hash: str,
    domain_kind: str,
) -> int:
    row = connection.execute(
        """
        SELECT COALESCE(MAX(version), 0) + 1
        FROM domain_records
        WHERE resource_id_hash = ? AND domain_kind = ?
        """,
        (resource_id_hash, domain_kind),
    ).fetchone()
    return int(row[0])


@dataclass(frozen=True)
class _DeliveryPlan:
    to_insert: tuple[DeliveryJobSpec, ...]
    duplicate_job_ids: list[str]
    conflict: IdempotencyDecision | None = None


def _plan_delivery_jobs(connection: sqlite3.Connection, jobs: tuple[DeliveryJobSpec, ...]) -> _DeliveryPlan:
    to_insert: list[DeliveryJobSpec] = []
    duplicate_job_ids: list[str] = []
    planned_keys: dict[str, str] = {}
    for job in jobs:
        existing_payload_hash = planned_keys.get(job.idempotency_key)
        if existing_payload_hash is not None:
            if existing_payload_hash == job.payload_hash:
                duplicate_job_ids.append(job.job_id)
                continue
            return _DeliveryPlan((), duplicate_job_ids, IdempotencyDecision("conflict", "same_batch_conflict"))
        existing = _find_delivery_job_by_idempotency_key(connection, job.idempotency_key)
        decision = classify_idempotency(
            existing,
            idempotency_key=job.idempotency_key,
            payload_hash=job.payload_hash,
        )
        if decision.outcome == IdempotencyOutcome.CONFLICT:
            return _DeliveryPlan((), duplicate_job_ids, decision)
        if decision.outcome == IdempotencyOutcome.DUPLICATE:
            duplicate_job_ids.append(str(existing.get("job_id") or job.job_id))
            continue
        to_insert.append(job)
        planned_keys[job.idempotency_key] = job.payload_hash
    return _DeliveryPlan(tuple(to_insert), duplicate_job_ids)


def _find_delivery_job_by_idempotency_key(connection: sqlite3.Connection, idempotency_key: str) -> dict | None:
    row = connection.execute(
        """
        SELECT * FROM delivery_jobs
        WHERE idempotency_key = ?
        ORDER BY created_at ASC
        LIMIT 1
        """,
        (idempotency_key,),
    ).fetchone()
    return _row_to_dict(row) if row else None


def _maybe_fail(inject_failure_at: str, stage: str) -> None:
    if inject_failure_at == stage:
        raise InjectedTransactionFailure(f"injected failure at {stage}")


def _lease_is_live(row: Mapping[str, object], now: datetime) -> bool:
    lease_until = str(row.get("lease_until") or "")
    if not lease_until:
        return False
    try:
        parsed = datetime.fromisoformat(lease_until)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed > now


def _row_to_dict(row: sqlite3.Row | None) -> dict:
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


def _json_dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    stamp = value or _utc_now()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).isoformat()


def _assert_safe_identifier(value: str) -> None:
    if not value.replace("_", "").isalnum():
        raise ValueError("unsafe SQL identifier")
