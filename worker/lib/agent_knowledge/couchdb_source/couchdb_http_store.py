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
import errno
import json
import os
import shutil
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from .couchdb_currentness_stream import (
    parse_spooled_descriptor_page,
)
from .spool_reader import (
    SpoolDiskFull,
    SpoolError,
    _CURRENTNESS_HTTP_NAMESPACE,
    _write_manifest,
    acquire_spool,
    body_budget_bytes,
    cleanup_currentness_spools,
    cleanup_stale_spools,
    create_owned_spool,
    current_worker_id,
    load_complete_spool,
    parse_spooled_document,
    remove_spool,
    response_budget_bytes,
    spool_base,
)
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
    _document_envelope_fingerprint,
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

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = (
            status_code
            if isinstance(status_code, int) and not isinstance(status_code, bool)
            else None
        )


def basic_auth_header(*, user: str, password: str) -> str:
    """Single shared implementation for CouchDB Basic auth headers."""
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


DEFAULT_COUCHDB_DB = "transcript_source"


class CouchDBResponseTooLarge(CouchDBError):
    """A bounded read response exceeded its caller-provided byte limit."""

    code = "couchdb_response_too_large"


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
        self.auth_header = auth_header
        self.request_timeout_seconds = request_timeout_seconds
        self.deadline_monotonic = deadline_monotonic

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
        if self.transport is _urllib_transport:
            # The default urllib transport reads at most cap + 1 bytes. An
            # injected transport is a test seam and may allocate its response
            # before returning; the post-return check below is not a hard
            # allocation bound for arbitrary injected transports.
            response = self.transport(
                method,
                self.base_url + path,
                headers,
                body,
                timeout_seconds=timeout_seconds,
                max_response_bytes=max_response_bytes,
            )
        else:
            response = self.transport(method, self.base_url + path, headers, body)
        if max_response_bytes is not None and len(response.body) > int(max_response_bytes):
            raise CouchDBResponseTooLarge("CouchDB response exceeded configured byte limit")
        try:
            payload = json.loads(response.body.decode("utf-8") or "{}") if response.body else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CouchDBError("invalid JSON response from CouchDB") from exc
        return response.status_code, payload

    @contextmanager
    def _request_to_spool(
        self,
        method: str,
        path: str,
        *,
        json_body: dict,
        max_response_bytes: int,
    ) -> Iterator[tuple[int, Path]]:
        """Stream one HTTP response to a private 0600 file with a hard cap."""

        limit = max(1, int(max_response_bytes))
        headers = {"Accept": "application/json"}
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
        cleanup_currentness_spools()
        root, lease = create_owned_spool(
            _CURRENTNESS_HTTP_NAMESPACE,
            prefix="neuron-currentness-http-",
            kind="currentness-http",
        )
        response_path = root / "response.json"
        try:
            descriptor = os.open(response_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as sink:
                def copy_limited(source) -> None:
                    total = 0
                    while True:
                        chunk = source.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > limit:
                            raise CouchDBResponseTooLarge(
                                "CouchDB response exceeded configured byte limit"
                            )
                        sink.write(chunk)

                if self.transport is _urllib_transport:
                    from urllib import request
                    from urllib.error import HTTPError, URLError

                    req = request.Request(
                        self.base_url + path,
                        data=body,
                        headers=headers,
                        method=method,
                    )
                    try:
                        with request.urlopen(req, timeout=timeout_seconds) as response:
                            status = int(response.status)
                            copy_limited(response)
                    except HTTPError as exc:
                        status = int(exc.code)
                        copy_limited(exc)
                    except (URLError, TimeoutError) as exc:
                        raise CouchDBError(f"connection failed: {exc}") from exc
                else:
                    status, payload = self._request(
                        method, path, json_body=json_body, max_response_bytes=limit
                    )
                    payload_bytes = (
                        payload
                        if isinstance(payload, bytes)
                        else json.dumps(
                            payload, separators=(",", ":"), ensure_ascii=False
                        ).encode("utf-8")
                    )
                    if len(payload_bytes) > limit:
                        raise CouchDBResponseTooLarge(
                            "CouchDB response exceeded configured byte limit"
                        )
                    sink.write(payload_bytes)
            yield status, response_path
        finally:
            lease.close()
            shutil.rmtree(root, ignore_errors=True)

    def _doc_path(self, doc_id: str) -> str:
        return f"/{self.db}/{quote(doc_id, safe='')}"

    # --- setup ---------------------------------------------------------------

    def ensure_database(self) -> None:
        """Create the database if it does not exist (idempotent)."""
        status, _ = self._request("PUT", f"/{self.db}")
        if status not in (201, 202, 412):  # 412 = already exists
            raise CouchDBError(f"could not ensure database {self.db!r}: HTTP {status}", status_code=status)

    # --- CouchDBSourceStore protocol -----------------------------------------

    def get(self, doc_id: str) -> dict | None:
        status, payload = self._request("GET", self._doc_path(doc_id))
        if status == 404:
            return None
        if status != 200:
            raise CouchDBError(f"GET {doc_id} failed: HTTP {status}", status_code=status)
        return payload

    def get_many(
        self,
        doc_ids: Sequence[str],
        *,
        page_size: int = 100,
        max_response_bytes: int | None = None,
    ) -> dict[str, dict | None]:
        """Read many documents through bounded ``_all_docs`` pages.

        ``max_response_bytes`` is applied by the transport before JSON parsing.
        Omission preserves existing callers; bounded callers get fail-closed
        ``CouchDBResponseTooLarge`` rather than an in-memory oversized response.
        """

        unique = list(dict.fromkeys(str(doc_id) for doc_id in doc_ids))
        if not unique:
            return {}
        effective_page = max(1, int(page_size))
        result: dict[str, dict | None] = {}
        for start in range(0, len(unique), effective_page):
            page = unique[start : start + effective_page]
            status, payload = self._request(
                "POST",
                f"/{self.db}/_all_docs?include_docs=true",
                json_body={"keys": page}, max_response_bytes=max_response_bytes,
            )
            if status != 200:
                raise CouchDBError(f"POST _all_docs failed: HTTP {status}", status_code=status)
            for row in payload.get("rows") or []:
                key = str(row.get("key") or "")
                document = row.get("doc")
                result[key] = document if isinstance(document, dict) else None
        for doc_id in unique:
            result.setdefault(doc_id, None)
        return result

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
            raise CouchDBError(f"bounded GET {doc_id} failed: HTTP {status}", status_code=status)
        if not isinstance(payload, dict):
            raise CouchDBError("bounded GET returned an invalid document")
        return payload


    def open_document_spool(
        self,
        doc_id: str,
        *,
        expected_hash: str,
        max_body_bytes: int | None = None,
        max_response_bytes: int | None = None,
        worker_id: str | None = None,
    ):
        """Stream one oversized document into a private, resumable 0600 spool."""
        from urllib import request
        from urllib.error import HTTPError, URLError

        body_limit = body_budget_bytes() if max_body_bytes is None else max_body_bytes
        response_limit = response_budget_bytes() if max_response_bytes is None else max_response_bytes
        owner = worker_id or current_worker_id()
        cleanup_stale_spools(current_worker_id=owner)
        directory, lease = acquire_spool(doc_id, expected_hash, worker_id=owner)
        raw_path = directory / "response.json"
        try:
            reuse = load_complete_spool(directory, expected_hash=expected_hash, lease=lease)
            if reuse is not None:
                return reuse
            _write_manifest(directory, {
                "schema_version": "bulk_spool.v1", "state": "partial",
                "doc_id": doc_id, "worker_id": owner, "pid": os.getpid(),
                "content_hash": expected_hash, "body_bytes": 0,
                "response_bytes": 0, "updated_at": time.time(),
            })
            headers = {"Accept": "application/json"}
            if self.auth_header:
                headers["Authorization"] = self.auth_header
            req = request.Request(
                f"{self.base_url}{self._doc_path(doc_id)}",
                headers=headers, method="GET",
            )
            with request.urlopen(req, timeout=self.request_timeout_seconds) as response:
                if response.status != 200:
                    raise CouchDBError(f"spool GET failed: HTTP {response.status}")
                total = 0
                with raw_path.open("wb") as sink:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > response_limit:
                            raise CouchDBResponseTooLarge("spool response exceeds bound")
                        try:
                            sink.write(chunk)
                        except OSError as exc:
                            if getattr(exc, "errno", None) in (errno.ENOSPC, errno.EDQUOT):
                                raise SpoolDiskFull("temporary spool disk full") from None
                            raise
            body = parse_spooled_document(
                raw_path, expected_hash=expected_hash, max_body_bytes=body_limit,
                directory=directory, doc_id=doc_id, worker_id=owner, response_bytes=total,
            )
            body._lease = lease
            return body
        except Exception:
            raw_path.unlink(missing_ok=True)
            remove_spool(directory)
            try:
                lease.close()
            except Exception:
                pass
            raise
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
        incoming_fingerprint = _document_envelope_fingerprint(document)
        existing = self.get(doc_id)

        decision = _classify_document_idempotency(
            existing,
            doc_id=doc_id,
            incoming_hash=incoming_hash,
            incoming_fingerprint=incoming_fingerprint,
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
        rev = self._write(doc_id, document, incoming_hash, incoming_fingerprint, existing)
        return StoredRevision(doc_id=doc_id, rev=rev, outcome=outcome)

    def put_if_absent(self, document: dict) -> StoredRevision:
        """Create an immutable document, rejecting a different existing body."""

        validate_for_write(document)
        doc_id = str(document["_id"])
        incoming_hash = payload_hash(document)
        incoming_fingerprint = _document_envelope_fingerprint(document)
        existing = self.get(doc_id)
        if existing is not None:
            decision = _classify_document_idempotency(
                existing,
                doc_id=doc_id,
                incoming_hash=incoming_hash,
                incoming_fingerprint=incoming_fingerprint,
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
        stored["envelope_fingerprint"] = incoming_fingerprint
        status, response = self._request("PUT", self._doc_path(doc_id), json_body=stored)
        if status == 409:
            current = self.get(doc_id)
            retry_decision = _classify_document_idempotency(
                current,
                doc_id=doc_id,
                incoming_hash=incoming_hash,
                incoming_fingerprint=incoming_fingerprint,
            )
            if current is not None and retry_decision.outcome == IdempotencyOutcome.DUPLICATE:
                return StoredRevision(
                    doc_id=doc_id,
                    rev=str(current.get("_rev") or ""),
                    outcome="duplicate",
                )
            raise SourceStoreConflict("immutable source document already exists")
        if status not in (201, 202) or not response.get("ok"):
            raise CouchDBError(f"immutable source write failed: HTTP {status}", status_code=status)
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
        incoming_fingerprint = _document_envelope_fingerprint(document)
        decision = _classify_document_idempotency(
            current,
            doc_id=doc_id,
            incoming_hash=incoming_hash,
            incoming_fingerprint=incoming_fingerprint,
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
        stored["envelope_fingerprint"] = incoming_fingerprint
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
            raise CouchDBError(f"conditional source write failed: HTTP {status}", status_code=status)
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
            incoming_fingerprint = _document_envelope_fingerprint(merged)
            decision = _classify_document_idempotency(
                current,
                doc_id=doc_id,
                incoming_hash=incoming_hash,
                incoming_fingerprint=incoming_fingerprint,
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
            stored["envelope_fingerprint"] = incoming_fingerprint
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
                raise CouchDBError(
                    f"transcript session aggregate merge failed: HTTP {status}",
                    status_code=status,
                )
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
        incoming_fingerprint = _document_envelope_fingerprint(stored)
        stored["idempotency_key"] = doc_id
        stored["payload_hash"] = incoming_hash
        stored["envelope_fingerprint"] = incoming_fingerprint
        status, response = self._request("PUT", self._doc_path(doc_id), json_body=stored)
        if status == 409:
            raise SourceStoreConflict("conditional temporal patch revision changed")
        if status not in (201, 202) or not response.get("ok"):
            raise CouchDBError(f"conditional temporal patch failed: HTTP {status}", status_code=status)
        return StoredRevision(
            doc_id=doc_id,
            rev=str(response.get("rev") or ""),
            outcome="conflict_resolved",
        )

    def _write(self, doc_id: str, document: dict, incoming_hash: str, incoming_fingerprint: str, existing: dict | None) -> str:
        stored = copy.deepcopy(document)
        stored.pop("_rev", None)
        stored["idempotency_key"] = doc_id
        stored["payload_hash"] = incoming_hash
        stored["envelope_fingerprint"] = incoming_fingerprint
        if existing is not None and existing.get("_rev"):
            stored["_rev"] = existing["_rev"]
        status, payload = self._request("PUT", self._doc_path(doc_id), json_body=stored)
        if status == 409:
            # Lost-update conflict: re-read current state and accept only a
            # true duplicate, using the same legacy-safe fingerprint rule.
            current = self.get(doc_id)
            retry_decision = _classify_document_idempotency(
                current,
                doc_id=doc_id,
                incoming_hash=incoming_hash,
                incoming_fingerprint=incoming_fingerprint,
            )
            if current is not None and retry_decision.outcome == IdempotencyOutcome.DUPLICATE:
                return str(current.get("_rev", ""))
            stored["_rev"] = current["_rev"] if current and current.get("_rev") else None
            if stored["_rev"] is None:
                stored.pop("_rev", None)
            status, payload = self._request("PUT", self._doc_path(doc_id), json_body=stored)
        if status not in (201, 202) or not payload.get("ok"):
            raise CouchDBError(f"PUT {doc_id} failed: HTTP {status}", status_code=status)
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
                raise CouchDBError(f"_find by type failed: HTTP {status}", status_code=status)
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
            raise CouchDBError(f"_explain failed: HTTP {status}", status_code=status)
        return payload

    def read_change_sequence(self) -> str:
        """Read the current database update watermark without opening a changes feed."""

        status, payload = self._request("GET", f"/{self.db}")
        if status != 200 or "update_seq" not in payload:
            raise CouchDBError(
                "database update sequence read failed",
                status_code=status if status != 200 else None,
            )
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
                raise CouchDBError(f"_changes failed: HTTP {status}", status_code=status)
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
            raise CouchDBError(f"_find execution-stats query failed: HTTP {status}", status_code=status)
        return {
            "documents": payload.get("docs"),
            "execution_stats": payload.get("execution_stats"),
        }

    def find_by_session(
        self,
        *,
        session_id_hash: str,
        doc_type: str = "",
        fields: list[str] | None = None,
        progress_callback=None,
    ) -> list[dict]:
        selector: dict = {"session_id_hash": session_id_hash}
        if doc_type:
            selector["doc_type"] = doc_type
        page_size = 10000
        docs: list[dict] = []
        bookmark = ""
        while True:
            body: dict = {"selector": selector, "limit": page_size}
            if fields:
                body["fields"] = list(fields)
            if bookmark:
                body["bookmark"] = bookmark
            status, payload = self._request("POST", f"/{self.db}/_find", json_body=body)
            if status != 200:
                raise CouchDBError(f"_find failed: HTTP {status}", status_code=status)
            page = payload.get("docs", [])
            if not page:
                break
            docs.extend(page)
            # One event per successfully fetched page: no content or ids.
            if progress_callback is not None:
                try:
                    progress_callback(len(docs), 0)
                except Exception:
                    pass
            if len(page) < page_size:
                # A short page is the last page; an extra empty round-trip only
                # adds latency to every session resolve.
                break
            next_bookmark = str(payload.get("bookmark") or "")
            if not next_bookmark or next_bookmark == bookmark:
                break
            bookmark = next_bookmark
        if not doc_type and (not fields or "doc_type" in fields):
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
        normalized_fields = list(fields) if fields else None
        while True:
            body: dict = {
                "selector": query_selector,
                "limit": size,
            }
            if normalized_fields:
                body["fields"] = list(normalized_fields)
            if bookmark:
                body["bookmark"] = bookmark
            status, payload = self._request(
                "POST",
                f"/{self.db}/_find",
                json_body=body,
                max_response_bytes=max_page_bytes,
            )
            if status != 200:
                raise CouchDBError(f"_find by session failed: HTTP {status}", status_code=status)
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

    def iter_by_session_descriptors(
        self,
        *,
        session_id_hash: str,
        doc_type: str,
        fields: list[str] | None,
        selector: dict | None,
        page_size: int,
        max_page_bytes: int,
    ) -> Iterator[dict]:
        """Yield bounded canonical descriptors without materializing source bodies.

        Every response page is streamed to a private 0600 file, parsed as a JSON
        byte stream, and reduced to small descriptor fields plus the exact
        canonical SHA-256 of the full public document. ``fields`` must be unset;
        projected metadata scans use :meth:`iter_by_session`.
        """

        if fields is not None:
            raise CouchDBError("descriptor streaming requires the full source document")
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
        while True:
            request_body: dict = {"selector": query_selector, "limit": size}
            if bookmark:
                request_body["bookmark"] = bookmark
            with self._request_to_spool(
                "POST",
                f"/{self.db}/_find",
                json_body=request_body,
                max_response_bytes=max_page_bytes,
            ) as (status, response_path):
                if status == 200:
                    rows, next_bookmark = parse_spooled_descriptor_page(
                        response_path, max_page_bytes=max_page_bytes, max_items=size
                    )
                else:
                    raise CouchDBError(f"_find by session failed: HTTP {status}", status_code=status)
            if not rows:
                return
            for row in rows:
                yield row
            if not next_bookmark:
                if len(rows) >= size:
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
        raise CouchDBError(f"DELETE {doc_id} failed: HTTP {status}", status_code=status)


__all__ = ["CouchDBHttpSourceStore", "CouchDBError"]
