"""PG session-memory projector for the postgres_pgvector backend.

Derives a deterministic chunk_id within 64 chars from a scoped identity
digest (project/provider/session/source/content/embedding profile).
Validates ownership, content, and embedding profile before reuse.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..couchdb_source.document_model import assert_index_target_allowed, sha256_hash
from ..model_connectors import (
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_PROFILE_ID,
)
from ..postgres_store.pgvector_store import PgVectorStore, SessionChunk
from .pg_embedding_privacy import assert_pg_embedding_egress_safe


def _derive_pg_chunk_id_impl(
    *,
    project: str,
    provider: str,
    session_id_hash: str,
    source_hash: str,
    content_hash: str,
    embedding_profile: str = "",
) -> str:
    """64-char deterministic chunk_id from scoped identity."""
    profile = embedding_profile or DEFAULT_EMBEDDING_PROFILE_ID
    identity = json.dumps([
        str(project or ""),
        str(provider or ""),
        str(session_id_hash or ""),
        str(source_hash or ""),
        str(content_hash or ""),
        profile,
    ], ensure_ascii=False, separators=(",", ":"))
    # sha256 hex = 64 chars exactly, fits the constraint
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def validated_pg_embedding_profile(provider: Any) -> str:
    if (getattr(provider, "model", ""), getattr(provider, "size", 0)) != (
        DEFAULT_EMBEDDING_MODEL, DEFAULT_EMBEDDING_DIM
    ):
        raise ValueError("unsupported PG embedding profile")
    return DEFAULT_EMBEDDING_PROFILE_ID


def normalized_pg_halfvec(sql_store, vector) -> list[float]:
    """Canonical readback using INSERT's literal and PostgreSQL halfvec cast.

    Executes only SELECT; callers may reuse this for read-only preflight.
    Python binary16 can differ at float32 double-rounding boundaries.
    """
    from ..postgres_store.pgvector_store import _parse_vector, _vector_literal

    with sql_store.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT %s::halfvec AS embedding", (_vector_literal(vector),))
            normalized = _parse_vector(cur.fetchone()["embedding"])
            # PostgreSQL text->float32->halfvec can underflow even when a
            # direct Python binary16 cast is nonzero. Cosine requires a norm.
            import math
            if (normalized is None or not normalized
                or not all(math.isfinite(value) for value in normalized)
                or not any(value != 0.0 for value in normalized)):
                raise ValueError("PG normalized vector has no finite nonzero norm")
            return normalized


def project_pg_representation(*, materialized, source_store, sql_store, embed_provider, body: str,
                              expected_vector=None, dry_run: bool = False) -> dict:
    """Local-only verified transform -> SQL readback -> current-source CAS.

    Recompute the complete authority materialization; never relabel its A as B.
    No hash override, provenance claim, or arbitrary transform is accepted.
    The separate legacy importer validates context before supplying its adapter
    and expected_vector. The latter fences collisions and halfvec readback.
    """
    from ..couchdb_source.session_memory_materializer import (
        materialize_session_memory, _commit_projection_state_if_source_current,
    )
    from .qdrant_backfill import public_safe_mask_body
    from .pg_representation import REPRESENTATION_KIND, mapping_digest

    profile = validated_pg_embedding_profile(embed_provider)
    assert_index_target_allowed(materialized.target_profile)
    current = materialize_session_memory(session_id_hash=materialized.session_id_hash, store=source_store)
    fields = ("session_id_hash", "provider", "project", "target_profile", "body", "content_hash",
              "source_hash", "fully_materialized", "conversation_chunk_count", "tool_evidence_bundle_count")
    if (not current.fully_materialized or not materialized.fully_materialized
        or any(getattr(current, key) != getattr(materialized, key) for key in fields)
        or sha256_hash(materialized.body) != materialized.content_hash
        or not isinstance(body, str) or body != public_safe_mask_body(current.body)):
        raise ValueError("PG representation verification failed")
    b_hash = sha256_hash(body)
    chunk_id = _derive_pg_chunk_id_impl(
        project=current.project, provider=current.provider, session_id_hash=current.session_id_hash,
        source_hash=current.source_hash, content_hash=b_hash, embedding_profile=profile,
    )
    mapping = dict(
        receipt_version=2, representation_kind=REPRESENTATION_KIND, embedding_profile=profile,
        session_id_hash=current.session_id_hash, provider=current.provider, project=current.project,
        active_content_hash=current.content_hash, projected_source_hash=current.source_hash,
        representation_content_hash=b_hash, session_memory_knowledge_id=chunk_id,
    )
    mapping["provenance_digest"] = mapping_digest(mapping)

    normalized_expected = (normalized_pg_halfvec(sql_store, expected_vector)
                           if expected_vector is not None else None)

    def validate(row, *, supplied=False):
        if (row.session_id_hash, row.project, row.provider, row.content_hash, row.content_markdown,
            row.embedding_model, row.embedding_state) != (
            current.session_id_hash, current.project, current.provider, b_hash, body,
            embed_provider.model, "ready"
        ) or row.embedding is None or len(row.embedding) != embed_provider.size:
            raise ValueError("PG representation row mismatch")
        if expected_vector is not None:
            # Fresh adapter output must equal the original supplied vector;
            # existing/reusable/readback rows must equal PostgreSQL's result.
            expected = expected_vector if supplied else normalized_expected
            if list(row.embedding) != list(expected):
                raise ValueError("PG representation vector mismatch")

    def reusable_row(conn=None):
        from ..couchdb_source.document_model import projection_state_doc_id
        from .pg_representation import valid_pg_receipt_metadata

        state = source_store.get(projection_state_doc_id(current.session_id_hash)) or {}
        previous = (state.get("backend_receipts") or {}).get("postgres_pgvector", {})
        # Same-body renewal can reuse only an already verified v2 mapping.
        # Dry-run and apply must inspect the same historical target.
        if (valid_pg_receipt_metadata(previous) and previous.get("receipt_version") == 2
            and previous.get("projection_status") == "projected"
            and all(previous.get(key) == mapping[key] for key in (
                "session_id_hash", "project", "provider", "active_content_hash",
                "representation_content_hash", "embedding_profile", "representation_kind",
            ))):
            return sql_store.get_chunk(previous["session_memory_knowledge_id"], conn=conn)
        return None

    if dry_run:
        row = sql_store.get_chunk(chunk_id)
        if row is None:
            row = reusable_row()
        if row is not None:
            validate(row)
        return {"status": "validated", "reason": "", "ref": ""}

    # Serialize canonical IDs. Never use the adapter's upsert on an existing row.
    with sql_store.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (chunk_id,))
        row = sql_store.get_chunk(chunk_id, conn=conn)
        if row is not None:
            validate(row)
        else:
            reusable = reusable_row(conn=conn)
            if reusable is not None:
                validate(reusable)
                vector = reusable.embedding
            else:
                assert_pg_embedding_egress_safe(body)
                vector = embed_provider.embed(body)
            row = SessionChunk(
                chunk_id=chunk_id, session_id_hash=current.session_id_hash,
                project=current.project, provider=current.provider, content_markdown=body,
                content_hash=b_hash, embedding_model=embed_provider.model,
                embedding_state="ready", embedding=vector,
            )
            validate(row, supplied=reusable is None)
            sql_store.insert_chunk(row, conn=conn, insert_only=True)
    stored = sql_store.get_chunk(chunk_id)
    if stored is None:
        raise ValueError("PG representation readback missing")
    validate(stored)
    status, _state = _commit_projection_state_if_source_current(
        materialized=materialized, store=source_store, projection_status="projected",
        ref=chunk_id, backend="postgres_pgvector", representation_receipt=mapping,
    )
    if status == "source_revision_changed":
        return {"status": "failed", "reason": status, "ref": ""}
    return {"status": "projected", "reason": "", "ref": chunk_id}


class PgSessionMemoryProjector:
    """Project session-memory into the PgVectorStore.

    Validates target_profile ownership, embedding dimension and model,
    and verifies the stored chunk is ready after save.
    """

    def __init__(self, store: PgVectorStore, embed_provider: Any) -> None:
        self._profile = validated_pg_embedding_profile(embed_provider)
        self._store = store
        self._embed = embed_provider

    def project(self, *, target_profile: str, document: dict[str, Any]) -> str:
        assert_index_target_allowed(target_profile)

        body = str(document.get("body") or "")
        content_hash = str(document.get("content_hash") or "")
        if content_hash != sha256_hash(body):
            raise ValueError("PG content hash mismatch")
        session_id_hash = str(document.get("session_id_hash") or "")
        project = str(document.get("project") or "")
        provider = str(document.get("provider") or "unspecified")
        source_hash = str(document.get("source_hash") or "")
        embedding_model = getattr(self._embed, "model", "")
        embedding_size = getattr(self._embed, "size", 0)
        embedding_profile = self._profile

        chunk_id = _derive_pg_chunk_id_impl(
            project=project,
            provider=provider,
            session_id_hash=session_id_hash,
            source_hash=source_hash,
            content_hash=content_hash,
            embedding_profile=embedding_profile,
        )
        identity_lock = hashlib.sha256(
            json.dumps(
                [session_id_hash, project, provider, content_hash, embedding_model],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        def validate_identity(row: SessionChunk) -> None:
            if (row.session_id_hash, row.project, row.provider, row.content_hash,
                row.content_markdown, row.embedding_model) != (
                session_id_hash, project, provider, content_hash, body, embedding_model
            ):
                raise ValueError("PG chunk identity mismatch")

        def reuse_ready(row: SessionChunk | None) -> str | None:
            if row is None or row.embedding_state != "ready":
                return None
            validate_identity(row)
            if row.embedding is None or len(row.embedding) != embedding_size:
                raise ValueError("PG ready chunk vector mismatch")
            return row.chunk_id

        reused = reuse_ready(self._store.get_chunk(chunk_id))
        if reused is not None:
            return reused
        reused = reuse_ready(
            self._store.find_ready_chunk_by_identity(
                session_id_hash=session_id_hash,
                project=project,
                provider=provider,
                content_hash=content_hash,
                embedding_model=embedding_model,
            )
        )
        if reused is not None:
            return reused

        # Validate embedding dimension
        if embedding_size <= 0:
            raise ValueError(f"embedding provider has invalid size: {embedding_size}")

        # Reuse above is private and causes no external egress, even if an
        # existing ready body contains credentials. It does not certify that
        # body's historical embedding provenance. Only new embedding is gated.
        # Reject, never redact: the unchanged body must match its content hash.
        assert_pg_embedding_egress_safe(body)
        vector = self._embed.embed(body)
        if len(vector) != embedding_size:
            raise ValueError(
                f"embedding returned {len(vector)} dims, expected {embedding_size}"
            )

        chunk = SessionChunk(
            chunk_id=chunk_id,
            session_id_hash=session_id_hash,
            project=project,
            provider=provider,
            chunk_index=0,
            content_markdown=body,
            content_hash=content_hash,
            embedding_state="ready",
            embedding=vector,
            embedding_model=embedding_model,
        )
        # Serialize this projection lane per identity, including the absent-row
        # case (a row lock alone cannot fence concurrent first inserts).
        with self._store.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (identity_lock,),
                )
            concurrent = reuse_ready(self._store.get_chunk(chunk_id, conn=conn))
            if concurrent is not None:
                return concurrent
            concurrent = reuse_ready(
                self._store.find_ready_chunk_by_identity(
                    session_id_hash=session_id_hash,
                    project=project,
                    provider=provider,
                    content_hash=content_hash,
                    embedding_model=embedding_model,
                    conn=conn,
                )
            )
            if concurrent is not None:
                return concurrent
            self._store.insert_chunk(chunk, conn=conn)

        # Verify ready readback
        stored = self._store.get_chunk(chunk_id)
        if stored is None or stored.embedding_state != "ready":
            raise RuntimeError(
                f"chunk {chunk_id} not in ready state after insert"
            )

        validate_identity(stored)
        if stored.embedding is None or len(stored.embedding) != embedding_size:
            raise ValueError("PG ready chunk vector mismatch")
        return chunk_id

    def close(self) -> None:
        embed_close = getattr(self._embed, "close", None)
        if callable(embed_close):
            embed_close()
