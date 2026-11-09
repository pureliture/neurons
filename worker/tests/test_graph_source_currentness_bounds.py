from __future__ import annotations

import json
from itertools import islice
from typing import Any, Callable, cast

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBResponseTooLarge
from agent_knowledge.couchdb_source.source_revision import (
    SourceRevisionCurrentnessLimitError,
    SourceRevisionResolutionError,
    SourceRevisionSnapshotChangedError,
    activate_source_revision,
    resolve_active_source_revision,
    resolve_active_source_revision_from_snapshot,
    resolve_source_hash_for_currentness,
)
from agent_knowledge.couchdb_source.source_store import (
    CouchDBSourceStore,
    InMemoryCouchDBSourceStore,
)
from agent_knowledge.llm_brain_core.runtime import (
    session_source_revision_from_couchdb_source,
)
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession


_PAGE_SIZE = 8
_MAX_PAGE_BYTES = 512 * 1024
_MAX_COMPACT_ROWS = 8_192
_MAX_COMPACT_BYTES = 4 * 1024 * 1024
_BODY_BYTES_PER_CHUNK = 32 * 1024


class _PagedSourceStore:
    def __init__(self, documents: list[dict]):
        self.documents = sorted(documents, key=lambda doc: str(doc.get("_id") or ""))
        self.find_by_session_calls = 0
        self.iter_requests: list[dict] = []
        self.page_sizes: list[int] = []
        self.rows_yielded = 0
        self.max_page_bytes = 0
        self.max_legacy_result_bytes = 0
        self.sequence = 0
        self.on_page: Callable[[str, int, Any], None] | None = None

    def get(self, doc_id: str):
        return next((dict(doc) for doc in self.documents if doc.get("_id") == doc_id), None)

    def get_bounded(self, doc_id: str, *, max_response_bytes: int):
        document = self.get(doc_id)
        if document is None:
            return None
        encoded_size = len(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        if encoded_size > max_response_bytes:
            raise CouchDBResponseTooLarge("synthetic bounded GET response exceeded limit")
        return document

    def read_change_sequence(self) -> str:
        return str(self.sequence)

    def find_by_session(self, *, session_id_hash: str, doc_type: str = "") -> list[dict]:
        self.find_by_session_calls += 1
        rows = [
            doc
            for doc in self.documents
            if doc.get("session_id_hash") == session_id_hash
            and (not doc_type or doc.get("doc_type") == doc_type)
        ]
        self.max_legacy_result_bytes = len(
            json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        return sorted(rows, key=lambda doc: str(doc.get("_id") or ""))

    def iter_by_session(
        self,
        *,
        session_id_hash: str,
        doc_type: str,
        fields: list[str] | None,
        selector: dict | None,
        page_size: int,
        max_page_bytes: int,
    ):
        self.iter_requests.append(
            {
                "doc_type": doc_type,
                "fields": list(fields) if fields is not None else None,
                "page_size": page_size,
                "max_page_bytes": max_page_bytes,
            }
        )
        rows = (
            doc
            for doc in self.documents
            if doc.get("session_id_hash") == session_id_hash
            and doc.get("doc_type") == doc_type
            and all(doc.get(key) == value for key, value in (selector or {}).items())
        )
        offset = 0
        while True:
            if self.on_page is not None:
                self.on_page(doc_type, offset, self)
            page = [
                {field: doc[field] for field in fields if field in doc}
                if fields is not None
                else dict(doc)
                for doc in islice(rows, page_size)
            ]
            if not page:
                break
            encoded_page_bytes = len(
                json.dumps(page, sort_keys=True, separators=(",", ":")).encode("utf-8")
            )
            self.max_page_bytes = max(self.max_page_bytes, encoded_page_bytes)
            if encoded_page_bytes > max_page_bytes:
                raise SourceRevisionCurrentnessLimitError("source_revision_page_bytes_exceeded")
            self.page_sizes.append(len(page))
            self.rows_yielded += len(page)
            yield from page
            offset += len(page)


def _source_documents() -> list[dict]:
    session_id_hash = dm.build_session_id_hash("codex", "bounded-currentness-session")
    session = dm.build_transcript_session_document(
        session=TranscriptSession(
            session_id_hash=session_id_hash,
            provider="codex",
            project="neurons",
            started_at="2026-11-01T00:00:00Z",
            ended_at="2026-11-01T00:01:00Z",
        )
    )
    documents = [session]
    for index in range(257):
        body = f"synthetic transcript body {index} " + ("x" * _BODY_BYTES_PER_CHUNK)
        documents.append(
            {
                "_id": f"conversation_chunk:{index:04d}",
                "doc_type": dm.SourceDocType.CONVERSATION_CHUNK,
                "session_id_hash": session_id_hash,
                "content_hash": dm.sha256_hash(body),
                "observed_at_start": "2026-11-01T00:00:00Z",
                "observed_at_end": "2026-11-01T00:01:00Z",
                "turn_start_index": index,
                "turn_end_index": index,
                "part_index": 0,
                "part_count": 1,
                "char_start": 0,
                "char_end": len(body),
                "body": body,
            }
        )
    return documents


def _active_source_fixture() -> tuple[str, list[dict], str]:
    session_id_hash = dm.build_session_id_hash("codex", "bounded-active-currentness")
    store = InMemoryCouchDBSourceStore()
    session = dm.build_transcript_session_document(
        session=TranscriptSession(
            session_id_hash=session_id_hash,
            provider="codex",
            project="neurons",
            started_at="2026-11-01T00:00:00Z",
            ended_at="2026-11-01T00:01:00Z",
        )
    )
    chunk = dm.build_conversation_chunk_document(
        chunk=TranscriptChunk.from_text(
            chunk_id="bounded-active-chunk",
            session_id_hash=session_id_hash,
            provider="codex",
            project="neurons",
            turn_start_index=0,
            turn_end_index=0,
            text="active source body for integrity validation",
        )
    )
    store.put(session)
    store.put(chunk)
    activate_source_revision(store=store, session_id_hash=session_id_hash)
    expected = resolve_active_source_revision(
        store=store,
        session_id_hash=session_id_hash,
    ).source_hash
    return session_id_hash, store.find_by_session(session_id_hash=session_id_hash), expected


def test_session_source_currentness_pages_metadata_without_retaining_transcript_bodies():
    documents = _source_documents()
    session_id_hash = str(documents[0]["session_id_hash"])
    expected = resolve_active_source_revision_from_snapshot(
        session_id_hash=session_id_hash,
        documents=documents,
    ).source_hash
    store = _PagedSourceStore(documents)

    actual = session_source_revision_from_couchdb_source(
        session_id_hash=session_id_hash,
        source_store=cast(CouchDBSourceStore, store),
    )

    assert actual == expected
    assert store.find_by_session_calls == 0
    assert len(store.page_sizes) > 1
    assert max(store.page_sizes) <= _PAGE_SIZE
    assert store.max_page_bytes <= _MAX_PAGE_BYTES
    assert store.max_legacy_result_bytes == 0
    assert all(
        request["fields"] is None or "body" not in request["fields"]
        for request in store.iter_requests
    )
    assert all(request["page_size"] <= _PAGE_SIZE for request in store.iter_requests)
    assert all(request["max_page_bytes"] == _MAX_PAGE_BYTES for request in store.iter_requests)


def test_session_source_currentness_rejects_compact_row_budget_overrun(monkeypatch):
    from agent_knowledge.couchdb_source import source_revision

    documents = _source_documents()
    session_id_hash = str(documents[0]["session_id_hash"])
    store = _PagedSourceStore(documents)
    monkeypatch.setattr(source_revision, "SOURCE_CURRENTNESS_MAX_ROWS", 4)

    with pytest.raises(SourceRevisionCurrentnessLimitError) as exc:
        session_source_revision_from_couchdb_source(
            session_id_hash=session_id_hash,
            source_store=cast(CouchDBSourceStore, store),
        )

    assert exc.value.code == "source_revision_currentness_limit_exceeded"
    assert store.find_by_session_calls == 0
    assert store.rows_yielded <= 4 + _PAGE_SIZE
    assert exc.value.reason_code == "source_revision_row_limit_exceeded"


def test_session_source_currentness_rejects_compact_byte_budget_overrun(monkeypatch):
    from agent_knowledge.couchdb_source import source_revision

    documents = _source_documents()
    session_id_hash = str(documents[0]["session_id_hash"])
    store = _PagedSourceStore(documents)
    monkeypatch.setattr(source_revision, "SOURCE_CURRENTNESS_MAX_COMPACT_BYTES", 128)

    with pytest.raises(SourceRevisionCurrentnessLimitError) as exc:
        session_source_revision_from_couchdb_source(
            session_id_hash=session_id_hash,
            source_store=cast(CouchDBSourceStore, store),
        )

    assert exc.value.code == "source_revision_currentness_limit_exceeded"
    assert store.find_by_session_calls == 0
    assert store.rows_yielded <= 1
    assert exc.value.reason_code == "source_revision_compact_bytes_exceeded"


def test_active_source_currentness_preserves_hash_and_bounds_source_pages():
    session_id_hash, documents, expected = _active_source_fixture()
    store = _PagedSourceStore(documents)

    actual = session_source_revision_from_couchdb_source(
        session_id_hash=session_id_hash,
        source_store=cast(CouchDBSourceStore, store),
    )

    assert actual == expected
    assert store.find_by_session_calls == 0
    assert store.page_sizes
    assert max(store.page_sizes) <= _PAGE_SIZE
    assert store.max_page_bytes <= _MAX_PAGE_BYTES
    assert all(request["page_size"] == _PAGE_SIZE for request in store.iter_requests)


def test_active_source_currentness_rejects_body_change_against_old_member_hash():
    session_id_hash, documents, _expected = _active_source_fixture()
    pointer = next(
        document
        for document in documents
        if document.get("doc_type") == dm.SourceDocType.ACTIVE_SOURCE_REVISION
    )
    manifest = next(
        document
        for document in documents
        if document.get("_id") == pointer["manifest_id"]
    )
    source_id = next(
        member["source_document_id"]
        for member in manifest["members"]
        if member["source_doc_type"] == dm.SourceDocType.CONVERSATION_CHUNK
    )
    source = next(document for document in documents if document.get("_id") == source_id)
    source["body"] = f'{source.get("body", "")} modified after activation'
    store = _PagedSourceStore(documents)

    with pytest.raises(SourceRevisionResolutionError, match="source changed"):
        session_source_revision_from_couchdb_source(
            session_id_hash=session_id_hash,
            source_store=cast(CouchDBSourceStore, store),
        )


def test_active_source_currentness_rejects_manifest_over_transport_byte_cap(monkeypatch):
    from agent_knowledge.couchdb_source import source_revision

    session_id_hash, documents, _expected = _active_source_fixture()
    store = _PagedSourceStore(documents)
    monkeypatch.setattr(source_revision, "SOURCE_CURRENTNESS_MAX_MANIFEST_BYTES", 128)

    with pytest.raises(SourceRevisionCurrentnessLimitError) as exc:
        session_source_revision_from_couchdb_source(
            session_id_hash=session_id_hash,
            source_store=cast(CouchDBSourceStore, store),
        )

    assert exc.value.reason_code == "source_revision_manifest_bytes_exceeded"


def test_active_source_currentness_rejects_manifest_member_count_over_cap(monkeypatch):
    from agent_knowledge.couchdb_source import source_revision

    session_id_hash, documents, _expected = _active_source_fixture()
    store = _PagedSourceStore(documents)
    monkeypatch.setattr(source_revision, "SOURCE_CURRENTNESS_MAX_MANIFEST_MEMBERS", 0)

    with pytest.raises(SourceRevisionCurrentnessLimitError) as exc:
        session_source_revision_from_couchdb_source(
            session_id_hash=session_id_hash,
            source_store=cast(CouchDBSourceStore, store),
        )

    assert exc.value.reason_code == "source_revision_manifest_members_exceeded"


@pytest.mark.parametrize(
    "corruption",
    ["missing_pointer", "duplicate_pointer", "missing_member", "duplicate_member"],
)
def test_active_source_currentness_fails_closed_on_control_corruption(corruption):
    session_id_hash, documents, _expected = _active_source_fixture()
    pointer = next(
        document
        for document in documents
        if document.get("doc_type") == dm.SourceDocType.ACTIVE_SOURCE_REVISION
    )
    member = next(
        document
        for document in documents
        if document.get("doc_type") == dm.SourceDocType.SOURCE_REVISION_MEMBER
    )
    if corruption == "missing_pointer":
        documents.remove(pointer)
    elif corruption == "duplicate_pointer":
        duplicate = dict(pointer)
        duplicate["_id"] = "active_source_revision:duplicate"
        documents.append(duplicate)
    elif corruption == "missing_member":
        documents.remove(member)
    else:
        documents.append(dict(member))
    store = _PagedSourceStore(documents)

    with pytest.raises(SourceRevisionResolutionError):
        session_source_revision_from_couchdb_source(
            session_id_hash=session_id_hash,
            source_store=cast(CouchDBSourceStore, store),
        )


def test_unrelated_database_write_is_ignored_by_session_scope_scan():
    documents = _source_documents()
    session_id_hash = str(documents[0]["session_id_hash"])
    expected = resolve_active_source_revision_from_snapshot(
        session_id_hash=session_id_hash,
        documents=documents,
    ).source_hash
    store = _PagedSourceStore(documents)
    unrelated_writes = 0

    def append_unrelated_session_chunk(doc_type, offset, current_store):
        nonlocal unrelated_writes
        if (
            unrelated_writes
            or doc_type != dm.SourceDocType.CONVERSATION_CHUNK
            or offset != 0
        ):
            return
        unrelated_writes += 1
        current_store.documents.append(
            {
                "_id": "unrelated-session:write",
                "doc_type": dm.SourceDocType.CONVERSATION_CHUNK,
                "session_id_hash": dm.build_session_id_hash("codex", "other-session"),
                "content_hash": dm.sha256_hash("unrelated"),
            }
        )

    store.on_page = append_unrelated_session_chunk

    actual = session_source_revision_from_couchdb_source(
        session_id_hash=session_id_hash,
        source_store=cast(CouchDBSourceStore, store),
    )

    assert actual == expected
    assert unrelated_writes == 1


def test_continued_same_session_churn_fails_after_one_retry():
    documents = _source_documents()
    session_id_hash = str(documents[0]["session_id_hash"])
    session_chunk = next(
        document
        for document in documents
        if document.get("doc_type") == dm.SourceDocType.CONVERSATION_CHUNK
    )
    store = _PagedSourceStore(documents)
    scannable = {"scans_started": 0}

    def flip_same_session_chunk_hash(doc_type, offset, current_store):
        if doc_type != dm.SourceDocType.CONVERSATION_CHUNK or offset != 0:
            return
        scannable["scans_started"] += 1
        for document in current_store.documents:
            if document.get("_id") == session_chunk["_id"]:
                document["content_hash"] = dm.sha256_hash(
                    f"churn-{scannable['scans_started']}"
                )
                break

    store.on_page = flip_same_session_chunk_hash

    with pytest.raises(SourceRevisionSnapshotChangedError):
        session_source_revision_from_couchdb_source(
            session_id_hash=session_id_hash,
            source_store=cast(CouchDBSourceStore, store),
        )

    assert scannable["scans_started"] == 4


def test_same_session_change_during_paging_discards_mixed_attempt_and_retries():
    documents = _source_documents()
    session_id_hash = str(documents[0]["session_id_hash"])
    store = _PagedSourceStore(documents)
    changed = False

    def change_same_session_between_pages(doc_type, offset, current_store):
        nonlocal changed
        if changed or doc_type != dm.SourceDocType.CONVERSATION_CHUNK or offset < _PAGE_SIZE:
            return
        chunk = next(
            document
            for document in current_store.documents
            if document.get("_id") == "conversation_chunk:0256"
        )
        chunk["body"] = "y" * len(chunk["body"])
        chunk["content_hash"] = dm.sha256_hash(chunk["body"])
        current_store.sequence += 1
        changed = True

    store.on_page = change_same_session_between_pages

    actual = session_source_revision_from_couchdb_source(
        session_id_hash=session_id_hash,
        source_store=cast(CouchDBSourceStore, store),
    )
    expected = resolve_active_source_revision_from_snapshot(
        session_id_hash=session_id_hash,
        documents=store.find_by_session(session_id_hash=session_id_hash),
    ).source_hash

    assert changed is True
    assert actual == expected


class _ActivatingLegacyStore:
    def __init__(self, inner: InMemoryCouchDBSourceStore, on_legacy_scan: Callable[[], None]):
        self._inner = inner
        self._on_legacy_scan = on_legacy_scan
        self.legacy_scans = 0

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def iter_by_session(self, *, doc_type: str, **kwargs):
        if doc_type == dm.SourceDocType.CONVERSATION_CHUNK:
            self.legacy_scans += 1
            self._on_legacy_scan()
        return self._inner.iter_by_session(doc_type=doc_type, **kwargs)


def test_legacy_currentness_rechecks_pointer_published_between_matching_scans():
    session_id_hash = dm.build_session_id_hash("codex", "legacy-to-active-race")
    inner = InMemoryCouchDBSourceStore()
    session = dm.build_transcript_session_document(
        session=TranscriptSession(
            session_id_hash=session_id_hash,
            provider="codex",
            project="neurons",
            started_at="2026-11-01T00:00:00Z",
            ended_at="2026-11-01T00:01:00Z",
        )
    )
    chunks = [
        dm.build_conversation_chunk_document(
            chunk=TranscriptChunk.from_text(
                chunk_id=f"legacy-race-chunk-{index}",
                session_id_hash=session_id_hash,
                provider="codex",
                project="neurons",
                turn_start_index=index,
                turn_end_index=index,
                text=f"legacy race body {index}",
            )
        )
        for index in range(3)
    ]
    inner.put(session)
    for chunk in chunks:
        inner.put(chunk)
    legacy_hash = resolve_active_source_revision_from_snapshot(
        session_id_hash=session_id_hash,
        documents=inner.find_by_session(session_id_hash=session_id_hash),
    ).source_hash

    def activate_subset_after_second_scan():
        if store.legacy_scans != 2:
            return
        activated.append(True)
        activate_source_revision(
            store=inner,
            session_id_hash=session_id_hash,
            source_document_ids=(str(session["_id"]), str(chunks[0]["_id"])),
        )

    activated: list[bool] = []
    store = _ActivatingLegacyStore(inner, activate_subset_after_second_scan)

    actual = resolve_source_hash_for_currentness(
        session_id_hash=session_id_hash,
        store=cast(CouchDBSourceStore, store),
    )

    expected = resolve_active_source_revision(
        store=inner,
        session_id_hash=session_id_hash,
    ).source_hash
    assert activated == [True]
    assert expected != legacy_hash
    assert actual == expected
