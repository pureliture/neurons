from __future__ import annotations

from __future__ import annotations
import hashlib
import os
import sqlite3
from datetime import datetime, timedelta, timezone
import json
import shutil
import tempfile
import uuid
from pathlib import Path
from .db_adapter import ClosingSqliteConnection, SqliteLedgerDbAdapter

from .db_adapter import ClosingSqliteConnection, SqliteLedgerDbAdapter
from .ledger_base import *  # noqa: F401,F403


class IngressStatusMixin:
    """Ingress Status Tracking & Queue Management — ledger.py god-class에서 분할(behavior-preserving).

    Ledger가 다중상속으로 합성하므로 self 는 Ledger 인스턴스. 호출부 변경 없음."""

    def upsert_provider_source_contract(self, contract) -> dict:
        record = contract.to_record()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO provider_source_contracts (
                    provider, contract_id, provider_version, installed_version_evidence,
                    hook_event, source_locator_field, parser_version,
                    native_parser_status, privacy_redaction_status, verification_status,
                    source_status, hook_install_status, rollback_state, evidence_hash,
                    redacted_evidence_ref, raw_prompt_policy, unsupported_reason, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    contract_id=excluded.contract_id,
                    provider_version=excluded.provider_version,
                    installed_version_evidence=excluded.installed_version_evidence,
                    hook_event=excluded.hook_event,
                    source_locator_field=excluded.source_locator_field,
                    parser_version=excluded.parser_version,
                    native_parser_status=excluded.native_parser_status,
                    privacy_redaction_status=excluded.privacy_redaction_status,
                    verification_status=excluded.verification_status,
                    source_status=excluded.source_status,
                    hook_install_status=excluded.hook_install_status,
                    rollback_state=excluded.rollback_state,
                    evidence_hash=excluded.evidence_hash,
                    redacted_evidence_ref=excluded.redacted_evidence_ref,
                    raw_prompt_policy=excluded.raw_prompt_policy,
                    unsupported_reason=excluded.unsupported_reason,
                    updated_at=excluded.updated_at
                """,
                (
                    record["provider"],
                    record["contract_id"],
                    record["provider_version"],
                    record.get("installed_version_evidence", ""),
                    record.get("hook_event", ""),
                    record.get("source_locator_field", ""),
                    record.get("parser_version", ""),
                    record.get("native_parser_status", ""),
                    record.get("privacy_redaction_status", ""),
                    record["verification_status"],
                    record["source_status"],
                    record["hook_install_status"],
                    record.get("rollback_state", ""),
                    record["evidence_hash"],
                    record.get("redacted_evidence_ref", ""),
                    record.get("raw_prompt_policy", ""),
                    record.get("unsupported_reason", ""),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM provider_source_contracts WHERE provider = ?",
                (record["provider"],),
            ).fetchone()
        return dict(row)
    def list_provider_source_contracts(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM provider_source_contracts ORDER BY provider",
            ).fetchall()
        return [dict(row) for row in rows]
    def get_provider_source_contract(self, provider: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM provider_source_contracts WHERE provider = ?",
                (provider,),
            ).fetchone()
        return dict(row) if row else None
    def upsert_backfill_source(self, source: dict) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO backfill_sources (
                    source_id, raw_source_path, source_path_hash, project, provider,
                    provider_contract_status, source_contract_status, parser_status,
                    inventory_status, quarantine_reason, discovered_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_path_hash) DO UPDATE SET
                    raw_source_path=excluded.raw_source_path,
                    project=excluded.project,
                    provider=excluded.provider,
                    provider_contract_status=excluded.provider_contract_status,
                    source_contract_status=excluded.source_contract_status,
                    parser_status=excluded.parser_status,
                    inventory_status=excluded.inventory_status,
                    quarantine_reason=excluded.quarantine_reason,
                    updated_at=excluded.updated_at
                """,
                (
                    source["source_id"],
                    source["raw_source_path"],
                    source["source_path_hash"],
                    source["project"],
                    source["provider"],
                    source.get("provider_contract_status", ""),
                    source.get("source_contract_status", ""),
                    source.get("parser_status", ""),
                    source.get("inventory_status", "discovered"),
                    source.get("quarantine_reason", ""),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM backfill_sources WHERE source_path_hash = ?",
                (source["source_path_hash"],),
            ).fetchone()
        return dict(row)
    def update_backfill_source_status(
        self,
        source_path_hash: str,
        *,
        provider_contract_status: str = "",
        source_contract_status: str = "",
        parser_status: str = "",
        inventory_status: str,
        quarantine_reason: str = "",
    ) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE backfill_sources
                SET provider_contract_status = ?,
                    source_contract_status = ?,
                    parser_status = ?,
                    inventory_status = ?,
                    quarantine_reason = ?,
                    updated_at = ?
                WHERE source_path_hash = ?
                """,
                (
                    provider_contract_status,
                    source_contract_status,
                    parser_status,
                    inventory_status,
                    quarantine_reason,
                    now,
                    source_path_hash,
                ),
            )
            row = connection.execute(
                "SELECT * FROM backfill_sources WHERE source_path_hash = ?",
                (source_path_hash,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown backfill source: {source_path_hash}")
        return dict(row)
    def list_backfill_sources(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM backfill_sources ORDER BY raw_source_path",
            ).fetchall()
        return [dict(row) for row in rows]
    def upsert_transcript_validation_file(self, record: dict) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        document_ids_json = json.dumps(
            list(record.get("validation_document_ids", [])),
            sort_keys=True,
            separators=(",", ":"),
        )
        knowledge_ids_json = json.dumps(
            list(record.get("validation_knowledge_ids", [])),
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transcript_validation_files (
                    legacy_document_id_hash, validation_dataset_id, source_dataset_id_hash,
                    source_locator_hash, provider, project, turn_start_index, turn_end_index,
                    status, validation_document_ids_json, validation_knowledge_ids_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(legacy_document_id_hash, validation_dataset_id) DO UPDATE SET
                    source_dataset_id_hash=excluded.source_dataset_id_hash,
                    source_locator_hash=excluded.source_locator_hash,
                    provider=excluded.provider,
                    project=excluded.project,
                    turn_start_index=excluded.turn_start_index,
                    turn_end_index=excluded.turn_end_index,
                    status=excluded.status,
                    validation_document_ids_json=excluded.validation_document_ids_json,
                    validation_knowledge_ids_json=excluded.validation_knowledge_ids_json,
                    updated_at=excluded.updated_at
                """,
                (
                    record["legacy_document_id_hash"],
                    record["validation_dataset_id"],
                    record.get("source_dataset_id_hash", ""),
                    record["source_locator_hash"],
                    record["provider"],
                    record["project"],
                    int(record["turn_start_index"]),
                    int(record["turn_end_index"]),
                    record["status"],
                    document_ids_json,
                    knowledge_ids_json,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM transcript_validation_files
                WHERE legacy_document_id_hash = ? AND validation_dataset_id = ?
                """,
                (record["legacy_document_id_hash"], record["validation_dataset_id"]),
            ).fetchone()
        return _transcript_validation_file_from_row(row)
    def list_transcript_validation_files(
        self,
        *,
        validation_dataset_id: str,
        status: str | None = None,
    ) -> list[dict]:
        with self._connect() as connection:
            if status:
                rows = connection.execute(
                    """
                    SELECT * FROM transcript_validation_files
                    WHERE validation_dataset_id = ? AND status = ?
                    ORDER BY updated_at, legacy_document_id_hash
                    """,
                    (validation_dataset_id, status),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM transcript_validation_files
                    WHERE validation_dataset_id = ?
                    ORDER BY updated_at, legacy_document_id_hash
                    """,
                    (validation_dataset_id,),
                ).fetchall()
        return [_transcript_validation_file_from_row(row) for row in rows]
    def insert_scheduler_run(self, run: dict) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO scheduler_runs (
                    run_id, scheduler_id, command_kind, status, started_at,
                    completed_at, error_class, argv_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run["run_id"],
                    run["scheduler_id"],
                    run["command_kind"],
                    run["status"],
                    run["started_at"],
                    run.get("completed_at", ""),
                    run.get("error_class", ""),
                    json.dumps(list(run.get("argv", [])), separators=(",", ":")),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM scheduler_runs WHERE run_id = ?",
                (run["run_id"],),
            ).fetchone()
        return dict(row)
    def list_scheduler_runs(self, scheduler_id: str | None = None) -> list[dict]:
        with self._connect() as connection:
            if scheduler_id:
                rows = connection.execute(
                    "SELECT * FROM scheduler_runs WHERE scheduler_id = ? ORDER BY started_at",
                    (scheduler_id,),
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM scheduler_runs ORDER BY started_at").fetchall()
        return [dict(row) for row in rows]
    def upsert_prepared(
        self,
        *,
        knowledge_id: str,
        content_hash: str,
        provider: str,
        project: str,
        domain: str,
        type: str,
        title: str,
        summary: str,
        privacy_level: str = "normal",
        supersedes: str = "",
        session_id_hash: str = "",
        evidence_status: str = "historical",
        coverage_status: str = "",
        coverage_gap_count: int = 0,
        coverage_duplicate_count: int = 0,
        source_manifest_hash: str = "",
        source_chunk_count: int = 0,
        metadata: dict | None = None,
    ) -> dict:
        metadata_json = _normalize_metadata_json(metadata)
        bounded_summary = summary[:500]
        coverage_gap_count = max(int(coverage_gap_count), 0)
        coverage_duplicate_count = max(int(coverage_duplicate_count), 0)
        source_chunk_count = max(int(source_chunk_count), 0)
        source_manifest_hash = str(source_manifest_hash or "")
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM knowledge_items WHERE knowledge_id = ?",
                (knowledge_id,),
            ).fetchone()
            if existing is not None:
                if not source_manifest_hash:
                    source_manifest_hash = str(existing["source_manifest_hash"] or "")
                if source_chunk_count <= 0:
                    source_chunk_count = max(int(existing["source_chunk_count"] or 0), 0)
            if existing is not None and existing["content_hash"] != content_hash:
                if (
                    existing["status"] != "prepared"
                    or existing["ragflow_dataset_id"]
                    or existing["ragflow_document_id"]
                    or existing["ingress_job_id"]
                    or existing["queued_at"]
                    or existing["indexed_at"]
                ):
                    raise ValueError("cannot change content hash for a delivered knowledge item")
                content_owner = connection.execute(
                    "SELECT knowledge_id FROM knowledge_items WHERE content_hash = ?",
                    (content_hash,),
                ).fetchone()
                if content_owner is not None and content_owner["knowledge_id"] != knowledge_id:
                    raise ValueError("content hash already belongs to another knowledge item")
                if existing is not None and metadata is None:
                    metadata_json = str(existing["metadata_json"] or "{}")
                connection.execute(
                    """
                    UPDATE knowledge_items
                    SET content_hash=?,
                        provider=?,
                        project=?,
                        domain=?,
                        type=?,
                        session_id_hash=CASE
                            WHEN ? != '' THEN ?
                            ELSE session_id_hash
                        END,
                        title=?,
                        summary=?,
                        privacy_level=?,
                        supersedes=?,
                        evidence_status=?,
                        coverage_status=?,
                        coverage_gap_count=?,
                        coverage_duplicate_count=?,
                        source_manifest_hash=?,
                        source_chunk_count=?,
                        metadata_json=?,
                        status='prepared',
                        ragflow_dataset_id='',
                        ragflow_document_id='',
                        ingress_target_profile='',
                        ingress_job_id='',
                        queued_at='',
                        ragflow_run='',
                        ragflow_progress=0,
                        indexed_at='',
                        disabled_at='',
                        authorization_status='active'
                    WHERE knowledge_id=?
                    """,
                    (
                        content_hash,
                        provider,
                        project,
                        domain,
                        type,
                        session_id_hash,
                        session_id_hash,
                        title,
                        bounded_summary,
                        privacy_level,
                        supersedes,
                        evidence_status,
                        coverage_status,
                        coverage_gap_count,
                        coverage_duplicate_count,
                        source_manifest_hash,
                        source_chunk_count,
                        metadata_json,
                        knowledge_id,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM knowledge_items WHERE knowledge_id = ?",
                    (knowledge_id,),
                ).fetchone()
                return dict(row)
            connection.execute(
                """
                INSERT INTO knowledge_items (
                    knowledge_id, content_hash, provider, project, domain, type,
                    session_id_hash, title, summary, privacy_level, supersedes,
                    evidence_status, coverage_status, coverage_gap_count,
                    coverage_duplicate_count, source_manifest_hash, source_chunk_count,
                    metadata_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared')
                ON CONFLICT(content_hash) DO UPDATE SET
                    session_id_hash=CASE
                        WHEN excluded.session_id_hash != '' THEN excluded.session_id_hash
                        ELSE knowledge_items.session_id_hash
                    END,
                    title=excluded.title,
                    summary=excluded.summary,
                    evidence_status=excluded.evidence_status,
                    coverage_status=excluded.coverage_status,
                    coverage_gap_count=excluded.coverage_gap_count,
                    coverage_duplicate_count=excluded.coverage_duplicate_count,
                    source_manifest_hash=CASE
                        WHEN excluded.source_manifest_hash != '' THEN excluded.source_manifest_hash
                        ELSE knowledge_items.source_manifest_hash
                    END,
                    source_chunk_count=CASE
                        WHEN excluded.source_chunk_count > 0 THEN excluded.source_chunk_count
                        ELSE knowledge_items.source_chunk_count
                    END,
                    metadata_json=CASE
                        WHEN ? THEN knowledge_items.metadata_json
                        ELSE excluded.metadata_json
                    END,
                    status='prepared',
                    ragflow_dataset_id='',
                    ragflow_document_id='',
                    ingress_target_profile='',
                    ingress_job_id='',
                    queued_at='',
                    ragflow_run='',
                    ragflow_progress=0,
                    indexed_at='',
                    disabled_at='',
                    authorization_status='active'
                """,
                (
                    knowledge_id,
                    content_hash,
                    provider,
                    project,
                    domain,
                    type,
                    session_id_hash,
                    title,
                    bounded_summary,
                    privacy_level,
                    supersedes,
                    evidence_status,
                    coverage_status,
                    coverage_gap_count,
                    coverage_duplicate_count,
                    source_manifest_hash,
                    source_chunk_count,
                    metadata_json,
                    # CASE WHEN ? (boolean): metadata가 없으면 기존 metadata_json 보존.
                    # Python bool로 바인딩 — SQLite(truthy)·PostgreSQL(boolean) 양쪽 호환.
                    metadata is None,
                ),
            )
        return self.get_by_knowledge_id(knowledge_id)
    def get_by_knowledge_id(self, knowledge_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM knowledge_items WHERE knowledge_id = ?", (knowledge_id,)).fetchone()
        return dict(row) if row else None
    def _update_status(self, knowledge_id: str, status: str, **fields) -> None:
        assignments = ["status = ?"]
        values = [status]
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            values.append(value)
        values.append(knowledge_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE knowledge_items SET {', '.join(assignments)} WHERE knowledge_id = ?",
                values,
            )
        for side_file in self.path.parent.glob(self.path.name + "*"):
            if side_file.exists():
                os.chmod(side_file, 0o600)
    def list_index_timeouts(self, *, dataset_id: str | None = None, limit: int = 50) -> list[dict]:
        query = """
            SELECT knowledge_id, ragflow_dataset_id, ragflow_document_id, ragflow_run, ragflow_progress
            FROM knowledge_items
            WHERE status = 'index_timeout'
              AND ragflow_dataset_id IS NOT NULL
              AND ragflow_dataset_id != ''
              AND ragflow_document_id IS NOT NULL
              AND ragflow_document_id != ''
        """
        params: list[object] = []
        if dataset_id:
            query += " AND ragflow_dataset_id = ?"
            params.append(dataset_id)
        query += " ORDER BY updated_at ASC LIMIT ?"
        params.append(max(limit, 1))
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]
    def list_queued_transcript_chunks(self, *, target_profile: str, limit: int = 50) -> list[dict]:
        query = """
            SELECT ki.knowledge_id AS knowledge_id,
                   tc.chunk_id AS chunk_id,
                   tc.provider AS provider,
                   tc.project AS project,
                   tc.session_id_hash AS session_id_hash,
                   ki.content_hash AS content_hash,
                   ki.ingress_job_id AS ingress_job_id
            FROM knowledge_items ki
            JOIN transcript_chunks tc ON tc.knowledge_id = ki.knowledge_id
            WHERE ki.type = 'conversation_chunk'
              AND ki.status = 'queued'
              AND ki.ingress_target_profile = ?
              AND ki.ingress_job_id IS NOT NULL
              AND ki.ingress_job_id != ''
            ORDER BY ki.queued_at ASC
            LIMIT ?
        """
        with self._connect() as connection:
            rows = connection.execute(query, [target_profile, max(limit, 1)]).fetchall()
        return [dict(row) for row in rows]
    def list_queued_documents(self, *, document_type: str, target_profile: str, limit: int = 50) -> list[dict]:
        query = """
            SELECT *
            FROM knowledge_items
            WHERE type = ?
              AND status = 'queued'
              AND ingress_target_profile = ?
              AND ingress_job_id IS NOT NULL
              AND ingress_job_id != ''
            ORDER BY queued_at ASC
            LIMIT ?
        """
        with self._connect() as connection:
            rows = [dict(row) for row in connection.execute(query, [document_type, target_profile, max(limit, 1)]).fetchall()]
            chunk_rows = {}
            if document_type == "conversation_chunk" and rows:
                chunk_rows = {
                    row["knowledge_id"]: dict(row)
                    for row in connection.execute(
                        "SELECT * FROM transcript_chunks WHERE knowledge_id IN (%s)" % ",".join("?" for _ in rows),
                        [row["knowledge_id"] for row in rows],
                    ).fetchall()
                }
        return [
            _queued_document_projection(
                row,
                chunk_rows.get(row["knowledge_id"]) if document_type == "conversation_chunk" else None,
            )
            for row in rows
        ]
    def mark_quarantined_if_queued(
        self,
        knowledge_id: str,
        *,
        reason: str,
        disposition_action: str,
        run_bucket: str = "",
        expected_target_profile: str,
        expected_ingress_job_id: str,
        expected_updated_at: str,
    ) -> bool:
        item = self.get_by_knowledge_id(knowledge_id) or {}
        metadata = _load_metadata_json(str(item.get("metadata_json") or "{}"))
        metadata["m5_disposition_status"] = "quarantined"
        metadata["m5_disposition_action"] = str(disposition_action)
        metadata["m5_disposition_reason"] = str(reason)
        if run_bucket:
            metadata["m5_backend_run_bucket"] = str(run_bucket)
        now = datetime.now(timezone.utc).isoformat()
        metadata["m5_disposition_at"] = now
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE knowledge_items
                SET status = 'quarantined',
                    metadata_json = ?,
                    ragflow_run = ?,
                    indexed_at = '',
                    updated_at = ?
                WHERE knowledge_id = ?
                  AND status = 'queued'
                  AND ingress_target_profile = ?
                  AND ingress_job_id = ?
                  AND updated_at = ?
                """,
                (
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    run_bucket or str(item.get("ragflow_run") or ""),
                    now,
                    knowledge_id,
                    expected_target_profile,
                    expected_ingress_job_id,
                    expected_updated_at,
                ),
            )
            updated = cursor.rowcount == 1
        for side_file in self.path.parent.glob(self.path.name + "*"):
            if side_file.exists():
                os.chmod(side_file, 0o600)
        return updated
    def mark_replay_requested_if_queued(
        self,
        knowledge_id: str,
        *,
        reason: str,
        expected_target_profile: str,
        expected_ingress_job_id: str,
        expected_updated_at: str,
    ) -> bool:
        """CAS record an idempotent local replay-request and re-arm a queued row.

        The row stays ``queued`` and keeps its ingress target/job; ragflow
        run/progress/document_id are reset and queued_at is refreshed, and an
        explicit ``m5_disposition_status=replay_requested`` marker plus attempt
        counter are stamped on the local legacy ledger only. This NEVER writes,
        disables, deletes, or directly replays a RAGFlow document.

        Important scope limit: this is a local re-arm + audit marker. It does not by
        itself re-enqueue a queue job or create a delivery record, so it does not on
        its own cause re-delivery. The actual queue-side re-enqueue (the path that
        re-delivers a replay-requested row through the rag-ingress-queue) is a
        separate, not-yet-implemented mechanism tracked as an M6 planning item.
        """
        item = self.get_by_knowledge_id(knowledge_id) or {}
        metadata = _load_metadata_json(str(item.get("metadata_json") or "{}"))
        now = datetime.now(timezone.utc).isoformat()
        metadata["m5_disposition_status"] = "replay_requested"
        metadata["m5_disposition_action"] = "replay_missing"
        metadata["m5_disposition_reason"] = str(reason)
        metadata["m5_disposition_at"] = now
        metadata["m5_replay_attempt"] = int(metadata.get("m5_replay_attempt") or 0) + 1
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE knowledge_items
                SET metadata_json = ?,
                    ragflow_run = 'QUEUED',
                    ragflow_progress = 0,
                    ragflow_document_id = '',
                    indexed_at = '',
                    queued_at = ?,
                    updated_at = ?
                WHERE knowledge_id = ?
                  AND status = 'queued'
                  AND ingress_target_profile = ?
                  AND ingress_job_id = ?
                  AND updated_at = ?
                """,
                (
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    now,
                    now,
                    knowledge_id,
                    expected_target_profile,
                    expected_ingress_job_id,
                    expected_updated_at,
                ),
            )
            updated = cursor.rowcount == 1
        for side_file in self.path.parent.glob(self.path.name + "*"):
            if side_file.exists():
                os.chmod(side_file, 0o600)
        return updated
    def get_transcript_chunk_by_knowledge_id(self, knowledge_id: str) -> dict | None:
        """Read-only fetch of a transcript_chunks row by knowledge_id (for replay reconstruction)."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM transcript_chunks WHERE knowledge_id = ?",
                (knowledge_id,),
            ).fetchone()
        return dict(row) if row else None
    def mark_replay_delivered_if_queued(
        self,
        knowledge_id: str,
        *,
        reason: str,
        new_job_id: str,
        expected_target_profile: str,
        expected_ingress_job_id: str,
        expected_updated_at: str,
    ) -> bool:
        """CAS record that a replay-requested row was genuinely re-enqueued.

        Unlike :meth:`mark_replay_requested_if_queued` (which only re-arms the row),
        this is called AFTER a successful re-POST to the rag-ingress-queue created a
        new queue job. It records the new ``ingress_job_id`` and marks
        ``m5_disposition_status=replay_delivered`` so the row drops out of the
        replay-requested selection on the next run (natural idempotency). The row
        stays ``queued`` because the existing delivery worker still has to drive the
        new job to RAGFlow. No RAGFlow document is written/disabled/deleted here.
        """
        item = self.get_by_knowledge_id(knowledge_id) or {}
        metadata = _load_metadata_json(str(item.get("metadata_json") or "{}"))
        now = datetime.now(timezone.utc).isoformat()
        metadata["m5_disposition_status"] = "replay_delivered"
        metadata["m5_disposition_action"] = "replay_missing"
        metadata["m5_disposition_reason"] = str(reason)
        metadata["m6_replay_delivered_at"] = now
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE knowledge_items
                SET metadata_json = ?,
                    ingress_job_id = ?,
                    ragflow_run = 'QUEUED',
                    ragflow_progress = 0,
                    ragflow_document_id = '',
                    indexed_at = '',
                    queued_at = ?,
                    updated_at = ?
                WHERE knowledge_id = ?
                  AND status = 'queued'
                  AND ingress_target_profile = ?
                  AND ingress_job_id = ?
                  AND updated_at = ?
                """,
                (
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    str(new_job_id),
                    now,
                    now,
                    knowledge_id,
                    expected_target_profile,
                    expected_ingress_job_id,
                    expected_updated_at,
                ),
            )
            updated = cursor.rowcount == 1
        for side_file in self.path.parent.glob(self.path.name + "*"):
            if side_file.exists():
                os.chmod(side_file, 0o600)
        return updated
    def mark_done_via_dedupe_if_queued(
        self,
        knowledge_id: str,
        *,
        reason: str,
        dataset_id: str,
        canonical_document_id: str,
        duplicate_doc_count: int,
        expected_target_profile: str,
        expected_ingress_job_id: str,
        expected_updated_at: str,
    ) -> bool:
        """CAS converge a duplicate-exact-DONE row to terminal ``indexed`` state.

        The row had more than one exact-match DONE backend document. The caller
        selects a deterministic canonical document id; this method reflects that
        single canonical into the ledger row. It does not delete, disable, or
        otherwise mutate any RAGFlow document; backend duplicate cleanup remains a
        separate operator-gated concern.
        """
        item = self.get_by_knowledge_id(knowledge_id) or {}
        metadata = _load_metadata_json(str(item.get("metadata_json") or "{}"))
        now = datetime.now(timezone.utc).isoformat()
        metadata["m5_disposition_status"] = "deduped"
        metadata["m5_disposition_action"] = "duplicate_done"
        metadata["m5_disposition_reason"] = str(reason)
        metadata["m5_disposition_at"] = now
        metadata["m5_dedupe_canonical_selected"] = True
        metadata["m5_dedupe_duplicate_doc_count"] = int(duplicate_doc_count)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE knowledge_items
                SET status = 'indexed',
                    metadata_json = ?,
                    ragflow_dataset_id = ?,
                    ragflow_document_id = ?,
                    ragflow_run = 'DONE',
                    ragflow_progress = 1.0,
                    ingress_target_profile = '',
                    ingress_job_id = '',
                    queued_at = '',
                    indexed_at = ?,
                    updated_at = ?
                WHERE knowledge_id = ?
                  AND status = 'queued'
                  AND ingress_target_profile = ?
                  AND ingress_job_id = ?
                  AND updated_at = ?
                """,
                (
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    dataset_id,
                    canonical_document_id,
                    now,
                    now,
                    knowledge_id,
                    expected_target_profile,
                    expected_ingress_job_id,
                    expected_updated_at,
                ),
            )
            updated = cursor.rowcount == 1
        for side_file in self.path.parent.glob(self.path.name + "*"):
            if side_file.exists():
                os.chmod(side_file, 0o600)
        return updated
    def count_m5_dispositions(self, *, document_type: str) -> dict:
        """Count all M5 operator dispositions (quarantine/replay/dedupe) by status and action.

        Scans by document type only so it remains correct even after a disposition
        clears the row's ingress target profile (e.g. dedupe terminal state). These
        are operator dispositions recorded on the local ledger, not delivery success.
        """
        query = """
            SELECT metadata_json
            FROM knowledge_items
            WHERE type = ?
        """
        by_status: dict[str, int] = {}
        by_action: dict[str, int] = {}
        total = 0
        with self._connect() as connection:
            rows = connection.execute(query, (document_type,)).fetchall()
        for row in rows:
            metadata = _load_metadata_json(str(row["metadata_json"] or "{}"))
            status = str(metadata.get("m5_disposition_status") or "")
            if not status:
                continue
            total += 1
            action = str(metadata.get("m5_disposition_action") or "unknown")
            by_status[status] = by_status.get(status, 0) + 1
            by_action[action] = by_action.get(action, 0) + 1
        return {
            "disposition_count": total,
            "by_status": dict(sorted(by_status.items())),
            "by_action": dict(sorted(by_action.items())),
            "raw_ids_printed": False,
        }
    def count_m5_quarantined_dispositions(self, *, document_type: str, target_profile: str) -> dict:
        query = """
            SELECT metadata_json
            FROM knowledge_items
            WHERE type = ?
              AND status = 'quarantined'
              AND ingress_target_profile = ?
        """
        by_action: dict[str, int] = {}
        by_action_run: dict[str, int] = {}
        with self._connect() as connection:
            rows = connection.execute(query, (document_type, target_profile)).fetchall()
        for row in rows:
            metadata = _load_metadata_json(str(row["metadata_json"] or "{}"))
            action = str(metadata.get("m5_disposition_action") or "unknown")
            run_bucket = str(metadata.get("m5_backend_run_bucket") or "")
            by_action[action] = by_action.get(action, 0) + 1
            key = f"{action}:{run_bucket or '-'}"
            by_action_run[key] = by_action_run.get(key, 0) + 1
        return {
            "quarantined_count": len(rows),
            "by_action": dict(sorted(by_action.items())),
            "by_action_run": dict(sorted(by_action_run.items())),
            "raw_ids_printed": False,
        }
    def get_session_memory_by_session_id_hash(self, session_id_hash: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM knowledge_items
                WHERE type = 'session_memory' AND session_id_hash = ?
                ORDER BY ingested_at DESC, knowledge_id DESC LIMIT 1
                """,
                (session_id_hash,),
            ).fetchone()
        return dict(row) if row else None
    def update_authorization_metadata(
        self,
        knowledge_id: str,
        *,
        project: str | None = None,
        status: str | None = None,
        privacy_level: str | None = None,
        supersedes: str | None = None,
        valid_until: str | None = None,
        authorization_status: str | None = None,
    ) -> dict:
        fields = {
            "project": project,
            "status": status,
            "privacy_level": privacy_level,
            "supersedes": supersedes,
            "valid_until": valid_until,
            "authorization_status": authorization_status,
        }
        assignments = []
        values = []
        for key, value in fields.items():
            if value is None:
                continue
            assignments.append(f"{key} = ?")
            values.append(value)
        if not assignments:
            return self.get_by_knowledge_id(knowledge_id)
        values.append(knowledge_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE knowledge_items SET {', '.join(assignments)} WHERE knowledge_id = ?",
                values,
            )
        return self.get_by_knowledge_id(knowledge_id)
    def upsert_transcript_session(self, session) -> dict:
        record = session.to_record()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transcript_sessions (
                    session_id_hash, provider, project, started_at, ended_at,
                    source_status, source_locator_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id_hash) DO UPDATE SET
                    provider=excluded.provider,
                    project=excluded.project,
                    started_at=excluded.started_at,
                    ended_at=excluded.ended_at,
                    source_status=excluded.source_status,
                    source_locator_hash=excluded.source_locator_hash,
                    updated_at=excluded.updated_at
                """,
                (
                    record["session_id_hash"],
                    record["provider"],
                    record["project"],
                    record.get("started_at", ""),
                    record.get("ended_at", ""),
                    record.get("source_status", "source_unproven"),
                    record.get("source_locator_hash", ""),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM transcript_sessions WHERE session_id_hash = ?",
                (record["session_id_hash"],),
            ).fetchone()
        return dict(row)
    def upsert_transcript_turn(self, turn) -> dict:
        record = turn.to_record()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transcript_turns (
                    turn_id_hash, session_id_hash, turn_index, role, observed_at,
                    redacted_text, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id_hash) DO UPDATE SET
                    session_id_hash=excluded.session_id_hash,
                    turn_index=excluded.turn_index,
                    role=excluded.role,
                    observed_at=excluded.observed_at,
                    redacted_text=excluded.redacted_text,
                    updated_at=excluded.updated_at
                """,
                (
                    record["turn_id_hash"],
                    record["session_id_hash"],
                    record["turn_index"],
                    record["role"],
                    record.get("observed_at", ""),
                    record["redacted_text"],
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM transcript_turns WHERE turn_id_hash = ?",
                (record["turn_id_hash"],),
            ).fetchone()
        return dict(row)
    def list_transcript_sessions(self, *, project: str | None = None, provider: str | None = None, limit: int = 100) -> list[dict]:
        filters = []
        params: list[object] = []
        if project:
            filters.append("project = ?")
            params.append(project)
        if provider:
            filters.append("provider = ?")
            params.append(provider)
        where = ("WHERE " + " AND ".join(filters)) if filters else ""
        params.append(max(int(limit), 1))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM transcript_sessions {where} ORDER BY session_id_hash LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]
    def list_transcript_turns(self, session_id_hash: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM transcript_turns WHERE session_id_hash = ? ORDER BY turn_index",
                (session_id_hash,),
            ).fetchall()
        return [dict(row) for row in rows]
    def get_transcript_session(self, session_id_hash: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM transcript_sessions WHERE session_id_hash = ?",
                (session_id_hash,),
            ).fetchone()
        return dict(row) if row else None
    def upsert_transcript_tool_event(self, tool_event) -> dict:
        record = tool_event.to_record()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transcript_tool_events (
                    tool_event_id_hash, turn_id_hash, event_index, tool_name,
                    event_type, redacted_summary, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tool_event_id_hash) DO UPDATE SET
                    turn_id_hash=excluded.turn_id_hash,
                    event_index=excluded.event_index,
                    tool_name=excluded.tool_name,
                    event_type=excluded.event_type,
                    redacted_summary=excluded.redacted_summary,
                    updated_at=excluded.updated_at
                """,
                (
                    record["tool_event_id_hash"],
                    record["turn_id_hash"],
                    record["event_index"],
                    record["tool_name"],
                    record["event_type"],
                    record["redacted_summary"],
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM transcript_tool_events WHERE tool_event_id_hash = ?",
                (record["tool_event_id_hash"],),
            ).fetchone()
        return dict(row)
    def upsert_transcript_chunk(self, *, knowledge_id: str, chunk) -> dict:
        record = chunk.to_record()
        item = self.get_by_content_hash(record["content_hash"])
        if item is None:
            item = self.upsert_prepared(
                knowledge_id=knowledge_id,
                content_hash=record["content_hash"],
                provider=record["provider"],
                project=record["project"],
                domain="agent_memory",
                type="conversation_chunk",
                title=chunk.title(),
                summary=chunk.summary(),
                privacy_level="private",
            )
        if item is None:
            item = self.get_by_content_hash(record["content_hash"])
        if item is None:
            raise ValueError("failed to resolve canonical knowledge item for transcript chunk")
        canonical_knowledge_id = item["knowledge_id"]
        if item["type"] != "conversation_chunk":
            self._update_transcript_knowledge_item(canonical_knowledge_id, chunk)
            item = self.get_by_knowledge_id(canonical_knowledge_id)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transcript_chunks (
                    chunk_id, knowledge_id, session_id_hash, provider, project,
                    turn_start_index, turn_end_index, part_index, part_count,
                    char_start, char_end, content_hash, redacted_text,
                    source_status, redaction_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    knowledge_id=excluded.knowledge_id,
                    session_id_hash=excluded.session_id_hash,
                    provider=excluded.provider,
                    project=excluded.project,
                    turn_start_index=excluded.turn_start_index,
                    turn_end_index=excluded.turn_end_index,
                    part_index=excluded.part_index,
                    part_count=excluded.part_count,
                    char_start=excluded.char_start,
                    char_end=excluded.char_end,
                    content_hash=excluded.content_hash,
                    redacted_text=excluded.redacted_text,
                    source_status=excluded.source_status,
                    redaction_version=excluded.redaction_version,
                    updated_at=excluded.updated_at
                """,
                (
                    record["chunk_id"],
                    canonical_knowledge_id,
                    record["session_id_hash"],
                    record["provider"],
                    record["project"],
                    record["turn_start_index"],
                    record["turn_end_index"],
                    record.get("part_index", 1),
                    record.get("part_count", 1),
                    record.get("char_start", 0),
                    record.get("char_end", 0),
                    record["content_hash"],
                    record["redacted_text"],
                    record["source_status"],
                    record["redaction_version"],
                    now,
                    now,
                ),
            )
        return item
    def list_indexed_transcript_chunks(
        self,
        *,
        project: str | None = None,
        provider: str | None = None,
        session_id_hash: str | None = None,
    ) -> list[dict]:
        filters = ["ki.type = 'conversation_chunk'", "ki.status = 'indexed'"]
        params: list[object] = []
        if project:
            filters.append("tc.project = ?")
            params.append(project)
        if provider:
            filters.append("tc.provider = ?")
            params.append(provider)
        if session_id_hash:
            filters.append("tc.session_id_hash = ?")
            params.append(session_id_hash)
        where = " AND ".join(filters)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    tc.knowledge_id,
                    tc.chunk_id,
                    tc.session_id_hash,
                    tc.provider,
                    tc.project,
                    tc.turn_start_index,
                    tc.turn_end_index,
                    tc.part_index,
                    tc.part_count,
                    tc.char_start,
                    tc.char_end,
                    COALESCE(
                        (
                            SELECT tt.observed_at
                            FROM transcript_turns tt
                            WHERE tt.session_id_hash = tc.session_id_hash
                              AND tt.turn_index >= tc.turn_start_index
                              AND tt.turn_index <= tc.turn_end_index
                              AND tt.observed_at != ''
                            ORDER BY tt.turn_index ASC
                            LIMIT 1
                        ),
                        ts.started_at,
                        tc.created_at
                    ) AS observed_at_start,
                    COALESCE(
                        (
                            SELECT tt.observed_at
                            FROM transcript_turns tt
                            WHERE tt.session_id_hash = tc.session_id_hash
                              AND tt.turn_index >= tc.turn_start_index
                              AND tt.turn_index <= tc.turn_end_index
                              AND tt.observed_at != ''
                            ORDER BY tt.turn_index DESC
                            LIMIT 1
                        ),
                        ts.ended_at,
                        tc.updated_at
                    ) AS observed_at_end,
                    tc.content_hash,
                    tc.redacted_text,
                    tc.source_status,
                    tc.redaction_version
                FROM transcript_chunks tc
                JOIN knowledge_items ki ON ki.knowledge_id = tc.knowledge_id
                LEFT JOIN transcript_sessions ts ON ts.session_id_hash = tc.session_id_hash
                WHERE {where}
                ORDER BY
                    tc.project,
                    tc.provider,
                    tc.session_id_hash,
                    tc.turn_start_index,
                    tc.turn_end_index,
                    tc.part_index,
                    tc.char_start,
                    tc.char_end,
                    tc.chunk_id
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]
    def _maybe_mark_session_memory_dirty_for_indexed_item(self, knowledge_id: str) -> None:
        item = self.get_by_knowledge_id(knowledge_id)
        if item is None or item.get("type") != "conversation_chunk":
            return
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT session_id_hash, provider, project
                FROM transcript_chunks
                WHERE knowledge_id = ?
                """,
                (knowledge_id,),
            ).fetchone()
        if row is None:
            return
        self.mark_session_memory_dirty(
            session_id_hash=str(row["session_id_hash"] or ""),
            provider=str(row["provider"] or item.get("provider") or ""),
            project=str(row["project"] or item.get("project") or ""),
            reason="new_chunk_indexed",
            source_knowledge_id=knowledge_id,
        )
    def _maybe_mark_project_memory_dirty_for_indexed_item(self, knowledge_id: str) -> None:
        item = self.get_by_knowledge_id(knowledge_id)
        if item is None or item.get("type") != "conversation_chunk":
            return
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT provider, project
                FROM transcript_chunks
                WHERE knowledge_id = ?
                """,
                (knowledge_id,),
            ).fetchone()
        if row is None:
            return
        self.mark_project_memory_dirty(
            provider=str(row["provider"] or item.get("provider") or ""),
            project=str(row["project"] or item.get("project") or ""),
            reason="new_chunk_indexed",
            source_knowledge_id=knowledge_id,
        )
    def get_by_content_hash(self, content_hash: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM knowledge_items WHERE content_hash = ?", (content_hash,)).fetchone()
        return dict(row) if row else None
    def _update_transcript_knowledge_item(self, knowledge_id: str, chunk) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE knowledge_items
                SET provider = ?,
                    project = ?,
                    domain = 'agent_memory',
                    type = 'conversation_chunk',
                    title = ?,
                    summary = ?,
                    privacy_level = 'private',
                    redaction_version = ?,
                    status = 'prepared',
                    ragflow_dataset_id = '',
                    ragflow_document_id = '',
                    ingress_target_profile = '',
                    ingress_job_id = '',
                    queued_at = '',
                    ragflow_run = '',
                    ragflow_progress = 0,
                    indexed_at = '',
                    disabled_at = '',
                    authorization_status = 'active'
                WHERE knowledge_id = ?
                """,
                (
                    chunk.provider,
                    chunk.project,
                    chunk.title(),
                    chunk.summary(),
                    chunk.redaction_version,
                    knowledge_id,
                ),
            )
    def authorize_document(self, document_id: str, *, filters: dict | None = None, include_private: bool = False) -> dict | None:
        """Resolve a locally readable indexed document.

        The historical method name is kept for API compatibility. Local read
        access is full-trust for privacy, but authorization and
        lifecycle/data-quality states still apply. Missing, non-indexed,
        authorization-disabled, disabled, superseded, expired,
        disabled-dataset, or provenance-invalid records remain hidden from
        retrieval.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_items WHERE ragflow_document_id = ?",
                (document_id,),
            ).fetchone()
        if row is None:
            return None
        return self._authorize_knowledge_item(dict(row), filters=filters)

    def authorize_document_by_content_hash(
        self, content_hash: str, *, filters: dict | None = None, include_private: bool = False
    ) -> dict | None:
        """Canonical authorization predicate keyed by ``content_hash``.

        Identical lifecycle/authority gate as :meth:`authorize_document` (status,
        authorization_status, disabled_at, supersedes, expiry, dataset-enabled,
        type-specific active-snapshot/coverage checks). The Qdrant searchable
        mirror resolves every hit through this so the non-authority mirror cannot
        diverge from canonical authority semantics.
        """
        _ = include_private
        if not content_hash:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_items WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()
        if row is None:
            return None
        return self._authorize_knowledge_item(dict(row), filters=filters)

    def _authorize_knowledge_item(self, item: dict, *, filters: dict | None = None) -> dict | None:
        filters = filters or {}
        if item["status"] != "indexed":
            return None
        if item.get("authorization_status") != "active":
            return None
        if item["disabled_at"]:
            return None
        if item["supersedes"]:
            return None
        if _is_expired(item.get("valid_until", "")):
            return None
        if not self._dataset_is_enabled(item.get("ragflow_dataset_id", "")):
            return None
        if item.get("type") == "session_summary":
            return None
        if item.get("type") == "session_memory":
            if item.get("evidence_status") != SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS:
                return None
            if not _session_memory_coverage_is_complete(item):
                return None
            if not self._session_memory_coverage_edges_are_complete(item):
                return None
            session_id_hash = item.get("session_id_hash", "")
            if not session_id_hash:
                return None
            active = self.get_session_memory_active_snapshot(session_id_hash)
            if not active:
                return None
            if active.get("active_knowledge_id") != item.get("knowledge_id"):
                return None
        if item.get("type") == "session_memory_sot":  # legacy compatibility
            if item.get("evidence_status") != SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS:
                return None
            if not _session_memory_coverage_is_complete(item):
                return None
            if not self._session_memory_coverage_edges_are_complete(item):
                return None
            session_id_hash = item.get("session_id_hash", "")
            if not session_id_hash:
                return None
            active = self.get_session_memory_sot_active_snapshot(session_id_hash)
            if not active:
                return None
            if active.get("active_sot_knowledge_id") != item.get("knowledge_id"):
                return None
        if item.get("type") == "project_context_snapshot":
            if item.get("evidence_status") != SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS:
                return None
            provider = item.get("provider", "")
            project = item.get("project", "")
            if not provider or not project:
                return None
            active = self.get_project_memory_active_snapshot(provider=provider, project=project)
            if not active:
                return None
            if active.get("active_knowledge_id") != item.get("knowledge_id"):
                return None
        for key in ("project", "provider", "domain", "type", "session_id_hash"):
            if filters.get(key) and item[key] != filters[key]:
                return None
        return item
    def get_conversation_chunk_by_document(self, document_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT tc.*
                FROM transcript_chunks tc
                JOIN knowledge_items ki ON ki.knowledge_id = tc.knowledge_id
                WHERE ki.ragflow_document_id = ?
                """,
                (document_id,),
            ).fetchone()
        return dict(row) if row else None
    def lifecycle_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT status, COUNT(*) AS count FROM knowledge_items GROUP BY status ORDER BY status").fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}
    def total_items(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM knowledge_items").fetchone()
        return int(row["count"])
