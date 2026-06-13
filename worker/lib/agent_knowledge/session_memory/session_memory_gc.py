from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..ledger import Ledger


SESSION_MEMORY_GC_OPERATION = "memory_regeneration_gc_dead_session_memory"
SESSION_MEMORY_GC_SCHEMA_VERSION = "agent_knowledge_session_memory_gc.v1"
SESSION_MEMORY_GC_ALLOWED_RETENTION_POLICIES: frozenset[str] = frozenset({"supersede_or_disable"})
MIN_DISABLED_AGE_FLOOR_SECONDS = 86400


@dataclass(frozen=True)
class SessionMemoryGcConfig:
    ledger_path: Path
    dataset_id: str
    ragflow_url: str
    max_items: int = 25
    min_disabled_age_seconds: int = MIN_DISABLED_AGE_FLOOR_SECONDS
    execute: bool = False
    declared_dataset_role: str = ""
    declared_retention_policy: str = ""
    backup_dir: str = ""

    def effective_min_disabled_age_seconds(self) -> int:
        return max(int(self.min_disabled_age_seconds), MIN_DISABLED_AGE_FLOOR_SECONDS)

    def declared_policy_input(self) -> str:
        return (self.declared_retention_policy or self.declared_dataset_role or "").strip()


class SessionMemoryGcRunner:
    def __init__(self, *, config: SessionMemoryGcConfig, token: str = ""):
        self.config = config
        self.token = token

    def run(self) -> dict:
        declared = self.config.declared_policy_input()
        if declared and _resolve_retention_policy(declared) not in SESSION_MEMORY_GC_ALLOWED_RETENTION_POLICIES:
            return self._blocked_retention_policy_report()
        ledger = Ledger(self.config.ledger_path)
        candidates = self._list_candidates(ledger)
        selected = candidates[: max(int(self.config.max_items), 1)]
        if self.config.execute:
            return self._blocked_live_execution_report(candidates, selected)
        return {
            "schema_version": SESSION_MEMORY_GC_SCHEMA_VERSION,
            "status": "ok",
            "mode": "dry_run",
            "retention_policy_enforced": bool(declared),
            "min_disabled_age_floor_seconds": MIN_DISABLED_AGE_FLOOR_SECONDS,
            "effective_min_disabled_age_seconds": self.config.effective_min_disabled_age_seconds(),
            "eligible_count": len(candidates),
            "selected_count": len(selected),
            "attempted_count": 0,
            "deleted_count": 0,
            "revalidation_skipped_count": 0,
            "backed_up_count": 0,
            "backup_enabled": bool(self.config.backup_dir),
            "failed_count": 0,
            "failed_error_class": "",
            "mutation_performed": False,
            "network_used": False,
            "raw_ids_printed": False,
        }

    def _blocked_live_execution_report(self, candidates: list[dict], selected: list[dict]) -> dict:
        return {
            "schema_version": SESSION_MEMORY_GC_SCHEMA_VERSION,
            "status": "blocked_live_execution",
            "mode": "execute",
            "retention_policy_enforced": bool(self.config.declared_policy_input()),
            "min_disabled_age_floor_seconds": MIN_DISABLED_AGE_FLOOR_SECONDS,
            "effective_min_disabled_age_seconds": self.config.effective_min_disabled_age_seconds(),
            "eligible_count": len(candidates),
            "selected_count": len(selected),
            "attempted_count": 0,
            "deleted_count": 0,
            "revalidation_skipped_count": 0,
            "backed_up_count": 0,
            "backup_enabled": bool(self.config.backup_dir),
            "failed_count": 0,
            "failed_error_class": "live_execution_not_vendored",
            "mutation_performed": False,
            "network_used": False,
            "raw_ids_printed": False,
        }

    def _blocked_retention_policy_report(self) -> dict:
        return {
            "schema_version": SESSION_MEMORY_GC_SCHEMA_VERSION,
            "status": "blocked_retention_policy",
            "mode": "execute" if self.config.execute else "dry_run",
            "retention_policy_enforced": True,
            "min_disabled_age_floor_seconds": MIN_DISABLED_AGE_FLOOR_SECONDS,
            "effective_min_disabled_age_seconds": self.config.effective_min_disabled_age_seconds(),
            "eligible_count": 0,
            "selected_count": 0,
            "attempted_count": 0,
            "deleted_count": 0,
            "revalidation_skipped_count": 0,
            "failed_count": 0,
            "failed_error_class": "",
            "mutation_performed": False,
            "network_used": False,
            "raw_ids_printed": False,
        }

    def _list_candidates(self, ledger: Ledger) -> list[dict]:
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=self.config.effective_min_disabled_age_seconds())
        ).isoformat()
        with ledger._connect() as connection:
            rows = connection.execute(
                """
                SELECT old.*
                FROM knowledge_items old
                JOIN dirty_session_memory d ON d.session_id_hash = old.session_id_hash
                WHERE old.type = 'session_memory'
                  AND old.status = 'disabled'
                  AND old.authorization_status = 'disabled'
                  AND old.disabled_at != ''
                  AND old.disabled_at <= ?
                  AND old.ragflow_dataset_id = ?
                  AND old.ragflow_document_id != ''
                  AND d.status = 'promoted'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM session_memory_active_snapshots active_old
                    WHERE active_old.active_knowledge_id = old.knowledge_id
                  )
                  AND EXISTS (
                    SELECT 1
                    FROM session_memory_active_snapshots active_snapshot
                    JOIN knowledge_items active
                      ON active.knowledge_id = active_snapshot.active_knowledge_id
                    WHERE active_snapshot.session_id_hash = old.session_id_hash
                      AND active.knowledge_id != old.knowledge_id
                      AND active.type = 'session_memory'
                      AND active.status IN ('indexed', 'active')
                      AND active.authorization_status = 'active'
                      AND active.disabled_at = ''
                      AND active.ragflow_dataset_id = old.ragflow_dataset_id
                      AND active.ragflow_document_id != ''
                  )
                ORDER BY old.disabled_at ASC, old.updated_at ASC
                """,
                (cutoff, self.config.dataset_id),
            ).fetchall()
        candidates: list[dict] = []
        for item in rows:
            row = dict(item)
            if _is_gc_deleted(row):
                continue
            if not _replacement_is_authorized(
                ledger,
                session_id_hash=str(row.get("session_id_hash") or ""),
                old_knowledge_id=str(row.get("knowledge_id") or ""),
            ):
                continue
            candidates.append(row)
        return candidates


def _metadata_dict(row: dict) -> dict:
    try:
        metadata = json.loads(str(row.get("metadata_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return metadata if isinstance(metadata, dict) else {}


def _is_gc_deleted(row: dict) -> bool:
    gc = _metadata_dict(row).get("session_memory_gc")
    return isinstance(gc, dict) and gc.get("status") == "deleted"


def _replacement_is_authorized(ledger: Ledger, *, session_id_hash: str, old_knowledge_id: str) -> bool:
    snapshot = ledger.get_session_memory_active_snapshot(session_id_hash) or {}
    active_knowledge_id = str(snapshot.get("active_knowledge_id") or "")
    if not active_knowledge_id or active_knowledge_id == old_knowledge_id:
        return False
    active = ledger.get_by_knowledge_id(active_knowledge_id)
    if not active or not active.get("ragflow_document_id"):
        return False
    if not ledger._session_memory_coverage_edges_are_complete(active):
        return False
    return ledger.authorize_document(str(active.get("ragflow_document_id") or "")) is not None


def _resolve_retention_policy(value: str) -> str:
    normalized = value.strip()
    aliases = {
        "session-memory": "supersede_or_disable",
        "ragflow-session-memory": "supersede_or_disable",
    }
    return aliases.get(normalized, normalized)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="session-memory-gc")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--ragflow-url", required=True)
    parser.add_argument("--max-items", type=int, default=25)
    parser.add_argument("--min-disabled-age-seconds", type=int, default=MIN_DISABLED_AGE_FLOOR_SECONDS)
    parser.add_argument("--declared-dataset-role", "--dataset-role", dest="declared_dataset_role", default="")
    parser.add_argument("--declared-retention-policy", "--retention-policy", dest="declared_retention_policy", default="")
    parser.add_argument("--backup-dir", dest="backup_dir", default="")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approval", default="")
    args = parser.parse_args(raw_argv)

    report = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=Path(args.ledger),
            dataset_id=args.dataset_id,
            ragflow_url=args.ragflow_url,
            max_items=args.max_items,
            min_disabled_age_seconds=args.min_disabled_age_seconds,
            execute=bool(args.execute),
            declared_dataset_role=args.declared_dataset_role,
            declared_retention_policy=args.declared_retention_policy,
            backup_dir=args.backup_dir,
        ),
    ).run()
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report.get("status") == "ok" else 1
