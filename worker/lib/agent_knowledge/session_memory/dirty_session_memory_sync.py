from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..ledger import Ledger
from ..ragflow_client import RagflowHttpClient
from .memory_regeneration import (
    LedgerTranscriptMemorySource,
    RagflowTranscriptMemorySource,
    SessionMemoryRegenerationRunner,
)
from .sync_roundtrip import (
    rollback_session_memory_document,
    rollback_session_memory_document_by_knowledge_id,
    verify_session_memory_sync_roundtrip,
)


SYNC_DIRTY_SESSION_MEMORY_OPERATION = "memory_regeneration_sync_dirty_session_memory"
SYNC_DIRTY_SESSION_MEMORY_SCHEMA_VERSION = "agent_knowledge_dirty_session_memory_sync.v1"

NON_RETRY_REASONS = {
    "coverage_incomplete_before_upload",
    "invalid_turn_window",
    "session_memory_identity_unresolved",
    "source_session_unresolved",
}
NON_RETRY_EXCEPTION_MESSAGES = {
    "session memory coverage must be complete before promotion",
    "session memory coverage edges must match source manifest before promotion",
    "turn_start_index must be strictly positive",
    "turn_end_index must be greater than or equal to turn_start_index",
}


@dataclass(frozen=True)
class DirtySessionMemorySyncConfig:
    ledger_path: Path
    dataset_id: str
    ragflow_url: str
    runtime_dir: Path
    batch_size: int = 25
    max_processed_per_run: int = 25
    max_session_attempts: int = 2
    retry_backoff_seconds: tuple[int, ...] = (60, 180)
    poll_attempts: int = 60
    poll_interval_seconds: float = 1.0
    # "ledger" (default) reads the Mac status mirror; "ragflow_read_sot" reads
    # RAGFlow transcript-memory as source-of-truth (neuron, Mac-ledger-free).
    transcript_read_source: str = "ledger"


def row_key(row: dict) -> str:
    return "\x1f".join([str(row["provider"]), str(row["project"]), str(row["session_id_hash"])])


def _select_transcript_source(config: DirtySessionMemorySyncConfig, ledger: Ledger, ragflow: RagflowHttpClient):
    """Pick the transcript-memory read source per config (default: Mac ledger mirror).

    ``ragflow_read_sot`` selects the RAGFlow read-SoT path used by the neuron
    builder so the build reconstructs from RAGFlow transcript-memory without the
    Mac ledger (AC4). Default preserves the legacy ledger-mirror behavior.
    """
    if config.transcript_read_source == "ragflow_read_sot":
        return RagflowTranscriptMemorySource(ragflow)
    return LedgerTranscriptMemorySource(ledger)


def resolve_dataset_id(*, ragflow: RagflowHttpClient, dataset_id: str = "", dataset_name: str = "") -> str:
    if dataset_name:
        datasets = ragflow.list_datasets(name=dataset_name)
        exact = [item for item in datasets if str(item.get("name") or "") == dataset_name and item.get("id")]
        if len(exact) != 1:
            raise ValueError(f"expected exactly one RAGFlow dataset named {dataset_name!r}, got {len(exact)}")
        return str(exact[0]["id"])
    if not dataset_id:
        raise ValueError("dataset_id or dataset_name is required")
    return dataset_id


class DirtySessionMemorySyncRunner:
    def __init__(self, *, config: DirtySessionMemorySyncConfig, token: str, log: Callable[[dict], None] | None = None):
        self.config = config
        self.token = token
        self.log = log or (lambda event: None)

    def actionable_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(self._actionable_count_sql()).fetchone()
        return int(row[0] if row else 0)

    def row_is_actionable(self, row: dict) -> bool:
        with self._connect() as connection:
            found = connection.execute(
                self._actionable_rows_sql(extra_filters="AND d.provider = ? AND d.project = ? AND d.session_id_hash = ? LIMIT 1"),
                (row["provider"], row["project"], row["session_id_hash"]),
            ).fetchone()
        return found is not None

    def next_rows(self, limit: int, *, exclude_keys: set[str] | None = None) -> list[dict]:
        exclude_keys = exclude_keys or set()
        with self._connect() as connection:
            rows = connection.execute(self._actionable_rows_sql()).fetchall()
        selected: list[dict] = []
        for row in rows:
            item = dict(row)
            if row_key(item) in exclude_keys:
                continue
            selected.append(item)
            if len(selected) >= max(int(limit), 1):
                break
        return selected

    def status_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, count(*) FROM knowledge_items WHERE type='session_memory' GROUP BY status"
            ).fetchall()
        return {str(status): int(count) for status, count in rows}

    def run(self) -> dict:
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        processed = 0
        deferred = 0
        attempted_keys: set[str] = set()
        self.log(
            {
                "event": "start",
                "batch_size": self.config.batch_size,
                "max_processed_per_run": self.config.max_processed_per_run,
                "actionable_total": self.actionable_count(),
                "status_counts": self.status_counts(),
            }
        )
        while True:
            if self.config.max_processed_per_run > 0 and processed + deferred >= self.config.max_processed_per_run:
                report = self._report("complete_bounded", processed, deferred)
                self.log(report)
                return report
            total = self.actionable_count()
            if total <= 0:
                report = self._report("complete", processed, deferred)
                self.log(report)
                return report
            remaining = (
                self.config.max_processed_per_run - (processed + deferred)
                if self.config.max_processed_per_run > 0
                else self.config.batch_size
            )
            rows = self.next_rows(min(self.config.batch_size, remaining), exclude_keys=attempted_keys)
            if not rows:
                report = self._report("complete_with_deferred", processed, deferred)
                self.log(report)
                return report
            self.log({"event": "batch_start", "selected": len(rows), "actionable_total": total, "processed": processed})
            for row in rows:
                attempted_keys.add(row_key(row))
                result = self.process_one(row)
                if result["ok"]:
                    processed += 1
                else:
                    deferred += 1
                    self.log({"event": "session_deferred", **result})
                if processed and processed % 10 == 0:
                    self.log({"event": "progress", "processed": processed, "actionable_total": self.actionable_count()})
            self.log({"event": "batch_done", "processed": processed, "deferred": deferred, "actionable_total": self.actionable_count()})

    def process_one(self, row: dict) -> dict:
        started = time.time()
        last_result: dict | None = None
        for attempt in range(1, max(self.config.max_session_attempts, 1) + 1):
            try:
                result = self.process_one_once(row)
            except Exception as exc:
                error_text = str(exc)
                retryable = not any(message in error_text for message in NON_RETRY_EXCEPTION_MESSAGES)
                last_result = {
                    "provider": row["provider"],
                    "project": row["project"],
                    "fragment": str(row["session_id_hash"]).split(":", 1)[-1][:12],
                    "ok": False,
                    "attempt": attempt,
                    "error_class": exc.__class__.__name__,
                    "error": error_text[:300],
                    "retryable": retryable,
                    "seconds": round(time.time() - started, 1),
                }
                if not retryable:
                    self._mark_skipped(row, reason=_non_retry_exception_reason(error_text))
                    last_result["eventual_status"] = "skipped"
                    return last_result
            else:
                result["attempt"] = attempt
                if result["ok"]:
                    return result
                retryable = result.get("reason") not in NON_RETRY_REASONS
                result["retryable"] = retryable
                last_result = result
                if not retryable:
                    self._mark_skipped(row, reason=str(result.get("reason") or "no_loss_check_failed"))
                    result["eventual_status"] = "skipped"
                    return result
            if attempt < max(self.config.max_session_attempts, 1):
                backoff = (
                    self.config.retry_backoff_seconds[min(attempt - 1, len(self.config.retry_backoff_seconds) - 1)]
                    if self.config.retry_backoff_seconds
                    else 60
                )
                self.log({"event": "session_retry", **last_result, "backoff_seconds": backoff})
                time.sleep(max(backoff, 1))
        assert last_result is not None
        self._mark_failed(row, error_class=str(last_result.get("reason") or last_result.get("error_class") or "sync_failed"))
        last_result["eventual_status"] = "failed"
        last_result["seconds"] = round(time.time() - started, 1)
        return last_result

    def process_one_once(self, row: dict) -> dict:
        provider = row["provider"]
        project = row["project"]
        session_id_hash = row["session_id_hash"]
        fragment = str(session_id_hash).split(":", 1)[-1][:12]
        started = time.time()
        ledger = Ledger(self.config.ledger_path)
        ragflow = RagflowHttpClient(base_url=self.config.ragflow_url, bearer_token=self.token, request_timeout_seconds=45)
        source = _select_transcript_source(self.config, ledger, ragflow)
        runner = SessionMemoryRegenerationRunner(
            source=source,
            sync=True,
            ledger=ledger,
            ragflow=ragflow,
            dataset_id=self.config.dataset_id,
            runtime_dir=self.config.runtime_dir / "tmp",
            max_poll_attempts=self.config.poll_attempts,
            poll_interval_seconds=self.config.poll_interval_seconds,
        )
        try:
            report = runner.run(project=project, provider=provider, session_id_hash=session_id_hash)
        except RuntimeError:
            rollback = rollback_session_memory_document(ledger, ragflow, self.config.dataset_id, session_id_hash)
            raise RuntimeError(f"live_sync_runtime_error:{rollback.get('disable_status', '')}")
        skipped_sessions = report.get("skipped_sessions") or []
        memory_rows = report.get("would_write_session_memory") or []
        if skipped_sessions and not memory_rows:
            skipped = dict(skipped_sessions[0])
            return {
                "provider": provider,
                "project": project,
                "fragment": fragment,
                "ok": False,
                "reason": str(skipped.get("reason") or "coverage_incomplete_before_upload"),
                "coverage_gap_count": int(skipped.get("coverage_gap_count") or 0),
                "coverage_duplicate_count": int(skipped.get("coverage_duplicate_count") or 0),
                "active_promoted": False,
                "ragflow_write_performed": False,
                "seconds": round(time.time() - started, 1),
            }
        if not memory_rows:
            return {
                "provider": provider,
                "project": project,
                "fragment": fragment,
                "ok": False,
                "reason": "source_session_unresolved",
                "active_promoted": False,
                "ragflow_write_performed": False,
                "seconds": round(time.time() - started, 1),
            }
        roundtrip = verify_session_memory_sync_roundtrip(
            ledger=ledger,
            ragflow=ragflow,
            dataset_id=self.config.dataset_id,
            session_id_hash=session_id_hash,
            source=source,
            provider=provider,
            project=project,
        )
        ok = bool(roundtrip.get("coverage_no_loss") and roundtrip.get("retrieval_no_loss"))
        if not ok:
            return {
                "provider": provider,
                "project": project,
                "fragment": fragment,
                "ok": False,
                "coverage_no_loss": bool(roundtrip.get("coverage_no_loss")),
                "retrieval_no_loss": bool(roundtrip.get("retrieval_no_loss")),
                "reason": str(roundtrip.get("reason") or "no_loss_check_failed"),
                "rolled_back": bool(roundtrip.get("rolled_back")),
                "disable_status": str(roundtrip.get("disable_status") or ""),
                "active_promoted": False,
                "seconds": round(time.time() - started, 1),
            }
        knowledge_id = str((memory_rows[0] if memory_rows else {}).get("knowledge_id") or "")
        if not knowledge_id:
            raise ValueError("session memory knowledge_id missing after sync")
        try:
            active = ledger.promote_session_memory(knowledge_id)
        except Exception as exc:
            rollback = rollback_session_memory_document_by_knowledge_id(
                ledger,
                ragflow,
                self.config.dataset_id,
                knowledge_id,
            )
            self.log(
                {
                    "event": "promotion_rollback",
                    "provider": provider,
                    "project": project,
                    "fragment": fragment,
                    "knowledge_id_fragment": knowledge_id.split("_")[-1][:12],
                    "error_class": exc.__class__.__name__,
                    **rollback,
                }
            )
            raise
        ledger.mark_dirty_session_memory_promoted(
            session_id_hash=session_id_hash,
            summary_knowledge_id=str(active.get("active_knowledge_id") or knowledge_id),
        )
        return {
            "provider": provider,
            "project": project,
            "fragment": fragment,
            "ok": True,
            "coverage_no_loss": True,
            "retrieval_no_loss": True,
            "active_promoted": True,
            "active_knowledge_id_fragment": str(active.get("active_knowledge_id") or "").split("_")[-1][:12],
            "seconds": round(time.time() - started, 1),
        }

    def _mark_failed(self, row: dict, *, error_class: str) -> None:
        Ledger(self.config.ledger_path).mark_dirty_session_memory_failed(
            session_id_hash=row["session_id_hash"],
            error_class=error_class,
        )

    def _mark_skipped(self, row: dict, *, reason: str) -> None:
        Ledger(self.config.ledger_path).mark_dirty_session_memory_skipped(
            session_id_hash=row["session_id_hash"],
            reason=reason[:120],
        )

    def _connect(self):
        connection = Ledger(self.config.ledger_path)._connect()
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _report(self, event: str, processed: int, deferred: int) -> dict:
        return {
            "schema_version": SYNC_DIRTY_SESSION_MEMORY_SCHEMA_VERSION,
            "event": event,
            "status": "ok",
            "processed": processed,
            "deferred": deferred,
            "attempted": processed + deferred,
            "actionable_total": self.actionable_count(),
            "status_counts": self.status_counts(),
            "network_used": True,
            "mutation_performed": processed + deferred > 0,
            "active_promotion_required": True,
        }

    @staticmethod
    def _actionable_count_sql() -> str:
        return f"SELECT count(*) FROM ({DirtySessionMemorySyncRunner._actionable_rows_sql()})"

    @staticmethod
    def _actionable_rows_sql(*, extra_filters: str = "") -> str:
        return f"""
            SELECT d.provider, d.project, d.session_id_hash
            FROM dirty_session_memory d
            WHERE (
                d.status IN ('pending', 'promoted')
                OR (
                    d.status = 'failed'
                    AND (
                        d.next_attempt_at = ''
                        OR julianday(replace(d.next_attempt_at, 'Z', '+00:00')) <= julianday('now')
                    )
                )
              )
              AND NOT EXISTS (
                SELECT 1
                FROM session_memory_active_snapshots a
                JOIN knowledge_items k ON k.knowledge_id = a.active_knowledge_id
                WHERE a.session_id_hash = d.session_id_hash
                  AND k.type = 'session_memory'
                  AND k.provider = d.provider
                  AND k.project = d.project
                  AND k.status IN ('indexed', 'active')
                  AND k.authorization_status = 'active'
                  AND k.disabled_at = ''
                  AND coalesce(nullif(a.updated_at, ''), nullif(a.activated_at, '')) != ''
                  AND julianday(replace(coalesce(nullif(a.updated_at, ''), nullif(a.activated_at, '')), 'Z', '+00:00'))
                      >= julianday(replace(d.dirty_at, 'Z', '+00:00'))
              )
              {extra_filters}
            ORDER BY d.dirty_at ASC, d.updated_at ASC
        """


def _non_retry_exception_reason(error_text: str) -> str:
    for message in NON_RETRY_EXCEPTION_MESSAGES:
        if message in error_text:
            return message
    return "non_retry_exception"


def log_event(event: dict) -> None:
    payload = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def parse_retry_backoff(value: str) -> tuple[int, ...]:
    return tuple(int(item) for item in value.split(",") if item.strip())


