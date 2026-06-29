"""M3 materializer must de-overlap same-session conversation chunks.

When a grown session is re-shipped (a longer chunk that subsumes an earlier shorter
one), the canonical materializer must embed only the longer chunk — not both — so
recall is not duplicated. Coverage gating stays on the STORED chunk count.
"""

from __future__ import annotations

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.session_memory_materializer import materialize_session_memory
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

PROVIDER = "hermes"
PROJECT = "neurons"


def _store_with_session(sid):
    store = InMemoryCouchDBSourceStore()
    store.put(
        dm.build_transcript_session_document(
            session=TranscriptSession(
                session_id_hash=sid, provider=PROVIDER, project=PROJECT, started_at="2026-06-17T01:00:00Z"
            )
        )
    )
    return store


def _put_chunk(store, sid, *, chunk_id, turn_start, turn_end, text):
    chunk = TranscriptChunk.from_text(
        chunk_id=chunk_id,
        session_id_hash=sid,
        provider=PROVIDER,
        project=PROJECT,
        turn_start_index=turn_start,
        turn_end_index=turn_end,
        text=text,
    )
    doc = dm.build_conversation_chunk_document(chunk=chunk)
    store.put(doc)
    return doc


def _put_coverage(store, sid, content_hashes):
    store.put(
        dm.build_coverage_manifest_document(
            session_id_hash=sid,
            provider=PROVIDER,
            project=PROJECT,
            conversation_chunk_count=len(content_hashes),
            tool_evidence_bundle_count=0,
            conversation_content_hashes=content_hashes,
            tool_evidence_coverage_hashes=[],
            project_authority={"project": PROJECT, "ambiguous": False, "eligible_for_retirement": True},
        )
    )


def test_materialize_drops_subsumed_overlapping_chunk():
    sid = dm.build_session_id_hash(PROVIDER, "sess-grown")
    store = _store_with_session(sid)
    short = _put_chunk(store, sid, chunk_id="chunk_short", turn_start=2, turn_end=3, text="beta gamma")
    longer = _put_chunk(store, sid, chunk_id="chunk_long", turn_start=1, turn_end=4, text="alpha beta gamma delta")
    _put_coverage(store, sid, [short["content_hash"], longer["content_hash"]])

    mat = materialize_session_memory(session_id_hash=sid, store=store)

    # longer chunk present; the subsumed shorter chunk's text appears only once
    # (inside the longer body), proving the short chunk was not separately embedded.
    assert "alpha beta gamma delta" in mat.body
    assert mat.body.count("beta gamma") == 1
    # coverage uses the STORED count (2 chunks both present), so still fully materialized
    assert mat.fully_materialized is True
    # the de-overlap is auditable via a note (one chunk dropped from the body)
    assert any(note.startswith("deoverlapped_") for note in mat.notes)


def test_materialize_keeps_non_overlapping_chunks():
    sid = dm.build_session_id_hash(PROVIDER, "sess-distinct")
    store = _store_with_session(sid)
    a = _put_chunk(store, sid, chunk_id="chunk_a", turn_start=1, turn_end=2, text="first window content")
    b = _put_chunk(store, sid, chunk_id="chunk_b", turn_start=3, turn_end=4, text="second window content")
    _put_coverage(store, sid, [a["content_hash"], b["content_hash"]])

    mat = materialize_session_memory(session_id_hash=sid, store=store)

    assert "first window content" in mat.body
    assert "second window content" in mat.body
    assert mat.fully_materialized is True
