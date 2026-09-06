"""PostgreSQL/pgvector storage adapter for LBrain Memory Authority.

The store deliberately has no process-local persistence mode. Every read and
write is executed against PostgreSQL through ``psycopg`` (or an explicitly
injected psycopg connection owned by the caller). This distinction matters:
the adapter is part of the authority boundary, so losing the database must be
visible to its caller rather than silently turning into data loss.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterator, Mapping

from ..model_connectors import DEFAULT_EMBEDDING_DIM, DEFAULT_EMBEDDING_MODEL

# Gemini Embedding 2의 full output을 보존한다. 일반 ``vector`` 타입은 2,000
# 차원 제한이 있으므로 DDL과 SQL cast는 ``halfvec(3072)``를 사용한다.
VECTOR_DIMENSION = DEFAULT_EMBEDDING_DIM
OUTBOX_RETRY_LIMIT = 5


def compute_cosine_similarity(vec1: list[float] | None, vec2: list[float] | None) -> float:
    """Compute cosine similarity between two vectors bounded in [0.0, 1.0]."""

    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0
    dot = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = math.sqrt(sum(a * a for a in vec1))
    norm2 = math.sqrt(sum(b * b for b in vec2))
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0
    val = dot / (norm1 * norm2)
    return max(0.0, min(1.0, float(val)))


def make_dummy_vector(seed: int, dim: int = VECTOR_DIMENSION) -> list[float]:
    """Generate a deterministic unit vector for isolated test data builders.

    This helper does not belong to the storage path and is intentionally kept
    separate from ``PgVectorStore``. Production embeddings are supplied by an
    embedding provider and persisted by the outbox worker.
    """

    vec = [(math.sin(seed * 1000 + i) + 1.0) / 2.0 for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return [0.0] * dim
    return [x / norm for x in vec]


@dataclass
class MemoryCard:
    memory_id: str
    project: str
    card_type: str
    title: str
    summary: str
    typed_payload: dict[str, Any] = field(default_factory=dict)
    lifecycle_state: str = "candidate"
    authorization_status: str = "disabled"
    currentness: str = "current"
    confidence: float = 1.0
    valid_from: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    valid_to: datetime | None = None
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_revision: int = 1
    embedding_state: str = "pending"
    embedding: list[float] | None = None
    content_hash: str = ""
    source_ref: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class MemoryEdge:
    edge_id: int = 0
    src_id: str = ""
    rel_type: str = "derived_from"
    dst_id: str = ""
    provenance_hash: str = ""
    confidence: float = 1.0
    properties: dict[str, Any] = field(default_factory=dict)
    valid_from: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    valid_to: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class SessionChunk:
    chunk_id: str
    session_id_hash: str
    project: str
    provider: str = "unspecified"
    chunk_index: int = 0
    content_markdown: str = ""
    token_count: int = 0
    content_hash: str = ""
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_state: str = "pending"
    embedding_revision: int = 1
    embedding: list[float] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class OutboxJob:
    outbox_id: int
    target_type: str
    target_id: str
    content_hash: str
    payload_text: str
    status: str = "queued"
    claimed_at: datetime | None = None
    lease_until: datetime | None = None
    worker_id: str | None = None
    retry_count: int = 0
    last_error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class GraphOutboxJob:
    projection_id: int
    source_type: str
    source_id: str
    source_revision: str
    content_hash: str
    episode_payload: dict[str, Any]
    status: str = "queued"
    claimed_at: datetime | None = None
    lease_until: datetime | None = None
    worker_id: str | None = None
    retry_count: int = 0
    last_error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PgVectorStore:
    """Real PostgreSQL store for cards, chunks, edges, and embedding jobs.

    ``dsn`` creates and owns one short-lived connection per operation.
    ``connection`` is an explicitly injected connection whose transaction is
    owned by the caller. The latter is used to compose a larger transaction;
    it is not an in-memory test mode.
    """

    def __init__(
        self,
        dsn: str | None = None,
        connection: Any | None = None,
        pgvector_version: str = "0.8.0",
        use_in_memory: bool = False,
    ) -> None:
        if use_in_memory:
            raise ValueError(
                "PgVectorStore does not support in-memory persistence; provide a PostgreSQL DSN"
            )
        if not dsn and connection is None:
            raise ValueError("PgVectorStore requires a PostgreSQL DSN or psycopg connection")
        if dsn and connection is not None:
            raise ValueError("provide either dsn or connection, not both")
        self.dsn = dsn
        self.connection = connection
        self.pgvector_version = str(pgvector_version)

    @classmethod
    def schema_sql(cls) -> str:
        """Return the checked-in schema without pretending it was executed."""

        schema_path = Path(__file__).with_name("pgvector_schema.sql")
        return schema_path.read_text(encoding="utf-8")

    def _open_connection(self) -> Any:
        if self.connection is not None:
            return self.connection
        try:
            import psycopg
            from psycopg.rows import dict_row

            return psycopg.connect(self.dsn, row_factory=dict_row)
        except Exception as exc:
            # Do not include the DSN: it can contain a password or private host.
            raise ConnectionError("PostgreSQL connection failed") from exc

    @contextmanager
    def _scope(self, *, conn: Any | None = None, write: bool = False) -> Iterator[Any]:
        """Yield a connection and manage only connections this object opened."""

        if conn is not None:
            yield conn
            return
        if self.connection is not None:
            # An injected connection is caller-owned. The operation remains
            # fail-closed but does not commit or close the caller's transaction.
            yield self.connection
            return

        opened = self._open_connection()
        try:
            yield opened
            if write:
                opened.commit()
            else:
                opened.rollback()
        except BaseException:
            try:
                opened.rollback()
            finally:
                opened.close()
            raise
        else:
            opened.close()

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """Open an explicit transaction for multiple store operations."""

        if self.connection is not None:
            yield self.connection
            return
        opened = self._open_connection()
        try:
            yield opened
            opened.commit()
        except BaseException:
            try:
                opened.rollback()
            finally:
                opened.close()
            raise
        else:
            opened.close()

    def execute_ddl(self, sql: str | None = None) -> None:
        """Execute the schema on PostgreSQL and propagate every DB failure."""

        with self._scope(write=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql or self.schema_sql())

    def set_local_guc(self, name: str, val: str, *, conn: Any | None = None) -> None:
        """Set a transaction-local pgvector GUC using a strict name allowlist."""

        if name != "hnsw.iterative_scan":
            raise ValueError("unsupported PostgreSQL GUC")
        if val not in {"relaxed_order", "strict_order", "off"}:
            raise ValueError("unsupported hnsw.iterative_scan value")
        with self._scope(conn=conn, write=conn is None and self.connection is None) as db:
            with db.cursor() as cur:
                # GUC names cannot be bound as values. The allowlist above is
                # therefore part of the injection boundary.
                cur.execute(f"SET LOCAL {name} = '{val}'")

    def set_guc_relaxed_order(self, *, conn: Any | None = None) -> bool:
        """Enable relaxed ordering only for pgvector versions that support it."""

        if _version_tuple(self.pgvector_version) < (0, 8, 0):
            return False
        self.set_local_guc("hnsw.iterative_scan", "relaxed_order", conn=conn)
        return True

    def insert_card(self, card: MemoryCard, conn: Any | None = None) -> str:
        """Insert one card using SQL; duplicate keys are not hidden."""

        self._validate_card(card)
        with self._scope(conn=conn, write=conn is None and self.connection is None) as db:
            self._insert_card_on(db, card)
        return card.memory_id

    def upsert_card(self, card: MemoryCard, conn: Any | None = None) -> str:
        """Atomically upsert a card, its pending embedding job, and graph projection outbox."""

        self._validate_card(card)
        with self._scope(conn=conn, write=conn is None and self.connection is None) as db:
            if not self._upsert_card_on(db, card):
                # The SQL conflict predicate deliberately refuses to mutate an
                # accepted authority row. Raising here also prevents an
                # outbox row from being created for a proposal that did not
                # become the stored card.
                raise ValueError("cannot overwrite an accepted memory card")
            if card.embedding_state == "pending" or card.embedding is None:
                self._enqueue_outbox_on(
                    db,
                    target_type="memory_card",
                    target_id=card.memory_id,
                    content_hash=card.content_hash,
                    payload_text=_card_payload_text(card),
                )
            # M7: PG authority transaction writes graph_projection_outbox
            episode = {
                "source_type": "memory_card",
                "source_id": card.memory_id,
                "source_revision": card.content_hash,
                "content_hash": card.content_hash,
                "authority_memory_id": card.memory_id,
                "project": card.project,
                "card_type": card.card_type,
                "title": card.title,
                "summary": card.summary,
                "typed_payload": card.typed_payload,
                "lifecycle_state": card.lifecycle_state,
                "currentness": card.currentness,
            }
            self._enqueue_graph_outbox_on(
                db,
                source_type="memory_card",
                source_id=card.memory_id,
                source_revision=card.content_hash,
                content_hash=card.content_hash,
                episode_payload=episode,
            )
        return card.memory_id

    def get_card(self, memory_id: str, conn: Any | None = None) -> MemoryCard | None:
        """Read one card back from PostgreSQL."""

        with self._scope(conn=conn) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT memory_id, project, card_type, title, summary,
                           typed_payload, lifecycle_state, authorization_status,
                           currentness, confidence, valid_from, valid_to,
                           embedding_model, embedding_revision, embedding_state,
                           embedding, content_hash, source_ref, created_at, updated_at
                      FROM memory_cards
                     WHERE memory_id = %s
                    """,
                    (memory_id,),
                )
                row = cur.fetchone()
        return _card_from_row(row) if row is not None else None

    def delete_card(self, memory_id: str, conn: Any | None = None) -> None:
        """Delete a card; PostgreSQL FK restrictions remain authoritative."""

        try:
            with self._scope(conn=conn, write=conn is None and self.connection is None) as db:
                with db.cursor() as cur:
                    cur.execute("DELETE FROM memory_cards WHERE memory_id = %s", (memory_id,))
        except Exception as exc:
            if "ForeignKeyViolation" in type(exc).__name__:
                raise ValueError("memory card is referenced by memory_edges") from exc
            raise

    def insert_edge(self, edge: MemoryEdge, conn: Any | None = None) -> int:
        """Insert a relational provenance edge and return its database id."""

        self._validate_edge(edge)
        values = (
            edge.src_id,
            edge.rel_type,
            edge.dst_id,
            edge.provenance_hash,
            edge.confidence,
            _json_text(edge.properties),
            edge.valid_from,
            edge.valid_to,
            edge.created_at,
        )
        columns = (
            "src_id, rel_type, dst_id, provenance_hash, confidence, properties, "
            "valid_from, valid_to, created_at"
        )
        with self._scope(conn=conn, write=conn is None and self.connection is None) as db:
            with db.cursor() as cur:
                if edge.edge_id:
                    cur.execute(
                        f"""
                        INSERT INTO memory_edges (edge_id, {columns})
                        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                        RETURNING edge_id
                        """,
                        (edge.edge_id, *values),
                    )
                else:
                    cur.execute(
                        f"""
                        INSERT INTO memory_edges ({columns})
                        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                        RETURNING edge_id
                        """,
                        values,
                    )
                row = cur.fetchone()
        if row is None:
            raise RuntimeError("memory edge insert returned no id")
        return int(_row_value(row, "edge_id", 0))

    def get_edges(
        self,
        src_id: str | None = None,
        dst_id: str | None = None,
        *,
        conn: Any | None = None,
    ) -> list[MemoryEdge]:
        """Read edges with optional endpoint filters."""

        clauses: list[str] = []
        params: list[Any] = []
        if src_id:
            clauses.append("src_id = %s")
            params.append(src_id)
        if dst_id:
            clauses.append("dst_id = %s")
            params.append(dst_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._scope(conn=conn) as db:
            with db.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT edge_id, src_id, rel_type, dst_id, provenance_hash,
                           confidence, properties, valid_from, valid_to, created_at
                      FROM memory_edges
                      {where}
                     ORDER BY edge_id
                    """,
                    params,
                )
                rows = cur.fetchall()
        return [_edge_from_row(row) for row in rows]

    def insert_chunk(self, chunk: SessionChunk, conn: Any | None = None) -> str:
        """Insert a chunk and enqueue its embedding in the same transaction."""

        normalized = self._normalize_chunk(chunk)
        values = (
            normalized.chunk_id,
            normalized.session_id_hash,
            normalized.project,
            normalized.provider,
            normalized.chunk_index,
            normalized.content_markdown,
            normalized.token_count,
            normalized.content_hash,
            normalized.embedding_model,
            normalized.embedding_state,
            normalized.embedding_revision,
            _vector_literal(normalized.embedding),
            normalized.created_at,
            normalized.updated_at,
        )
        with self._scope(conn=conn, write=conn is None and self.connection is None) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO session_memory_chunks (
                        chunk_id, session_id_hash, project, provider, chunk_index,
                        content_markdown, token_count, content_hash, embedding_model,
                        embedding_state, embedding_revision, embedding, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s::halfvec, %s, %s
                    )
                    ON CONFLICT (chunk_id) DO UPDATE SET
                        session_id_hash = EXCLUDED.session_id_hash,
                        project = EXCLUDED.project,
                        provider = EXCLUDED.provider,
                        chunk_index = EXCLUDED.chunk_index,
                        content_markdown = EXCLUDED.content_markdown,
                        token_count = EXCLUDED.token_count,
                        content_hash = EXCLUDED.content_hash,
                        embedding_model = EXCLUDED.embedding_model,
                        embedding_state = EXCLUDED.embedding_state,
                        embedding_revision = EXCLUDED.embedding_revision,
                        embedding = EXCLUDED.embedding,
                        updated_at = EXCLUDED.updated_at
                    """,
                    values,
                )
            if normalized.embedding_state == "pending" or normalized.embedding is None:
                self._enqueue_outbox_on(
                    db,
                    target_type="session_chunk",
                    target_id=normalized.chunk_id,
                    content_hash=normalized.content_hash,
                    payload_text=normalized.content_markdown,
                )
        return normalized.chunk_id

    def get_chunk(self, chunk_id: str, conn: Any | None = None) -> SessionChunk | None:
        """Read one session chunk from PostgreSQL."""

        with self._scope(conn=conn) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT chunk_id, session_id_hash, project, provider, chunk_index,
                           content_markdown, token_count, content_hash, embedding_model,
                           embedding_state, embedding_revision, embedding,
                           created_at, updated_at
                      FROM session_memory_chunks
                     WHERE chunk_id = %s
                    """,
                    (chunk_id,),
                )
                row = cur.fetchone()
        return _chunk_from_row(row) if row is not None else None

    def search_session_chunks(
        self,
        query_vector: list[float],
        project: str | None = None,
        limit: int = 5,
        conn: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Run vector similarity search over session_memory_chunks."""
        _validate_vector(query_vector)
        vector_literal = _vector_literal(query_vector)
        clauses = ["embedding IS NOT NULL"]
        params: list[Any] = [vector_literal]
        if project:
            clauses.append("project = %s")
            params.append(project)
        params.extend([vector_literal, int(limit)])
        with self._scope(conn=conn) as db:
            with db.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT chunk_id, session_id_hash, project, provider, chunk_index,
                           content_markdown, token_count, content_hash,
                           embedding <=> %s::halfvec AS distance
                      FROM session_memory_chunks
                     WHERE {' AND '.join(clauses)}
                     ORDER BY embedding <=> %s::halfvec, chunk_id
                     LIMIT %s
                    """,
                    params,
                )
                names = [col.name for col in cur.description]
                return [
                    dict(row) if isinstance(row, Mapping) else dict(zip(names, row))
                    for row in cur.fetchall()
                ]

    def enqueue_outbox(
        self,
        target_type: str,
        target_id: str,
        content_hash: str,
        payload_text: str,
        *,
        conn: Any | None = None,
    ) -> int:
        """Enqueue an embedding job with database-backed active deduplication."""

        with self._scope(conn=conn, write=conn is None and self.connection is None) as db:
            return self._enqueue_outbox_on(
                db,
                target_type=target_type,
                target_id=target_id,
                content_hash=content_hash,
                payload_text=payload_text,
            )

    def enqueue_embedding(
        self,
        target_type: str,
        target_id: str,
        content_hash: str,
        payload_text: str,
        *,
        conn: Any | None = None,
    ) -> int:
        """Compatibility alias for ``enqueue_outbox``."""

        return self.enqueue_outbox(
            target_type,
            target_id,
            content_hash,
            payload_text,
            conn=conn,
        )

    def get_outbox_job(self, outbox_id: int, *, conn: Any | None = None) -> OutboxJob | None:
        with self._scope(conn=conn) as db:
            with db.cursor() as cur:
                cur.execute(_OUTBOX_SELECT + " WHERE outbox_id = %s", (outbox_id,))
                row = cur.fetchone()
        return _outbox_from_row(row) if row is not None else None

    def list_outbox_jobs(
        self,
        *,
        status: str | None = None,
        conn: Any | None = None,
    ) -> list[OutboxJob]:
        params: list[Any] = []
        where = ""
        if status:
            where = " WHERE status = %s"
            params.append(status)
        with self._scope(conn=conn) as db:
            with db.cursor() as cur:
                cur.execute(_OUTBOX_SELECT + where + " ORDER BY outbox_id", params)
                rows = cur.fetchall()
        return [_outbox_from_row(row) for row in rows]

    def claim_outbox_leases(
        self,
        worker_id: str,
        batch_size: int = 10,
        lease_seconds: int = 30,
    ) -> list[OutboxJob]:
        """Claim jobs with PostgreSQL row locks and an atomic lease update."""

        if not worker_id or batch_size < 1 or lease_seconds < 1:
            raise ValueError("invalid outbox lease arguments")
        with self._scope(write=True) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    WITH candidates AS (
                        SELECT outbox_id
                          FROM embedding_outbox
                         WHERE (
                                status = 'queued'
                                OR (status = 'failed' AND retry_count < %s)
                                OR (status = 'processing' AND retry_count < %s)
                               )
                           AND (lease_until IS NULL OR lease_until < NOW())
                         ORDER BY created_at, outbox_id
                         LIMIT %s
                         FOR UPDATE SKIP LOCKED
                    )
                    UPDATE embedding_outbox AS job
                       SET status = 'processing',
                           worker_id = %s,
                           claimed_at = NOW(),
                           lease_until = NOW() + (%s * INTERVAL '1 second'),
                           updated_at = NOW()
                      FROM candidates
                     WHERE job.outbox_id = candidates.outbox_id
                    RETURNING job.outbox_id, job.target_type, job.target_id,
                              job.content_hash, job.payload_text, job.status,
                              job.claimed_at, job.lease_until, job.worker_id,
                              job.retry_count, job.last_error, job.created_at,
                              job.updated_at
                    """,
                    (
                        OUTBOX_RETRY_LIMIT,
                        OUTBOX_RETRY_LIMIT,
                        int(batch_size),
                        worker_id,
                        int(lease_seconds),
                    ),
                )
                rows = cur.fetchall()
        return [_outbox_from_row(row) for row in rows]

    def claim_outbox_jobs(
        self,
        worker_id: str,
        batch_size: int = 10,
        lease_seconds: int = 30,
    ) -> list[OutboxJob]:
        """Compatibility alias for ``claim_outbox_leases``."""

        return self.claim_outbox_leases(worker_id, batch_size, lease_seconds)

    def renew_outbox_lease(
        self,
        outbox_id: int,
        worker_id: str,
        lease_seconds: int = 30,
    ) -> bool:
        """Renew only a lease still owned by the same worker."""

        if not worker_id or lease_seconds < 1:
            raise ValueError("invalid outbox lease arguments")
        with self._scope(write=True) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    UPDATE embedding_outbox
                       SET lease_until = clock_timestamp() + (%s * INTERVAL '1 second'),
                           updated_at = NOW()
                     WHERE outbox_id = %s
                       AND status = 'processing'
                       AND worker_id = %s
                       AND lease_until > clock_timestamp()
                    """,
                    (int(lease_seconds), outbox_id, worker_id),
                )
                return cur.rowcount == 1

    def cas_update_embedding(
        self,
        outbox_id_or_target_type: int | str,
        target_id: str,
        enqueued_content_hash: str,
        vector: list[float],
        outbox_id: int | None = None,
        target_type: str = "memory_card",
        *,
        worker_id: str | None = None,
    ) -> bool:
        """CAS-write a vector and retire its job in one fenced transaction.

        When ``worker_id`` is provided, both the target row and the terminal
        outbox update are fenced by the still-active processing lease. This
        prevents a slow worker from writing after another worker reclaimed an
        expired lease.
        """

        if isinstance(outbox_id_or_target_type, int):
            actual_outbox_id = outbox_id_or_target_type
            actual_target_type = target_type
        else:
            actual_target_type = str(outbox_id_or_target_type)
            actual_outbox_id = outbox_id
        if actual_target_type not in {"memory_card", "session_chunk"}:
            raise ValueError("unsupported outbox target type")
        if not worker_id or actual_outbox_id is None:
            raise ValueError("embedding write-back requires worker_id and outbox_id")
        vector_literal = _vector_literal(vector)
        with self._scope(write=True) as db:
            with db.cursor() as cur:
                # 직렬화 순서를 outbox → target으로 통일한다. lease를 읽기만
                # 하면 동일 owner의 중복 호출이 embedding_revision을 두 번 올린다.
                cur.execute(
                    """
                    SELECT outbox_id FROM embedding_outbox
                     WHERE outbox_id = %s AND target_type = %s
                       AND target_id = %s AND content_hash = %s
                       AND status = 'processing' AND worker_id = %s
                       AND lease_until > clock_timestamp()
                     FOR UPDATE
                    """,
                    (actual_outbox_id, actual_target_type, target_id, enqueued_content_hash, worker_id),
                )
                if cur.fetchone() is None:
                    raise ValueError("outbox job not found or lease owner mismatch")
                table = "memory_cards" if actual_target_type == "memory_card" else "session_memory_chunks"
                id_column = "memory_id" if actual_target_type == "memory_card" else "chunk_id"
                lease_guard = ""
                target_params: list[Any] = [vector_literal, target_id, enqueued_content_hash]
                if worker_id is not None:
                    lease_guard = """
                       AND EXISTS (
                           SELECT 1
                             FROM embedding_outbox AS job
                            WHERE job.outbox_id = %s
                              AND job.target_type = %s
                              AND job.target_id = %s
                              AND job.content_hash = %s
                              AND job.status = 'processing'
                              AND job.worker_id = %s
                              AND job.lease_until > clock_timestamp()
                       )
                    """
                    target_params.extend(
                        [actual_outbox_id, actual_target_type, target_id, enqueued_content_hash, worker_id]
                    )
                cur.execute(
                    f"""
                    UPDATE {table}
                       SET embedding = %s::halfvec,
                           embedding_state = 'ready',
                           embedding_revision = embedding_revision + 1,
                           updated_at = NOW()
                     WHERE {id_column} = %s
                       AND content_hash = %s
                       {lease_guard}
                    """,
                    target_params,
                )
                updated = cur.rowcount == 1
                if actual_outbox_id is not None:
                    outbox_guard = ""
                    outbox_params: list[Any] = [updated, actual_outbox_id]
                    if worker_id is not None:
                        outbox_guard = """
                           AND status = 'processing'
                           AND worker_id = %s
                           AND lease_until > clock_timestamp()
                        """
                        outbox_params.append(worker_id)
                    cur.execute(
                        f"""
                        UPDATE embedding_outbox
                           SET status = CASE WHEN %s THEN 'completed' ELSE 'cas_skipped' END,
                               last_error = CASE WHEN %s THEN NULL ELSE 'CAS skip: content_hash mismatch' END,
                               lease_until = NULL,
                               updated_at = NOW()
                         WHERE outbox_id = %s
                           {outbox_guard}
                        """,
                        [updated, updated, *outbox_params[1:]],
                    )
                    if worker_id is not None and cur.rowcount != 1:
                        raise ValueError("outbox job not found or lease owner mismatch")
        return updated

    def apply_embedding_cas(
        self,
        outbox_id: int,
        target_type: str,
        target_id: str,
        enqueued_content_hash: str,
        vector: list[float],
        *,
        worker_id: str | None = None,
    ) -> bool:
        """Compatibility alias for ``cas_update_embedding``."""

        return self.cas_update_embedding(
            outbox_id,
            target_id,
            enqueued_content_hash,
            vector,
            target_type=target_type,
            worker_id=worker_id,
        )

    def mark_outbox_failed(
        self,
        outbox_id: int,
        error_message: str,
        max_retries: int = OUTBOX_RETRY_LIMIT,
        *,
        worker_id: str | None = None,
    ) -> None:
        """Record failure, schedule a retry, or move the job to dead letter."""

        if max_retries < 1:
            raise ValueError("max_retries must be positive")
        if not worker_id:
            raise ValueError("outbox failure requires worker_id")
        with self._scope(write=True) as db:
            with db.cursor() as cur:
                owner_clause = ""
                if worker_id is not None:
                    owner_clause = " AND worker_id = %s"
                params: list[Any] = [
                    error_message,
                    int(max_retries),
                    int(max_retries),
                    outbox_id,
                    *([worker_id] if worker_id is not None else []),
                ]
                cur.execute(
                    f"""
                    UPDATE embedding_outbox
                           SET retry_count = retry_count + 1,
                           last_error = %s,
                           status = CASE
                               WHEN retry_count + 1 >= %s THEN 'dead_letter'
                               ELSE 'failed'
                           END,
                           lease_until = CASE
                               WHEN retry_count + 1 >= %s THEN NULL
                               ELSE NOW() + (LEAST(60, POWER(2, retry_count + 1)) * INTERVAL '1 second')
                           END,
                           updated_at = NOW()
                    WHERE outbox_id = %s
                       AND status = 'processing'
                       AND lease_until > clock_timestamp(){owner_clause}
                    """,
                    params,
                )
                if cur.rowcount != 1:
                    raise ValueError("outbox job not found or lease owner mismatch")
                cur.execute(
                    """
                    UPDATE memory_cards AS card
                       SET embedding_state = 'failed', updated_at = NOW()
                      FROM embedding_outbox AS job
                     WHERE job.outbox_id = %s
                       AND job.status = 'dead_letter'
                       AND job.target_type = 'memory_card'
                       AND card.memory_id = job.target_id
                       AND card.content_hash = job.content_hash
                    """,
                    (outbox_id,),
                )
                cur.execute(
                    """
                    UPDATE session_memory_chunks AS chunk
                       SET embedding_state = 'failed', updated_at = NOW()
                      FROM embedding_outbox AS job
                     WHERE job.outbox_id = %s
                       AND job.status = 'dead_letter'
                       AND job.target_type = 'session_chunk'
                       AND chunk.chunk_id = job.target_id
                       AND chunk.content_hash = job.content_hash
                    """,
                    (outbox_id,),
                )

    def mark_outbox_completed(
        self,
        outbox_id: int,
        *,
        worker_id: str | None = None,
    ) -> None:
        if not worker_id:
            raise ValueError("outbox completion requires worker_id")
        with self._scope(write=True) as db:
            with db.cursor() as cur:
                owner_clause = ""
                params: list[Any] = [outbox_id]
                if worker_id is not None:
                    owner_clause = " AND worker_id = %s"
                    params.append(worker_id)
                cur.execute(
                    f"""
                    UPDATE embedding_outbox
                       SET status = 'completed', lease_until = NULL, updated_at = NOW()
                     WHERE outbox_id = %s
                       AND status = 'processing'
                       AND lease_until > clock_timestamp(){owner_clause}
                    """,
                    params,
                )
                if cur.rowcount != 1:
                    raise ValueError("outbox job not found or lease owner mismatch")

    def list_authorized_cards(
        self, *, project: str, memory_ids: list[str] | None = None,
        as_of: datetime | date | str | None = None, limit: int = 100,
        after_memory_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """공개 조회와 graph join의 권위 필터를 SQL 한 번으로 적용한다."""
        if not project or not 1 <= limit <= 100:
            raise ValueError("project and bounded limit required")
        clauses, params = _authority_filter(project, as_of)
        if memory_ids is not None:
            clauses.append("memory_id = ANY(%s)")
            params.append(list(memory_ids)[:100])
        if after_memory_id is not None:
            clauses.append("memory_id > %s")
            params.append(after_memory_id)
        with self._scope() as db:
            with db.cursor() as cur:
                cur.execute(
                    f"""SELECT memory_id, project, card_type, title, summary,
                               typed_payload, currentness, content_hash, source_ref,
                               lifecycle_state, authorization_status
                          FROM memory_cards WHERE {' AND '.join(clauses)}
                         ORDER BY memory_id LIMIT %s""",
                    [*params, limit],
                )
                names = [column.name for column in cur.description]
                return [dict(row) if isinstance(row, Mapping) else dict(zip(names, row))
                        for row in cur.fetchall()]

    def graph_projection_health(self, project: str, *, as_of: Any = None) -> dict[str, Any]:
        """현재 권위 revision의 투영 누락만 측정한다. 검색 무응답은 lag 증거가 아니다."""
        clauses, params = _authority_filter(project, as_of)
        with self._scope() as db:
            with db.cursor() as cur:
                cur.execute(
                    f"""SELECT COUNT(*) AS missing,
                               MIN((SELECT MIN(job.created_at) FROM graph_projection_outbox job
                                    WHERE job.source_type = 'memory_card'
                                      AND job.source_id = memory_cards.memory_id
                                      AND job.content_hash = memory_cards.content_hash)) AS queued_at
                          FROM memory_cards WHERE {' AND '.join(clauses)}
                           AND NOT EXISTS (
                               SELECT 1 FROM graph_projection_outbox job
                                WHERE job.source_type = 'memory_card'
                                  AND job.source_id = memory_cards.memory_id
                                  AND job.content_hash = memory_cards.content_hash
                                  AND job.status = 'completed')""",
                    params,
                )
                row = cur.fetchone()
        queued_at = _row_value(row, "queued_at", 1)
        lag = None if queued_at is None else max(
            0, int((datetime.now(timezone.utc) - queued_at).total_seconds() * 1000)
        )
        return {"unprojected": int(_row_value(row, "missing", 0)) > 0,
                "projection_lag_ms": lag}

    def hybrid_search(
        self,
        project: str | list[float] = "",
        query_vector: list[float] | str | None = None,
        limit: int = 5,
        card_type: str | None = None,
        authorization_status: str | None = "active",
        currentness: str | None = "current",
        as_of: datetime | date | str | None = None,
        text_query: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Run filtered pgvector search with deterministic tie-breaking."""

        if isinstance(project, (list, tuple)):
            actual_query_vector = list(project)
            actual_project = str(query_vector) if query_vector is not None else str(kwargs.get("project") or "")
        else:
            actual_project = str(project or kwargs.get("project") or "")
            actual_query_vector = query_vector if query_vector is not None else kwargs.get("query_vector")
        if actual_query_vector is None:
            raise ValueError("hybrid_search requires a query vector")
        if isinstance(actual_query_vector, str):
            raise ValueError("query vector must be a numeric sequence")
        _validate_vector(actual_query_vector)
        if limit < 1:
            raise ValueError("limit must be positive")
        vector_literal = _vector_literal(list(actual_query_vector))
        clauses = ["embedding IS NOT NULL", "embedding_state = 'ready'"]
        params: list[Any] = []
        if actual_project:
            clauses.append("project = %s")
            params.append(actual_project)
        if authorization_status:
            clauses.append("authorization_status = %s")
            params.append(authorization_status)
        if currentness == "current" and as_of:
            clauses.append("currentness IN ('current', 'superseded')")
        elif currentness:
            clauses.append("currentness = %s")
            params.append(currentness)
        if card_type:
            clauses.append("card_type = %s")
            params.append(card_type)
        clauses.append("lifecycle_state IN ('accepted', 'human_accepted', 'auto_accepted')")
        as_of_dt = _parse_as_of(as_of) or datetime.now(timezone.utc)
        clauses.append("valid_from <= %s AND (valid_to IS NULL OR valid_to > %s)")
        params.extend((as_of_dt, as_of_dt))
        score_expression = "1.0 - distance"
        query_params = [vector_literal, *params, vector_literal, max(50, int(limit) * 5)]
        if text_query and text_query.strip():
            score_expression = (
                "(0.8 * (1.0 - distance) + "
                "0.2 * ts_rank_cd(to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(summary, '')), "
                "plainto_tsquery('simple', %s)))"
            )
            query_params.append(text_query)
        query_params.append(int(limit))
        with self._scope(write=False) as db:
            self.set_guc_relaxed_order(conn=db)
            with db.cursor() as cur:
                cur.execute(
                    f"""
                    WITH candidates AS MATERIALIZED (
                        SELECT *, embedding <=> %s::halfvec AS distance
                          FROM memory_cards
                         WHERE {' AND '.join(clauses)}
                         ORDER BY embedding <=> %s::halfvec
                         LIMIT %s
                    )
                    SELECT memory_id, card_type, title, summary, typed_payload,
                           currentness, confidence, content_hash,
                           {score_expression} AS similarity_score
                      FROM candidates
                     ORDER BY similarity_score DESC, memory_id
                     LIMIT %s
                    """,
                    query_params,
                )
                rows = cur.fetchall()
        return [
            {
                "memory_id": str(_row_value(row, "memory_id", 0)),
                "card_type": str(_row_value(row, "card_type", 1)),
                "title": str(_row_value(row, "title", 2)),
                "summary": str(_row_value(row, "summary", 3)),
                "typed_payload": _json_value(_row_value(row, "typed_payload", 4)),
                "currentness": str(_row_value(row, "currentness", 5)),
                "confidence": float(_row_value(row, "confidence", 6)),
                "content_hash": str(_row_value(row, "content_hash", 7)),
                "similarity_score": round(float(_row_value(row, "similarity_score", 8)), 6),
            }
            for row in rows
        ]

    def hybrid_vector_search(
        self,
        query_vector: list[float],
        project: str,
        limit: int = 5,
        authorization_status: str | None = "active",
        currentness: str | None = "current",
        as_of: datetime | date | str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        return self.hybrid_search(
            project=project,
            query_vector=query_vector,
            limit=limit,
            authorization_status=authorization_status,
            currentness=currentness,
            as_of=as_of,
            **kwargs,
        )

    def traverse_provenance_dag(
        self,
        root_memory_id: str,
        max_depth: int = 5,
        as_of: datetime | date | str | None = None,
    ) -> list[dict[str, Any]]:
        """단일 root 호환 진입점도 공개 evidence와 같은 권위 필터를 사용한다."""
        _parse_as_of(as_of)
        if not 1 <= max_depth <= 5:
            raise ValueError("max_depth must be between 1 and 5")
        root = self.get_card(root_memory_id)
        if root is None:
            return []
        return self.read_authorized_evidence(
            project=root.project, root_memory_ids=[root_memory_id],
            max_depth=max_depth, as_of=as_of,
        )["edges"]

    def read_authorized_evidence(
        self, *, project: str, root_memory_ids: list[str],
        as_of: datetime | date | str | None = None, max_depth: int = 5, limit: int = 100,
    ) -> dict[str, Any]:
        """승인된 명시적 관계만 한 번에 탐색한다. 추론 graph와 원문은 읽지 않는다."""
        if not project or not 1 <= len(root_memory_ids) <= 20:
            raise ValueError("project and 1..20 evidence roots required")
        if not 1 <= max_depth <= 5 or not 1 <= limit <= 100:
            raise ValueError("bounded evidence depth and limit required")
        instant = _parse_as_of(as_of) or datetime.now(timezone.utc)
        clauses, params = _authority_filter(project, as_of)
        with self._scope() as db:
            with db.cursor() as cur:
                cur.execute("SELECT current_setting('statement_timeout') AS value, setting::bigint AS timeout_ms FROM pg_settings WHERE name='statement_timeout'")
                timeout_row = cur.fetchone()
                previous_timeout = _row_value(timeout_row, "value", 0)
                previous_ms = int(_row_value(timeout_row, "timeout_ms", 1))
                effective_ms = min(previous_ms, 1000) if previous_ms else 1000
                cur.execute("SELECT set_config('statement_timeout', %s, true)", (f"{effective_ms}ms",))
                cur.execute(
                    f"""
                    WITH RECURSIVE authorized AS MATERIALIZED (
                        SELECT memory_id, content_hash FROM memory_cards
                         WHERE {' AND '.join(clauses)}
                    ), permitted_edges AS MATERIALIZED (
                        SELECT edge.edge_id, edge.src_id, edge.dst_id, edge.rel_type,
                               edge.provenance_hash, src.content_hash AS src_content_hash,
                               dst.content_hash AS dst_content_hash
                          FROM memory_edges AS edge
                          JOIN authorized AS src ON src.memory_id = edge.src_id
                          JOIN authorized AS dst ON dst.memory_id = edge.dst_id
                         WHERE edge.valid_from <= %s
                           AND (edge.valid_to IS NULL OR edge.valid_to > %s)
                           AND edge.src_id <> edge.dst_id
                    ), walk AS (
                        SELECT edge.*, edge.src_id AS root_id, 1 AS depth,
                               ARRAY[edge.src_id, edge.dst_id]::varchar[] AS visited_path
                          FROM permitted_edges AS edge
                         WHERE edge.src_id = ANY(%s)
                        UNION ALL
                        SELECT edge.*, tree.root_id, tree.depth + 1,
                               tree.visited_path || edge.dst_id
                          FROM permitted_edges AS edge
                          JOIN walk AS tree ON edge.src_id = tree.dst_id
                         WHERE tree.depth < %s
                           AND NOT (edge.dst_id = ANY(tree.visited_path))
                    ), unique_edges AS (
                        SELECT DISTINCT ON (root_id, edge_id) * FROM walk
                         ORDER BY root_id, edge_id, depth, visited_path
                    ), limited AS (
                    SELECT root_id, src_id, dst_id, rel_type, provenance_hash,
                           src_content_hash, dst_content_hash, depth, visited_path,
                           EXISTS (SELECT 1 FROM permitted_edges AS next
                                    WHERE unique_edges.depth = %s
                                      AND next.src_id = unique_edges.dst_id
                                      AND NOT next.dst_id = ANY(unique_edges.visited_path)) AS depth_limited
                      FROM unique_edges
                     ORDER BY root_id, depth, src_id, dst_id, rel_type
                     LIMIT %s
                    )
                    SELECT COALESCE(jsonb_agg(to_jsonb(limited) ORDER BY root_id, depth, src_id, dst_id, rel_type), '[]'::jsonb) AS edges,
                           (SELECT jsonb_object_agg(memory_id, content_hash)
                              FROM authorized WHERE memory_id=ANY(%s)) AS root_hashes
                      FROM limited
                    """,
                    [*params, instant, instant, root_memory_ids, max_depth, max_depth, limit + 1, root_memory_ids],
                )
                batch = cur.fetchone()
                rows = _row_value(batch, "edges", 0)
                root_hashes = _row_value(batch, "root_hashes", 1) or {}
                cur.execute("SELECT set_config('statement_timeout', %s, true)", (previous_timeout,))
        truncated = len(rows) > limit or any(row["depth_limited"] for row in rows)
        return {"edges": [{key: value for key, value in row.items() if key != "depth_limited"}
                          for row in rows[:limit]], "truncated": truncated, "max_depth": max_depth,
                "root_hashes": root_hashes}

    def recursive_dag_traversal(
        self,
        root_memory_id: str,
        max_depth: int = 5,
        as_of: datetime | date | str | None = None,
    ) -> list[dict[str, Any]]:
        return self.traverse_provenance_dag(root_memory_id, max_depth=max_depth, as_of=as_of)

    def _insert_card_on(self, conn: Any, card: MemoryCard) -> None:
        with conn.cursor() as cur:
            cur.execute(_CARD_INSERT_SQL, _card_values(card))

    def _upsert_card_on(self, conn: Any, card: MemoryCard) -> bool:
        with conn.cursor() as cur:
            cur.execute(_CARD_UPSERT_SQL, _card_values(card))
            return cur.rowcount == 1

    def _enqueue_outbox_on(
        self,
        conn: Any,
        *,
        target_type: str,
        target_id: str,
        content_hash: str,
        payload_text: str,
    ) -> int:
        if target_type not in {"memory_card", "session_chunk"}:
            raise ValueError("unsupported outbox target type")
        if not target_id:
            raise ValueError("outbox target_id is required")
        if not isinstance(payload_text, str):
            raise ValueError("outbox payload_text must be text")
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT outbox_id
                  FROM embedding_outbox
                 WHERE target_type = %s
                   AND target_id = %s
                   AND content_hash = %s
                   AND status IN ('queued', 'processing', 'failed')
                 ORDER BY outbox_id
                 LIMIT 1
                """,
                (target_type, target_id, content_hash),
            )
            existing = cur.fetchone()
            if existing is not None:
                return int(_row_value(existing, "outbox_id", 0))
            row = None
            try:
                cur.execute("SAVEPOINT embedding_outbox_enqueue")
                cur.execute(
                    """
                    INSERT INTO embedding_outbox (
                        target_type, target_id, content_hash, payload_text, status
                    ) VALUES (%s, %s, %s, %s, 'queued')
                    RETURNING outbox_id
                    """,
                    (target_type, target_id, content_hash, payload_text),
                )
                row = cur.fetchone()
            except Exception as exc:
                # The partial unique index is the race-safe authority. If a
                # concurrent transaction won it, read that winner; every other
                # database error is propagated.
                if "UniqueViolation" not in type(exc).__name__:
                    raise
                cur.execute("ROLLBACK TO SAVEPOINT embedding_outbox_enqueue")
                cur.execute(
                    """
                    SELECT outbox_id
                      FROM embedding_outbox
                     WHERE target_type = %s AND target_id = %s
                       AND content_hash = %s
                       AND status IN ('queued', 'processing', 'failed')
                     ORDER BY outbox_id
                     LIMIT 1
                    """,
                    (target_type, target_id, content_hash),
                )
                row = cur.fetchone()
            finally:
                # Releasing a savepoint is harmless after the successful
                # insert and leaves the caller's outer transaction intact.
                cur.execute("RELEASE SAVEPOINT embedding_outbox_enqueue")
        if row is None:
            raise RuntimeError("embedding outbox insert returned no id")
        return int(_row_value(row, "outbox_id", 0))

    def _enqueue_graph_outbox_on(
        self,
        conn: Any,
        *,
        source_type: str,
        source_id: str,
        source_revision: str,
        content_hash: str,
        episode_payload: dict[str, Any],
    ) -> int:
        if source_type not in {"memory_card", "session_chunk"}:
            raise ValueError("unsupported graph projection source type")
        if not source_id or not source_revision or not content_hash:
            raise ValueError("graph projection key fields are required")
        payload_json = json.dumps(episode_payload) if not isinstance(episode_payload, str) else episode_payload
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT projection_id
                  FROM graph_projection_outbox
                 WHERE source_type = %s
                   AND source_id = %s
                   AND source_revision = %s
                 LIMIT 1
                """,
                (source_type, source_id, source_revision),
            )
            existing = cur.fetchone()
            if existing is not None:
                return int(_row_value(existing, "projection_id", 0))
            row = None
            try:
                cur.execute("SAVEPOINT graph_outbox_enqueue")
                cur.execute(
                    """
                    INSERT INTO graph_projection_outbox (
                        source_type, source_id, source_revision, content_hash,
                        episode_payload, status
                    ) VALUES (%s, %s, %s, %s, %s::jsonb, 'queued')
                    RETURNING projection_id
                    """,
                    (source_type, source_id, source_revision, content_hash, payload_json),
                )
                row = cur.fetchone()
            except Exception as exc:
                if "UniqueViolation" not in type(exc).__name__:
                    raise
                cur.execute("ROLLBACK TO SAVEPOINT graph_outbox_enqueue")
                cur.execute(
                    """
                    SELECT projection_id
                      FROM graph_projection_outbox
                     WHERE source_type = %s
                       AND source_id = %s
                       AND source_revision = %s
                     LIMIT 1
                    """,
                    (source_type, source_id, source_revision),
                )
                row = cur.fetchone()
            finally:
                cur.execute("RELEASE SAVEPOINT graph_outbox_enqueue")
        if row is None:
            raise RuntimeError("graph projection outbox insert returned no id")
        return int(_row_value(row, "projection_id", 0))

    def claim_graph_projection_leases(
        self,
        worker_id: str,
        batch_size: int = 10,
        lease_seconds: int = 30,
    ) -> list[GraphOutboxJob]:
        """Claim graph projection outbox jobs with atomic FOR UPDATE SKIP LOCKED lease."""
        if not worker_id or batch_size < 1 or lease_seconds < 1:
            raise ValueError("invalid outbox lease arguments")
        with self._scope(write=True) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    WITH candidates AS (
                        SELECT projection_id
                          FROM graph_projection_outbox
                         WHERE (
                                status = 'queued'
                                OR (status = 'failed' AND retry_count < %s)
                                OR (status = 'processing' AND retry_count < %s)
                               )
                           AND (lease_until IS NULL OR lease_until < NOW())
                         ORDER BY created_at, projection_id
                         LIMIT %s
                         FOR UPDATE SKIP LOCKED
                    )
                    UPDATE graph_projection_outbox AS job
                       SET status = 'processing',
                           worker_id = %s,
                           claimed_at = NOW(),
                           lease_until = NOW() + (%s * INTERVAL '1 second'),
                           updated_at = NOW()
                      FROM candidates
                     WHERE job.projection_id = candidates.projection_id
                    RETURNING job.projection_id, job.source_type, job.source_id,
                              job.source_revision, job.content_hash, job.episode_payload,
                              job.status, job.claimed_at, job.lease_until,
                              job.worker_id, job.retry_count, job.last_error,
                              job.created_at, job.updated_at
                    """,
                    (
                        OUTBOX_RETRY_LIMIT,
                        OUTBOX_RETRY_LIMIT,
                        int(batch_size),
                        worker_id,
                        int(lease_seconds),
                    ),
                )
                rows = cur.fetchall()
        return [_graph_outbox_from_row(row) for row in rows]

    def mark_graph_projection_completed(
        self,
        projection_id: int,
        worker_id: str,
    ) -> None:
        """Mark a graph projection job completed after successful projection."""
        with self._scope(write=True) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    UPDATE graph_projection_outbox
                       SET status = 'completed',
                           updated_at = NOW()
                     WHERE projection_id = %s
                       AND worker_id = %s
                       AND status = 'processing'
                    """,
                    (int(projection_id), str(worker_id)),
                )
                if cur.rowcount == 0:
                    raise ValueError(f"graph projection job {projection_id} not owned or not processing")

    def mark_graph_projection_failed(
        self,
        projection_id: int,
        error_message: str,
        max_retries: int = OUTBOX_RETRY_LIMIT,
        *,
        worker_id: str | None = None,
    ) -> None:
        """Record graph projection failure, schedule retry, or move to dead letter."""
        if max_retries < 1:
            raise ValueError("max_retries must be positive")
        if not worker_id:
            raise ValueError("outbox failure requires worker_id")
        with self._scope(write=True) as db:
            with db.cursor() as cur:
                params: list[Any] = [
                    error_message,
                    int(max_retries),
                    int(max_retries),
                    int(projection_id),
                    str(worker_id),
                ]
                cur.execute(
                    """
                    UPDATE graph_projection_outbox
                       SET retry_count = retry_count + 1,
                           last_error = %s,
                           status = CASE
                               WHEN retry_count + 1 >= %s THEN 'dead_letter'
                               ELSE 'failed'
                           END,
                           lease_until = CASE
                               WHEN retry_count + 1 >= %s THEN NULL
                               ELSE NOW() + (LEAST(60, POWER(2, retry_count + 1)) * INTERVAL '1 second')
                           END,
                           updated_at = NOW()
                     WHERE projection_id = %s
                       AND worker_id = %s
                       AND status = 'processing'
                    """,
                    params,
                )
                if cur.rowcount == 0:
                    raise ValueError(f"graph projection job {projection_id} not owned or not processing")

    def list_graph_projection_jobs(
        self,
        *,
        status: str | None = None,
        conn: Any | None = None,
    ) -> list[GraphOutboxJob]:
        params: list[Any] = []
        where = ""
        if status:
            where = " WHERE status = %s"
            params.append(status)
        with self._scope(conn=conn) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT projection_id, source_type, source_id, source_revision,
                           content_hash, episode_payload, status, claimed_at,
                           lease_until, worker_id, retry_count, last_error,
                           created_at, updated_at
                      FROM graph_projection_outbox
                    """
                    + where
                    + " ORDER BY projection_id",
                    params,
                )
                rows = cur.fetchall()
        return [_graph_outbox_from_row(row) for row in rows]

    @staticmethod
    def _validate_card(card: MemoryCard) -> None:
        if not card.memory_id or not card.project or not card.card_type:
            raise ValueError("memory card identity fields are required")
        if card.embedding is not None:
            _validate_vector(card.embedding)
        if card.valid_to and card.valid_to < card.valid_from:
            raise ValueError("valid_to cannot be earlier than valid_from")
        if card.lifecycle_state not in {
            "candidate", "suggested_accept", "human_accepted", "human_rejected",
            "auto_accepted", "needs_review", "accepted", "rejected",
        }:
            raise ValueError(f"Invalid lifecycle_state: {card.lifecycle_state}")
        if card.authorization_status not in {"active", "disabled"}:
            raise ValueError(f"Invalid authorization_status: {card.authorization_status}")
        if card.currentness not in {"current", "superseded", "stale", "conflicted", "unknown"}:
            raise ValueError(f"Invalid currentness: {card.currentness}")
        if not 0.0 <= float(card.confidence) <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")

    @staticmethod
    def _validate_edge(edge: MemoryEdge) -> None:
        if not edge.src_id or not edge.dst_id:
            raise ValueError("memory edge endpoints are required")
        if edge.rel_type not in {"supersedes", "derived_from", "contradicts", "supports"}:
            raise ValueError(f"Invalid rel_type: {edge.rel_type}")
        if not 0.0 <= float(edge.confidence) <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        if edge.valid_to and edge.valid_to < edge.valid_from:
            raise ValueError("valid_to cannot be earlier than valid_from")

    @staticmethod
    def _normalize_chunk(chunk: SessionChunk) -> SessionChunk:
        if not chunk.chunk_id or not chunk.project:
            raise ValueError("session chunk identity fields are required")
        if chunk.embedding is not None:
            _validate_vector(chunk.embedding)
        if chunk.embedding_state not in {"pending", "ready", "stale", "failed"}:
            raise ValueError(f"Invalid embedding_state: {chunk.embedding_state}")
        content_hash = chunk.content_hash
        if not content_hash:
            content_hash = "sha256:" + hashlib.sha256(
                chunk.content_markdown.encode("utf-8")
            ).hexdigest()
        if content_hash != chunk.content_hash:
            return SessionChunk(
                **{**chunk.__dict__, "content_hash": content_hash}
            )
        return chunk


_CARD_COLUMNS = (
    "memory_id, project, card_type, title, summary, typed_payload, "
    "lifecycle_state, authorization_status, currentness, confidence, valid_from, valid_to, "
    "embedding_model, embedding_revision, embedding_state, embedding, content_hash, source_ref, "
    "created_at, updated_at"
)
_CARD_INSERT_SQL = f"""
    INSERT INTO memory_cards ({_CARD_COLUMNS})
    VALUES (
        %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s::halfvec, %s, %s::jsonb, %s, %s
    )
"""
_CARD_UPSERT_SQL = f"""
    INSERT INTO memory_cards ({_CARD_COLUMNS})
    VALUES (
        %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s::halfvec, %s, %s::jsonb, %s, %s
    )
    ON CONFLICT (memory_id) DO UPDATE SET
        project = EXCLUDED.project,
        card_type = EXCLUDED.card_type,
        title = EXCLUDED.title,
        summary = EXCLUDED.summary,
        typed_payload = EXCLUDED.typed_payload,
        lifecycle_state = EXCLUDED.lifecycle_state,
        authorization_status = EXCLUDED.authorization_status,
        currentness = EXCLUDED.currentness,
        confidence = EXCLUDED.confidence,
        valid_from = EXCLUDED.valid_from,
        valid_to = EXCLUDED.valid_to,
        embedding_model = EXCLUDED.embedding_model,
        embedding_revision = EXCLUDED.embedding_revision,
        embedding_state = EXCLUDED.embedding_state,
        embedding = EXCLUDED.embedding,
        content_hash = EXCLUDED.content_hash,
        source_ref = EXCLUDED.source_ref,
        updated_at = EXCLUDED.updated_at
    WHERE memory_cards.lifecycle_state NOT IN ('accepted', 'human_accepted', 'auto_accepted')
"""

_OUTBOX_SELECT = """
    SELECT outbox_id, target_type, target_id, content_hash, payload_text,
           status, claimed_at, lease_until, worker_id, retry_count, last_error,
           created_at, updated_at
      FROM embedding_outbox
"""


def _authority_filter(project: str, as_of: Any) -> tuple[list[str], list[Any]]:
    instant = _parse_as_of(as_of) or datetime.now(timezone.utc)
    return [
        "project = %s", "authorization_status = 'active'",
        "currentness IN ('current', 'superseded')" if as_of else "currentness = 'current'",
        "lifecycle_state IN ('accepted', 'human_accepted', 'auto_accepted')",
        "valid_from <= %s", "(valid_to IS NULL OR valid_to > %s)",
    ], [project, instant, instant]


def _card_values(card: MemoryCard) -> tuple[Any, ...]:
    return (
        card.memory_id,
        card.project,
        card.card_type,
        card.title,
        card.summary,
        _json_text(card.typed_payload),
        card.lifecycle_state,
        card.authorization_status,
        card.currentness,
        card.confidence,
        card.valid_from,
        card.valid_to,
        card.embedding_model,
        card.embedding_revision,
        card.embedding_state,
        _vector_literal(card.embedding),
        card.content_hash,
        _json_text(card.source_ref),
        card.created_at,
        card.updated_at,
    )


def _card_payload_text(card: MemoryCard) -> str:
    return card.summary or card.title or json.dumps(
        card.typed_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_text(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))


def _json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if value is not None else {}


def _vector_literal(vector: list[float] | tuple[float, ...] | None) -> str | None:
    if vector is None:
        return None
    _validate_vector(vector)
    return "[" + ",".join(format(float(value), ".17g") for value in vector) + "]"


def _validate_vector(vector: Any) -> None:
    if not isinstance(vector, (list, tuple)) or len(vector) != VECTOR_DIMENSION:
        length = len(vector) if isinstance(vector, (list, tuple)) else "unknown"
        raise ValueError(f"Vector dimension mismatch: expected {VECTOR_DIMENSION}, got {length}")
    for value in vector:
        if not math.isfinite(float(value)):
            raise ValueError("vector values must be finite numbers")


def _parse_vector(value: Any) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [float(item) for item in value]
    if isinstance(value, str):
        raw = value.strip().strip("[]")
        if not raw:
            return []
        return [float(item.strip()) for item in raw.split(",")]
    return [float(item) for item in value]


def _row_value(row: Any, key: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row[key]
    return row[index]


def _card_from_row(row: Any) -> MemoryCard:
    return MemoryCard(
        memory_id=str(_row_value(row, "memory_id", 0)),
        project=str(_row_value(row, "project", 1)),
        card_type=str(_row_value(row, "card_type", 2)),
        title=str(_row_value(row, "title", 3)),
        summary=str(_row_value(row, "summary", 4)),
        typed_payload=_json_value(_row_value(row, "typed_payload", 5)),
        lifecycle_state=str(_row_value(row, "lifecycle_state", 6)),
        authorization_status=str(_row_value(row, "authorization_status", 7)),
        currentness=str(_row_value(row, "currentness", 8)),
        confidence=float(_row_value(row, "confidence", 9)),
        valid_from=_as_aware_datetime(_row_value(row, "valid_from", 10)),
        valid_to=_as_optional_datetime(_row_value(row, "valid_to", 11)),
        embedding_model=str(_row_value(row, "embedding_model", 12)),
        embedding_revision=int(_row_value(row, "embedding_revision", 13)),
        embedding_state=str(_row_value(row, "embedding_state", 14)),
        embedding=_parse_vector(_row_value(row, "embedding", 15)),
        content_hash=str(_row_value(row, "content_hash", 16)),
        source_ref=_json_value(_row_value(row, "source_ref", 17)),
        created_at=_as_aware_datetime(_row_value(row, "created_at", 18)),
        updated_at=_as_aware_datetime(_row_value(row, "updated_at", 19)),
    )


def _edge_from_row(row: Any) -> MemoryEdge:
    return MemoryEdge(
        edge_id=int(_row_value(row, "edge_id", 0)),
        src_id=str(_row_value(row, "src_id", 1)),
        rel_type=str(_row_value(row, "rel_type", 2)),
        dst_id=str(_row_value(row, "dst_id", 3)),
        provenance_hash=str(_row_value(row, "provenance_hash", 4)),
        confidence=float(_row_value(row, "confidence", 5)),
        properties=_json_value(_row_value(row, "properties", 6)),
        valid_from=_as_aware_datetime(_row_value(row, "valid_from", 7)),
        valid_to=_as_optional_datetime(_row_value(row, "valid_to", 8)),
        created_at=_as_aware_datetime(_row_value(row, "created_at", 9)),
    )


def _chunk_from_row(row: Any) -> SessionChunk:
    return SessionChunk(
        chunk_id=str(_row_value(row, "chunk_id", 0)),
        session_id_hash=str(_row_value(row, "session_id_hash", 1)),
        project=str(_row_value(row, "project", 2)),
        provider=str(_row_value(row, "provider", 3)),
        chunk_index=int(_row_value(row, "chunk_index", 4)),
        content_markdown=str(_row_value(row, "content_markdown", 5)),
        token_count=int(_row_value(row, "token_count", 6)),
        content_hash=str(_row_value(row, "content_hash", 7)),
        embedding_model=str(_row_value(row, "embedding_model", 8)),
        embedding_state=str(_row_value(row, "embedding_state", 9)),
        embedding_revision=int(_row_value(row, "embedding_revision", 10)),
        embedding=_parse_vector(_row_value(row, "embedding", 11)),
        created_at=_as_aware_datetime(_row_value(row, "created_at", 12)),
        updated_at=_as_aware_datetime(_row_value(row, "updated_at", 13)),
    )


def _outbox_from_row(row: Any) -> OutboxJob:
    return OutboxJob(
        outbox_id=int(_row_value(row, "outbox_id", 0)),
        target_type=str(_row_value(row, "target_type", 1)),
        target_id=str(_row_value(row, "target_id", 2)),
        content_hash=str(_row_value(row, "content_hash", 3)),
        payload_text=str(_row_value(row, "payload_text", 4)),
        status=str(_row_value(row, "status", 5)),
        claimed_at=_as_optional_datetime(_row_value(row, "claimed_at", 6)),
        lease_until=_as_optional_datetime(_row_value(row, "lease_until", 7)),
        worker_id=_optional_str(_row_value(row, "worker_id", 8)),
        retry_count=int(_row_value(row, "retry_count", 9)),
        last_error=_optional_str(_row_value(row, "last_error", 10)),
        created_at=_as_aware_datetime(_row_value(row, "created_at", 11)),
        updated_at=_as_aware_datetime(_row_value(row, "updated_at", 12)),
    )


def _graph_outbox_from_row(row: Any) -> GraphOutboxJob:
    payload = _row_value(row, "episode_payload", 5)
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    elif not isinstance(payload, dict):
        payload = dict(payload) if payload else {}
    return GraphOutboxJob(
        projection_id=int(_row_value(row, "projection_id", 0)),
        source_type=str(_row_value(row, "source_type", 1)),
        source_id=str(_row_value(row, "source_id", 2)),
        source_revision=str(_row_value(row, "source_revision", 3)),
        content_hash=str(_row_value(row, "content_hash", 4)),
        episode_payload=payload,
        status=str(_row_value(row, "status", 6)),
        claimed_at=_as_optional_datetime(_row_value(row, "claimed_at", 7)),
        lease_until=_as_optional_datetime(_row_value(row, "lease_until", 8)),
        worker_id=_optional_str(_row_value(row, "worker_id", 9)),
        retry_count=int(_row_value(row, "retry_count", 10)),
        last_error=_optional_str(_row_value(row, "last_error", 11)),
        created_at=_as_aware_datetime(_row_value(row, "created_at", 12)),
        updated_at=_as_aware_datetime(_row_value(row, "updated_at", 13)),
    )


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _as_aware_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    parsed = _parse_as_of(value)
    if parsed is None:
        raise ValueError("PostgreSQL timestamp is missing")
    return parsed


def _as_optional_datetime(value: Any) -> datetime | None:
    return None if value is None else _as_aware_datetime(value)


def _parse_as_of(value: datetime | date | str | Any | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("as_of must be a valid ISO-8601 timestamp") from exc
    else:
        raise ValueError("as_of must be a valid ISO-8601 timestamp")
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for raw in str(version).split("."):
        digits = "".join(char for char in raw if char.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts or [0])


__all__ = [
    "MemoryCard",
    "MemoryEdge",
    "SessionChunk",
    "OutboxJob",
    "PgVectorStore",
    "compute_cosine_similarity",
    "make_dummy_vector",
]
