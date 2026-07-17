"""CouchDB-native authority resolver for Qdrant searchable-mirror hits.

The go-forward recall authority is the CouchDB source plane, not the (retiring)
ledger ``knowledge_items``. A derived session-memory is *current* iff its
``projection_state`` doc is ``projection_status == projected`` (one
projection_state per session, latest-wins; no supersede/expiry/active-snapshot on
the CouchDB side). The ``active_content_hash`` field on that doc pins which body is
the currently-projected one.

:class:`CouchDBProjectionStateAuthorityResolver` implements the
:class:`MirrorAuthorityResolver` Protocol (``qdrant_authority_join``). A mirror hit
resolves to authority only when:

  1. a ``projection_state`` doc exists for the hit's ``session_id_hash``, AND
  2. its ``projection_status`` is ``projected``, AND
  3. its ``active_content_hash`` equals the hit's ``content_hash`` (so a stale
     mirror point for a re-projected session is dropped).

On success it returns the reconciled scope ``{provider, project, content_hash}``.
It deliberately returns NO ``privacy_level`` key: privacy is not part of the
CouchDB source plane (the whole corpus is private session transcripts, uniformly
labeled ``"private"`` at mirror-build time), so the authority-join privacy check
(``qdrant_authority_join.py:56-58``) is skipped (its guard requires BOTH a record
privacy and a hit privacy to be non-empty before comparing). Negative cases drop.
"""

from __future__ import annotations

from typing import Any

from ..couchdb_source.document_model import ProjectionStatus, projection_state_doc_id
from ..llm_brain_core.runtime import session_source_revision_from_couchdb_source


class CouchDBProjectionStateAuthorityResolver:
    """Resolve a mirror hit to CouchDB projection authority by session + content_hash."""

    def __init__(self, store: Any, *, filters: dict[str, str] | None = None) -> None:
        self._store = store
        self._filters = dict(filters or {})

    def resolve(self, hit: dict[str, Any]) -> dict[str, Any] | None:
        session_id_hash = str(hit.get("session_id_hash") or "")
        content_hash = str(hit.get("content_hash") or "")
        if not session_id_hash or not content_hash:
            return None
        state = self._store.get(projection_state_doc_id(session_id_hash))
        if state is None:
            return None
        if str(state.get("projection_status") or "") != ProjectionStatus.PROJECTED:
            return None
        # Status alone is not currentness evidence. Legacy rows are unresolved
        # until a rebuild records both active body and projected source revision.
        active = str(state.get("active_content_hash") or "")
        projected_source_hash = str(state.get("projected_source_hash") or "")
        if not active or active != content_hash or not projected_source_hash:
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
            "content_hash": content_hash,
        }


__all__ = ["CouchDBProjectionStateAuthorityResolver"]
