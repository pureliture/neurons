from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..ledger import Ledger, SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS


TRANSCRIPT_VOLUME_GC_SCHEMA_VERSION = "agent_knowledge_transcript_volume_gc.v1"
MIN_ACTIVE_AGE_FLOOR_SECONDS = 86400


@dataclass(frozen=True)
class TranscriptVolumeGcConfig:
    ledger_path: Path
    transcript_dataset_id: str
    ragflow_url: str
    backup_dir: str = ""
    max_items: int = 25
    min_active_age_seconds: int = MIN_ACTIVE_AGE_FLOOR_SECONDS
    execute: bool = False

    def effective_min_active_age_seconds(self) -> int:
        return max(int(self.min_active_age_seconds), MIN_ACTIVE_AGE_FLOOR_SECONDS)


@dataclass(frozen=True)
class _Candidate:
    source_content_hash: str
    active_knowledge_id: str
    session_id_hash: str
    provider: str
    project: str


class TranscriptVolumeGcRunner:
    def __init__(self, *, config: TranscriptVolumeGcConfig, token: str = ""):
        self.config = config
        self.token = token

    def run(self) -> dict:
        ledger = Ledger(self.config.ledger_path)
        candidates = self._list_candidates(ledger)
        selected = candidates[: max(int(self.config.max_items), 1)]
        if self.config.execute:
            return self._report(candidates, selected, 0, 0, 0, 0, "live_execution_not_vendored", status="blocked_live_execution")
        return self._report(candidates, selected, 0, 0, 0, 0, "")

    def _report(self, candidates, selected, deleted, backed_up, unresolved, failed, failed_error_class, *, status: str = "ok") -> dict:
        return {
            "schema_version": TRANSCRIPT_VOLUME_GC_SCHEMA_VERSION,
            "status": status if failed_error_class else status,
            "mode": "execute" if self.config.execute else "dry_run",
            "min_active_age_floor_seconds": MIN_ACTIVE_AGE_FLOOR_SECONDS,
            "effective_min_active_age_seconds": self.config.effective_min_active_age_seconds(),
            "eligible_count": len(candidates),
            "selected_count": len(selected),
            "attempted_count": 0,
            "deleted_count": deleted,
            "backed_up_count": backed_up,
            "unresolved_count": unresolved,
            "failed_count": failed,
            "failed_error_class": failed_error_class,
            "backup_enabled": bool(self.config.backup_dir),
            "mutation_performed": False,
            "network_used": False,
            "raw_ids_printed": False,
            "hard_delete_performed": False,
        }

    def _list_candidates(self, ledger: Ledger) -> list[_Candidate]:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=self.config.effective_min_active_age_seconds())
        ).isoformat()
        with ledger._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT
                    edge.source_content_hash AS source_content_hash,
                    active.knowledge_id AS active_knowledge_id,
                    active.session_id_hash AS session_id_hash,
                    active.provider AS provider,
                    active.project AS project,
                    active.ragflow_document_id AS active_document_id
                FROM session_memory_coverage_edges edge
                JOIN knowledge_items active
                  ON active.knowledge_id = edge.active_knowledge_id
                JOIN session_memory_active_snapshots snap
                  ON snap.active_knowledge_id = active.knowledge_id
                WHERE active.type = 'session_memory'
                  AND active.status IN ('indexed', 'active')
                  AND active.authorization_status = 'active'
                  AND active.disabled_at = ''
                  AND active.evidence_status = ?
                  AND active.coverage_status = 'complete'
                  AND active.coverage_gap_count = 0
                  AND active.coverage_duplicate_count = 0
                  AND active.ragflow_document_id != ''
                  AND coalesce(nullif(snap.updated_at, ''), nullif(snap.activated_at, '')) != ''
                  AND coalesce(nullif(snap.updated_at, ''), nullif(snap.activated_at, '')) <= ?
                  AND edge.source_content_hash != ''
                ORDER BY edge.source_content_hash ASC
                """,
                (SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS, cutoff),
            ).fetchall()
        candidates: list[_Candidate] = []
        auth_cache: dict[str, bool] = {}
        seen: set[str] = set()
        for item in rows:
            row = dict(item)
            source_hash = str(row.get("source_content_hash") or "")
            active_kid = str(row.get("active_knowledge_id") or "")
            if not source_hash or source_hash in seen:
                continue
            if not self._active_is_authorized(ledger, active_kid, str(row.get("active_document_id") or ""), auth_cache):
                continue
            seen.add(source_hash)
            candidates.append(
                _Candidate(
                    source_content_hash=source_hash,
                    active_knowledge_id=active_kid,
                    session_id_hash=str(row.get("session_id_hash") or ""),
                    provider=str(row.get("provider") or ""),
                    project=str(row.get("project") or ""),
                )
            )
        return candidates

    def _active_is_authorized(self, ledger: Ledger, active_knowledge_id: str, active_document_id: str, cache: dict[str, bool]) -> bool:
        if not active_knowledge_id or not active_document_id:
            return False
        if active_knowledge_id not in cache:
            authorized = ledger.authorize_document(active_document_id)
            cache[active_knowledge_id] = bool(authorized and authorized.get("type") == "session_memory")
        return cache[active_knowledge_id]


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="transcript-volume-gc")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--transcript-dataset-id", required=True)
    parser.add_argument("--ragflow-url", required=True)
    parser.add_argument("--backup-dir", dest="backup_dir", default="")
    parser.add_argument("--max-items", type=int, default=25)
    parser.add_argument("--min-active-age-seconds", type=int, default=MIN_ACTIVE_AGE_FLOOR_SECONDS)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approval", default="")
    args = parser.parse_args(raw_argv)

    report = TranscriptVolumeGcRunner(
        config=TranscriptVolumeGcConfig(
            ledger_path=Path(args.ledger),
            transcript_dataset_id=args.transcript_dataset_id,
            ragflow_url=args.ragflow_url,
            backup_dir=args.backup_dir,
            max_items=args.max_items,
            min_active_age_seconds=args.min_active_age_seconds,
            execute=bool(args.execute),
        ),
    ).run()
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report.get("status") == "ok" else 1
