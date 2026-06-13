from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..ledger import Ledger


TERMINAL_SKIPPED_QUARANTINE_OPERATION = "memory_regeneration_quarantine_terminal_skipped"
TERMINAL_SKIPPED_QUARANTINE_SCHEMA_VERSION = "agent_knowledge_session_memory_terminal_skipped_quarantine.v1"
TERMINAL_SKIPPED_QUARANTINE_MARKER = "terminal_skipped_quarantined"
TERMINAL_SKIPPED_STATUS = "quarantined"

TERMINAL_REASON_CATEGORIES = {
    "coverage_incomplete_before_upload": "source_coverage_guard",
    "session memory coverage must be complete before promotion": "source_coverage_guard",
    "session memory coverage edges must match source manifest before promotion": "source_coverage_guard",
    "invalid_turn_window": "source_window_invalid",
    "session_memory_identity_unresolved": "identity_unresolved",
    "source_session_unresolved": "source_unresolved",
}


@dataclass(frozen=True)
class TerminalSkippedQuarantineConfig:
    ledger_path: Path
    max_items: int = 1000
    execute: bool = False


class TerminalSkippedQuarantineRunner:
    def __init__(self, *, config: TerminalSkippedQuarantineConfig):
        self.config = config

    def run(self) -> dict:
        ledger = Ledger(self.config.ledger_path) if self.config.execute else Ledger.open_read_only(self.config.ledger_path)
        candidates = self._list_candidates(ledger)
        selected = candidates[: max(int(self.config.max_items), 1)]
        before_blocking = self._blocking_missing_count(ledger)
        quarantined_count = 0
        if self.config.execute and selected:
            quarantined_count = self._quarantine_selected(ledger, selected)
            after_blocking = self._blocking_missing_count(ledger)
            audit_count = self._audit_count(ledger)
        else:
            after_blocking = max(before_blocking - len(selected), 0)
            audit_count = self._audit_count(ledger)

        return {
            "schema_version": TERMINAL_SKIPPED_QUARANTINE_SCHEMA_VERSION,
            "operation": TERMINAL_SKIPPED_QUARANTINE_OPERATION,
            "status": "ok",
            "mode": "execute" if self.config.execute else "dry_run",
            "eligible_count": len(candidates),
            "selected_count": len(selected),
            "quarantined_count": quarantined_count,
            "blocking_missing_count_before": before_blocking,
            "blocking_missing_count_after": after_blocking,
            "unknown_skipped_count": self._unknown_skipped_count(ledger),
            "audit_count": audit_count,
            "by_reason": _count_by(candidates, "reason"),
            "by_category": _count_categories(candidates),
            "top_provider_projects": _top_provider_projects(candidates, limit=20),
            "mutation_performed": bool(self.config.execute and quarantined_count),
            "network_used": False,
            "raw_ids_printed": False,
        }

    def _list_candidates(self, ledger: Ledger) -> list[dict]:
        reason_placeholders = ",".join("?" for _ in TERMINAL_REASON_CATEGORIES)
        with ledger._connect() as connection:
            connection.execute("PRAGMA busy_timeout=30000")
            rows = connection.execute(
                f"""
                SELECT
                    d.session_id_hash,
                    d.provider,
                    d.project,
                    d.status,
                    d.reason,
                    d.source_knowledge_id,
                    d.dirty_at,
                    d.updated_at,
                    d.attempts,
                    d.last_error_class
                FROM dirty_session_memory d
                WHERE d.status = 'skipped'
                  AND d.reason IN ({reason_placeholders})
                  AND NOT EXISTS (
                    SELECT 1
                    FROM knowledge_items k
                    WHERE k.type = 'session_memory'
                      AND k.provider = d.provider
                      AND k.project = d.project
                      AND k.session_id_hash = d.session_id_hash
                      AND k.status IN ('indexed', 'active')
                      AND k.authorization_status = 'active'
                      AND COALESCE(k.disabled_at, '') = ''
                  )
                ORDER BY d.dirty_at ASC, d.updated_at ASC
                """,
                tuple(TERMINAL_REASON_CATEGORIES),
            ).fetchall()
        return [dict(row) for row in rows]

    def _quarantine_selected(self, ledger: Ledger, selected: list[dict]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        with ledger._connect() as connection:
            connection.execute("PRAGMA busy_timeout=30000")
            for row in selected:
                cursor = connection.execute(
                    """
                    UPDATE dirty_session_memory
                    SET status = ?,
                        updated_at = ?,
                        next_attempt_at = '',
                        last_error_class = ?
                    WHERE session_id_hash = ?
                      AND provider = ?
                      AND project = ?
                      AND status = 'skipped'
                      AND reason = ?
                      AND NOT EXISTS (
                        SELECT 1
                        FROM knowledge_items k
                        WHERE k.type = 'session_memory'
                          AND k.provider = dirty_session_memory.provider
                          AND k.project = dirty_session_memory.project
                          AND k.session_id_hash = dirty_session_memory.session_id_hash
                          AND k.status IN ('indexed', 'active')
                          AND k.authorization_status = 'active'
                          AND COALESCE(k.disabled_at, '') = ''
                      )
                    """,
                    (
                        TERMINAL_SKIPPED_STATUS,
                        now,
                        TERMINAL_SKIPPED_QUARANTINE_MARKER,
                        row["session_id_hash"],
                        row["provider"],
                        row["project"],
                        row["reason"],
                    ),
                )
                if cursor.rowcount <= 0:
                    continue
                connection.execute(
                    """
                    INSERT INTO session_memory_terminal_skipped_audit (
                        session_id_hash, provider, project, original_status, terminal_status,
                        reason, category, source_knowledge_id, attempts, dirty_at,
                        skipped_at, audited_at, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id_hash) DO UPDATE SET
                        provider=excluded.provider,
                        project=excluded.project,
                        original_status=excluded.original_status,
                        terminal_status=excluded.terminal_status,
                        reason=excluded.reason,
                        category=excluded.category,
                        source_knowledge_id=excluded.source_knowledge_id,
                        attempts=excluded.attempts,
                        dirty_at=excluded.dirty_at,
                        skipped_at=excluded.skipped_at,
                        audited_at=excluded.audited_at,
                        details_json=excluded.details_json
                    """,
                    (
                        row["session_id_hash"],
                        row["provider"],
                        row["project"],
                        "skipped",
                        TERMINAL_SKIPPED_STATUS,
                        row["reason"],
                        _category_for_reason(str(row["reason"])),
                        row.get("source_knowledge_id") or "",
                        int(row.get("attempts") or 0),
                        row["dirty_at"],
                        row["updated_at"],
                        now,
                        json.dumps(_details_for(row), sort_keys=True, separators=(",", ":")),
                    ),
                )
                count += 1
        return count

    def _blocking_missing_count(self, ledger: Ledger) -> int:
        with ledger._connect() as connection:
            row = connection.execute(
                """
                SELECT count(*)
                FROM dirty_session_memory d
                WHERE d.status NOT IN ('quarantined', 'excluded')
                  AND NOT EXISTS (
                    SELECT 1
                    FROM knowledge_items k
                    WHERE k.type = 'session_memory'
                      AND k.provider = d.provider
                      AND k.project = d.project
                      AND k.session_id_hash = d.session_id_hash
                      AND k.status IN ('indexed', 'active')
                      AND k.authorization_status = 'active'
                      AND COALESCE(k.disabled_at, '') = ''
                  )
                """
            ).fetchone()
        return int(row[0] if row else 0)

    def _unknown_skipped_count(self, ledger: Ledger) -> int:
        reason_placeholders = ",".join("?" for _ in TERMINAL_REASON_CATEGORIES)
        with ledger._connect() as connection:
            row = connection.execute(
                f"""
                SELECT count(*)
                FROM dirty_session_memory d
                WHERE d.status = 'skipped'
                  AND d.reason NOT IN ({reason_placeholders})
                  AND NOT EXISTS (
                    SELECT 1
                    FROM knowledge_items k
                    WHERE k.type = 'session_memory'
                      AND k.provider = d.provider
                      AND k.project = d.project
                      AND k.session_id_hash = d.session_id_hash
                      AND k.status IN ('indexed', 'active')
                      AND k.authorization_status = 'active'
                      AND COALESCE(k.disabled_at, '') = ''
                  )
                """,
                tuple(TERMINAL_REASON_CATEGORIES),
            ).fetchone()
        return int(row[0] if row else 0)

    def _audit_count(self, ledger: Ledger) -> int:
        with ledger._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'session_memory_terminal_skipped_audit'"
            ).fetchone()
            if not exists:
                return 0
            row = connection.execute("SELECT count(*) FROM session_memory_terminal_skipped_audit").fetchone()
        return int(row[0] if row else 0)


def _category_for_reason(reason: str) -> str:
    return TERMINAL_REASON_CATEGORIES.get(reason, "unknown_terminal_skip")


def _details_for(row: dict) -> dict:
    return {
        "terminal_reason": row.get("reason") or "",
        "source_knowledge_id_present": bool(row.get("source_knowledge_id")),
        "attempts": int(row.get("attempts") or 0),
        "last_error_class": row.get("last_error_class") or "",
    }


def _count_by(rows: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _count_categories(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        category = _category_for_reason(str(row.get("reason") or ""))
        counts[category] = counts.get(category, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _top_provider_projects(rows: list[dict], *, limit: int) -> list[dict]:
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (str(row.get("provider") or ""), str(row.get("project") or ""))
        counts[key] = counts.get(key, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0][0], item[0][1]))
    return [
        {"provider": provider, "project": project, "count": count}
        for (provider, project), count in ordered[: max(int(limit), 0)]
    ]


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--max-items", type=int, default=1000)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    report = TerminalSkippedQuarantineRunner(
        config=TerminalSkippedQuarantineConfig(
            ledger_path=Path(args.ledger),
            max_items=args.max_items,
            execute=bool(args.execute),
        )
    ).run()
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0
