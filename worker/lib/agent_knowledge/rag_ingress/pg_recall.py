"""PG-backed brain.query recall (M8 pgvector cutover).

Fills brain.query's ``archive`` / ``evidence_candidates`` lanes from the
PgVectorStore instead of the Qdrant searchable mirror.  Returns the
``(query, brain_id) -> list[dict]`` shape that
``session_memory.brain_query.build_brain_query_response_v2`` consumes.

Every PG hit is authority-joined through
``CouchDBProjectionStateAuthorityResolver`` (the go-forward CouchDB
authority). A hit needs a backend-scoped receipt, a ready/profile-valid SQL row,
matching body hash and scope, and a still-current source revision. Legacy Qdrant
receipts never authorize PG rows.
"""

from __future__ import annotations

import base64
from typing import Any, Callable

from ..postgres_store.pgvector_store import PgVectorStore
from ..couchdb_source.document_model import sha256_hash
from .qdrant_couchdb_authority import CouchDBProjectionStateAuthorityResolver
from .qdrant_authority_join import join_mirror_hits_to_authority
from .pg_backfill import validated_pg_embedding_profile
from .pg_embedding_privacy import assert_pg_embedding_egress_safe
from ..session_memory.brain_query import project_from_brain_id

_SYNTHETIC_CANARY_PROVIDER = "lbrain-temporal-canary"
_RECALL_LIMIT = 5
# 로컬 보수적 작업 예산: authority 검사는 최대 100건, SQL은 sentinel 1건 추가.
# 기존 top-5 포화를 넘되 CouchDB source 검증 비용은 제한한다. 운영 규모에서
# 충분한 임계값인지는 미검증이며, 부족하면 성공/빈 결과로 위장하지 않는다.
_RECALL_CANDIDATE_LIMIT = 100

BrainQuerySearch = Callable[[str, str], list[dict[str, Any]]]


def _is_synthetic_canary_authority_hit(hit: dict[str, Any]) -> bool:
    """Keep additive projection canaries out of public mirror recall lanes.

    ``provider`` is deliberately read only after ``join_mirror_hits_to_authority``
    has replaced mirror metadata with the CouchDB authority record.  A raw mirror
    payload is not trusted for this exclusion decision.
    """
    return (
        str(hit.get("provider") or "").strip().casefold()
        == _SYNTHETIC_CANARY_PROVIDER
    )


def build_pg_brain_query_search_from_env(environ: Any) -> BrainQuerySearch | None:
    """Build the PG recall callable from env, or None when not configured.

    Requires a ``NEURON_LBRAIN_PGVECTOR_DSN``/``LLM_BRAIN_PGVECTOR_DSN``/
    ``NEURON_LEDGER_PG_DSN`` + ``COUCHDB_URL`` (the authority store).  Reuses the
    existing CouchDB Basic auth configuration (``COUCHDB_USER``/``COUCHDB_PASSWORD``)
    and the ``transcript_source`` default DB.

    An absent DSN disables this optional lane. Once configured, missing authority
    or construction errors are explicit redacted failures, never silent fallback.
    """

    dsn = (
        environ.get("NEURON_LBRAIN_PGVECTOR_DSN", "")
        or environ.get("LLM_BRAIN_PGVECTOR_DSN", "")
        or environ.get("NEURON_LEDGER_PG_DSN", "")
    )
    couch_url = str(environ.get("COUCHDB_URL") or "").strip()
    if not dsn:
        return None
    if not couch_url:
        raise RuntimeError("PG recall requires CouchDB authority")
    embed_provider = None
    try:
        from ..couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
        from .qdrant_embedding import build_openai_embedding_provider

        user = str(environ.get("COUCHDB_USER") or "")
        password = str(environ.get("COUCHDB_PASSWORD") or "")
        db = str(environ.get("COUCHDB_DB") or "transcript_source")
        auth_header = (
            "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
            if user
            else ""
        )
        store = CouchDBHttpSourceStore(
            base_url=couch_url,
            db=db,
            auth_header=auth_header,
        )
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

        # format raw chunks to match expected inputs for join_mirror_hits_to_authority
        raw_hits = []
        for c in raw_chunks[:_RECALL_CANDIDATE_LIMIT]:
            row = pg_store.get_chunk(c["chunk_id"])
            if (
                row is None or row.embedding_state != "ready"
                or row.embedding_model != embed_provider.model
                or row.embedding is None or len(row.embedding) != embed_provider.size
                or sha256_hash(row.content_markdown) != row.content_hash
                or any(getattr(row, key) != c[key] for key in (
                    "session_id_hash", "content_hash", "content_markdown", "provider", "project"
                ))
            ):
                continue
            raw_hits.append({
                "session_id_hash": c["session_id_hash"],
                "content_hash": c["content_hash"],
                "memory_id": c["chunk_id"],
                "score": 1.0 - float(c["distance"]),
                "summary": c.get("content_markdown", ""),
                "provider": c.get("provider", ""),
                "project": c.get("project", ""),
            })

        filters = {"project": project} if project else None
        resolver = CouchDBProjectionStateAuthorityResolver(store, filters=filters, backend="postgres_pgvector")

        joined = join_mirror_hits_to_authority(
            raw_hits, resolver=resolver, drop_unresolved=True
        )

        results: list[dict[str, Any]] = []
        for hit in joined:
            if _is_synthetic_canary_authority_hit(hit):
                continue
            results.append(
                {
                    "result_type": "session_memory",
                    "retrieval_lane": "pg_semantic",
                    "memory_id": str(hit.get("memory_id") or ""),
                    "card_type": "",
                    "summary": str(hit.get("summary") or ""),
                    "currentness": str(hit.get("authority_currentness") or "current"),
                    "score": hit.get("score"),
                    "content_hash": str(hit.get("content_hash") or ""),
                }
            )
        if len(results) < _RECALL_LIMIT and len(raw_chunks) > _RECALL_CANDIDATE_LIMIT:
            # brain.query maps this to projection_state=unavailable, not fresh [].
            raise RuntimeError("PG recall candidate limit exhausted")
        return results[:_RECALL_LIMIT]

    return _search
