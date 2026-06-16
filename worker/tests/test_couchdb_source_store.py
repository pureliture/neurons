from __future__ import annotations

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_store import (
    CouchDBSourceStore,
    InMemoryCouchDBSourceStore,
    SourceStoreError,
)
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession


def _sid() -> str:
    return dm.build_session_id_hash("codex", "sess-001")


def _session_doc() -> dict:
    session = TranscriptSession(
        session_id_hash=_sid(),
        provider="codex",
        project="neurons",
        started_at="2026-06-17T01:00:00Z",
    )
    return dm.build_transcript_session_document(session=session)


def _chunk(text: str) -> TranscriptChunk:
    # chunk_id is content-addressed off the text so distinct text -> distinct id.
    seed = "chunk_" + dm.sha256_hash(text).split(":", 1)[1][:16]
    return TranscriptChunk.from_text(
        chunk_id=seed,
        session_id_hash=_sid(),
        provider="codex",
        project="neurons",
        turn_start_index=0,
        turn_end_index=2,
        text=text,
    )


def _chunk_doc(text: str) -> dict:
    return dm.build_conversation_chunk_document(chunk=_chunk(text))


def test_inmemory_store_satisfies_protocol() -> None:
    assert isinstance(InMemoryCouchDBSourceStore(), CouchDBSourceStore)


def test_put_then_get_roundtrip() -> None:
    store = InMemoryCouchDBSourceStore()
    doc = _session_doc()
    rev = store.put(doc)
    assert rev.outcome == "accepted"
    assert rev.rev.startswith("1-")
    got = store.get(doc["_id"])
    assert got is not None
    assert got["_rev"] == rev.rev
    assert got["doc_type"] == dm.SourceDocType.TRANSCRIPT_SESSION


def test_put_is_idempotent_for_identical_content() -> None:
    store = InMemoryCouchDBSourceStore()
    doc = _chunk_doc("same body")
    first = store.put(doc)
    second = store.put(_chunk_doc("same body"))
    assert second.outcome == "duplicate"
    assert second.rev == first.rev  # no revision churn on identical re-put


def test_put_conflict_resolved_bumps_rev_for_changed_content() -> None:
    store = InMemoryCouchDBSourceStore()
    first = store.put(_chunk_doc("original body"))
    # same deterministic _id (same session + part_index) but different content
    changed = _chunk_doc("original body")
    changed["body"] = "edited body"
    changed["content_hash"] = dm.sha256_hash("edited body")
    second = store.put(changed)
    assert second.outcome == "conflict_resolved"
    assert second.rev.startswith("2-")
    assert second.rev != first.rev


def test_store_rejects_non_couchdb_owned_doc_type() -> None:
    store = InMemoryCouchDBSourceStore()
    with pytest.raises(dm.OwnershipViolation):
        store.put({"_id": "x:1", "doc_type": "transcript-memory", "session_id_hash": _sid()})


def test_store_rejects_document_without_id() -> None:
    store = InMemoryCouchDBSourceStore()
    with pytest.raises(SourceStoreError):
        store.put({"doc_type": dm.SourceDocType.TRANSCRIPT_SESSION})


def test_store_rejects_body_with_leak_defense_in_depth() -> None:
    store = InMemoryCouchDBSourceStore()
    leaking = {
        "_id": dm.conversation_chunk_doc_id(_sid(), "chunk_x"),
        "doc_type": dm.SourceDocType.CONVERSATION_CHUNK,
        "session_id_hash": _sid(),
        "content_hash": dm.sha256_hash("x"),
        "body": "leaked " + "/Users/" + "exampleuser/secret.md",
    }
    with pytest.raises(dm.SourceRedactionLeak):
        store.put(leaking)


def test_find_by_session_filters_by_doc_type() -> None:
    store = InMemoryCouchDBSourceStore()
    store.put(_session_doc())
    chunk_a, chunk_b = _chunk("body a"), _chunk("body b")
    store.put(dm.build_conversation_chunk_document(chunk=chunk_a))
    store.put(dm.build_conversation_chunk_document(chunk=chunk_b))

    chunks = store.find_by_session(
        session_id_hash=_sid(), doc_type=dm.SourceDocType.CONVERSATION_CHUNK
    )
    assert len(chunks) == 2
    assert {c["_id"] for c in chunks} == {
        dm.conversation_chunk_doc_id(_sid(), chunk_a.chunk_id),
        dm.conversation_chunk_doc_id(_sid(), chunk_b.chunk_id),
    }

    everything = store.find_by_session(session_id_hash=_sid())
    assert len(everything) == 3


def test_get_returns_independent_copy() -> None:
    store = InMemoryCouchDBSourceStore()
    doc = _session_doc()
    store.put(doc)
    got = store.get(doc["_id"])
    got["provider"] = "tampered"
    assert store.get(doc["_id"])["provider"] == "codex"
