from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from agent_knowledge.ledger_base import _ensure_column, _table_exists

from .artifact_store import _reject_external_index_fields
from .graphiti_adapter import _graphiti_group_id, _group_id_for_episode
from .models import OntologyEpisode, SessionMemoryArtifact, SourceRefRecord
from .source_ref import SourceRefResolver

# Default extraction pass for a projected episode. The episodic-only pass
# (raw EpisodicNode, no entity extraction) is the production default; the entity
# pass runs add_episode so Graphiti extracts EntityNode/RELATES_TO. Recording the
# level lets a re-run resume per-pass instead of skipping the entity pass just
# because the episodic pass already ran (composite (episode_id, extraction_level)
# idempotency).
EXTRACTION_LEVEL_EPISODIC = "episodic"
EXTRACTION_LEVEL_ENTITY = "entity"


class LedgerSessionMemoryArtifactStore:
    """SessionMemoryArtifact persistence backed by the existing neurons Ledger."""

    def __init__(self, ledger: Any) -> None:
        self._ledger = ledger
        self._ensure_schema()

    def upsert(self, artifact: SessionMemoryArtifact) -> str:
        _reject_external_index_fields(artifact.to_dict())
        payload = json.dumps(artifact.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        with self._ledger._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO llm_brain_session_memory_artifacts (
                    artifact_id, session_id_hash, project, provider,
                    content_hash, artifact_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id) DO NOTHING
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
            if cursor.rowcount == 1:
                return "inserted"
            existing = connection.execute(
                """
                SELECT content_hash
                FROM llm_brain_session_memory_artifacts
                WHERE artifact_id = ?
                """,
                (artifact.artifact_id,),
            ).fetchone()
            if existing is not None and str(existing["content_hash"]) == artifact.content_hash:
                return "duplicate"
            raise ValueError("artifact id collision with different content_hash")

    def get(self, artifact_id: str) -> SessionMemoryArtifact | None:
        with self._ledger._connect() as connection:
            # Dialect-aware pre-check: an absent table is an empty result, not an
            # error. _table_exists branches on sqlite vs postgres, so we never
            # rely on a caught sqlite3.OperationalError (which would let a
            # postgres UndefinedTable propagate instead of degrading cleanly).
            if not _table_exists(connection, "llm_brain_session_memory_artifacts"):
                return None
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
            if not _table_exists(connection, "llm_brain_session_memory_artifacts"):
                return []
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
        with self._ledger._connect() as connection:
            self._register_on_connection(connection, record)

    def register_all(self, records: list[SourceRefRecord]) -> None:
        """Register many records in a single transaction (all-or-nothing).

        A failure on any record rolls back the whole batch (sqlite3's connection
        context manager commits on clean exit, rolls back on exception), so a
        mid-batch write error never leaves the catalog partially loaded.
        """

        if not records:
            return
        with self._ledger._connect() as connection:
            for record in records:
                self._register_on_connection(connection, record)

    def _register_on_connection(self, connection: Any, record: SourceRefRecord) -> None:
        payload = json.dumps(asdict(record), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
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
            if not _table_exists(connection, "llm_brain_source_refs"):
                return None
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
            if not _table_exists(connection, "llm_brain_source_refs"):
                return []
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


class LedgerGraphProjectionStateStore:
    """Durable SoT for which OntologyEpisodes have been projected to the graph.

    Same shape as LedgerSourceRefCatalog / LedgerSessionMemoryArtifactStore: it
    ensures its schema on construction (skipped for a read-only ledger) and uses
    the ledger's own connection. It records only successful projections (inserted
    / duplicate); skips and failures live on a different plane and are not stored
    here. A re-run reads list_projected_ids to resume without an upsert round-trip.
    """

    def __init__(self, ledger: Any) -> None:
        self._ledger = ledger
        self._ensure_schema()

    def mark_projected(
        self,
        episode: OntologyEpisode,
        upsert_result: str,
        extraction_level: str = EXTRACTION_LEVEL_EPISODIC,
    ) -> None:
        # group_id is derived with the graphiti helpers (not reimplemented) so the
        # stored group key matches exactly what the graph adapter writes.
        group_id = _graphiti_group_id(_group_id_for_episode(episode, ""))
        project = str(episode.payload.get("project") or "")
        brain_id = str(episode.payload.get("brain_id") or "")
        level = str(extraction_level or EXTRACTION_LEVEL_EPISODIC)
        with self._ledger._connect() as connection:
            # Conflict target is the composite (episode_id, extraction_level): the
            # episodic and entity passes of the SAME episode are tracked as two
            # rows, so a re-run can resume per-pass instead of skipping the entity
            # pass just because the episodic pass already ran.
            connection.execute(
                """
                INSERT INTO llm_brain_graph_projection_state (
                    episode_id, extraction_level, project, entity_type,
                    natural_id, group_id, brain_id, content_hash,
                    ontology_version, extractor_version, upsert_result,
                    projected_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(episode_id, extraction_level) DO UPDATE SET
                    upsert_result=excluded.upsert_result,
                    projected_at=excluded.projected_at,
                    updated_at=excluded.updated_at
                """,
                (
                    episode.episode_id,
                    level,
                    project,
                    episode.entity_type,
                    episode.natural_id,
                    group_id,
                    brain_id,
                    episode.content_hash,
                    episode.ontology_version,
                    episode.extractor_version,
                    str(upsert_result or ""),
                ),
            )

    def list_projected_ids(
        self,
        project: str | None = None,
        *,
        extraction_level: str | None = None,
    ) -> set[str]:
        # `extraction_level=None` returns episode_ids projected at ANY level
        # (backward-compatible: the episodic-only resume set). A specific level
        # narrows the resume set to that pass, so the entity pass resumes only on
        # ids already projected at the entity level.
        clauses: list[str] = []
        params: list[str] = []
        if project is not None:
            clauses.append("project = ?")
            params.append(project)
        if extraction_level is not None:
            clauses.append("extraction_level = ?")
            params.append(str(extraction_level))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._ledger._connect() as connection:
            # Dialect-aware pre-check: an absent table degrades to an empty set
            # without letting a postgres UndefinedTable propagate.
            if not _table_exists(connection, "llm_brain_graph_projection_state"):
                return set()
            rows = connection.execute(
                "SELECT episode_id FROM llm_brain_graph_projection_state" + where,
                tuple(params),
            ).fetchall()
        return {str(row["episode_id"]) for row in rows}

    def list_projected_natural_ids(
        self,
        project: str | None = None,
        *,
        extraction_level: str | None = None,
        entity_type: str | None = None,
    ) -> set[str]:
        clauses: list[str] = []
        params: list[str] = []
        if project is not None:
            clauses.append("project = ?")
            params.append(project)
        if extraction_level is not None:
            clauses.append("extraction_level = ?")
            params.append(str(extraction_level))
        if entity_type is not None:
            clauses.append("entity_type = ?")
            params.append(str(entity_type))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._ledger._connect() as connection:
            if not _table_exists(connection, "llm_brain_graph_projection_state"):
                return set()
            rows = connection.execute(
                "SELECT natural_id FROM llm_brain_graph_projection_state" + where,
                tuple(params),
            ).fetchall()
        return {str(row["natural_id"]) for row in rows if str(row["natural_id"])}

    def _ensure_schema(self) -> None:
        if getattr(self._ledger, "read_only", False):
            return
        with self._ledger._connect() as connection:
            # Migrate FIRST: a pre-M2 table lacks the extraction_level column, so
            # the schema's level index must not run before the migration adds it.
            # On a brand-new ledger the migration is a no-op (table absent) and the
            # schema below creates the table + indexes.
            _migrate_extraction_level(connection)
            connection.executescript(_GRAPH_PROJECTION_STATE_SCHEMA)


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


# Single source of truth for the graph projection_state table. Ledger._initialize
# imports and installs this exact constant so the schema is declared once and the
# store + ledger can never drift. Standard SQL only (TEXT / UNIQUE /
# CREATE INDEX IF NOT EXISTS) so it works on both sqlite and postgres.
#
# Idempotency key is the COMPOSITE (episode_id, extraction_level), enforced by a
# UNIQUE constraint rather than a sole episode_id PRIMARY KEY, so the episodic and
# entity passes of the same episode coexist as two rows. extraction_level carries
# a NOT NULL DEFAULT so a legacy episodic-only insert (no level) backfills to
# 'episodic'.
_GRAPH_PROJECTION_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_brain_graph_projection_state (
    episode_id TEXT NOT NULL,
    extraction_level TEXT NOT NULL DEFAULT 'episodic',
    project TEXT NOT NULL DEFAULT '',
    entity_type TEXT NOT NULL DEFAULT '',
    natural_id TEXT NOT NULL DEFAULT '',
    group_id TEXT NOT NULL DEFAULT '',
    brain_id TEXT DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    ontology_version TEXT NOT NULL DEFAULT '',
    extractor_version TEXT NOT NULL DEFAULT '',
    upsert_result TEXT NOT NULL DEFAULT '',
    projected_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(episode_id, extraction_level)
);
CREATE INDEX IF NOT EXISTS idx_llm_brain_graph_projection_state_project_projected
    ON llm_brain_graph_projection_state(project, projected_at);
CREATE INDEX IF NOT EXISTS idx_llm_brain_graph_projection_state_group
    ON llm_brain_graph_projection_state(group_id);
CREATE INDEX IF NOT EXISTS idx_llm_brain_graph_projection_state_level
    ON llm_brain_graph_projection_state(extraction_level);
"""


def _migrate_extraction_level(connection: Any) -> None:
    """Lazily bring a pre-M2 projection_state table up to the composite schema.

    A pre-M2 table has ``episode_id`` as a sole PRIMARY KEY and no
    ``extraction_level`` column, so it cannot hold both passes of one episode.
    This migration is non-destructive: it adds the column (defaulting existing
    rows to 'episodic') and, only when the legacy sole-PK shape is detected,
    rebuilds the table into the composite-unique shape, copying every row. The
    table is derived resume state (re-derivable from the graph), so the rebuild
    carries no authoritative data loss risk.

    On a freshly-created table (already the new shape) this is a no-op: the column
    exists and there is no sole episode_id PRIMARY KEY to migrate.
    """

    table = "llm_brain_graph_projection_state"
    if not _table_exists(connection, table):
        return
    # Step 1 (always safe / idempotent): ensure the column exists and backfill.
    _ensure_column(connection, table, "extraction_level", "TEXT NOT NULL DEFAULT 'episodic'")
    connection.execute(
        f"UPDATE {table} SET extraction_level = 'episodic' "
        "WHERE extraction_level IS NULL OR extraction_level = ''"
    )
    # Step 2 (only for the legacy sole-PK shape): rebuild so the composite
    # (episode_id, extraction_level) uniqueness replaces episode_id-as-sole-PK.
    if not _episode_id_is_sole_primary_key(connection, table):
        return
    connection.executescript(
        f"""
        ALTER TABLE {table} RENAME TO {table}_pre_m2;
        {_GRAPH_PROJECTION_STATE_SCHEMA}
        INSERT INTO {table} (
            episode_id, extraction_level, project, entity_type, natural_id,
            group_id, brain_id, content_hash, ontology_version,
            extractor_version, upsert_result, projected_at, updated_at
        )
        SELECT
            episode_id,
            COALESCE(NULLIF(extraction_level, ''), 'episodic'),
            project, entity_type, natural_id, group_id, brain_id, content_hash,
            ontology_version, extractor_version, upsert_result, projected_at,
            updated_at
        FROM {table}_pre_m2;
        DROP TABLE {table}_pre_m2;
        """
    )


def _episode_id_is_sole_primary_key(connection: Any, table: str) -> bool:
    """Return True when ``episode_id`` is the table's only PRIMARY KEY column.

    Detects the pre-M2 schema shape so the composite-unique rebuild runs at most
    once. Dialect-aware (sqlite PRAGMA vs postgres information_schema); a backend
    whose PK introspection is unavailable degrades to False (skip the rebuild)
    rather than risk an unnecessary destructive table swap.
    """

    if getattr(connection, "dialect", "sqlite") == "postgres":
        rows = connection.execute(
            """
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
                AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
                AND tc.table_schema = 'public'
                AND tc.table_name = ?
            """,
            (table,),
        ).fetchall()
        pk_columns = [str(row["column_name"]) for row in rows]
    else:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        pk_columns = [str(row["name"]) for row in rows if row["pk"]]
    return pk_columns == ["episode_id"]


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
