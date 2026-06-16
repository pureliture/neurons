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


class GcSafetyMixin:
    """GC Planning & Auditing (GC Safety Lane) — ledger.py god-class에서 분할(behavior-preserving).

    Ledger가 다중상속으로 합성하므로 self 는 Ledger 인스턴스. 호출부 변경 없음."""

    def list_retrieval_audit(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT audit_id, pack_id, prompt_hash, query_hash, filters_json,
                       result_count, private_allowed, created_at
                FROM retrieval_audit
                ORDER BY created_at
                """
            ).fetchall()
        return [dict(row) for row in rows]
    def record_auto_recall_audit(
        self,
        *,
        provider: str,
        project: str,
        status: str,
        policy_reasons: list[str],
        private_policy_allowed: bool,
        prompt_hash: str,
        preview_hash: str = "",
        context_pack_id: str = "",
        selected_items: list[dict] | None = None,
    ) -> dict:
        audit_id = "auto_recall_" + uuid.uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO auto_recall_audit (
                    audit_id, provider, project, status, policy_reasons_json,
                    private_policy_allowed, prompt_hash, preview_hash,
                    context_pack_id, selected_items_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    provider,
                    project,
                    status,
                    json.dumps(list(policy_reasons), sort_keys=True, separators=(",", ":")),
                    1 if private_policy_allowed else 0,
                    prompt_hash,
                    preview_hash,
                    context_pack_id,
                    json.dumps(selected_items or [], sort_keys=True, separators=(",", ":")),
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM auto_recall_audit WHERE audit_id = ?",
                (audit_id,),
            ).fetchone()
        return dict(row)
    def record_memory_gc_audit(
        self,
        *,
        gc_kind: str,
        operation: str,
        schema_version: str,
        mode: str,
        knowledge_id: str,
        ragflow_document_id: str,
        dataset_id: str,
        replacement_knowledge_id: str,
        dirty_at: str = "",
        snapshot_updated_at: str = "",
        approval_operation: str = "",
        age_gate_seconds: int = 0,
        mutated: bool = True,
    ) -> dict:
        """G-3 (M-GC contract §3.4 A1/A2/A3): durable append-only audit row for
        one successful GC mutation. The raw RAGFlow document id is NEVER stored;
        only its sha256 hex digest is persisted (A3). For the irreversible
        session_memory hard delete, ``replacement_knowledge_id`` records the
        active generation that justified the delete so it stays reconstructable
        after the doc disappears (A2). The bound epoch markers (``dirty_at`` and
        ``snapshot_updated_at``) make "which generation activation justified the
        GC" reconstructable (E3)."""
        audit_id = "memory_gc_" + uuid.uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        document_id_hash = hashlib.sha256(str(ragflow_document_id or "").encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memory_gc_audit (
                    audit_id, gc_kind, operation, schema_version, mode,
                    knowledge_id, ragflow_document_id_hash, dataset_id,
                    replacement_knowledge_id, dirty_at, snapshot_updated_at,
                    approval_operation, age_gate_seconds, mutated, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    gc_kind,
                    operation,
                    schema_version,
                    mode,
                    knowledge_id,
                    document_id_hash,
                    dataset_id,
                    replacement_knowledge_id,
                    dirty_at,
                    snapshot_updated_at,
                    approval_operation,
                    int(age_gate_seconds),
                    1 if mutated else 0,
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM memory_gc_audit WHERE audit_id = ?",
                (audit_id,),
            ).fetchone()
        return dict(row)
    def list_memory_gc_audit(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT audit_id, gc_kind, operation, schema_version, mode,
                       knowledge_id, ragflow_document_id_hash, dataset_id,
                       replacement_knowledge_id, dirty_at, snapshot_updated_at,
                       approval_operation, age_gate_seconds, mutated, created_at
                FROM memory_gc_audit
                ORDER BY created_at, audit_id
                """
            ).fetchall()
        return [dict(row) for row in rows]
