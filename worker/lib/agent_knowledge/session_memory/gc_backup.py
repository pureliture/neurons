"""Recoverable-delete backup store for GC hard deletes.

This worker-owned slice intentionally contains only the local backup record
store. Restore/upload/parse CLI behavior remains outside neurons until the GC
safety lane is explicitly approved.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

GC_BACKUP_SCHEMA_VERSION = "agent_knowledge_gc_backup.v1"
GC_BACKUP_KINDS = ("session_memory", "transcript_memory")


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in str(value or ""))
    return (cleaned or "unknown")[:120]


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def write_gc_backup(
    backup_dir: Path | str,
    *,
    kind: str,
    knowledge_id: str,
    content_hash: str,
    session_id_hash: str,
    provider: str,
    project: str,
    dataset_id: str,
    ragflow_document_id: str,
    body: str,
    replacement_knowledge_id: str = "",
    coverage: list | None = None,
    extra: dict | None = None,
) -> Path:
    """Write one recoverable-delete backup record before hard delete."""
    if kind not in GC_BACKUP_KINDS:
        raise ValueError(f"unsupported gc backup kind: {kind!r}")
    root = Path(backup_dir)
    target_dir = root / kind
    target_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    os.chmod(target_dir, 0o700)
    record = {
        "schema_version": GC_BACKUP_SCHEMA_VERSION,
        "kind": kind,
        "knowledge_id": knowledge_id,
        "content_hash": content_hash,
        "session_id_hash": session_id_hash,
        "provider": provider,
        "project": project,
        "dataset_id": dataset_id,
        "ragflow_document_id_hash": _sha256_hex(ragflow_document_id),
        "body": body,
        "replacement_knowledge_id": replacement_knowledge_id,
        "coverage": coverage or [],
        "extra": extra or {},
        "backed_up_at": datetime.now(timezone.utc).isoformat(),
    }
    path = target_dir / (_safe_name(knowledge_id or content_hash) + ".json")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path


def read_gc_backup(path: Path | str) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != GC_BACKUP_SCHEMA_VERSION:
        raise ValueError("not a valid gc backup record")
    return payload


def list_gc_backups(backup_dir: Path | str, *, kind: str | None = None) -> list[Path]:
    root = Path(backup_dir)
    kinds = (kind,) if kind else GC_BACKUP_KINDS
    out: list[Path] = []
    for backup_kind in kinds:
        directory = root / backup_kind
        if directory.is_dir():
            out.extend(sorted(path for path in directory.glob("*.json")))
    return out
