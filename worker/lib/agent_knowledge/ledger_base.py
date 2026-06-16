"""Ledger 공유 module-level(상수 + helper 함수). god-class 분할 시 ledger.py와 모든
mixin이 `from .ledger_base import *` 로 가져온다(__all__로 underscore 포함, 순환 차단)."""

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

SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS = "regenerated-from-indexed-transcript"
SQLITE_BUSY_TIMEOUT_MS = 60000

__all__ = [
    'SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS',
    'SQLITE_BUSY_TIMEOUT_MS',
    '_is_expired',
    '_is_sha256_hash',
    '_session_memory_coverage_edge_manifest_hash',
    '_sha256_text',
    '_session_memory_sot_coverage_edge_manifest_hash',
    '_session_memory_coverage_is_complete',
    '_session_memory_sot_coverage_is_complete',
    '_project_key_hash',
    '_table_exists',
    '_normalize_metadata_json',
    '_load_metadata_json',
    '_queued_document_projection',
    '_ensure_column',
    '_memory_candidate_from_row',
    '_transcript_validation_file_from_row',
    '_eval_query_from_row',
    '_persisted_context_summary',
]


def _is_expired(valid_until: str) -> bool:
    if not valid_until:
        return False
    try:
        expires_at = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)

def _is_sha256_hash(value: str | None) -> bool:
    if not value:
        return False
    value = str(value)
    if not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)

def _session_memory_coverage_edge_manifest_hash(pairs: list[tuple[str, str]]) -> str:
    material = "\n".join("|".join(pair) for pair in sorted(pairs))
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

def _session_memory_sot_coverage_edge_manifest_hash(pairs: list[tuple[str, str]]) -> str:
    """Deprecated compatibility wrapper for the retired session_memory_sot name."""
    return _session_memory_coverage_edge_manifest_hash(pairs)

def _session_memory_coverage_is_complete(item: dict) -> bool:
    coverage_status = str(item.get("coverage_status") or "")
    try:
        coverage_gap_count = int(item.get("coverage_gap_count") or 0)
        coverage_duplicate_count = int(item.get("coverage_duplicate_count") or 0)
    except (TypeError, ValueError):
        return False
    if coverage_gap_count or coverage_duplicate_count:
        return False
    return coverage_status in ("", "complete")

def _session_memory_sot_coverage_is_complete(item: dict) -> bool:
    """Deprecated compatibility wrapper for the retired session_memory_sot name."""
    return _session_memory_coverage_is_complete(item)

def _project_key_hash(provider: str, project: str) -> str:
    return "sha256:" + hashlib.sha256(f"{provider}|{project}".encode("utf-8")).hexdigest()

def _table_exists(connection, table: str) -> bool:
    if getattr(connection, "dialect", "sqlite") == "postgres":
        row = connection.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = ?",
            (table,),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    return row is not None

def _normalize_metadata_json(metadata: dict | None) -> str:
    if metadata is None:
        return "{}"
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be a JSON object")
    normalized = {}
    for key, value in metadata.items():
        if isinstance(value, (dict, list, tuple, set)):
            raise ValueError("metadata values must be scalar")
        normalized[str(key)] = "" if value is None else str(value)
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def _load_metadata_json(value: str) -> dict[str, str]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(item) for key, item in parsed.items()}

def _queued_document_projection(row: dict, transcript_chunk: dict | None = None) -> dict:
    metadata = _load_metadata_json(row.get("metadata_json", "{}"))
    if transcript_chunk:
        metadata.setdefault("chunk_id", str(transcript_chunk.get("chunk_id") or ""))
        metadata.setdefault("session_id_hash", str(transcript_chunk.get("session_id_hash") or ""))
        metadata.setdefault("provider", str(transcript_chunk.get("provider") or row.get("provider") or ""))
        metadata.setdefault("project", str(transcript_chunk.get("project") or row.get("project") or ""))
    metadata.setdefault("knowledge_id", str(row.get("knowledge_id") or ""))
    metadata.setdefault("provider", str(row.get("provider") or ""))
    metadata.setdefault("project", str(row.get("project") or ""))
    metadata.setdefault("session_id_hash", str(row.get("session_id_hash") or ""))
    return {
        "knowledge_id": str(row["knowledge_id"]),
        "document_type": str(row["type"]),
        "provider": str(row["provider"]),
        "project": str(row["project"]),
        "content_hash": str(row["content_hash"]),
        "target_profile": str(row["ingress_target_profile"]),
        "ingress_job_id": str(row["ingress_job_id"]),
        "updated_at": str(row.get("updated_at") or ""),
        "session_id_hash": str(row.get("session_id_hash") or metadata.get("session_id_hash") or ""),
        "metadata": metadata,
    }

def _ensure_column(connection, table: str, column: str, declaration: str) -> None:
    if getattr(connection, "dialect", "sqlite") == "postgres":
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = ?",
            (table,),
        ).fetchall()
        existing = {row["column_name"] for row in rows}
    else:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row["name"] for row in rows}
    if column in existing:
        return
    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

def _memory_candidate_from_row(row: sqlite3.Row) -> dict:
    record = dict(row)
    record["requires_manual_approval"] = bool(record["requires_manual_approval"])
    record["evidence_refs"] = json.loads(record.pop("evidence_refs_json") or "[]")
    return record

def _transcript_validation_file_from_row(row: sqlite3.Row) -> dict:
    record = dict(row)
    record["validation_document_ids"] = json.loads(record.pop("validation_document_ids_json") or "[]")
    record["validation_knowledge_ids"] = json.loads(record.pop("validation_knowledge_ids_json") or "[]")
    return record

def _eval_query_from_row(row: sqlite3.Row) -> dict:
    record = dict(row)
    record["query_terms"] = json.loads(record.pop("query_terms_json") or "[]")
    record["expected_memory_ids"] = json.loads(record.pop("expected_memory_ids_json") or "[]")
    record["enabled"] = bool(record["enabled"])
    record["k"] = int(record["k"])
    record["min_recall"] = float(record["min_recall"])
    record["min_precision"] = float(record["min_precision"])
    return record

def _persisted_context_summary(item: dict) -> str:
    if item.get("kind") == "conversation_chunk":
        return "[bounded-fallback-not-persisted]"
    if item.get("kind") == "memory_card":
        return "[approved-memory-summary-not-persisted]"
    return str(item.get("summary") or "")
