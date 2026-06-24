"""Authority-join gate for Qdrant searchable-mirror hits.

A Qdrant hit is never authoritative on its own -- ``SearchableMirrorHit`` carries
``canonical_resolution_required=True`` / ``authority_join_status="not_checked"``.
Before any product use, each candidate must be resolved against the canonical
authority (ledger / CouchDB), exactly as the RAGFlow retrieval path resolves every
chunk through ``ledger.authorize_document``.

This module provides the join: resolved hits are flipped to
``authority_join_status="resolved"`` (``canonical_resolution_required=False``) and
carry only public-safe authority refs; unresolved hits are dropped by default and
never silently promoted to authority.
"""

from __future__ import annotations

from typing import Any, Iterable, Protocol, runtime_checkable

# knowledge_items.status values that authorize a mirror hit as a real, current
# canonical record. Conservative: only fully indexed/active records authorize.
AUTHORIZED_STATUSES = frozenset({"indexed", "active"})


@runtime_checkable
class MirrorAuthorityResolver(Protocol):
    def resolve(self, hit: dict[str, Any]) -> dict[str, Any] | None: ...


def join_mirror_hits_to_authority(
    hits: Iterable[dict[str, Any]],
    *,
    resolver: MirrorAuthorityResolver,
    drop_unresolved: bool = True,
) -> list[dict[str, Any]]:
    """Resolve mirror candidates against canonical authority.

    Resolved hits become authoritative-joined; unresolved hits are dropped
    (default) or kept flagged ``unresolved`` but never promoted to authority.
    """

    out: list[dict[str, Any]] = []
    for hit in hits:
        record = resolver.resolve(hit)
        if record is None:
            if drop_unresolved:
                continue
            rejected = dict(hit)
            rejected["authority_join_status"] = "unresolved"
            rejected["canonical_resolution_required"] = True
            out.append(rejected)
            continue
        joined = dict(hit)
        joined["authority_join_status"] = "resolved"
        joined["canonical_resolution_required"] = False
        joined["authority"] = "local_ledger"
        # public-safe authority signal only -- never raw ids / paths
        joined["authority_currentness"] = str(record.get("currentness") or record.get("status") or "")
        out.append(joined)
    return out


class LedgerContentHashAuthorityResolver:
    """Resolve a mirror hit to a ``knowledge_items`` row via ``content_hash``.

    ``content_hash`` is a sha256 of the document body, so a match is a strong
    identity link. The record must additionally carry an authorized status.
    """

    def __init__(self, ledger: Any, *, authorized_statuses: Iterable[str] = AUTHORIZED_STATUSES) -> None:
        self._ledger = ledger
        self._authorized = frozenset(authorized_statuses)

    def resolve(self, hit: dict[str, Any]) -> dict[str, Any] | None:
        content_hash = str(hit.get("content_hash") or "")
        if not content_hash:
            return None
        record = self._ledger.get_by_content_hash(content_hash)
        if record is None:
            return None
        if str(record.get("status") or "") not in self._authorized:
            return None
        return record


__all__ = [
    "AUTHORIZED_STATUSES",
    "LedgerContentHashAuthorityResolver",
    "MirrorAuthorityResolver",
    "join_mirror_hits_to_authority",
]
