"""Authority-join gate for Qdrant searchable-mirror hits.

A Qdrant hit is never authoritative on its own -- ``SearchableMirrorHit`` carries
``canonical_resolution_required=True`` / ``authority_join_status="not_checked"``.
Before any product use, each candidate must be resolved against the canonical
authority, exactly as the RAGFlow retrieval path resolves every chunk through
``ledger.authorize_document``.

Critically, resolution routes through the SAME canonical predicate the ledger uses
(``authorize_document_by_content_hash`` -> ``_authorize_knowledge_item``), so the
mirror cannot diverge: a superseded / disabled / expired / disabled-dataset /
authorization-revoked record is dropped here exactly as it is for a local read.
Resolved hits are flipped to ``authority_join_status="resolved"`` and have their
scope (privacy/project/provider/currentness) reconciled from the authoritative
record; unresolved hits are dropped by default and never promoted to authority.
"""

from __future__ import annotations

from typing import Any, Iterable, Protocol, runtime_checkable


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

    Resolved hits become authoritative-joined and have privacy/project/provider/
    currentness reconciled from the canonical record; unresolved hits are dropped
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
        # Privacy isolation: the mirror payload's privacy_class must agree with the
        # canonical privacy_level. If they disagree the mirror is mislabeled --
        # NEVER relabel-and-serve (that would silently move a stricter-tier record
        # into a looser scope the caller queried under). Drop as if unresolved.
        record_privacy = str(record.get("privacy_level") or "")
        hit_privacy = str(hit.get("privacy_class") or "")
        if record_privacy and hit_privacy and record_privacy != hit_privacy:
            if drop_unresolved:
                continue
            mismatch = dict(hit)
            mismatch["authority_join_status"] = "privacy_mismatch"
            mismatch["canonical_resolution_required"] = True
            out.append(mismatch)
            continue
        joined = dict(hit)
        joined["authority_join_status"] = "resolved"
        joined["canonical_resolution_required"] = False
        joined["authority"] = "local_ledger"
        # The gate guarantees the record is current (supersedes empty, not
        # disabled/expired, authorization_status active); reconcile scope from the
        # authoritative record so the non-authority mirror cannot relabel scope.
        joined["authority_currentness"] = str(record.get("currentness") or "current")
        for hit_key, record_key in (
            ("privacy_class", "privacy_level"),
            ("project", "project"),
            ("provider", "provider"),
        ):
            value = record.get(record_key)
            if value not in (None, ""):
                joined[hit_key] = str(value)
        out.append(joined)
    return out


class LedgerContentHashAuthorityResolver:
    """Resolve a mirror hit to a canonical ``knowledge_items`` row via content_hash.

    Delegates the entire lifecycle/authority decision to
    ``ledger.authorize_document_by_content_hash`` so this seam can never diverge
    from canonical authorization. ``content_hash`` is a sha256 of the body and is
    UNIQUE in ``knowledge_items``, so a match is a strong identity link.
    """

    def __init__(self, ledger: Any, *, filters: dict[str, str] | None = None) -> None:
        self._ledger = ledger
        self._filters = dict(filters or {})

    def resolve(self, hit: dict[str, Any]) -> dict[str, Any] | None:
        content_hash = str(hit.get("content_hash") or "")
        if not content_hash:
            return None
        return self._ledger.authorize_document_by_content_hash(
            content_hash, filters=self._filters or None
        )


__all__ = [
    "LedgerContentHashAuthorityResolver",
    "MirrorAuthorityResolver",
    "join_mirror_hits_to_authority",
]
