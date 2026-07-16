"""Concrete CouchDB HTTP adapter implementing :class:`CouchDBSourceStore`.

Mirrors the ``RetiredIndexBridgeHttpClient`` transport-injection pattern (a ``transport``
callable returning :class:`ProxyResponse`) so it is fully unit-testable without a
running CouchDB. The default transport uses ``urllib``.

Idempotency parity with the in-memory store: the caller never supplies ``_rev``.
``put`` reads the current doc, dedups on the shared ``payload_hash`` (identical
content -> ``duplicate`` no-op), and otherwise writes with the current ``_rev``
(retrying once on a 409 conflict). Auth is an injected header value (CouchDB has
its own credentials; this is NOT the RetiredIndexBridge token).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from urllib.parse import quote

from ..rag_ingress.idempotency import IdempotencyOutcome
from ..transport_contract import ProxyResponse
from .source_store import (
    SourceStoreConflict,
    SourceStoreError,
    StoredRevision,
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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.db = db
        self.transport = transport or _urllib_transport
        self.auth_header = auth_header
        self.request_timeout_seconds = request_timeout_seconds

    # --- HTTP plumbing --------------------------------------------------------

    def _request(self, method: str, path: str, *, json_body: dict | None = None) -> tuple[int, dict]:
        headers = {"Accept": "application/json"}
        body = b""
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.auth_header:
            headers["Authorization"] = self.auth_header
        if self.transport is _urllib_transport:
            response = self.transport(
                method, self.base_url + path, headers, body, timeout_seconds=self.request_timeout_seconds
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

    def put(self, document: dict) -> StoredRevision:
        validate_for_write(document)
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

        outcome = "conflict_resolved" if existing is not None else "accepted"
        rev = self._write(doc_id, document, incoming_hash, existing)
        return StoredRevision(doc_id=doc_id, rev=rev, outcome=outcome)

    def put_if_revision(
        self,
        document: dict,
        *,
        expected_rev: str,
    ) -> StoredRevision:
        """Write exactly one known revision and never retry a stale payload."""

        validate_for_write(document)
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
    ) -> StoredRevision:
        """CAS temporal metadata without retrying over a concurrent source write.

        The ordinary ``put`` path intentionally retries 409 conflicts for full
        deterministic upserts.  A recovery patch must be stricter: retrying the
        stale planned document could overwrite a newer live-ingress body.  This
        method binds the patch to both the current content hash and CouchDB rev,
        and a 409 is returned to the caller as a fail-closed conflict.
        """

        current = self.get(doc_id)
        if current is None:
            raise SourceStoreConflict("conditional temporal patch source is missing")
        if str(current.get("content_hash") or "") != str(expected_content_hash or ""):
            raise SourceStoreConflict("conditional temporal patch content changed")
        if not expected_rev or str(current.get("_rev") or "") != str(expected_rev):
            raise SourceStoreConflict("conditional temporal patch revision changed")
        if (
            str(current.get("observed_at_start") or "") == str(observed_at_start or "")
            and str(current.get("observed_at_end") or "") == str(observed_at_end or "")
        ):
            return StoredRevision(
                doc_id=doc_id,
                rev=str(current.get("_rev") or ""),
                outcome="duplicate",
            )
        current_rev = str(current.get("_rev") or "")
        if not current_rev:
            raise SourceStoreConflict("conditional temporal patch revision is missing")
        stored = copy.deepcopy(current)
        stored["observed_at_start"] = str(observed_at_start or "")
        stored["observed_at_end"] = str(observed_at_end or "")
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
