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
import re
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from ..local_embedding.canonical import card_input_v1, compute_input_hash
from ..model_connectors import (
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_PROFILE_ID,
    LOCAL_EMBEDDING_DIM,
    LOCAL_EMBEDDING_MODEL,
    LOCAL_EMBEDDING_PROFILE_ID,
    resolve_embedding_profile_id,
)

# Gemini Embedding 2의 full output을 보존한다. 일반 ``vector`` 타입은 2,000
# 차원 제한이 있으므로 DDL과 SQL cast는 ``halfvec(3072)``를 사용한다.
VECTOR_DIMENSION = DEFAULT_EMBEDDING_DIM
LOCAL_VECTOR_DIMENSION = LOCAL_EMBEDDING_DIM
OUTBOX_RETRY_LIMIT = 5
_TRANSIENT_LOCAL_OUTBOX_ERRORS = frozenset(
    {
        "model_unavailable",
        "capacity_exceeded",
        "TimeoutError",
        "ConnectionError",
        "LocalEmbeddingError:model_unavailable",
        "LocalEmbeddingError:capacity_exceeded",
        "transport_unavailable",
        "sdk_transport_error",
        "http_429",
        "http_503",
    }
)


def compute_identity_lock_id(identity_parts: Sequence[str]) -> int:
    """Compute a deterministic signed int64 advisory lock key from canonical JSON array."""

    parts: list[str] = []
    for item in identity_parts:
        value = str(item if item is not None else "")
        if "\x00" in value:
            raise ValueError("identity lock component must not contain NUL")
        parts.append(value)
    canonical = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def card_identity_lock_id(memory_id: str) -> int:
    return compute_identity_lock_id(["card", str(memory_id or "")])


def session_identity_lock_id(
    project: str,
    provider: str,
    session_id_hash: str,
    content_hash: str,
) -> int:
    return compute_identity_lock_id(
        ["session", str(project or ""), str(provider or ""), str(session_id_hash or ""), str(content_hash or "")]
    )


def target_advisory_lock_id(target_type: str, target_id: str) -> int:
    return compute_identity_lock_id([str(target_type or ""), str(target_id or "")])


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
    freshness_state: str = "unchecked"
    freshness_checked_at: datetime | None = None
    freshness_next_check_after: datetime | None = None
    source_hash: str | None = None
    observed_at: datetime | None = None


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
    observed_at: datetime | None = None


@dataclass(frozen=True)
class SessionChunkMeta:
    chunk_id: str
    session_id_hash: str
    project: str
    provider: str
    content_hash: str
    content_bytes: int
    created_at: datetime | None
    updated_at: datetime | None
    observed_at: datetime | None


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


@dataclass
class LocalOutboxJob:
    outbox_id: int
    target_type: str
    target_id: str
    profile_id: str
    source_content_hash: str
    input_hash: str
    payload_text: str
    incarnation: int = 1
    status: str = "queued"
    fencing_token: int = 0
    worker_id: str | None = None
    claimed_at: datetime | None = None
    lease_until: datetime | None = None
    retry_count: int = 0
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def embedding_profile(self) -> str:
        return self.profile_id

    @property
    def content_hash(self) -> str:
        return self.source_content_hash

    @property
    def lease_epoch(self) -> int:
        return self.fencing_token

    @property
    def target_incarnation(self) -> int:
        return self.incarnation


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
        embedding_profile: str | None = None,
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
        self.embedding_profile = str(
            embedding_profile if embedding_profile is not None else resolve_embedding_profile_id()
        )

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
            if self.embedding_profile == LOCAL_EMBEDDING_PROFILE_ID:
                self._acquire_local_locks_on(
                    db,
                    identity_parts_list=[["card", card.memory_id]],
                    target_type="memory_card",
                    target_id=card.memory_id,
                )
            if not self._upsert_card_on(db, card):
                # The SQL conflict predicate deliberately refuses to mutate an
                # accepted authority row. Raising here also prevents an
                # outbox row from being created for a proposal that did not
                # become the stored card.
                raise ValueError("cannot overwrite an accepted memory card")
            if self.embedding_profile == LOCAL_EMBEDDING_PROFILE_ID:
                payload_text = card_input_v1(
                    summary=card.summary,
                    title=card.title,
                    typed_payload=card.typed_payload,
                )
                self._enqueue_local_outbox_on(
                    db,
                    target_type="memory_card",
                    target_id=card.memory_id,
                    source_content_hash=card.content_hash,
                    payload_text=payload_text,
                    profile_id=LOCAL_EMBEDDING_PROFILE_ID,
                    acquire_locks=False,
                )
            elif card.embedding_state == "pending" or card.embedding is None:
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
                "memory_id": card.memory_id,
                "project": card.project,
                "card_type": card.card_type,
                "title": card.title,
                "summary": card.summary,
                "typed_payload": card.typed_payload,
                "lifecycle_state": card.lifecycle_state,
                "currentness": card.currentness,
                "valid_from": card.valid_from.isoformat() if getattr(card, "valid_from", None) else "",
                "valid_to": (card.valid_to.isoformat() if getattr(card, "valid_to", None) else ""),
                "observed_at": str((card.typed_payload or {}).get("observed_at") or card.created_at.isoformat()),
                "created_at": card.created_at.isoformat() if getattr(card, "created_at", None) else "",
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

    @staticmethod
    def _lock_steward_authority_on(db: Any) -> None:
        """Fence short authority writes before any decision/card lock or read.

        All steward entry points use table -> global advisory -> narrow locks.
        SHARE ROW EXCLUSIVE also fences generic/direct SQL card writers, whose
        automatic ROW EXCLUSIVE lock conflicts with it. Ordinary readers are
        not blocked. Keep callbacks bounded: this serializes authority writers
        globally for the duration of the transaction, not just one scope.
        Local timeouts cap waits/statements at 1s/5s without lengthening a
        caller's shorter existing limits (zero means unlimited in PostgreSQL).
        """
        with db.cursor() as cur:
            cur.execute("""SELECT set_config('lock_timeout',
                (SELECT (CASE WHEN setting::bigint = 0 THEN %s ELSE LEAST(setting::bigint, %s) END)::text || 'ms'
                   FROM pg_settings WHERE name = 'lock_timeout'), true),
                set_config('statement_timeout',
                (SELECT (CASE WHEN setting::bigint = 0 THEN %s ELSE LEAST(setting::bigint, %s) END)::text || 'ms'
                   FROM pg_settings WHERE name = 'statement_timeout'), true)""",
                (1000, 1000, 5000, 5000))
            cur.execute("LOCK TABLE memory_cards IN SHARE ROW EXCLUSIVE MODE")
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        ('lbrain-steward-authority-write-v1',))

    def put_steward_proposal(self, card: Mapping[str, Any]) -> dict[str, Any]:
        """Persist a non-authoritative steward envelope; never overwrite a committed card."""
        from ..session_memory.llm_brain_service import _build_pg_card

        envelope = dict(card)
        from ..session_memory.memory_card import validate_memory_card_envelope
        validate_memory_card_envelope(envelope)
        if envelope.get('lifecycle_state') not in ('candidate', 'needs_review', 'suggested_accept') or envelope.get('authorization_status') != 'disabled':
            raise ValueError('steward proposal must be pending and disabled')
        pg_card = _build_pg_card(envelope, active=False)
        with self.transaction() as db:
            self._lock_steward_authority_on(db)
            with db.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (pg_card.memory_id,))
                cur.execute("SELECT steward_envelope, content_hash, lifecycle_state FROM memory_cards WHERE memory_id = %s FOR UPDATE", (pg_card.memory_id,))
                row = cur.fetchone()
                if row is not None:
                    if (_row_value(row, "steward_envelope", 0) is None or
                        _row_value(row, "content_hash", 1) != pg_card.content_hash):
                        raise ValueError("proposal identity collision")
                    stored = _json_value(_row_value(row, "steward_envelope", 0))
                    if any(stored.get(field) != envelope.get(field) for field in ("project", "brain_id", "scope")):
                        raise ValueError("steward proposal scope collision")
                    kind = str(envelope.get("steward_proposal_kind") or "")
                    committed = _row_value(row, "lifecycle_state", 2) not in ("candidate", "needs_review", "suggested_accept")
                    if committed and kind not in ("candidate", "needs_review"):
                        raise ValueError("proposal identity collision or already committed")
                    identity_fields = ("card_type", "source_refs", "span_refs", "provider", "observed_at")
                    if kind != "stale":
                        # Stale claims use (target, reason) identity and stamp a
                        # fresh observation time on each retry. Candidate identity
                        # instead binds the immutable extraction payload.
                        identity_fields += ("typed_payload", "candidate_id", "idempotency_key")
                    if any(stored.get(field) != envelope.get(field) for field in identity_fields):
                        raise ValueError("steward candidate identity collision")
                    if (stored.get('steward_proposal_kind') != envelope.get('steward_proposal_kind') or
                        stored.get('steward_target_memory_id') != envelope.get('steward_target_memory_id')):
                        raise ValueError('conflicting steward proposal')
                    # Safe replay is an exact immutable candidate match, not a
                    # proposal update. A concurrent human decision remains intact.
                    return stored
            # A proposal is not approved. Do not enqueue embedding/graph work
            # before the restricted decision commits the accepted authority.
            self.insert_card(pg_card, conn=db)
            with db.cursor() as cur:
                cur.execute("UPDATE memory_cards SET steward_envelope = %s::jsonb WHERE memory_id = %s", (_json_text(envelope), pg_card.memory_id))
        return envelope

    def get_steward_card(self, memory_id: str, *, conn: Any | None = None) -> dict[str, Any] | None:
        with self._scope(conn=conn) as db:
            with db.cursor() as cur:
                cur.execute("SELECT steward_envelope FROM memory_cards WHERE memory_id = %s AND steward_envelope IS NOT NULL", (memory_id,))
                row = cur.fetchone()
        return _json_value(_row_value(row, "steward_envelope", 0)) if row else None

    def list_steward_cards(self, *, project: str = "", review_only: bool = False, accepted_only: bool = False, current_only: bool = False, limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= int(limit) <= 100:
            raise ValueError("bounded steward limit required")
        clauses = ["steward_envelope IS NOT NULL"]
        params: list[Any] = []
        if project:
            clauses.append("project = %s")
            params.append(project)
        if review_only:
            clauses.append("lifecycle_state IN ('candidate', 'needs_review', 'suggested_accept')")
        if accepted_only:
            clauses.extend(("lifecycle_state IN ('accepted', 'human_accepted', 'auto_accepted')", "steward_envelope->>'approval_state' IN ('approved', 'auto_accepted')", "authorization_status = 'active'", "valid_from <= NOW()", "(valid_to IS NULL OR valid_to > NOW())"))
        if current_only:
            clauses.append("currentness = 'current'")
        if accepted_only:
            clauses.append("coalesce(steward_envelope->>'steward_proposal_kind', '') <> 'stale'")
            clauses.append(_STATUS_EXPIRY_FILTER)
            params.append(datetime.now(timezone.utc))
        with self._scope() as db:
            with db.cursor() as cur:
                cur.execute(f"SELECT steward_envelope FROM memory_cards WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, memory_id LIMIT %s", (*params, int(limit)))
                return [_json_value(_row_value(row, "steward_envelope", 0)) for row in cur.fetchall()]

    def list_steward_current_context(self, project: str = "") -> list[dict[str, Any]]:
        """Read the complete live steward context in one statement, without a cap."""
        clauses = [
            "steward_envelope IS NOT NULL",
            "lifecycle_state IN ('accepted', 'human_accepted', 'auto_accepted')",
            "steward_envelope->>'approval_state' IN ('approved', 'auto_accepted')",
            "authorization_status = 'active'",
            "currentness = 'current'",
            "valid_from <= statement_timestamp()",
            "(valid_to IS NULL OR valid_to > statement_timestamp())",
            "coalesce(steward_envelope->>'steward_proposal_kind', '') <> 'stale'",
            _STATUS_EXPIRY_FILTER.replace("%s", "statement_timestamp()"),
        ]
        params: list[Any] = []
        if project:
            clauses.append("project = %s")
            params.append(project)
        with self._scope() as db:
            with db.cursor() as cur:
                cur.execute(
                    f"SELECT steward_envelope FROM memory_cards WHERE {' AND '.join(clauses)} ORDER BY memory_id",
                    params,
                )
                return [_json_value(_row_value(row, "steward_envelope", 0)) for row in cur.fetchall()]

    def read_steward_bundle_state(
        self, *, project: str, brain_id: str, scope: str,
        member_ids: list[str] | tuple[str, ...], conn: Any | None = None,
    ) -> dict[str, Any]:
        """Read members and the complete scoped current set in one SQL snapshot.

        Accepted/current rows deliberately include disabled and out-of-window
        rows. Their later reactivation must invalidate a signed state digest.
        These internal records are not a public authority/recall response.
        """
        ids = _validate_steward_bundle_scope(project, brain_id, scope, member_ids)
        with self._scope(conn=conn) as db:
            with db.cursor() as cur:
                cur.execute(
                    f"""SELECT steward_envelope, {', '.join(_STEWARD_BUNDLE_SQL_FIELDS)}
                        FROM memory_cards
                        WHERE memory_id = ANY(%s)
                           OR (project = %s AND steward_envelope IS NOT NULL
                               AND steward_envelope->>'brain_id' = %s
                               AND steward_envelope->>'scope' = %s
                               AND lifecycle_state IN ('accepted', 'human_accepted', 'auto_accepted')
                               AND currentness = 'current')
                        ORDER BY memory_id""",
                    (ids, project, brain_id, scope),
                )
                records = [_steward_bundle_record(row) for row in cur.fetchall()]
        for record in records:
            card, sql = record['card'], record['sql']
            if not isinstance(card, dict) or card.get('memory_id') != sql['memory_id']:
                raise ValueError('unknown steward member or envelope identity mismatch')
            if (sql['project'] != project or card.get('project') != project
                    or card.get('brain_id') != brain_id or card.get('scope') != scope):
                raise ValueError('steward bundle member scope mismatch')
        requested_ids = set(ids)
        if requested_ids - {record['sql']['memory_id'] for record in records}:
            raise ValueError('unknown steward bundle member')
        records.sort(key=lambda record: record['sql']['memory_id'])
        return {
            'members': [record for record in records if record['sql']['memory_id'] in requested_ids],
            'current': [record for record in records
                        if record['sql']['lifecycle_state'] in ('accepted', 'human_accepted', 'auto_accepted')
                        and record['sql']['currentness'] == 'current'],
        }

    def list_steward_project_counts(self) -> list[tuple[str, int]]:
        with self._scope() as db:
            with db.cursor() as cur:
                cur.execute("""SELECT project, count(*) AS n FROM memory_cards
                    WHERE steward_envelope IS NOT NULL AND authorization_status = 'active'
                      AND lifecycle_state IN ('accepted', 'human_accepted', 'auto_accepted')
                    GROUP BY project ORDER BY project""")
                return [(str(_row_value(row, 'project', 0)), int(_row_value(row, 'n', 1)))
                        for row in cur.fetchall()]

    def get_temporal_coverage_metrics(self, project: str) -> dict[str, Any]:
        """Calculate temporal metadata coverage and missing/unknown ratios for a project (FR-AGE-064)."""
        with self._scope() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        count(*) AS total_items,
                        count(observed_at) AS with_observed_at
                    FROM memory_cards
                    WHERE project = %s
                      AND authorization_status = 'active'
                    """,
                    (project,),
                )
                row = cur.fetchone()
                total = int(_row_value(row, "total_items", 0) or 0) if row else 0
                with_obs = int(_row_value(row, "with_observed_at", 1) or 0) if row else 0
                missing = total - with_obs

        unknown_ratio = round(missing / total, 4) if total > 0 else 0.0
        coverage_ratio = round(with_obs / total, 4) if total > 0 else 0.0

        return {
            "project": project,
            "total_items": total,
            "with_observed_at": with_obs,
            "missing_observed_at": missing,
            "unknown_ratio": unknown_ratio,
            "coverage_ratio": coverage_ratio,
        }

    def get_steward_decision(self, decision_id: str) -> dict[str, Any] | None:
        with self._scope() as db:
            with db.cursor() as cur:
                cur.execute("SELECT decision_id, memory_id, content_hash, action, actor, target_memory_id, result_card, result_target FROM steward_card_decisions WHERE decision_id = %s", (decision_id,))
                row = cur.fetchone()
                if row is None:
                    return None
                return {name: _row_value(row, name, index) for index, name in enumerate(
                    ('decision_id', 'memory_id', 'content_hash', 'action', 'actor', 'target_memory_id', 'result_card', 'result_target'))}

    def steward_bundle_decision(
        self, *, decision_id: str, request_fingerprint: str, project: str,
        brain_id: str, scope: str, member_ids: list[str] | tuple[str, ...], decide: Any,
    ) -> dict[str, Any]:
        """Persist bounded child authority decisions and their full safe response atomically.

        The trusted callback re-reads and validates the signed state and all
        transitions using the scoped store. No model/network work belongs in
        this short globally serialized transaction.

        A caller-owned connection is refused at this outer entry point: its
        transaction() cannot guarantee rollback if the caller catches errors.
        Only the callback's child store uses an injected connection.
        """
        if self.connection is not None:
            raise ValueError('steward bundle decision requires a store-owned outer transaction')
        if (any(not isinstance(value, str) or not value for value in (decision_id, request_fingerprint))
                or not callable(decide)):
            raise ValueError('steward bundle decision identity and callback required')
        ids = sorted(_validate_steward_bundle_scope(project, brain_id, scope, member_ids))
        if not ids:
            raise ValueError('steward bundle decision members required')
        with self.transaction() as db:
            # An owned connection may inherit REPEATABLE READ from its DSN.
            # Set this before any SELECT so reads after a waited-for lock see
            # all writes committed by the preceding authority writer.
            with db.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            self._lock_steward_authority_on(db)
            with db.cursor() as cur:
                cur.execute("""SELECT request_fingerprint, project, brain_id, scope, member_ids, result
                    FROM steward_bundle_decisions WHERE decision_id = %s""", (decision_id,))
                previous = cur.fetchone()
                if previous is not None:
                    expected = (request_fingerprint, project, brain_id, scope)
                    stored = tuple(_row_value(previous, name, index) for index, name in enumerate(
                        ('request_fingerprint', 'project', 'brain_id', 'scope')))
                    if stored != expected or _json_value(_row_value(previous, 'member_ids', 4)) != ids:
                        raise ValueError('conflicting steward bundle decision id')
                    # Retry returns the decision-time response before touching
                    # members, expiry, fingerprints of current rows, or callback.
                    raw_result = _row_value(previous, 'result', 5)
                    stored_result = _json_value(raw_result)
                    if raw_result is None or not isinstance(stored_result, dict):
                        raise ValueError('steward bundle decision result unavailable for safe retry')
                    return stored_result
            scoped_store = PgVectorStore(connection=db, pgvector_version=self.pgvector_version)
            result = decide(scoped_store)
            if not isinstance(result, dict):
                raise ValueError('steward bundle decision result must be a JSON object')
            try:
                result_json = json.dumps(result, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError('steward bundle decision result must be serializable JSON') from exc
            with db.cursor() as cur:
                cur.execute("""INSERT INTO steward_bundle_decisions
                    (decision_id, request_fingerprint, project, brain_id, scope, member_ids, result)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                    RETURNING result""",
                    (decision_id, request_fingerprint, project, brain_id, scope,
                     _json_text(ids), result_json))
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError('steward bundle audit insert returned no result')
                return _json_value(_row_value(row, 'result', 0))

    def steward_decision(self, *, decision_id: str, memory_id: str, action: str, actor: str,
                         content_hash: str, target_memory_id: str = "", transform: Any) -> dict[str, Any]:
        """Lock, validate and change steward rows, audit and outboxes in one PG transaction."""
        if not decision_id or not actor or action not in {'approve', 'auto_accept', 'reject', 'supersede', 'stale'}:
            raise ValueError("steward decision identity required")
        with self.transaction() as db:
            self._lock_steward_authority_on(db)
            with db.cursor() as cur:
                # Serialize same-decision retries before checking the audit row.
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (decision_id,))
                cur.execute("SELECT decision_id, memory_id, content_hash, action, actor, target_memory_id, result_card, result_target FROM steward_card_decisions WHERE decision_id = %s", (decision_id,))
                previous = cur.fetchone()
                if previous:
                    values = tuple(_row_value(previous, name, i) for i, name in enumerate(('memory_id', 'content_hash', 'action', 'actor', 'target_memory_id'), 1))
                    if values != (memory_id, content_hash, action, actor, target_memory_id or None):
                        raise ValueError("conflicting steward decision id")
                    # An idempotent retry must return the decision-time result,
                    # not a card that may have since become stale or superseded.
                    stored_card = _row_value(previous, 'result_card', 6)
                    if stored_card is None:
                        raise ValueError('steward decision result unavailable for safe retry')
                    stored_target = _row_value(previous, 'result_target', 7)
                    if action == 'supersede':
                        _require_same_supersede_scope(_json_value(stored_card), _json_value(stored_target))
                    return {'card': _json_value(stored_card),
                            'target': _json_value(stored_target) if stored_target is not None else None}
                # Consistent lock ordering across competing decisions.
                ids = sorted({memory_id, *([target_memory_id] if target_memory_id else [])})
                for card_id in ids:
                    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (card_id,))
                if self.embedding_profile == LOCAL_EMBEDDING_PROFILE_ID:
                    for card_id in ids:
                        identity_parts = self._resolve_target_identity_parts_on(
                            db,
                            target_type="memory_card",
                            target_id=card_id,
                        )
                        self._acquire_local_locks_on(
                            db,
                            identity_parts_list=[identity_parts],
                            target_type="memory_card",
                            target_id=card_id,
                        )
                cur.execute("SELECT memory_id, steward_envelope, content_hash FROM memory_cards WHERE memory_id = ANY(%s) ORDER BY memory_id FOR UPDATE", (ids,))
                rows = {str(_row_value(row, 'memory_id', 0)): row for row in cur.fetchall()}
                if len(rows) != len(ids) or any(_row_value(row, 'steward_envelope', 1) is None for row in rows.values()):
                    raise ValueError("unknown steward card or target")
                if _row_value(rows[memory_id], 'content_hash', 2) != content_hash:
                    raise ValueError("steward card content hash changed")
                card = _json_value(_row_value(rows[memory_id], 'steward_envelope', 1))
                target = _json_value(_row_value(rows[target_memory_id], 'steward_envelope', 1)) if target_memory_id else None
                if action == 'supersede':
                    _require_same_supersede_scope(card, target)
                changed_card, changed_target = transform(card, target)
                # Use one DB timestamp inside the decision transaction for every
                # formerly-current card. The envelope and SQL validity must agree.
                cur.execute("SELECT clock_timestamp()")
                committed_at = _row_value(cur.fetchone(), 'clock_timestamp', 0)
                for changed in (changed_card, changed_target):
                    if changed is None:
                        continue
                    original = card if changed['memory_id'] == memory_id else target
                    if changed.get('currentness') in ('superseded', 'stale'):
                        if original is not None and original.get('currentness') == 'current':
                            changed['valid_to'] = committed_at.isoformat()
                    elif (changed.get('approval_state') in ('approved', 'auto_accepted')
                          and original is not None and original.get('approval_state') not in ('approved', 'auto_accepted')
                          and changed.get('steward_proposal_kind') != 'stale'):
                        # A pending proposal had no authority before this decision.
                        changed['valid_from'] = committed_at.isoformat()
                for changed in (changed_card, changed_target):
                    if changed is None:
                        continue
                    card_id = str(changed['memory_id'])
                    if changed['content_hash'] != _row_value(rows[card_id], 'content_hash', 2):
                        raise ValueError("steward content hash cannot change during transition")
                    # Reuse the existing SQL authorization rule for the envelope
                    # and immutable audit snapshot as well. This changes only
                    # their representation, not authority/recall eligibility.
                    changed['authorization_status'] = (
                        'active' if changed.get('approval_state') in ('approved', 'auto_accepted') else 'disabled'
                    )
                    cur.execute("""UPDATE memory_cards SET steward_envelope = %s::jsonb, lifecycle_state = %s,
                        authorization_status = %s, currentness = %s, valid_from = COALESCE(%s::timestamptz, valid_from), valid_to = %s, updated_at = NOW()
                        WHERE memory_id = %s""", (_json_text(changed), changed['lifecycle_state'],
                        changed['authorization_status'],
                        changed.get('currentness') or 'unknown', changed.get('valid_from'), changed.get('valid_to'), card_id))
                    # Graph projection must reflect both approvals and demotions
                    # (stale/superseded/retired). Enqueue for authority cards, not
                    # for proposals being committed (those carry a proposal kind).
                    if (changed.get('approval_state') in ('approved', 'auto_accepted')
                            and str(changed.get('steward_proposal_kind') or '') != 'stale'):
                        changed_currentness = str(changed.get('currentness') or 'unknown')
                        # Re-embed only when the card is (or becomes) current and
                        # is not a stale proposal; demotions keep their embedding.
                        if (changed_currentness == 'current'
                                and changed.get('steward_proposal_kind') != 'stale'):
                            if self.embedding_profile == LOCAL_EMBEDDING_PROFILE_ID:
                                local_payload_text = card_input_v1(
                                    summary=changed.get("summary"),
                                    title=changed.get("title"),
                                    typed_payload=changed.get("typed_payload"),
                                )
                                self._enqueue_local_outbox_on(
                                    db,
                                    target_type="memory_card",
                                    target_id=card_id,
                                    source_content_hash=str(changed["content_hash"]),
                                    payload_text=local_payload_text,
                                    profile_id=LOCAL_EMBEDDING_PROFILE_ID,
                                    acquire_locks=False,
                                )
                            else:
                                self._enqueue_outbox_on(db, target_type='memory_card', target_id=card_id,
                                                        content_hash=changed['content_hash'], payload_text=str(changed.get('summary') or ''))
                        revision = f"{changed['content_hash']}:{changed['lifecycle_state']}:{changed_currentness}"
                        self._enqueue_graph_outbox_on(db, source_type='memory_card', source_id=card_id,
                            source_revision=revision,
                            content_hash=changed['content_hash'], episode_payload={
                                'source_type': 'memory_card', 'source_id': card_id, 'source_revision': revision,
                                'content_hash': changed['content_hash'], 'authority_memory_id': card_id,
                                'project': changed['project'], 'card_type': changed['card_type'],
                                'title': changed['title'], 'summary': changed['summary'],
                                'typed_payload': changed.get('typed_payload') or {},
                                'lifecycle_state': changed['lifecycle_state'],
                                'currentness': changed_currentness,
                                'valid_from': str(changed.get('valid_from') or ''),
                                'valid_to': str(changed.get('valid_to') or ''),
                                'observed_at': str((changed.get('typed_payload') or {}).get('observed_at') or changed.get('valid_from') or ''),
                                'created_at': str(changed.get('created_at') or ''),
                            })
                cur.execute("""INSERT INTO steward_card_decisions
                    (decision_id, memory_id, content_hash, action, actor, target_memory_id, result_card, result_target)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)""",
                    (decision_id, memory_id, content_hash, action, actor, target_memory_id or None,
                     _json_text(changed_card), _json_text(changed_target) if changed_target is not None else None))
                return {'card': changed_card, 'target': changed_target}

    def get_card(self, memory_id: str, conn: Any | None = None) -> MemoryCard | None:
        """Read one card back from PostgreSQL."""

        with self._scope(conn=conn) as db:
            with db.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {_CARD_COLUMNS}
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
                if self.embedding_profile == LOCAL_EMBEDDING_PROFILE_ID:
                    self._delete_local_target_on(
                        db,
                        target_type="memory_card",
                        target_id=memory_id,
                        identity_parts_list=[["card", memory_id]],
                        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
                    )
                else:
                    try:
                        with db.cursor() as cur:
                            cur.execute("SAVEPOINT delete_card_local_tombstone")
                        self._delete_local_target_on(
                            db,
                            target_type="memory_card",
                            target_id=memory_id,
                            identity_parts_list=[["card", memory_id]],
                            profile_id=LOCAL_EMBEDDING_PROFILE_ID,
                        )
                        with db.cursor() as cur:
                            cur.execute("RELEASE SAVEPOINT delete_card_local_tombstone")
                    except Exception:
                        try:
                            with db.cursor() as cur:
                                cur.execute("ROLLBACK TO SAVEPOINT delete_card_local_tombstone")
                                cur.execute("RELEASE SAVEPOINT delete_card_local_tombstone")
                        except Exception:
                            pass
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

    def insert_chunk(self, chunk: SessionChunk, conn: Any | None = None, *, insert_only: bool = False) -> str:
        """Insert/enqueue atomically; insert_only refuses any existing ID."""

        normalized = self._normalize_chunk(chunk)
        conflict_clause = "" if insert_only else """
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
                updated_at = EXCLUDED.updated_at,
                observed_at = COALESCE(EXCLUDED.observed_at, session_memory_chunks.observed_at)
        """
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
            normalized.observed_at,
        )
        with self._scope(conn=conn, write=conn is None and self.connection is None) as db:
            with db.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO session_memory_chunks (
                        chunk_id, session_id_hash, project, provider, chunk_index,
                        content_markdown, token_count, content_hash, embedding_model,
                        embedding_state, embedding_revision, embedding, created_at, updated_at,
                        observed_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s::halfvec, %s, %s, %s
                    )
                    {conflict_clause}
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

    def chunk_embedding_equals(
        self, chunk_id: str, source_vector: list[float] | tuple[float, ...], conn: Any | None = None
    ) -> bool:
        """Compare one stored embedding to PostgreSQL's halfvec cast of source_vector."""

        literal = _vector_literal(source_vector)
        if literal is None:
            return False
        with self._scope(conn=conn) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT embedding = %s::halfvec AS embedding_equal
                      FROM session_memory_chunks
                     WHERE chunk_id = %s
                    """,
                    (literal, chunk_id),
                )
                row = cur.fetchone()
        return bool(row is not None and _row_value(row, "embedding_equal", 0) is True)

    def get_chunk(self, chunk_id: str, conn: Any | None = None) -> SessionChunk | None:
        """Read one session chunk from PostgreSQL."""

        with self._scope(conn=conn) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT chunk_id, session_id_hash, project, provider, chunk_index,
                           content_markdown, token_count, content_hash, embedding_model,
                           embedding_state, embedding_revision, embedding,
                           created_at, updated_at, observed_at
                      FROM session_memory_chunks
                     WHERE chunk_id = %s
                    """,
                    (chunk_id,),
                )
                row = cur.fetchone()
        return _chunk_from_row(row) if row is not None else None

    def get_session_chunk_by_id_prefix(
        self,
        *,
        project: str,
        chunk_id_prefix: str,
        conn: Any | None = None,
    ) -> SessionChunk | None:
        """프로젝트 범위에서 ID prefix로 유일한 session chunk를 조회한다."""

        escaped_prefix = (
            chunk_id_prefix.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        with self._scope(conn=conn) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT chunk_id, session_id_hash, project, provider, chunk_index,
                           content_markdown, token_count, content_hash, embedding_model,
                           embedding_state, embedding_revision, embedding,
                           created_at, updated_at, observed_at
                      FROM session_memory_chunks
                   WHERE project = %s AND chunk_id LIKE %s ESCAPE '\\'
                   ORDER BY chunk_id
                   LIMIT 2
                  """,
                    (project, f"{escaped_prefix}%"),
                )
                rows = cur.fetchall()
        if len(rows) != 1:
            return None
        return _chunk_from_row(rows[0])

    def iter_session_chunk_meta_batches(
        self,
        *,
        batch_size: int = 100,
        limit_sessions: int = 20_000,
        conn: Any | None = None,
    ) -> Iterator[tuple[str, tuple[SessionChunkMeta, ...]]]:
        """본문과 벡터 없이 세션 경계를 유지하며 메타데이터를 읽는다."""

        if isinstance(batch_size, bool) or batch_size < 1:
            raise ValueError("batch_size must be positive")
        if isinstance(limit_sessions, bool) or limit_sessions < 1:
            raise ValueError("limit_sessions must be positive")
        last_session_id: str | None = None
        sessions_yielded = 0
        with self._scope(conn=conn) as db:
            if conn is None and self.connection is None:
                with db.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
            while sessions_yielded < limit_sessions:
                current_batch_size = min(
                    batch_size,
                    limit_sessions - sessions_yielded,
                )
                with db.cursor() as cur:
                    if last_session_id is None:
                        cur.execute(
                            """
                            SELECT DISTINCT session_id_hash
                              FROM session_memory_chunks
                             ORDER BY session_id_hash
                             LIMIT %s
                            """,
                            (current_batch_size,),
                        )
                    else:
                        cur.execute(
                            """
                            SELECT DISTINCT session_id_hash
                              FROM session_memory_chunks
                             WHERE session_id_hash > %s
                             ORDER BY session_id_hash
                             LIMIT %s
                            """,
                            (last_session_id, current_batch_size),
                        )
                    session_rows = cur.fetchall()
                session_ids = [
                    str(_row_value(row, "session_id_hash", 0) or "")
                    for row in session_rows
                ]
                if not session_ids:
                    break
                chunks_by_session: dict[str, list[SessionChunkMeta]] = {
                    session_id: [] for session_id in session_ids
                }
                with db.cursor() as cur:
                    cur.execute(
                        """
                        SELECT chunk_id, session_id_hash, project, provider, content_hash,
                               octet_length(content_markdown) AS content_bytes,
                               created_at, updated_at, observed_at
                          FROM session_memory_chunks
                         WHERE session_id_hash = ANY(%s)
                         ORDER BY session_id_hash, chunk_id
                        """,
                        (session_ids,),
                    )
                    rows = cur.fetchall()
                for row in rows:
                    chunk = SessionChunkMeta(
                        chunk_id=str(_row_value(row, "chunk_id", 0) or ""),
                        session_id_hash=str(
                            _row_value(row, "session_id_hash", 1) or ""
                        ),
                        project=str(_row_value(row, "project", 2) or ""),
                        provider=str(_row_value(row, "provider", 3) or ""),
                        content_hash=str(_row_value(row, "content_hash", 4) or ""),
                        content_bytes=int(_row_value(row, "content_bytes", 5) or 0),
                        created_at=_as_optional_datetime(
                            _row_value(row, "created_at", 6)
                        ),
                        updated_at=_as_optional_datetime(
                            _row_value(row, "updated_at", 7)
                        ),
                        observed_at=_as_optional_datetime(
                            _row_value(row, "observed_at", 8)
                        ),
                    )
                    chunks_by_session.setdefault(chunk.session_id_hash, []).append(chunk)
                for session_id in session_ids:
                    yield session_id, tuple(chunks_by_session[session_id])
                sessions_yielded += len(session_ids)
                last_session_id = session_ids[-1]

    def find_ready_chunk_by_identity(
        self,
        *,
        session_id_hash: str,
        project: str,
        provider: str,
        content_hash: str,
        embedding_model: str,
        conn: Any | None = None,
    ) -> SessionChunk | None:
        """Return the unique ready chunk for this identity, independent of chunk_id.

        Legacy rows used shorter ids than the scoped 64-char projector. Reuse
        must not re-embed when the same session/content/model is already ready.
        Ambiguous matches fail closed.
        """

        with self._scope(conn=conn) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT chunk_id, session_id_hash, project, provider, chunk_index,
                           content_markdown, token_count, content_hash, embedding_model,
                           embedding_state, embedding_revision, embedding,
                           created_at, updated_at, observed_at
                      FROM session_memory_chunks
                     WHERE session_id_hash = %s
                       AND project = %s
                       AND provider = %s
                       AND content_hash = %s
                       AND embedding_model = %s
                       AND embedding_state = 'ready'
                       AND embedding IS NOT NULL
                     ORDER BY chunk_id
                     LIMIT 2
                    """,
                    (session_id_hash, project, provider, content_hash, embedding_model),
                )
                rows = cur.fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise ValueError("PG ready chunk identity is not unique")
        return _chunk_from_row(rows[0])

    def search_session_chunks(
        self,
        query_vector: list[float],
        project: str | None = None,
        limit: int = 5,
        conn: Any | None = None,
        *,
        profile_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run vector similarity search over session_memory_chunks."""
        effective_profile = str(profile_id or self.embedding_profile)
        if effective_profile == LOCAL_EMBEDDING_PROFILE_ID or (
            profile_id is None
            and isinstance(query_vector, (list, tuple))
            and len(query_vector) == LOCAL_VECTOR_DIMENSION
        ):
            return self.search_session_chunks_local(
                query_vector,
                project=project,
                limit=limit,
                conn=conn,
                profile_id=LOCAL_EMBEDDING_PROFILE_ID,
            )
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
                           created_at, observed_at,
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

    def skip_stale_outbox_embedding(
        self,
        *,
        outbox_id: int,
        target_type: str,
        target_id: str,
        content_hash: str,
        worker_id: str,
    ) -> bool:
        """Retire an obsolete leased job before spending on its embedding."""

        targets = {
            "memory_card": ("memory_cards", "memory_id"),
            "session_chunk": ("session_memory_chunks", "chunk_id"),
        }
        if target_type not in targets:
            raise ValueError("unsupported outbox target type")
        if not worker_id or not outbox_id or not target_id or not content_hash:
            raise ValueError("invalid outbox stale-check arguments")
        table, id_column = targets[target_type]

        with self._scope(write=True) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT outbox_id FROM embedding_outbox
                     WHERE outbox_id = %s AND target_type = %s
                       AND target_id = %s AND content_hash = %s
                       AND status = 'processing' AND worker_id = %s
                       AND lease_until > clock_timestamp()
                     FOR UPDATE
                    """,
                    (outbox_id, target_type, target_id, content_hash, worker_id),
                )
                if cur.fetchone() is None:
                    raise ValueError("outbox job not found or lease owner mismatch")

                cur.execute(
                    f"SELECT content_hash FROM {table} WHERE {id_column} = %s FOR UPDATE",
                    (target_id,),
                )
                target = cur.fetchone()
                if target is not None and _row_value(target, "content_hash", 0) == content_hash:
                    return False

                cur.execute(
                    """
                    UPDATE embedding_outbox
                       SET status = 'cas_skipped',
                           last_error = 'CAS skip: content_hash mismatch',
                           lease_until = NULL,
                           updated_at = NOW()
                     WHERE outbox_id = %s
                       AND status = 'processing'
                       AND worker_id = %s
                       AND lease_until > clock_timestamp()
                    """,
                    (outbox_id, worker_id),
                )
                if cur.rowcount != 1:
                    raise ValueError("outbox job not found or lease owner mismatch")
                return True

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
        after_memory_id: str | None = None, steward_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Apply authority SQL; steward-only reads explicitly exclude legacy cards."""
        if not project or not 1 <= limit <= 100:
            raise ValueError("project and bounded limit required")
        clauses, params = _authority_filter(project, as_of)
        if steward_only:
            clauses.extend(("steward_envelope IS NOT NULL",
                            "steward_envelope->>'approval_state' IN ('approved', 'auto_accepted')",
                            "coalesce(steward_envelope->>'steward_proposal_kind', '') <> 'stale'"))
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
                               lifecycle_state, authorization_status, created_at,
                               valid_from, valid_to,
                               freshness_state, freshness_checked_at, freshness_next_check_after, source_hash
                          FROM memory_cards WHERE {' AND '.join(clauses)}
                         ORDER BY memory_id LIMIT %s""",
                    [*params, limit],
                )
                names = [column.name for column in cur.description]
                return [dict(row) if isinstance(row, Mapping) else dict(zip(names, row))
                        for row in cur.fetchall()]

    def list_context_cards(
        self, *, project: str, query: str = "", as_of: datetime | date | str | None = None,
        limit: int = 5, steward_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Select a balanced current-work pack in PostgreSQL before applying LIMIT."""
        if not project or not 1 <= limit <= 100:
            raise ValueError("project and bounded limit required")
        terms = _context_query_tokens(query)
        overlap_sql = """(
            SELECT count(DISTINCT query_term)
              FROM unnest(%s::text[]) AS query_terms(query_term)
             WHERE query_term = ANY(regexp_split_to_array(
                 regexp_replace(lower(coalesce(card_type, '') || ' ' || coalesce(title, '') || ' ' || coalesce(summary, '') || ' ' ||
                                coalesce(typed_payload::text, '')),
                                '[^a-z0-9가-힣_-]+', ' ', 'g'),
                 '\\s+'))
        )"""
        clauses, params = _authority_filter(project, as_of)
        instant = _parse_as_of(as_of) or datetime.now(timezone.utc)
        if steward_only:
            clauses.extend((
                "steward_envelope IS NOT NULL",
                "steward_envelope->>'approval_state' IN ('approved', 'auto_accepted')",
                "coalesce(steward_envelope->>'steward_proposal_kind', '') <> 'stale'",
            ))
            clauses.append("""(
                card_type <> 'preference'
                OR NULLIF(steward_envelope->>'active_until', '') IS NULL
                OR CASE
                    WHEN steward_envelope->>'active_until' ~
                         '^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}(:\\d{2}(\\.\\d+)?)?(Z|[+-]\\d{2}:\\d{2})$'
                    THEN (steward_envelope->>'active_until')::timestamptz > %s
                    ELSE FALSE
                   END
            )""")
            params.append(instant)
        threshold = max(1, (len(terms) + 1) // 2) if terms else 0
        query_sql = f"""
            WITH authorized AS (
                SELECT memory_id, project, card_type, title, summary, typed_payload,
                       currentness, content_hash, source_ref, lifecycle_state,
                       authorization_status, created_at, updated_at,
                       {overlap_sql} AS relevance_overlap
                  FROM memory_cards
                 WHERE {' AND '.join(clauses)}
            ), ranked AS (
                SELECT *,
                       CASE card_type
                           WHEN 'task' THEN 0 WHEN 'work' THEN 0 WHEN 'work_unit' THEN 0
                           WHEN 'decision' THEN 1 WHEN 'status' THEN 2
                           WHEN 'preference' THEN 3 ELSE 4
                       END AS context_priority,
                       row_number() OVER (
                           PARTITION BY card_type
                           ORDER BY relevance_overlap DESC, updated_at DESC, created_at DESC, memory_id
                       ) AS type_rank
                  FROM authorized
                 WHERE cardinality(%s::text[]) = 0
                    OR relevance_overlap >= %s
                    OR card_type = ANY(%s::text[])
            )
            SELECT memory_id, project, card_type, title, summary, typed_payload,
                   currentness, content_hash, source_ref, lifecycle_state,
                   authorization_status, created_at, updated_at
              FROM ranked
             WHERE (card_type IN ('task', 'work', 'work_unit', 'decision', 'status') AND type_rank <= 2)
                OR (card_type = 'preference' AND type_rank <= 3)
                OR (relevance_overlap >= %s AND type_rank <= 3)
             ORDER BY type_rank, context_priority, relevance_overlap DESC,
                      updated_at DESC, created_at DESC, memory_id
             LIMIT %s
        """
        query_params = [terms, *params, terms, threshold, _context_query_card_types(terms), threshold, limit]
        with self._scope() as db:
            with db.cursor() as cur:
                cur.execute(query_sql, query_params)
                names = [column.name for column in cur.description]
                return [dict(row) if isinstance(row, Mapping) else dict(zip(names, row))
                        for row in cur.fetchall()]

    def graph_projection_health(self, project: str, *, as_of: Any = None,
                                steward_only: bool = False) -> dict[str, Any]:
        """현재 권위 revision의 투영 누락만 측정한다. 검색 무응답은 lag 증거가 아니다."""
        clauses, params = _authority_filter(project, as_of)
        if steward_only:
            clauses.extend(("steward_envelope IS NOT NULL",
                            "steward_envelope->>'approval_state' IN ('approved', 'auto_accepted')",
                            "coalesce(steward_envelope->>'steward_proposal_kind', '') <> 'stale'"))
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

    def session_memory_freshness(self, project: str, *, conn: Any | None = None) -> dict[str, Any]:
        """프로젝트 session-memory의 PG 반영 상태. ingress/CouchDB 단계의 지연은 보지 못한다.

        pending은 어느 embedding profile로도 검색 가능 상태가 아닌 청크다
        (legacy `embedding_state <> 'ready'` 이면서 ready 상태의 local embedding 행도 없음).
        """
        pending_clause = """
            NOT (
                smc.embedding_state = 'ready'
                OR EXISTS (
                    SELECT 1 FROM session_local_embeddings sle
                     WHERE sle.chunk_id = smc.chunk_id
                       AND sle.state = 'ready'
                       AND sle.source_content_hash = smc.content_hash
                )
            )"""
        with self._scope(conn=conn) as db:
            with db.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT MAX(smc.created_at) AS last_built_at,
                           MAX(smc.observed_at) AS last_observed_at,
                           COUNT(*) AS total_chunks,
                           COUNT(*) FILTER (WHERE {pending_clause}) AS pending_chunks,
                           COUNT(DISTINCT smc.session_id_hash) FILTER (WHERE {pending_clause}) AS pending_sessions
                      FROM session_memory_chunks AS smc
                     WHERE smc.project = %s
                    """,
                    (project,),
                )
                row = cur.fetchone()
        return {
            "last_built_at": _row_value(row, "last_built_at", 0),
            "last_observed_at": _row_value(row, "last_observed_at", 1),
            "total_chunks": int(_row_value(row, "total_chunks", 2) or 0),
            "pending_chunks": int(_row_value(row, "pending_chunks", 3) or 0),
            "pending_sessions": int(_row_value(row, "pending_sessions", 4) or 0),
        }

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
        steward_only: bool = False,
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
        requested_profile = str(kwargs.get("profile_id") or self.embedding_profile)
        if requested_profile == LOCAL_EMBEDDING_PROFILE_ID or (
            "profile_id" not in kwargs
            and isinstance(actual_query_vector, (list, tuple))
            and len(actual_query_vector) == LOCAL_VECTOR_DIMENSION
        ):
            return self.hybrid_search_local(
                project=actual_project,
                query_vector=list(actual_query_vector),
                limit=limit,
                card_type=card_type,
                authorization_status=authorization_status,
                currentness=currentness,
                as_of=as_of,
                text_query=text_query,
                steward_only=steward_only,
                profile_id=LOCAL_EMBEDDING_PROFILE_ID,
            )
        _validate_vector(actual_query_vector)
        if limit < 1:
            raise ValueError("limit must be positive")
        vector_literal = _vector_literal(list(actual_query_vector))
        clauses = ["embedding IS NOT NULL", "embedding_state = 'ready'", "embedding_model = %s"]
        params: list[Any] = [DEFAULT_EMBEDDING_MODEL]
        if actual_project:
            clauses.append("project = %s")
            params.append(actual_project)
        if steward_only:
            clauses.extend(("steward_envelope IS NOT NULL",
                            "steward_envelope->>'approval_state' IN ('approved', 'auto_accepted')",
                            "coalesce(steward_envelope->>'steward_proposal_kind', '') <> 'stale'"))
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
        clauses.append(_STATUS_EXPIRY_FILTER)
        params.append(as_of_dt)
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
                           {score_expression} AS similarity_score,
                           1.0 - distance AS semantic_score,
                           valid_from, valid_to,
                           freshness_state, freshness_checked_at, freshness_next_check_after, source_hash
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
                "semantic_score": round(float(_row_value(row, "semantic_score", 9)), 6),
                # as_of 창은 SQL이 이미 적용했지만, resolver가 카드 단위로 과거/현재
                # 레인을 다시 판정하므로 유효기간을 함께 돌려줘야 한다.
                "valid_from": _row_value(row, "valid_from", 10),
                "valid_to": _row_value(row, "valid_to", 11),
                "freshness_state": str(_row_value(row, "freshness_state", 12, default="unchecked") or "unchecked"),
                "freshness_checked_at": _row_value(row, "freshness_checked_at", 13),
                "freshness_next_check_after": _row_value(row, "freshness_next_check_after", 14),
                "source_hash": _row_value(row, "source_hash", 15),
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

    def reset_graph_projection_dead_letters(
        self,
        *,
        source_ids: list[str] | None = None,
        conn: Any | None = None,
    ) -> int:
        """Reset dead-lettered graph projection jobs to failed for retry.

        Only rows currently in ``dead_letter`` are touched.  ``retry_count`` is
        reset to 0 so the worker can claim them again.  When ``source_ids`` is
        given, only those source IDs are reset.  Returns the number of rows
        updated.
        """
        params: list[Any] = []
        where = " WHERE status = 'dead_letter'"
        if source_ids:
            placeholders = ", ".join(["%s"] * len(source_ids))
            where += f" AND source_id IN ({placeholders})"
            params.extend(source_ids)
        with self._scope(conn=conn, write=True) as db:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE graph_projection_outbox"
                    + " SET status = 'failed',"
                    + "     retry_count = 0,"
                    + "     lease_until = NULL,"
                    + "     worker_id = NULL,"
                    + "     claimed_at = NULL,"
                    + "     updated_at = NOW()"
                    + where,
                    params,
                )
                return cur.rowcount

    # --- Local Model2Vec 256-dim Profile Operations (specs/model2vec-local-embedding D2/D3) ---

    @staticmethod
    def _acquire_local_locks_on(
        conn: Any,
        *,
        identity_parts_list: Sequence[Sequence[str]],
        target_type: str | None = None,
        target_id: str | None = None,
    ) -> None:
        """Acquire identity advisory lock(s) in sorted order, then target advisory lock."""

        canonical_identities = sorted(
            {
                json.dumps(list(parts), ensure_ascii=False, separators=(",", ":")): list(parts)
                for parts in identity_parts_list
            }.items(),
            key=lambda item: item[0],
        )
        with conn.cursor() as cur:
            for _canonical, parts in canonical_identities:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (compute_identity_lock_id(parts),),
                )
            if target_type and target_id:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (target_advisory_lock_id(target_type, target_id),),
                )

    def acquire_session_identity_lock(
        self,
        *,
        project: str,
        provider: str,
        session_id_hash: str,
        content_hash: str,
        conn: Any,
    ) -> int:
        lock_id = session_identity_lock_id(project, provider, session_id_hash, content_hash)
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id,))
        return lock_id

    def find_session_chunks_by_natural_scope(
        self,
        *,
        project: str,
        provider: str,
        session_id_hash: str,
        content_hash: str,
        conn: Any | None = None,
    ) -> list[SessionChunk]:
        """Return all session_memory_chunks matching the natural scope regardless of state or ID scheme."""

        with self._scope(conn=conn) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT chunk_id, session_id_hash, project, provider, chunk_index,
                           content_markdown, token_count, content_hash, embedding_model,
                           embedding_state, embedding_revision, embedding,
                           created_at, updated_at, observed_at
                      FROM session_memory_chunks
                     WHERE session_id_hash = %s
                       AND project = %s
                       AND provider = %s
                       AND content_hash = %s
                     ORDER BY chunk_id
                    """,
                    (session_id_hash, project, provider, content_hash),
                )
                rows = cur.fetchall()
        return [_chunk_from_row(row) for row in rows]

    def _resolve_target_identity_parts_on(
        self,
        conn: Any,
        *,
        target_type: str,
        target_id: str,
        fallback_content_hash: str = "",
    ) -> list[str]:
        if target_type == "memory_card":
            return ["card", target_id]
        if target_type != "session_chunk":
            raise ValueError(f"unsupported local outbox target_type: {target_type}")
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT project, provider, session_id_hash, content_hash
                  FROM session_memory_chunks
                 WHERE chunk_id = %s
                """,
                (target_id,),
            )
            row = cur.fetchone()
        if row is None:
            return ["session", "", "", "", fallback_content_hash]
        return [
            "session",
            str(_row_value(row, "project", 0) or ""),
            str(_row_value(row, "provider", 1) or ""),
            str(_row_value(row, "session_id_hash", 2) or ""),
            str(_row_value(row, "content_hash", 3) or fallback_content_hash),
        ]

    def _enqueue_local_outbox_on(
        self,
        conn: Any,
        *,
        target_type: str,
        target_id: str,
        source_content_hash: str,
        payload_text: str,
        profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
        acquire_locks: bool = True,
    ) -> int:
        if target_type not in {"memory_card", "session_chunk"}:
            raise ValueError("unsupported local outbox target type")
        if profile_id != LOCAL_EMBEDDING_PROFILE_ID:
            raise ValueError(f"unsupported local embedding profile_id: {profile_id}")
        if not target_id or not source_content_hash:
            raise ValueError("target_id and source_content_hash are required")
        input_hash = compute_input_hash(payload_text)
        if acquire_locks:
            identity_parts = self._resolve_target_identity_parts_on(
                conn,
                target_type=target_type,
                target_id=target_id,
                fallback_content_hash=source_content_hash,
            )
            self._acquire_local_locks_on(
                conn,
                identity_parts_list=[identity_parts],
                target_type=target_type,
                target_id=target_id,
            )

        parent_table = "memory_cards" if target_type == "memory_card" else "session_memory_chunks"
        parent_id_col = "memory_id" if target_type == "memory_card" else "chunk_id"
        derived_table = "card_local_embeddings" if target_type == "memory_card" else "session_local_embeddings"

        with conn.cursor() as cur:
            # Lock order: queue -> parent -> derived (after identity -> target advisory locks)
            cur.execute(
                """
                UPDATE local_embedding_outbox
                   SET status = 'cancelled',
                       lease_until = NULL,
                       updated_at = NOW()
                 WHERE target_type = %s
                   AND target_id = %s
                   AND profile_id = %s
                   AND status IN ('queued', 'processing', 'failed')
                   AND (source_content_hash <> %s OR input_hash <> %s)
                """,
                (target_type, target_id, profile_id, source_content_hash, input_hash),
            )
            cur.execute(
                f"""
                SELECT content_hash
                  FROM {parent_table}
                 WHERE {parent_id_col} = %s
                 FOR KEY SHARE
                """,
                (target_id,),
            )
            parent_row = cur.fetchone()
            if parent_row is None:
                raise ValueError(f"local outbox parent {target_type}:{target_id} does not exist")
            parent_hash = str(_row_value(parent_row, "content_hash", 0) or "")
            if parent_hash != source_content_hash:
                raise ValueError("local outbox source_content_hash does not match parent row")

            cur.execute(
                """
                INSERT INTO local_embedding_tombstones (
                    target_type, target_id, profile_id, incarnation, deleted_at, updated_at
                ) VALUES (%s, %s, %s, 1, NULL, NOW())
                ON CONFLICT (target_type, target_id, profile_id) DO UPDATE SET
                    deleted_at = NULL,
                    updated_at = NOW()
                RETURNING incarnation
                """,
                (target_type, target_id, profile_id),
            )
            tomb_row = cur.fetchone()
            incarnation = int(_row_value(tomb_row, "incarnation", 0) or 1)

            cur.execute(
                f"""
                INSERT INTO {derived_table} (
                    {parent_id_col}, profile_id, source_content_hash, input_hash, vector, state, updated_at
                ) VALUES (%s, %s, %s, %s, NULL, 'pending', NOW())
                ON CONFLICT ({parent_id_col}, profile_id) DO UPDATE SET
                    source_content_hash = EXCLUDED.source_content_hash,
                    input_hash = EXCLUDED.input_hash,
                    vector = CASE
                        WHEN {derived_table}.source_content_hash = EXCLUDED.source_content_hash
                         AND {derived_table}.input_hash = EXCLUDED.input_hash
                         AND {derived_table}.state = 'ready'
                        THEN {derived_table}.vector
                        ELSE NULL
                    END,
                    state = CASE
                        WHEN {derived_table}.source_content_hash = EXCLUDED.source_content_hash
                         AND {derived_table}.input_hash = EXCLUDED.input_hash
                         AND {derived_table}.state = 'ready'
                        THEN 'ready'
                        WHEN {derived_table}.source_content_hash = EXCLUDED.source_content_hash
                         AND {derived_table}.input_hash = EXCLUDED.input_hash
                         AND {derived_table}.state = 'failed'
                        THEN 'failed'
                        ELSE 'pending'
                    END,
                    updated_at = NOW()
                """,
                (target_id, profile_id, source_content_hash, input_hash),
            )

            cur.execute(
                """
                SELECT outbox_id
                  FROM local_embedding_outbox
                 WHERE target_type = %s
                   AND target_id = %s
                   AND profile_id = %s
                   AND source_content_hash = %s
                   AND input_hash = %s
                   AND incarnation = %s
                   AND status IN ('queued', 'processing', 'failed', 'dead_letter')
                 ORDER BY outbox_id
                 LIMIT 1
                """,
                (target_type, target_id, profile_id, source_content_hash, input_hash, incarnation),
            )
            existing = cur.fetchone()
            if existing is not None:
                return int(_row_value(existing, "outbox_id", 0))

            row = None
            try:
                cur.execute("SAVEPOINT local_embedding_outbox_enqueue")
                cur.execute(
                    """
                    INSERT INTO local_embedding_outbox (
                        target_type, target_id, profile_id, source_content_hash,
                        input_hash, payload_text, incarnation, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'queued')
                    RETURNING outbox_id
                    """,
                    (
                        target_type,
                        target_id,
                        profile_id,
                        source_content_hash,
                        input_hash,
                        payload_text,
                        incarnation,
                    ),
                )
                row = cur.fetchone()
            except Exception as exc:
                if "UniqueViolation" not in type(exc).__name__:
                    raise
                cur.execute("ROLLBACK TO SAVEPOINT local_embedding_outbox_enqueue")
                cur.execute(
                    """
                    SELECT outbox_id
                      FROM local_embedding_outbox
                     WHERE target_type = %s
                       AND target_id = %s
                       AND profile_id = %s
                       AND source_content_hash = %s
                       AND input_hash = %s
                       AND incarnation = %s
                       AND status IN ('queued', 'processing', 'failed')
                     ORDER BY outbox_id
                     LIMIT 1
                    """,
                    (target_type, target_id, profile_id, source_content_hash, input_hash, incarnation),
                )
                row = cur.fetchone()
            finally:
                cur.execute("RELEASE SAVEPOINT local_embedding_outbox_enqueue")
        if row is None:
            raise RuntimeError("local embedding outbox insert returned no id")
        return int(_row_value(row, "outbox_id", 0))

    def enqueue_local_outbox(
        self,
        target_type: str,
        target_id: str,
        source_content_hash: str,
        payload_text: str,
        *,
        profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
        conn: Any | None = None,
    ) -> int:
        with self._scope(conn=conn, write=conn is None and self.connection is None) as db:
            return self._enqueue_local_outbox_on(
                db,
                target_type=target_type,
                target_id=target_id,
                source_content_hash=source_content_hash,
                payload_text=payload_text,
                profile_id=profile_id,
                acquire_locks=True,
            )

    def insert_chunk_with_local_outbox(
        self,
        chunk: SessionChunk,
        *,
        profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
        conn: Any | None = None,
        insert_only: bool = False,
        already_locked_identity: bool = False,
    ) -> tuple[str, int]:
        """Atomically persist session_memory_chunks metadata/body (without mutating Gemini vector) and enqueue local outbox."""

        normalized = self._normalize_chunk(chunk)
        identity_parts = [
            "session",
            normalized.project,
            normalized.provider,
            normalized.session_id_hash,
            normalized.content_hash,
        ]
        conflict_clause = (
            ""
            if insert_only
            else """
            ON CONFLICT (chunk_id) DO UPDATE SET
                session_id_hash = EXCLUDED.session_id_hash,
                project = EXCLUDED.project,
                provider = EXCLUDED.provider,
                chunk_index = EXCLUDED.chunk_index,
                content_markdown = EXCLUDED.content_markdown,
                token_count = EXCLUDED.token_count,
                content_hash = EXCLUDED.content_hash,
                updated_at = EXCLUDED.updated_at,
                observed_at = COALESCE(EXCLUDED.observed_at, session_memory_chunks.observed_at)
            """
        )
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
            normalized.observed_at,
        )
        with self._scope(conn=conn, write=conn is None and self.connection is None) as db:
            if already_locked_identity:
                with db.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(%s)",
                        (target_advisory_lock_id("session_chunk", normalized.chunk_id),),
                    )
            else:
                self._acquire_local_locks_on(
                    db,
                    identity_parts_list=[identity_parts],
                    target_type="session_chunk",
                    target_id=normalized.chunk_id,
                )
            with db.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO session_memory_chunks (
                        chunk_id, session_id_hash, project, provider, chunk_index,
                        content_markdown, token_count, content_hash, embedding_model,
                        embedding_state, embedding_revision, embedding, created_at, updated_at,
                        observed_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s::halfvec, %s, %s, %s
                    )
                    {conflict_clause}
                    """,
                    values,
                )
            outbox_id = self._enqueue_local_outbox_on(
                db,
                target_type="session_chunk",
                target_id=normalized.chunk_id,
                source_content_hash=normalized.content_hash,
                payload_text=normalized.content_markdown,
                profile_id=profile_id,
                acquire_locks=False,
            )
        return normalized.chunk_id, outbox_id

    def claim_local_outbox_leases(
        self,
        worker_id: str,
        batch_size: int = 10,
        lease_seconds: int = 30,
        *,
        profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
        max_retries: int = OUTBOX_RETRY_LIMIT,
    ) -> list[LocalOutboxJob]:
        """Claim local embedding jobs using unlocked candidate scan + ordered advisory & row locks."""

        if not worker_id or batch_size < 1 or lease_seconds < 1:
            raise ValueError("invalid local outbox lease arguments")
        claimed: list[LocalOutboxJob] = []
        scan_limit = max(int(batch_size) * 4, 16)
        with self._scope(write=True) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT outbox_id, target_type, target_id, profile_id,
                           source_content_hash, input_hash, incarnation
                      FROM local_embedding_outbox
                     WHERE profile_id = %s
                       AND (
                            status = 'queued'
                            OR (
                                status = 'failed'
                                AND retry_count < %s
                                AND (next_attempt_at IS NULL OR next_attempt_at <= clock_timestamp())
                            )
                            OR (
                                status = 'processing'
                                AND retry_count < %s
                                AND lease_until IS NOT NULL
                                AND lease_until < clock_timestamp()
                            )
                           )
                     ORDER BY created_at ASC, outbox_id ASC
                     LIMIT %s
                    """,
                    (profile_id, max_retries, max_retries, scan_limit),
                )
                candidates = cur.fetchall()

            for cand in candidates:
                if len(claimed) >= int(batch_size):
                    break
                cand_outbox_id = int(_row_value(cand, "outbox_id", 0))
                cand_target_type = str(_row_value(cand, "target_type", 1))
                cand_target_id = str(_row_value(cand, "target_id", 2))
                cand_source_hash = str(_row_value(cand, "source_content_hash", 4))
                cand_input_hash = str(_row_value(cand, "input_hash", 5))
                cand_incarnation = int(_row_value(cand, "incarnation", 6))

                identity_parts = self._resolve_target_identity_parts_on(
                    db,
                    target_type=cand_target_type,
                    target_id=cand_target_id,
                    fallback_content_hash=cand_source_hash,
                )
                id_lock = compute_identity_lock_id(identity_parts)
                tgt_lock = target_advisory_lock_id(cand_target_type, cand_target_id)

                with db.cursor() as cur:
                    cur.execute("SAVEPOINT local_claim_candidate")
                    cur.execute("SELECT pg_try_advisory_xact_lock(%s) AS locked", (id_lock,))
                    id_ok = bool(_row_value(cur.fetchone(), "locked", 0))
                    if not id_ok:
                        cur.execute("ROLLBACK TO SAVEPOINT local_claim_candidate")
                        cur.execute("RELEASE SAVEPOINT local_claim_candidate")
                        continue
                    cur.execute("SELECT pg_try_advisory_xact_lock(%s) AS locked", (tgt_lock,))
                    tgt_ok = bool(_row_value(cur.fetchone(), "locked", 0))
                    if not tgt_ok:
                        cur.execute("ROLLBACK TO SAVEPOINT local_claim_candidate")
                        cur.execute("RELEASE SAVEPOINT local_claim_candidate")
                        continue

                    cur.execute(
                        """
                        SELECT outbox_id, target_type, target_id, profile_id,
                               source_content_hash, input_hash, payload_text, incarnation,
                               status, fencing_token, worker_id, claimed_at, lease_until,
                               retry_count, next_attempt_at, last_error, created_at, updated_at
                          FROM local_embedding_outbox
                         WHERE outbox_id = %s
                           AND profile_id = %s
                           AND (
                                status = 'queued'
                                OR (
                                    status = 'failed'
                                    AND retry_count < %s
                                    AND (next_attempt_at IS NULL OR next_attempt_at <= clock_timestamp())
                                )
                                OR (
                                    status = 'processing'
                                    AND retry_count < %s
                                    AND lease_until IS NOT NULL
                                    AND lease_until < clock_timestamp()
                                )
                               )
                         FOR UPDATE SKIP LOCKED
                        """,
                        (cand_outbox_id, profile_id, max_retries, max_retries),
                    )
                    locked_job = cur.fetchone()
                    if locked_job is None:
                        cur.execute("ROLLBACK TO SAVEPOINT local_claim_candidate")
                        cur.execute("RELEASE SAVEPOINT local_claim_candidate")
                        continue

                    job_status = str(_row_value(locked_job, "status", 8) or "")
                    job_retry_count = int(_row_value(locked_job, "retry_count", 13) or 0)

                    # Reclaiming an expired processing job counts against the retry budget
                    if job_status == "processing" and (job_retry_count + 1) >= int(max_retries):
                        cur.execute(
                            """
                            UPDATE local_embedding_outbox
                               SET status = 'dead_letter',
                                   retry_count = retry_count + 1,
                                   lease_until = NULL,
                                   last_error = 'lease expired and retry budget exhausted',
                                   updated_at = clock_timestamp()
                             WHERE outbox_id = %s
                            """,
                            (cand_outbox_id,),
                        )
                        derived_table = (
                            "card_local_embeddings"
                            if cand_target_type == "memory_card"
                            else "session_local_embeddings"
                        )
                        parent_id_col = "memory_id" if cand_target_type == "memory_card" else "chunk_id"
                        cur.execute(
                            f"""
                            INSERT INTO {derived_table} (
                                {parent_id_col}, profile_id, source_content_hash, input_hash,
                                vector, state, updated_at
                            ) VALUES (%s, %s, %s, %s, NULL, 'failed', NOW())
                            ON CONFLICT ({parent_id_col}, profile_id) DO UPDATE SET
                                source_content_hash = EXCLUDED.source_content_hash,
                                input_hash = EXCLUDED.input_hash,
                                vector = NULL,
                                state = 'failed',
                                updated_at = NOW()
                            """,
                            (cand_target_id, profile_id, cand_source_hash, cand_input_hash),
                        )
                        cur.execute("RELEASE SAVEPOINT local_claim_candidate")
                        continue

                    # Verify tombstone and parent still match before leasing
                    cur.execute(
                        """
                        SELECT incarnation, deleted_at
                          FROM local_embedding_tombstones
                         WHERE target_type = %s AND target_id = %s AND profile_id = %s
                        """,
                        (cand_target_type, cand_target_id, profile_id),
                    )
                    tomb = cur.fetchone()
                    if tomb is not None and (
                        _row_value(tomb, "deleted_at", 1) is not None
                        or int(_row_value(tomb, "incarnation", 0) or 1) != cand_incarnation
                    ):
                        cur.execute(
                            """
                            UPDATE local_embedding_outbox
                               SET status = 'cancelled',
                                   lease_until = NULL,
                                   last_error = 'cancelled: target deleted or incarnation changed',
                                   updated_at = NOW()
                             WHERE outbox_id = %s
                            """,
                            (cand_outbox_id,),
                        )
                        cur.execute("RELEASE SAVEPOINT local_claim_candidate")
                        continue

                    parent_table = (
                        "memory_cards" if cand_target_type == "memory_card" else "session_memory_chunks"
                    )
                    parent_id_col = "memory_id" if cand_target_type == "memory_card" else "chunk_id"
                    cur.execute(
                        f"SELECT content_hash FROM {parent_table} WHERE {parent_id_col} = %s FOR KEY SHARE",
                        (cand_target_id,),
                    )
                    parent_row = cur.fetchone()
                    if (
                        parent_row is None
                        or str(_row_value(parent_row, "content_hash", 0) or "") != cand_source_hash
                    ):
                        cur.execute(
                            """
                            UPDATE local_embedding_outbox
                               SET status = 'cas_skipped',
                                   lease_until = NULL,
                                   last_error = 'CAS skip: parent missing or content_hash changed',
                                   updated_at = NOW()
                             WHERE outbox_id = %s
                            """,
                            (cand_outbox_id,),
                        )
                        cur.execute("RELEASE SAVEPOINT local_claim_candidate")
                        continue

                    cur.execute(
                        """
                        UPDATE local_embedding_outbox
                           SET status = 'processing',
                               retry_count = CASE
                                   WHEN status = 'processing' THEN retry_count + 1
                                   ELSE retry_count
                               END,
                               fencing_token = fencing_token + 1,
                               worker_id = %s,
                               claimed_at = clock_timestamp(),
                               lease_until = clock_timestamp() + (%s * INTERVAL '1 second'),
                               last_error = CASE
                                   WHEN status = 'processing' THEN 'lease expired; reclaimed'
                                   ELSE last_error
                               END,
                               updated_at = clock_timestamp()
                         WHERE outbox_id = %s
                        RETURNING outbox_id, target_type, target_id, profile_id,
                                  source_content_hash, input_hash, payload_text, incarnation,
                                  status, fencing_token, worker_id, claimed_at, lease_until,
                                  retry_count, next_attempt_at, last_error, created_at, updated_at
                        """,
                        (worker_id, int(lease_seconds), cand_outbox_id),
                    )
                    updated_row = cur.fetchone()
                    cur.execute("RELEASE SAVEPOINT local_claim_candidate")
                    if updated_row is not None:
                        claimed.append(_local_outbox_from_row(updated_row))
        return claimed

    def renew_local_outbox_lease(
        self,
        outbox_id: int,
        worker_id: str,
        fencing_token: int,
        lease_seconds: int = 30,
    ) -> bool:
        if not worker_id or lease_seconds < 1 or fencing_token < 1:
            raise ValueError("invalid local outbox lease renewal arguments")
        with self._scope(write=True) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    UPDATE local_embedding_outbox
                       SET lease_until = clock_timestamp() + (%s * INTERVAL '1 second'),
                           updated_at = clock_timestamp()
                     WHERE outbox_id = %s
                       AND status = 'processing'
                       AND worker_id = %s
                       AND fencing_token = %s
                       AND lease_until > clock_timestamp()
                    """,
                    (int(lease_seconds), int(outbox_id), worker_id, int(fencing_token)),
                )
                return cur.rowcount == 1

    def complete_local_outbox_cas(
        self,
        *,
        outbox_id: int,
        worker_id: str,
        fencing_token: int,
        vector: list[float],
        profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
    ) -> bool:
        """CAS-write a 256-dim local vector and complete the outbox job in global lock order."""

        if not worker_id or outbox_id < 1 or fencing_token < 1:
            raise ValueError("local embedding write-back requires outbox_id, worker_id, and fencing_token")
        if profile_id != LOCAL_EMBEDDING_PROFILE_ID:
            raise ValueError(f"unsupported local embedding profile_id: {profile_id}")
        vector_literal = _local_vector_literal(vector)

        with self._scope(write=True) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT target_type, target_id, source_content_hash
                      FROM local_embedding_outbox
                     WHERE outbox_id = %s
                    """,
                    (int(outbox_id),),
                )
                meta = cur.fetchone()
            if meta is None:
                raise ValueError("local outbox job not found")
            target_type = str(_row_value(meta, "target_type", 0))
            target_id = str(_row_value(meta, "target_id", 1))
            source_hash_hint = str(_row_value(meta, "source_content_hash", 2))
            identity_parts = self._resolve_target_identity_parts_on(
                db,
                target_type=target_type,
                target_id=target_id,
                fallback_content_hash=source_hash_hint,
            )
            self._acquire_local_locks_on(
                db,
                identity_parts_list=[identity_parts],
                target_type=target_type,
                target_id=target_id,
            )

            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT outbox_id, target_type, target_id, profile_id,
                           source_content_hash, input_hash, incarnation,
                           status, fencing_token, worker_id, lease_until
                      FROM local_embedding_outbox
                     WHERE outbox_id = %s
                       AND profile_id = %s
                       AND status = 'processing'
                       AND worker_id = %s
                       AND fencing_token = %s
                       AND lease_until > clock_timestamp()
                     FOR UPDATE
                    """,
                    (int(outbox_id), profile_id, worker_id, int(fencing_token)),
                )
                job_row = cur.fetchone()
                if job_row is None:
                    raise ValueError("local outbox job not found or lease/fencing_token mismatch")

                job_source_hash = str(_row_value(job_row, "source_content_hash", 4))
                job_input_hash = str(_row_value(job_row, "input_hash", 5))
                job_incarnation = int(_row_value(job_row, "incarnation", 6))

                cur.execute(
                    """
                    SELECT incarnation, deleted_at
                      FROM local_embedding_tombstones
                     WHERE target_type = %s AND target_id = %s AND profile_id = %s
                    """,
                    (target_type, target_id, profile_id),
                )
                tomb = cur.fetchone()
                if tomb is not None and (
                    _row_value(tomb, "deleted_at", 1) is not None
                    or int(_row_value(tomb, "incarnation", 0) or 1) != job_incarnation
                ):
                    cur.execute(
                        """
                        UPDATE local_embedding_outbox
                           SET status = 'cancelled',
                               last_error = 'CAS skip: target deleted or incarnation changed',
                               lease_until = NULL,
                               updated_at = NOW()
                         WHERE outbox_id = %s
                        """,
                        (int(outbox_id),),
                    )
                    return False

                if target_type == "memory_card":
                    cur.execute(
                        """
                        SELECT content_hash, summary, title, typed_payload
                          FROM memory_cards
                         WHERE memory_id = %s
                         FOR KEY SHARE
                        """,
                        (target_id,),
                    )
                    parent = cur.fetchone()
                    if parent is None:
                        cur.execute(
                            """
                            UPDATE local_embedding_outbox
                               SET status = 'cancelled',
                                   last_error = 'CAS skip: parent card deleted',
                                   lease_until = NULL,
                                   updated_at = NOW()
                             WHERE outbox_id = %s
                            """,
                            (int(outbox_id),),
                        )
                        return False
                    current_source_hash = str(_row_value(parent, "content_hash", 0) or "")
                    try:
                        current_input = card_input_v1(
                            summary=_row_value(parent, "summary", 1),
                            title=_row_value(parent, "title", 2),
                            typed_payload=_json_value(_row_value(parent, "typed_payload", 3)),
                        )
                        current_input_hash = compute_input_hash(current_input)
                    except Exception:
                        current_input_hash = ""
                    derived_table = "card_local_embeddings"
                    parent_id_col = "memory_id"
                else:
                    cur.execute(
                        """
                        SELECT content_hash, content_markdown
                          FROM session_memory_chunks
                         WHERE chunk_id = %s
                         FOR KEY SHARE
                        """,
                        (target_id,),
                    )
                    parent = cur.fetchone()
                    if parent is None:
                        cur.execute(
                            """
                            UPDATE local_embedding_outbox
                               SET status = 'cancelled',
                                   last_error = 'CAS skip: parent chunk deleted',
                                   lease_until = NULL,
                                   updated_at = NOW()
                             WHERE outbox_id = %s
                            """,
                            (int(outbox_id),),
                        )
                        return False
                    current_source_hash = str(_row_value(parent, "content_hash", 0) or "")
                    try:
                        current_input_hash = compute_input_hash(
                            str(_row_value(parent, "content_markdown", 1) or "")
                        )
                    except Exception:
                        current_input_hash = ""
                    derived_table = "session_local_embeddings"
                    parent_id_col = "chunk_id"

                if current_source_hash != job_source_hash or current_input_hash != job_input_hash:
                    cur.execute(
                        """
                        UPDATE local_embedding_outbox
                           SET status = 'cas_skipped',
                               last_error = 'CAS skip: source or input hash mismatch',
                               lease_until = NULL,
                               updated_at = NOW()
                         WHERE outbox_id = %s
                        """,
                        (int(outbox_id),),
                    )
                    return False

                cur.execute(
                    f"""
                    INSERT INTO {derived_table} (
                        {parent_id_col}, profile_id, source_content_hash, input_hash,
                        vector, state, updated_at
                    ) VALUES (%s, %s, %s, %s, %s::vector(256), 'ready', NOW())
                    ON CONFLICT ({parent_id_col}, profile_id) DO UPDATE SET
                        source_content_hash = EXCLUDED.source_content_hash,
                        input_hash = EXCLUDED.input_hash,
                        vector = EXCLUDED.vector,
                        state = 'ready',
                        updated_at = NOW()
                    """,
                    (target_id, profile_id, job_source_hash, job_input_hash, vector_literal),
                )
                cur.execute(
                    """
                    UPDATE local_embedding_outbox
                       SET status = 'completed',
                           last_error = NULL,
                           lease_until = NULL,
                           updated_at = NOW()
                     WHERE outbox_id = %s
                       AND status = 'processing'
                       AND worker_id = %s
                       AND fencing_token = %s
                    """,
                    (int(outbox_id), worker_id, int(fencing_token)),
                )
                if cur.rowcount != 1:
                    raise ValueError("local outbox completion failed fence verification")
                return True

    def mark_local_outbox_failed(
        self,
        *,
        outbox_id: int,
        worker_id: str,
        fencing_token: int,
        error_code: str,
        retryable: bool = True,
        max_retries: int = OUTBOX_RETRY_LIMIT,
        profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
    ) -> str:
        """Record failure on a local embedding job with bounded retry or dead-letter transition."""

        if max_retries < 1:
            raise ValueError("max_retries must be positive")
        if not worker_id or outbox_id < 1 or fencing_token < 1:
            raise ValueError("local outbox failure requires outbox_id, worker_id, and fencing_token")
        safe_error = str(error_code or "unknown_error")[:200]

        with self._scope(write=True) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT target_type, target_id, source_content_hash
                      FROM local_embedding_outbox
                     WHERE outbox_id = %s
                    """,
                    (int(outbox_id),),
                )
                meta = cur.fetchone()
            if meta is None:
                raise ValueError("local outbox job not found")
            target_type = str(_row_value(meta, "target_type", 0))
            target_id = str(_row_value(meta, "target_id", 1))
            source_hash_hint = str(_row_value(meta, "source_content_hash", 2))
            identity_parts = self._resolve_target_identity_parts_on(
                db,
                target_type=target_type,
                target_id=target_id,
                fallback_content_hash=source_hash_hint,
            )
            self._acquire_local_locks_on(
                db,
                identity_parts_list=[identity_parts],
                target_type=target_type,
                target_id=target_id,
            )
            with db.cursor() as cur:
                cur.execute(
                    """
                    UPDATE local_embedding_outbox
                       SET retry_count = retry_count + 1,
                           last_error = %s,
                           status = CASE
                               WHEN NOT %s OR retry_count + 1 >= %s THEN 'dead_letter'
                               ELSE 'failed'
                           END,
                           lease_until = NULL,
                           next_attempt_at = CASE
                               WHEN NOT %s OR retry_count + 1 >= %s THEN NULL
                               ELSE clock_timestamp() + (LEAST(60, POWER(2, retry_count + 1)) * INTERVAL '1 second')
                           END,
                           updated_at = clock_timestamp()
                     WHERE outbox_id = %s
                       AND profile_id = %s
                       AND status = 'processing'
                       AND worker_id = %s
                       AND fencing_token = %s
                       AND lease_until > clock_timestamp()
                    RETURNING status, source_content_hash, input_hash
                    """,
                    (
                        safe_error,
                        bool(retryable),
                        int(max_retries),
                        bool(retryable),
                        int(max_retries),
                        int(outbox_id),
                        profile_id,
                        worker_id,
                        int(fencing_token),
                    ),
                )
                updated = cur.fetchone()
                if updated is None:
                    raise ValueError("local outbox job not found or lease/fencing_token mismatch")
                new_status = str(_row_value(updated, "status", 0))
                if new_status == "dead_letter":
                    derived_table = (
                        "card_local_embeddings"
                        if target_type == "memory_card"
                        else "session_local_embeddings"
                    )
                    parent_id_col = "memory_id" if target_type == "memory_card" else "chunk_id"
                    cur.execute(
                        f"""
                        UPDATE {derived_table}
                           SET state = 'failed',
                               updated_at = NOW()
                         WHERE {parent_id_col} = %s
                           AND profile_id = %s
                           AND source_content_hash = %s
                           AND input_hash = %s
                           AND state <> 'ready'
                        """,
                        (
                            target_id,
                            profile_id,
                            str(_row_value(updated, "source_content_hash", 1)),
                            str(_row_value(updated, "input_hash", 2)),
                        ),
                    )
                return new_status

    def requeue_local_dead_letters(
        self,
        *,
        target_ids: Sequence[str] | None = None,
        allowed_error_codes: Sequence[str] = tuple(_TRANSIENT_LOCAL_OUTBOX_ERRORS),
        profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
    ) -> int:
        """Bounded, idempotent requeue of transient dead-lettered local embedding jobs."""

        allowed = [str(code) for code in allowed_error_codes if str(code)]
        if not allowed:
            return 0
        requeued = 0
        with self._scope(write=True) as db:
            params: list[Any] = [profile_id, allowed]
            target_clause = ""
            if target_ids is not None:
                ids = [str(item) for item in target_ids if str(item)]
                if not ids:
                    return 0
                target_clause = " AND target_id = ANY(%s)"
                params.append(ids)
            with db.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT outbox_id, target_type, target_id, source_content_hash, input_hash, incarnation
                      FROM local_embedding_outbox
                     WHERE profile_id = %s
                       AND status = 'dead_letter'
                       AND regexp_replace(last_error, '^retryable_error:', '') = ANY(%s)
                       {target_clause}
                     ORDER BY outbox_id ASC
                    """,
                    params,
                )
                candidates = cur.fetchall()

            for row in candidates:
                outbox_id = int(_row_value(row, "outbox_id", 0))
                target_type = str(_row_value(row, "target_type", 1))
                target_id = str(_row_value(row, "target_id", 2))
                source_hash = str(_row_value(row, "source_content_hash", 3))
                input_hash = str(_row_value(row, "input_hash", 4))
                incarnation = int(_row_value(row, "incarnation", 5))

                identity_parts = self._resolve_target_identity_parts_on(
                    db,
                    target_type=target_type,
                    target_id=target_id,
                    fallback_content_hash=source_hash,
                )
                self._acquire_local_locks_on(
                    db,
                    identity_parts_list=[identity_parts],
                    target_type=target_type,
                    target_id=target_id,
                )
                with db.cursor() as cur:
                    cur.execute(
                        """
                        SELECT incarnation, deleted_at
                          FROM local_embedding_tombstones
                         WHERE target_type = %s AND target_id = %s AND profile_id = %s
                        """,
                        (target_type, target_id, profile_id),
                    )
                    tomb = cur.fetchone()
                    if tomb is not None and (
                        _row_value(tomb, "deleted_at", 1) is not None
                        or int(_row_value(tomb, "incarnation", 0) or 1) != incarnation
                    ):
                        continue
                    parent_table = (
                        "memory_cards" if target_type == "memory_card" else "session_memory_chunks"
                    )
                    parent_id_col = "memory_id" if target_type == "memory_card" else "chunk_id"
                    cur.execute(
                        f"SELECT content_hash FROM {parent_table} WHERE {parent_id_col} = %s FOR KEY SHARE",
                        (target_id,),
                    )
                    parent = cur.fetchone()
                    if parent is None or str(_row_value(parent, "content_hash", 0) or "") != source_hash:
                        continue
                    cur.execute(
                        """
                        SELECT 1
                          FROM local_embedding_outbox
                         WHERE target_type = %s
                           AND target_id = %s
                           AND profile_id = %s
                           AND source_content_hash = %s
                           AND input_hash = %s
                           AND incarnation = %s
                           AND status IN ('queued', 'processing', 'failed', 'completed')
                         LIMIT 1
                        """,
                        (target_type, target_id, profile_id, source_hash, input_hash, incarnation),
                    )
                    if cur.fetchone() is not None:
                        continue
                    cur.execute(
                        """
                        UPDATE local_embedding_outbox
                           SET status = 'queued',
                               retry_count = 0,
                               next_attempt_at = NULL,
                               lease_until = NULL,
                               worker_id = NULL,
                               claimed_at = NULL,
                               updated_at = NOW()
                         WHERE outbox_id = %s
                           AND status = 'dead_letter'
                        """,
                        (outbox_id,),
                    )
                    if cur.rowcount == 1:
                        derived_table = (
                            "card_local_embeddings"
                            if target_type == "memory_card"
                            else "session_local_embeddings"
                        )
                        cur.execute(
                            f"""
                            UPDATE {derived_table}
                               SET state = 'pending',
                                   updated_at = NOW()
                             WHERE {parent_id_col} = %s
                               AND profile_id = %s
                               AND source_content_hash = %s
                               AND state <> 'ready'
                            """,
                            (target_id, profile_id, source_hash),
                        )
                        requeued += 1
        return requeued

    def _delete_local_target_on(
        self,
        conn: Any,
        *,
        target_type: str,
        target_id: str,
        identity_parts_list: Sequence[Sequence[str]],
        profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
    ) -> int:
        self._acquire_local_locks_on(
            conn,
            identity_parts_list=identity_parts_list,
            target_type=target_type,
            target_id=target_id,
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE local_embedding_outbox
                   SET status = 'cancelled',
                       lease_until = NULL,
                       last_error = 'cancelled: parent deleted',
                       updated_at = NOW()
                 WHERE target_type = %s
                   AND target_id = %s
                   AND profile_id = %s
                   AND status IN ('queued', 'processing', 'failed')
                """,
                (target_type, target_id, profile_id),
            )
            cur.execute(
                """
                INSERT INTO local_embedding_tombstones (
                    target_type, target_id, profile_id, incarnation, deleted_at, updated_at
                ) VALUES (%s, %s, %s, 2, NOW(), NOW())
                ON CONFLICT (target_type, target_id, profile_id) DO UPDATE SET
                    incarnation = local_embedding_tombstones.incarnation + 1,
                    deleted_at = NOW(),
                    updated_at = NOW()
                RETURNING incarnation
                """,
                (target_type, target_id, profile_id),
            )
            row = cur.fetchone()
            return int(_row_value(row, "incarnation", 0) or 2)

    def delete_session_chunk(
        self,
        chunk_id: str,
        *,
        conn: Any | None = None,
        profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
    ) -> None:
        """Delete a session chunk in global lock order with tombstone incarnation increment."""

        with self._scope(conn=conn, write=conn is None and self.connection is None) as db:
            identity_parts = self._resolve_target_identity_parts_on(
                db,
                target_type="session_chunk",
                target_id=chunk_id,
            )
            self._delete_local_target_on(
                db,
                target_type="session_chunk",
                target_id=chunk_id,
                identity_parts_list=[identity_parts],
                profile_id=profile_id,
            )
            with db.cursor() as cur:
                cur.execute("DELETE FROM session_memory_chunks WHERE chunk_id = %s", (chunk_id,))

    def hybrid_search_local(
        self,
        project: str = "",
        query_vector: list[float] | None = None,
        limit: int = 5,
        card_type: str | None = None,
        authorization_status: str | None = "active",
        currentness: str | None = "current",
        as_of: datetime | date | str | None = None,
        text_query: str | None = None,
        steward_only: bool = False,
        *,
        profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
    ) -> list[dict[str, Any]]:
        """Run filtered pgvector search over card_local_embeddings (256-dim cosine) without mixing 3072 vectors."""

        if query_vector is None:
            raise ValueError("hybrid_search_local requires a query vector")
        if profile_id != LOCAL_EMBEDDING_PROFILE_ID:
            raise ValueError(f"unsupported local embedding profile_id: {profile_id}")
        _validate_local_vector(query_vector)
        if limit < 1:
            raise ValueError("limit must be positive")
        vector_literal = _local_vector_literal(query_vector)

        clauses = [
            "cle.profile_id = %s",
            "cle.state = 'ready'",
            "cle.vector IS NOT NULL",
            "cle.source_content_hash = mc.content_hash",
        ]
        params: list[Any] = [profile_id]
        if project:
            clauses.append("mc.project = %s")
            params.append(project)
        if steward_only:
            clauses.extend(
                (
                    "mc.steward_envelope IS NOT NULL",
                    "mc.steward_envelope->>'approval_state' IN ('approved', 'auto_accepted')",
                    "coalesce(mc.steward_envelope->>'steward_proposal_kind', '') <> 'stale'",
                )
            )
        if authorization_status:
            clauses.append("mc.authorization_status = %s")
            params.append(authorization_status)
        if currentness == "current" and as_of:
            clauses.append("mc.currentness IN ('current', 'superseded')")
        elif currentness:
            clauses.append("mc.currentness = %s")
            params.append(currentness)
        if card_type:
            clauses.append("mc.card_type = %s")
            params.append(card_type)
        clauses.append("mc.lifecycle_state IN ('accepted', 'human_accepted', 'auto_accepted')")
        as_of_dt = _parse_as_of(as_of) or datetime.now(timezone.utc)
        clauses.append("mc.valid_from <= %s AND (mc.valid_to IS NULL OR mc.valid_to > %s)")
        params.extend((as_of_dt, as_of_dt))
        clauses.append(
            _STATUS_EXPIRY_FILTER.replace("card_type", "mc.card_type").replace(
                "typed_payload", "mc.typed_payload"
            )
        )
        params.append(as_of_dt)

        score_expression = "1.0 - distance"
        query_params: list[Any] = [vector_literal, *params, vector_literal, max(50, int(limit) * 5)]
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
                        SELECT mc.memory_id, mc.card_type, mc.title, mc.summary, mc.typed_payload,
                               mc.currentness, mc.confidence, mc.content_hash,
                               mc.valid_from, mc.valid_to,
                               mc.freshness_state, mc.freshness_checked_at,
                               mc.freshness_next_check_after, mc.source_hash,
                               cle.input_hash AS local_input_hash,
                               cle.vector <=> %s::vector(256) AS distance
                          FROM memory_cards AS mc
                          JOIN card_local_embeddings AS cle
                            ON cle.memory_id = mc.memory_id
                         WHERE {' AND '.join(clauses)}
                         ORDER BY cle.vector <=> %s::vector(256)
                         LIMIT %s
                    )
                    SELECT memory_id, card_type, title, summary, typed_payload,
                           currentness, confidence, content_hash,
                           {score_expression} AS similarity_score,
                           1.0 - distance AS semantic_score,
                           valid_from, valid_to,
                           freshness_state, freshness_checked_at, freshness_next_check_after, source_hash,
                           local_input_hash
                      FROM candidates
                     ORDER BY similarity_score DESC, memory_id
                     LIMIT %s
                    """,
                    query_params,
                )
                rows = cur.fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            summary_val = str(_row_value(row, "summary", 3))
            title_val = str(_row_value(row, "title", 2))
            typed_payload_val = _json_value(_row_value(row, "typed_payload", 4))
            local_input_hash = str(_row_value(row, "local_input_hash", 16) or "")
            try:
                expected_input_hash = compute_input_hash(
                    card_input_v1(
                        summary=summary_val,
                        title=title_val,
                        typed_payload=typed_payload_val,
                    )
                )
            except Exception:
                continue
            if local_input_hash != expected_input_hash:
                continue
            results.append(
                {
                    "memory_id": str(_row_value(row, "memory_id", 0)),
                    "card_type": str(_row_value(row, "card_type", 1)),
                    "title": title_val,
                    "summary": summary_val,
                    "typed_payload": typed_payload_val,
                    "currentness": str(_row_value(row, "currentness", 5)),
                    "confidence": float(_row_value(row, "confidence", 6)),
                    "content_hash": str(_row_value(row, "content_hash", 7)),
                    "similarity_score": round(float(_row_value(row, "similarity_score", 8)), 6),
                    "semantic_score": round(float(_row_value(row, "semantic_score", 9)), 6),
                    "valid_from": _row_value(row, "valid_from", 10),
                    "valid_to": _row_value(row, "valid_to", 11),
                    "freshness_state": str(
                        _row_value(row, "freshness_state", 12, default="unchecked") or "unchecked"
                    ),
                    "freshness_checked_at": _row_value(row, "freshness_checked_at", 13),
                    "freshness_next_check_after": _row_value(row, "freshness_next_check_after", 14),
                    "source_hash": _row_value(row, "source_hash", 15),
                }
            )
        return results

    def search_session_chunks_local(
        self,
        query_vector: list[float],
        project: str | None = None,
        limit: int = 5,
        conn: Any | None = None,
        *,
        profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
        exclude_session_id_hashes: list[str] | tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        """Run vector similarity search over session_local_embeddings joined to session_memory_chunks."""

        if profile_id != LOCAL_EMBEDDING_PROFILE_ID:
            raise ValueError(f"unsupported local embedding profile_id: {profile_id}")
        _validate_local_vector(query_vector)
        vector_literal = _local_vector_literal(query_vector)
        excluded_sessions = sorted(
            {str(value) for value in exclude_session_id_hashes if str(value)}
        )
        clauses = [
            "sle.profile_id = %s",
            "sle.state = 'ready'",
            "sle.vector IS NOT NULL",
            "sle.source_content_hash = smc.content_hash",
        ]
        params: list[Any] = [vector_literal, profile_id]
        if project:
            clauses.append("smc.project = %s")
            params.append(project)
        if excluded_sessions:
            clauses.append("NOT (smc.session_id_hash = ANY(%s))")
            params.append(excluded_sessions)
        params.extend([vector_literal, int(limit)])
        with self._scope(conn=conn) as db:
            if excluded_sessions:
                self.set_guc_relaxed_order(conn=db)
            with db.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT smc.chunk_id, smc.session_id_hash, smc.project, smc.provider,
                           smc.chunk_index, smc.content_markdown, smc.token_count,
                           smc.content_hash, smc.created_at, smc.observed_at,
                           sle.input_hash AS local_input_hash,
                           sle.profile_id AS local_profile_id,
                           sle.vector <=> %s::vector(256) AS distance
                      FROM session_memory_chunks AS smc
                      JOIN session_local_embeddings AS sle
                        ON sle.chunk_id = smc.chunk_id
                     WHERE {' AND '.join(clauses)}
                     ORDER BY sle.vector <=> %s::vector(256), smc.chunk_id
                     LIMIT %s
                    """,
                    params,
                )
                names = [col.name for col in cur.description]
                raw_rows = [
                    dict(row) if isinstance(row, Mapping) else dict(zip(names, row))
                    for row in cur.fetchall()
                ]
        verified: list[dict[str, Any]] = []
        for row in raw_rows:
            body_text = str(row.get("content_markdown") or "")
            try:
                expected_input_hash = compute_input_hash(body_text)
            except Exception:
                continue
            if str(row.get("local_input_hash") or "") != expected_input_hash:
                continue
            verified.append(row)
        return sorted(
            verified,
            key=lambda row: (
                float(row.get("distance") or 0.0),
                str(row.get("chunk_id") or ""),
            ),
        )

    def search_session_chunks_local_by_ids(
        self,
        query_vector: list[float],
        chunk_ids: list[str] | tuple[str, ...],
        *,
        limit: int = 1,
        conn: Any | None = None,
        profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
    ) -> list[dict[str, Any]]:
        """Search ready local embeddings restricted to receipt-approved chunk IDs."""

        if profile_id != LOCAL_EMBEDDING_PROFILE_ID:
            raise ValueError(f"unsupported local embedding profile_id: {profile_id}")
        _validate_local_vector(query_vector)
        if int(limit) < 1:
            raise ValueError("limit must be positive")
        approved_ids = sorted({str(value) for value in chunk_ids if str(value)})
        if not approved_ids:
            return []
        vector_literal = _local_vector_literal(query_vector)
        params: list[Any] = [
            vector_literal,
            approved_ids,
            profile_id,
            vector_literal,
            int(limit),
        ]
        with self._scope(conn=conn) as db:
            self.set_guc_relaxed_order(conn=db)
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT smc.chunk_id, smc.session_id_hash, smc.project, smc.provider,
                           smc.chunk_index, smc.content_markdown, smc.token_count,
                           smc.content_hash, smc.created_at, smc.observed_at,
                           sle.input_hash AS local_input_hash,
                           sle.profile_id AS local_profile_id,
                           sle.vector <=> %s::vector(256) AS distance
                      FROM session_memory_chunks AS smc
                      JOIN session_local_embeddings AS sle
                        ON sle.chunk_id = smc.chunk_id
                     WHERE smc.chunk_id = ANY(%s)
                       AND sle.profile_id = %s
                       AND sle.state = 'ready'
                       AND sle.vector IS NOT NULL
                       AND sle.source_content_hash = smc.content_hash
                     ORDER BY sle.vector <=> %s::vector(256), smc.chunk_id
                     LIMIT %s
                    """,
                    params,
                )
                names = [col.name for col in cur.description]
                raw_rows = [
                    dict(row) if isinstance(row, Mapping) else dict(zip(names, row))
                    for row in cur.fetchall()
                ]
        verified: list[dict[str, Any]] = []
        for row in raw_rows:
            body_text = str(row.get("content_markdown") or "")
            try:
                expected_input_hash = compute_input_hash(body_text)
            except Exception:
                continue
            if str(row.get("local_input_hash") or "") != expected_input_hash:
                continue
            verified.append(row)
        return sorted(
            verified,
            key=lambda row: (
                float(row.get("distance") or 0.0),
                str(row.get("chunk_id") or ""),
            ),
        )

    def list_local_outbox_jobs(
        self,
        *,
        status: str | None = None,
        profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
        conn: Any | None = None,
    ) -> list[LocalOutboxJob]:
        params: list[Any] = [profile_id]
        where = " WHERE profile_id = %s"
        if status:
            where += " AND status = %s"
            params.append(status)
        with self._scope(conn=conn) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT outbox_id, target_type, target_id, profile_id,
                           source_content_hash, input_hash, payload_text, incarnation,
                           status, fencing_token, worker_id, claimed_at, lease_until,
                           retry_count, next_attempt_at, last_error, created_at, updated_at
                      FROM local_embedding_outbox
                    """
                    + where
                    + " ORDER BY outbox_id",
                    params,
                )
                rows = cur.fetchall()
        return [_local_outbox_from_row(row) for row in rows]

    def get_local_embedding_row(
        self,
        *,
        target_type: str,
        target_id: str,
        profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
        embedding_profile: str | None = None,
        conn: Any | None = None,
    ) -> dict[str, Any] | None:
        resolved_profile = str(embedding_profile or profile_id or LOCAL_EMBEDDING_PROFILE_ID)
        if target_type in ("memory_card", "card"):
            table, id_col = "card_local_embeddings", "memory_id"
        elif target_type in ("session_chunk", "session"):
            table, id_col = "session_local_embeddings", "chunk_id"
        else:
            raise ValueError(f"unsupported target_type: {target_type}")
        with self._scope(conn=conn) as db:
            with db.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {id_col} AS target_id, profile_id, source_content_hash,
                           input_hash, vector, state, updated_at
                      FROM {table}
                     WHERE {id_col} = %s AND profile_id = %s
                    """,
                    (target_id, resolved_profile),
                )
                row = cur.fetchone()
        if row is None:
            return None
        parsed_vec = _parse_vector(_row_value(row, "vector", 4))
        src_hash = str(_row_value(row, "source_content_hash", 2))
        state_val = str(_row_value(row, "state", 5))
        prof_val = str(_row_value(row, "profile_id", 1))
        return {
            "target_id": str(_row_value(row, "target_id", 0)),
            "profile_id": prof_val,
            "embedding_profile": prof_val,
            "embedding_model": LOCAL_EMBEDDING_MODEL,
            "source_content_hash": src_hash,
            "content_hash": src_hash,
            "input_hash": str(_row_value(row, "input_hash", 3)),
            "vector": parsed_vec,
            "embedding": parsed_vec,
            "state": state_val,
            "embedding_state": state_val,
            "updated_at": _as_aware_datetime(_row_value(row, "updated_at", 6)),
        }

    def upsert_graph_operation_journal(
        self,
        *,
        operation_id: str,
        group_id: str,
        outbox_id: int | None = None,
        episode_uuid: str = "",
        source_type: str = "graph_operation",
        source_id: str = "",
        source_revision: str = "",
        content_hash: str = "",
        profile_id: str = LOCAL_EMBEDDING_PROFILE_ID,
        fence_epoch: int = 0,
        state: str = "claimed",
        execution_plan: Mapping[str, Any] | None = None,
        step_receipts: Mapping[str, Any] | None = None,
        last_error: str | None = None,
        conn: Any | None = None,
        operation_type: str | None = None,
        status: str | None = None,
        step_cursor: str | None = None,
        expected_epoch: int | None = None,
        lease_owner: str | None = None,
        lease_seconds: int | None = None,
        payload: Mapping[str, Any] | None = None,
        **extra_kwargs: Any,
    ) -> dict[str, Any]:
        """Create or update a durable graph_operation_journal record in PostgreSQL."""
        if status is not None and state == "claimed":
            if status in ("quarantine", "quarantined"):
                state = "quarantine"
            elif status in ("neo4j_fenced", "planned", "episode_written", "entities_resolved", "edges_resolved", "completed"):
                state = status
            elif status in ("in_progress", "claimed"):
                state = "claimed"
        if expected_epoch is not None and fence_epoch == 0:
            fence_epoch = int(expected_epoch)
        if payload is not None and execution_plan is None:
            execution_plan = dict(payload)
        if operation_type is not None and source_type == "graph_operation":
            source_type = str(operation_type)
        if step_cursor is not None:
            execution_plan = {**dict(execution_plan or {}), "step_cursor": step_cursor}

        with self._scope(conn=conn, write=conn is None and self.connection is None) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO graph_operation_journal (
                        operation_id, outbox_id, group_id, episode_uuid,
                        source_type, source_id, source_revision, content_hash,
                        profile_id, fence_epoch, state, execution_plan, step_receipts,
                        last_error, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s, NOW(), NOW()
                    )
                    ON CONFLICT (operation_id) DO UPDATE SET
                        outbox_id = COALESCE(EXCLUDED.outbox_id, graph_operation_journal.outbox_id),
                        fence_epoch = GREATEST(graph_operation_journal.fence_epoch, EXCLUDED.fence_epoch),
                        state = EXCLUDED.state,
                        execution_plan = CASE
                            WHEN graph_operation_journal.execution_plan = '{}'::jsonb
                            THEN EXCLUDED.execution_plan
                            ELSE graph_operation_journal.execution_plan
                        END,
                        step_receipts = graph_operation_journal.step_receipts || EXCLUDED.step_receipts,
                        last_error = EXCLUDED.last_error,
                        updated_at = NOW()
                    RETURNING operation_id, outbox_id, group_id, episode_uuid,
                              source_type, source_id, source_revision, content_hash,
                              profile_id, fence_epoch, state, execution_plan,
                              step_receipts, last_error
                    """,
                    (
                        operation_id,
                        outbox_id,
                        group_id,
                        episode_uuid,
                        source_type,
                        source_id,
                        source_revision,
                        content_hash,
                        profile_id,
                        int(fence_epoch),
                        state,
                        _json_text(dict(execution_plan or {})),
                        _json_text(dict(step_receipts or {})),
                        last_error,
                    ),
                )
                row = cur.fetchone()
        return {
            "operation_id": str(_row_value(row, "operation_id", 0)),
            "outbox_id": _row_value(row, "outbox_id", 1),
            "group_id": str(_row_value(row, "group_id", 2)),
            "episode_uuid": str(_row_value(row, "episode_uuid", 3)),
            "source_type": str(_row_value(row, "source_type", 4)),
            "source_id": str(_row_value(row, "source_id", 5)),
            "source_revision": str(_row_value(row, "source_revision", 6)),
            "content_hash": str(_row_value(row, "content_hash", 7)),
            "profile_id": str(_row_value(row, "profile_id", 8)),
            "fence_epoch": int(_row_value(row, "fence_epoch", 9) or 0),
            "state": str(_row_value(row, "state", 10)),
            "execution_plan": _json_value(_row_value(row, "execution_plan", 11)),
            "step_receipts": _json_value(_row_value(row, "step_receipts", 12)),
            "last_error": _optional_str(_row_value(row, "last_error", 13)),
            "status": str(_row_value(row, "state", 10)),
            "step_receipts_json": _json_value(_row_value(row, "step_receipts", 12)),
            "quarantine_reason": _optional_str(_row_value(row, "last_error", 13)),
        }

    def record_graph_operation_step(
        self,
        *,
        operation_id: str,
        step_id: str,
        effect_hash: str,
        completed: bool = False,
        status: str | None = None,
        error: str | None = None,
        conn: Any | None = None,
    ) -> str:
        """Record or verify a step receipt in graph_operation_journal.

        Returns:
            'noop' if receipt already exists with same effect_hash,
            'quarantined' if receipt exists with mismatched effect_hash or explicitly quarantined,
            'executed' if receipt was newly recorded.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._scope(conn=conn, write=conn is None and self.connection is None) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT state, step_receipts, fence_epoch, last_error
                      FROM graph_operation_journal
                     WHERE operation_id = %s
                       FOR UPDATE
                    """,
                    (operation_id,),
                )
                row = cur.fetchone()
                if row is None:
                    self.upsert_graph_operation_journal(
                        operation_id=operation_id,
                        group_id="unknown",
                        state="claimed",
                        conn=db,
                    )
                    receipts: dict[str, Any] = {}
                    current_state = "claimed"
                else:
                    current_state = str(_row_value(row, "state", 0))
                    raw_rcpts = _json_value(_row_value(row, "step_receipts", 1))
                    receipts = dict(raw_rcpts) if isinstance(raw_rcpts, dict) else {}

                if status in ("quarantine", "quarantined"):
                    cur.execute(
                        """
                        UPDATE graph_operation_journal
                           SET state = 'quarantine',
                               last_error = COALESCE(%s, last_error),
                               updated_at = NOW()
                         WHERE operation_id = %s
                        """,
                        (error or "step_quarantined", operation_id),
                    )
                    return "quarantined"

                if step_id in receipts:
                    entry = receipts[step_id]
                    recorded_hash = str(entry.get("effect_hash") or "") if isinstance(entry, dict) else str(entry)
                    if recorded_hash != effect_hash:
                        reason = error or f"step={step_id} effect_hash mismatch: recorded={recorded_hash} vs current={effect_hash}"
                        cur.execute(
                            """
                            UPDATE graph_operation_journal
                               SET state = 'quarantine',
                                   last_error = %s,
                                   updated_at = NOW()
                             WHERE operation_id = %s
                            """,
                            (reason, operation_id),
                        )
                        return "quarantined"
                    return "noop"

                receipts[step_id] = {
                    "effect_hash": effect_hash,
                    "recorded_at": now_iso,
                }
                new_state = "completed" if completed else (
                    current_state if current_state not in ("claimed", "planned") else "entities_resolved"
                )
                cur.execute(
                    """
                    UPDATE graph_operation_journal
                       SET step_receipts = %s::jsonb,
                           state = %s,
                           updated_at = NOW()
                     WHERE operation_id = %s
                    """,
                    (_json_text(receipts), new_state, operation_id),
                )
                return "executed"

    def get_graph_operation_journal(
        self,
        operation_id: str,
        *,
        conn: Any | None = None,
    ) -> dict[str, Any] | None:
        with self._scope(conn=conn) as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT operation_id, outbox_id, group_id, episode_uuid,
                           source_type, source_id, source_revision, content_hash,
                           profile_id, fence_epoch, state, execution_plan,
                           step_receipts, last_error
                      FROM graph_operation_journal
                     WHERE operation_id = %s
                    """,
                    (operation_id,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return {
            "operation_id": str(_row_value(row, "operation_id", 0)),
            "outbox_id": _row_value(row, "outbox_id", 1),
            "group_id": str(_row_value(row, "group_id", 2)),
            "episode_uuid": str(_row_value(row, "episode_uuid", 3)),
            "source_type": str(_row_value(row, "source_type", 4)),
            "source_id": str(_row_value(row, "source_id", 5)),
            "source_revision": str(_row_value(row, "source_revision", 6)),
            "content_hash": str(_row_value(row, "content_hash", 7)),
            "profile_id": str(_row_value(row, "profile_id", 8)),
            "fence_epoch": int(_row_value(row, "fence_epoch", 9) or 0),
            "state": str(_row_value(row, "state", 10)),
            "execution_plan": _json_value(_row_value(row, "execution_plan", 11)),
            "step_receipts": _json_value(_row_value(row, "step_receipts", 12)),
            "last_error": _optional_str(_row_value(row, "last_error", 13)),
            "status": str(_row_value(row, "state", 10)),
            "step_receipts_json": _json_value(_row_value(row, "step_receipts", 12)),
            "quarantine_reason": _optional_str(_row_value(row, "last_error", 13)),
        }

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
        if card.currentness not in {"current", "superseded", "stale", "conflicted", "unknown", "retired"}:
            raise ValueError(f"Invalid currentness: {card.currentness}")
        if card.freshness_state not in {
            "verified_current", "verified_changed", "unchecked",
            "check_due", "unverifiable", "unknown",
        }:
            raise ValueError(f"Invalid freshness_state: {card.freshness_state}")
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


_STEWARD_BUNDLE_SQL_FIELDS = (
    'memory_id', 'project', 'card_type', 'title', 'summary', 'typed_payload',
    'lifecycle_state', 'authorization_status', 'currentness', 'confidence',
    'valid_from', 'valid_to', 'content_hash', 'source_ref',
)


def _validate_steward_bundle_scope(
    project: str, brain_id: str, scope: str, member_ids: Any,
) -> list[str]:
    if any(not isinstance(value, str) or not value for value in (project, brain_id, scope)):
        raise ValueError('steward bundle project, brain_id and scope required')
    if len(project) > 64 or not isinstance(member_ids, (list, tuple)):
        raise ValueError('invalid steward bundle scope or member identities')
    if (any(not isinstance(value, str) or not 1 <= len(value) <= 64 for value in member_ids)
            or len(set(member_ids)) != len(member_ids)):
        raise ValueError('unique steward bundle member identities required')
    return list(member_ids)


def _steward_bundle_record(row: Any) -> dict[str, Any]:
    values = {name: _row_value(row, name, index)
              for index, name in enumerate(_STEWARD_BUNDLE_SQL_FIELDS, 1)}
    values['confidence'] = float(values['confidence'])
    for name in ('valid_from', 'valid_to'):
        values[name] = (None if values[name] is None else
                        _as_aware_datetime(values[name]).astimezone(timezone.utc).isoformat())
    for name in ('typed_payload', 'source_ref'):
        values[name] = _json_value(values[name])
    return {'card': _json_value(_row_value(row, 'steward_envelope', 0)), 'sql': values}


_CARD_COLUMNS = (
    "memory_id, project, card_type, title, summary, typed_payload, "
    "lifecycle_state, authorization_status, currentness, confidence, valid_from, valid_to, "
    "embedding_model, embedding_revision, embedding_state, embedding, content_hash, source_ref, "
    "created_at, updated_at, "
    "freshness_state, freshness_checked_at, freshness_next_check_after, source_hash, observed_at"
)
_CARD_INSERT_SQL = f"""
    INSERT INTO memory_cards ({_CARD_COLUMNS})
    VALUES (
        %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s::halfvec, %s, %s::jsonb, %s, %s,
        %s, %s, %s, %s, %s
    )
"""
_CARD_UPSERT_SQL = f"""
    INSERT INTO memory_cards ({_CARD_COLUMNS})
    VALUES (
        %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s::halfvec, %s, %s::jsonb, %s, %s,
        %s, %s, %s, %s, %s
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
        updated_at = EXCLUDED.updated_at,
        freshness_state = EXCLUDED.freshness_state,
        freshness_checked_at = EXCLUDED.freshness_checked_at,
        freshness_next_check_after = EXCLUDED.freshness_next_check_after,
        source_hash = EXCLUDED.source_hash,
        observed_at = EXCLUDED.observed_at
    WHERE memory_cards.lifecycle_state NOT IN ('accepted', 'human_accepted', 'auto_accepted')
"""

_OUTBOX_SELECT = """
    SELECT outbox_id, target_type, target_id, content_hash, payload_text,
           status, claimed_at, lease_until, worker_id, retry_count, last_error,
           created_at, updated_at
      FROM embedding_outbox
"""


def _context_query_tokens(query: Any) -> list[str]:
    return list(dict.fromkeys(
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9가-힣_-]+", str(query or ""))
        if len(token) > 1
    ))


def _context_query_card_types(terms: list[str]) -> list[str]:
    requested = set(terms)
    families = (
        ({"task", "tasks", "work", "work_unit", "작업"}, ("task", "work", "work_unit")),
        ({"decision", "decisions", "결정"}, ("decision",)),
        ({"status", "statuses", "상태"}, ("status",)),
        ({"preference", "preferences", "guardrail", "guardrails", "선호", "제약"}, ("preference",)),
    )
    return [card_type for aliases, card_types in families if requested & aliases for card_type in card_types]


def _require_same_supersede_scope(card: Mapping[str, Any], target: Mapping[str, Any] | None) -> None:
    if target is None or any(
        not card.get(field) or card.get(field) != target.get(field)
        for field in ("project", "brain_id", "scope")
    ):
        raise ValueError("supersede requires matching project, brain_id, and scope")


_STATUS_EXPIRY_FILTER = r"""(
    coalesce(card_type, '') <> 'status'
    OR NULLIF(typed_payload->>'expires_at', '') IS NULL
    OR CASE
        WHEN typed_payload->>'expires_at' ~
             '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$'
        THEN (typed_payload->>'expires_at')::timestamptz > %s
        ELSE FALSE
       END
)"""


def _authority_filter(project: str, as_of: Any) -> tuple[list[str], list[Any]]:
    instant = _parse_as_of(as_of) or datetime.now(timezone.utc)
    return [
        "project = %s", "authorization_status = 'active'",
        "currentness IN ('current', 'superseded')" if as_of else "currentness = 'current'",
        "lifecycle_state IN ('accepted', 'human_accepted', 'auto_accepted')",
        "valid_from <= %s", "(valid_to IS NULL OR valid_to > %s)",
        _STATUS_EXPIRY_FILTER,
    ], [project, instant, instant, instant]


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
        card.freshness_state or "unchecked",
        card.freshness_checked_at,
        card.freshness_next_check_after,
        card.source_hash,
        card.observed_at,
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


def _local_vector_literal(vector: list[float] | tuple[float, ...] | None) -> str:
    if vector is None:
        raise ValueError("local embedding vector must not be None")
    _validate_local_vector(vector)
    return "[" + ",".join(format(float(value), ".17g") for value in vector) + "]"


def _validate_vector(vector: Any) -> None:
    if not isinstance(vector, (list, tuple)) or len(vector) != VECTOR_DIMENSION:
        length = len(vector) if isinstance(vector, (list, tuple)) else "unknown"
        raise ValueError(f"Vector dimension mismatch: expected {VECTOR_DIMENSION}, got {length}")
    for value in vector:
        if not math.isfinite(float(value)):
            raise ValueError("vector values must be finite numbers")


def _validate_local_vector(vector: Any) -> None:
    if not isinstance(vector, (list, tuple)) or len(vector) != LOCAL_VECTOR_DIMENSION:
        length = len(vector) if isinstance(vector, (list, tuple)) else "unknown"
        raise ValueError(
            f"Local vector dimension mismatch: expected {LOCAL_VECTOR_DIMENSION}, got {length}"
        )
    norm_sq = 0.0
    for value in vector:
        fval = float(value)
        if not math.isfinite(fval):
            raise ValueError("local vector values must be finite numbers")
        norm_sq += fval * fval
    if math.sqrt(norm_sq) <= 0.0:
        raise ValueError("local vector norm must be positive")


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


def _row_value(row: Any, key: str, index: int, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    if hasattr(row, "__len__") and index >= len(row):
        return default
    try:
        return row[index]
    except (IndexError, TypeError):
        return default


def _card_from_row(row: Any) -> MemoryCard:
    freshness_val = _row_value(row, "freshness_state", 20, default="unchecked")
    freshness_state = str(freshness_val) if freshness_val else "unchecked"
    raw_source_hash = _row_value(row, "source_hash", 23, default=None)
    source_hash = str(raw_source_hash) if raw_source_hash is not None else None

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
        freshness_state=freshness_state,
        freshness_checked_at=_as_optional_datetime(_row_value(row, "freshness_checked_at", 21)),
        freshness_next_check_after=_as_optional_datetime(_row_value(row, "freshness_next_check_after", 22)),
        source_hash=source_hash,
        observed_at=_as_optional_datetime(_row_value(row, "observed_at", 24)),
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
        observed_at=_as_optional_datetime(_row_value(row, "observed_at", 14)),
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


def _local_outbox_from_row(row: Any) -> LocalOutboxJob:
    return LocalOutboxJob(
        outbox_id=int(_row_value(row, "outbox_id", 0)),
        target_type=str(_row_value(row, "target_type", 1)),
        target_id=str(_row_value(row, "target_id", 2)),
        profile_id=str(_row_value(row, "profile_id", 3)),
        source_content_hash=str(_row_value(row, "source_content_hash", 4)),
        input_hash=str(_row_value(row, "input_hash", 5)),
        payload_text=str(_row_value(row, "payload_text", 6)),
        incarnation=int(_row_value(row, "incarnation", 7, default=1)),
        status=str(_row_value(row, "status", 8)),
        fencing_token=int(_row_value(row, "fencing_token", 9, default=0)),
        worker_id=_optional_str(_row_value(row, "worker_id", 10)),
        claimed_at=_as_optional_datetime(_row_value(row, "claimed_at", 11)),
        lease_until=_as_optional_datetime(_row_value(row, "lease_until", 12)),
        retry_count=int(_row_value(row, "retry_count", 13, default=0)),
        next_attempt_at=_as_optional_datetime(_row_value(row, "next_attempt_at", 14)),
        last_error=_optional_str(_row_value(row, "last_error", 15)),
        created_at=_as_aware_datetime(_row_value(row, "created_at", 16)),
        updated_at=_as_aware_datetime(_row_value(row, "updated_at", 17)),
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
    "SessionChunkMeta",
    "OutboxJob",
    "LocalOutboxJob",
    "PgVectorStore",
    "compute_cosine_similarity",
    "make_dummy_vector",
    "compute_identity_lock_id",
    "card_identity_lock_id",
    "session_identity_lock_id",
    "target_advisory_lock_id",
]
