"""PG session-memory projector for the postgres_pgvector backend.

Derives a deterministic chunk_id within 64 chars from a scoped identity
digest (project/provider/session/source/content/embedding profile).
Validates ownership, content, and embedding profile before reuse.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sys
from typing import Any

from ..couchdb_source.document_model import assert_index_target_allowed, sha256_hash
from ..local_embedding.canonical import canonical_input_hash_v1, canonical_session_input_v1
from ..model_connectors import (
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_PROFILE_ID,
    LOCAL_EMBEDDING_DIM,
    LOCAL_EMBEDDING_MODEL,
    LOCAL_EMBEDDING_PROFILE_ID,
)
from ..postgres_store.pgvector_store import PgVectorStore, SessionChunk, _as_optional_datetime
from .pg_embedding_privacy import assert_pg_embedding_egress_safe
from .pg_embedding_chunks import (
    CHUNK_BYTES, CHUNKED_PROFILE, INVALID_TEXT_NUL,
    InvalidProjectionText, assert_pg_text_safe,
)


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


def _pg_identity_lock(*, session_id_hash: str, project: str, provider: str,
                      content_hash: str, embedding_model: str) -> str:
    """Shared absence fence, including ready-body reuse across source revisions."""
    return hashlib.sha256(
        json.dumps(
            [session_id_hash, project, provider, content_hash, embedding_model],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def validated_pg_embedding_profile(provider: Any) -> str:
    model_and_size = (getattr(provider, "model", ""), getattr(provider, "size", 0))
    if model_and_size == (DEFAULT_EMBEDDING_MODEL, DEFAULT_EMBEDDING_DIM):
        return DEFAULT_EMBEDDING_PROFILE_ID
    if model_and_size == (LOCAL_EMBEDDING_MODEL, LOCAL_EMBEDDING_DIM):
        return LOCAL_EMBEDDING_PROFILE_ID
    raise ValueError("unsupported PG embedding profile")


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
    try:
        assert_pg_text_safe(current.body, body, current.session_id_hash,
                            current.project, current.provider, current.source_hash)
    except InvalidProjectionText:
        if dry_run:
            raise
        status, _state = _commit_projection_state_if_source_current(
            materialized=current, store=source_store, projection_status="failed",
            failure_reason=INVALID_TEXT_NUL, failure_source_hash=current.source_hash,
            backend="postgres_pgvector",
        )
        return {"status": "failed", "reason": (
            status if status == "source_revision_changed" else INVALID_TEXT_NUL), "ref": ""}
    b_hash = sha256_hash(body)
    # Preserve the source's observation time (see project() for rationale).
    observed_raw = (
        getattr(current, "observed_at_start", None)
        or getattr(current, "observed_at_end", None)
        or getattr(current, "materialized_at", None)
    )
    observed_at = _as_optional_datetime(observed_raw)
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

    # Share the normal projector's identity fence before any absence/reuse read.
    # Exclude source revision so same-body renewals serialize too. All writers
    # acquire this advisory lock before row writes; never upsert an existing row.
    identity_lock = _pg_identity_lock(
        session_id_hash=current.session_id_hash, project=current.project,
        provider=current.provider, content_hash=b_hash, embedding_model=embed_provider.model,
    )
    with sql_store.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (identity_lock,))
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
                observed_at=observed_at,
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

    def __init__(
        self,
        store: PgVectorStore,
        embed_provider: Any,
        *,
        chunk_embedder=None,
        allow_deferred_on_model_unavailable: bool = True,
    ) -> None:
        self._profile = validated_pg_embedding_profile(embed_provider)
        self._store = store
        self._embed = embed_provider
        self._chunk_embedder = chunk_embedder
        self._allow_deferred = bool(allow_deferred_on_model_unavailable)
        self.last_local_representation_receipt: dict[str, Any] | None = None
        self.last_local_identity_scheme: str | None = None
        self.last_local_identity_bypassed_rows: int = 0
        self.last_local_identity_resolution: str = ""

    @property
    def embedding_profile(self) -> str:
        return self._profile

    def project(self, *, target_profile: str, document: dict[str, Any]) -> str:
        self.last_local_identity_bypassed_rows = 0
        self.last_local_identity_resolution = ""
        assert_index_target_allowed(target_profile)

        body = str(document.get("body") or "")
        content_hash = str(document.get("content_hash") or "")
        if content_hash != sha256_hash(body):
            raise ValueError("PG content hash mismatch")
        session_id_hash = str(document.get("session_id_hash") or "")
        project = str(document.get("project") or "")
        provider = str(document.get("provider") or "unspecified")
        source_hash = str(document.get("source_hash") or "")
        # Preserve the source's observation time. Never fall back to now(): an
        # unknown age must stay unknown rather than masquerade as fresh.
        observed_raw = (
            document.get("observed_at_start")
            or document.get("observed_at_end")
            or document.get("materialized_at")
        )
        observed_at = _as_optional_datetime(observed_raw)
        assert_pg_text_safe(body, session_id_hash, project, provider, source_hash, content_hash)
        if self._profile == LOCAL_EMBEDDING_PROFILE_ID:
            return self._project_local_profile(
                body=body,
                content_hash=content_hash,
                session_id_hash=session_id_hash,
                project=project,
                provider=provider,
                source_hash=source_hash,
                observed_at=observed_at,
            )
        embedding_model = getattr(self._embed, "model", "")
        embedding_size = getattr(self._embed, "size", 0)
        # len(text) is a cheap lower bound; only small strings need a byte count.
        chunked = len(body) > CHUNK_BYTES or len(body.encode("utf-8")) > CHUNK_BYTES
        embedding_profile = CHUNKED_PROFILE if chunked else self._profile
        embedding_revision = 2 if chunked else 1

        chunk_id = _derive_pg_chunk_id_impl(
            project=project,
            provider=provider,
            session_id_hash=session_id_hash,
            source_hash=source_hash,
            content_hash=content_hash,
            embedding_profile=embedding_profile,
        )
        identity_lock = _pg_identity_lock(
            session_id_hash=session_id_hash, project=project, provider=provider,
            content_hash=content_hash, embedding_model=embedding_model,
        )

        def validate_identity(row: SessionChunk) -> None:
            if (row.session_id_hash, row.project, row.provider, row.content_hash,
                row.content_markdown, row.embedding_model) != (
                session_id_hash, project, provider, content_hash, body, embedding_model
            ):
                raise ValueError("PG chunk identity mismatch")

        def reuse_ready(row: SessionChunk | None) -> str | None:
            if row is None or row.embedding_state != "ready":
                return None
            if row.embedding_revision != embedding_revision:
                return None
            validate_identity(row)
            if row.embedding is None or len(row.embedding) != embedding_size:
                raise ValueError("PG ready chunk vector mismatch")
            return row.chunk_id

        reused = reuse_ready(self._store.get_chunk(chunk_id))
        if reused is not None:
            return reused
        reused = None if chunked else reuse_ready(
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
        if chunked:
            if self._chunk_embedder is None:
                raise ValueError("bounded PG chunk embedder is required for large input")
            vector = self._chunk_embedder.embed(body, content_hash=content_hash)
        else:
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
            embedding_revision=embedding_revision,
            observed_at=observed_at,
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
            concurrent = None if chunked else reuse_ready(
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

    def _project_local_profile(
        self,
        *,
        body: str,
        content_hash: str,
        session_id_hash: str,
        project: str,
        provider: str,
        source_hash: str,
        observed_at: Any,
    ) -> str:
        from .pg_representation import (
            LOCAL_SESSION_REPRESENTATION_KIND,
            build_local_representation_receipt_v3,
            derive_local_pg_chunk_id,
        )

        embedding_size = getattr(self._embed, "size", 0)
        if embedding_size != LOCAL_EMBEDDING_DIM:
            raise ValueError(f"local embedding provider has invalid size: {embedding_size}")

        assert_pg_embedding_egress_safe(body)
        input_text = canonical_session_input_v1(body)
        input_hash = canonical_input_hash_v1(input_text)
        body_bytes = len(body.encode("utf-8"))
        is_over_8k = body_bytes > CHUNK_BYTES

        legacy_v1_id = (
            _derive_pg_chunk_id_impl(
                project=project,
                provider=provider,
                session_id_hash=session_id_hash,
                source_hash=source_hash,
                content_hash=content_hash,
                embedding_profile=DEFAULT_EMBEDDING_PROFILE_ID,
            )
            if source_hash
            else ""
        )
        legacy_v2_id = (
            _derive_pg_chunk_id_impl(
                project=project,
                provider=provider,
                session_id_hash=session_id_hash,
                source_hash=source_hash,
                content_hash=content_hash,
                embedding_profile=CHUNKED_PROFILE,
            )
            if source_hash
            else ""
        )
        local_v1_id = (
            derive_local_pg_chunk_id(
                project=project,
                provider=provider,
                session_id_hash=session_id_hash,
                source_hash=source_hash,
                content_hash=content_hash,
                embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
            )
            if source_hash
            else ""
        )

        def _validate_row_scope_and_body(row: SessionChunk) -> None:
            if (
                row.session_id_hash,
                row.project,
                row.provider,
                row.content_hash,
                row.content_markdown,
            ) != (
                session_id_hash,
                project,
                provider,
                content_hash,
                body,
            ):
                raise ValueError("PG chunk identity conflict: row scope or body mismatch")

        with self._store.transaction() as conn:
            self._store.acquire_session_identity_lock(
                project=project,
                provider=provider,
                session_id_hash=session_id_hash,
                content_hash=content_hash,
                conn=conn,
            )
            scope_rows = self._store.find_session_chunks_by_natural_scope(
                session_id_hash=session_id_hash,
                project=project,
                provider=provider,
                content_hash=content_hash,
                conn=conn,
            )
            candidate_rows: dict[str, SessionChunk] = {
                srow.chunk_id: srow for srow in scope_rows
            }
            for cand_id in (legacy_v1_id, legacy_v2_id, local_v1_id):
                if cand_id and cand_id not in candidate_rows:
                    direct_row = self._store.get_chunk(cand_id, conn=conn)
                    if direct_row is not None:
                        candidate_rows[cand_id] = direct_row

            valid_rows: dict[str, SessionChunk] = {}
            for candidate_id, candidate in candidate_rows.items():
                try:
                    _validate_row_scope_and_body(candidate)
                except ValueError:
                    continue
                valid_rows[candidate_id] = candidate

            exact_legacy_matches = [
                cid for cid in (legacy_v1_id, legacy_v2_id) if cid and cid in valid_rows
            ]
            identity_resolution = ""
            if local_v1_id and local_v1_id in candidate_rows:
                chosen_id = local_v1_id
                identity_scheme = "local_v1"
                existing_parent = True
                if len(candidate_rows) > 1:
                    identity_resolution = "local_v1_preferred"
            elif len(exact_legacy_matches) == 1 and len(valid_rows) == 1:
                chosen_id = exact_legacy_matches[0]
                identity_scheme = "legacy_exact"
                existing_parent = True
            elif len(valid_rows) == 1 and not exact_legacy_matches and not is_over_8k:
                single_row = next(iter(valid_rows.values()))
                chosen_id = single_row.chunk_id
                identity_scheme = "legacy_reuse"
                existing_parent = True
            else:
                if not source_hash or not local_v1_id:
                    raise ValueError(
                        "PG chunk identity conflict: source_hash is required to create a new local_v1 chunk"
                    )
                chosen_id = local_v1_id
                identity_scheme = "local_v1"
                existing_parent = False
                if candidate_rows:
                    identity_resolution = "new_local_v1_over_unverifiable_legacy"

            self.last_local_identity_bypassed_rows = len(candidate_rows) - int(
                existing_parent
            )
            if existing_parent:
                _validate_row_scope_and_body(candidate_rows[chosen_id])
            self.last_local_identity_resolution = identity_resolution
            if identity_resolution:
                print(
                    "[pg-identity] "
                    f"resolution={identity_resolution} "
                    f"bypassed={self.last_local_identity_bypassed_rows}",
                    file=sys.stderr,
                )

            if existing_parent:
                outbox_id = self._store._enqueue_local_outbox_on(
                    conn,
                    target_type="session_chunk",
                    target_id=chosen_id,
                    source_content_hash=content_hash,
                    payload_text=input_text,
                    profile_id=LOCAL_EMBEDDING_PROFILE_ID,
                    acquire_locks=False,
                )
            else:
                new_chunk = SessionChunk(
                    chunk_id=chosen_id,
                    session_id_hash=session_id_hash,
                    project=project,
                    provider=provider,
                    chunk_index=0,
                    content_markdown=body,
                    content_hash=content_hash,
                    embedding_state="pending",
                    embedding=None,
                    embedding_model=LOCAL_EMBEDDING_MODEL,
                    embedding_revision=1,
                    observed_at=observed_at,
                )
                _, outbox_id = self._store.insert_chunk_with_local_outbox(
                    new_chunk,
                    conn=conn,
                    profile_id=LOCAL_EMBEDDING_PROFILE_ID,
                    already_locked_identity=True,
                )

        # Model call happens strictly outside the PG transaction.
        # If the model service is temporarily unavailable and deferred mode is enabled,
        # the parent row and durable local_embedding_outbox job remain committed.
        if outbox_id is not None and callable(getattr(self._embed, "embed", None)):
            from ..postgres_store.outbox_worker import LocalEmbeddingOutboxWorker

            worker = LocalEmbeddingOutboxWorker(
                store=self._store,
                embedding_fn=self._embed.embed,
                batch_size=1,
                lease_seconds=60,
                embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
            )
            worker.run_once()
            if worker.last_batch_failed and not self._allow_deferred:
                raise RuntimeError("local embedding model call failed during projection")

        stored = self._store.get_chunk(chosen_id)
        if stored is None:
            raise RuntimeError(f"chunk {chosen_id} missing after local projection commit")
        _validate_row_scope_and_body(stored)

        local_row = self._store.get_local_embedding_row(
            target_type="session_chunk",
            target_id=chosen_id,
            embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
        )
        if (
            local_row is None
            or local_row.get("state") != "ready"
            or local_row.get("source_content_hash") != content_hash
            or local_row.get("input_hash") != input_hash
        ):
            from .pg_embedding_chunks import EmbeddingChunksPending

            self.last_local_representation_receipt = None
            raise EmbeddingChunksPending()

        self.last_local_identity_scheme = identity_scheme
        if source_hash:
            self.last_local_representation_receipt = build_local_representation_receipt_v3(
                representation_kind=LOCAL_SESSION_REPRESENTATION_KIND,
                project=project,
                provider=provider,
                session_id_hash=session_id_hash,
                active_content_hash=content_hash,
                projected_source_hash=source_hash,
                representation_content_hash=content_hash,
                session_memory_knowledge_id=chosen_id,
                identity_scheme=identity_scheme,
                embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
                input_hash=input_hash,
            )
        else:
            self.last_local_representation_receipt = None
        return chosen_id

    def close(self) -> None:
        if self._chunk_embedder is not None:
            self._chunk_embedder.close()
        embed_close = getattr(self._embed, "close", None)
        if callable(embed_close):
            embed_close()
