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
