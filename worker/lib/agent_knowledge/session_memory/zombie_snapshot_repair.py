from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..ledger import Ledger


ZOMBIE_SNAPSHOT_REPAIR_OPERATION = "memory_regeneration_requeue_disabled_active_snapshot"
ZOMBIE_SNAPSHOT_REPAIR_SCHEMA_VERSION = "agent_knowledge_session_memory_zombie_snapshot_repair.v1"
ZOMBIE_SNAPSHOT_REPAIR_MARKER = "disabled_active_snapshot_requeued"


@dataclass(frozen=True)
class ZombieSnapshotRepairConfig:
    ledger_path: Path
    max_items: int = 100
    execute: bool = False


class ZombieSnapshotRepairRunner:
    def __init__(self, *, config: ZombieSnapshotRepairConfig):
        self.config = config

    def run(self) -> dict:
        ledger = Ledger(self.config.ledger_path)
        candidates = self._list_candidates(ledger)
        selected = candidates[: max(int(self.config.max_items), 1)]
        requeued_count = 0
        removed_snapshot_count = 0
        if self.config.execute and selected:
            requeued_count, removed_snapshot_count = self._repair_selected(ledger, selected)
        return {
            "schema_version": ZOMBIE_SNAPSHOT_REPAIR_SCHEMA_VERSION,
            "operation": ZOMBIE_SNAPSHOT_REPAIR_OPERATION,
            "status": "ok",
            "mode": "execute" if self.config.execute else "dry_run",
            "eligible_count": len(candidates),
            "selected_count": len(selected),
            "requeued_count": requeued_count,
            "removed_snapshot_count": removed_snapshot_count,
            "by_reason": _count_by_reason(candidates),
            "mutation_performed": bool(self.config.execute and requeued_count),
            "network_used": False,
            "raw_ids_printed": False,
        }

    def _list_candidates(self, ledger: Ledger) -> list[dict]:
        with ledger._connect() as connection:
            connection.execute("PRAGMA busy_timeout=30000")
            rows = connection.execute(
                """
                SELECT
                    d.session_id_hash,
                    d.provider,
                    d.project,
                    d.reason,
                    d.dirty_at,
                    d.updated_at,
                    d.last_summary_knowledge_id,
                    s.active_knowledge_id,
                    k.content_hash AS active_content_hash,
                    k.session_id_hash AS active_item_session_id_hash,
                    k.status AS active_item_status,
                    k.authorization_status AS active_item_authorization_status,
                    k.disabled_at
                FROM dirty_session_memory d
                JOIN session_memory_active_snapshots s
                  ON s.session_id_hash = d.session_id_hash
                JOIN knowledge_items k
                  ON k.knowledge_id = s.active_knowledge_id
                WHERE d.status IN ('pending', 'promoted')
                  AND NOT (
                    k.type = 'session_memory'
                    AND k.provider = d.provider
                    AND k.project = d.project
                    AND k.session_id_hash = d.session_id_hash
                    AND k.status IN ('indexed', 'active')
                    AND k.authorization_status = 'active'
                    AND COALESCE(k.disabled_at, '') = ''
                  )
                ORDER BY d.dirty_at ASC, d.updated_at ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def _repair_selected(self, ledger: Ledger, selected: list[dict]) -> tuple[int, int]:
        now = datetime.now(timezone.utc).isoformat()
        requeued_count = 0
        removed_snapshot_count = 0
        with ledger._connect() as connection:
            connection.execute("PRAGMA busy_timeout=30000")
            for row in selected:
                cursor = connection.execute(
                    """
                    DELETE FROM session_memory_active_snapshots
                    WHERE session_id_hash = ?
                    AND active_knowledge_id = ?
                      AND EXISTS (
                        SELECT 1
                        FROM knowledge_items active
                        WHERE active.knowledge_id = ?
                          AND NOT (
                            active.type = 'session_memory'
                            AND active.provider = ?
                            AND active.project = ?
                            AND active.session_id_hash = ?
                            AND active.status IN ('indexed', 'active')
                            AND active.authorization_status = 'active'
                            AND COALESCE(active.disabled_at, '') = ''
                          )
                      )
                    """,
                    (
                        row["session_id_hash"],
                        row["active_knowledge_id"],
                        row["active_knowledge_id"],
                        row["provider"],
                        row["project"],
                        row["session_id_hash"],
                    ),
                )
                if cursor.rowcount <= 0:
                    continue
                removed_snapshot_count += int(cursor.rowcount)
                update_cursor = connection.execute(
                    """
                    UPDATE dirty_session_memory
                    SET status = 'pending',
                        updated_at = ?,
                        attempts = 0,
                        next_attempt_at = '',
                        last_error_class = ?,
                        last_ingress_job_id = ''
                    WHERE session_id_hash = ?
                      AND provider = ?
                      AND project = ?
                      AND status IN ('pending', 'promoted')
                    """,
                    (
                        now,
                        ZOMBIE_SNAPSHOT_REPAIR_MARKER,
                        row["session_id_hash"],
                        row["provider"],
                        row["project"],
                    ),
                )
                if update_cursor.rowcount > 0:
                    requeued_count += 1
        return requeued_count, removed_snapshot_count


def _count_by_reason(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        reason = str(row.get("reason") or "")
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--max-items", type=int, default=100)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    report = ZombieSnapshotRepairRunner(
        config=ZombieSnapshotRepairConfig(
            ledger_path=Path(args.ledger),
            max_items=args.max_items,
            execute=bool(args.execute),
        )
    ).run()
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0
