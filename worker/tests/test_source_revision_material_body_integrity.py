"""Full-document source resolution must fail closed when a stored body no longer
matches its material hash (reproduced against an isolated CouchDB; see PR)."""

from __future__ import annotations

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.session_memory_materializer import materialize_session_memory
from agent_knowledge.couchdb_source.source_revision import (
    SourceRevisionResolutionError,
    resolve_active_source_revision,
    resolve_source_hash_for_currentness,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

PROVIDER = "codex"
PROJECT = "material-body-integrity"
TEXT = "synthetic conversation body for material hash integrity"


def _seed(mutate) -> tuple[str, InMemoryCouchDBSourceStore]:
    session_id_hash = dm.build_session_id_hash(PROVIDER, "material-body-integrity-session")
    store = InMemoryCouchDBSourceStore()
    store.put(
        dm.build_transcript_session_document(
            session=TranscriptSession(
                session_id_hash=session_id_hash,
                provider=PROVIDER,
                project=PROJECT,
                started_at="2026-11-01T00:00:00Z",
                ended_at="2026-11-01T00:01:00Z",
            )
        )
    )
    chunk = dm.build_conversation_chunk_document(
        chunk=TranscriptChunk.from_text(
            chunk_id="chunk-0",
            session_id_hash=session_id_hash,
            provider=PROVIDER,
            project=PROJECT,
            turn_start_index=0,
            turn_end_index=0,
            text=TEXT,
        )
    )
    # Write straight into the backing map: this models an out-of-band or legacy
    # document that bypassed the store's write-time guards.
    store._docs[chunk["_id"]] = mutate(dict(chunk))
    return session_id_hash, store


def _without(key: str):
    return lambda doc: {k: v for k, v in doc.items() if k != key}


def _seed_bundle(mutate) -> tuple[str, InMemoryCouchDBSourceStore]:
    session_id_hash, store = _seed(lambda doc: doc)
    bundle = dm.build_tool_evidence_bundle_document(
        session_id_hash=session_id_hash,
        provider=PROVIDER,
        project=PROJECT,
        part_index=1,
        part_count=1,
        evidence_index_start=0,
        evidence_index_end=0,
        record_content_hashes=[dm.sha256_hash("synthetic evidence record")],
        body="synthetic tool evidence bundle body",
    )
    store._docs[bundle["_id"]] = mutate(dict(bundle))
    return session_id_hash, store


@pytest.mark.parametrize(
    ("label", "mutate", "message"),
    [
        ("body_missing", _without("body"), "body does not match"),
        ("body_empty", lambda doc: {**doc, "body": ""}, "body does not match"),
        ("hash_mismatch", lambda doc: {**doc, "content_hash": dm.sha256_hash("other")}, "body does not match"),
        ("hash_field_missing", _without("content_hash"), "missing its material hash"),
    ],
)
def test_legacy_unpinned_resolution_fails_closed_on_body_hash_corruption(label, mutate, message):
    session_id_hash, store = _seed(mutate)

    with pytest.raises(SourceRevisionResolutionError, match=message):
        resolve_active_source_revision(session_id_hash=session_id_hash, store=store)
    with pytest.raises(SourceRevisionResolutionError, match=message):
        materialize_session_memory(session_id_hash=session_id_hash, store=store)


@pytest.mark.parametrize(
    ("label", "mutate", "message"),
    [
        ("bundle_body_missing", _without("body"), "body does not match"),
        ("bundle_body_empty", lambda doc: {**doc, "body": ""}, "body does not match"),
        ("bundle_hash_mismatch", lambda doc: {**doc, "content_hash": dm.sha256_hash("other")}, "body does not match"),
        ("bundle_hash_field_missing", _without("content_hash"), "missing its material hash"),
    ],
)
def test_legacy_unpinned_resolution_fails_closed_on_bundle_corruption(label, mutate, message):
    # A bundle keeps a valid coverage_hash in every case; it covers member
    # records, not the body, so it must not stand in for content_hash.
    session_id_hash, store = _seed_bundle(mutate)

    with pytest.raises(SourceRevisionResolutionError, match=message):
        resolve_active_source_revision(session_id_hash=session_id_hash, store=store)
    with pytest.raises(SourceRevisionResolutionError, match=message):
        materialize_session_memory(session_id_hash=session_id_hash, store=store)


def test_legacy_unpinned_resolution_accepts_intact_bundle():
    session_id_hash, store = _seed_bundle(lambda doc: doc)

    materialized = materialize_session_memory(session_id_hash=session_id_hash, store=store)

    assert TEXT in materialized.body
    assert materialized.source_hash == resolve_source_hash_for_currentness(
        session_id_hash=session_id_hash, store=store
    )


def test_legacy_unpinned_resolution_accepts_intact_body():
    session_id_hash, store = _seed(lambda doc: doc)

    materialized = materialize_session_memory(session_id_hash=session_id_hash, store=store)

    assert TEXT in materialized.body
    assert materialized.source_hash == resolve_source_hash_for_currentness(
        session_id_hash=session_id_hash, store=store
    )


def test_legacy_currentness_classifies_malformed_material_hash_as_resolution_error():
    session_id_hash, store = _seed(lambda doc: {**doc, "content_hash": "not-a-hash"})

    with pytest.raises(SourceRevisionResolutionError, match="currentness material is invalid"):
        resolve_source_hash_for_currentness(session_id_hash=session_id_hash, store=store)
