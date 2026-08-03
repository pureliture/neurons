from __future__ import annotations

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_store import (
    CouchDBSourceStore,
    InMemoryCouchDBSourceStore,
    SourceStoreConflict,
    SourceStoreError,
)
from agent_knowledge.couchdb_source.source_revision import activate_source_revision
from agent_knowledge.couchdb_source.session_memory_materializer import (
    upsert_transcript_session_aggregate,
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


def _pin_chunk(store: InMemoryCouchDBSourceStore) -> tuple[dict, dict, str]:
    session = _session_doc()
    chunk = _chunk_doc("pinned source")
    store.put(session)
    chunk_revision = store.put(chunk)
    activate_source_revision(store=store, session_id_hash=_sid())
    return session, chunk, chunk_revision.rev


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


def test_identity_upgrade_keeps_exact_legacy_duplicate_idempotent() -> None:
    store = InMemoryCouchDBSourceStore()
    document = _chunk_doc("legacy identity body")
    first = store.put(document)
    store._docs[document["_id"]]["payload_hash"] = document["content_hash"]

    duplicate = store.put(document)

    assert duplicate.outcome == "duplicate"
    assert duplicate.rev == first.rev


def test_put_preserves_later_temporal_metadata_for_identical_chunk_body() -> None:
    store = InMemoryCouchDBSourceStore()
    original = _chunk_doc("same body")
    first = store.put(original)
    enriched = dict(original)
    enriched["observed_at_start"] = "2026-07-09T10:00:00Z"
    enriched["observed_at_end"] = "2026-07-09T10:30:00Z"

    second = store.put(enriched)

    stored = store.get(original["_id"])
    assert stored is not None
    assert second.outcome == "conflict_resolved"
    assert second.rev != first.rev
    assert stored["observed_at_start"] == "2026-07-09T10:00:00Z"
    assert stored["observed_at_end"] == "2026-07-09T10:30:00Z"


def test_revision_member_rejects_changed_put_and_conditional_put_but_allows_exact_duplicate() -> None:
    store = InMemoryCouchDBSourceStore()
    _session, chunk, chunk_rev = _pin_chunk(store)
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


def test_revision_member_rejects_temporal_patch_but_allows_unreferenced_additive_write() -> None:
    store = InMemoryCouchDBSourceStore()
    _session, chunk, chunk_rev = _pin_chunk(store)

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


def test_conditional_temporal_patch_recovers_legacy_locator_when_times_already_match() -> None:
    store = InMemoryCouchDBSourceStore()
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


def test_conditional_temporal_patch_rejects_legacy_locator_cas_drift() -> None:
    store = InMemoryCouchDBSourceStore()
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


def test_conditional_temporal_patch_rejects_invalid_legacy_locator_replacement() -> None:
    store = InMemoryCouchDBSourceStore()
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


def test_session_aggregate_merge_preserves_projector_currentness_and_extends_bounds() -> None:
    store = InMemoryCouchDBSourceStore()
    existing = _session_doc()
    existing.update(
        {
            "started_at": "2026-07-09T10:00:00Z",
            "ended_at": "2026-07-09T11:00:00Z",
            "observed_at_start": "2026-07-09T10:00:00Z",
            "observed_at_end": "2026-07-09T11:00:00Z",
            "materialized_at": "2026-07-16T01:00:00Z",
            "source_hash": dm.sha256_hash("projector-current"),
            "source_status": "materialized",
        }
    )
    store.put(existing)

    incoming = _session_doc()
    incoming.update(
        {
            "started_at": "2026-07-09T09:00:00Z",
            "ended_at": "2026-07-09T13:00:00Z",
            "observed_at_start": "2026-07-09T09:00:00Z",
            "observed_at_end": "2026-07-09T13:00:00Z",
            "materialized_at": "2026-07-15T01:00:00Z",
            "source_hash": dm.sha256_hash("stale-incoming"),
            "source_status": "source_unproven",
        }
    )

    revision = upsert_transcript_session_aggregate(store=store, incoming=incoming)

    current = store.get(existing["_id"])
    assert current is not None
    assert revision.outcome == "conflict_resolved"
    assert current["started_at"] == "2026-07-09T09:00:00Z"
    assert current["ended_at"] == "2026-07-09T13:00:00Z"
    assert current["observed_at_start"] == "2026-07-09T09:00:00Z"
    assert current["observed_at_end"] == "2026-07-09T13:00:00Z"
    assert current["materialized_at"] == "2026-07-16T01:00:00Z"
    assert current["source_hash"] == dm.sha256_hash("projector-current")
    assert current["source_status"] == "materialized"


def test_tool_evidence_same_body_with_distinct_coverage_is_a_new_revision() -> None:
    store = InMemoryCouchDBSourceStore()
    original = dm.build_tool_evidence_bundle_document(
        session_id_hash=_sid(),
        provider="codex",
        project="neurons",
        part_index=1,
        part_count=1,
        evidence_index_start=0,
        evidence_index_end=0,
        record_content_hashes=[dm.sha256_hash("record-a")],
        body="same public evidence summary",
    )
    changed = dm.build_tool_evidence_bundle_document(
        session_id_hash=_sid(),
        provider="codex",
        project="neurons",
        part_index=1,
        part_count=1,
        evidence_index_start=0,
        evidence_index_end=0,
        record_content_hashes=[dm.sha256_hash("record-b")],
        body="same public evidence summary",
    )

    first = store.put(original)
    second = store.put(changed)
    duplicate = store.put(changed)

    assert second.outcome == "conflict_resolved"
    assert second.rev != first.rev
    assert duplicate.outcome == "duplicate"
    assert duplicate.rev == second.rev
    assert store.get(changed["_id"])["coverage_hash"] == changed["coverage_hash"]


def test_conversation_chunk_same_body_with_distinct_position_is_a_new_revision() -> None:
    store = InMemoryCouchDBSourceStore()
    original = _chunk_doc("same positioned body")
    moved = dict(original)
    moved.update(
        {
            "turn_start_index": 2,
            "turn_end_index": 3,
            "part_index": 2,
            "part_count": 3,
            "char_start": 20,
            "char_end": 40,
        }
    )

    first = store.put(original)
    second = store.put(moved)
    duplicate = store.put(moved)

    assert second.outcome == "conflict_resolved"
    assert second.rev != first.rev
    assert duplicate.outcome == "duplicate"
    assert duplicate.rev == second.rev
    assert store.get(moved["_id"])["char_start"] == 20
    assert dm.build_source_revision_token(
        original, material_hash_field="content_hash"
    ) != dm.build_source_revision_token(moved, material_hash_field="content_hash")


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
