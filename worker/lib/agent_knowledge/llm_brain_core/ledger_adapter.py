from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from .artifact_store import _reject_external_index_fields
from .models import SessionMemoryArtifact, SourceRefRecord
from .source_ref import SourceRefResolver


class LedgerSessionMemoryArtifactStore:
    """SessionMemoryArtifact persistence backed by the existing neurons Ledger."""

    def __init__(self, ledger: Any) -> None:
        self._ledger = ledger
        self._ensure_schema()

    def upsert(self, artifact: SessionMemoryArtifact) -> str:
        _reject_external_index_fields(artifact.to_dict())
        payload = json.dumps(artifact.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        with self._ledger._connect() as connection:
            existing = connection.execute(
                """
                SELECT content_hash
                FROM llm_brain_session_memory_artifacts
                WHERE artifact_id = ?
                """,
                (artifact.artifact_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["content_hash"]) != artifact.content_hash:
                    raise ValueError("artifact id collision with different content_hash")
                return "duplicate"
            connection.execute(
                """
                INSERT INTO llm_brain_session_memory_artifacts (
                    artifact_id, session_id_hash, project, provider,
                    content_hash, artifact_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.session_id_hash,
                    artifact.project,
                    artifact.provider,
                    artifact.content_hash,
                    payload,
                    artifact.created_at,
                    artifact.created_at,
                ),
            )
        return "inserted"

    def get(self, artifact_id: str) -> SessionMemoryArtifact | None:
        with self._ledger._connect() as connection:
            row = connection.execute(
                """
                SELECT artifact_json
                FROM llm_brain_session_memory_artifacts
                WHERE artifact_id = ?
                """,
                (artifact_id,),
            ).fetchone()
        if not row:
            return None
        return _artifact_from_json(str(row["artifact_json"]))

    def list_recent(self, *, project: str, limit: int = 10) -> list[SessionMemoryArtifact]:
        bounded = max(1, min(int(limit), 100))
        with self._ledger._connect() as connection:
            rows = connection.execute(
                """
                SELECT artifact_json
                FROM llm_brain_session_memory_artifacts
                WHERE project = ?
                ORDER BY created_at DESC, artifact_id DESC
                LIMIT ?
                """,
                (project, bounded),
            ).fetchall()
        return [_artifact_from_json(str(row["artifact_json"])) for row in rows]

    def _ensure_schema(self) -> None:
        if getattr(self._ledger, "read_only", False):
            return
        with self._ledger._connect() as connection:
            connection.executescript(_ARTIFACT_SCHEMA)


class LedgerSourceRefCatalog:
    """SourceRef metadata catalog backed by the existing neurons Ledger."""

    def __init__(self, ledger: Any) -> None:
        self._ledger = ledger
        self._ensure_schema()

    def register(self, record: SourceRefRecord) -> None:
        payload = json.dumps(asdict(record), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        with self._ledger._connect() as connection:
            connection.execute(
                """
                INSERT INTO llm_brain_source_refs (
                    source_ref_id, device_id_hash, root_id, relative_path_hash,
                    content_hash, sync_policy, record_json, last_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_ref_id) DO UPDATE SET
                    device_id_hash=excluded.device_id_hash,
                    root_id=excluded.root_id,
                    relative_path_hash=excluded.relative_path_hash,
                    content_hash=excluded.content_hash,
                    sync_policy=excluded.sync_policy,
                    record_json=excluded.record_json,
                    last_seen_at=excluded.last_seen_at,
                    updated_at=excluded.updated_at
                """,
                (
                    record.source_ref_id,
                    record.device_id_hash,
                    record.root_id,
                    record.relative_path_hash,
                    record.content_hash,
                    record.sync_policy,
                    payload,
                    record.last_seen_at,
                    record.last_seen_at,
                ),
            )

    def get(self, source_ref_id: str) -> SourceRefRecord | None:
        with self._ledger._connect() as connection:
            row = connection.execute(
                """
                SELECT record_json
                FROM llm_brain_source_refs
                WHERE source_ref_id = ?
                """,
                (source_ref_id,),
            ).fetchone()
        if not row:
            return None
        return _source_ref_from_json(str(row["record_json"]))

    def list_all(self) -> list[SourceRefRecord]:
        with self._ledger._connect() as connection:
            rows = connection.execute(
                """
                SELECT record_json
                FROM llm_brain_source_refs
                ORDER BY last_seen_at DESC, source_ref_id
                """
            ).fetchall()
        return [_source_ref_from_json(str(row["record_json"])) for row in rows]

    def resolver(self) -> SourceRefResolver:
        return SourceRefResolver(self.list_all())

    def _ensure_schema(self) -> None:
        if getattr(self._ledger, "read_only", False):
            return
        with self._ledger._connect() as connection:
            connection.executescript(_SOURCE_REF_SCHEMA)


_ARTIFACT_SCHEMA = """
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
"""


_SOURCE_REF_SCHEMA = """
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
"""


def _artifact_from_json(value: str) -> SessionMemoryArtifact:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("artifact_json must decode to an object")
    return SessionMemoryArtifact(
        artifact_id=str(parsed["artifact_id"]),
        session_id_hash=str(parsed["session_id_hash"]),
        project=str(parsed["project"]),
        provider=str(parsed["provider"]),
        source_event_ids=tuple(parsed.get("source_event_ids") or ()),
        chunk_refs=tuple(parsed.get("chunk_refs") or ()),
        tool_evidence_refs=tuple(parsed.get("tool_evidence_refs") or ()),
        summary=str(parsed["summary"]),
        content_hash=str(parsed["content_hash"]),
        ontology_version=str(parsed.get("ontology_version") or "1.0.0"),
        extractor_version=str(parsed.get("extractor_version") or "0.1.0"),
        created_at=str(parsed.get("created_at") or ""),
    )


def _source_ref_from_json(value: str) -> SourceRefRecord:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("source ref record must decode to an object")
    return SourceRefRecord(
        source_ref_id=str(parsed["source_ref_id"]),
        device_id_hash=str(parsed["device_id_hash"]),
        root_id=str(parsed["root_id"]),
        relative_path_hash=str(parsed["relative_path_hash"]),
        content_hash=str(parsed["content_hash"]),
        mtime=str(parsed["mtime"]),
        size=int(parsed["size"]),
        sync_policy=parsed["sync_policy"],
        permission_scope=str(parsed.get("permission_scope") or "project"),
        last_seen_at=str(parsed.get("last_seen_at") or ""),
        deleted_at=str(parsed.get("deleted_at") or ""),
        revoked_at=str(parsed.get("revoked_at") or ""),
        derived_summary=str(parsed.get("derived_summary") or ""),
        redacted_content=str(parsed.get("redacted_content") or ""),
    )
