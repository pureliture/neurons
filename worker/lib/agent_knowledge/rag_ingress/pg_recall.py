"""PG-backed brain.query recall.

By default, PG candidates remain joined to CouchDB projection receipts. During
the explicit migrated-reader cutover, ``PG_RECALL_DIRECT_MIGRATED=true`` serves
ready, integrity-valid PostgreSQL rows directly. No path falls back to Qdrant.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..postgres_store.pgvector_store import PgVectorStore
from ..llm_brain_core.temporal_envelope import build_temporal_envelope


def _session_temporal_block(
    *,
    observed_at: Any,
    recorded_at: Any,
    valid_from: Any = None,
    valid_to: Any = None,
    as_of: Any = None,
    currentness: str = "current",
    lifecycle_status: str = "accepted",
    projection_lag_seconds: float | int | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build a public-safe 22-field Section 8 temporal block for one PG session chunk.

    Delegates to build_temporal_envelope to guarantee schema parity between card and chunk lanes.
    """
    return build_temporal_envelope(
        observed_at=observed_at,
        recorded_at=recorded_at,
        valid_from=valid_from,
        valid_to=valid_to,
        as_of=as_of,
        currentness=currentness,
        lifecycle_status=lifecycle_status,
        projection_lag_seconds=projection_lag_seconds,
        card_type="",
        **kwargs,
    )


def _age_summary(obs, age_seconds, confidence):
    if obs is None:
        return "나이 불명 (observed_at 없음)"
    if confidence == "anomalous":
        return "시간 이상 (미래 관측 시각)"
    if age_seconds is None:
        return "나이 불명"
    days = age_seconds // 86400
    if days >= 180:
        bucket = "historical"
    elif days >= 30:
        bucket = "old"
    elif days >= 7:
        bucket = "aging"
    elif days >= 1:
        bucket = "recent"
    else:
        bucket = "fresh"
    return f"관측 {days}일 전 · 현재 유효 · 신선도 미검증 ({bucket})"

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

    def _search(
        query: str,
        brain_id: str,
        *,
        as_of: str | None = None,
        include_historical: bool = False,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        project = project_from_brain_id(brain_id)
        if project is None:
            raise RuntimeError("PG recall requires project scope")

        as_of_dt = None
        if as_of:
            from ..llm_brain_core.temporal_envelope import _parse_instant, CLOCK_SKEW_TOLERANCE_SECONDS
            as_of_dt, _ = _parse_instant(as_of)
            if as_of_dt is None:
                raise ValueError("as_of must be an ISO-8601 timestamp")
            now = datetime.now(timezone.utc)
            if as_of_dt > now + timedelta(seconds=CLOCK_SKEW_TOLERANCE_SECONDS):
                raise ValueError(
                    f"invalid_as_of_future: as_of cannot be more than 5 minutes in the future (requested={as_of_dt.isoformat()}, now={now.isoformat()})"
                )

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
                "observed_at": row.observed_at,
                "recorded_at": row.created_at,
                "authority_currentness": getattr(row, "authority_currentness", getattr(row, "currentness", "current")),
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

        all_hits: list[dict[str, Any]] = []
        for hit in authorized_hits:
            if _is_synthetic_canary_pg_row(str(hit.get("provider") or "")):
                continue
            curr = str(hit.get("authority_currentness") or "current")
            temporal_block = _session_temporal_block(
                observed_at=hit.get("observed_at"),
                recorded_at=hit.get("recorded_at"),
                as_of=as_of,
                currentness=curr,
                projection_lag_seconds=hit.get("projection_lag_seconds"),
            )
            all_hits.append({
                "result_type": "session_memory",
                "retrieval_lane": "pg_semantic",
                "memory_id": str(hit["memory_id"]),
                "card_type": "",
                "summary": str(hit["summary"]),
                "currentness": curr,
                "score": hit["score"],
                "content_hash": str(hit["content_hash"]),
                "temporal": temporal_block,
            })

        current_hits: list[dict[str, Any]] = []
        past_hits: list[dict[str, Any]] = []
        for hit in all_hits:
            t = hit["temporal"]
            curr = hit["currentness"]
            if t.get("time_confidence") == "anomalous" or t.get("temporal_relation") == "future":
                continue
            if curr in ("superseded", "stale", "retired"):
                past_hits.append(hit)
            elif curr == "current":
                current_hits.append(hit)
            else:
                past_hits.append(hit)

        if include_historical:
            results = current_hits + past_hits
        elif current_hits:
            results = current_hits
        else:
            results = past_hits
            for hit in results:
                hit["fallback_reason"] = "no_current_match"

        if len(results) < _RECALL_LIMIT and len(raw_chunks) > _RECALL_CANDIDATE_LIMIT:
            # brain.query maps this to projection_state=unavailable, not fresh [].
            raise RuntimeError("PG recall candidate limit exhausted")
        return results[:_RECALL_LIMIT]

    return _search
