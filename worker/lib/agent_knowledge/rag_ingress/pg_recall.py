"""PG-backed brain.query recall.

By default, PG candidates remain joined to CouchDB projection receipts. During
the explicit migrated-reader cutover, ``PG_RECALL_DIRECT_MIGRATED=true`` serves
ready, integrity-valid PostgreSQL rows directly. No path falls back to Qdrant.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any, Callable

from ..postgres_store.pgvector_store import PgVectorStore


def _session_temporal_block(*, observed_at: Any, recorded_at: Any) -> dict[str, Any]:
    '''Build a public-safe temporal block for one PG session chunk.

    observed_at is the source observation time; recorded_at is the
    ingestion time. Age is computed against now (UTC) and never substituted
    with ingestion time when observation time is missing.
    '''
    obs_original = observed_at if isinstance(observed_at, datetime) else None
    rec = recorded_at if isinstance(recorded_at, datetime) else None
    clock = "observed_at"
    obs = obs_original
    if obs is None and rec is not None:
        # Fallback allowed by FR-AGE-011, but clearly marked as low confidence.
        obs = rec
        clock = "recorded_at"
    now = datetime.now(timezone.utc)
    if obs is None:
        age_seconds = None
        confidence = "unknown"
        reason = "observed_at_missing"
        clock = "unknown"
    else:
        if obs.tzinfo is None:
            obs = obs.replace(tzinfo=timezone.utc)
        delta = (now - obs).total_seconds()
        if delta < -300:
            confidence = "anomalous"
            reason = "observed_after_now"
            age_seconds = 0
        else:
            age_seconds = max(0, int(delta))
            confidence = "low" if clock == "recorded_at" else "high"
            reason = None
    bucket = "unknown"
    if age_seconds is not None:
        days = age_seconds // 86400
        if days < 1:
            bucket = "fresh"
        elif days < 7:
            bucket = "recent"
        elif days < 30:
            bucket = "aging"
        elif days < 180:
            bucket = "old"
        else:
            bucket = "historical"
    ingestion_lag = None
    if obs_original is not None and rec is not None:
        try:
            ingestion_lag = int((rec - obs_original).total_seconds())
        except Exception:
            ingestion_lag = None
    return {
        "observed_at": obs_original.isoformat() if obs_original is not None else None,
        "recorded_at": rec.isoformat() if rec is not None else None,
        "valid_from": None,
        "valid_to": None,
        "superseded_by": [],
        "as_of": now.isoformat(),
        "age": {
            "seconds": age_seconds,
            "label": bucket,
            "bucket": bucket,
            "clock": clock if obs is not None else "unknown",
            "confidence": confidence,
            "reason_codes": [reason] if reason else [],
        },
        "ingestion_lag_seconds": ingestion_lag,
        "time_confidence": confidence,
        "currentness": "current",
        "lifecycle_status": "accepted",
        "freshness": {
            "state": "unchecked",
            "checked_at": None,
            "next_check_after": None,
            "gaps": ["no_recent_freshness_check"],
        },
        "temporal_relation": "past" if (age_seconds or 0) > 0 else "present",
        "temporal_conflict": False,
        "recommended_action": "use_with_verification",
        "summary": _age_summary(obs, age_seconds, confidence),
    }


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
                "observed_at": row.observed_at,
                "recorded_at": row.created_at,
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
                "temporal": _session_temporal_block(
                    observed_at=hit.get("observed_at"),
                    recorded_at=hit.get("recorded_at"),
                ),
            }
            for hit in authorized_hits
            if not _is_synthetic_canary_pg_row(str(hit.get("provider") or ""))
        ]
        if len(results) < _RECALL_LIMIT and len(raw_chunks) > _RECALL_CANDIDATE_LIMIT:
            # brain.query maps this to projection_state=unavailable, not fresh [].
            raise RuntimeError("PG recall candidate limit exhausted")
        return results[:_RECALL_LIMIT]

    return _search
