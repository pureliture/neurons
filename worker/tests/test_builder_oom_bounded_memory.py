"""Regression tests for builder bounded memory, fail-closed drift defense, and canonical contracts.

Contract Properties Verified:
1. Fail-Closed on Body Tamper with Unchanged Manifest:
   When chunk body changes but pointer/manifest remains unchanged, resolution strictly
   fails closed (SourceRevisionResolutionError) rather than silently projecting corrupted content.
2. Mixed Large Sessions & Many Sessions Peak Memory Bounded:
   Sequential materialization of 30 sessions (mixed normal + large 500KB+ bodies) runs
   with bounded memory overhead (verified via tracemalloc peak allocation).
3. Drift Protection & Lossless Reprocessing:
   Modified source revision (via chunk revision) is detected by canonical selector and
   re-queued for projection without loss.
4. Representation Drift Re-queueing:
   Representation switch (session_v1 -> segment_v1) strictly re-queues already-projected sessions.
5. Safe Failure Isolation & Data Retention:
   Failing projection records FAILED status in projection_state backend receipts without
   corrupting, deleting, or dropping source transcript documents.
"""

from __future__ import annotations

import hashlib
import tracemalloc

import pytest

from agent_knowledge.couchdb_source import build_cli, document_model as dm
from agent_knowledge.couchdb_source.build_cli import (
    _select_sessions_needing_projection,
)
from agent_knowledge.couchdb_source.session_memory_materializer import (
    materialize_session_memory,
    project_session_memory,
)
from agent_knowledge.couchdb_source.source_revision import (
    SourceRevisionResolutionError,
    activate_source_revision,
    resolve_active_source_revision,
    resolve_active_source_revision_from_snapshot,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from test_couchdb_build_cli import (
    _build_synthetic_session,
    _mark_projected,
)


def _h(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def test_body_change_with_same_manifest_fails_closed():
    """Verify fail-closed behavior when chunk body changes but manifest remains unupdated.

    If a naive short-circuit was present or drift checks were bypassed, tampered content
    would either be projected or silently ignored. Canonical resolution strictly raises
    SourceRevisionResolutionError ('active source revision source changed').
    """
    store = InMemoryCouchDBSourceStore()
    sid = _build_synthetic_session(store, provider="hermes", project="neurons", raw_id="tamper-test")
    activated = activate_source_revision(store=store, session_id_hash=sid)

    # 1. Baseline: resolution succeeds when body and manifest match
    resolved = resolve_active_source_revision(session_id_hash=sid, store=store)
    assert resolved.source_hash == activated.source_hash
    assert not resolved.is_legacy_unpinned

    # 2. Tamper: mutate the exact snapshot source document referenced by the active manifest member
    manifest = store.get(activated.manifest_id)
    assert manifest is not None and "members" in manifest
    # Find chunk member
    chunk_member = next(m for m in manifest["members"] if m.get("source_doc_type") == dm.SourceDocType.CONVERSATION_CHUNK)
    target_doc_id = chunk_member["source_document_id"]
    target_doc = dict(store.get(target_doc_id))
    target_doc["body"] += " tampered-in-place-without-manifest-update"
    target_doc["content_hash"] = dm.sha256_hash(target_doc["body"])
    # Bypass normal put to simulate underlying storage corruption
    store._docs[target_doc_id] = target_doc

    # 3. Fail-Closed assertion: resolve_active_source_revision must reject with SourceRevisionResolutionError
    with pytest.raises(SourceRevisionResolutionError, match="active source revision source changed"):
        resolve_active_source_revision(session_id_hash=sid, store=store)

    # Snapshot-based resolution in selection must also fail closed
    with pytest.raises(SourceRevisionResolutionError, match="active source revision source changed"):
        resolve_active_source_revision_from_snapshot(
            session_id_hash=sid,
            documents=store.find_by_session(session_id_hash=sid),
        )


def test_mixed_large_and_many_sessions_peak_memory_bounded():
    """Verify that processing mixed large and normal sessions maintains bounded peak memory."""
    store = InMemoryCouchDBSourceStore()
    sids: list[str] = []

    # Seed 25 normal sessions + 5 large sessions (total 30 sessions)
    for i in range(30):
        is_large = (i % 6 == 0)
        body = ("large turn content " * 25_000) if is_large else f"turn content for session {i}"
        sid = _build_synthetic_session(
            store,
            provider="hermes",
            project="neurons",
            raw_id=f"session-{i:03d}",
            body=body,
        )
        sids.append(sid)

    # Measure memory during sequential materialization
    tracemalloc.start()
    tracemalloc.reset_peak()

    materialized_count = 0
    total_body_bytes = 0
    for sid in sids:
        mat = materialize_session_memory(session_id_hash=sid, store=store)
        assert mat.fully_materialized is True
        materialized_count += 1
        total_body_bytes += len(mat.body)
        # Verify deterministic hash
        assert mat.content_hash == dm.sha256_hash(mat.body)
        del mat

    current_mem, peak_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert materialized_count == 30
    assert total_body_bytes > 2_000_000  # Total content processed exceeds 2MB
    # Peak heap allocation during loop must remain well bounded (e.g. under 15MB, far below 1Gi)
    assert peak_mem < 15 * 1024 * 1024, f"Peak memory {peak_mem / 1024 / 1024:.2f}MB exceeded 15MB budget"


def test_drift_protection_requeues_when_source_hash_changes():
    """Verify that source hash drift re-queues already-projected sessions without skipping."""
    store = InMemoryCouchDBSourceStore()
    sid = _build_synthetic_session(store, provider="hermes", project="neurons", raw_id="drift-test")
    _mark_projected(store, sid, "hermes", "neurons")

    # 1. Initially already projected: must not be selected
    selected = _select_sessions_needing_projection(
        store,
        limit=10,
        backend="postgres_pgvector",
        representation="session_v1",
    )
    assert sid not in {s.get("session_id_hash") for s in selected}

    # 2. Modify source chunk body and content_hash
    docs = store.find_by_session(session_id_hash=sid)
    chunk = next(d for d in docs if d.get("doc_type") == dm.SourceDocType.CONVERSATION_CHUNK)
    chunk["body"] += " updated conversation suffix"
    chunk["content_hash"] = dm.sha256_hash(chunk["body"])
    store.put(chunk)

    # 3. Canonical selector must re-queue session due to source_hash drift
    selected = _select_sessions_needing_projection(
        store,
        limit=10,
        backend="postgres_pgvector",
        representation="session_v1",
    )
    selected_ids = {s.get("session_id_hash") for s in selected}
    assert sid in selected_ids


def test_representation_drift_requeues_session():
    """Verify that representation mismatch (session_v1 vs segment_v1) re-queues the session."""
    store = InMemoryCouchDBSourceStore()
    sid = _build_synthetic_session(store, provider="hermes", project="neurons", raw_id="rep-drift")
    _mark_projected(store, sid, "hermes", "neurons")

    # Querying for segment_v1 when projected as session_v1 must re-queue
    selected = _select_sessions_needing_projection(
        store,
        limit=10,
        backend="postgres_pgvector",
        representation="segment_v1",
    )
    assert sid in {s.get("session_id_hash") for s in selected}


def test_failed_projection_retains_state_and_does_not_drop_work():
    """Verify that a failing projector records FAILED without dropping or deleting source."""
    store = InMemoryCouchDBSourceStore()
    sid = _build_synthetic_session(store, provider="hermes", project="neurons", raw_id="fail-test")

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

    # Transcript documents must remain intact
    docs = store.find_by_session(session_id_hash=sid)
    assert len(docs) >= 3  # session + chunk + coverage
