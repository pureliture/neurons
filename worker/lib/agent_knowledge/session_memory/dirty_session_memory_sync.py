"""Read-only dirty-session drain planner for the retired session-memory index lane.

The retired external index bridge was this lane's only write target: it owned
the transcript-memory read-SoT, the session-memory document upload, the
retrieval round-trip verification and the disable/rollback. With that backend
retired and its client package deleted, the lane can no longer upload, verify
or promote a session-memory document.

What survives is the ledger-side introspection — which dirty sessions are
still actionable, and what session-memory document each of them would produce.
The runner therefore plans and reports; it performs no network call, no index
write and no ledger mutation.

The live builder that replaced this lane is the CouchDB-native
``couchdb-session-memory-build`` command.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..ledger import Ledger
from .memory_regeneration import (
    LedgerTranscriptMemorySource,
    SessionMemoryRegenerationRunner,
)

SYNC_DIRTY_SESSION_MEMORY_OPERATION = "memory_regeneration_sync_dirty_session_memory"
SYNC_DIRTY_SESSION_MEMORY_SCHEMA_VERSION = "agent_knowledge_dirty_session_memory_sync.v1"


@dataclass(frozen=True)
class DirtySessionMemorySyncConfig:
    ledger_path: Path
    batch_size: int = 25
    max_processed_per_run: int = 25


def row_key(row: dict) -> str:
    return "\x1f".join([str(row["provider"]), str(row["project"]), str(row["session_id_hash"])])


class DirtySessionMemorySyncRunner:
    """Plan the session-memory document of every actionable dirty session."""

    def __init__(self, *, config: DirtySessionMemorySyncConfig, log: Callable[[dict], None] | None = None):
        self.config = config
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
        """Plan (never write) the session-memory document of every dirty session.

        The plan never promotes a row, so the actionable set does not shrink
        while the loop runs; the work is bounded by an explicit budget instead.
        """
        processed = 0
        planned_documents = 0
        skipped_sessions: list[dict] = []
        budget = self.config.max_processed_per_run
        if budget <= 0:
            budget = self.actionable_count()
        self.log(
            {
                "event": "start",
                "batch_size": self.config.batch_size,
                "max_processed_per_run": self.config.max_processed_per_run,
                "actionable_total": self.actionable_count(),
                "status_counts": self.status_counts(),
            }
        )
        while processed < budget:
            remaining = budget - processed
            rows = self.next_rows(min(self.config.batch_size, remaining))
            if not rows:
                break
            self.log({"event": "batch_start", "selected": len(rows), "actionable_total": self.actionable_count(), "processed": processed})
            for row in rows:
                result = self.plan_one(row)
                processed += 1
                planned_documents += int(result.get("memory_documents_planned") or 0)
                if result.get("skipped_sessions"):
                    skipped_sessions.extend(result["skipped_sessions"])
                    self.log({"event": "session_skipped", **result})
            self.log({"event": "batch_done", "processed": processed})
        report = self._report(processed, planned_documents, skipped_sessions)
        self.log(report)
        return report

    def plan_one(self, row: dict) -> dict:
        """Build the session-memory plan for one dirty session (read-only)."""
        provider = row["provider"]
        project = row["project"]
        session_id_hash = row["session_id_hash"]
        fragment = str(session_id_hash).split(":", 1)[-1][:12]
        ledger = Ledger(self.config.ledger_path)
        source = LedgerTranscriptMemorySource(ledger)
        runner = SessionMemoryRegenerationRunner(source=source, ledger=ledger)
        report = runner.run(project=project, provider=provider, session_id_hash=session_id_hash)
        return {
            "provider": provider,
            "project": project,
            "fragment": fragment,
            "memory_documents_planned": len(report.get("would_write_session_memory") or []),
            "skipped_sessions": list(report.get("skipped_sessions") or []),
            "index_write_performed": False,
            "active_promoted": False,
        }

    def _connect(self):
        connection = Ledger(self.config.ledger_path)._connect()
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _report(self, processed: int, planned_documents: int, skipped_sessions: list[dict]) -> dict:
        return {
            "schema_version": SYNC_DIRTY_SESSION_MEMORY_SCHEMA_VERSION,
            "event": "complete",
            "status": "ok",
            "processed": processed,
            "planned_document_count": planned_documents,
            "skipped_session_count": len(skipped_sessions),
            "actionable_total": self.actionable_count(),
            "status_counts": self.status_counts(),
            "network_used": False,
            "mutation_performed": False,
            "index_write_performed": False,
            "active_promotion_required": False,
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


def log_event(event: dict) -> None:
    payload = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def parse_retry_backoff(value: str) -> tuple[int, ...]:
    return tuple(int(item) for item in value.split(",") if item.strip())
