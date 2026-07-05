from __future__ import annotations

from datetime import datetime, timezone

from .ledger_base import _project_key_hash


class MemoryPromotionArea:
    """Private Ledger area object for memory-promotion dirty marking."""

    def __init__(self, ledger) -> None:
        self._ledger = ledger

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
        with self._ledger._connect() as connection:
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
        with self._ledger._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dirty_session_memory WHERE session_id_hash = ?",
                (session_id_hash,),
            ).fetchone()
        return dict(row) if row else None

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
        with self._ledger._connect() as connection:
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
        with self._ledger._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dirty_project_memory WHERE project_key_hash = ?",
                (_project_key_hash(provider, project),),
            ).fetchone()
        return dict(row) if row else None
