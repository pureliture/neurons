from __future__ import annotations

import hashlib
import json
from urllib.parse import urlparse

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBError, CouchDBHttpSourceStore
from agent_knowledge.couchdb_source.source_store import CouchDBSourceStore, SourceStoreConflict
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

        if len(parts) == 2:
            doc_id = parts[1]
            if method == "GET":
                doc = store.get(doc_id)
                return self._json(200, doc) if doc else self._json(404, {"error": "not_found"})
            if method == "PUT":
                incoming = json.loads(body.decode())
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


def test_satisfies_protocol():
    assert isinstance(_store(FakeCouch()), CouchDBSourceStore)


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
