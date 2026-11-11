"""Concrete CouchDB HTTP adapter implementing :class:`CouchDBSourceStore`.

Mirrors the ``RetiredIndexBridgeHttpClient`` transport-injection pattern (a ``transport``
callable returning :class:`ProxyResponse`) so it is fully unit-testable without a
running CouchDB. The default transport uses ``urllib``.

Idempotency parity with the in-memory store: the caller never supplies ``_rev``.
``put`` reads the current doc, dedups on the shared ``payload_hash`` (identical
content -> ``duplicate`` no-op), and otherwise writes with the current ``_rev``
(retrying once on a 409 conflict). Revision-scoped source copies and documents
referenced by the active source-revision manifest are immutable: changed writes
are rejected before a PUT. Staged members do not block legacy source writes
before pointer movement.
Auth is an injected header value (CouchDB has its own credentials; this is NOT
the RetiredIndexBridge token).
"""

from __future__ import annotations

import base64
import copy
import inspect
import json
import logging
import time
from collections.abc import Iterator
from urllib.parse import quote

logger = logging.getLogger(__name__)

from ..rag_ingress.idempotency import IdempotencyOutcome
from ..transport_contract import ProxyResponse
from .document_model import (
    SourceDocType,
    assert_hash_like,
)
from .source_store import (
    IMMUTABLE_SOURCE_REVISION_DOC_TYPES,
    SESSION_QUERY_MAX_PAGE_SIZE,
    SourceStoreConflict,
    SourceStoreError,
    StoredRevision,
    _classify_document_idempotency,
    _is_immutable_source_document,
    _is_revision_scoped_source_copy,
    _require_active_source_revision_allows_delete,
    _require_active_source_revision_allows_write,
    merge_transcript_session_documents,
    payload_hash,
    validate_for_write,
)


class CouchDBError(RuntimeError):
    """Raised on a non-success CouchDB HTTP response or connection failure."""


def basic_auth_header(*, user: str, password: str) -> str:
    """Single shared implementation for CouchDB Basic auth headers."""
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


DEFAULT_COUCHDB_DB = "transcript_source"


class CouchDBResponseTooLarge(CouchDBError):
    """A bounded read response exceeded its caller-provided byte limit."""

    code = "couchdb_response_too_large"


def _is_no_usable_index(payload: object) -> bool:
    """True when a _find rejection says no index matches the sort."""
    if not isinstance(payload, dict):
        return False
    return str(payload.get("error") or "") == "no_usable_index"


COUCHDB_FIND_MAX_PAGE_RESPONSE_BYTES = 8 * 1024 * 1024


def _urllib_transport(
    method: str,
    url: str,
    headers: dict,
    body: bytes,
    *,
    timeout_seconds: float = 30,
    max_response_bytes: int | None = None,
) -> ProxyResponse:
    from urllib import request
    from urllib.error import HTTPError, URLError

    req = request.Request(url, data=body if body else None, headers=headers, method=method)
    read_size = None if max_response_bytes is None else max(0, int(max_response_bytes)) + 1
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            return ProxyResponse(
                status_code=response.status,
                body=response.read() if read_size is None else response.read(read_size),
                headers={key.lower(): value for key, value in response.headers.items()},
            )
    except HTTPError as exc:
        return ProxyResponse(
            status_code=exc.code,
            body=exc.read() if read_size is None else exc.read(read_size),
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
        self._transport_accepts_timeout, self._transport_accepts_max_response_bytes = (
            self._inspect_transport_params(self.transport)
        )
        self.auth_header = auth_header
        self.request_timeout_seconds = request_timeout_seconds
        self.deadline_monotonic = deadline_monotonic

    @staticmethod
    def _inspect_transport_params(transport) -> tuple[bool, bool]:
        if transport is _urllib_transport:
            return True, True
        try:
            sig = inspect.signature(transport)
            params = sig.parameters
            has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            accepts_timeout = has_var_keyword or "timeout_seconds" in params
            accepts_max_response_bytes = has_var_keyword or "max_response_bytes" in params
            return accepts_timeout, accepts_max_response_bytes
        except (ValueError, TypeError):
            # If inspection fails (e.g. built-in C functions without signature), pass only positional args
            return False, False

    # --- HTTP plumbing --------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        max_response_bytes: int | None = None,
    ) -> tuple[int, dict]:
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

        transport_kwargs = {}
        if self._transport_accepts_timeout:
            transport_kwargs["timeout_seconds"] = timeout_seconds
        if self._transport_accepts_max_response_bytes:
            transport_kwargs["max_response_bytes"] = max_response_bytes

        # Call transport exactly once without runtime catch-and-retry
        # to guarantee mutations (POST/PUT) are never duplicated on ambiguous errors.
        response = self.transport(
            method,
            self.base_url + path,
            headers,
            body,
            **transport_kwargs,
        )

        if max_response_bytes is not None and len(response.body) > int(max_response_bytes):
            # Note: For custom transports that do not accept max_response_bytes, the transport
            # may already have buffered the entire body in memory before this check runs.
            # This check enforces the fail-closed boundary, but does not guarantee O(1) streaming
            # allocation for cap-unsupported custom transports.
            raise CouchDBResponseTooLarge("CouchDB response exceeded configured byte limit")
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

    def get_bounded(self, doc_id: str, *, max_response_bytes: int) -> dict | None:
        """Read a document with the transport byte cap applied before JSON parsing."""

        if max_response_bytes < 1:
            raise ValueError("bounded GET byte limit must be positive")
        status, payload = self._request(
            "GET",
            self._doc_path(doc_id),
            max_response_bytes=max_response_bytes,
        )
        if status == 404:
            return None
        if status != 200:
            raise CouchDBError(f"bounded GET {doc_id} failed: HTTP {status}")
        if not isinstance(payload, dict):
            raise CouchDBError("bounded GET returned an invalid document")
        return payload

    def _require_source_document_unpinned(
        self,
        doc_id: str,
        *,
        doc_type: str,
        session_id_hash: str,
        allow_active_append: bool = False,
    ) -> None:
        _require_active_source_revision_allows_write(
            store=self,
            doc_id=doc_id,
            doc_type=doc_type,
            session_id_hash=session_id_hash,
            allow_active_append=allow_active_append,
        )

    def put(self, document: dict) -> StoredRevision:
        validate_for_write(document)
        if _is_immutable_source_document(document):
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
        if _is_immutable_source_document(existing):
            raise SourceStoreConflict("immutable source document already exists")

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
            self._require_source_document_unpinned(
                doc_id,
                doc_type=str(existing.get("doc_type") or ""),
                session_id_hash=str(existing.get("session_id_hash") or ""),
                allow_active_append=True,
            )
            raise SourceStoreConflict("immutable source document already exists")

        self._require_source_document_unpinned(
            doc_id,
            doc_type=str(document.get("doc_type") or ""),
            session_id_hash=str(document.get("session_id_hash") or ""),
            allow_active_append=True,
        )

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
        if _is_revision_scoped_source_copy(document) or _is_revision_scoped_source_copy(current):
            source_document = current if current is not None else document
            self._require_source_document_unpinned(
                doc_id,
                doc_type=str(source_document.get("doc_type") or ""),
                session_id_hash=str(source_document.get("session_id_hash") or ""),
            )
            raise SourceStoreConflict("immutable source document must be additive")
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
            try:
                status, payload = self._request(
                    "POST",
                    f"/{self.db}/_find",
                    json_body=body,
                    max_response_bytes=COUCHDB_FIND_MAX_PAGE_RESPONSE_BYTES,
                )
            except CouchDBResponseTooLarge:
                # Keep the same bookmark and retry with fewer rows. Even a
                # single document above the byte cap fails closed before JSON
                # decoding rather than allocating an unbounded page.
                if page_limit <= 1:
                    raise
                page_size = max(1, page_limit // 2)
                continue
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

    def changes_since(self, *, since: str, limit: int = 2000) -> list[dict[str, object]]:
        """Return bounded changed-document rows (seq, id, deleted) since a watermark."""

        from urllib.parse import quote

        target = max(0, int(limit))
        if target == 0:
            return []
        rows: list[dict[str, object]] = []
        watermark = str(since or "0")
        while len(rows) < target:
            status, payload = self._request(
                "GET",
                f"/{self.db}/_changes?since={quote(watermark, safe='')}"
                f"&limit={min(target - len(rows), 1000)}&style=main_only",
            )
            if status != 200:
                raise CouchDBError(f"_changes failed: HTTP {status}")
            page = payload.get("results") if isinstance(payload, dict) else None
            if not page:
                break
            for row in page:
                if not isinstance(row, dict):
                    continue
                rows.append(
                    {
                        "seq": str(row.get("seq") or ""),
                        "id": str(row.get("id") or ""),
                        "deleted": bool(row.get("deleted")),
                    }
                )
            if len(rows) >= target:
                break
            next_watermark = str(page[-1].get("seq") or "")
            if not next_watermark or next_watermark == watermark:
                break
            watermark = next_watermark
        return rows[:target]

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

    def find_by_session(
        self,
        *,
        session_id_hash: str,
        doc_type: str = "",
        page_size: int = 500,
        max_response_bytes: int | None = COUCHDB_FIND_MAX_PAGE_RESPONSE_BYTES,
        allow_oversized_single_fallback: bool = True,
    ) -> list[dict]:
        selector: dict = {"session_id_hash": session_id_hash}
        if doc_type:
            selector["doc_type"] = doc_type
        docs: list[dict] = []
        seen_docs: dict[str, str] = {}
        bookmark = ""
        current_page_size = max(1, page_size)
        while True:
            body: dict = {"selector": selector, "limit": current_page_size}
            if bookmark:
                body["bookmark"] = bookmark
            try:
                status, payload = self._request(
                    "POST",
                    f"/{self.db}/_find",
                    json_body=body,
                    max_response_bytes=max_response_bytes,
                )
            except CouchDBResponseTooLarge:
                # If a multi-document page exceeds the cap, halve the page limit to
                # bound the transport buffer and retry with the same bookmark.
                if current_page_size > 1:
                    current_page_size = max(1, current_page_size // 2)
                    continue
                # Single document exceeds the page response byte cap.
                # To preserve existing behavior for valid previously supported documents
                # without introducing a silent new hard business restriction that halts
                # the entire Builder pipeline, fall back to unbounded read if allowed.
                if not allow_oversized_single_fallback:
                    raise
                logger.warning(
                    "Single CouchDB document exceeded page response byte cap; "
                    "fetching unbounded to preserve valid existing document"
                )
                status, payload = self._request(
                    "POST",
                    f"/{self.db}/_find",
                    json_body=body,
                    max_response_bytes=None,
                )
            if status != 200:
                raise CouchDBError(f"_find failed: HTTP {status}")
            page = payload.get("docs", [])
            if not page:
                break
            for doc in page:
                doc_id = str(doc.get("_id") or "")
                if not doc_id:
                    raise CouchDBError("malformed CouchDB document without _id")
                doc_hash = payload_hash(doc) if callable(payload_hash) else str(doc.get("_rev") or "")
                if doc_id in seen_docs:
                    if seen_docs[doc_id] != doc_hash:
                        raise SourceStoreConflict(
                            "conflicting duplicate document detected across page boundary"
                        )
                    continue
                seen_docs[doc_id] = doc_hash
                docs.append(doc)
            next_bookmark = str(payload.get("bookmark") or "")
            # Only terminate when bookmark is missing or unchanged. Sparse pages in CouchDB Mango
            # (len(page) < current_page_size) can legitimately occur under selector filtering.
            if not next_bookmark or next_bookmark == bookmark:
                break
            bookmark = next_bookmark
        if not doc_type:
            docs = [doc for doc in docs if doc.get("doc_type") != SourceDocType.REPO_USAGE_PATTERN]
        docs.sort(key=lambda d: str(d.get("_id")))
        return docs

    def iter_by_session(
        self,
        *,
        session_id_hash: str,
        doc_type: str,
        fields: list[str] | None = None,
        selector: dict | None = None,
        page_size: int = SESSION_QUERY_MAX_PAGE_SIZE,
        max_page_bytes: int = 524288,
    ) -> Iterator[dict]:
        """Yield a bounded session-scoped query without accumulating all pages."""

        if not session_id_hash or not doc_type:
            raise ValueError("session query requires exact session and document type")
        if max_page_bytes < 1:
            raise ValueError("session query byte limit must be positive")
        extra_selector = dict(selector or {})
        if "session_id_hash" in extra_selector or "doc_type" in extra_selector:
            raise ValueError("session query selector cannot override its scope")
        query_selector = {
            **extra_selector,
            "session_id_hash": session_id_hash,
            "doc_type": doc_type,
        }
        size = min(SESSION_QUERY_MAX_PAGE_SIZE, max(1, int(page_size)))
        bookmark = ""
        use_sort = True
        while True:
            body: dict = {
                "selector": query_selector,
                "limit": size,
            }
            if use_sort:
                body["sort"] = [{"_id": "asc"}]
            if fields:
                body["fields"] = list(fields)
            if bookmark:
                body["bookmark"] = bookmark
            status, payload = self._request(
                "POST",
                f"/{self.db}/_find",
                json_body=body,
                max_response_bytes=max_page_bytes,
            )
            if status == 400 and _is_no_usable_index(payload) and use_sort:
                # Some databases have no index matching the sort, so _find
                # rejects the request. Drop the sort and retry without it
                # (CouchDB returns documents in its default order). The index
                # does not appear mid-iteration, so remember the decision and
                # stop paying a rejected request on every later page.
                use_sort = False
                body = dict(body)
                body.pop("sort", None)
                status, payload = self._request(
                    "POST",
                    f"/{self.db}/_find",
                    json_body=body,
                    max_response_bytes=max_page_bytes,
                )
            if status != 200:
                raise CouchDBError(f"_find by session failed: HTTP {status}")
            docs = payload.get("docs") if isinstance(payload, dict) else None
            if not isinstance(docs, list):
                raise CouchDBError("_find by session returned an invalid page")
            if len(docs) > size:
                raise CouchDBError("_find by session exceeded its requested page size")
            if not docs:
                return
            for doc in docs:
                if not isinstance(doc, dict):
                    raise CouchDBError("_find by session returned an invalid document")
                yield doc
            next_bookmark = str(payload.get("bookmark") or "")
            if not next_bookmark:
                if len(docs) >= size:
                    raise CouchDBError("_find by session omitted a required continuation bookmark")
                return
            if next_bookmark == bookmark:
                raise CouchDBError("_find by session bookmark did not advance")
            bookmark = next_bookmark

    def delete(self, doc_id: str) -> bool:
        existing = self.get(doc_id)
        if existing is None or not existing.get("_rev"):
            return False
        _require_active_source_revision_allows_delete(store=self, document=existing)
        status, _ = self._request(
            "DELETE", f"{self._doc_path(doc_id)}?rev={quote(str(existing['_rev']), safe='')}"
        )
        if status in (200, 202):
            return True
        if status == 404:
            return False
        raise CouchDBError(f"DELETE {doc_id} failed: HTTP {status}")


__all__ = ["CouchDBHttpSourceStore", "CouchDBError"]
