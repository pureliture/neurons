"""M8 read cutover: Qdrant-backed brain.query recall.

Fills brain.query's ``archive`` / ``evidence_candidates`` lanes from the Qdrant
searchable mirror instead of RetiredIndexBridge. Returns the ``(query, brain_id) -> list[dict]``
shape that ``session_memory.brain_query.build_brain_query_response_v2`` consumes
(memory_id / result_type / card_type / summary / currentness / score / content_hash).

Every mirror hit is authority-joined through ``CouchDBProjectionStateAuthorityResolver``
(the go-forward CouchDB authority), so a hit only surfaces if its session is still
PROJECTED and its content_hash matches the currently-projected body. Additive: in
the live MCP the RetiredIndexBridge archive search is already off (dataset_ids empty), so wiring
this can only ADD recall, never regress an existing surface.
"""

from __future__ import annotations

from typing import Any, Callable

from .qdrant_authority_join import join_mirror_hits_to_authority
from .qdrant_couchdb_authority import CouchDBProjectionStateAuthorityResolver

DEFAULT_RECALL_LIMIT = 8

BrainQuerySearch = Callable[[str, str], list[dict[str, Any]]]


def build_qdrant_brain_query_search(
    *, adapter: Any, store: Any, limit: int = DEFAULT_RECALL_LIMIT
) -> BrainQuerySearch:
    """Build the ``(query, brain_id) -> list[dict]`` Qdrant recall callable.

    ``adapter`` is a QdrantDoclingMirrorAdapter; ``store`` a CouchDBSourceStore used
    by the authority resolver. Project scope is derived from the brain_id and applied
    as an authority filter so cross-project hits are dropped.
    """

    from ..session_memory.brain_query import project_from_brain_id

    def _search(query: str, brain_id: str) -> list[dict[str, Any]]:
        project = project_from_brain_id(brain_id)
        filters = {"project": project} if project else None
        resolver = CouchDBProjectionStateAuthorityResolver(store, filters=filters)
        # query_mirror_candidates supports a ``filters`` arg, so push the project
        # scope down to the Qdrant query too (the authority-join resolver filter
        # stays as the authoritative gate -- this is an additional pre-filter).
        raw = adapter.query_mirror_candidates(
            str(query or ""), target_profile="session-memory", filters=filters, limit=limit
        )
        joined = join_mirror_hits_to_authority(raw, resolver=resolver, drop_unresolved=True)
        results: list[dict[str, Any]] = []
        for hit in joined:
            results.append(
                {
                    # this search is always over the session-memory profile; the raw
                    # mirror hit's result_type is the generic "searchable_mirror".
                    "result_type": "session_memory",
                    "memory_id": str(hit.get("memory_id") or ""),
                    # session-memory points carry no card_type -> archive lane
                    "card_type": "",
                    "summary": str(hit.get("summary") or ""),
                    # authority-resolved hits are current by construction
                    "currentness": str(hit.get("authority_currentness") or "current"),
                    "score": hit.get("score"),
                    "content_hash": str(hit.get("content_hash") or ""),
                }
            )
        return results

    return _search


def build_qdrant_brain_query_search_from_env(environ: Any) -> BrainQuerySearch | None:
    """Build the Qdrant recall callable from env, or None when not configured.

    Requires ``QDRANT_URL`` + ``COUCHDB_URL`` (the authority store). Reuses the
    OpenAI-compatible embedding provider and the mirror collection
    (``ensure_collection=False`` -- never create). Two failure modes are
    distinguished: "not configured" (either URL absent -> silent None, normal) vs
    "configured but construction failed" (URL present but build raised -> a
    redaction-safe warning is logged before returning None, so a real recall
    misconfig is not silently swallowed). Either way None lets the MCP fall back to
    its prior (RetiredIndexBridge-off) recall rather than crashing.
    """

    url = str(environ.get("QDRANT_URL") or "").strip()
    couch_url = str(environ.get("COUCHDB_URL") or "").strip()
    # "not configured": QDRANT_URL/COUCHDB_URL이 없으면 조용히 None을 반환한다(정상).
    if not url or not couch_url:
        return None
    try:
        import base64

        from ..couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
        from .qdrant_docling_mirror import (
            DEFAULT_COLLECTION_NAME,
            PassthroughMarkdownNormalizer,
            build_remote_qdrant_docling_mirror_adapter,
        )
        from .qdrant_embedding import build_openai_embedding_provider

        collection = str(environ.get("QDRANT_COLLECTION") or DEFAULT_COLLECTION_NAME).strip()
        adapter = build_remote_qdrant_docling_mirror_adapter(
            url=url,
            collection_name=collection,
            embedding_provider=build_openai_embedding_provider(environ=environ),
            normalizer=PassthroughMarkdownNormalizer(),
            ensure_collection=False,
        )
        user = str(environ.get("COUCHDB_USER") or "")
        password = str(environ.get("COUCHDB_PASSWORD") or "")
        db = str(environ.get("COUCHDB_DB") or "transcript_source")
        auth_header = (
            "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
            if user
            else ""
        )
        store = CouchDBHttpSourceStore(base_url=couch_url, db=db, auth_header=auth_header)
        return build_qdrant_brain_query_search(adapter=adapter, store=store)
    except Exception as exc:
        # "configured but construction failed": URL은 있는데 빌드가 깨진 경우다.
        # 실제 recall misconfig가 조용히 묻히지 않도록 redaction-safe하게(예외 타입만)
        # 경고를 남기고 None을 반환한다(메시지/payload는 절대 로깅하지 않는다).
        import logging

        logging.getLogger(__name__).warning(
            "qdrant recall configured but construction failed: %s", type(exc).__name__
        )
        return None


__all__ = [
    "DEFAULT_RECALL_LIMIT",
    "BrainQuerySearch",
    "build_qdrant_brain_query_search",
    "build_qdrant_brain_query_search_from_env",
]
