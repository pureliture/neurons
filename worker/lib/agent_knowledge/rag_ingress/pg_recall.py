"""PG-backed brain.query recall.

By default, PG candidates remain joined to CouchDB projection receipts. During
the explicit migrated-reader cutover, ``PG_RECALL_DIRECT_MIGRATED=true`` serves
ready, integrity-valid PostgreSQL rows directly. No path falls back to Qdrant.
"""

from __future__ import annotations

import base64
from typing import Any, Callable

from ..postgres_store.pgvector_store import PgVectorStore
from ..couchdb_source.document_model import sha256_hash
from .pg_backfill import validated_pg_embedding_profile
from .pg_embedding_privacy import assert_pg_embedding_egress_safe
from ..session_memory.brain_query import project_from_brain_id
from .qdrant_authority_join import join_mirror_hits_to_authority
from .qdrant_couchdb_authority import CouchDBProjectionStateAuthorityResolver


def _direct_migrated_reader_enabled(environ: Any) -> bool:
    return str(environ.get("PG_RECALL_DIRECT_MIGRATED") or "").strip().casefold() == "true"

_SYNTHETIC_CANARY_PROVIDER = "lbrain-temporal-canary"
_RECALL_LIMIT = 5
# Bounded direct-PG integrity validation: inspect at most 100 candidates plus
# one SQL sentinel. If that window cannot produce the requested result count,
# fail explicitly rather than treating truncation as an empty successful recall.
_RECALL_CANDIDATE_LIMIT = 100

BrainQuerySearch = Callable[[str, str], list[dict[str, Any]]]


def _is_synthetic_canary_pg_row(provider: str) -> bool:
    """Keep additive projection canaries out of public PG recall lanes."""
    return provider.strip().casefold() == _SYNTHETIC_CANARY_PROVIDER


def build_pg_brain_query_search_from_env(environ: Any) -> BrainQuerySearch | None:
    """Build the PG recall callable from env, or None when not configured.

    Requires a ``NEURON_LBRAIN_PGVECTOR_DSN``/``LLM_BRAIN_PGVECTOR_DSN``/
    ``NEURON_LEDGER_PG_DSN``.  An absent DSN disables this optional lane. Once
    configured, construction errors are explicit redacted failures, never silent
    fallback.
    """

    dsn = (
        environ.get("NEURON_LBRAIN_PGVECTOR_DSN", "")
        or environ.get("LLM_BRAIN_PGVECTOR_DSN", "")
        or environ.get("NEURON_LEDGER_PG_DSN", "")
    )
    if not dsn:
        return None
    direct_migrated = _direct_migrated_reader_enabled(environ)
    couch_store = None
    embed_provider = None
    try:
        if not direct_migrated:
            couch_url = str(environ.get("COUCHDB_URL") or "").strip()
            if not couch_url:
                raise RuntimeError("PG recall requires CouchDB authority")
            from ..couchdb_source.couchdb_http_store import CouchDBHttpSourceStore

            user = str(environ.get("COUCHDB_USER") or "")
            password = str(environ.get("COUCHDB_PASSWORD") or "")
            auth_header = (
                "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
                if user
                else ""
            )
            couch_store = CouchDBHttpSourceStore(
                base_url=couch_url,
                db=str(environ.get("COUCHDB_DB") or "transcript_source"),
                auth_header=auth_header,
            )
        from .qdrant_embedding import build_openai_embedding_provider

        pg_store = PgVectorStore(
            dsn=dsn,
            pgvector_version=environ.get("LLM_BRAIN_PGVECTOR_VERSION", "0.8.0"),
        )
        embed_provider = build_openai_embedding_provider(environ=environ)
        validated_pg_embedding_profile(embed_provider)
    except Exception:
        closer = getattr(embed_provider, "close", None)
        if callable(closer):
            closer()
        raise RuntimeError("PG recall construction failed") from None

    def _search(query: str, brain_id: str) -> list[dict[str, Any]]:
        project = project_from_brain_id(brain_id)
        if project is None:
            raise RuntimeError("PG recall requires project scope")

        # Query embedding leaves the tailnet too; preserve text or reject it.
        assert_pg_embedding_egress_safe(query)
        vector = embed_provider.embed(query)

        # One bounded SQL result, already ordered by distance then chunk_id.
        # A sentinel distinguishes exhausted input from a truncated candidate set.
        raw_chunks = pg_store.search_session_chunks(
            query_vector=vector, project=project, limit=_RECALL_CANDIDATE_LIMIT + 1
        )

        validated_hits: list[dict[str, Any]] = []
        for candidate in raw_chunks[:_RECALL_CANDIDATE_LIMIT]:
            row = pg_store.get_chunk(candidate["chunk_id"])
            if (
                row is None
                or row.chunk_id != candidate["chunk_id"]
                or row.embedding_state != "ready"
                or row.embedding_model != embed_provider.model
                or row.embedding is None
                or len(row.embedding) != embed_provider.size
                or row.project != project
                or sha256_hash(row.content_markdown) != row.content_hash
                or any(
                    getattr(row, key) != candidate[key]
                    for key in (
                        "session_id_hash",
                        "content_hash",
                        "content_markdown",
                        "provider",
                        "project",
                    )
                )
                or _is_synthetic_canary_pg_row(row.provider)
            ):
                continue
            validated_hits.append({
                "session_id_hash": row.session_id_hash,
                "content_hash": row.content_hash,
                "memory_id": row.chunk_id,
                "summary": row.content_markdown,
                "provider": row.provider,
                "project": row.project,
                "score": 1.0 - float(candidate["distance"]),
            })

        if direct_migrated:
            authorized_hits = validated_hits
        else:
            resolver = CouchDBProjectionStateAuthorityResolver(
                couch_store, filters={"project": project}, backend="postgres_pgvector"
            )
            authorized_hits = join_mirror_hits_to_authority(
                validated_hits, resolver=resolver, drop_unresolved=True
            )

        results = [
            {
                "result_type": "session_memory",
                "retrieval_lane": "pg_semantic",
                "memory_id": str(hit["memory_id"]),
                "card_type": "",
                "summary": str(hit["summary"]),
                "currentness": str(hit.get("authority_currentness") or "current"),
                "score": hit["score"],
                "content_hash": str(hit["content_hash"]),
            }
            for hit in authorized_hits
            if not _is_synthetic_canary_pg_row(str(hit.get("provider") or ""))
        ]
        if len(results) < _RECALL_LIMIT and len(raw_chunks) > _RECALL_CANDIDATE_LIMIT:
            # brain.query maps this to projection_state=unavailable, not fresh [].
            raise RuntimeError("PG recall candidate limit exhausted")
        return results[:_RECALL_LIMIT]

    return _search
