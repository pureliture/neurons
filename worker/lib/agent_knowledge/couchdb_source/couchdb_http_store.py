"""Concrete CouchDB HTTP adapter implementing :class:`CouchDBSourceStore`.

Mirrors the ``RagflowHttpClient`` transport-injection pattern (a ``transport``
callable returning :class:`ProxyResponse`) so it is fully unit-testable without a
running CouchDB. The default transport uses ``urllib``.

Idempotency parity with the in-memory store: the caller never supplies ``_rev``.
``put`` reads the current doc, dedups on the shared ``payload_hash`` (identical
content -> ``duplicate`` no-op), and otherwise writes with the current ``_rev``
(retrying once on a 409 conflict). Auth is an injected header value (CouchDB has
its own credentials; this is NOT the RAGFlow token).
"""

from __future__ import annotations

import copy
import json
from urllib.parse import quote

from ..rag_ingress.idempotency import IdempotencyOutcome, classify_idempotency
from ..transport_contract import ProxyResponse
from .source_store import StoredRevision, payload_hash, validate_for_write


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

        decision = classify_idempotency(existing, idempotency_key=doc_id, payload_hash=incoming_hash)
        if existing is not None and decision.outcome == IdempotencyOutcome.DUPLICATE:
            return StoredRevision(doc_id=doc_id, rev=str(existing.get("_rev", "")), outcome="duplicate")

        outcome = "conflict_resolved" if existing is not None else "accepted"
        rev = self._write(doc_id, document, incoming_hash, existing)
        return StoredRevision(doc_id=doc_id, rev=rev, outcome=outcome)

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
