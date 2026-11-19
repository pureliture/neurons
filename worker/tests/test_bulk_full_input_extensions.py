"""TDD: evidence lane, bounded streaming, source pin/currentness."""

from __future__ import annotations

import os

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core.bulk_full_input import (
    SourceInvalid,
    assert_source_current,
    iter_bounded_chunks,
)
from agent_knowledge.llm_brain_core.runtime import session_source_revision_from_couchdb_source
from agent_knowledge.llm_brain_core.bulk_full_input import FullInputBuilder
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

P = "synthetic-provider"
PR = "synthetic-project"


def _doc(i: int, body: str) -> dict:
    return {"_id": f"member-{i}", "turn_start_index": i, "turn_end_index": i,
            "part_index": 0, "char_start": 0, "body": body}


def test_evidence_lane_is_separate_from_conversation_coverage():
    conversation = [_doc(0, "conversation body")]
    evidence = [{"_id": "ev-0", "part_index": 0, "evidence_index_start": 0,
                 "body": "evidence body"}]
    builder = FullInputBuilder(conversation, session_hash="s", source_hash="src",
                               policy_version="v", run_id="r", evidence_documents=evidence)
    assert builder.plan.total_bytes == len("conversation body")
    assert builder.evidence_plan.total_bytes == len("evidence body")
    assert builder.render_evidence(0) == "evidence body"
    assert all(chunk.lane == "evidence" for chunk in builder.evidence_plan.chunks)


def test_bounded_streaming_keeps_at_most_two_member_bodies():
    docs = [_doc(i, "x" * 100) for i in range(20)]
    seen = []
    for ordinal, data in iter_bounded_chunks(docs, max_resident_bodies=2):
        seen.append((ordinal, len(data)))
    assert [ordinal for ordinal, _ in seen] == list(range(20))
    assert all(length == 100 for _, length in seen)


def test_source_pin_rejects_changed_revision():
    store = InMemoryCouchDBSourceStore()
    sid = dm.build_session_id_hash(P, "synthetic-session")
    store.put(dm.build_transcript_session_document(
        session=TranscriptSession(session_id_hash=sid, provider=P, project=PR,
                                  started_at="2026-01-01T00:00:00Z")))
    chunk = TranscriptChunk.from_text(chunk_id="chunk_0", session_id_hash=sid,
                                      provider=P, project=PR, turn_start_index=0,
                                      turn_end_index=0, text="body")
    doc = dm.build_conversation_chunk_document(chunk=chunk)
    store.put(doc)
    current = session_source_revision_from_couchdb_source(session_id_hash=sid, source_store=store)
    assert_source_current(session_id_hash=sid, source_store=store, expected_hash=current)
    with pytest.raises(SourceInvalid):
        assert_source_current(session_id_hash=sid, source_store=store,
                              expected_hash="sha256:" + "0" * 64)


def test_privacy_gate_blocks_synthetic_marker_before_input_render():
    from agent_knowledge.llm_brain_core.bulk_full_input import (
        PrivacyGateRejected,
        make_full_chunk_session_input,
    )
    builder = FullInputBuilder([_doc(0, "public SYNTHETIC_PRIVATE_MARKER")],
                               session_hash="s", source_hash="src",
                               policy_version="v", run_id="r")
    with pytest.raises(PrivacyGateRejected):
        make_full_chunk_session_input(builder=builder, ordinal=0, max_bytes=1000,
                                      forbidden_markers=(b"SYNTHETIC_PRIVATE_MARKER",))
