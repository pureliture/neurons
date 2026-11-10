"""CouchDB-native authority resolver for derived session-memory hits.

The go-forward recall authority is the CouchDB source plane, not the (retiring)
ledger ``knowledge_items``. A derived session-memory is *current* iff its
``projection_state`` doc is ``projection_status == projected`` (one
projection_state per session, latest-wins; no supersede/expiry/active-snapshot on
the CouchDB side). The ``active_content_hash`` field on that doc pins which body is
the currently-projected one.

:class:`CouchDBProjectionStateAuthorityResolver` implements the
:class:`MirrorAuthorityResolver` protocol (``authority_join``). A derived hit
resolves to authority only when:

  1. a ``projection_state`` doc exists for the hit's ``session_id_hash``, AND
  2. its ``projection_status`` is ``projected``, AND
  3. its ``active_content_hash`` equals the hit's ``content_hash`` (so a stale
     stale result for a re-projected session is dropped).

On success it returns the reconciled scope ``{provider, project, content_hash}``.
It deliberately returns NO ``privacy_level`` key: privacy is not part of the
CouchDB source plane (the whole corpus is private session transcripts, uniformly
labeled ``"private"`` at projection time), so the authority-join privacy check
(``authority_join.py``) is skipped (its guard requires BOTH a record
privacy and a hit privacy to be non-empty before comparing). Negative cases drop.
"""

from __future__ import annotations

from typing import Any

from ..couchdb_source.document_model import ProjectionStatus, projection_state_doc_id
from ..llm_brain_core.runtime import session_source_revision_from_couchdb_source


class CouchDBProjectionStateAuthorityResolver:
    """Resolve a derived hit to CouchDB projection authority by session + content_hash."""

    def __init__(
        self,
        store: Any,
        *,
        filters: dict[str, str] | None = None,
        backend: str = "postgres_pgvector",
        embedding_profile: str = "",
        pg_store: Any | None = None,
    ) -> None:
        self._store = store
        self._backend = backend
        self._filters = dict(filters or {})
        self._embedding_profile = embedding_profile
        self._pg_store = pg_store

    def resolve(self, hit: dict[str, Any]) -> dict[str, Any] | None:
        return self._resolve(hit, require_segment_hit=True)

    def resolve_session_segments(self, hit: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve the current local segment receipt without requiring an ANN row ID."""
        if self._backend != "postgres_pgvector_local":
            return None
        if not all(
            str(hit.get(key) or "")
            for key in ("session_id_hash", "provider", "project")
        ):
            return None
        return self._resolve(hit, require_segment_hit=False)

    def _resolve(
        self,
        hit: dict[str, Any],
        *,
        require_segment_hit: bool,
    ) -> dict[str, Any] | None:
        session_id_hash = str(hit.get("session_id_hash") or "")
        content_hash = str(hit.get("content_hash") or "")
        if not session_id_hash or (require_segment_hit and not content_hash):
            return None
        state = self._store.get(projection_state_doc_id(session_id_hash))
        if state is None:
            return None
        representation_hash = None
        representation_metadata: dict[str, Any] = {}
        if self._backend == "postgres_pgvector_local":
            from ..model_connectors import LOCAL_EMBEDDING_PROFILE_ID
            from ..couchdb_source.session_memory_materializer import materialize_session_memory
            from .pg_representation import (
                REPRESENTATION_KIND,
                valid_local_pg_receipt_metadata,
            )

            profile_key = self._embedding_profile or LOCAL_EMBEDDING_PROFILE_ID
            receipts = state.get("backend_receipts") or {}
            if not isinstance(receipts, dict):
                return None
            local_map = receipts.get("postgres_pgvector_local") or {}
            if not isinstance(local_map, dict):
                return None
            state = local_map.get(profile_key, {})
            if not isinstance(state, dict) or state.get("receipt_version") not in (3, 4):
                return None
            current = materialize_session_memory(session_id_hash=session_id_hash, store=self._store)
            if not valid_local_pg_receipt_metadata(
                state,
                store=self._pg_store,
                current_materialized=current,
                require_ready_vector=state.get("receipt_version") == 4,
            ):
                return None
            if state["session_id_hash"] != session_id_hash:
                return None
            if state.get("receipt_version") == 4:
                if state.get("representation") != "segment_v1":
                    return None
                if current.content_hash != state["representation_content_hash"]:
                    return None
                representation_metadata = {
                    "representation": "segment_v1",
                    "segment_ids": list(state["segment_ids"]),
                    "segment_content_hashes": list(state["segment_content_hashes"]),
                    "segment_set_digest": state["segment_set_digest"],
                }
            elif state.get("representation_kind") == REPRESENTATION_KIND:
                if not require_segment_hit:
                    return None
                from ..couchdb_source.document_model import sha256_hash
                from ..public_safe_util import public_safe_mask_body

                if sha256_hash(public_safe_mask_body(current.body)) != state["representation_content_hash"]:
                    return None
            else:
                if not require_segment_hit:
                    return None
                if current.content_hash != state["representation_content_hash"]:
                    return None
            representation_hash = state["representation_content_hash"]
            if require_segment_hit:
                hit_id = str(hit.get("memory_id") or "")
                if state.get("receipt_version") == 4:
                    try:
                        hit_index = state["segment_ids"].index(hit_id)
                    except ValueError:
                        return None
                    representation_hash = state["segment_content_hashes"][hit_index]
                elif str(state.get("session_memory_knowledge_id") or "") != hit_id:
                    return None
            if any(
                str(state.get(key) or "") != str(hit.get(key) or "")
                for key in ("provider", "project")
            ):
                return None
        elif self._backend == "postgres_pgvector":
            if not require_segment_hit:
                return None
            receipts = state.get("backend_receipts") or {}
            if not isinstance(receipts, dict):
                return None
            state = receipts.get(self._backend, {})
            from .pg_representation import valid_pg_receipt_metadata
            if not valid_pg_receipt_metadata(state):
                return None
            if state.get("receipt_version") == 2:
                from .pg_representation import valid_representation_receipt
                if not valid_representation_receipt(state) or state["session_id_hash"] != session_id_hash:
                    return None
                from ..couchdb_source.session_memory_materializer import materialize_session_memory
                from ..couchdb_source.document_model import sha256_hash
                from ..public_safe_util import public_safe_mask_body

                current = materialize_session_memory(session_id_hash=session_id_hash, store=self._store)
                if (not current.fully_materialized
                    or current.content_hash != state["active_content_hash"]
                    or current.source_hash != state["projected_source_hash"]
                    or current.provider != state["provider"] or current.project != state["project"]
                    or sha256_hash(public_safe_mask_body(current.body)) != state["representation_content_hash"]):
                    return None
                representation_hash = state["representation_content_hash"]
            if str(state.get("session_memory_knowledge_id") or "") != str(hit.get("memory_id") or ""):
                return None
            if any(str(state.get(key) or "") != str(hit.get(key) or "") for key in ("provider", "project")):
                return None
        if str(state.get("projection_status") or "") != ProjectionStatus.PROJECTED:
            return None
        # Status alone is not currentness evidence. Legacy rows are unresolved
        # until a rebuild records both active body and projected source revision.
        active = str(state.get("active_content_hash") or "")
        projected_source_hash = str(state.get("projected_source_hash") or "")
        expected_content_hash = active if not require_segment_hit else content_hash
        if (
            not active
            or (representation_hash or active) != expected_content_hash
            or not projected_source_hash
        ):
            return None
        current_source_hash = session_source_revision_from_couchdb_source(
            session_id_hash=session_id_hash,
            source_store=self._store,
        )
        if not current_source_hash or projected_source_hash != current_source_hash:
            return None
        # Optional scope filters (provider/project), parity with the ledger resolver.
        for key in ("provider", "project"):
            wanted = self._filters.get(key)
            if wanted and str(state.get(key) or "") != str(wanted):
                return None
        # No privacy_level key: privacy is not a CouchDB-source concept (see module
        # docstring). provider/project are reconciled from the authoritative doc.
        return {
            "provider": str(state.get("provider") or ""),
            "project": str(state.get("project") or ""),
            "content_hash": content_hash or active,
            **representation_metadata,
        }


__all__ = ["CouchDBProjectionStateAuthorityResolver"]
