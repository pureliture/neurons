"""CouchDB source store seam + an in-memory fake for tests.

:class:`CouchDBSourceStore` is the backend-neutral boundary (mirroring the
ingress :class:`RetiredIndexBridgeAdapter` Protocol). The concrete CouchDB HTTP
adapter is introduced in a later milestone; M1 ships the contract and an
in-memory fake that the rest of the migration tests against.

Idempotent upsert (design "CouchDB revision conflict: retry idempotent upsert
using deterministic document id and content hash"): the caller never supplies a
``_rev``. The store keys on the deterministic ``_id``; a re-put of identical
content is a no-op duplicate that returns the existing revision, and a put of
changed content under the same ``_id`` is accepted and bumps the revision. This
models a conflict-free upsert without leaking CouchDB revision bookkeeping to
callers.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..rag_ingress.idempotency import IdempotencyOutcome, classify_idempotency
from .document_model import (
    assert_couchdb_owned,
    assert_no_secret_like_metadata,
    assert_source_text_clean,
)


class SourceStoreError(ValueError):
    """Raised when a document violates the store write contract."""


@dataclass(frozen=True)
class StoredRevision:
    doc_id: str
    rev: str
    outcome: str  # accepted | duplicate | conflict_resolved


@runtime_checkable
class CouchDBSourceStore(Protocol):
    def put(self, document: dict) -> StoredRevision: ...

    def get(self, doc_id: str) -> dict | None: ...

    def find_by_type(self, doc_type: str, *, fields: list[str] | None = None) -> list[dict]: ...

    def find_by_session(self, *, session_id_hash: str, doc_type: str = "") -> list[dict]: ...

    def delete(self, doc_id: str) -> bool: ...


def payload_hash(document: dict) -> str:
    """The natural payload identity used for idempotency.

    Conversation chunks and bundles carry a ``content_hash``; coverage manifests
    carry a ``coverage_hash``; the remaining families hash their public-safe
    body. The result is never raw text. Shared by the in-memory and HTTP stores
    so their dedup behavior is identical.
    """

    for key in ("content_hash", "coverage_hash"):
        value = document.get(key)
        if value:
            return str(value)
    payload = {k: v for k, v in document.items() if k not in ("_id", "_rev", "idempotency_key", "payload_hash")}
    blob = repr(sorted(payload.items()))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def validate_for_write(document: dict) -> None:
    doc_id = document.get("_id")
    if not doc_id:
        raise SourceStoreError("document is missing a deterministic _id")
    doc_type = document.get("doc_type", "")
    assert_couchdb_owned(doc_type)
    assert_no_secret_like_metadata(document)
    body = document.get("body")
    if isinstance(body, str) and body:
        # Defense in depth: builders already enforce this, but a hand-built doc
        # written straight to the store must still pass the fail-closed gate.
        assert_source_text_clean(body)


class InMemoryCouchDBSourceStore:
    """In-memory :class:`CouchDBSourceStore` for tests and dry runs."""

    def __init__(self) -> None:
        self._docs: dict[str, dict] = {}

    def put(self, document: dict) -> StoredRevision:
        validate_for_write(document)
        doc_id = str(document["_id"])
        incoming_hash = payload_hash(document)
        existing = self._docs.get(doc_id)

        decision = classify_idempotency(
            existing,
            idempotency_key=doc_id,
            payload_hash=incoming_hash,
        )
        if existing is not None and decision.outcome == IdempotencyOutcome.DUPLICATE:
            return StoredRevision(doc_id=doc_id, rev=str(existing["_rev"]), outcome="duplicate")

        if existing is None:
            rev_number = 1
            outcome = "accepted"
        else:
            rev_number = int(str(existing["_rev"]).split("-", 1)[0]) + 1
            outcome = "conflict_resolved"

        rev = f"{rev_number}-{incoming_hash.split(':', 1)[-1][:12]}"
        stored = copy.deepcopy(document)
        stored["_rev"] = rev
        # ``idempotency_key``/``payload_hash`` let a later put re-use
        # classify_idempotency without recomputing the prior payload identity.
        stored["idempotency_key"] = doc_id
        stored["payload_hash"] = incoming_hash
        self._docs[doc_id] = stored
        return StoredRevision(doc_id=doc_id, rev=rev, outcome=outcome)

    def get(self, doc_id: str) -> dict | None:
        stored = self._docs.get(doc_id)
        return copy.deepcopy(stored) if stored is not None else None

    def find_by_session(self, *, session_id_hash: str, doc_type: str = "") -> list[dict]:
        results = [
            copy.deepcopy(doc)
            for doc in self._docs.values()
            if doc.get("session_id_hash") == session_id_hash
            and (not doc_type or doc.get("doc_type") == doc_type)
        ]
        results.sort(key=lambda d: str(d.get("_id")))
        return results

    def delete(self, doc_id: str) -> bool:
        return self._docs.pop(doc_id, None) is not None

    def all_docs(self) -> list[dict]:
        return [copy.deepcopy(doc) for doc in self._docs.values()]

    def find_by_type(self, doc_type: str, *, fields: list[str] | None = None) -> list[dict]:
        out = []
        for doc in self._docs.values():
            if doc.get("doc_type") != doc_type:
                continue
            out.append(copy.deepcopy(doc) if not fields else {k: doc.get(k) for k in fields})
        return out


__all__ = [
    "CouchDBSourceStore",
    "InMemoryCouchDBSourceStore",
    "SourceStoreError",
    "StoredRevision",
    "payload_hash",
    "validate_for_write",
]
