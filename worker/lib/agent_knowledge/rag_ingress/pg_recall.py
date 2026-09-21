"""PG-backed brain.query recall.

Fills brain.query's ``archive`` / ``evidence_candidates`` lanes directly from
ready, integrity-valid PgVectorStore rows.  Returns the
``(query, brain_id) -> list[dict]`` shape that
``session_memory.brain_query.build_brain_query_response_v2`` consumes.
"""

from __future__ import annotations

from typing import Any, Callable

from ..postgres_store.pgvector_store import PgVectorStore
from ..couchdb_source.document_model import sha256_hash
from .pg_backfill import validated_pg_embedding_profile
from .pg_embedding_privacy import assert_pg_embedding_egress_safe
from ..session_memory.brain_query import project_from_brain_id

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
    embed_provider = None
    try:
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

        results: list[dict[str, Any]] = []
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
            results.append(
                {
                    "result_type": "session_memory",
                    "retrieval_lane": "pg_semantic",
                    "memory_id": row.chunk_id,
                    "card_type": "",
                    "summary": row.content_markdown,
                    "currentness": "current",
                    "score": 1.0 - float(candidate["distance"]),
                    "content_hash": row.content_hash,
                }
            )
        if len(results) < _RECALL_LIMIT and len(raw_chunks) > _RECALL_CANDIDATE_LIMIT:
            # brain.query maps this to projection_state=unavailable, not fresh [].
            raise RuntimeError("PG recall candidate limit exhausted")
        return results[:_RECALL_LIMIT]

    return _search
