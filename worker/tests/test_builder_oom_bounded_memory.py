"""Regression tests for builder OOM bounded paging and materialization.

Asserts:
1. Bounded Paging: In a store with many already-projected sessions, the selection
   fast-path skips heavy `find_by_session` store reads (0 calls for projected sessions).
2. Large Session Materialization & Hash Integrity: A synthetic large session with many
   conversation turns and large tool evidence bodies materializes deterministically with
   exact matching sha256 content_hash and source_hash.
3. Drift Protection & Lossless Reprocessing: Modified source_hash or representation mismatch
   is never short-circuited and is correctly re-queued for projection.
4. Safe Failure Isolation & ACK Retention: Failed projections retain their state in CouchDB
   and checkpoint without loss, arbitrary quarantine, or deletion.
"""

from __future__ import annotations

import hashlib

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.build_cli import (
    _select_sessions_needing_projection,
)
from agent_knowledge.couchdb_source.session_memory_materializer import (
    materialize_session_memory,
    project_session_memory,
    update_coverage_with_tool_evidence,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.session_memory.transcript_model import (
    TranscriptChunk,
    TranscriptSession,
)


def _h(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


class SpyCouchDBSourceStore(InMemoryCouchDBSourceStore):
    """Store wrapper tracking find_by_session call counts for bounded paging validation."""

    def __init__(self) -> None:
        super().__init__()
        self.find_by_session_calls: list[str] = []

    def find_by_session(self, *, session_id_hash: str) -> list[dict]:
        self.find_by_session_calls.append(session_id_hash)
        return super().find_by_session(session_id_hash=session_id_hash)


def _seed_session_with_state(
    store: InMemoryCouchDBSourceStore,
    *,
    session_id_hash: str,
    provider: str = "hermes",
    project: str = "neurons",
    source_hash: str = "",
    projected: bool = False,
    representation: str = "session_v1",
) -> str:
    effective_source_hash = source_hash or _h(f"source_hash_for_{session_id_hash}")
    session = TranscriptSession(
        session_id_hash=session_id_hash,
        provider=provider,
        project=project,
        started_at="2026-11-11T00:00:00Z",
        source_hash=effective_source_hash,
    )
    session_doc = dm.build_transcript_session_document(session=session)
    store.put(session_doc)

    chunk = TranscriptChunk.from_text(
        chunk_id="chunk_00",
        session_id_hash=session_id_hash,
        provider=provider,
        project=project,
        turn_start_index=0,
        turn_end_index=0,
        text="test conversation chunk body",
    )
    chunk_doc = dm.build_conversation_chunk_document(chunk=chunk)
    store.put(chunk_doc)

    cov = dm.build_coverage_manifest_document(
        session_id_hash=session_id_hash,
        provider=provider,
        project=project,
        conversation_chunk_count=1,
        tool_evidence_bundle_count=0,
        conversation_content_hashes=[chunk_doc["content_hash"]],
        tool_evidence_coverage_hashes=[],
        conversation_revision_tokens=[
            dm.build_source_revision_token(
                chunk_doc,
                material_hash_field="content_hash",
            )
        ],
        project_authority={"project": project, "ambiguous": False, "eligible_for_retirement": True},
    )
    store.put(cov)

    if projected:
        proj_state = dm.build_projection_state_document(
            session_id_hash=session_id_hash,
            provider=provider,
            project=project,
            target_profile="session-memory",
            projection_status=dm.ProjectionStatus.PROJECTED,
            active_content_hash=chunk_doc["content_hash"],
            session_memory_knowledge_id="chunk_id_test",
            projected_source_hash=effective_source_hash,
        )
        proj_state["backend_receipts"] = {
            "postgres_pgvector": {
                "receipt_version": 4 if representation == "segment_v1" else 2,
                "projection_status": "projected",
                "projected_source_hash": effective_source_hash,
                "session_id_hash": session_id_hash,
            }
        }
        store.put(proj_state)

    return effective_source_hash


def test_bounded_paging_skips_find_by_session_for_projected_sessions():
    """Verify that already-projected sessions skip heavy find_by_session calls."""
    store = SpyCouchDBSourceStore()

    # Seed 50 already-projected sessions
    for i in range(50):
        sid = _h(f"projected_{i}")
        _seed_session_with_state(
            store,
            session_id_hash=sid,
            source_hash=_h(f"hash_v1_{i}"),
            projected=True,
        )

    # Seed 2 unprojected sessions
    unproj_1 = _h("unprojected_1")
    unproj_2 = _h("unprojected_2")
    _seed_session_with_state(store, session_id_hash=unproj_1, source_hash=_h("hash_unproj_1"), projected=False)
    _seed_session_with_state(store, session_id_hash=unproj_2, source_hash=_h("hash_unproj_2"), projected=False)

    store.find_by_session_calls.clear()

    # Run selection
    selected = _select_sessions_needing_projection(
        store,
        limit=10,
        backend="postgres_pgvector",
        representation="session_v1",
    )

    # Selected must only contain the 2 unprojected sessions
    selected_ids = {s.get("session_id_hash") for s in selected}
    assert unproj_1 in selected_ids
    assert unproj_2 in selected_ids
    assert len(selected) == 2

    # find_by_session must NOT have been called for any of the 50 projected sessions!
    # Only called for unprojected sessions needing active revision resolution.
    assert len(store.find_by_session_calls) == 2
    assert set(store.find_by_session_calls) == {unproj_1, unproj_2}


def test_synthetic_large_session_materialization_and_hash_integrity():
    """Verify memory bounded materialization for a synthetic large session with exact hash match."""
    store = InMemoryCouchDBSourceStore()
    sid = _h("large_session_001")
    provider = "hermes"
    project = "neurons"

    session = TranscriptSession(
        session_id_hash=sid,
        provider=provider,
        project=project,
        started_at="2026-11-11T00:00:00Z",
    )
    store.put(dm.build_transcript_session_document(session=session))

    # Create 20 large conversation chunks (each ~50KB)
    chunk_hashes = []
    chunk_tokens = []
    for i in range(20):
        body_part = f"Turn {i}: " + ("x" * 50_000)
        chunk = TranscriptChunk.from_text(
            chunk_id=f"chunk_{i:02d}",
            session_id_hash=sid,
            provider=provider,
            project=project,
            turn_start_index=i,
            turn_end_index=i,
            text=body_part,
        )
        chunk_doc = dm.build_conversation_chunk_document(chunk=chunk)
        store.put(chunk_doc)
        chunk_hashes.append(chunk_doc["content_hash"])
        chunk_tokens.append(
            dm.build_source_revision_token(chunk_doc, material_hash_field="content_hash")
        )

    # Add 5 large tool evidence bundles (each ~100KB)
    bundle_hashes = []
    bundle_tokens = []
    for j in range(5):
        bundle_body = f"Tool Evidence {j}: " + ("y" * 100_000)
        rec_hash = _h(f"rec_{j}")
        bundle_doc = dm.build_tool_evidence_bundle_document(
            session_id_hash=sid,
            provider=provider,
            project=project,
            part_index=j,
            part_count=5,
            evidence_index_start=j * 10,
            evidence_index_end=(j + 1) * 10 - 1,
            record_content_hashes=[rec_hash],
            body=bundle_body,
        )
        store.put(bundle_doc)
        bundle_hashes.append(bundle_doc["coverage_hash"])
        bundle_tokens.append(
            dm.build_source_revision_token(bundle_doc, material_hash_field="coverage_hash")
        )

    cov = dm.build_coverage_manifest_document(
        session_id_hash=sid,
        provider=provider,
        project=project,
        conversation_chunk_count=20,
        tool_evidence_bundle_count=5,
        conversation_content_hashes=chunk_hashes,
        tool_evidence_coverage_hashes=bundle_hashes,
        conversation_revision_tokens=chunk_tokens,
        tool_evidence_revision_tokens=bundle_tokens,
        project_authority={"project": project, "ambiguous": False, "eligible_for_retirement": True},
    )
    store.put(cov)

    # Materialize
    mat = materialize_session_memory(session_id_hash=sid, store=store)

    assert mat.fully_materialized is True
    assert mat.conversation_chunk_count == 20
    assert mat.tool_evidence_bundle_count == 5
    assert len(mat.body) > 1_500_000  # > 1.5MB text body
    # Strict hash validation
    expected_content_hash = dm.sha256_hash(mat.body)
    assert mat.content_hash == expected_content_hash
    assert "materialization_loss" not in mat.notes


def test_drift_protection_requeues_when_source_hash_changes():
    """Verify that source hash drift re-queues already-projected sessions without skipping."""
    store = SpyCouchDBSourceStore()
    sid = _h("session_drift_001")
    v1_hash = _h("source_hash_v1")
    v2_hash = _h("source_hash_v2")

    # Seed projected with source_hash_v1
    _seed_session_with_state(
        store,
        session_id_hash=sid,
        source_hash=v1_hash,
        projected=True,
    )

    # Now update the session document to source_hash_v2 (source modified)
    session_doc = store.get(dm.session_doc_id(sid))
    session_doc["source_hash"] = v2_hash
    store.put(session_doc)

    store.find_by_session_calls.clear()

    selected = _select_sessions_needing_projection(
        store,
        limit=10,
        backend="postgres_pgvector",
        representation="session_v1",
    )

    # Must be selected due to drift
    assert len(selected) == 1
    assert selected[0]["session_id_hash"] == sid
    assert sid in store.find_by_session_calls


def test_representation_drift_requeues_session():
    """Verify that representation mismatch (session_v1 vs segment_v1) re-queues the session."""
    store = InMemoryCouchDBSourceStore()
    sid = _h("session_rep_drift_001")
    v1_hash = _h("source_hash_v1")

    # Seed projected as session_v1
    _seed_session_with_state(
        store,
        session_id_hash=sid,
        source_hash=v1_hash,
        projected=True,
        representation="session_v1",
    )

    # When querying for segment_v1, it must NOT skip and must be re-queued
    selected = _select_sessions_needing_projection(
        store,
        limit=10,
        backend="postgres_pgvector",
        representation="segment_v1",
    )
    assert len(selected) == 1
    assert selected[0]["session_id_hash"] == sid


def test_failed_projection_retains_state_and_does_not_drop_work():
    """Verify that a failing projector records FAILED without dropping or deleting source."""
    store = InMemoryCouchDBSourceStore()
    sid = _h("session_failure_retention_001")
    _seed_session_with_state(store, session_id_hash=sid, projected=False)

    class FailingProjector:
        def project(self, *, target_profile: str, document: dict) -> str:
            raise RuntimeError("synthetic_projection_failure")

    mat = materialize_session_memory(session_id_hash=sid, store=store)
    result = project_session_memory(
        materialized=mat,
        store=store,
        projector=FailingProjector(),
        backend="postgres_pgvector",
    )

    assert result["status"] == dm.ProjectionStatus.FAILED
    assert result["reason"] == "RuntimeError"

    # CouchDB projection_state must be recorded as FAILED, source documents must be intact
    saved_state = store.get(dm.projection_state_doc_id(sid))
    assert saved_state is not None
    receipt = (saved_state.get("backend_receipts") or {}).get("postgres_pgvector", {})
    assert receipt.get("projection_status") == dm.ProjectionStatus.FAILED
    assert receipt.get("failure_reason") == "RuntimeError"
    # Session document must remain intact
    session_doc = store.get(dm.session_doc_id(sid))
    assert session_doc is not None
