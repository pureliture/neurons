"""Concrete CouchDB HTTP adapter implementing :class:`CouchDBSourceStore`.

Mirrors the ``RetiredIndexBridgeHttpClient`` transport-injection pattern (a ``transport``
callable returning :class:`ProxyResponse`) so it is fully unit-testable without a
running CouchDB. The default transport uses ``urllib``.

Idempotency parity with the in-memory store: the caller never supplies ``_rev``.
``put`` reads the current doc, dedups on the shared ``payload_hash`` (identical
content -> ``duplicate`` no-op), and otherwise writes with the current ``_rev``
(retrying once on a 409 conflict). A source document referenced by the active
source-revision manifest is the exception: changed writes are rejected before a
PUT. Staged members do not block legacy source writes before pointer movement.
Auth is an injected header value (CouchDB has its own credentials; this is NOT
the RetiredIndexBridge token).
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Iterator
from urllib.parse import quote

from ..rag_ingress.idempotency import IdempotencyOutcome
from ..transport_contract import ProxyResponse
from .document_model import (
    SourceDocType,
    active_source_revision_pointer_doc_id,
    assert_hash_like,
)
from .source_store import (
    IMMUTABLE_SOURCE_REVISION_DOC_TYPES,
    SOURCE_REVISION_MEMBER_SOURCE_DOC_TYPES,
    SourceStoreConflict,
    SourceStoreError,
    StoredRevision,
    _active_manifest_references_source_document,
    _classify_document_idempotency,
    merge_transcript_session_documents,
    payload_hash,
    validate_for_write,
)


class CouchDBError(RuntimeError):
    """Raised on a non-success CouchDB HTTP response or connection failure."""


def _urllib_transport(method: str, url: str, headers: dict, body: bytes, *, timeout_seconds: float = 30) -> ProxyResponse:
    from urllib import request
    from urllib.error import HTTPError, URLError

    req = request.Request(url, data=body if body else None, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            return ProxyResponse(
                status_code=response.status,
                body=response.read(),
                headers={key.lower(): value for key, value in response.headers.items()},
            )
    except HTTPError as exc:
        return ProxyResponse(
            status_code=exc.code,
            body=exc.read(),
            headers={key.lower(): value for key, value in exc.headers.items()},
        )
    except (URLError, TimeoutError) as exc:
        raise CouchDBError(f"connection failed: {exc}") from exc


class CouchDBHttpSourceStore:
    """CouchDB-backed :class:`CouchDBSourceStore`.

    ``base_url`` is the CouchDB root (e.g. ``http://127.0.0.1:5984``); ``db`` is
    the database name. ``auth_header`` is an optional ``Authorization`` value.
    """

    def __init__(
        self,
        *,
        base_url: str,
        db: str,
        transport=None,
        auth_header: str = "",
        request_timeout_seconds: float = 30,
        deadline_monotonic: float | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.db = db
        self.transport = transport or _urllib_transport
        self.auth_header = auth_header
        self.request_timeout_seconds = request_timeout_seconds
        self.deadline_monotonic = deadline_monotonic

    # --- HTTP plumbing --------------------------------------------------------

    def _request(self, method: str, path: str, *, json_body: dict | None = None) -> tuple[int, dict]:
        headers = {"Accept": "application/json"}
        body = b""
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.auth_header:
            headers["Authorization"] = self.auth_header
        timeout_seconds = self.request_timeout_seconds
        if self.deadline_monotonic is not None:
            remaining = self.deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise CouchDBError("operation deadline exceeded")
            timeout_seconds = min(timeout_seconds, remaining)
        if self.transport is _urllib_transport:
            response = self.transport(
                method, self.base_url + path, headers, body, timeout_seconds=timeout_seconds
            )
        else:
            response = self.transport(method, self.base_url + path, headers, body)
        try:
            payload = json.loads(response.body.decode("utf-8") or "{}") if response.body else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CouchDBError("invalid JSON response from CouchDB") from exc
        return response.status_code, payload

    def _doc_path(self, doc_id: str) -> str:
        return f"/{self.db}/{quote(doc_id, safe='')}"

    # --- setup ---------------------------------------------------------------

    def ensure_database(self) -> None:
        """Create the database if it does not exist (idempotent)."""
        status, _ = self._request("PUT", f"/{self.db}")
        if status not in (201, 202, 412):  # 412 = already exists
            raise CouchDBError(f"could not ensure database {self.db!r}: HTTP {status}")

    # --- CouchDBSourceStore protocol -----------------------------------------

    def get(self, doc_id: str) -> dict | None:
        status, payload = self._request("GET", self._doc_path(doc_id))
        if status == 404:
            return None
        if status != 200:
            raise CouchDBError(f"GET {doc_id} failed: HTTP {status}")
        return payload

    def _require_source_document_unpinned(
        self,
        doc_id: str,
        *,
        doc_type: str,
        session_id_hash: str,
    ) -> None:
        if doc_type not in SOURCE_REVISION_MEMBER_SOURCE_DOC_TYPES:
            return
        pointer = self.get(active_source_revision_pointer_doc_id(session_id_hash))
        manifest_id = str((pointer or {}).get("manifest_id") or "")
        manifest = self.get(manifest_id) if manifest_id else None
        if _active_manifest_references_source_document(
            pointer=pointer,
            manifest=manifest,
            session_id_hash=session_id_hash,
            source_document_id=doc_id,
        ):
            raise SourceStoreConflict(
                "source revision member makes source document immutable"
            )

    def put(self, document: dict) -> StoredRevision:
        validate_for_write(document)
        if document.get("doc_type") in IMMUTABLE_SOURCE_REVISION_DOC_TYPES:
            return self.put_if_absent(document)
        doc_id = str(document["_id"])
        incoming_hash = payload_hash(document)
        existing = self.get(doc_id)

        decision = _classify_document_idempotency(
            existing,
            doc_id=doc_id,
            incoming_hash=incoming_hash,
        )
        if existing is not None and decision.outcome == IdempotencyOutcome.DUPLICATE:
            return StoredRevision(doc_id=doc_id, rev=str(existing.get("_rev", "")), outcome="duplicate")

        source_document = existing if existing is not None else document
        self._require_source_document_unpinned(
            doc_id,
            doc_type=str(source_document.get("doc_type") or ""),
            session_id_hash=str(source_document.get("session_id_hash") or ""),
        )
        outcome = "conflict_resolved" if existing is not None else "accepted"
        rev = self._write(doc_id, document, incoming_hash, existing)
        return StoredRevision(doc_id=doc_id, rev=rev, outcome=outcome)

    def put_if_absent(self, document: dict) -> StoredRevision:
        """Create an immutable document, rejecting a different existing body."""

        validate_for_write(document)
        doc_id = str(document["_id"])
        incoming_hash = payload_hash(document)
        existing = self.get(doc_id)
        if existing is not None:
            decision = _classify_document_idempotency(
                existing,
                doc_id=doc_id,
                incoming_hash=incoming_hash,
            )
            if decision.outcome == IdempotencyOutcome.DUPLICATE:
                return StoredRevision(
                    doc_id=doc_id,
                    rev=str(existing.get("_rev") or ""),
                    outcome="duplicate",
                )
            raise SourceStoreConflict("immutable source document already exists")

        stored = copy.deepcopy(document)
        stored.pop("_rev", None)
        stored["idempotency_key"] = doc_id
        stored["payload_hash"] = incoming_hash
        status, response = self._request("PUT", self._doc_path(doc_id), json_body=stored)
        if status == 409:
            current = self.get(doc_id)
            if current is not None and payload_hash(current) == incoming_hash:
                return StoredRevision(
                    doc_id=doc_id,
                    rev=str(current.get("_rev") or ""),
                    outcome="duplicate",
                )
            raise SourceStoreConflict("immutable source document already exists")
        if status not in (201, 202) or not response.get("ok"):
            raise CouchDBError(f"immutable source write failed: HTTP {status}")
        return StoredRevision(
            doc_id=doc_id,
            rev=str(response.get("rev") or ""),
            outcome="accepted",
        )

    def put_if_revision(
        self,
        document: dict,
        *,
        expected_rev: str,
    ) -> StoredRevision:
        """Write exactly one known revision and never retry a stale payload."""

        validate_for_write(document)
        if document.get("doc_type") in IMMUTABLE_SOURCE_REVISION_DOC_TYPES:
            raise SourceStoreConflict("immutable source document must be additive")
        doc_id = str(document["_id"])
        current = self.get(doc_id)
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
        self._require_source_document_unpinned(
            doc_id,
            doc_type=str(current.get("doc_type") or "") if current is not None else str(document.get("doc_type") or ""),
            session_id_hash=str(current.get("session_id_hash") or "") if current is not None else str(document.get("session_id_hash") or ""),
        )
        stored = copy.deepcopy(document)
        stored.pop("_rev", None)
        stored["idempotency_key"] = doc_id
        stored["payload_hash"] = incoming_hash
        if current_rev:
            stored["_rev"] = current_rev
        status, response = self._request(
            "PUT",
            self._doc_path(doc_id),
            json_body=stored,
        )
        if status == 409:
            raise SourceStoreConflict("conditional source revision changed")
        if status not in (201, 202) or not response.get("ok"):
            raise CouchDBError(f"conditional source write failed: HTTP {status}")
        return StoredRevision(
            doc_id=doc_id,
            rev=str(response.get("rev") or ""),
            outcome="conflict_resolved" if current is not None else "accepted",
        )

    def merge_transcript_session_aggregate(
        self,
        *,
        incoming: dict,
        max_attempts: int = 3,
        source_hash_authoritative: bool = False,
    ) -> StoredRevision:
        """CAS-merge a cumulative session envelope with bounded conflict retry.

        Every 409 discards the stale merged payload.  The next attempt performs
        a fresh GET and pure re-merge, so a concurrent projector's newer
        ``materialized_at``/``source_hash`` cannot be overwritten by ingress.
        """

        if max_attempts < 1:
            raise SourceStoreError("aggregate merge max_attempts must be positive")
        validate_for_write(incoming)
        doc_id = str(incoming["_id"])
        for _attempt in range(max_attempts):
            current = self.get(doc_id)
            merged = merge_transcript_session_documents(
                existing=current,
                incoming=incoming,
                source_hash_authoritative=source_hash_authoritative,
            )
            incoming_hash = payload_hash(merged)
            decision = _classify_document_idempotency(
                current,
                doc_id=doc_id,
                incoming_hash=incoming_hash,
            )
            if current is not None and decision.outcome == IdempotencyOutcome.DUPLICATE:
                return StoredRevision(
                    doc_id=doc_id,
                    rev=str(current.get("_rev") or ""),
                    outcome="duplicate",
                )

            self._require_source_document_unpinned(
                doc_id,
                doc_type=str(current.get("doc_type") or "") if current is not None else str(merged.get("doc_type") or ""),
                session_id_hash=str(current.get("session_id_hash") or "") if current is not None else str(merged.get("session_id_hash") or ""),
            )

            stored = copy.deepcopy(merged)
            stored["idempotency_key"] = doc_id
            stored["payload_hash"] = incoming_hash
            if current is not None:
                current_rev = str(current.get("_rev") or "")
                if not current_rev:
                    raise SourceStoreConflict("transcript session aggregate revision is missing")
                stored["_rev"] = current_rev
            status, response = self._request(
                "PUT",
                self._doc_path(doc_id),
                json_body=stored,
            )
            if status == 409:
                continue
            if status not in (201, 202) or not response.get("ok"):
                raise CouchDBError(f"transcript session aggregate merge failed: HTTP {status}")
            return StoredRevision(
                doc_id=doc_id,
                rev=str(response.get("rev") or ""),
                outcome="conflict_resolved" if current is not None else "accepted",
            )
        raise SourceStoreConflict("transcript session aggregate conflict retry exhausted")

    def patch_observed_time_if_content_hash(
        self,
        *,
        doc_id: str,
        expected_content_hash: str,
        expected_rev: str,
        observed_at_start: str,
        observed_at_end: str,
        expected_source_locator_hash: str | None = None,
        replacement_source_locator_hash: str | None = None,
    ) -> StoredRevision:
        """CAS temporal metadata without retrying over a concurrent source write.

        The ordinary ``put`` path intentionally retries 409 conflicts for full
        deterministic upserts.  A recovery patch must be stricter: retrying the
        stale planned document could overwrite a newer live-ingress body.  This
        method binds the patch to both the current content hash and CouchDB rev.
        An optional locator hash is bound too, and its replacement is persisted
        in the same write. A 409 is returned to the caller as a fail-closed
        conflict.
        """

        if replacement_source_locator_hash is not None:
            assert_hash_like("replacement_source_locator_hash", replacement_source_locator_hash)
        current = self.get(doc_id)
        if current is None:
            raise SourceStoreConflict("conditional temporal patch source is missing")
        if str(current.get("content_hash") or "") != str(expected_content_hash or ""):
            raise SourceStoreConflict("conditional temporal patch content changed")
        if not expected_rev or str(current.get("_rev") or "") != str(expected_rev):
            raise SourceStoreConflict("conditional temporal patch revision changed")
        current_locator_hash = str(current.get("source_locator_hash") or "")
        if (
            expected_source_locator_hash is not None
            and current_locator_hash != expected_source_locator_hash
        ):
            raise SourceStoreConflict("conditional temporal patch locator changed")
        if (
            str(current.get("observed_at_start") or "") == str(observed_at_start or "")
            and str(current.get("observed_at_end") or "") == str(observed_at_end or "")
            and (
                replacement_source_locator_hash is None
                or current_locator_hash == replacement_source_locator_hash
            )
        ):
            return StoredRevision(
                doc_id=doc_id,
                rev=str(current.get("_rev") or ""),
                outcome="duplicate",
            )
        self._require_source_document_unpinned(
            doc_id,
            doc_type=str(current.get("doc_type") or ""),
            session_id_hash=str(current.get("session_id_hash") or ""),
        )
        current_rev = str(current.get("_rev") or "")
        if not current_rev:
            raise SourceStoreConflict("conditional temporal patch revision is missing")
        stored = copy.deepcopy(current)
        stored["observed_at_start"] = str(observed_at_start or "")
        stored["observed_at_end"] = str(observed_at_end or "")
        if replacement_source_locator_hash is not None:
            stored["source_locator_hash"] = replacement_source_locator_hash
        validate_for_write(stored)
        incoming_hash = payload_hash(stored)
        stored["idempotency_key"] = doc_id
        stored["payload_hash"] = incoming_hash
        status, response = self._request("PUT", self._doc_path(doc_id), json_body=stored)
        if status == 409:
            raise SourceStoreConflict("conditional temporal patch revision changed")
        if status not in (201, 202) or not response.get("ok"):
            raise CouchDBError(f"conditional temporal patch failed: HTTP {status}")
        return StoredRevision(
            doc_id=doc_id,
            rev=str(response.get("rev") or ""),
            outcome="conflict_resolved",
        )

    def _write(self, doc_id: str, document: dict, incoming_hash: str, existing: dict | None) -> str:
        stored = copy.deepcopy(document)
        stored.pop("_rev", None)
        stored["idempotency_key"] = doc_id
        stored["payload_hash"] = incoming_hash
        if existing is not None and existing.get("_rev"):
            stored["_rev"] = existing["_rev"]
        status, payload = self._request("PUT", self._doc_path(doc_id), json_body=stored)
        if status == 409:
            # Lost-update conflict: re-read the current _rev and retry once. The
            # deterministic _id + content hash keep this idempotent.
            current = self.get(doc_id)
            if current is not None and payload_hash(current) == incoming_hash:
                return str(current.get("_rev", ""))
            stored["_rev"] = current["_rev"] if current and current.get("_rev") else None
            if stored["_rev"] is None:
                stored.pop("_rev", None)
            status, payload = self._request("PUT", self._doc_path(doc_id), json_body=stored)
        if status not in (201, 202) or not payload.get("ok"):
            raise CouchDBError(f"PUT {doc_id} failed: HTTP {status}")
        return str(payload.get("rev", ""))

    def iter_by_type(
        self,
        doc_type: str,
        *,
        fields: list[str] | None = None,
        selector: dict | None = None,
        limit: int = 0,
        page_size: int = 10000,
        use_index: str | list[str] = "",
        allow_fallback: bool = True,
    ) -> Iterator[dict]:
        page_size = max(1, int(page_size or 10000))
        selector = {**(selector or {}), "doc_type": doc_type}
        yielded = 0
        bookmark = ""
        while True:
            page_limit = page_size
            if limit > 0:
                remaining = limit - yielded
                if remaining <= 0:
                    return
                page_limit = min(page_size, remaining)

            body: dict = {"selector": selector, "limit": page_limit}
            if fields:
                body["fields"] = fields
            if use_index:
                body["use_index"] = use_index
                body["allow_fallback"] = bool(allow_fallback)
            if bookmark:
                body["bookmark"] = bookmark
            status, payload = self._request("POST", f"/{self.db}/_find", json_body=body)
            if status != 200:
                raise CouchDBError(f"_find by type failed: HTTP {status}")
            docs = payload.get("docs", [])
            if not docs:
                return
            for doc in docs:
                if limit > 0 and yielded >= limit:
                    return
                yielded += 1
                yield doc
            next_bookmark = str(payload.get("bookmark") or "")
            if not next_bookmark or next_bookmark == bookmark:
                return
            bookmark = next_bookmark

    def find_by_type(
        self,
        doc_type: str,
        *,
        fields: list[str] | None = None,
        selector: dict | None = None,
        limit: int = 0,
        page_size: int = 10000,
        use_index: str | list[str] = "",
        allow_fallback: bool = True,
    ) -> list[dict]:
        return list(
            self.iter_by_type(
                doc_type,
                fields=fields,
                selector=selector,
                limit=limit,
                page_size=page_size,
                use_index=use_index,
                allow_fallback=allow_fallback,
            )
        )

    def explain_find(
        self,
        *,
        selector: dict,
        fields: list[str],
        limit: int,
        index_name: str,
        index_design_document: str,
        allow_fallback: bool,
    ) -> dict:
        """Return CouchDB Mango's selected plan without mutating index state."""

        status, payload = self._request(
            "POST",
            f"/{self.db}/_explain",
            json_body={
                "selector": selector,
                "fields": fields,
                "limit": int(limit),
                "use_index": [index_design_document, index_name],
                "allow_fallback": bool(allow_fallback),
            },
        )
        if status != 200:
            raise CouchDBError(f"_explain failed: HTTP {status}")
        return payload

    def read_change_sequence(self) -> str:
        """Read the current database update watermark without opening a changes feed."""

        status, payload = self._request("GET", f"/{self.db}")
        if status != 200 or "update_seq" not in payload:
            raise CouchDBError("database update sequence read failed")
        return str(payload["update_seq"])

    def find_by_type_with_execution_stats(
        self,
        doc_type: str,
        *,
        fields: list[str],
        selector: dict | None = None,
        limit: int,
        use_index: list[str] | tuple[str, str],
        allow_fallback: bool,
    ) -> dict:
        """Run one bounded Mango query and return only its documents and scan totals.

        This is intentionally separate from ``find_by_type`` because the
        inventory needs CouchDB's single-query execution statistics to enforce
        a scan bound.  It never creates an index or follows a bookmark.
        """

        query_selector = {**(selector or {}), "doc_type": doc_type}
        status, payload = self._request(
            "POST",
            f"/{self.db}/_find",
            json_body={
                "selector": query_selector,
                "fields": list(fields),
                "limit": int(limit),
                "use_index": list(use_index),
                "allow_fallback": bool(allow_fallback),
                "execution_stats": True,
            },
        )
        if status != 200:
            raise CouchDBError(f"_find execution-stats query failed: HTTP {status}")
        return {
            "documents": payload.get("docs"),
            "execution_stats": payload.get("execution_stats"),
        }

    def find_by_session(self, *, session_id_hash: str, doc_type: str = "") -> list[dict]:
        selector: dict = {"session_id_hash": session_id_hash}
        if doc_type:
            selector["doc_type"] = doc_type
        status, payload = self._request(
            "POST", f"/{self.db}/_find", json_body={"selector": selector, "limit": 10000}
        )
        if status != 200:
            raise CouchDBError(f"_find failed: HTTP {status}")
        docs = payload.get("docs", [])
        docs.sort(key=lambda d: str(d.get("_id")))
        return docs

    def delete(self, doc_id: str) -> bool:
        existing = self.get(doc_id)
        if existing is None or not existing.get("_rev"):
            return False
        status, _ = self._request(
            "DELETE", f"{self._doc_path(doc_id)}?rev={quote(str(existing['_rev']), safe='')}"
        )
        if status in (200, 202):
            return True
        if status == 404:
            return False
        raise CouchDBError(f"DELETE {doc_id} failed: HTTP {status}")


__all__ = ["CouchDBHttpSourceStore", "CouchDBError"]
