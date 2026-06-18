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


class MemoryPromotionMixin:
    """Session & Project Memory Promotion State Machine — ledger.py god-class에서 분할(behavior-preserving).

    Ledger가 다중상속으로 합성하므로 self 는 Ledger 인스턴스. 호출부 변경 없음."""

    def upsert_memory_candidate(self, candidate: dict) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memory_candidates (
                    candidate_id, candidate_type, project, provider, statement,
                    content_hash, sensitivity, requires_manual_approval,
                    approval_state, evidence_refs_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    candidate_type=excluded.candidate_type,
                    project=excluded.project,
                    provider=excluded.provider,
                    statement=excluded.statement,
                    content_hash=excluded.content_hash,
                    sensitivity=excluded.sensitivity,
                    requires_manual_approval=excluded.requires_manual_approval,
                    evidence_refs_json=excluded.evidence_refs_json
                """,
                (
                    candidate["candidate_id"],
                    candidate["candidate_type"],
                    candidate["project"],
                    candidate["provider"],
                    candidate["statement"],
                    candidate["content_hash"],
                    candidate["sensitivity"],
                    1 if candidate["requires_manual_approval"] else 0,
                    candidate.get("approval_state", "pending"),
                    json.dumps(candidate.get("evidence_refs", []), sort_keys=True, separators=(",", ":")),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM memory_candidates WHERE candidate_id = ?",
                (candidate["candidate_id"],),
            ).fetchone()
        return _memory_candidate_from_row(row)
    def get_memory_candidate(self, candidate_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM memory_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        return _memory_candidate_from_row(row) if row else None
    def list_memory_candidates(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_candidates ORDER BY created_at, candidate_id",
            ).fetchall()
        return [_memory_candidate_from_row(row) for row in rows]
    def update_memory_candidate_state(self, candidate_id: str, state: str, *, reviewed_by: str = "", reason: str = "") -> dict:
        reviewed_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE memory_candidates
                SET approval_state = ?, reviewed_at = ?, reviewed_by = ?, review_reason = ?
                WHERE candidate_id = ?
                """,
                (state, reviewed_at, reviewed_by, reason, candidate_id),
            )
        candidate = self.get_memory_candidate(candidate_id)
        if candidate is None:
            raise ValueError(f"unknown memory candidate: {candidate_id}")
        return candidate
    def mark_session_memory_dirty(
        self,
        *,
        session_id_hash: str,
        provider: str,
        project: str,
        reason: str,
        source_knowledge_id: str = "",
    ) -> dict:
        if not session_id_hash:
            raise ValueError("session_id_hash is required")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO dirty_session_memory (
                    session_id_hash, provider, project, status, reason,
                    source_knowledge_id, dirty_at, updated_at, attempts,
                    next_attempt_at, last_error_class, last_summary_knowledge_id,
                    last_ingress_job_id
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, 0, '', '', '', '')
                ON CONFLICT(session_id_hash) DO UPDATE SET
                    provider=excluded.provider,
                    project=excluded.project,
                    status='pending',
                    reason=excluded.reason,
                    source_knowledge_id=excluded.source_knowledge_id,
                    dirty_at=excluded.dirty_at,
                    updated_at=excluded.updated_at,
                    attempts=0,
                    next_attempt_at='',
                    last_error_class='',
                    last_summary_knowledge_id='',
                    last_ingress_job_id=''
                """,
                (session_id_hash, provider, project, reason, source_knowledge_id, now, now),
            )
        return self.get_dirty_session_memory(session_id_hash)
    def get_dirty_session_memory(self, session_id_hash: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dirty_session_memory WHERE session_id_hash = ?",
                (session_id_hash,),
            ).fetchone()
        return dict(row) if row else None
    def list_dirty_session_memory(self, *, limit: int = 50, quiet_period_seconds: int = 60) -> list[dict]:
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(seconds=max(int(quiet_period_seconds), 0))).isoformat()
        now_text = now.isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM dirty_session_memory
                WHERE status IN ('pending', 'failed')
                  AND dirty_at <= ?
                  AND (next_attempt_at = '' OR next_attempt_at <= ?)
                ORDER BY dirty_at ASC, updated_at ASC
                LIMIT ?
                """,
                (cutoff, now_text, max(int(limit), 1)),
            ).fetchall()
        return [dict(row) for row in rows]
    def mark_dirty_session_memory_enqueued(
        self,
        *,
        session_id_hash: str,
        summary_knowledge_id: str,
        ingress_job_id: str,
    ) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE dirty_session_memory
                SET status='enqueued',
                    updated_at=?,
                    last_summary_knowledge_id=?,
                    last_ingress_job_id=?,
                    last_error_class=''
                WHERE session_id_hash=?
                """,
                (now, summary_knowledge_id, ingress_job_id, session_id_hash),
            )
        return self.get_dirty_session_memory(session_id_hash)
    def mark_dirty_session_memory_skipped(self, *, session_id_hash: str, reason: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE dirty_session_memory
                SET status='skipped',
                    reason=?,
                    updated_at=?,
                    last_error_class=''
                WHERE session_id_hash=?
                """,
                (reason, now, session_id_hash),
            )
        return self.get_dirty_session_memory(session_id_hash)
    def mark_dirty_session_memory_failed(self, *, session_id_hash: str, error_class: str) -> dict:
        now = datetime.now(timezone.utc)
        next_attempt = (now + timedelta(seconds=60)).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE dirty_session_memory
                SET status='failed',
                    updated_at=?,
                    attempts=attempts + 1,
                    next_attempt_at=?,
                    last_error_class=?
                WHERE session_id_hash=?
                """,
                (now.isoformat(), next_attempt, error_class[:80], session_id_hash),
            )
        return self.get_dirty_session_memory(session_id_hash)
    def mark_dirty_session_memory_promoted(self, *, session_id_hash: str, summary_knowledge_id: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE dirty_session_memory
                SET status='promoted',
                    updated_at=?,
                    last_summary_knowledge_id=?,
                    last_error_class=''
                WHERE session_id_hash=?
                """,
                (now, summary_knowledge_id, session_id_hash),
            )
        return self.get_dirty_session_memory(session_id_hash)
    def get_session_memory_active_snapshot(self, session_id_hash: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM session_memory_active_snapshots WHERE session_id_hash = ?",
                (session_id_hash,),
            ).fetchone()
        return dict(row) if row else None
    def record_session_memory_coverage(
        self,
        *,
        active_knowledge_id: str,
        source_content_hash: str,
        source_window_hash: str,
        derived_content_hash: str,
        redaction_version: str,
        turn_start_index: int,
        turn_end_index: int,
    ) -> dict:
        if not active_knowledge_id:
            raise ValueError("active_knowledge_id is required")
        if not _is_sha256_hash(source_content_hash):
            raise ValueError("source_content_hash must be a hash")
        if not _is_sha256_hash(source_window_hash):
            raise ValueError("source_window_hash must be a hash")
        if not _is_sha256_hash(derived_content_hash):
            raise ValueError("derived_content_hash must be a hash")
        if turn_start_index <= 0:
            raise ValueError("turn_start_index must be strictly positive")
        if turn_end_index < turn_start_index:
            raise ValueError("turn_end_index must be >= turn_start_index")
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO session_memory_coverage_edges (
                    active_knowledge_id,
                    source_content_hash,
                    source_window_hash,
                    derived_content_hash,
                    redaction_version,
                    created_at,
                    turn_start_index,
                    turn_end_index
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    active_knowledge_id,
                    source_content_hash,
                    source_window_hash,
                    derived_content_hash
                ) DO UPDATE SET
                    redaction_version=excluded.redaction_version,
                    created_at=excluded.created_at,
                    turn_start_index=excluded.turn_start_index,
                    turn_end_index=excluded.turn_end_index
                """,
                (
                    active_knowledge_id,
                    source_content_hash,
                    source_window_hash,
                    derived_content_hash,
                    redaction_version,
                    created_at,
                    turn_start_index,
                    turn_end_index,
                ),
            )
            row = connection.execute(
                """
                SELECT
                    active_knowledge_id,
                    source_content_hash,
                    source_window_hash,
                    derived_content_hash,
                    redaction_version,
                    created_at,
                    turn_start_index,
                    turn_end_index
                FROM session_memory_coverage_edges
                WHERE active_knowledge_id = ?
                  AND source_content_hash = ?
                  AND source_window_hash = ?
                  AND derived_content_hash = ?
                  AND redaction_version = ?
                """,
                (
                    active_knowledge_id,
                    source_content_hash,
                    source_window_hash,
                    derived_content_hash,
                    redaction_version,
                ),
            ).fetchone()
        return dict(row)
    def list_session_memory_coverage(self, active_knowledge_id: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    active_knowledge_id,
                    source_content_hash,
                    source_window_hash,
                    derived_content_hash,
                    redaction_version,
                    created_at,
                    turn_start_index,
                    turn_end_index
                FROM session_memory_coverage_edges
                WHERE active_knowledge_id = ?
                ORDER BY turn_start_index, turn_end_index, created_at
                """,
                (active_knowledge_id,),
            ).fetchall()
        return [dict(row) for row in rows]
    def mark_project_memory_dirty(
        self,
        *,
        provider: str,
        project: str,
        reason: str,
        source_knowledge_id: str = "",
    ) -> dict:
        if not provider:
            raise ValueError("provider is required")
        if not project:
            raise ValueError("project is required")
        project_key_hash = _project_key_hash(provider, project)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO dirty_project_memory (
                    project_key_hash, provider, project, status, reason,
                    source_knowledge_id, dirty_at, updated_at, attempts,
                    next_attempt_at, last_error_class, last_snapshot_knowledge_id,
                    last_ingress_job_id
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, 0, '', '', '', '')
                ON CONFLICT(project_key_hash) DO UPDATE SET
                    provider=excluded.provider,
                    project=excluded.project,
                    status='pending',
                    reason=excluded.reason,
                    source_knowledge_id=excluded.source_knowledge_id,
                    dirty_at=excluded.dirty_at,
                    updated_at=excluded.updated_at,
                    attempts=0,
                    next_attempt_at='',
                    last_error_class='',
                    last_snapshot_knowledge_id='',
                    last_ingress_job_id=''
                """,
                (project_key_hash, provider, project, reason, source_knowledge_id, now, now),
            )
        return self.get_dirty_project_memory(provider=provider, project=project)
    def get_dirty_project_memory(self, *, provider: str, project: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dirty_project_memory WHERE project_key_hash = ?",
                (_project_key_hash(provider, project),),
            ).fetchone()
        return dict(row) if row else None
    def list_dirty_project_memory(self, *, limit: int = 50, quiet_period_seconds: int = 60) -> list[dict]:
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(seconds=max(int(quiet_period_seconds), 0))).isoformat()
        now_text = now.isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM dirty_project_memory
                WHERE status IN ('pending', 'failed')
                  AND dirty_at <= ?
                  AND (next_attempt_at = '' OR next_attempt_at <= ?)
                ORDER BY dirty_at ASC, updated_at ASC
                LIMIT ?
                """,
                (cutoff, now_text, max(int(limit), 1)),
            ).fetchall()
        return [dict(row) for row in rows]
    def mark_dirty_project_memory_enqueued(
        self,
        *,
        provider: str,
        project: str,
        snapshot_knowledge_id: str,
        ingress_job_id: str,
    ) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE dirty_project_memory
                SET status='enqueued',
                    updated_at=?,
                    last_snapshot_knowledge_id=?,
                    last_ingress_job_id=?,
                    last_error_class=''
                WHERE project_key_hash=?
                """,
                (now, snapshot_knowledge_id, ingress_job_id, _project_key_hash(provider, project)),
            )
        return self.get_dirty_project_memory(provider=provider, project=project)
    def mark_dirty_project_memory_skipped(self, *, provider: str, project: str, reason: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE dirty_project_memory
                SET status='skipped',
                    reason=?,
                    updated_at=?,
                    last_error_class=''
                WHERE project_key_hash=?
                """,
                (reason, now, _project_key_hash(provider, project)),
            )
        return self.get_dirty_project_memory(provider=provider, project=project)
    def mark_dirty_project_memory_failed(self, *, provider: str, project: str, error_class: str) -> dict:
        now = datetime.now(timezone.utc)
        next_attempt = (now + timedelta(seconds=60)).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE dirty_project_memory
                SET status='failed',
                    updated_at=?,
                    attempts=attempts + 1,
                    next_attempt_at=?,
                    last_error_class=?
                WHERE project_key_hash=?
                """,
                (now.isoformat(), next_attempt, error_class[:80], _project_key_hash(provider, project)),
            )
        return self.get_dirty_project_memory(provider=provider, project=project)
    def mark_dirty_project_memory_promoted(self, *, provider: str, project: str, snapshot_knowledge_id: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE dirty_project_memory
                SET status='promoted',
                    updated_at=?,
                    last_snapshot_knowledge_id=?,
                    last_error_class=''
                WHERE project_key_hash=?
                """,
                (now, snapshot_knowledge_id, _project_key_hash(provider, project)),
            )
        return self.get_dirty_project_memory(provider=provider, project=project)
    def get_project_memory_active_snapshot(self, *, provider: str, project: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM project_memory_active_snapshots WHERE project_key_hash = ?",
                (_project_key_hash(provider, project),),
            ).fetchone()
        return dict(row) if row else None
