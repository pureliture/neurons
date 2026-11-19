"""Direct legacy/current hash-contract tests on synthetic in-memory docs."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path

import pytest

from agent_knowledge.couchdb_source import couchdb_currentness_stream as cs
from agent_knowledge.couchdb_source import source_revision as sr
from agent_knowledge.couchdb_source.document_model import (
    SourceDocType,
    build_conversation_chunk_document,
    build_tool_evidence_bundle_document,
    build_transcript_session_document,
    sha256_hash,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession


def _store(tmp_path: Path) -> tuple[InMemoryCouchDBSourceStore, str, dict]:
    store = InMemoryCouchDBSourceStore()
    sid = sr.dm.build_session_id_hash("synthetic-provider", tmp_path.name)
    store.put(
        build_transcript_session_document(
            session=TranscriptSession(
                session_id_hash=sid,
                provider="synthetic-provider",
                project="legacy-contract",
                started_at="2026-01-01T00:00:00Z",
                ended_at="2026-01-01T00:01:00Z",
            )
        )
    )
    chunk = TranscriptChunk.from_text(
        chunk_id="chunk_0",
        session_id_hash=sid,
        provider="synthetic-provider",
        project="legacy-contract",
        turn_start_index=0,
        turn_end_index=0,
        text="legacy body",
    )
    doc = build_conversation_chunk_document(chunk=chunk)
    store.put(doc)
    store.put(
        build_tool_evidence_bundle_document(
            session_id_hash=sid,
            provider="synthetic-provider",
            project="legacy-contract",
            part_index=0,
            part_count=1,
            evidence_index_start=0,
            evidence_index_end=1,
            record_content_hashes=[str(doc["content_hash"])],
            body="synthetic bundle body",
        )
    )
    return store, sid, doc


def test_legacy_batch_equals_per_session_hash(tmp_path):
    store, sid, _doc = _store(tmp_path)
    batch = sr.iter_legacy_source_hashes(store)
    components = sr._legacy_currentness_components(
        store=store,
        session_id_hash=sid,
        budget={"rows": 0, "compact_bytes": 0},
    )
    assert batch[sid] == sr._source_hash_from_currentness_components(components)


def test_normal_legacy_hash_is_stable_and_d45_compatible(tmp_path):
    store, sid, _doc = _store(tmp_path)
    # d45 legacy construction: identical projected fields and component order.
    expected = sr._source_hash_from_currentness_components(
        [
            sr._currentness_component(doc, budget={"rows": 1, "compact_bytes": 0})
            for doc in (
                next(store.iter_by_type(SourceDocType.TRANSCRIPT_SESSION)),
                next(store.iter_by_type(SourceDocType.CONVERSATION_CHUNK)),
                next(store.iter_by_type(SourceDocType.TOOL_EVIDENCE_BUNDLE)),
            )
        ]
    )
    assert sr.iter_legacy_source_hashes(store)[sid] == expected


def test_nfc_and_nfd_bodies_are_preserved_and_hashed_exactly(tmp_path):
    store = InMemoryCouchDBSourceStore()
    sid = sr.dm.build_session_id_hash("synthetic-provider", tmp_path.name)
    bodies = {
        "nfc": unicodedata.normalize("NFC", "가"),
        "nfd": unicodedata.normalize("NFD", "가"),
    }
    for label, body in bodies.items():
        chunk = TranscriptChunk.from_text(
            chunk_id=f"chunk_{label}",
            session_id_hash=sid,
            provider="synthetic-provider",
            project="unicode-contract",
            turn_start_index=0,
            turn_end_index=0,
            text=body,
        )
        store.put(build_conversation_chunk_document(chunk=chunk))

    rows_by_id = {}
    for document in store.iter_by_type(SourceDocType.CONVERSATION_CHUNK):
        raw = json.dumps({"docs": [document], "bookmark": ""}, ensure_ascii=False).encode("utf-8")
        page = tmp_path / f"{document['_id']}.json"
        page.write_bytes(raw)
        rows, _ = cs.parse_spooled_descriptor_page(page, max_page_bytes=len(raw) + 1024, max_items=8)
        rows_by_id[document["_id"]] = (document, rows[0])

    assert len(rows_by_id) == 2
    hashes = []
    for document, row in rows_by_id.values():
        expected_hash = sha256_hash(document["body"])
        assert row["__body_hash"] == expected_hash
        assert row["__source_document_hash"] == sr._source_document_hash(document)
        hashes.append(row["__body_hash"])
    assert len(hashes) == 2
    assert hashes[0] != hashes[1]


def test_body_mismatch_fail_closed_reason_and_batch_omits_session(tmp_path, monkeypatch):
    store, sid, doc = _store(tmp_path)
    tampered = dict(doc)
    tampered["_rev"] = "2-tampered"
    tampered["body"] = "tampered body"
    store.put(tampered)

    def streamed_row(document: dict) -> dict:
        raw = json.dumps({"docs": [document], "bookmark": ""}, ensure_ascii=False).encode("utf-8")
        page = tmp_path / f"{document['_id']}-stream.json"
        page.write_bytes(raw)
        rows, _ = cs.parse_spooled_descriptor_page(
            page, max_page_bytes=len(raw) + 1024, max_items=8
        )
        return rows[0]

    tampered_row = streamed_row(tampered)
    with pytest.raises(sr.SourceRevisionResolutionError) as exc:
        sr._currentness_component(tampered_row, budget={"rows": 1, "compact_bytes": 0})
    assert str(exc.value) == "source revision body hash does not match its content hash"

    rows_by_type = {
        SourceDocType.TRANSCRIPT_SESSION: [
            streamed_row(next(store.iter_by_type(SourceDocType.TRANSCRIPT_SESSION)))
        ],
        SourceDocType.CONVERSATION_CHUNK: [tampered_row],
        SourceDocType.TOOL_EVIDENCE_BUNDLE: [
            streamed_row(next(store.iter_by_type(SourceDocType.TOOL_EVIDENCE_BUNDLE)))
        ],
    }
    monkeypatch.setattr(
        store,
        "iter_by_type",
        lambda doc_type, **kwargs: iter(rows_by_type[doc_type]),
    )
    assert sid not in sr.iter_legacy_source_hashes(store)
