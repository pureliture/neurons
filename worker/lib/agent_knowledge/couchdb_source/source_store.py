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
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..rag_ingress.idempotency import IdempotencyOutcome, classify_idempotency
from .document_model import (
    SourceDocType,
    assert_couchdb_owned,
    assert_no_secret_like_metadata,
    assert_source_text_clean,
)


class SourceStoreError(ValueError):
    """Raised when a document violates the store write contract."""


class SourceStoreConflict(SourceStoreError):
    """Raised when a conditional additive patch no longer matches its source."""


@dataclass(frozen=True)
class StoredRevision:
    doc_id: str
    rev: str
    outcome: str  # accepted | duplicate | conflict_resolved


def _matches_selector(doc: dict, selector: dict) -> bool:
    return all(doc.get(key) == value for key, value in selector.items())


def _project_fields(doc: dict, fields: list[str] | None) -> dict:
    return copy.deepcopy(doc) if not fields else {key: copy.deepcopy(doc.get(key)) for key in fields}


@runtime_checkable
class CouchDBSourceStore(Protocol):
    def put(self, document: dict) -> StoredRevision: ...

    def get(self, doc_id: str) -> dict | None: ...

    def put_if_revision(
        self,
        document: dict,
        *,
        expected_rev: str,
    ) -> StoredRevision: ...

    def merge_transcript_session_aggregate(
        self,
        *,
        incoming: dict,
        max_attempts: int = 3,
        source_hash_authoritative: bool = False,
    ) -> StoredRevision: ...

    def patch_observed_time_if_content_hash(
        self,
        *,
        doc_id: str,
        expected_content_hash: str,
        expected_rev: str,
        observed_at_start: str,
        observed_at_end: str,
    ) -> StoredRevision: ...

    def iter_by_type(
        self,
        doc_type: str,
        *,
        fields: list[str] | None = None,
        selector: dict | None = None,
        limit: int = 0,
        page_size: int = 10000,
    ) -> Iterator[dict]: ...

    def find_by_type(
        self,
        doc_type: str,
        *,
        fields: list[str] | None = None,
        selector: dict | None = None,
        limit: int = 0,
        page_size: int = 10000,
    ) -> list[dict]: ...

    def find_by_session(self, *, session_id_hash: str, doc_type: str = "") -> list[dict]: ...

    def delete(self, doc_id: str) -> bool: ...


def payload_hash(document: dict) -> str:
    """The natural payload identity used for idempotency.

    Conversation chunks and bundles carry a ``content_hash``; coverage manifests
    carry a ``coverage_hash``; the remaining families hash their public-safe
    body. The result is never raw text. Shared by the in-memory and HTTP stores
    so their dedup behavior is identical.
    """

    doc_type = str(document.get("doc_type") or "")
    if doc_type == SourceDocType.TOOL_EVIDENCE_BUNDLE:
        return _structured_payload_hash(
            {
                "doc_type": doc_type,
                "content_hash": str(document.get("content_hash") or ""),
                "coverage_hash": str(document.get("coverage_hash") or ""),
                "observed_at_start": str(document.get("observed_at_start") or ""),
                "observed_at_end": str(document.get("observed_at_end") or ""),
            }
        )
    if doc_type == SourceDocType.CONVERSATION_CHUNK:
        return _structured_payload_hash(
            {
                "doc_type": doc_type,
                "content_hash": str(document.get("content_hash") or ""),
                "observed_at_start": str(document.get("observed_at_start") or ""),
                "observed_at_end": str(document.get("observed_at_end") or ""),
                "turn_start_index": int(document.get("turn_start_index") or 0),
                "turn_end_index": int(document.get("turn_end_index") or 0),
                "part_index": int(document.get("part_index") or 0),
                "part_count": int(document.get("part_count") or 0),
                "char_start": int(document.get("char_start") or 0),
                "char_end": int(document.get("char_end") or 0),
            }
        )
    for key in ("content_hash", "coverage_hash"):
        if value := document.get(key):
            return _structured_payload_hash(
                {
                    key: str(value),
                    "observed_at_start": str(document.get("observed_at_start") or ""),
                    "observed_at_end": str(document.get("observed_at_end") or ""),
                }
            )
    payload = {k: v for k, v in document.items() if k not in ("_id", "_rev", "idempotency_key", "payload_hash")}
    blob = repr(sorted(payload.items()))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _structured_payload_hash(identity: dict) -> str:
    encoded = json.dumps(
        identity,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _classify_document_idempotency(
    existing: dict | None,
    *,
    doc_id: str,
    incoming_hash: str,
):
    normalized = copy.deepcopy(existing) if existing is not None else None
    if normalized is not None:
        # Recompute from document fields so identity-schema upgrades do not turn
        # an exact legacy duplicate into a one-time false conflict merely because
        # its stored bookkeeping hash used the older identity contract.
        normalized["payload_hash"] = payload_hash(normalized)
    return classify_idempotency(
        normalized,
        idempotency_key=doc_id,
        payload_hash=incoming_hash,
    )


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


def _chronological_bound(values: tuple[object, object], *, latest: bool) -> str:
    parsed_values: list[tuple[datetime, str]] = []
    fallback_values: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        fallback_values.append(text)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed_values.append((parsed.astimezone(timezone.utc), text))
    if parsed_values:
        return (max(parsed_values) if latest else min(parsed_values))[1]
    if not fallback_values:
        return ""
    return max(fallback_values) if latest else min(fallback_values)


def merge_transcript_session_documents(
    *,
    existing: dict | None,
    incoming: dict,
    source_hash_authoritative: bool = False,
) -> dict:
    """Pure cumulative merge for the live ``transcript_session`` envelope.

    Ingress contributes only identity metadata and cumulative event-time bounds.
    Projector-owned currentness fields already present on the latest CouchDB
    revision win over the incoming envelope.  This makes it safe for the HTTP
    adapter to re-read and re-run this merge after every CAS conflict.
    """

    validate_for_write(incoming)
    if incoming.get("doc_type") != SourceDocType.TRANSCRIPT_SESSION:
        raise SourceStoreError("aggregate merge requires a transcript_session document")

    if existing is None:
        merged = copy.deepcopy(incoming)
        for field in ("_rev", "idempotency_key", "payload_hash"):
            merged.pop(field, None)
        return merged

    if existing.get("doc_type") != SourceDocType.TRANSCRIPT_SESSION:
        raise SourceStoreError("existing aggregate is not a transcript_session document")
    if str(existing.get("_id") or "") != str(incoming.get("_id") or ""):
        raise SourceStoreError("transcript session document id changed")
    for field in ("session_id_hash", "provider", "project"):
        old = str(existing.get(field) or "")
        new = str(incoming.get(field) or "")
        if old and new and old != new:
            raise SourceStoreError(f"transcript session {field} changed")

    merged = copy.deepcopy(existing)
    for field in ("_rev", "idempotency_key", "payload_hash"):
        merged.pop(field, None)
    for field in (
        "doc_type",
        "schema_version",
        "owner",
        "provider",
        "project",
        "session_id_hash",
        "source_locator_hash",
        "redaction_version",
    ):
        if incoming.get(field) not in (None, ""):
            merged[field] = copy.deepcopy(incoming[field])
    for field in ("started_at", "observed_at_start"):
        merged[field] = _chronological_bound(
            (existing.get(field), incoming.get(field)), latest=False
        )
    for field in ("ended_at", "observed_at_end"):
        merged[field] = _chronological_bound(
            (existing.get(field), incoming.get(field)), latest=True
        )
    if source_hash_authoritative and incoming.get("source_hash"):
        merged["source_hash"] = copy.deepcopy(incoming["source_hash"])
    else:
        merged["source_hash"] = copy.deepcopy(
            existing.get("source_hash") or incoming.get("source_hash") or ""
        )
    for field in ("materialized_at", "source_status"):
        merged[field] = copy.deepcopy(existing.get(field) or incoming.get(field) or "")
    validate_for_write(merged)
    return merged


class InMemoryCouchDBSourceStore:
    """In-memory :class:`CouchDBSourceStore` for tests and dry runs."""

    def __init__(self) -> None:
        self._docs: dict[str, dict] = {}

    def put(self, document: dict) -> StoredRevision:
        validate_for_write(document)
        doc_id = str(document["_id"])
        incoming_hash = payload_hash(document)
        existing = self._docs.get(doc_id)

        decision = _classify_document_idempotency(
            existing,
            doc_id=doc_id,
            incoming_hash=incoming_hash,
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

    def put_if_revision(
        self,
        document: dict,
        *,
        expected_rev: str,
    ) -> StoredRevision:
        validate_for_write(document)
        doc_id = str(document["_id"])
        current = self._docs.get(doc_id)
        current_rev = str((current or {}).get("_rev") or "")
        if current_rev != str(expected_rev or ""):
            raise SourceStoreConflict("conditional source revision changed")
        incoming_hash = payload_hash(document)
        decision = _classify_document_idempotency(
            current,
            doc_id=doc_id,
            incoming_hash=incoming_hash,
        )
        if current is not None and decision.outcome == IdempotencyOutcome.DUPLICATE:
            return StoredRevision(doc_id=doc_id, rev=current_rev, outcome="duplicate")
        rev_number = int(current_rev.split("-", 1)[0]) + 1 if current_rev else 1
        rev = f"{rev_number}-{incoming_hash.split(':', 1)[-1][:12]}"
        stored = copy.deepcopy(document)
        stored.pop("_rev", None)
        stored["_rev"] = rev
        stored["idempotency_key"] = doc_id
        stored["payload_hash"] = incoming_hash
        self._docs[doc_id] = stored
        return StoredRevision(
            doc_id=doc_id,
            rev=rev,
            outcome="conflict_resolved" if current is not None else "accepted",
        )

    def merge_transcript_session_aggregate(
        self,
        *,
        incoming: dict,
        max_attempts: int = 3,
        source_hash_authoritative: bool = False,
    ) -> StoredRevision:
        if max_attempts < 1:
            raise SourceStoreError("aggregate merge max_attempts must be positive")
        document_id = str(incoming.get("_id") or "")
        current = self._docs.get(document_id) if document_id else None
        merged = merge_transcript_session_documents(
            existing=current,
            incoming=incoming,
            source_hash_authoritative=source_hash_authoritative,
        )
        return self.put(merged)

    def patch_observed_time_if_content_hash(
        self,
        *,
        doc_id: str,
        expected_content_hash: str,
        expected_rev: str,
        observed_at_start: str,
        observed_at_end: str,
    ) -> StoredRevision:
        """Atomically patch temporal fields only when the content revision matches."""

        current = self._docs.get(doc_id)
        if current is None:
            raise SourceStoreConflict("conditional temporal patch source is missing")
        if str(current.get("content_hash") or "") != str(expected_content_hash or ""):
            raise SourceStoreConflict("conditional temporal patch content changed")
        if not expected_rev or str(current.get("_rev") or "") != str(expected_rev):
            raise SourceStoreConflict("conditional temporal patch revision changed")
        updated = copy.deepcopy(current)
        for key in ("_rev", "idempotency_key", "payload_hash"):
            updated.pop(key, None)
        updated["observed_at_start"] = str(observed_at_start or "")
        updated["observed_at_end"] = str(observed_at_end or "")
        return self.put(updated)

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

    def iter_by_type(
        self,
        doc_type: str,
        *,
        fields: list[str] | None = None,
        selector: dict | None = None,
        limit: int = 0,
        page_size: int = 10000,
    ) -> Iterator[dict]:
        del page_size
        selector = {**(selector or {}), "doc_type": doc_type}
        yielded = 0
        for doc in sorted(self._docs.values(), key=lambda item: str(item.get("_id") or "")):
            if not _matches_selector(doc, selector):
                continue
            if limit > 0 and yielded >= limit:
                break
            yielded += 1
            yield _project_fields(doc, fields)

    def find_by_type(
        self,
        doc_type: str,
        *,
        fields: list[str] | None = None,
        selector: dict | None = None,
        limit: int = 0,
        page_size: int = 10000,
    ) -> list[dict]:
        return list(
            self.iter_by_type(
                doc_type,
                fields=fields,
                selector=selector,
                limit=limit,
                page_size=page_size,
            )
        )


__all__ = [
    "CouchDBSourceStore",
    "InMemoryCouchDBSourceStore",
    "SourceStoreConflict",
    "SourceStoreError",
    "StoredRevision",
    "merge_transcript_session_documents",
    "payload_hash",
    "validate_for_write",
]
