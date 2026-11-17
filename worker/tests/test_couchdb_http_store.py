from __future__ import annotations

import hashlib
import json
import urllib.request
from urllib.parse import unquote, urlparse

import pytest

import agent_knowledge.couchdb_source.couchdb_http_store as couchdb_http_store
from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.couchdb_http_store import (
    CouchDBError,
    CouchDBHttpSourceStore,
    CouchDBResponseTooLarge,
)
from agent_knowledge.couchdb_source.source_revision import (
    activate_source_revision,
    build_revision_scoped_source_documents,
    resolve_active_source_revision,
)
from agent_knowledge.couchdb_source.source_store import (
    CouchDBSourceStore,
    SourceContractViolation,
    SourceStoreConflict,
    SourceStoreError,
)
from agent_knowledge.couchdb_source.session_memory_materializer import (
    upsert_transcript_session_aggregate,
)
from agent_knowledge.transport_contract import ProxyResponse
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession


def _sid() -> str:
    return dm.build_session_id_hash("codex", "sess-1")


def _session_doc() -> dict:
    return dm.build_transcript_session_document(
        session=TranscriptSession(
            session_id_hash=_sid(), provider="codex", project="neurons", started_at="2026-06-17T01:00:00Z"
        )
    )


def _chunk_doc(text: str) -> dict:
    seed = "chunk_" + dm.sha256_hash(text).split(":", 1)[1][:16]
    chunk = TranscriptChunk.from_text(
        chunk_id=seed,
        session_id_hash=_sid(),
        provider="codex",
        project="neurons",
        turn_start_index=0,
        turn_end_index=1,
        text=text,
    )
    return dm.build_conversation_chunk_document(chunk=chunk)


class FakeCouch:
    """Minimal in-process CouchDB simulating PUT/GET/_find/DELETE over the transport."""

    def __init__(self) -> None:
        self.dbs: dict[str, dict[str, dict]] = {}
        self.find_bodies: list[dict] = []
        self.put_conflict_once = False
        self.put_conflict_always = False
        self._conflicted = False
        self.on_put_conflict = None
        self.on_before_document_put = None

    def __call__(self, method: str, url: str, headers: dict, body: bytes) -> ProxyResponse:
        path = urlparse(url).path
        query = urlparse(url).query
        parts = [p for p in path.split("/") if p]
        db = parts[0] if parts else ""

        if method == "PUT" and len(parts) == 1:
            if db in self.dbs:
                return self._json(412, {"error": "file_exists"})
            self.dbs[db] = {}
            return self._json(201, {"ok": True})

        store = self.dbs.setdefault(db, {})

        if method == "POST" and len(parts) == 2 and parts[1] == "_find":
            return self._handle_find(store, body)

        if method == "POST" and len(parts) == 2 and parts[1] == "_all_docs":
            keys = [str(key) for key in json.loads(body.decode())["keys"]]
            # Stored ids stay percent-encoded because this fake never unquotes
            # the request path; index by the canonical id CouchDB returns.
            index = {unquote(stored): document for stored, document in store.items()}
            rows = []
            for key in keys:
                document = store.get(key) or index.get(key)
                if document is None:
                    rows.append({"id": key, "key": key, "error": "not_found"})
                else:
                    rows.append(
                        {
                            "id": key,
                            "key": key,
                            "value": {"rev": document.get("_rev")},
                            "doc": document,
                        }
                    )
            return self._json(200, {"rows": rows})

        if len(parts) == 2:
            doc_id = parts[1]
            if method == "GET":
                doc = store.get(doc_id)
                return self._json(200, doc) if doc else self._json(404, {"error": "not_found"})
            if method == "PUT":
                incoming = json.loads(body.decode())
                if self.on_before_document_put is not None:
                    callback = self.on_before_document_put
                    self.on_before_document_put = None
                    callback(doc_id)
                if self.put_conflict_always or (self.put_conflict_once and not self._conflicted):
                    self._conflicted = True
                    if self.on_put_conflict is not None:
                        self.on_put_conflict(store, doc_id)
                    return self._json(409, {"error": "conflict"})
                current = store.get(doc_id)
                if current is not None and incoming.get("_rev") != current.get("_rev"):
                    return self._json(409, {"error": "conflict"})
                n = int(str(current["_rev"]).split("-")[0]) + 1 if current else 1
                rev = f"{n}-" + hashlib.sha256(body).hexdigest()[:8]
                incoming["_rev"] = rev
                store[doc_id] = incoming
                return self._json(201, {"ok": True, "id": doc_id, "rev": rev})
            if method == "DELETE":
                if doc_id in store:
                    del store[doc_id]
                    return self._json(200, {"ok": True})
                return self._json(404, {"error": "not_found"})

        return self._json(400, {"error": "bad_request"})

    @staticmethod
    def _json(status: int, payload: dict) -> ProxyResponse:
        return ProxyResponse(status_code=status, body=json.dumps(payload).encode("utf-8"))

    def _handle_find(self, store: dict[str, dict], body: bytes) -> ProxyResponse:
        request = json.loads(body.decode())
        self.find_bodies.append(request)

        docs = self._matching_docs(store, request["selector"])
        start = int(str(request.get("bookmark") or "0"))
        limit = int(request.get("limit") or len(docs) or 1)
        page = self._project_fields(docs[start:start + limit], request.get("fields") or [])

        payload = {"docs": page}
        if start + limit < len(docs):
            payload["bookmark"] = str(start + limit)
        return self._json(200, payload)

    @staticmethod
    def _matching_docs(store: dict[str, dict], selector: dict) -> list[dict]:
        return [
            doc
            for doc in sorted(store.values(), key=lambda item: str(item.get("_id") or ""))
            if all(doc.get(key) == value for key, value in selector.items())
        ]

    @staticmethod
    def _project_fields(docs: list[dict], fields: list[str]) -> list[dict]:
        if not fields:
            return docs
        return [{field: doc.get(field) for field in fields} for doc in docs]


def _store(fake: FakeCouch) -> CouchDBHttpSourceStore:
    return CouchDBHttpSourceStore(base_url="http://couch.test:5984", db="transcript_source", transport=fake)


def _pin_chunk(store: CouchDBHttpSourceStore) -> tuple[dict, dict, str]:
    session = _session_doc()
    chunk = _chunk_doc("pinned source")
    store.put(session)
    chunk_revision = store.put(chunk)
    activate_source_revision(store=store, session_id_hash=_sid())
    return session, chunk, chunk_revision.rev


def _active_snapshot_chunk(store: CouchDBHttpSourceStore) -> dict:
    resolved = resolve_active_source_revision(store=store, session_id_hash=_sid())
    assert len(resolved.conversation_chunks) == 1
    return resolved.conversation_chunks[0]


def test_satisfies_protocol():
    assert isinstance(_store(FakeCouch()), CouchDBSourceStore)


def test_get_many_pages_keys_and_reports_missing_documents():
    store = _store(FakeCouch())
    session = _session_doc()
    chunk = _chunk_doc("batched body")
    store.put(session)
    store.put(chunk)

    docs = store.get_many([session["_id"], chunk["_id"], "transcript_session:missing"], page_size=2)

    assert docs[session["_id"]]["doc_type"] == dm.SourceDocType.TRANSCRIPT_SESSION
    assert docs[chunk["_id"]]["_id"] == chunk["_id"]
    assert docs["transcript_session:missing"] is None


def test_ensure_database():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    assert "transcript_source" in fake.dbs
    store.ensure_database()  # idempotent (412 tolerated)


def test_put_then_get_roundtrip():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    doc = _session_doc()
    rev = store.put(doc)
    assert rev.outcome == "accepted"
    assert rev.rev.startswith("1-")
    got = store.get(doc["_id"])
    assert got["doc_type"] == dm.SourceDocType.TRANSCRIPT_SESSION
    assert got["_rev"] == rev.rev


def test_iter_by_type_retries_oversized_page_with_smaller_limit():
    oversized_bytes = 8 * 1024 * 1024 + 1
    calls = []

    def transport(method, url, headers, body):
        request = json.loads(body.decode("utf-8"))
        calls.append(request)
        if len(calls) == 1:
            return ProxyResponse(200, b"x" * oversized_bytes, {})
        if len(calls) == 2:
            return ProxyResponse(
                200,
                json.dumps({"docs": [{"_id": "small-page"}], "bookmark": "page-1"}).encode(),
                {},
            )
        return ProxyResponse(200, json.dumps({"docs": [], "bookmark": "page-1"}).encode(), {})

    store = CouchDBHttpSourceStore(
        base_url="http://couch.test:5984",
        db="transcript_source",
        transport=transport,
    )

    docs = list(store.iter_by_type("transcript_session", limit=300, page_size=256))

    assert docs == [{"_id": "small-page"}]
    assert [request["limit"] for request in calls] == [256, 128, 128]
    assert "bookmark" not in calls[0]
    assert "bookmark" not in calls[1]
    assert calls[2]["bookmark"] == "page-1"


def test_iter_by_type_fails_closed_when_single_document_page_exceeds_byte_limit():
    def transport(_method, _url, _headers, _body):
        return ProxyResponse(200, b"x" * (8 * 1024 * 1024 + 1), {})

    store = CouchDBHttpSourceStore(
        base_url="http://couch.test:5984",
        db="transcript_source",
        transport=transport,
    )

    with pytest.raises(CouchDBResponseTooLarge):
        list(store.iter_by_type("transcript_session", limit=1, page_size=1))


def test_iter_by_type_transport_reads_only_page_limit_plus_one(monkeypatch):
    class OversizedResponse:
        status = 200
        headers = {}

        def __init__(self):
            self.read_sizes = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size=-1):
            self.read_sizes.append(size)
            return b"x" * size

    response = OversizedResponse()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: response)
    store = CouchDBHttpSourceStore(base_url="http://couch.test:5984", db="transcript_source")

    with pytest.raises(CouchDBResponseTooLarge):
        list(store.iter_by_type("transcript_session", limit=1, page_size=1))

    assert response.read_sizes == [couchdb_http_store.COUCHDB_FIND_MAX_PAGE_RESPONSE_BYTES + 1]


def test_bounded_get_reads_only_limit_plus_one_before_json_decode(monkeypatch):
    class OversizedResponse:
        status = 200
        headers = {}

        def __init__(self):
            self.read_sizes = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size=-1):
            self.read_sizes.append(size)
            return b"x" * (size if size >= 0 else 4096)

    response = OversizedResponse()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: response)
    store = CouchDBHttpSourceStore(base_url="http://couch.test:5984", db="transcript_source")

    with pytest.raises(CouchDBResponseTooLarge):
        store.get_bounded("large-manifest", max_response_bytes=64)

    assert response.read_sizes == [65]


def test_put_is_idempotent_for_identical_content():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    first = store.put(_chunk_doc("same body"))
    second = store.put(_chunk_doc("same body"))
    assert second.outcome == "duplicate"
    assert second.rev == first.rev


def test_put_update_uses_rev_and_resolves():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    first = store.put(_chunk_doc("original"))
    changed = _chunk_doc("original")
    changed["body"] = "edited public body"
    changed["content_hash"] = dm.sha256_hash("edited public body")
    second = store.put(changed)
    assert second.outcome == "conflict_resolved"
    assert second.rev.startswith("2-")
    assert store.get(changed["_id"])["body"] == "edited public body"


@pytest.mark.parametrize("scope_kind", ("snapshot", "current"))
def test_unpublished_revision_scoped_source_copy_rejects_changed_put_and_conditional_mutation(
    scope_kind: str,
):
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    source = _chunk_doc("unpublished revision-scoped source")
    snapshot = build_revision_scoped_source_documents(
        documents=(source,),
        source_snapshot_hash=dm.sha256_hash("unpublished revision source set"),
        scope_kind=scope_kind,
    )[0]

    first = store.put(snapshot)
    before = store.get(snapshot["_id"])
    assert before is not None
    assert store.get(dm.active_source_revision_pointer_doc_id(_sid())) is None

    duplicate = store.put(snapshot)
    conditional_duplicate = store.put_if_revision(snapshot, expected_rev=first.rev)
    changed = dict(snapshot)
    changed["body"] = "changed unpublished revision-scoped source"
    changed["content_hash"] = dm.sha256_hash(changed["body"])

    assert duplicate.outcome == "duplicate"
    assert duplicate.rev == first.rev
    assert conditional_duplicate.outcome == "duplicate"
    assert conditional_duplicate.rev == first.rev
    with pytest.raises(SourceStoreConflict, match="immutable source document"):
        store.put(changed)
    with pytest.raises(SourceStoreConflict, match="immutable source document"):
        store.put_if_revision(changed, expected_rev=first.rev)
    assert store.get(snapshot["_id"]) == before


@pytest.mark.parametrize("scope_kind", ("snapshot", "current"))
def test_unpublished_revision_scoped_source_copy_cannot_be_deleted(scope_kind: str):
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    source = _chunk_doc("unpublished immutable source copy")
    snapshot = build_revision_scoped_source_documents(
        documents=(source,),
        source_snapshot_hash=dm.sha256_hash("unpublished immutable source set"),
        scope_kind=scope_kind,
    )[0]
    store.put(snapshot)

    with pytest.raises(SourceStoreConflict, match="immutable"):
        store.delete(snapshot["_id"])
    assert store.get(snapshot["_id"]) is not None


def test_store_rejects_noncanonical_active_source_revision_pointer_id():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()

    with pytest.raises(SourceStoreError, match="active source revision pointer id is invalid"):
        store.put(
            {
                "_id": f"{dm.SourceDocType.ACTIVE_SOURCE_REVISION}:rogue",
                "doc_type": dm.SourceDocType.ACTIVE_SOURCE_REVISION,
                "session_id_hash": _sid(),
            }
        )


def test_active_snapshot_rejects_changed_put_and_conditional_put_for_its_origin():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    _session, origin_chunk, _origin_chunk_rev = _pin_chunk(store)
    chunk = _active_snapshot_chunk(store)
    chunk_rev = str(chunk["_rev"])
    before = store.get(chunk["_id"])
    assert before is not None

    duplicate = store.put(chunk)
    conditional_duplicate = store.put_if_revision(chunk, expected_rev=chunk_rev)
    changed = dict(chunk)
    changed["body"] = "changed public-safe pinned source"
    changed["content_hash"] = dm.sha256_hash(changed["body"])

    assert duplicate.outcome == "duplicate"
    assert duplicate.rev == chunk_rev
    assert conditional_duplicate.outcome == "duplicate"
    assert conditional_duplicate.rev == chunk_rev
    with pytest.raises(SourceStoreConflict, match="revision member"):
        store.put(changed)
    with pytest.raises(SourceStoreConflict, match="revision member"):
        store.put_if_revision(changed, expected_rev=chunk_rev)
    assert store.get(chunk["_id"]) == before

    changed_origin = dict(origin_chunk)
    changed_origin["body"] = "changed public-safe mutable origin"
    changed_origin["content_hash"] = dm.sha256_hash(changed_origin["body"])
    with pytest.raises(SourceStoreConflict, match="explicit successor"):
        store.put(changed_origin)


def test_delete_rejects_active_revision_control_member_and_source_documents():
    for protected_kind in ("pointer", "manifest", "member", "source"):
        fake = FakeCouch()
        store = _store(fake)
        store.ensure_database()
        _session, _origin_chunk, _origin_chunk_rev = _pin_chunk(store)
        resolved = resolve_active_source_revision(store=store, session_id_hash=_sid())
        assert resolved.manifest_id is not None
        manifest = store.get(resolved.manifest_id)
        assert manifest is not None
        active_chunk = _active_snapshot_chunk(store)
        protected_document_ids = {
            "pointer": dm.active_source_revision_pointer_doc_id(_sid()),
            "manifest": resolved.manifest_id,
            "member": manifest["members"][0]["member_id"],
            "source": active_chunk["_id"],
        }
        document_id = protected_document_ids[protected_kind]
        before = store.get(document_id)
        with pytest.raises(SourceStoreConflict, match="active source revision"):
            store.delete(document_id)
        assert store.get(document_id) == before

    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    _pin_chunk(store)
    resolved = resolve_active_source_revision(store=store, session_id_hash=_sid())
    inactive = _chunk_doc("unreferenced source remains deletable")
    store.put(inactive)
    assert store.delete(inactive["_id"]) is True
    assert store.get(inactive["_id"]) is None
    assert resolve_active_source_revision(store=store, session_id_hash=_sid()) == resolved


def test_delete_fails_closed_when_active_pointer_is_corrupt():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    _pin_chunk(store)
    inactive = _chunk_doc("unreferenced source with corrupt active pointer")
    store.put(inactive)
    pointer_id = dm.active_source_revision_pointer_doc_id(_sid())
    pointer = next(
        document
        for document in fake.dbs["transcript_source"].values()
        if document.get("_id") == pointer_id
    )
    pointer["manifest_id"] = "source_revision_manifest:missing"

    with pytest.raises(SourceStoreConflict, match="active source revision is invalid"):
        store.delete(inactive["_id"])
    assert store.get(inactive["_id"]) is not None


def test_staged_revision_member_does_not_block_source_write_before_pointer_exists():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    _session, chunk, _chunk_rev = _pin_chunk(store)
    pointer_id = dm.active_source_revision_pointer_doc_id(_sid())
    pointer_key = next(
        key
        for key, document in fake.dbs["transcript_source"].items()
        if document.get("_id") == pointer_id
    )
    del fake.dbs["transcript_source"][pointer_key]

    changed = dict(chunk)
    changed["body"] = "changed while activation is still staging"
    changed["content_hash"] = dm.sha256_hash(changed["body"])

    revision = store.put(changed)

    assert revision.outcome == "conflict_resolved"
    assert store.get(changed["_id"])["body"] == changed["body"]


@pytest.mark.parametrize(
    "corrupt_active_control",
    (
        lambda pointer, _manifest: pointer.pop("manifest_id"),
        lambda pointer, _manifest: pointer.update(
            {"manifest_id": "source_revision_manifest:missing"}
        ),
        lambda _pointer, manifest: manifest.update({"members": "corrupt"}),
    ),
)
def test_incomplete_active_control_rejects_source_write(corrupt_active_control):
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    _session, chunk, _chunk_rev = _pin_chunk(store)
    pointer_id = dm.active_source_revision_pointer_doc_id(_sid())
    documents = fake.dbs["transcript_source"].values()
    pointer = next(document for document in documents if document.get("_id") == pointer_id)
    manifest = next(
        document
        for document in fake.dbs["transcript_source"].values()
        if document.get("_id") == pointer["manifest_id"]
    )
    corrupt_active_control(pointer, manifest)
    changed = dict(chunk)
    changed["body"] = "must not bypass incomplete active control"
    changed["content_hash"] = dm.sha256_hash(changed["body"])

    with pytest.raises(SourceStoreConflict, match="active source revision"):
        store.put(changed)

    assert store.get(chunk["_id"])["body"] == chunk["body"]


@pytest.mark.parametrize(
    "corrupt_active_control",
    (
        lambda pointer, _manifest: pointer.pop("manifest_id"),
        lambda pointer, _manifest: pointer.update(
            {"manifest_id": "source_revision_manifest:missing"}
        ),
        lambda _pointer, manifest: manifest.update({"members": "corrupt"}),
    ),
)
def test_incomplete_active_control_rejects_new_source_append_but_not_exact_duplicate(
    corrupt_active_control,
):
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    _session, _chunk, _chunk_rev = _pin_chunk(store)
    existing_append = _chunk_doc("unreferenced append before control corruption")
    first = store.put_if_absent(existing_append)
    pointer_id = dm.active_source_revision_pointer_doc_id(_sid())
    pointer = next(
        document
        for document in fake.dbs["transcript_source"].values()
        if document.get("_id") == pointer_id
    )
    manifest = next(
        document
        for document in fake.dbs["transcript_source"].values()
        if document.get("_id") == pointer["manifest_id"]
    )
    corrupt_active_control(pointer, manifest)
    new_append = _chunk_doc("must not append through incomplete active control")

    duplicate = store.put_if_absent(existing_append)
    with pytest.raises(SourceStoreConflict, match="active source revision"):
        store.put_if_absent(new_append)

    assert first.outcome == "accepted"
    assert duplicate.outcome == "duplicate"
    assert duplicate.rev == first.rev
    assert store.get(new_append["_id"]) is None


def test_active_snapshot_rejects_temporal_patch_but_allows_unreferenced_additive_write():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    _session, _origin_chunk, _origin_chunk_rev = _pin_chunk(store)
    chunk = _active_snapshot_chunk(store)
    chunk_rev = str(chunk["_rev"])

    temporal_duplicate = store.patch_observed_time_if_content_hash(
        doc_id=chunk["_id"],
        expected_content_hash=chunk["content_hash"],
        expected_rev=chunk_rev,
        observed_at_start=str(chunk.get("observed_at_start") or ""),
        observed_at_end=str(chunk.get("observed_at_end") or ""),
    )

    assert temporal_duplicate.outcome == "duplicate"
    assert temporal_duplicate.rev == chunk_rev
    with pytest.raises(SourceStoreConflict, match="revision member"):
        store.patch_observed_time_if_content_hash(
            doc_id=chunk["_id"],
            expected_content_hash=chunk["content_hash"],
            expected_rev=chunk_rev,
            observed_at_start="2026-07-09T10:00:00Z",
            observed_at_end="2026-07-09T10:30:00Z",
        )

    additive = _chunk_doc("additive source after pin")
    accepted = store.put_if_absent(additive)
    duplicate = store.put_if_absent(additive)
    assert accepted.outcome == "accepted"
    assert duplicate.outcome == "duplicate"


def test_http_pointer_can_publish_after_temporal_guard_before_raw_put():
    """CouchDB's separate pointer GET and source PUT are not one transaction."""

    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    session = _session_doc()
    chunk = _chunk_doc("http pointer race source")
    store.put(session)
    original = store.put(chunk)

    def publish_pointer_before_put(document_id: str) -> None:
        assert document_id.endswith(chunk["_id"].split(":", 1)[1].replace(":", "%3A"))
        activate_source_revision(store=store, session_id_hash=_sid())

    fake.on_before_document_put = publish_pointer_before_put
    patched = store.patch_observed_time_if_content_hash(
        doc_id=chunk["_id"],
        expected_content_hash=chunk["content_hash"],
        expected_rev=original.rev,
        observed_at_start="2026-07-09T10:00:00Z",
        observed_at_end="2026-07-09T10:30:00Z",
    )

    raw_origin = store.get(chunk["_id"])
    active = resolve_active_source_revision(store=store, session_id_hash=_sid())
    assert patched.outcome == "conflict_resolved"
    assert raw_origin is not None
    assert raw_origin["observed_at_start"] == "2026-07-09T10:00:00Z"
    assert active.conversation_chunks[0]["observed_at_start"] == ""


def test_put_retries_once_on_conflict():
    fake = FakeCouch()
    fake.put_conflict_once = True
    store = _store(fake)
    store.ensure_database()
    rev = store.put(_session_doc())
    assert rev.outcome == "accepted"
    assert rev.rev  # succeeded after one 409 retry


def test_conditional_temporal_patch_preserves_body_and_content_hash():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    document = _chunk_doc("stable body")
    first = store.put(document)

    patched = store.patch_observed_time_if_content_hash(
        doc_id=document["_id"],
        expected_content_hash=document["content_hash"],
        expected_rev=first.rev,
        observed_at_start="2026-07-09T10:00:00Z",
        observed_at_end="2026-07-09T10:30:00Z",
    )

    current = store.get(document["_id"])
    assert current is not None
    assert patched.rev != first.rev
    assert current["body"] == document["body"]
    assert current["content_hash"] == document["content_hash"]
    assert current["observed_at_start"] == "2026-07-09T10:00:00Z"


def test_conditional_temporal_patch_recovers_legacy_locator_when_times_already_match():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    document = _chunk_doc("legacy locator recovery")
    document["observed_at_start"] = "2026-07-09T10:00:00Z"
    document["observed_at_end"] = "2026-07-09T10:30:00Z"
    first = store.put(document)
    replacement_locator_hash = dm.build_source_locator_hash("legacy-locator")

    patched = store.patch_observed_time_if_content_hash(
        doc_id=document["_id"],
        expected_content_hash=document["content_hash"],
        expected_rev=first.rev,
        observed_at_start=document["observed_at_start"],
        observed_at_end=document["observed_at_end"],
        expected_source_locator_hash="",
        replacement_source_locator_hash=replacement_locator_hash,
    )

    current = store.get(document["_id"])
    assert current is not None
    assert patched.rev != first.rev
    assert current["observed_at_start"] == document["observed_at_start"]
    assert current["observed_at_end"] == document["observed_at_end"]
    assert current["source_locator_hash"] == replacement_locator_hash

    duplicate = store.patch_observed_time_if_content_hash(
        doc_id=document["_id"],
        expected_content_hash=document["content_hash"],
        expected_rev=patched.rev,
        observed_at_start=document["observed_at_start"],
        observed_at_end=document["observed_at_end"],
        expected_source_locator_hash=replacement_locator_hash,
        replacement_source_locator_hash=replacement_locator_hash,
    )

    assert duplicate.outcome == "duplicate"
    assert duplicate.rev == patched.rev


def test_conditional_temporal_patch_rejects_legacy_locator_cas_drift():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    document = _chunk_doc("legacy locator cas drift")
    document["source_locator_hash"] = dm.build_source_locator_hash("current-locator")
    first = store.put(document)

    with pytest.raises(SourceStoreConflict, match="locator changed"):
        store.patch_observed_time_if_content_hash(
            doc_id=document["_id"],
            expected_content_hash=document["content_hash"],
            expected_rev=first.rev,
            observed_at_start="2026-07-09T10:00:00Z",
            observed_at_end="2026-07-09T10:30:00Z",
            expected_source_locator_hash="",
            replacement_source_locator_hash=dm.build_source_locator_hash("replacement-locator"),
        )


def test_conditional_temporal_patch_rejects_invalid_legacy_locator_replacement():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    document = _chunk_doc("invalid legacy locator replacement")
    first = store.put(document)

    with pytest.raises(ValueError, match="replacement_source_locator_hash"):
        store.patch_observed_time_if_content_hash(
            doc_id=document["_id"],
            expected_content_hash=document["content_hash"],
            expected_rev=first.rev,
            observed_at_start="2026-07-09T10:00:00Z",
            observed_at_end="2026-07-09T10:30:00Z",
            expected_source_locator_hash="",
            replacement_source_locator_hash="",
        )


def test_conditional_temporal_patch_does_not_retry_a_revision_conflict():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    document = _chunk_doc("stable body")
    first = store.put(document)
    fake.put_conflict_once = True
    fake._conflicted = False

    with pytest.raises(SourceStoreConflict):
        store.patch_observed_time_if_content_hash(
            doc_id=document["_id"],
            expected_content_hash=document["content_hash"],
            expected_rev=first.rev,
            observed_at_start="2026-07-09T10:00:00Z",
            observed_at_end="2026-07-09T10:30:00Z",
        )

    current = store.get(document["_id"])
    assert current is not None
    assert current["_rev"] == first.rev
    assert current["observed_at_start"] == ""


def test_conditional_put_does_not_retry_a_revision_conflict():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    document = _session_doc()
    first = store.put(document)
    changed = dict(document)
    changed["materialized_at"] = "2026-07-16T03:00:00Z"
    fake.put_conflict_once = True
    fake._conflicted = False

    with pytest.raises(SourceStoreConflict):
        store.put_if_revision(changed, expected_rev=first.rev)

    current = store.get(document["_id"])
    assert current is not None
    assert current["_rev"] == first.rev
    assert current["materialized_at"] == ""


def test_session_aggregate_conflict_rereads_and_remerges_projector_currentness():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    existing = _session_doc()
    existing.update(
        {
            "started_at": "2026-07-09T10:00:00Z",
            "ended_at": "2026-07-09T11:00:00Z",
            "observed_at_start": "2026-07-09T10:00:00Z",
            "observed_at_end": "2026-07-09T11:00:00Z",
            "materialized_at": "2026-07-15T01:00:00Z",
            "source_hash": dm.sha256_hash("old-projector-state"),
            "source_status": "materialized",
        }
    )
    store.put(existing)

    concurrent_source_hash = dm.sha256_hash("concurrent-projector-state")

    def _commit_concurrent_projection(documents: dict[str, dict], doc_id: str) -> None:
        current = dict(documents[doc_id])
        current.update(
            {
                "_rev": "2-concurrent-projector",
                "materialized_at": "2026-07-16T02:00:00Z",
                "source_hash": concurrent_source_hash,
                "source_status": "materialized",
            }
        )
        documents[doc_id] = current

    fake.put_conflict_once = True
    fake.on_put_conflict = _commit_concurrent_projection
    incoming = _session_doc()
    incoming.update(
        {
            "started_at": "2026-07-09T09:00:00Z",
            "ended_at": "2026-07-09T13:00:00Z",
            "observed_at_start": "2026-07-09T09:00:00Z",
            "observed_at_end": "2026-07-09T13:00:00Z",
        }
    )

    revision = upsert_transcript_session_aggregate(store=store, incoming=incoming)

    current = store.get(existing["_id"])
    assert current is not None
    assert revision.outcome == "conflict_resolved"
    assert current["_rev"].startswith("3-")
    assert current["started_at"] == "2026-07-09T09:00:00Z"
    assert current["ended_at"] == "2026-07-09T13:00:00Z"
    assert current["observed_at_start"] == "2026-07-09T09:00:00Z"
    assert current["observed_at_end"] == "2026-07-09T13:00:00Z"
    assert current["materialized_at"] == "2026-07-16T02:00:00Z"
    assert current["source_hash"] == concurrent_source_hash
    assert current["source_status"] == "materialized"


def test_session_aggregate_conflict_retry_is_bounded_and_fail_closed():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    existing = _session_doc()
    store.put(existing)
    fake.put_conflict_always = True
    incoming = _session_doc()
    incoming["observed_at_end"] = "2026-07-09T13:00:00Z"

    with pytest.raises(SourceStoreConflict):
        store.merge_transcript_session_aggregate(incoming=incoming, max_attempts=2)

    current = store.get(existing["_id"])
    assert current is not None
    assert current["observed_at_end"] == ""


def test_find_by_session_filters_by_doc_type():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    store.put(_session_doc())
    store.put(_chunk_doc("a"))
    store.put(_chunk_doc("b"))
    chunks = store.find_by_session(session_id_hash=_sid(), doc_type=dm.SourceDocType.CONVERSATION_CHUNK)
    assert len(chunks) == 2
    everything = store.find_by_session(session_id_hash=_sid())
    assert len(everything) == 3


def test_find_by_session_follows_bookmarks_past_ten_thousand_documents():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    fake.dbs["transcript_source"].update(
        {
            f"conversation_chunk:{index:05d}": {
                "_id": f"conversation_chunk:{index:05d}",
                "doc_type": dm.SourceDocType.CONVERSATION_CHUNK,
                "session_id_hash": _sid(),
            }
            for index in range(10_001)
        }
    )

    documents = store.find_by_session(session_id_hash=_sid())

    assert len(documents) == 10_001
    assert documents[0]["_id"] == "conversation_chunk:00000"
    assert documents[-1]["_id"] == "conversation_chunk:10000"


def test_find_by_type_follows_bookmark_pages_and_applies_selector_fields():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    fake.dbs["transcript_source"].update(
        {
            "transcript_session:1": {
                "_id": "transcript_session:1",
                "doc_type": dm.SourceDocType.TRANSCRIPT_SESSION,
                "session_id_hash": "s1",
                "project": "neurons",
                "provider": "codex",
                "ignored": "x",
            },
            "transcript_session:2": {
                "_id": "transcript_session:2",
                "doc_type": dm.SourceDocType.TRANSCRIPT_SESSION,
                "session_id_hash": "s2",
                "project": "neurons",
                "provider": "codex",
                "ignored": "x",
            },
            "transcript_session:3": {
                "_id": "transcript_session:3",
                "doc_type": dm.SourceDocType.TRANSCRIPT_SESSION,
                "session_id_hash": "s3",
                "project": "neurons",
                "provider": "codex",
                "ignored": "x",
            },
            "transcript_session:other": {
                "_id": "transcript_session:other",
                "doc_type": dm.SourceDocType.TRANSCRIPT_SESSION,
                "session_id_hash": "other",
                "project": "other",
                "provider": "codex",
            },
        }
    )

    docs = store.find_by_type(
        dm.SourceDocType.TRANSCRIPT_SESSION,
        fields=["_id", "session_id_hash"],
        selector={"project": "neurons", "provider": "codex"},
        page_size=2,
    )

    assert docs == [
        {"_id": "transcript_session:1", "session_id_hash": "s1"},
        {"_id": "transcript_session:2", "session_id_hash": "s2"},
        {"_id": "transcript_session:3", "session_id_hash": "s3"},
    ]
    assert fake.find_bodies == [
        {
            "selector": {
                "doc_type": dm.SourceDocType.TRANSCRIPT_SESSION,
                "project": "neurons",
                "provider": "codex",
            },
            "limit": 2,
            "fields": ["_id", "session_id_hash"],
        },
        {
            "selector": {
                "doc_type": dm.SourceDocType.TRANSCRIPT_SESSION,
                "project": "neurons",
                "provider": "codex",
            },
            "limit": 2,
            "fields": ["_id", "session_id_hash"],
            "bookmark": "2",
        },
    ]


def test_find_by_type_limit_stops_pagination_after_requested_docs():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    for i in range(5):
        fake.dbs["transcript_source"][f"transcript_session:{i}"] = {
            "_id": f"transcript_session:{i}",
            "doc_type": dm.SourceDocType.TRANSCRIPT_SESSION,
            "session_id_hash": f"s{i}",
            "project": "neurons",
            "provider": "codex",
        }

    docs = store.find_by_type(
        dm.SourceDocType.TRANSCRIPT_SESSION,
        fields=["session_id_hash"],
        limit=3,
        page_size=2,
    )

    assert docs == [{"session_id_hash": "s0"}, {"session_id_hash": "s1"}, {"session_id_hash": "s2"}]
    assert [body["limit"] for body in fake.find_bodies] == [2, 1]


def test_delete():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    doc = _session_doc()
    store.put(doc)
    assert store.delete(doc["_id"]) is True
    assert store.get(doc["_id"]) is None
    assert store.delete(doc["_id"]) is False


def test_rejects_non_couchdb_owned_doc_type():
    store = _store(FakeCouch())
    store.ensure_database()
    with pytest.raises(dm.OwnershipViolation):
        store.put({"_id": "x:1", "doc_type": "transcript-memory", "session_id_hash": _sid()})


def test_http_error_raises():
    def boom(method, url, headers, body):
        return ProxyResponse(status_code=500, body=b'{"error":"server"}')

    store = CouchDBHttpSourceStore(base_url="http://couch.test:5984", db="x", transport=boom)
    with pytest.raises(CouchDBError):
        store.get("transcript_session:abc")


def test_completed_transport_response_is_not_discarded_after_deadline(monkeypatch):
    clock = {"now": 9.0}

    def completed_transport(method, url, headers, body):
        _ = method, url, headers, body
        clock["now"] = 11.0
        return ProxyResponse(status_code=201, body=b'{"ok":true}')

    monkeypatch.setattr(couchdb_http_store.time, "monotonic", lambda: clock["now"])
    store = CouchDBHttpSourceStore(
        base_url="http://couch.test:5984",
        db="x",
        transport=completed_transport,
        deadline_monotonic=10.0,
    )

    status, payload = store._request("PUT", "/x")

    assert status == 201
    assert payload == {"ok": True}


def test_expired_deadline_still_blocks_before_transport(monkeypatch):
    called = False

    def transport(method, url, headers, body):
        nonlocal called
        _ = method, url, headers, body
        called = True
        return ProxyResponse(status_code=201, body=b'{"ok":true}')

    monkeypatch.setattr(couchdb_http_store.time, "monotonic", lambda: 10.0)
    store = CouchDBHttpSourceStore(
        base_url="http://couch.test:5984",
        db="x",
        transport=transport,
        deadline_monotonic=10.0,
    )

    with pytest.raises(CouchDBError, match="operation deadline exceeded"):
        store._request("PUT", "/x")

    assert called is False


def test_http_aggregate_merge_propagates_contract_violation_without_put():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    stored = store.put(_session_doc())

    drifted = dict(_session_doc())
    drifted["project"] = "other-project"
    with pytest.raises(SourceContractViolation) as raised:
        store.merge_transcript_session_aggregate(incoming=drifted)

    assert raised.value.reason_code == "transcript_session_project_changed"
    current = store.get(_session_doc()["_id"])
    assert current["_rev"] == stored.rev
    assert current["project"] == "neurons"


def test_iter_by_session_remembers_no_sort_fallback_across_pages():
    # A database without an index for the `_id` sort rejects the first page with
    # no_usable_index. The index cannot appear mid-iteration, so later pages must
    # skip the sort up front instead of paying a rejected request per page.
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    for i in range(5):
        fake.dbs["transcript_source"][f"conversation_chunk:{i}"] = {
            "_id": f"conversation_chunk:{i}",
            "doc_type": dm.SourceDocType.CONVERSATION_CHUNK,
            "session_id_hash": "s0",
        }
    real_request = store._request
    rejected = []

    def _request(method, path, json_body=None, **kwargs):
        if path.endswith("/_find") and json_body is not None and "sort" in json_body:
            rejected.append(dict(json_body))
            return 400, {"error": "no_usable_index", "reason": "no index exists for this sort"}
        return real_request(method, path, json_body=json_body, **kwargs)

    store._request = _request

    docs = list(
        store.iter_by_session(
            session_id_hash="s0",
            doc_type=dm.SourceDocType.CONVERSATION_CHUNK,
            fields=["_id"],
            page_size=2,
        )
    )

    assert [doc["_id"] for doc in docs] == [f"conversation_chunk:{i}" for i in range(5)]
    assert len(rejected) == 1
    assert all("sort" not in body for body in fake.find_bodies)
    assert [body["limit"] for body in fake.find_bodies] == [2, 2, 2]


def test_iter_by_session_caches_no_usable_index_across_sessions_without_repeating_400():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    for s_idx in range(3):
        sid = dm.build_session_id_hash("codex", f"cached-sess-{s_idx}")
        for c_idx in range(5):
            doc_id = f"conversation_chunk:{s_idx}:{c_idx}"
            fake.dbs["transcript_source"][doc_id] = {
                "_id": doc_id,
                "doc_type": dm.SourceDocType.CONVERSATION_CHUNK,
                "session_id_hash": sid,
            }
    real_request = store._request
    rejected: list[dict] = []

    def _request(method, path, json_body=None, **kwargs):
        if path.endswith("/_find") and json_body is not None and "sort" in json_body:
            rejected.append(dict(json_body))
            return 400, {"error": "no_usable_index", "reason": "no index exists for this sort"}
        return real_request(method, path, json_body=json_body, **kwargs)

    store._request = _request  # type: ignore[method-assign]

    for s_idx in range(3):
        sid = dm.build_session_id_hash("codex", f"cached-sess-{s_idx}")
        docs = list(
            store.iter_by_session(
                session_id_hash=sid,
                doc_type=dm.SourceDocType.CONVERSATION_CHUNK,
                fields=["_id", "session_id_hash", "doc_type"],
                page_size=2,
            )
        )
        assert [doc["_id"] for doc in docs] == [f"conversation_chunk:{s_idx}:{c_idx}" for c_idx in range(5)]

    # Exactly one 400 probe for (db, doc_type, selector_shape, sort_spec) across all 3 sessions.
    assert len(rejected) == 1


def test_iter_by_session_no_usable_index_cache_scopes_by_doc_type_selector_and_recovers_after_ttl(monkeypatch):
    from agent_knowledge.couchdb_source.source_revision import (
        SourceRevisionSnapshotChangedError,
        resolve_source_hash_for_currentness,
    )

    clock = {"now": 100.0}
    monkeypatch.setattr(couchdb_http_store.time, "monotonic", lambda: clock["now"])

    fake = FakeCouch()
    store = CouchDBHttpSourceStore(
        base_url="http://couch.test:5984",
        db="transcript_source",
        transport=fake,
        no_usable_index_ttl_seconds=60.0,
    )
    store.ensure_database()

    sid_a = dm.build_session_id_hash("codex", "scope-a")
    sid_b = dm.build_session_id_hash("codex", "scope-b")
    for sid, label in ((sid_a, "a"), (sid_b, "b")):
        store.put(
            dm.build_transcript_session_document(
                session=TranscriptSession(
                    session_id_hash=sid,
                    provider="codex",
                    project="neurons",
                    started_at="2026-11-01T00:00:00Z",
                    ended_at="2026-11-01T00:05:00Z",
                )
            )
        )
        for idx in range(3):
            chunk = TranscriptChunk.from_text(
                chunk_id=f"chunk_{label}_{idx}",
                session_id_hash=sid,
                provider="codex",
                project="neurons",
                turn_start_index=idx,
                turn_end_index=idx,
                text=f"body {label} {idx}",
            )
            store.put(dm.build_conversation_chunk_document(chunk=chunk))
        fake.dbs["transcript_source"][f"tool_evidence_bundle:{label}:0"] = {
            "_id": f"tool_evidence_bundle:{label}:0",
            "doc_type": dm.SourceDocType.TOOL_EVIDENCE_BUNDLE,
            "session_id_hash": sid,
            "provider": "codex",
            "project": "neurons",
            "part_index": 1,
            "part_count": 1,
            "evidence_index_start": 0,
            "evidence_index_end": 0,
            "coverage_hash": dm.sha256_hash(f"cov-{label}"),
        }

    chunk_index_ready = {"value": False}
    rejected_chunk_sorts = 0
    accepted_bundle_sorts = 0
    accepted_chunk_sorts = 0
    reverse_unsorted = {"value": True}
    real_request = store._request

    def _request(method, path, json_body=None, **kwargs):
        nonlocal rejected_chunk_sorts, accepted_bundle_sorts, accepted_chunk_sorts
        if path.endswith("/_find") and json_body is not None:
            doc_type = (json_body.get("selector") or {}).get("doc_type")
            if "sort" in json_body:
                if doc_type == dm.SourceDocType.CONVERSATION_CHUNK and not chunk_index_ready["value"]:
                    rejected_chunk_sorts += 1
                    return 400, {"error": "no_usable_index", "reason": "missing chunk sort index"}
                if doc_type == dm.SourceDocType.TOOL_EVIDENCE_BUNDLE:
                    accepted_bundle_sorts += 1
                if doc_type == dm.SourceDocType.CONVERSATION_CHUNK and chunk_index_ready["value"]:
                    accepted_chunk_sorts += 1
                return real_request(method, path, json_body=json_body, **kwargs)
            # Simulate fallback index returning docs in reverse _id order when sort is omitted.
            status, payload = real_request(method, path, json_body=json_body, **kwargs)
            if status == 200 and reverse_unsorted["value"] and doc_type == dm.SourceDocType.CONVERSATION_CHUNK:
                matching = list(reversed(fake._matching_docs(fake.dbs["transcript_source"], json_body["selector"])))
                start = int(str(json_body.get("bookmark") or "0"))
                limit = int(json_body.get("limit") or len(matching) or 1)
                page = fake._project_fields(matching[start : start + limit], json_body.get("fields") or [])
                out = {"docs": page}
                if start + limit < len(matching):
                    out["bookmark"] = str(start + limit)
                return 200, out
            return status, payload
        return real_request(method, path, json_body=json_body, **kwargs)

    store._request = _request  # type: ignore[method-assign]

    # 1) Partial index isolation: conversation_chunk caches no_usable_index once,
    # while tool_evidence_bundle keeps sending sort=[{"_id": "asc"}].
    hash_a_unsorted = resolve_source_hash_for_currentness(session_id_hash=sid_a, store=store)
    hash_b_unsorted = resolve_source_hash_for_currentness(session_id_hash=sid_b, store=store)
    assert rejected_chunk_sorts == 1
    assert accepted_bundle_sorts >= 4

    # 2) Future index creation: once TTL expires, sort=[{"_id": "asc"}] is re-probed
    # and produces the exact same source_hash (zero semantic drift).
    chunk_index_ready["value"] = True
    clock["now"] = 161.0
    hash_a_sorted = resolve_source_hash_for_currentness(session_id_hash=sid_a, store=store)
    hash_b_sorted = resolve_source_hash_for_currentness(session_id_hash=sid_b, store=store)
    assert accepted_chunk_sorts >= 4
    assert hash_a_sorted == hash_a_unsorted
    assert hash_b_sorted == hash_b_unsorted

    # 3) Snapshot race fail-closed semantics preserved even when no_usable_index is cached:
    chunk_index_ready["value"] = False
    clock["now"] = 300.0
    # Prime the cache on sid_a first so sid_b runs with zero 400 round-trips.
    _ = resolve_source_hash_for_currentness(session_id_hash=sid_a, store=store)
    rejected_before_race = rejected_chunk_sorts

    chunk_reads = {"n": 0}
    base_request = store._request

    def _mutating_request(method, path, json_body=None, **kwargs):
        status, payload = base_request(method, path, json_body=json_body, **kwargs)
        if (
            path.endswith("/_find")
            and json_body is not None
            and (json_body.get("selector") or {}).get("session_id_hash") == sid_b
            and (json_body.get("selector") or {}).get("doc_type") == dm.SourceDocType.CONVERSATION_CHUNK
            and not json_body.get("bookmark")
        ):
            chunk_reads["n"] += 1
            # Mutate sid_b chunk hash on every scan so both attempts see drift -> fail-closed.
            for doc in fake.dbs["transcript_source"].values():
                if doc.get("session_id_hash") == sid_b and doc.get("doc_type") == dm.SourceDocType.CONVERSATION_CHUNK:
                    doc["content_hash"] = dm.sha256_hash(f"drift-{chunk_reads['n']}")
                    break
        return status, payload

    store._request = _mutating_request  # type: ignore[method-assign]
    with pytest.raises(SourceRevisionSnapshotChangedError):
        resolve_source_hash_for_currentness(session_id_hash=sid_b, store=store)
    assert rejected_chunk_sorts == rejected_before_race


def test_iter_by_session_pins_unsorted_mode_across_pages_even_if_ttl_expires_mid_iteration(monkeypatch):
    clock = {"now": 50.0}
    monkeypatch.setattr(couchdb_http_store.time, "monotonic", lambda: clock["now"])

    fake = FakeCouch()
    store = CouchDBHttpSourceStore(
        base_url="http://couch.test:5984",
        db="transcript_source",
        transport=fake,
        no_usable_index_ttl_seconds=10.0,
    )
    store.ensure_database()
    for s_label in ("prime", "paged"):
        sid = dm.build_session_id_hash("codex", f"mid-ttl-{s_label}")
        for idx in range(5):
            doc_id = f"conversation_chunk:{s_label}:{idx}"
            fake.dbs["transcript_source"][doc_id] = {
                "_id": doc_id,
                "doc_type": dm.SourceDocType.CONVERSATION_CHUNK,
                "session_id_hash": sid,
            }

    index_created = {"value": False}
    sort_flags_by_call: list[bool] = []
    real_request = store._request

    def _request(method, path, json_body=None, **kwargs):
        if path.endswith("/_find") and json_body is not None:
            has_sort = "sort" in json_body
            sort_flags_by_call.append(has_sort)
            if has_sort and not index_created["value"]:
                return 400, {"error": "no_usable_index", "reason": "no index"}
            status, payload = real_request(method, path, json_body=json_body, **kwargs)
            # Advance clock past TTL and mark index ready right after page 1 of "paged" session.
            if json_body.get("bookmark") == "" or "bookmark" not in json_body:
                if (json_body.get("selector") or {}).get("session_id_hash") == dm.build_session_id_hash("codex", "mid-ttl-paged"):
                    clock["now"] = 100.0
                    index_created["value"] = True
            return status, payload
        return real_request(method, path, json_body=json_body, **kwargs)

    store._request = _request  # type: ignore[method-assign]

    # Prime no_usable_index cache on first session.
    _ = list(
        store.iter_by_session(
            session_id_hash=dm.build_session_id_hash("codex", "mid-ttl-prime"),
            doc_type=dm.SourceDocType.CONVERSATION_CHUNK,
            fields=["_id"],
            page_size=2,
        )
    )
    sort_flags_by_call.clear()

    # Second session starts while cache is valid; TTL expires after page 1,
    # yet pages 2 and 3 must stay unsorted so bookmarks are never mixed across plans.
    paged_docs = list(
        store.iter_by_session(
            session_id_hash=dm.build_session_id_hash("codex", "mid-ttl-paged"),
            doc_type=dm.SourceDocType.CONVERSATION_CHUNK,
            fields=["_id"],
            page_size=2,
        )
    )
    assert len(paged_docs) == 5
    assert sort_flags_by_call == [False, False, False]

    # Next fresh iter_by_session call now sees the expired TTL and uses the new sort index.
    sort_flags_by_call.clear()
    fresh_docs = list(
        store.iter_by_session(
            session_id_hash=dm.build_session_id_hash("codex", "mid-ttl-paged"),
            doc_type=dm.SourceDocType.CONVERSATION_CHUNK,
            fields=["_id"],
            page_size=2,
        )
    )
    assert len(fresh_docs) == 5
    assert sort_flags_by_call == [True, True, True]


def test_no_usable_index_cache_enforces_entry_bound_and_never_bypasses_page_byte_cap():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()

    # Bound check: inserting >64 distinct selector shapes never grows cache past 64 entries.
    for idx in range(80):
        store._remember_unusable_sort(("transcript_source", "conversation_chunk", ((f"k{idx}", "$scalar"),), ()))
    assert len(store._no_usable_index_until) <= 64

    # Security/bound check: even when no_usable_index is cached (unsorted fast path),
    # max_page_bytes cap still fails closed with CouchDBResponseTooLarge.
    sid = dm.build_session_id_hash("codex", "byte-cap-cached")
    fake.dbs["transcript_source"]["conversation_chunk:oversized"] = {
        "_id": "conversation_chunk:oversized",
        "doc_type": dm.SourceDocType.CONVERSATION_CHUNK,
        "session_id_hash": sid,
        "body": "x" * 2048,
    }
    prime_sid = dm.build_session_id_hash("codex", "byte-cap-prime")
    real_request = store._request

    def _reject_sort(method, path, json_body=None, **kwargs):
        if path.endswith("/_find") and json_body is not None and "sort" in json_body:
            return 400, {"error": "no_usable_index", "reason": "no index"}
        return real_request(method, path, json_body=json_body, **kwargs)

    store._request = _reject_sort  # type: ignore[method-assign]
    assert list(
        store.iter_by_session(
            session_id_hash=prime_sid,
            doc_type=dm.SourceDocType.CONVERSATION_CHUNK,
            page_size=2,
            max_page_bytes=128,
        )
    ) == []
    with pytest.raises(CouchDBResponseTooLarge):
        list(
            store.iter_by_session(
                session_id_hash=sid,
                doc_type=dm.SourceDocType.CONVERSATION_CHUNK,
                page_size=2,
                max_page_bytes=128,
            )
        )


def test_no_usable_index_cache_avoids_deep_list_type_and_partial_index_value_collisions():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    sid = dm.build_session_id_hash("codex", "partial-idx-session")
    fake.dbs["transcript_source"]["conversation_chunk:active"] = {
        "_id": "conversation_chunk:active",
        "doc_type": dm.SourceDocType.CONVERSATION_CHUNK,
        "session_id_hash": sid,
        "active": True,
        "flag": True,
    }
    fake.dbs["transcript_source"]["conversation_chunk:inactive"] = {
        "_id": "conversation_chunk:inactive",
        "doc_type": dm.SourceDocType.CONVERSATION_CHUNK,
        "session_id_hash": sid,
        "active": False,
        "flag": 1,
    }

    # Build two deep (>=10 levels) and wide (>16 list elements) selectors that differ
    # only beyond depth 8 / index 16: they must NOT collide in the cache key.
    deep_a: dict = {"leaf": "alpha"}
    deep_b: dict = {"leaf": "beta"}
    for level in range(10):
        deep_a = {f"lvl_{level}": deep_a}
        deep_b = {f"lvl_{level}": deep_b}
    wide_a = {"tags": {"$in": [f"t{i}" for i in range(16)] + ["tail_a"]}}
    wide_b = {"tags": {"$in": [f"t{i}" for i in range(16)] + ["tail_b"]}}

    # Simulate a Mango partial index with `partial_filter_selector: {"active": true}` and
    # another with `{"flag": true}` (distinct from integer `1`), plus usable indexes on
    # `deep_b` and `wide_b` while `deep_a` and `wide_a` have no usable index.
    sorted_Accepted: list[dict] = []
    real_request = store._request

    def _request(method, path, json_body=None, **kwargs):
        if path.endswith("/_find") and json_body is not None and "sort" in json_body:
            sel = json_body.get("selector") or {}
            if sel.get("active") is False:
                return 400, {"error": "no_usable_index", "reason": "partial index requires active=true"}
            if "flag" in sel and sel.get("flag") is not True:
                return 400, {"error": "no_usable_index", "reason": "partial index requires boolean true"}
            if "lvl_9" in sel and sel == {**deep_a, "session_id_hash": sid, "doc_type": dm.SourceDocType.CONVERSATION_CHUNK}:
                return 400, {"error": "no_usable_index", "reason": "deep_a unindexed"}
            if "tags" in sel and sel["tags"]["$in"][-1] == "tail_a":
                return 400, {"error": "no_usable_index", "reason": "wide_a unindexed"}
            sorted_Accepted.append(dict(sel))
        return real_request(method, path, json_body=json_body, **kwargs)

    store._request = _request  # type: ignore[method-assign]

    # 1) `active=False` hits 400 and caches unusable sort, but `active=True` matches the
    # partial index and MUST still send `sort=[{"_id": "asc"}]` instead of colliding.
    _ = list(
        store.iter_by_session(
            session_id_hash=sid,
            doc_type=dm.SourceDocType.CONVERSATION_CHUNK,
            selector={"active": False},
        )
    )
    active_docs = list(
        store.iter_by_session(
            session_id_hash=sid,
            doc_type=dm.SourceDocType.CONVERSATION_CHUNK,
            selector={"active": True},
        )
    )
    assert [d["_id"] for d in active_docs] == ["conversation_chunk:active"]
    assert any(sel.get("active") is True for sel in sorted_Accepted)

    # 2) `flag=1` (int) must not poison `flag=True` (bool) despite `1 == True` in Python.
    _ = list(
        store.iter_by_session(
            session_id_hash=sid,
            doc_type=dm.SourceDocType.CONVERSATION_CHUNK,
            selector={"flag": 1},
        )
    )
    _ = list(
        store.iter_by_session(
            session_id_hash=sid,
            doc_type=dm.SourceDocType.CONVERSATION_CHUNK,
            selector={"flag": True},
        )
    )
    assert any(sel.get("flag") is True for sel in sorted_Accepted)

    # 3) Selectors differing beyond depth 8 or list index 16 must not poison each other.
    _ = list(
        store.iter_by_session(
            session_id_hash=sid,
            doc_type=dm.SourceDocType.CONVERSATION_CHUNK,
            selector=deep_a,
        )
    )
    _ = list(
        store.iter_by_session(
            session_id_hash=sid,
            doc_type=dm.SourceDocType.CONVERSATION_CHUNK,
            selector=deep_b,
        )
    )
    assert any("lvl_9" in sel and sel != {**deep_a, "session_id_hash": sid, "doc_type": dm.SourceDocType.CONVERSATION_CHUNK} for sel in sorted_Accepted)

    _ = list(
        store.iter_by_session(
            session_id_hash=sid,
            doc_type=dm.SourceDocType.CONVERSATION_CHUNK,
            selector=wide_a,
        )
    )
    _ = list(
        store.iter_by_session(
            session_id_hash=sid,
            doc_type=dm.SourceDocType.CONVERSATION_CHUNK,
            selector=wide_b,
        )
    )
    assert any("tags" in sel and sel["tags"]["$in"][-1] == "tail_b" for sel in sorted_Accepted)


def test_iter_by_session_fails_closed_if_sort_index_disappears_after_sorted_bookmark():
    fake = FakeCouch()
    store = _store(fake)
    store.ensure_database()
    sid = dm.build_session_id_hash("codex", "mid-page-drop")
    for idx in range(5):
        fake.dbs["transcript_source"][f"conversation_chunk:{idx}"] = {
            "_id": f"conversation_chunk:{idx}",
            "doc_type": dm.SourceDocType.CONVERSATION_CHUNK,
            "session_id_hash": sid,
        }

    index_alive = {"value": True}
    unsorted_with_bookmark = []
    real_request = store._request

    def _request(method, path, json_body=None, **kwargs):
        if path.endswith("/_find") and json_body is not None:
            if "sort" in json_body:
                if not index_alive["value"]:
                    return 400, {"error": "no_usable_index", "reason": "index dropped mid-scan"}
                status, payload = real_request(method, path, json_body=json_body, **kwargs)
                # Drop the sort index right after page 1 returns a sorted bookmark.
                index_alive["value"] = False
                return status, payload
            if json_body.get("bookmark") and not index_alive["value"] and len(unsorted_with_bookmark) == 0:
                # Track if any request illegally sent a sorted-plan bookmark without sort.
                if str(json_body.get("bookmark")) == "2":
                    unsorted_with_bookmark.append(dict(json_body))
        return real_request(method, path, json_body=json_body, **kwargs)

    store._request = _request  # type: ignore[method-assign]

    # Mid-pagination index disappearance MUST fail closed instead of reusing the sorted
    # bookmark ("2") on an unsorted fallback query.
    with pytest.raises(CouchDBError) as exc_info:
        it = store.iter_by_session(
            session_id_hash=sid,
            doc_type=dm.SourceDocType.CONVERSATION_CHUNK,
            fields=["_id"],
            page_size=2,
        )
        first = next(it)
        assert first["_id"] == "conversation_chunk:0"
        second = next(it)
        assert second["_id"] == "conversation_chunk:1"
        _ = list(it)

    assert exc_info.value.status_code == 400
    assert unsorted_with_bookmark == []

    # A subsequent fresh iteration starts cleanly from page 1 (bookmark="") without sort
    # and yields the complete 5-document set without repeating a 400.
    recovered_docs = list(
        store.iter_by_session(
            session_id_hash=sid,
            doc_type=dm.SourceDocType.CONVERSATION_CHUNK,
            fields=["_id"],
            page_size=2,
        )
    )
    assert [d["_id"] for d in recovered_docs] == [f"conversation_chunk:{i}" for i in range(5)]
