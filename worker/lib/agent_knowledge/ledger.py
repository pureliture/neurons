from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import shutil
import tempfile
import uuid
from pathlib import Path

from .db_adapter import ClosingSqliteConnection, SqliteLedgerDbAdapter
from .ledger_base import *  # noqa: F401,F403 (상수/helper re-export 호환)
from .ledger_ingress_mixin import IngressStatusMixin
from .ledger_gc_safety_mixin import GcSafetyMixin
from .ledger_memory_promotion_area import MemoryPromotionArea
from .ledger_memory_promotion_mixin import MemoryPromotionMixin
from .ledger_native_memory_mixin import NativeMemoryMixin
from .public_safe_util import ensure_public_safe, public_safe_text


_READ_ONLY_SQL_KEYWORD_RE = re.compile(
    r"^\s*(?:(?:--[^\n]*\n)|(?:/\*.*?\*/))*\s*([A-Za-z]+)",
    re.DOTALL,
)
_READ_ONLY_SQL_ALLOWED_KEYWORDS = {"EXPLAIN", "PRAGMA", "SELECT"}
_READ_ONLY_SQL_CTE_FINAL_KEYWORDS = {"SELECT"}

_OBJECT_AUTHORITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS object_review_proposals (
    proposal_id TEXT PRIMARY KEY,
    project TEXT NOT NULL DEFAULT '',
    proposal_type TEXT NOT NULL,
    target_object_id TEXT NOT NULL,
    status TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_object_review_proposals_project_status
    ON object_review_proposals(project, status, updated_at);
CREATE TABLE IF NOT EXISTS object_authority_decisions (
    decision_id TEXT PRIMARY KEY,
    project TEXT NOT NULL DEFAULT '',
    proposal_id TEXT NOT NULL,
    target_object_id TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    previous_authority_lane TEXT NOT NULL,
    new_authority_lane TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_object_authority_decisions_target_created
    ON object_authority_decisions(target_object_id, created_at);
CREATE TABLE IF NOT EXISTS object_authority_states (
    target_object_id TEXT PRIMARY KEY,
    project TEXT NOT NULL DEFAULT '',
    authority_lane TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    decision_reason TEXT NOT NULL DEFAULT '',
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_object_authority_states_project_lane
    ON object_authority_states(project, authority_lane, updated_at);
"""

_SCHEMA_MIGRATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""

_OBJECT_AUTHORITY_SCHEMA_MIGRATIONS = """
INSERT INTO schema_migrations(version, applied_at)
VALUES ('agent_knowledge_object_review_proposals.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
INSERT INTO schema_migrations(version, applied_at)
VALUES ('agent_knowledge_object_authority_decisions.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
INSERT INTO schema_migrations(version, applied_at)
VALUES ('agent_knowledge_object_authority_states.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
"""


def _reference_corpus_default_policy_status() -> dict:
    return {
        "supported_storage_modes": ["external_object_store", "managed_snapshot", "metadata_only"],
        "raw_body_policy": {
            "raw_body_policy": "no_raw_return_by_default",
            "return_capability": "denied_without_explicit_approval",
            "retention_class": "user_managed_reference",
            "redaction_profile": "public_safe_summary",
            "deletion_policy": "delete_snapshot_keep_metadata",
            "license_source_rights": "operator_attested",
        },
        "source_rights_policy": "operator_attested_reference_use",
        "production_ingest_gate": "approved_bounded_cli_gate_required",
    }


def _object_proposal_status_for_decision(decision_type: str, new_authority_lane: str) -> str:
    if decision_type == "reject_candidate" or new_authority_lane == "rejected":
        return "rejected"
    if decision_type == "rollback_decision":
        return "rolled_back"
    if decision_type == "commit_stale":
        return "stale"
    if decision_type == "commit_supersession":
        return "superseded"
    if decision_type == "retire":
        return "retired"
    return "accepted"


def _introspection_query(
    connection: sqlite3.Connection,
    table: str,
    *,
    query_type: str,
) -> tuple[str, tuple[str, ...]]:
    if getattr(connection, "dialect", "sqlite") == "postgres":
        if query_type == "table":
            return (
                "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = ?",
                (table,),
            )
        if query_type == "columns":
            return (
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = ?",
                (table,),
            )
    elif query_type == "table":
        return (
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        )
    elif query_type == "columns":
        _assert_safe_sql_identifier(table)
        return (f"PRAGMA table_info({table})", ())
    raise ValueError(f"unknown introspection query type: {query_type}")


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    sql, params = _introspection_query(connection, table, query_type="table")
    row = connection.execute(sql, params).fetchone()
    return row is not None


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(connection, table):
        return set()
    sql, params = _introspection_query(connection, table, query_type="columns")
    if getattr(connection, "dialect", "sqlite") == "postgres":
        return {str(row.get("column_name") or "") for row in connection.execute(sql, params).fetchall()}
    return {str(row[1]) for row in connection.execute(sql, params).fetchall()}


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    _assert_safe_sql_identifier(table)
    _assert_safe_sql_identifier(column)
    if column in _column_names(connection, table):
        return
    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _copy_column_if_present(
    connection: sqlite3.Connection,
    table: str,
    *,
    old_column: str,
    new_column: str,
) -> None:
    _assert_safe_sql_identifier(table)
    _assert_safe_sql_identifier(old_column)
    _assert_safe_sql_identifier(new_column)
    columns = _column_names(connection, table)
    if old_column not in columns or new_column not in columns:
        return
    connection.execute(
        f"""
        UPDATE {table}
        SET {new_column} = {old_column}
        WHERE ({new_column} IS NULL OR {new_column} = '')
          AND {old_column} IS NOT NULL
          AND {old_column} != ''
        """
    )


def _migrate_backend_neutral_index_schema(connection: sqlite3.Connection) -> None:
    _ensure_column(connection, "knowledge_items", "index_target_id", "TEXT DEFAULT ''")
    _ensure_column(connection, "knowledge_items", "index_document_id", "TEXT DEFAULT ''")
    _ensure_column(connection, "knowledge_items", "index_run_id", "TEXT DEFAULT ''")
    for old_column in ("ragflow_dataset_id", "index_dataset_id"):
        _copy_column_if_present(
            connection,
            "knowledge_items",
            old_column=old_column,
            new_column="index_target_id",
        )
    _copy_column_if_present(
        connection,
        "knowledge_items",
        old_column="ragflow_document_id",
        new_column="index_document_id",
    )
    for old_column in ("ragflow_run", "index_run"):
        _copy_column_if_present(
            connection,
            "knowledge_items",
            old_column=old_column,
            new_column="index_run_id",
        )
    _ensure_column(connection, "native_memory_mirror", "index_memory_id", "TEXT DEFAULT ''")
    _ensure_column(connection, "native_memory_mirror", "index_disabled_at", "TEXT DEFAULT ''")
    _copy_column_if_present(
        connection,
        "native_memory_mirror",
        old_column="ragflow_memory_id",
        new_column="index_memory_id",
    )
    _copy_column_if_present(
        connection,
        "native_memory_mirror",
        old_column="ragflow_disabled_at",
        new_column="index_disabled_at",
    )
    _ensure_column(connection, "memory_gc_audit", "index_document_id_hash", "TEXT NOT NULL DEFAULT ''")
    _copy_column_if_present(
        connection,
        "memory_gc_audit",
        old_column="ragflow_document_id_hash",
        new_column="index_document_id_hash",
    )
    if _table_exists(connection, "index_targets"):
        _copy_index_targets_from_legacy_table(connection, "ragflow_datasets")
        _copy_index_targets_from_legacy_table(connection, "index_datasets")


def _copy_index_targets_from_legacy_table(connection: sqlite3.Connection, legacy_table: str) -> None:
    _assert_safe_sql_identifier(legacy_table)
    if not _table_exists(connection, legacy_table):
        return
    legacy_columns = _column_names(connection, legacy_table)
    required_columns = {
        "logical_name",
        "dataset_id",
        "embedding_model",
        "chunk_method",
        "metadata_policy_version",
        "contract_version",
        "created_at",
        "enabled",
        "disabled_at",
    }
    if not required_columns.issubset(legacy_columns):
        return
    if getattr(connection, "dialect", "sqlite") == "postgres":
        connection.execute(
            f"""
            INSERT INTO index_targets (
                logical_name, dataset_id, embedding_model, chunk_method,
                metadata_policy_version, contract_version, created_at, enabled, disabled_at
            )
            SELECT logical_name, dataset_id, embedding_model, chunk_method,
                   metadata_policy_version, contract_version, created_at, enabled, disabled_at
            FROM {legacy_table}
            ON CONFLICT DO NOTHING
            """
        )
    else:
        connection.execute(
            f"""
            INSERT OR IGNORE INTO index_targets (
                logical_name, dataset_id, embedding_model, chunk_method,
                metadata_policy_version, contract_version, created_at, enabled, disabled_at
            )
            SELECT logical_name, dataset_id, embedding_model, chunk_method,
                   metadata_policy_version, contract_version, created_at, enabled, disabled_at
            FROM {legacy_table}
            """
        )


def _assert_safe_sql_identifier(value: str) -> None:
    if not value.replace("_", "").isalnum():
        raise ValueError("unsafe SQL identifier")


def _read_only_sql_allowed(sql: str) -> bool:
    match = _READ_ONLY_SQL_KEYWORD_RE.match(sql)
    if match is None:
        return not sql.strip()
    keyword = match.group(1).upper()
    if keyword in _READ_ONLY_SQL_ALLOWED_KEYWORDS:
        return True
    if keyword == "WITH":
        return _read_only_cte_sql_allowed(sql[match.start(1):])
    return False


def _read_only_cte_sql_allowed(sql: str) -> bool:
    """Allow read-only CTE queries without allowing write CTE statements.

    SQLite permits `WITH ... SELECT` and `WITH ... INSERT/UPDATE/DELETE`.
    The read-only ledger guard therefore cannot treat every statement starting
    with WITH as safe. This scanner follows the CTE declarations and allows
    only a final read statement.
    """

    index = _skip_sql_keyword(sql, 0, "WITH")
    if index is None:
        return False
    index = _skip_sql_ws_and_comments(sql, index)
    recursive_index = _skip_sql_keyword(sql, index, "RECURSIVE")
    if recursive_index is not None:
        index = _skip_sql_ws_and_comments(sql, recursive_index)
    while True:
        index = _skip_sql_identifier(sql, index)
        if index is None:
            return False
        index = _skip_sql_ws_and_comments(sql, index)
        if index < len(sql) and sql[index] == "(":
            index = _scan_sql_balanced_parentheses(sql, index)
            if index is None:
                return False
            index = _skip_sql_ws_and_comments(sql, index)
        index = _skip_sql_keyword(sql, index, "AS")
        if index is None:
            return False
        index = _skip_sql_ws_and_comments(sql, index)
        if index >= len(sql) or sql[index] != "(":
            return False
        index = _scan_sql_balanced_parentheses(sql, index)
        if index is None:
            return False
        index = _skip_sql_ws_and_comments(sql, index)
        if index < len(sql) and sql[index] == ",":
            index = _skip_sql_ws_and_comments(sql, index + 1)
            continue
        match = re.match(r"([A-Za-z]+)", sql[index:])
        if match is None:
            return False
        return match.group(1).upper() in _READ_ONLY_SQL_CTE_FINAL_KEYWORDS


def _skip_sql_ws_and_comments(sql: str, index: int) -> int:
    while index < len(sql):
        if sql[index].isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            return len(sql) if newline == -1 else _skip_sql_ws_and_comments(sql, newline + 1)
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            return len(sql) if end == -1 else _skip_sql_ws_and_comments(sql, end + 2)
        break
    return index


def _skip_sql_keyword(sql: str, index: int, keyword: str) -> int | None:
    match = re.match(r"([A-Za-z]+)", sql[index:])
    if match is None or match.group(1).upper() != keyword:
        return None
    end = index + len(match.group(1))
    if end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
        return None
    return end


def _skip_sql_identifier(sql: str, index: int) -> int | None:
    if index >= len(sql):
        return None
    quote_pairs = {'"': '"', "`": "`", "[": "]"}
    if sql[index] in quote_pairs:
        closing = quote_pairs[sql[index]]
        cursor = index + 1
        while cursor < len(sql):
            if sql[cursor] == closing:
                return cursor + 1
            cursor += 1
        return None
    match = re.match(r"[A-Za-z_][A-Za-z0-9_.$]*", sql[index:])
    return index + len(match.group(0)) if match is not None else None


def _scan_sql_balanced_parentheses(sql: str, index: int) -> int | None:
    if index >= len(sql) or sql[index] != "(":
        return None
    depth = 0
    quote = ""
    cursor = index
    while cursor < len(sql):
        char = sql[cursor]
        if quote:
            if char == quote:
                if quote == "'" and cursor + 1 < len(sql) and sql[cursor + 1] == "'":
                    cursor += 2
                    continue
                quote = ""
            cursor += 1
            continue
        if sql.startswith("--", cursor):
            newline = sql.find("\n", cursor + 2)
            cursor = len(sql) if newline == -1 else newline + 1
            continue
        if sql.startswith("/*", cursor):
            end = sql.find("*/", cursor + 2)
            if end == -1:
                return None
            cursor = end + 2
            continue
        if char in {"'", '"', "`"}:
            quote = char
            cursor += 1
            continue
        if char == "[":
            quote = "]"
            cursor += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return cursor + 1
            if depth < 0:
                return None
        cursor += 1
    return None


class _ReadOnlyLedgerConnection:
    """Fail-closed guard for server-backed read-only ledger connections."""

    def __init__(self, connection):
        self._connection = connection

    def __enter__(self):
        entered = self._connection.__enter__()
        if entered is not None:
            self._connection = entered
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return self._connection.__exit__(exc_type, exc, tb)

    def __getattr__(self, name: str):
        return getattr(self._connection, name)

    def execute(self, sql: str, params=None):
        if not _read_only_sql_allowed(sql):
            raise sqlite3.OperationalError("read-only ledger는 write SQL을 허용하지 않습니다")
        if params is None:
            return self._connection.execute(sql)
        return self._connection.execute(sql, params)

    def executescript(self, script: str) -> None:
        if script.strip():
            raise sqlite3.OperationalError("read-only ledger는 SQL script execution을 허용하지 않습니다")
        return self._connection.executescript(script)


class _LedgerTransaction:
    """다중 write ledger workflow를 위한 M1 private transaction-bound facade."""

    def __init__(self, ledger: "Ledger", connection):
        self._ledger = ledger
        self._connection = connection
        self._indexed_knowledge_ids: list[str] = []

    def upsert_llm_brain_memory_card(self, card: dict) -> dict:
        # 공유 connection 으로 실행 — restricted commit 의 다중 write 를 한 트랜잭션으로 묶는다.
        from .ledger_native_memory_mixin import upsert_llm_brain_memory_card_on

        return upsert_llm_brain_memory_card_on(self._connection, card)

    def upsert_llm_brain_feedback_record(self, record: dict) -> dict:
        from .ledger_native_memory_mixin import upsert_llm_brain_feedback_record_on

        return upsert_llm_brain_feedback_record_on(self._connection, record)

    def upsert_memory_card(self, card: dict) -> dict:
        self._connection.execute(
            """
            INSERT INTO memory_cards (
                memory_id, candidate_id, card_type, project, provider, title,
                summary, content_hash, state, approved_by, approved_at, supersedes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                candidate_id=excluded.candidate_id,
                card_type=excluded.card_type,
                project=excluded.project,
                provider=excluded.provider,
                title=excluded.title,
                summary=excluded.summary,
                content_hash=excluded.content_hash,
                state=excluded.state,
                approved_by=excluded.approved_by,
                approved_at=excluded.approved_at,
                supersedes=excluded.supersedes
            """,
            (
                card["memory_id"],
                card["candidate_id"],
                card["card_type"],
                card["project"],
                card["provider"],
                card["title"],
                card["summary"],
                card["content_hash"],
                card.get("state", "active"),
                card["approved_by"],
                card["approved_at"],
                card.get("supersedes", ""),
            ),
        )
        self._upsert_prepared(
            knowledge_id=card["memory_id"],
            content_hash=card["content_hash"],
            provider=card["provider"],
            project=card["project"],
            domain="agent_memory",
            type="memory_card",
            title=card["title"],
            summary=card["summary"],
            privacy_level="private",
        )
        self._mark_uploaded(
            card["memory_id"],
            dataset_id=card.get("index_target_id") or "local-approved-memory-cards",
            document_id=card.get("index_document_id") or f"memdoc_{card['memory_id']}",
            run="LOCAL",
        )
        self._mark_indexed(card["memory_id"], run="LOCAL")
        return self.get_memory_card(card["memory_id"])

    def upsert_object_review_proposal(self, proposal: dict) -> dict:
        return self._ledger._upsert_object_review_proposal_on(self._connection, proposal)

    def commit_object_authority_decision(self, decision: dict) -> dict:
        return self._ledger._commit_object_authority_decision_on(self._connection, decision)

    def add_memory_card_evidence(self, memory_id: str, evidence_refs: list[dict]) -> None:
        for ref in evidence_refs:
            self._connection.execute(
                """
                INSERT INTO memory_card_evidence (memory_id, knowledge_id, content_hash)
                VALUES (?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (memory_id, ref["knowledge_id"], ref["content_hash"]),
            )

    def update_memory_candidate_state(
        self,
        candidate_id: str,
        state: str,
        *,
        reviewed_by: str = "",
        reason: str = "",
    ) -> dict:
        reviewed_at = datetime.now(timezone.utc).isoformat()
        self._connection.execute(
            """
            UPDATE memory_candidates
            SET approval_state = ?, reviewed_at = ?, reviewed_by = ?, review_reason = ?
            WHERE candidate_id = ?
            """,
            (state, reviewed_at, reviewed_by, reason, candidate_id),
        )
        row = self._connection.execute(
            "SELECT * FROM memory_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown memory candidate: {candidate_id}")
        return _memory_candidate_from_row(row)

    def upsert_profile_fact(
        self,
        *,
        memory_id: str,
        project: str,
        fact_type: str,
        content_hash: str,
        state: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO profile_facts (memory_id, project, fact_type, content_hash, state)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                project=excluded.project,
                fact_type=excluded.fact_type,
                content_hash=excluded.content_hash,
                state=excluded.state
            """,
            (memory_id, project, fact_type, content_hash, state),
        )

    def get_memory_card(self, memory_id: str) -> dict | None:
        row = self._connection.execute(
            """
            SELECT mc.*, ki.index_target_id, ki.index_document_id, ki.status AS ledger_status
            FROM memory_cards mc
            LEFT JOIN knowledge_items ki ON ki.knowledge_id = mc.memory_id
            WHERE mc.memory_id = ?
            """,
            (memory_id,),
        ).fetchone()
        return dict(row) if row else None

    def _upsert_prepared(
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
    ) -> dict:
        metadata_json = _normalize_metadata_json(None)
        bounded_summary = summary[:500]
        existing = self._connection.execute(
            "SELECT * FROM knowledge_items WHERE knowledge_id = ?",
            (knowledge_id,),
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] != content_hash:
                if (
                    existing["status"] != "prepared"
                    or existing["index_target_id"]
                    or existing["index_document_id"]
                    or existing["ingress_job_id"]
                    or existing["queued_at"]
                    or existing["indexed_at"]
                ):
                    raise ValueError("cannot change content hash for a delivered knowledge item")
                content_owner = self._connection.execute(
                    "SELECT knowledge_id FROM knowledge_items WHERE content_hash = ?",
                    (content_hash,),
                ).fetchone()
                if content_owner is not None and content_owner["knowledge_id"] != knowledge_id:
                    raise ValueError("content hash already belongs to another knowledge item")
                self._connection.execute(
                    """
                    UPDATE knowledge_items
                    SET content_hash=?,
                        provider=?,
                        project=?,
                        domain=?,
                        type=?,
                        title=?,
                        summary=?,
                        privacy_level=?,
                        status='prepared',
                        index_target_id='',
                        index_document_id='',
                        ingress_target_profile='',
                        ingress_job_id='',
                        queued_at='',
                        index_run_id='',
                        index_progress=0,
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
                        title,
                        bounded_summary,
                        privacy_level,
                        knowledge_id,
                    ),
                )
                return self._get_by_knowledge_id(knowledge_id) or {}
            self._connection.execute(
                """
                UPDATE knowledge_items
                SET provider=?,
                    project=?,
                    domain=?,
                    type=?,
                    title=?,
                    summary=?,
                    privacy_level=?,
                    status='prepared',
                    index_target_id='',
                    index_document_id='',
                    ingress_target_profile='',
                    ingress_job_id='',
                    queued_at='',
                    index_run_id='',
                    index_progress=0,
                    indexed_at='',
                    disabled_at='',
                    authorization_status='active'
                WHERE knowledge_id=?
                """,
                (
                    provider,
                    project,
                    domain,
                    type,
                    title,
                    bounded_summary,
                    privacy_level,
                    knowledge_id,
                ),
            )
            return self._get_by_knowledge_id(knowledge_id) or {}
        content_owner = self._connection.execute(
            "SELECT knowledge_id FROM knowledge_items WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
        if content_owner is not None and content_owner["knowledge_id"] != knowledge_id:
            raise ValueError("content hash already belongs to another knowledge item")
        self._connection.execute(
            """
            INSERT INTO knowledge_items (
                knowledge_id, content_hash, provider, project, domain, type,
                title, summary, privacy_level, metadata_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared')
            """,
            (
                knowledge_id,
                content_hash,
                provider,
                project,
                domain,
                type,
                title,
                bounded_summary,
                privacy_level,
                metadata_json,
            ),
        )
        return self._get_by_knowledge_id(knowledge_id) or {}

    def _mark_uploaded(self, knowledge_id: str, *, dataset_id: str, document_id: str, run: str) -> None:
        self._update_status(
            knowledge_id,
            "uploaded_unparsed",
            index_target_id=dataset_id,
            index_document_id=document_id,
            ingress_target_profile="",
            ingress_job_id="",
            queued_at="",
            index_run_id=run,
            indexed_at="",
        )

    def _mark_indexed(self, knowledge_id: str, *, run: str) -> None:
        self._update_status(
            knowledge_id,
            "indexed",
            index_run_id=run,
            index_progress=1.0,
            indexed_at=datetime.now(timezone.utc).isoformat(),
        )
        self._indexed_knowledge_ids.append(knowledge_id)

    def _run_after_commit_hooks(self) -> None:
        for knowledge_id in self._indexed_knowledge_ids:
            self._ledger._maybe_mark_session_memory_dirty_for_indexed_item(knowledge_id)
            self._ledger._maybe_mark_project_memory_dirty_for_indexed_item(knowledge_id)

    def _update_status(self, knowledge_id: str, status: str, **fields) -> None:
        assignments = ["status = ?"]
        values = [status]
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            values.append(value)
        values.append(knowledge_id)
        self._connection.execute(
            f"UPDATE knowledge_items SET {', '.join(assignments)} WHERE knowledge_id = ?",
            values,
        )

    def _get_by_knowledge_id(self, knowledge_id: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM knowledge_items WHERE knowledge_id = ?",
            (knowledge_id,),
        ).fetchone()
        return dict(row) if row else None


class Ledger(
    IngressStatusMixin, GcSafetyMixin, MemoryPromotionMixin, NativeMemoryMixin,
):
    _REQUIRED_EXISTING_SCHEMA_TABLES = (
        "knowledge_items",
        "memory_candidates",
        "schema_migrations",
    )

    def __init__(
        self,
        path: Path | str,
        *,
        read_only: bool = False,
        db_adapter=None,
        initialize_schema: bool = True,
    ):
        self.path = Path(path)
        self.read_only = bool(read_only)
        self._temp_dir: Path | None = None
        self._transaction_active = False
        self._memory_promotion_area_impl = MemoryPromotionArea(self)
        # B: DB 엔진 접근 seam. None이면 현행 SQLite 어댑터를 lazy 생성(behavior-preserving).
        self._db_adapter = db_adapter
        # C cutover switch: 명시 어댑터가 없고 NEURON_LEDGER_PG_DSN 이 설정돼 있으면 PostgreSQL
        # 엔진을 기본으로 쓴다(엔진 flip = 환경변수 하나). 미설정이면 SQLite(기존 동작 그대로).
        if self._db_adapter is None:
            _pg_dsn = os.environ.get("NEURON_LEDGER_PG_DSN", "")
            if _pg_dsn:
                from .postgres_db_adapter import PostgresLedgerDbAdapter

                self._db_adapter = PostgresLedgerDbAdapter(_pg_dsn)
        # C: 파일 기반 엔진(SQLite)만 파일 권한 준비/하드닝을 한다. 서버형(Postgres)은 skip.
        file_backed = (
            True if self._db_adapter is None else getattr(self._db_adapter, "is_file_backed", True)
        )
        if not self.read_only:
            if file_backed:
                if initialize_schema:
                    self._prepare_parent_directory()
                else:
                    self._validate_existing_file_backed_schema()
            if initialize_schema:
                self._initialize()
            if file_backed:
                for p in self.path.parent.glob(f"{self.path.name}*"):
                    try:
                        os.chmod(p, 0o600)
                    except OSError:
                        pass
            return
        if file_backed:
            self.path = self._snapshot_read_only_copy(self.path)

    @classmethod
    def open_read_only(cls, path: Path | str) -> "Ledger":
        if not Path(path).exists() and not os.environ.get("NEURON_LEDGER_PG_DSN", ""):
            raise ValueError(f"ledger path does not exist: {path}")
        return cls(path, read_only=True)

    def __del__(self) -> None:
        if self._temp_dir is not None:
            try:
                shutil.rmtree(self._temp_dir)
            except OSError:
                pass

    @property
    def _memory_promotion_area(self) -> MemoryPromotionArea:
        return self._memory_promotion_area_impl

    def _validate_existing_file_backed_schema(self) -> None:
        parent = self.path.parent
        if parent.is_symlink():
            raise ValueError("ledger parent must not be a symlink")
        if not self.path.exists():
            raise ValueError(f"ledger path does not exist: {self.path}")
        mode = parent.stat().st_mode & 0o777
        if mode & 0o077:
            raise ValueError("ledger parent must be private")
        adapter = SqliteLedgerDbAdapter(self.path, read_only=True)
        try:
            with adapter.connect() as connection:
                missing = [
                    table
                    for table in self._REQUIRED_EXISTING_SCHEMA_TABLES
                    if not _table_exists(connection, table)
                ]
        except sqlite3.DatabaseError as exc:
            raise ValueError("ledger schema is not initialized") from exc
        if missing:
            raise ValueError("ledger schema is not initialized")

    def _snapshot_read_only_copy(self, source_path: Path) -> Path:
        if not source_path.exists():
            raise ValueError(f"ledger path does not exist: {source_path}")
        snapshot_dir = Path(tempfile.mkdtemp(prefix="agent-knowledge-ledger-ro-"))
        self._temp_dir = snapshot_dir
        for source_file in source_path.parent.glob(f"{source_path.name}*"):
            shutil.copy2(source_file, snapshot_dir / source_file.name)
        return snapshot_dir / source_path.name

    def _prepare_parent_directory(self) -> None:
        parent = self.path.parent
        if parent.is_symlink():
            raise ValueError("ledger parent must not be a symlink")
        existed = parent.exists()
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not existed:
            os.chmod(parent, 0o700)
            return
        mode = parent.stat().st_mode & 0o777
        if mode & 0o077:
            raise ValueError("ledger parent must be private")

    def _connect(self, *, configure_journal: bool = False) -> sqlite3.Connection:
        # B: 연결 생성을 ILedgerCoreDbAdapter 경계 뒤로. 기본은 현행 SQLite 어댑터(동작
        # 동일). C에서 PostgreSQL 어댑터를 주입하면 이 한 점에서 엔진이 바뀐다.
        if self._db_adapter is None:
            self._db_adapter = SqliteLedgerDbAdapter(self.path, read_only=self.read_only)
        connection = self._db_adapter.connect(configure_journal=configure_journal)
        if self.read_only:
            return _ReadOnlyLedgerConnection(connection)
        return connection

    def ensure_object_authority_schema(self) -> dict:
        if self.read_only:
            raise sqlite3.OperationalError("read-only ledger는 object authority schema ensure를 허용하지 않습니다")
        with self._connect(configure_journal=True) as connection:
            connection.executescript(
                _SCHEMA_MIGRATIONS_SCHEMA
                + _OBJECT_AUTHORITY_SCHEMA
                + _OBJECT_AUTHORITY_SCHEMA_MIGRATIONS
            )
        server_backed_ledger = not bool(getattr(self._db_adapter, "is_file_backed", True))
        result = {
            "schema_version": "object_authority_schema_ensure.v1",
            "status": "ensured",
            "tables": [
                "object_review_proposals",
                "object_authority_decisions",
                "object_authority_states",
            ],
            "schema_migrations": [
                "agent_knowledge_object_review_proposals.v1",
                "agent_knowledge_object_authority_decisions.v1",
                "agent_knowledge_object_authority_states.v1",
            ],
            "mutation_performed": True,
            "network_used": server_backed_ledger,
            "server_backed_ledger": server_backed_ledger,
            "protected_values_returned": False,
        }
        ensure_public_safe(result, "object_authority_schema_ensure")
        return result

    @contextmanager
    def _transaction(self):
        if self.read_only:
            raise sqlite3.OperationalError("read-only ledger는 write transaction을 허용하지 않습니다")
        if self._transaction_active:
            raise RuntimeError("중첩 ledger transaction은 지원하지 않습니다")
        self._transaction_active = True
        tx = None
        try:
            with self._connect() as connection:
                tx = _LedgerTransaction(self, connection)
                yield tx
            tx._run_after_commit_hooks()
        finally:
            self._transaction_active = False

    def _initialize(self) -> None:
        # Lazy import to avoid a module-load circular import: ledger_adapter lives
        # in the llm_brain_core package whose __init__ imports modules that import
        # this ledger module. By initialize-time this module is fully loaded, so
        # referencing the single-source schema constant here is safe.
        from .llm_brain_core.ledger_adapter import (
            _GRAPH_PROJECTION_STATE_SCHEMA,
            _migrate_extraction_level,
        )

        with self._connect(configure_journal=True) as connection:
            # Lazy non-destructive upgrade of a pre-M2 projection_state table
            # BEFORE the schema script runs: the script's extraction_level index
            # must not execute before the migration adds that column. No-op on a
            # fresh ledger (table absent) and on an already-new-shape table.
            _migrate_extraction_level(connection)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_items (
                    knowledge_id TEXT PRIMARY KEY,
                    content_hash TEXT UNIQUE NOT NULL,
                    provider TEXT NOT NULL,
                    project TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    type TEXT NOT NULL,
                    session_id_hash TEXT DEFAULT '',
                    observed_at TEXT DEFAULT '',
                    ingested_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT '',
                    privacy_level TEXT NOT NULL DEFAULT 'normal',
                    index_target_id TEXT DEFAULT '',
                    index_document_id TEXT DEFAULT '',
                    ingress_target_profile TEXT DEFAULT '',
                    ingress_job_id TEXT DEFAULT '',
                    queued_at TEXT DEFAULT '',
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    source_event_ids TEXT DEFAULT '',
                    redaction_version TEXT DEFAULT 'redaction.v1',
                    confidence TEXT DEFAULT 'medium',
                    supersedes TEXT DEFAULT '',
                    valid_until TEXT DEFAULT '',
                    evidence_status TEXT DEFAULT 'historical',
                    coverage_status TEXT DEFAULT '',
                    coverage_gap_count INTEGER DEFAULT 0,
                    coverage_duplicate_count INTEGER DEFAULT 0,
                    source_manifest_hash TEXT DEFAULT '',
                    source_chunk_count INTEGER DEFAULT 0,
                    index_run_id TEXT DEFAULT '',
                    index_progress REAL DEFAULT 0,
                    indexed_at TEXT DEFAULT '',
                    disabled_at TEXT DEFAULT '',
                    authorization_status TEXT DEFAULT 'active',
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ingest_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    knowledge_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_class TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    completed_at TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS index_targets (
                    logical_name TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL,
                    embedding_model TEXT DEFAULT '',
                    chunk_method TEXT DEFAULT '',
                    metadata_policy_version TEXT DEFAULT '',
                    contract_version TEXT DEFAULT '',
                    created_at TEXT DEFAULT '',
                    enabled INTEGER DEFAULT 1,
                    disabled_at TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS qdrant_collections (
                    logical_name TEXT PRIMARY KEY,
                    collection TEXT NOT NULL,
                    embedding_model TEXT DEFAULT '',
                    vector_size INTEGER DEFAULT 0,
                    distance TEXT DEFAULT '',
                    payload_index_version TEXT DEFAULT '',
                    created_at TEXT DEFAULT '',
                    enabled INTEGER DEFAULT 1,
                    disabled_at TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS transcript_sessions (
                    session_id_hash TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    project TEXT NOT NULL,
                    started_at TEXT DEFAULT '',
                    ended_at TEXT DEFAULT '',
                    source_status TEXT NOT NULL DEFAULT 'source_unproven',
                    source_locator_hash TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transcript_turns (
                    turn_id_hash TEXT PRIMARY KEY,
                    session_id_hash TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    observed_at TEXT DEFAULT '',
                    redacted_text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transcript_tool_events (
                    tool_event_id_hash TEXT PRIMARY KEY,
                    turn_id_hash TEXT NOT NULL,
                    event_index INTEGER NOT NULL,
                    tool_name TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    redacted_summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transcript_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    knowledge_id TEXT NOT NULL UNIQUE,
                    session_id_hash TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    project TEXT NOT NULL,
                    turn_start_index INTEGER NOT NULL,
                    turn_end_index INTEGER NOT NULL,
                    part_index INTEGER NOT NULL DEFAULT 1,
                    part_count INTEGER NOT NULL DEFAULT 1,
                    char_start INTEGER NOT NULL DEFAULT 0,
                    char_end INTEGER NOT NULL DEFAULT 0,
                    content_hash TEXT NOT NULL,
                    redacted_text TEXT NOT NULL,
                    source_status TEXT NOT NULL,
                    redaction_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transcript_validation_files (
                    legacy_document_id_hash TEXT NOT NULL,
                    validation_dataset_id TEXT NOT NULL,
                    source_dataset_id_hash TEXT NOT NULL,
                    source_locator_hash TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    project TEXT NOT NULL,
                    turn_start_index INTEGER NOT NULL,
                    turn_end_index INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    validation_document_ids_json TEXT NOT NULL,
                    validation_knowledge_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(legacy_document_id_hash, validation_dataset_id)
                );
                CREATE TABLE IF NOT EXISTS provider_source_contracts (
                    provider TEXT PRIMARY KEY,
                    contract_id TEXT NOT NULL,
                    provider_version TEXT NOT NULL,
                    installed_version_evidence TEXT DEFAULT '',
                    hook_event TEXT DEFAULT '',
                    source_locator_field TEXT DEFAULT '',
                    parser_version TEXT DEFAULT '',
                    native_parser_status TEXT DEFAULT '',
                    privacy_redaction_status TEXT DEFAULT '',
                    verification_status TEXT NOT NULL,
                    source_status TEXT NOT NULL,
                    hook_install_status TEXT NOT NULL,
                    rollback_state TEXT DEFAULT '',
                    evidence_hash TEXT NOT NULL,
                    redacted_evidence_ref TEXT DEFAULT '',
                    raw_prompt_policy TEXT DEFAULT '',
                    unsupported_reason TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backfill_sources (
                    source_id TEXT PRIMARY KEY,
                    raw_source_path TEXT NOT NULL,
                    source_path_hash TEXT NOT NULL UNIQUE,
                    project TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    provider_contract_status TEXT DEFAULT '',
                    source_contract_status TEXT DEFAULT '',
                    parser_status TEXT DEFAULT '',
                    inventory_status TEXT NOT NULL,
                    quarantine_reason TEXT DEFAULT '',
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scheduler_runs (
                    run_id TEXT PRIMARY KEY,
                    scheduler_id TEXT NOT NULL,
                    command_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT DEFAULT '',
                    error_class TEXT DEFAULT '',
                    argv_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    candidate_type TEXT NOT NULL,
                    project TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    sensitivity TEXT NOT NULL,
                    requires_manual_approval INTEGER NOT NULL,
                    approval_state TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT DEFAULT '',
                    reviewed_by TEXT DEFAULT '',
                    review_reason TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS memory_cards (
                    memory_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    card_type TEXT NOT NULL,
                    project TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    approved_at TEXT NOT NULL,
                    supersedes TEXT DEFAULT '',
                    disabled_at TEXT DEFAULT '',
                    disabled_by TEXT DEFAULT '',
                    disable_reason TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS memory_card_evidence (
                    memory_id TEXT NOT NULL,
                    knowledge_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    PRIMARY KEY(memory_id, knowledge_id)
                );
                CREATE TABLE IF NOT EXISTS llm_brain_memory_cards (
                    memory_id TEXT PRIMARY KEY,
                    brain_id TEXT NOT NULL,
                    card_type TEXT NOT NULL,
                    project TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    lifecycle_state TEXT NOT NULL,
                    judgment_state TEXT NOT NULL,
                    approval_state TEXT NOT NULL,
                    currentness TEXT NOT NULL,
                    status TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    accepted_at TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS llm_brain_feedback_records (
                    feedback_id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    repo_id TEXT NOT NULL,
                    final_status TEXT NOT NULL,
                    user_action TEXT NOT NULL,
                    conflict_state TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS llm_brain_projection_jobs (
                    job_id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    job_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS llm_brain_session_memory_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    session_id_hash TEXT NOT NULL,
                    project TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    artifact_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_llm_brain_artifacts_project_created
                    ON llm_brain_session_memory_artifacts(project, created_at);
                CREATE INDEX IF NOT EXISTS idx_llm_brain_artifacts_session
                    ON llm_brain_session_memory_artifacts(session_id_hash);
                CREATE TABLE IF NOT EXISTS llm_brain_source_refs (
                    source_ref_id TEXT PRIMARY KEY,
                    device_id_hash TEXT NOT NULL,
                    root_id TEXT NOT NULL,
                    relative_path_hash TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    sync_policy TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    last_seen_at TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_llm_brain_source_refs_device_root
                    ON llm_brain_source_refs(device_id_hash, root_id);
                CREATE INDEX IF NOT EXISTS idx_llm_brain_source_refs_content_hash
                    ON llm_brain_source_refs(content_hash);
                CREATE TABLE IF NOT EXISTS profile_facts (
                    memory_id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    fact_type TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_packs (
                    pack_id TEXT PRIMARY KEY,
                    prompt_hash TEXT NOT NULL,
                    filters_json TEXT NOT NULL,
                    item_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_pack_items (
                    pack_id TEXT NOT NULL,
                    item_index INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    reference_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    score REAL,
                    metadata_json TEXT NOT NULL,
                    PRIMARY KEY(pack_id, item_index)
                );
                CREATE TABLE IF NOT EXISTS retrieval_audit (
                    audit_id TEXT PRIMARY KEY,
                    pack_id TEXT NOT NULL,
                    prompt_hash TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    filters_json TEXT NOT NULL,
                    result_count INTEGER NOT NULL,
                    private_allowed INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auto_recall_audit (
                    audit_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    project TEXT NOT NULL,
                    status TEXT NOT NULL,
                    policy_reasons_json TEXT NOT NULL,
                    private_policy_allowed INTEGER NOT NULL,
                    prompt_hash TEXT NOT NULL,
                    preview_hash TEXT NOT NULL,
                    context_pack_id TEXT NOT NULL,
                    selected_items_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS eval_queries (
                    query_id TEXT PRIMARY KEY,
                    query_hash TEXT NOT NULL,
                    query_terms_json TEXT NOT NULL,
                    project TEXT NOT NULL,
                    provider TEXT DEFAULT '',
                    expected_memory_ids_json TEXT NOT NULL,
                    k INTEGER NOT NULL,
                    min_recall REAL NOT NULL,
                    min_precision REAL NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS eval_runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    project TEXT DEFAULT '',
                    provider TEXT DEFAULT '',
                    k INTEGER NOT NULL,
                    query_count INTEGER NOT NULL,
                    metrics_json TEXT NOT NULL,
                    failures_json TEXT NOT NULL,
                    network_used INTEGER NOT NULL,
                    mutation_performed INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dirty_session_memory (
                    session_id_hash TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    project TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    source_knowledge_id TEXT DEFAULT '',
                    dirty_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT DEFAULT '',
                    last_error_class TEXT DEFAULT '',
                    last_summary_knowledge_id TEXT DEFAULT '',
                    last_ingress_job_id TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS session_memory_terminal_skipped_audit (
                    session_id_hash TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    project TEXT NOT NULL,
                    original_status TEXT NOT NULL,
                    terminal_status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    category TEXT NOT NULL,
                    source_knowledge_id TEXT DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    dirty_at TEXT NOT NULL,
                    skipped_at TEXT NOT NULL,
                    audited_at TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_memory_active_snapshots (
                    session_id_hash TEXT PRIMARY KEY,
                    active_knowledge_id TEXT NOT NULL,
                    active_content_hash TEXT NOT NULL,
                    previous_knowledge_id TEXT DEFAULT '',
                    activated_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_memory_coverage_edges (
                    active_knowledge_id TEXT NOT NULL,
                    source_content_hash TEXT NOT NULL,
                    source_window_hash TEXT NOT NULL,
                    derived_content_hash TEXT NOT NULL,
                    redaction_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    turn_start_index INTEGER DEFAULT 0,
                    turn_end_index INTEGER DEFAULT 0,
                    PRIMARY KEY (
                        active_knowledge_id,
                        source_content_hash,
                        source_window_hash,
                        derived_content_hash
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_session_memory_coverage_active_knowledge
                    ON session_memory_coverage_edges(active_knowledge_id);
                CREATE INDEX IF NOT EXISTS idx_session_memory_terminal_skipped_audit_status
                    ON session_memory_terminal_skipped_audit(terminal_status, category);
                CREATE TABLE IF NOT EXISTS dirty_project_memory (
                    project_key_hash TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    project TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    source_knowledge_id TEXT DEFAULT '',
                    dirty_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT DEFAULT '',
                    last_error_class TEXT DEFAULT '',
                    last_snapshot_knowledge_id TEXT DEFAULT '',
                    last_ingress_job_id TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS project_memory_active_snapshots (
                    project_key_hash TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    project TEXT NOT NULL,
                    active_knowledge_id TEXT NOT NULL,
                    active_content_hash TEXT NOT NULL,
                    previous_knowledge_id TEXT DEFAULT '',
                    activated_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tool_evidence_summaries (
                    evidence_id_hash TEXT PRIMARY KEY,
                    session_id_hash TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    project TEXT NOT NULL,
                    category TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    command_summary TEXT NOT NULL DEFAULT '',
                    redacted_summary TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    observed_at TEXT DEFAULT '',
                    evidence_index INTEGER NOT NULL DEFAULT 0,
                    redaction_version TEXT NOT NULL DEFAULT 'redaction.v2',
                    source_status TEXT NOT NULL DEFAULT 'source_locator_private_spool_only',
                    knowledge_id TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'prepared',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_transcript_turns_session_turn_observed
                    ON transcript_turns(session_id_hash, turn_index, observed_at);
                CREATE INDEX IF NOT EXISTS idx_transcript_chunks_project_provider_session_turn
                    ON transcript_chunks(project, provider, session_id_hash, turn_start_index, turn_end_index, chunk_id);
                CREATE INDEX IF NOT EXISTS idx_tool_evidence_summaries_session
                    ON tool_evidence_summaries(project, provider, session_id_hash, category, evidence_index);
                CREATE INDEX IF NOT EXISTS idx_knowledge_items_type_status
                    ON knowledge_items(type, status);
                CREATE TABLE IF NOT EXISTS native_memory_mirror (
                    statement_id TEXT PRIMARY KEY,
                    brain_id TEXT NOT NULL,
                    session_tag TEXT UNIQUE NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    superseded_by TEXT DEFAULT '',
                    original_content_hash TEXT NOT NULL,
                    search_text TEXT NOT NULL DEFAULT '',
                    card_type TEXT NOT NULL DEFAULT '',
                    index_memory_id TEXT DEFAULT '',
                    index_disabled_at TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    superseded_at TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_native_memory_mirror_brain_status
                    ON native_memory_mirror(brain_id, status);
                CREATE TABLE IF NOT EXISTS memory_gc_audit (
                    audit_id TEXT PRIMARY KEY,
                    gc_kind TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    knowledge_id TEXT NOT NULL,
                    index_document_id_hash TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    replacement_knowledge_id TEXT NOT NULL,
                    dirty_at TEXT NOT NULL DEFAULT '',
                    snapshot_updated_at TEXT NOT NULL DEFAULT '',
                    approval_operation TEXT NOT NULL DEFAULT '',
                    age_gate_seconds INTEGER NOT NULL DEFAULT 0,
                    mutated INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_gc_audit_kind_created
                    ON memory_gc_audit(gc_kind, created_at);
                """
                + _OBJECT_AUTHORITY_SCHEMA
                + """
                CREATE TABLE IF NOT EXISTS reference_corpus_bundles (
                    corpus_id TEXT PRIMARY KEY,
                    project TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    storage_mode TEXT NOT NULL DEFAULT '',
                    manifest_hash TEXT NOT NULL DEFAULT '',
                    source_count INTEGER NOT NULL DEFAULT 0,
                    bundle_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reference_corpus_bundles_project_updated
                    ON reference_corpus_bundles(project, updated_at);
                CREATE TABLE IF NOT EXISTS reference_corpus_document_versions (
                    version_id TEXT PRIMARY KEY,
                    corpus_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    project TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL,
                    metadata_hash TEXT NOT NULL,
                    version_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reference_corpus_versions_project_corpus
                    ON reference_corpus_document_versions(project, corpus_id, updated_at);
                CREATE TABLE IF NOT EXISTS reference_corpus_document_sources (
                    source_id TEXT PRIMARY KEY,
                    corpus_id TEXT NOT NULL,
                    project TEXT NOT NULL DEFAULT '',
                    storage_mode TEXT NOT NULL DEFAULT '',
                    source_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reference_corpus_sources_project_corpus
                    ON reference_corpus_document_sources(project, corpus_id, updated_at);
                CREATE TABLE IF NOT EXISTS reference_corpus_document_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    corpus_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    version_id TEXT NOT NULL DEFAULT '',
                    project TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL DEFAULT '',
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reference_corpus_snapshots_project_corpus
                    ON reference_corpus_document_snapshots(project, corpus_id, updated_at);
                CREATE TABLE IF NOT EXISTS reference_corpus_document_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    corpus_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    project TEXT NOT NULL DEFAULT '',
                    chunk_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reference_corpus_chunks_project_corpus
                    ON reference_corpus_document_chunks(project, corpus_id, updated_at);
                CREATE TABLE IF NOT EXISTS reference_corpus_freshness_checks (
                    check_id TEXT PRIMARY KEY,
                    corpus_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    project TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    result TEXT NOT NULL DEFAULT '',
                    check_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reference_corpus_freshness_project_corpus
                    ON reference_corpus_freshness_checks(project, corpus_id, updated_at);
                CREATE TABLE IF NOT EXISTS reference_corpus_extraction_runs (
                    run_id TEXT PRIMARY KEY,
                    corpus_id TEXT NOT NULL,
                    project TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    input_hash TEXT NOT NULL DEFAULT '',
                    run_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reference_corpus_runs_project_corpus
                    ON reference_corpus_extraction_runs(project, corpus_id, updated_at);
                """
                + _SCHEMA_MIGRATIONS_SCHEMA
                + """
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_ledger.v2', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_ledger.v3', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_scheduler_runs.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_memory_cards.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_context_packs.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_auto_recall_audit.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_backfill_sources.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_eval.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_ingress_queue.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_session_memory_state_machine.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_project_memory_state_machine.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_transcript_lookup_indexes.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_session_memory_sot.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_session_memory_terminology.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_tool_evidence_summary.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_native_memory_mirror.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_memory_gc_audit.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_qdrant_collections.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_graph_projection_state.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                """
                + _OBJECT_AUTHORITY_SCHEMA_MIGRATIONS
                + """
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_reference_corpus_bundles.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_reference_corpus_document_versions.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_reference_corpus_first_class_objects.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                UPDATE knowledge_items SET type = 'session_memory' WHERE type = 'session_memory_sot';
                """
                # Single-source graph projection_state schema: injected from
                # ledger_adapter so the table is declared in exactly one place.
                # The pre-M2 -> composite-unique migration already ran above so the
                # extraction_level column exists before this script's level index.
                + _GRAPH_PROJECTION_STATE_SCHEMA
            )
            _migrate_backend_neutral_index_schema(connection)
            _ensure_column(connection, "knowledge_items", "session_id_hash", "TEXT DEFAULT ''")
            _ensure_column(connection, "knowledge_items", "evidence_status", "TEXT DEFAULT 'historical'")
            _ensure_column(connection, "knowledge_items", "coverage_status", "TEXT DEFAULT ''")
            _ensure_column(connection, "knowledge_items", "coverage_gap_count", "INTEGER DEFAULT 0")
            _ensure_column(connection, "knowledge_items", "coverage_duplicate_count", "INTEGER DEFAULT 0")
            _ensure_column(connection, "knowledge_items", "source_manifest_hash", "TEXT DEFAULT ''")
            _ensure_column(connection, "knowledge_items", "source_chunk_count", "INTEGER DEFAULT 0")
            _ensure_column(connection, "knowledge_items", "indexed_at", "TEXT DEFAULT ''")
            _ensure_column(connection, "knowledge_items", "ingress_target_profile", "TEXT DEFAULT ''")
            _ensure_column(connection, "knowledge_items", "ingress_job_id", "TEXT DEFAULT ''")
            _ensure_column(connection, "knowledge_items", "queued_at", "TEXT DEFAULT ''")
            _ensure_column(connection, "knowledge_items", "metadata_json", "TEXT DEFAULT '{}'")
            _ensure_column(connection, "transcript_chunks", "part_index", "INTEGER NOT NULL DEFAULT 1")
            _ensure_column(connection, "transcript_chunks", "part_count", "INTEGER NOT NULL DEFAULT 1")
            _ensure_column(connection, "transcript_chunks", "char_start", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(connection, "transcript_chunks", "char_end", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(connection, "transcript_validation_files", "validation_document_ids_json", "TEXT DEFAULT '[]'")
            _ensure_column(connection, "transcript_validation_files", "validation_knowledge_ids_json", "TEXT DEFAULT '[]'")
            _ensure_column(connection, "transcript_validation_files", "created_at", "TEXT DEFAULT ''")
            _ensure_column(connection, "transcript_validation_files", "updated_at", "TEXT DEFAULT ''")
            _ensure_column(connection, "provider_source_contracts", "installed_version_evidence", "TEXT DEFAULT ''")
            _ensure_column(connection, "provider_source_contracts", "native_parser_status", "TEXT DEFAULT ''")
            _ensure_column(connection, "provider_source_contracts", "privacy_redaction_status", "TEXT DEFAULT ''")
            _ensure_column(connection, "provider_source_contracts", "rollback_state", "TEXT DEFAULT ''")
            _ensure_column(connection, "provider_source_contracts", "redacted_evidence_ref", "TEXT DEFAULT ''")
            _ensure_column(connection, "session_memory_coverage_edges", "turn_start_index", "INTEGER DEFAULT 0")
            _ensure_column(connection, "session_memory_coverage_edges", "turn_end_index", "INTEGER DEFAULT 0")
            # native_memory_mirror.v1 흡수(미배포): reconcile용 search_text + governance tier용 card_type.
            _ensure_column(connection, "native_memory_mirror", "search_text", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(connection, "native_memory_mirror", "card_type", "TEXT NOT NULL DEFAULT ''")
            if _table_exists(connection, "session_memory_sot_coverage_edges"):
                _ensure_column(connection, "session_memory_sot_coverage_edges", "turn_start_index", "INTEGER DEFAULT 0")
                _ensure_column(connection, "session_memory_sot_coverage_edges", "turn_end_index", "INTEGER DEFAULT 0")
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
                    )
                    SELECT
                        active_sot_knowledge_id,
                        source_content_hash,
                        source_window_hash,
                        derived_content_hash,
                        redaction_version,
                        created_at,
                        turn_start_index,
                        turn_end_index
                    FROM session_memory_sot_coverage_edges
                    ON CONFLICT DO NOTHING
                    """
                )
            if _table_exists(connection, "session_memory_sot_active_snapshots"):
                connection.execute(
                    """
                    INSERT INTO session_memory_active_snapshots (
                        session_id_hash,
                        active_knowledge_id,
                        active_content_hash,
                        previous_knowledge_id,
                        activated_at,
                        updated_at
                    )
                    SELECT
                        session_id_hash,
                        active_sot_knowledge_id,
                        active_sot_content_hash,
                        previous_sot_knowledge_id,
                        activated_at,
                        updated_at
                    FROM session_memory_sot_active_snapshots
                    ON CONFLICT DO NOTHING
                    """
                )
















    def get_memory_card(self, memory_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT mc.*, ki.index_target_id, ki.index_document_id, ki.status AS ledger_status
                FROM memory_cards mc
                LEFT JOIN knowledge_items ki ON ki.knowledge_id = mc.memory_id
                WHERE mc.memory_id = ?
                """,
                (memory_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_memory_card_state(self, memory_id: str) -> str | None:
        """memory_cards.state 단건 조회(없으면 None). JOIN 없는 경량 read.

        native memory supersede-sync 가 mirror active row 마다 ledger 상태를 확인할 때
        N+1 로 호출하므로 get_memory_card(knowledge_items JOIN + 전컬럼) 대신 단일 컬럼만.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state FROM memory_cards WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
        return row["state"] if row else None
















    def list_memory_cards_for_eval(self, *, project: str | None = None, provider: str | None = None) -> list[dict]:
        filters = []
        values = []
        if project:
            filters.append("mc.project = ?")
            values.append(project)
        if provider:
            filters.append("mc.provider = ?")
            values.append(provider)
        where = "WHERE " + " AND ".join(filters) if filters else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    mc.memory_id,
                    mc.candidate_id,
                    mc.card_type,
                    mc.project,
                    mc.provider,
                    mc.title,
                    mc.summary,
                    mc.content_hash,
                    mc.state,
                    mc.approved_by,
                    mc.approved_at,
                    mc.supersedes,
                    mc.disabled_at AS card_disabled_at,
                    mc.disabled_by,
                    mc.disable_reason,
                    ki.status AS ledger_status,
                    ki.disabled_at AS ledger_disabled_at,
                    ki.authorization_status,
                    ki.valid_until,
                    ki.supersedes AS ledger_supersedes
                FROM memory_cards mc
                LEFT JOIN knowledge_items ki ON ki.knowledge_id = mc.memory_id
                {where}
                ORDER BY mc.approved_at, mc.memory_id
                """,
                values,
            ).fetchall()
        return [dict(row) for row in rows]




    def record_context_pack(self, pack: dict, *, filters: dict | None = None, query_hash: str = "", private_allowed: bool = False) -> None:
        filters_json = json.dumps(filters or {}, sort_keys=True, separators=(",", ":"))
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO context_packs (pack_id, prompt_hash, filters_json, item_count, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (pack["pack_id"], pack["prompt_hash"], filters_json, len(pack.get("items", [])), created_at),
            )
            for index, item in enumerate(pack.get("items", [])):
                reference_id = item.get("memory_id") or item.get("knowledge_id") or ""
                connection.execute(
                    """
                    INSERT INTO context_pack_items (
                        pack_id, item_index, kind, reference_id, title, summary, score, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(pack_id, item_index) DO UPDATE SET
                        kind=excluded.kind,
                        reference_id=excluded.reference_id,
                        title=excluded.title,
                        summary=excluded.summary,
                        score=excluded.score,
                        metadata_json=excluded.metadata_json
                    """,
                    (
                        pack["pack_id"],
                        index,
                        item["kind"],
                        reference_id,
                        item.get("title", ""),
                        _persisted_context_summary(item),
                        item.get("score"),
                        json.dumps(item.get("metadata", {}), sort_keys=True, separators=(",", ":")),
                    ),
                )
            audit_id = "audit_" + uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO retrieval_audit (
                    audit_id, pack_id, prompt_hash, query_hash, filters_json,
                    result_count, private_allowed, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    pack["pack_id"],
                    pack["prompt_hash"],
                    query_hash,
                    filters_json,
                    len(pack.get("items", [])),
                    1 if private_allowed else 0,
                    created_at,
                ),
            )









    def mark_uploaded(self, knowledge_id: str, *, dataset_id: str, document_id: str, run: str) -> None:
        self._update_status(
            knowledge_id,
            "uploaded_unparsed",
            index_target_id=dataset_id,
            index_document_id=document_id,
            ingress_target_profile="",
            ingress_job_id="",
            queued_at="",
            index_run_id=run,
            indexed_at="",
        )

    def mark_enqueued(self, knowledge_id: str, *, target_profile: str, job_id: str, run: str = "QUEUED") -> None:
        self._update_status(
            knowledge_id,
            "queued",
            index_target_id="",
            index_document_id="",
            ingress_target_profile=target_profile,
            ingress_job_id=job_id,
            queued_at=datetime.now(timezone.utc).isoformat(),
            index_run_id=run,
            index_progress=0,
            indexed_at="",
        )

    def mark_metadata_applied(self, knowledge_id: str) -> None:
        self._update_status(knowledge_id, "metadata_applied")

    def mark_parse_requested(self, knowledge_id: str) -> None:
        self._update_status(knowledge_id, "parse_requested")

    def mark_indexing(self, knowledge_id: str, *, run: str, progress: float) -> None:
        self._update_status(knowledge_id, "indexing", index_run_id=run, index_progress=progress, indexed_at="")

    def mark_indexed(self, knowledge_id: str, *, run: str) -> None:
        self._update_status(
            knowledge_id,
            "indexed",
            index_run_id=run,
            index_progress=1.0,
            indexed_at=datetime.now(timezone.utc).isoformat(),
        )
        self._maybe_mark_session_memory_dirty_for_indexed_item(knowledge_id)
        self._maybe_mark_project_memory_dirty_for_indexed_item(knowledge_id)

    def mark_index_timeout(self, knowledge_id: str, *, run: str = "TIMEOUT", progress: float = 0) -> None:
        self._update_status(knowledge_id, "index_timeout", index_run_id=run, index_progress=progress, indexed_at="")




    def mark_parse_failed(self, knowledge_id: str, *, run: str = "FAIL") -> None:
        self._update_status(knowledge_id, "parse_failed", index_run_id=run, indexed_at="")

    def mark_quarantined(
        self,
        knowledge_id: str,
        *,
        reason: str,
        disposition_action: str,
        run_bucket: str = "",
    ) -> bool:
        item = self.get_by_knowledge_id(knowledge_id) or {}
        return self.mark_quarantined_if_queued(
            knowledge_id,
            reason=reason,
            disposition_action=disposition_action,
            run_bucket=run_bucket,
            expected_target_profile=str(item.get("ingress_target_profile") or ""),
            expected_ingress_job_id=str(item.get("ingress_job_id") or ""),
            expected_updated_at=str(item.get("updated_at") or ""),
        )








    def mark_disabled(self, knowledge_id: str) -> None:
        self._update_status(
            knowledge_id,
            "disabled",
            disabled_at=datetime.now(timezone.utc).isoformat(),
            authorization_status="disabled",
        )

    def mark_enabled(self, knowledge_id: str) -> None:
        # G-7 (M-GC §3.2 B1/B4): mark_disabled의 역함수. status->'indexed',
        # authorization_status->'active', disabled_at->''. mark_disabled가 쓰는 컬럼만
        # 정확히 되돌린다.
        self._update_status(
            knowledge_id,
            "indexed",
            disabled_at="",
            authorization_status="active",
        )

    def upsert_session_summary(
        self,
        *,
        knowledge_id: str,
        content_hash: str,
        provider: str,
        project: str,
        session_id_hash: str = "",
        title: str,
        summary: str,
        evidence_status: str = "historical",
        coverage_status: str = "",
        coverage_gap_count: int = 0,
        coverage_duplicate_count: int = 0,
        source_manifest_hash: str = "",
        source_chunk_count: int = 0,
    ) -> dict:
        item = self.upsert_prepared(
            knowledge_id=knowledge_id,
            content_hash=content_hash,
            provider=provider,
            project=project,
            domain="agent_memory",
            type="session_summary",
            session_id_hash=session_id_hash,
            title=title,
            summary=summary,
            privacy_level="private",
            evidence_status=evidence_status,
            coverage_status=coverage_status,
            coverage_gap_count=coverage_gap_count,
            coverage_duplicate_count=coverage_duplicate_count,
            source_manifest_hash=source_manifest_hash,
            source_chunk_count=source_chunk_count,
        )
        if item is None:
            item = self.get_by_content_hash(content_hash)
        if item is None:
            raise ValueError("failed to resolve canonical knowledge item for session summary")
        return item

    def upsert_session_memory_sot(
        self,
        *,
        knowledge_id: str,
        content_hash: str,
        provider: str,
        project: str,
        session_id_hash: str = "",
        title: str,
        summary: str,
        evidence_status: str = "historical",
        coverage_status: str = "",
        coverage_gap_count: int = 0,
        coverage_duplicate_count: int = 0,
        source_manifest_hash: str = "",
        source_chunk_count: int = 0,
    ) -> dict:
        """Deprecated compatibility wrapper for the retired session_memory_sot name."""
        return self.upsert_session_memory(
            knowledge_id=knowledge_id,
            content_hash=content_hash,
            provider=provider,
            project=project,
            session_id_hash=session_id_hash,
            title=title,
            summary=summary,
            evidence_status=evidence_status,
            coverage_status=coverage_status,
            coverage_gap_count=coverage_gap_count,
            coverage_duplicate_count=coverage_duplicate_count,
            source_manifest_hash=source_manifest_hash,
            source_chunk_count=source_chunk_count,
        )

    def upsert_session_memory(
        self,
        *,
        knowledge_id: str,
        content_hash: str,
        provider: str,
        project: str,
        session_id_hash: str = "",
        title: str,
        summary: str,
        evidence_status: str = "historical",
        coverage_status: str = "",
        coverage_gap_count: int = 0,
        coverage_duplicate_count: int = 0,
        source_manifest_hash: str = "",
        source_chunk_count: int = 0,
    ) -> dict:
        existing = self.get_by_knowledge_id(knowledge_id) or self.get_by_content_hash(content_hash)
        if existing is not None:
            if not source_manifest_hash:
                source_manifest_hash = str(existing.get("source_manifest_hash") or "")
            if int(source_chunk_count or 0) <= 0:
                source_chunk_count = int(existing.get("source_chunk_count") or 0)
        if not _is_sha256_hash(source_manifest_hash):
            raise ValueError("session memory source manifest hash is required")
        if int(source_chunk_count or 0) <= 0:
            raise ValueError("session memory source_manifest source_chunk_count is required")
        item = self.upsert_prepared(
            knowledge_id=knowledge_id,
            content_hash=content_hash,
            provider=provider,
            project=project,
            domain="agent_memory",
            type="session_memory",
            session_id_hash=session_id_hash,
            title=title,
            summary=summary,
            privacy_level="private",
            evidence_status=evidence_status,
            coverage_status=coverage_status,
            coverage_gap_count=coverage_gap_count,
            coverage_duplicate_count=coverage_duplicate_count,
            source_manifest_hash=source_manifest_hash,
            source_chunk_count=source_chunk_count,
        )
        if item is None:
            item = self.get_by_content_hash(content_hash)
        if item is None:
            raise ValueError("failed to resolve canonical knowledge item for session memory")
        return item

    def upsert_session_recap(
        self,
        *,
        knowledge_id: str,
        content_hash: str,
        provider: str,
        project: str,
        session_id_hash: str = "",
        title: str,
        summary: str,
        evidence_status: str = "historical",
        coverage_status: str = "",
        coverage_gap_count: int = 0,
        coverage_duplicate_count: int = 0,
    ) -> dict:
        item = self.upsert_prepared(
            knowledge_id=knowledge_id,
            content_hash=content_hash,
            provider=provider,
            project=project,
            domain="agent_memory",
            type="session_recap",
            session_id_hash=session_id_hash,
            title=title,
            summary=summary,
            privacy_level="private",
            evidence_status=evidence_status,
            coverage_status=coverage_status,
            coverage_gap_count=coverage_gap_count,
            coverage_duplicate_count=coverage_duplicate_count,
        )
        if item is None:
            item = self.get_by_content_hash(content_hash)
        if item is None:
            raise ValueError("failed to resolve canonical knowledge item for session recap")
        return item








    def list_session_memory_indexed_candidates(self, *, limit: int = 50) -> list[dict]:
        return []

    def promote_session_memory_snapshot(self, knowledge_id: str) -> dict:
        raise ValueError("legacy session_summary pipeline has been removed; use promote_session_memory")

    def promote_session_memory(self, knowledge_id: str) -> dict:
        item = self.get_by_knowledge_id(knowledge_id)
        if item is None:
            raise ValueError("session memory not found")
        if item.get("type") != "session_memory":
            raise ValueError("session memory must be type=session_memory")
        if item.get("authorization_status") != "active":
            raise ValueError("session memory authorization must be active before promotion")
        if item.get("disabled_at"):
            raise ValueError("disabled session memory cannot be promoted")
        if item.get("status") != "indexed":
            raise ValueError("session memory must be indexed before promotion")
        if item.get("supersedes"):
            raise ValueError("superseded session memory cannot be promoted")
        if _is_expired(item.get("valid_until", "")):
            raise ValueError("expired session memory cannot be promoted")
        if item.get("evidence_status") != SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS:
            raise ValueError("session memory requires regenerated transcript provenance before promotion")
        if not _session_memory_coverage_is_complete(item):
            raise ValueError("session memory coverage must be complete before promotion")
        if not self._session_memory_coverage_edges_are_complete(item):
            raise ValueError("session memory coverage edges must match source manifest before promotion")
        if not item.get("index_target_id"):
            raise ValueError("session memory requires index_target_id before promotion")
        if not item.get("index_document_id"):
            raise ValueError("session memory requires index_document_id before promotion")
        if not self._dataset_is_enabled(item.get("index_target_id", "")):
            raise ValueError("session memory dataset must be enabled before promotion")
        session_id_hash = item.get("session_id_hash") or ""
        if not session_id_hash:
            raise ValueError("session memory requires session_id_hash before promotion")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            previous = connection.execute(
                "SELECT active_knowledge_id FROM session_memory_active_snapshots WHERE session_id_hash = ?",
                (session_id_hash,),
            ).fetchone()
            previous_knowledge_id = str(previous["active_knowledge_id"]) if previous else ""
            if previous_knowledge_id and previous_knowledge_id != knowledge_id:
                connection.execute(
                    "UPDATE knowledge_items SET supersedes = ? WHERE knowledge_id = ?",
                    (knowledge_id, previous_knowledge_id),
                )
            connection.execute(
                """
                INSERT INTO session_memory_active_snapshots (
                    session_id_hash, active_knowledge_id, active_content_hash,
                    previous_knowledge_id, activated_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id_hash) DO UPDATE SET
                    active_knowledge_id=excluded.active_knowledge_id,
                    active_content_hash=excluded.active_content_hash,
                    previous_knowledge_id=excluded.previous_knowledge_id,
                    activated_at=excluded.activated_at,
                    updated_at=excluded.updated_at
                """,
                (
                    session_id_hash,
                    knowledge_id,
                    item.get("content_hash", ""),
                    previous_knowledge_id if previous_knowledge_id != knowledge_id else "",
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM session_memory_active_snapshots WHERE session_id_hash = ?",
                (session_id_hash,),
            ).fetchone()
        return dict(row)

    def promote_session_memory_sot(self, knowledge_id: str) -> dict:
        """Deprecated compatibility wrapper for the retired session_memory_sot name."""
        return self.promote_session_memory(knowledge_id)


    def get_session_memory_sot_active_snapshot(self, session_id_hash: str) -> dict | None:
        """Deprecated compatibility wrapper for the retired session_memory_sot name."""
        row = self.get_session_memory_active_snapshot(session_id_hash)
        if not row:
            return None
        return {
            "session_id_hash": row["session_id_hash"],
            "active_sot_knowledge_id": row["active_knowledge_id"],
            "active_sot_content_hash": row["active_content_hash"],
            "previous_sot_knowledge_id": row.get("previous_knowledge_id", ""),
            "activated_at": row["activated_at"],
            "updated_at": row["updated_at"],
        }


    def get_session_memory_sot_by_session_id_hash(self, session_id_hash: str) -> dict | None:
        """Deprecated compatibility getter; canonical type is session_memory."""
        return self.get_session_memory_by_session_id_hash(session_id_hash)

    def record_session_memory_sot_coverage(
        self,
        *,
        active_sot_knowledge_id: str,
        source_content_hash: str,
        source_window_hash: str,
        derived_content_hash: str,
        redaction_version: str,
        turn_start_index: int,
        turn_end_index: int,
    ) -> dict:
        """Deprecated compatibility wrapper for the retired session_memory_sot name."""
        row = self.record_session_memory_coverage(
            active_knowledge_id=active_sot_knowledge_id,
            source_content_hash=source_content_hash,
            source_window_hash=source_window_hash,
            derived_content_hash=derived_content_hash,
            redaction_version=redaction_version,
            turn_start_index=turn_start_index,
            turn_end_index=turn_end_index,
        )
        return {
            "active_sot_knowledge_id": row["active_knowledge_id"],
            "source_content_hash": row["source_content_hash"],
            "source_window_hash": row["source_window_hash"],
            "derived_content_hash": row["derived_content_hash"],
            "redaction_version": row["redaction_version"],
            "created_at": row["created_at"],
            "turn_start_index": row["turn_start_index"],
            "turn_end_index": row["turn_end_index"],
        }


    def list_session_memory_sot_coverage(self, active_sot_knowledge_id: str) -> list[dict]:
        """Deprecated compatibility wrapper for the retired session_memory_sot name."""
        return [
            {
                "active_sot_knowledge_id": row["active_knowledge_id"],
                "source_content_hash": row["source_content_hash"],
                "source_window_hash": row["source_window_hash"],
                "derived_content_hash": row["derived_content_hash"],
                "redaction_version": row["redaction_version"],
                "created_at": row["created_at"],
                "turn_start_index": row["turn_start_index"],
                "turn_end_index": row["turn_end_index"],
            }
            for row in self.list_session_memory_coverage(active_sot_knowledge_id)
        ]


    def _session_memory_coverage_edges_are_complete(self, item: dict) -> bool:
        try:
            expected_source_count = int(item.get("source_chunk_count") or 0)
        except (TypeError, ValueError):
            return False
        if expected_source_count <= 0:
            return False
        expected_manifest_hash = str(item.get("source_manifest_hash") or "")
        if not _is_sha256_hash(expected_manifest_hash):
            return False
        active_knowledge_id = str(item.get("knowledge_id") or "")
        content_hash = str(item.get("content_hash") or "")
        if not active_knowledge_id or not _is_sha256_hash(content_hash):
            return False
        edges = self.list_session_memory_coverage(active_knowledge_id)
        if len(edges) != expected_source_count:
            return False
        windows = []
        for edge in edges:
            if str(edge.get("derived_content_hash") or "") != content_hash:
                return False
            try:
                start = int(edge.get("turn_start_index") or 0)
                end = int(edge.get("turn_end_index") or 0)
            except (TypeError, ValueError):
                return False
            if start <= 0 or end < start:
                return False
            windows.append((start, end))
        previous_start = 0
        previous_end = 0
        for start, end in sorted(windows):
            if start == previous_start and end == previous_end:
                continue
            if start != previous_end + 1:
                return False
            previous_start = start
            previous_end = end
        pairs = [(str(edge["source_content_hash"]), str(edge["source_window_hash"])) for edge in edges]
        return _session_memory_coverage_edge_manifest_hash(pairs) == expected_manifest_hash

    def _session_memory_sot_coverage_edges_are_complete(self, item: dict) -> bool:
        """Deprecated compatibility wrapper for the retired session_memory_sot name."""
        return self._session_memory_coverage_edges_are_complete(item)








    def list_project_memory_indexed_candidates(self, *, limit: int = 50) -> list[dict]:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    ki.*
                FROM knowledge_items ki
                WHERE ki.type = 'project_context_snapshot'
                  AND ki.status = 'indexed'
                  AND ki.disabled_at = ''
                  AND ki.authorization_status = 'active'
                  AND ki.evidence_status = ?
                  AND ki.supersedes = ''
                  AND (ki.valid_until = '' OR ki.valid_until > ?)
                  AND ki.index_target_id != ''
                  AND ki.index_document_id != ''
                  AND NOT EXISTS (
                    SELECT 1 FROM index_targets rd
                    WHERE rd.dataset_id = ki.index_target_id
                      AND (rd.enabled = 0 OR rd.disabled_at != '')
                  )
                ORDER BY ki.indexed_at ASC, ki.updated_at ASC
                LIMIT ?
                """,
                (SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS, now, max(int(limit), 1)),
            ).fetchall()
        candidates = []
        for row in rows:
            item = dict(row)
            dirty = self.get_dirty_project_memory(provider=item.get("provider", ""), project=item.get("project", ""))
            if dirty and dirty.get("status") == "promoted":
                continue
            candidates.append(item)
        return candidates[: max(int(limit), 1)]

    def promote_project_memory_snapshot(self, knowledge_id: str) -> dict:
        item = self.get_by_knowledge_id(knowledge_id)
        if item is None:
            raise ValueError("project memory snapshot not found")
        if item.get("type") != "project_context_snapshot":
            raise ValueError("project memory snapshot must be type=project_context_snapshot")
        if item.get("status") != "indexed":
            raise ValueError("project memory snapshot must be indexed before promotion")
        if item.get("authorization_status") != "active":
            raise ValueError("project memory snapshot authorization must be active before promotion")
        if item.get("disabled_at"):
            raise ValueError("disabled project memory snapshot cannot be promoted")
        if item.get("supersedes"):
            raise ValueError("superseded project memory snapshot cannot be promoted")
        if _is_expired(item.get("valid_until", "")):
            raise ValueError("expired project memory snapshot cannot be promoted")
        if item.get("evidence_status") != SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS:
            raise ValueError("project memory snapshot requires regenerated transcript provenance before promotion")
        if not item.get("index_target_id"):
            raise ValueError("project memory snapshot requires index_target_id before promotion")
        if not item.get("index_document_id"):
            raise ValueError("project memory snapshot requires index_document_id before promotion")
        if not self._dataset_is_enabled(item.get("index_target_id", "")):
            raise ValueError("project memory snapshot dataset must be enabled before promotion")
        provider = item.get("provider") or ""
        project = item.get("project") or ""
        if not provider or not project:
            raise ValueError("project memory snapshot requires provider and project before promotion")
        project_key_hash = _project_key_hash(provider, project)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            previous = connection.execute(
                "SELECT active_knowledge_id FROM project_memory_active_snapshots WHERE project_key_hash = ?",
                (project_key_hash,),
            ).fetchone()
            previous_knowledge_id = str(previous["active_knowledge_id"]) if previous else ""
            if previous_knowledge_id and previous_knowledge_id != knowledge_id:
                connection.execute(
                    "UPDATE knowledge_items SET supersedes = ? WHERE knowledge_id = ?",
                    (knowledge_id, previous_knowledge_id),
                )
            connection.execute(
                """
                INSERT INTO project_memory_active_snapshots (
                    project_key_hash, provider, project, active_knowledge_id,
                    active_content_hash, previous_knowledge_id, activated_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_key_hash) DO UPDATE SET
                    provider=excluded.provider,
                    project=excluded.project,
                    active_knowledge_id=excluded.active_knowledge_id,
                    active_content_hash=excluded.active_content_hash,
                    previous_knowledge_id=excluded.previous_knowledge_id,
                    activated_at=excluded.activated_at,
                    updated_at=excluded.updated_at
                """,
                (
                    project_key_hash,
                    provider,
                    project,
                    knowledge_id,
                    item.get("content_hash", ""),
                    previous_knowledge_id if previous_knowledge_id != knowledge_id else "",
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM project_memory_active_snapshots WHERE project_key_hash = ?",
                (project_key_hash,),
            ).fetchone()
        return dict(row)












    def upsert_tool_evidence_summary(self, *, record) -> dict:
        """Append-only upsert of one redacted tool-evidence record.

        Keyed by ``evidence_id_hash`` (content-addressed), so re-running the
        extractor over the same source is idempotent. Never touches
        conversation_chunk rows or their knowledge_items.
        """
        data = record.to_record()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tool_evidence_summaries (
                    evidence_id_hash, session_id_hash, provider, project,
                    category, outcome, tool_name, command_summary, redacted_summary,
                    content_hash, observed_at, evidence_index, redaction_version,
                    source_status, knowledge_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(evidence_id_hash) DO UPDATE SET
                    session_id_hash=excluded.session_id_hash,
                    provider=excluded.provider,
                    project=excluded.project,
                    category=excluded.category,
                    outcome=excluded.outcome,
                    tool_name=excluded.tool_name,
                    command_summary=excluded.command_summary,
                    redacted_summary=excluded.redacted_summary,
                    content_hash=excluded.content_hash,
                    observed_at=excluded.observed_at,
                    evidence_index=excluded.evidence_index,
                    redaction_version=excluded.redaction_version,
                    source_status=excluded.source_status,
                    updated_at=excluded.updated_at
                """,
                (
                    data["evidence_id_hash"],
                    data["session_id_hash"],
                    data["provider"],
                    data["project"],
                    data["category"],
                    data["outcome"],
                    data["tool_name"],
                    data["command_summary"],
                    data["redacted_summary"],
                    data["content_hash"],
                    data.get("observed_at", ""),
                    data.get("evidence_index", 0),
                    data.get("redaction_version", "redaction.v2"),
                    data.get("source_status", "source_locator_private_spool_only"),
                    "",
                    "prepared",
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM tool_evidence_summaries WHERE evidence_id_hash = ?",
                (data["evidence_id_hash"],),
            ).fetchone()
        return dict(row) if row is not None else {}

    def upsert_object_review_proposal(self, proposal: dict) -> dict:
        if self.read_only:
            raise sqlite3.OperationalError("read-only ledger는 object proposal write를 허용하지 않습니다")
        with self._connect() as connection:
            return self._upsert_object_review_proposal_on(connection, proposal)

    def _upsert_object_review_proposal_on(self, connection, proposal: dict) -> dict:
        ensure_public_safe(proposal, "object_review_proposal")
        proposal_id = str(proposal.get("proposal_id") or "")
        proposal_type = str(proposal.get("proposal_type") or "")
        target_object_id = str(proposal.get("target_object_id") or "")
        if not proposal_id or not proposal_type or not target_object_id:
            raise ValueError("object review proposal requires proposal_id, proposal_type and target_object_id")
        project = str(proposal.get("project") or "")
        status = str(proposal.get("status") or "needs_review")
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        payload = json.dumps(proposal, ensure_ascii=False, sort_keys=True)
        connection.execute(
            """
            INSERT INTO object_review_proposals (
                proposal_id, project, proposal_type, target_object_id,
                status, proposal_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(proposal_id) DO UPDATE SET
                project=excluded.project,
                proposal_type=excluded.proposal_type,
                target_object_id=excluded.target_object_id,
                status=excluded.status,
                proposal_json=excluded.proposal_json,
                updated_at=excluded.updated_at
            """,
            (proposal_id, project, proposal_type, target_object_id, status, payload, now, now),
        )
        row = connection.execute(
            "SELECT proposal_json FROM object_review_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Failed to read back upserted object review proposal: {proposal_id}")
        return json.loads(row["proposal_json"])

    def list_object_review_proposals(self, *, project: str = "", limit: int = 20) -> list[dict]:
        bounded = max(1, min(int(limit or 20), 100))
        with self._connect() as connection:
            if project:
                rows = connection.execute(
                    """
                    SELECT proposal_json
                    FROM object_review_proposals
                    WHERE project = ? OR project = ''
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (project, bounded),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT proposal_json
                    FROM object_review_proposals
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (bounded,),
                ).fetchall()
        return [json.loads(row["proposal_json"]) for row in rows]

    def commit_object_authority_decision(self, decision: dict) -> dict:
        if self.read_only:
            raise sqlite3.OperationalError("read-only ledger는 object authority decision write를 허용하지 않습니다")
        with self._connect() as connection:
            return self._commit_object_authority_decision_on(connection, decision)

    def _commit_object_authority_decision_on(self, connection, decision: dict) -> dict:
        ensure_public_safe(decision, "object_authority_decision")
        decision_id = str(decision.get("decision_id") or "")
        proposal_id = str(decision.get("proposal_id") or "")
        target_object_id = str(decision.get("target_object_id") or "")
        decision_type = str(decision.get("decision_type") or "")
        previous_lane = str(decision.get("previous_authority_lane") or "")
        new_lane = str(decision.get("new_authority_lane") or "")
        if not all((decision_id, proposal_id, target_object_id, decision_type, previous_lane, new_lane)):
            raise ValueError(
                "object authority decision requires decision_id, proposal_id, target_object_id, "
                "decision_type, previous_authority_lane and new_authority_lane"
            )
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        project = public_safe_text(str(decision.get("project") or ""), max_chars=120)
        decision_payload = dict(decision)
        decision_payload["project"] = project
        decision_payload.setdefault("created_at", now)
        decision_payload["authority_write_performed"] = True
        decision_payload["authoritative_memory_changed"] = True
        decision_payload["cache_invalidated"] = True
        rollback_of_decision_id = public_safe_text(str(decision.get("rollback_of_decision_id") or ""), max_chars=180)
        supersedes_decision_id = public_safe_text(str(decision.get("supersedes_decision_id") or ""), max_chars=180)
        state = {
            "schema_version": "object_authority_state.v1",
            "project": project,
            "target_object_id": target_object_id,
            "authority_lane": new_lane,
            "decision_id": decision_id,
            "proposal_id": proposal_id,
            "decision_type": decision_type,
            "previous_authority_lane": previous_lane,
            "decision_reason": public_safe_text(str(decision.get("decision_reason") or ""), max_chars=512),
            "evidence_refs": list(decision.get("evidence_refs") or []),
            "approved_by_hash": str(decision.get("approved_by_hash") or ""),
            "rollback_of_decision_id": rollback_of_decision_id,
            "supersedes_decision_id": supersedes_decision_id,
            "updated_at": now,
        }
        ensure_public_safe(state, "object_authority_state")
        decision_json = json.dumps(decision_payload, ensure_ascii=False, sort_keys=True)
        state_json = json.dumps(state, ensure_ascii=False, sort_keys=True)
        proposal_status = _object_proposal_status_for_decision(decision_type, new_lane)
        row = connection.execute(
            "SELECT proposal_json FROM object_review_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise ValueError("object authority decision requires an existing review proposal")
        proposal = json.loads(row["proposal_json"])
        if str(proposal.get("target_object_id") or "") != target_object_id:
            raise ValueError("object authority decision target must match the review proposal target")
        connection.execute(
            """
            INSERT INTO object_authority_decisions (
                decision_id, project, proposal_id, target_object_id, decision_type,
                previous_authority_lane, new_authority_lane, decision_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(decision_id) DO UPDATE SET
                project=excluded.project,
                proposal_id=excluded.proposal_id,
                target_object_id=excluded.target_object_id,
                decision_type=excluded.decision_type,
                previous_authority_lane=excluded.previous_authority_lane,
                new_authority_lane=excluded.new_authority_lane,
                decision_json=excluded.decision_json
            """,
            (
                decision_id,
                project,
                proposal_id,
                target_object_id,
                decision_type,
                previous_lane,
                new_lane,
                decision_json,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO object_authority_states (
                target_object_id, project, authority_lane, decision_id, proposal_id,
                decision_reason, state_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(target_object_id) DO UPDATE SET
                project=excluded.project,
                authority_lane=excluded.authority_lane,
                decision_id=excluded.decision_id,
                proposal_id=excluded.proposal_id,
                decision_reason=excluded.decision_reason,
                state_json=excluded.state_json,
                updated_at=excluded.updated_at
            """,
            (
                target_object_id,
                project,
                new_lane,
                decision_id,
                proposal_id,
                state["decision_reason"],
                state_json,
                now,
            ),
        )
        proposal["status"] = proposal_status
        proposal["decision_id"] = decision_id
        proposal["authority_write_performed"] = False
        proposal["authoritative_memory_changed"] = False
        proposal_json = json.dumps(proposal, ensure_ascii=False, sort_keys=True)
        connection.execute(
            """
            UPDATE object_review_proposals
            SET status = ?, proposal_json = ?, updated_at = ?
            WHERE proposal_id = ?
            """,
            (proposal_status, proposal_json, now, proposal_id),
        )
        readback = connection.execute(
            "SELECT decision_json FROM object_authority_decisions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if readback is None:
            raise ValueError(f"Failed to read back object authority decision: {decision_id}")
        return json.loads(readback["decision_json"])

    def get_object_authority_state(self, target_object_id: str) -> dict:
        target = str(target_object_id or "")
        if not target:
            return {}
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM object_authority_states WHERE target_object_id = ?",
                (target,),
            ).fetchone()
        return json.loads(row["state_json"]) if row is not None else {}

    def get_object_authority_states(self, target_object_ids: list[str]) -> dict[str, dict]:
        targets = [str(target_id or "") for target_id in target_object_ids if target_id]
        if not targets:
            return {}
        placeholders = ", ".join("?" for _ in targets)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT target_object_id, state_json
                FROM object_authority_states
                WHERE target_object_id IN ({placeholders})
                """,
                targets,
            ).fetchall()
        return {str(row["target_object_id"]): json.loads(row["state_json"]) for row in rows}

    def list_object_authority_decisions(
        self,
        *,
        target_object_id: str = "",
        project: str = "",
        limit: int = 20,
    ) -> list[dict]:
        bounded = max(1, min(int(limit or 20), 100))
        target = str(target_object_id or "")
        project_name = public_safe_text(project, max_chars=120)
        with self._connect() as connection:
            if target:
                rows = connection.execute(
                    """
                    SELECT decision_json
                    FROM object_authority_decisions
                    WHERE target_object_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (target, bounded),
                ).fetchall()
            elif project_name:
                rows = connection.execute(
                    """
                    SELECT decision_json
                    FROM object_authority_decisions
                    WHERE project = ? OR project = ''
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (project_name, bounded),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT decision_json
                    FROM object_authority_decisions
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (bounded,),
                ).fetchall()
        return [json.loads(row["decision_json"]) for row in rows]

    def upsert_reference_corpus_bundle(self, bundle: dict, *, project: str = "") -> dict:
        if self.read_only:
            raise sqlite3.OperationalError("read-only ledger는 reference corpus write를 허용하지 않습니다")
        ensure_public_safe(bundle, "reference_corpus_bundle")
        corpus = dict(bundle.get("corpus") or {})
        corpus_id = str(corpus.get("corpus_id") or "")
        name = str(corpus.get("name") or "")
        storage_mode = str(corpus.get("storage_mode") or "")
        manifest_hash = str(corpus.get("manifest_ref") or "")
        if not corpus_id or not name or not storage_mode or not manifest_hash:
            raise ValueError("reference corpus bundle requires corpus_id, name, storage_mode and manifest hash")
        project_name = public_safe_text(project or str(bundle.get("project") or ""), max_chars=120)
        sources = [dict(source) for source in bundle.get("sources") or [] if isinstance(source, dict)]
        versions = [dict(version) for version in bundle.get("versions") or [] if isinstance(version, dict)]
        snapshots = [dict(snapshot) for snapshot in bundle.get("snapshots") or [] if isinstance(snapshot, dict)]
        chunks = [dict(chunk) for chunk in bundle.get("chunks") or [] if isinstance(chunk, dict)]
        freshness_checks = [dict(check) for check in bundle.get("freshness_checks") or [] if isinstance(check, dict)]
        extraction_run = dict(bundle.get("extraction_run") or {})
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        payload = json.dumps(bundle, ensure_ascii=False, sort_keys=True)
        source_ids = [str(source.get("source_id") or "") for source in sources]
        version_ids = [str(version.get("version_id") or "") for version in versions]
        snapshot_ids = [str(snapshot.get("snapshot_id") or "") for snapshot in snapshots]
        chunk_ids = [str(chunk.get("chunk_id") or "") for chunk in chunks]
        freshness_check_ids = [str(check.get("check_id") or "") for check in freshness_checks]
        extraction_run_ids = [str(extraction_run.get("run_id") or "")] if extraction_run else []
        with self._connect() as connection:
            def delete_missing_child_rows(table: str, id_column: str, current_ids: list[str]) -> None:
                ids = [current_id for current_id in current_ids if current_id]
                if not ids:
                    connection.execute(f"DELETE FROM {table} WHERE corpus_id = ?", (corpus_id,))
                    return
                placeholders = ", ".join("?" for _ in ids)
                connection.execute(
                    f"DELETE FROM {table} WHERE corpus_id = ? AND {id_column} NOT IN ({placeholders})",
                    (corpus_id, *ids),
                )

            delete_missing_child_rows("reference_corpus_extraction_runs", "run_id", extraction_run_ids)
            delete_missing_child_rows("reference_corpus_freshness_checks", "check_id", freshness_check_ids)
            delete_missing_child_rows("reference_corpus_document_chunks", "chunk_id", chunk_ids)
            delete_missing_child_rows("reference_corpus_document_snapshots", "snapshot_id", snapshot_ids)
            delete_missing_child_rows("reference_corpus_document_versions", "version_id", version_ids)
            delete_missing_child_rows("reference_corpus_document_sources", "source_id", source_ids)
            connection.execute(
                """
                INSERT INTO reference_corpus_bundles (
                    corpus_id, project, name, storage_mode, manifest_hash,
                    source_count, bundle_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(corpus_id) DO UPDATE SET
                    project=excluded.project,
                    name=excluded.name,
                    storage_mode=excluded.storage_mode,
                    manifest_hash=excluded.manifest_hash,
                    source_count=excluded.source_count,
                    bundle_json=excluded.bundle_json,
                    updated_at=excluded.updated_at
                """,
                (corpus_id, project_name, name, storage_mode, manifest_hash, len(sources), payload, now, now),
            )
            for source in sources:
                source_id = str(source.get("source_id") or "")
                source_mode = str(source.get("storage_mode") or "")
                if not source_id or not source_mode:
                    raise ValueError("document source requires source_id and storage_mode")
                source_payload = json.dumps(source, ensure_ascii=False, sort_keys=True)
                connection.execute(
                    """
                    INSERT INTO reference_corpus_document_sources (
                        source_id, corpus_id, project, storage_mode, source_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        corpus_id=excluded.corpus_id,
                        project=excluded.project,
                        storage_mode=excluded.storage_mode,
                        source_json=excluded.source_json,
                        updated_at=excluded.updated_at
                    """,
                    (source_id, corpus_id, project_name, source_mode, source_payload, now, now),
                )
            for version in versions:
                version_id = str(version.get("version_id") or "")
                source_id = str(version.get("source_id") or "")
                content_hash = str(version.get("content_hash") or "")
                metadata_hash = str(version.get("metadata_hash") or "")
                if not version_id or not source_id or not content_hash or not metadata_hash:
                    raise ValueError("document version requires version_id, source_id, content_hash and metadata_hash")
                version_payload = json.dumps(version, ensure_ascii=False, sort_keys=True)
                connection.execute(
                    """
                    INSERT INTO reference_corpus_document_versions (
                        version_id, corpus_id, source_id, project, content_hash,
                        metadata_hash, version_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(version_id) DO UPDATE SET
                        corpus_id=excluded.corpus_id,
                        source_id=excluded.source_id,
                        project=excluded.project,
                        content_hash=excluded.content_hash,
                        metadata_hash=excluded.metadata_hash,
                        version_json=excluded.version_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        version_id,
                        corpus_id,
                        source_id,
                        project_name,
                        content_hash,
                        metadata_hash,
                        version_payload,
                        now,
                        now,
                    ),
                )
            for snapshot in snapshots:
                snapshot_id = str(snapshot.get("snapshot_id") or "")
                source_id = str(snapshot.get("source_id") or "")
                version_id = str(snapshot.get("version_id") or "")
                content_hash = str(snapshot.get("content_hash") or "")
                if not snapshot_id or not source_id or not content_hash:
                    raise ValueError("document snapshot requires snapshot_id, source_id and content_hash")
                snapshot_payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
                connection.execute(
                    """
                    INSERT INTO reference_corpus_document_snapshots (
                        snapshot_id, corpus_id, source_id, version_id, project,
                        content_hash, snapshot_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(snapshot_id) DO UPDATE SET
                        corpus_id=excluded.corpus_id,
                        source_id=excluded.source_id,
                        version_id=excluded.version_id,
                        project=excluded.project,
                        content_hash=excluded.content_hash,
                        snapshot_json=excluded.snapshot_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        snapshot_id,
                        corpus_id,
                        source_id,
                        version_id,
                        project_name,
                        content_hash,
                        snapshot_payload,
                        now,
                        now,
                    ),
                )
            for chunk in chunks:
                chunk_id = str(chunk.get("chunk_id") or "")
                snapshot_id = str(chunk.get("snapshot_id") or "")
                if not chunk_id or not snapshot_id:
                    raise ValueError("document chunk requires chunk_id and snapshot_id")
                chunk_payload = json.dumps(chunk, ensure_ascii=False, sort_keys=True)
                connection.execute(
                    """
                    INSERT INTO reference_corpus_document_chunks (
                        chunk_id, corpus_id, snapshot_id, project, chunk_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chunk_id) DO UPDATE SET
                        corpus_id=excluded.corpus_id,
                        snapshot_id=excluded.snapshot_id,
                        project=excluded.project,
                        chunk_json=excluded.chunk_json,
                        updated_at=excluded.updated_at
                    """,
                    (chunk_id, corpus_id, snapshot_id, project_name, chunk_payload, now, now),
                )
            for check in freshness_checks:
                check_id = str(check.get("check_id") or "")
                source_id = str(check.get("source_id") or "")
                status = str(check.get("status") or "")
                result = str(check.get("result") or "")
                if not check_id or not source_id:
                    raise ValueError("freshness check requires check_id and source_id")
                check_payload = json.dumps(check, ensure_ascii=False, sort_keys=True)
                connection.execute(
                    """
                    INSERT INTO reference_corpus_freshness_checks (
                        check_id, corpus_id, source_id, project, status, result,
                        check_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(check_id) DO UPDATE SET
                        corpus_id=excluded.corpus_id,
                        source_id=excluded.source_id,
                        project=excluded.project,
                        status=excluded.status,
                        result=excluded.result,
                        check_json=excluded.check_json,
                        updated_at=excluded.updated_at
                    """,
                    (check_id, corpus_id, source_id, project_name, status, result, check_payload, now, now),
                )
            if extraction_run:
                run_id = str(extraction_run.get("run_id") or "")
                status = str(extraction_run.get("status") or "")
                input_hash = str(extraction_run.get("input_hash") or "")
                if not run_id or not status or not input_hash:
                    raise ValueError("extraction run requires run_id, status and input_hash")
                run_payload = json.dumps(extraction_run, ensure_ascii=False, sort_keys=True)
                connection.execute(
                    """
                    INSERT INTO reference_corpus_extraction_runs (
                        run_id, corpus_id, project, status, input_hash,
                        run_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        corpus_id=excluded.corpus_id,
                        project=excluded.project,
                        status=excluded.status,
                        input_hash=excluded.input_hash,
                        run_json=excluded.run_json,
                        updated_at=excluded.updated_at
                    """,
                    (run_id, corpus_id, project_name, status, input_hash, run_payload, now, now),
                )
            row = connection.execute(
                "SELECT bundle_json FROM reference_corpus_bundles WHERE corpus_id = ?",
                (corpus_id,),
            ).fetchone()
            count_row = connection.execute(
                "SELECT COUNT(*) AS c FROM reference_corpus_bundles WHERE corpus_id = ?",
                (corpus_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Failed to read back upserted reference corpus bundle: {corpus_id}")
        stored = json.loads(row["bundle_json"])
        ensure_public_safe(stored, "reference_corpus_bundle")
        return {
            "schema_version": "reference_corpus_store_write.v1",
            "status": "stored",
            "corpus_id": corpus_id,
            "project": project_name,
            "write_count": int(count_row["c"] if count_row is not None else 0),
            "source_count": len(sources),
            "document_source_count": len(sources),
            "version_count": len(versions),
            "snapshot_count": len(snapshots),
            "chunk_count": len(chunks),
            "freshness_check_count": len(freshness_checks),
            "extraction_run_count": 1 if extraction_run else 0,
            "mutation_performed": True,
            "production_mutation_performed": False,
        }

    def reference_corpus_status(self, *, project: str = "", corpus_id: str = "", limit: int = 20) -> dict:
        bounded = max(1, min(int(limit or 20), 100))
        project_name = public_safe_text(project, max_chars=120)
        corpus_key = public_safe_text(corpus_id, max_chars=180)
        with self._connect() as connection:
            def count_rows(table: str) -> int:
                if corpus_key:
                    row = connection.execute(
                        f"""
                        SELECT COUNT(*) AS c
                        FROM {table}
                        WHERE corpus_id = ? AND (? = '' OR project = ? OR project = '')
                        """,
                        (corpus_key, project_name, project_name),
                    ).fetchone()
                elif project_name:
                    row = connection.execute(
                        f"""
                        SELECT COUNT(*) AS c
                        FROM {table}
                        WHERE project = ? OR project = ''
                        """,
                        (project_name,),
                    ).fetchone()
                else:
                    row = connection.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
                return int(row["c"] if row is not None else 0)

            def sum_bundle_source_count() -> int:
                if corpus_key:
                    row = connection.execute(
                        """
                        SELECT COALESCE(SUM(source_count), 0) AS c
                        FROM reference_corpus_bundles
                        WHERE corpus_id = ? AND (? = '' OR project = ? OR project = '')
                        """,
                        (corpus_key, project_name, project_name),
                    ).fetchone()
                elif project_name:
                    row = connection.execute(
                        """
                        SELECT COALESCE(SUM(source_count), 0) AS c
                        FROM reference_corpus_bundles
                        WHERE project = ? OR project = ''
                        """,
                        (project_name,),
                    ).fetchone()
                else:
                    row = connection.execute(
                        "SELECT COALESCE(SUM(source_count), 0) AS c FROM reference_corpus_bundles"
                    ).fetchone()
                return int(row["c"] if row is not None else 0)

            def storage_mode_counts() -> dict[str, int]:
                if corpus_key:
                    rows_for_modes = connection.execute(
                        """
                        SELECT storage_mode, COUNT(*) AS c
                        FROM reference_corpus_document_sources
                        WHERE corpus_id = ? AND (? = '' OR project = ? OR project = '')
                        GROUP BY storage_mode
                        """,
                        (corpus_key, project_name, project_name),
                    ).fetchall()
                elif project_name:
                    rows_for_modes = connection.execute(
                        """
                        SELECT storage_mode, COUNT(*) AS c
                        FROM reference_corpus_document_sources
                        WHERE project = ? OR project = ''
                        GROUP BY storage_mode
                        """,
                        (project_name,),
                    ).fetchall()
                else:
                    rows_for_modes = connection.execute(
                        """
                        SELECT storage_mode, COUNT(*) AS c
                        FROM reference_corpus_document_sources
                        GROUP BY storage_mode
                        """
                    ).fetchall()
                return {
                    str(row["storage_mode"]): int(row["c"])
                    for row in rows_for_modes
                    if str(row["storage_mode"] or "")
                }

            total_bundles = count_rows("reference_corpus_bundles")
            total_document_sources = count_rows("reference_corpus_document_sources")
            total_document_versions = count_rows("reference_corpus_document_versions")
            total_document_snapshots = count_rows("reference_corpus_document_snapshots")
            total_document_chunks = count_rows("reference_corpus_document_chunks")
            total_freshness_checks = count_rows("reference_corpus_freshness_checks")
            total_extraction_runs = count_rows("reference_corpus_extraction_runs")
            total_bundle_sources = sum_bundle_source_count()
            if corpus_key:
                rows = connection.execute(
                    """
                    SELECT bundle_json
                    FROM reference_corpus_bundles
                    WHERE corpus_id = ? AND (? = '' OR project = ? OR project = '')
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (corpus_key, project_name, project_name, bounded),
                ).fetchall()
            elif project_name:
                rows = connection.execute(
                    """
                    SELECT bundle_json
                    FROM reference_corpus_bundles
                    WHERE project = ? OR project = ''
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (project_name, bounded),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT bundle_json
                    FROM reference_corpus_bundles
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (bounded,),
                ).fetchall()
            if corpus_key:
                version_rows = connection.execute(
                    """
                    SELECT version_json
                    FROM reference_corpus_document_versions
                    WHERE corpus_id = ? AND (? = '' OR project = ? OR project = '')
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (corpus_key, project_name, project_name, bounded),
                ).fetchall()
            elif project_name:
                version_rows = connection.execute(
                    """
                    SELECT version_json
                    FROM reference_corpus_document_versions
                    WHERE project = ? OR project = ''
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (project_name, bounded),
                ).fetchall()
            else:
                version_rows = connection.execute(
                    """
                    SELECT version_json
                    FROM reference_corpus_document_versions
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (bounded,),
                ).fetchall()
            if corpus_key:
                source_rows = connection.execute(
                    """
                    SELECT source_json
                    FROM reference_corpus_document_sources
                    WHERE corpus_id = ? AND (? = '' OR project = ? OR project = '')
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (corpus_key, project_name, project_name, bounded),
                ).fetchall()
                snapshot_rows = connection.execute(
                    """
                    SELECT snapshot_json
                    FROM reference_corpus_document_snapshots
                    WHERE corpus_id = ? AND (? = '' OR project = ? OR project = '')
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (corpus_key, project_name, project_name, bounded),
                ).fetchall()
                chunk_rows = connection.execute(
                    """
                    SELECT chunk_json
                    FROM reference_corpus_document_chunks
                    WHERE corpus_id = ? AND (? = '' OR project = ? OR project = '')
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (corpus_key, project_name, project_name, bounded),
                ).fetchall()
                freshness_rows = connection.execute(
                    """
                    SELECT check_json
                    FROM reference_corpus_freshness_checks
                    WHERE corpus_id = ? AND (? = '' OR project = ? OR project = '')
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (corpus_key, project_name, project_name, bounded),
                ).fetchall()
                run_rows = connection.execute(
                    """
                    SELECT run_json
                    FROM reference_corpus_extraction_runs
                    WHERE corpus_id = ? AND (? = '' OR project = ? OR project = '')
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (corpus_key, project_name, project_name, bounded),
                ).fetchall()
            elif project_name:
                source_rows = connection.execute(
                    """
                    SELECT source_json
                    FROM reference_corpus_document_sources
                    WHERE project = ? OR project = ''
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (project_name, bounded),
                ).fetchall()
                snapshot_rows = connection.execute(
                    """
                    SELECT snapshot_json
                    FROM reference_corpus_document_snapshots
                    WHERE project = ? OR project = ''
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (project_name, bounded),
                ).fetchall()
                chunk_rows = connection.execute(
                    """
                    SELECT chunk_json
                    FROM reference_corpus_document_chunks
                    WHERE project = ? OR project = ''
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (project_name, bounded),
                ).fetchall()
                freshness_rows = connection.execute(
                    """
                    SELECT check_json
                    FROM reference_corpus_freshness_checks
                    WHERE project = ? OR project = ''
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (project_name, bounded),
                ).fetchall()
                run_rows = connection.execute(
                    """
                    SELECT run_json
                    FROM reference_corpus_extraction_runs
                    WHERE project = ? OR project = ''
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (project_name, bounded),
                ).fetchall()
            else:
                source_rows = connection.execute(
                    """
                    SELECT source_json
                    FROM reference_corpus_document_sources
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (bounded,),
                ).fetchall()
                snapshot_rows = connection.execute(
                    """
                    SELECT snapshot_json
                    FROM reference_corpus_document_snapshots
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (bounded,),
                ).fetchall()
                chunk_rows = connection.execute(
                    """
                    SELECT chunk_json
                    FROM reference_corpus_document_chunks
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (bounded,),
                ).fetchall()
                freshness_rows = connection.execute(
                    """
                    SELECT check_json
                    FROM reference_corpus_freshness_checks
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (bounded,),
                ).fetchall()
                run_rows = connection.execute(
                    """
                    SELECT run_json
                    FROM reference_corpus_extraction_runs
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (bounded,),
                ).fetchall()
            storage_modes = storage_mode_counts()
        bundles = [json.loads(row["bundle_json"]) for row in rows]
        document_sources = [json.loads(row["source_json"]) for row in source_rows]
        document_versions = [json.loads(row["version_json"]) for row in version_rows]
        document_snapshots = [json.loads(row["snapshot_json"]) for row in snapshot_rows]
        document_chunks = [json.loads(row["chunk_json"]) for row in chunk_rows]
        freshness_checks = [json.loads(row["check_json"]) for row in freshness_rows]
        stored_extraction_runs = [json.loads(row["run_json"]) for row in run_rows]
        freshness_gaps: list[dict] = []
        extraction_runs: list[dict] = []
        source_count = total_document_sources or total_bundle_sources
        reference_object_count = 0
        snapshot_count = total_document_snapshots
        chunk_count = total_document_chunks
        manifest_hashes: list[str] = []
        for run in stored_extraction_runs:
            extraction_runs.append(
                {
                    "run_id": str(run.get("run_id") or ""),
                    "corpus_id": str(run.get("corpus_id") or ""),
                    "status": str(run.get("status") or ""),
                    "input_hash": str(run.get("input_hash") or ""),
                    "output_object_count": int(run.get("output_object_count") or 0),
                }
            )
        for bundle in bundles:
            sources = list(bundle.get("sources") or [])
            reference_object_count += len(bundle.get("objects") or [])
            if not total_document_snapshots:
                snapshot_count += len(bundle.get("snapshots") or [])
            if not total_document_chunks:
                chunk_count += len(bundle.get("chunks") or [])
            if not storage_modes:
                for source in sources:
                    mode = str(source.get("storage_mode") or "")
                    if mode:
                        storage_modes[mode] = storage_modes.get(mode, 0) + 1
            freshness_gaps.extend(dict(gap) for gap in bundle.get("freshness_gaps") or [] if isinstance(gap, dict))
            run = bundle.get("extraction_run")
            if isinstance(run, dict) and not stored_extraction_runs:
                extraction_runs.append(
                    {
                        "run_id": str(run.get("run_id") or ""),
                        "corpus_id": str(run.get("corpus_id") or ""),
                        "status": str(run.get("status") or ""),
                        "input_hash": str(run.get("input_hash") or ""),
                        "output_object_count": int(run.get("output_object_count") or 0),
                    }
                )
            manifest_hash = str((bundle.get("corpus") or {}).get("manifest_ref") or "")
            if manifest_hash:
                manifest_hashes.append(manifest_hash)
        status = {
            "schema_version": "brain_corpus_status.v1",
            "project": project_name,
            "corpus_id": corpus_key,
            "corpus_count": total_bundles,
            "source_count": source_count,
            "storage_modes": storage_modes,
            "reference_object_count": reference_object_count,
            "document_source_count": total_document_sources if total_document_sources else source_count,
            "document_sources": document_sources,
            "version_count": total_document_versions,
            "document_versions": document_versions,
            "snapshot_count": snapshot_count,
            "document_snapshots": document_snapshots,
            "chunk_count": chunk_count,
            "document_chunks": document_chunks,
            "freshness_check_count": total_freshness_checks,
            "freshness_checks": freshness_checks,
            "extraction_run_count": total_extraction_runs,
            "freshness_gaps": freshness_gaps,
            "extraction_runs": extraction_runs,
            "first_class_store_counts": {
                "document_sources": total_document_sources,
                "document_versions": total_document_versions,
                "document_snapshots": total_document_snapshots,
                "document_chunks": total_document_chunks,
                "freshness_checks": total_freshness_checks,
                "extraction_runs": total_extraction_runs,
            },
            "manifest_hashes": manifest_hashes,
            "limit": bounded,
            **_reference_corpus_default_policy_status(),
            "gaps": [] if bundles else ["reference_corpus_store_empty"],
        }
        ensure_public_safe(status, "reference_corpus_status")
        return status
