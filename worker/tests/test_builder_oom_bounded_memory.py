"""Regression tests for builder bounded memory, fail-closed drift defense, and canonical contracts.

Contract Properties Verified:
1. Fail-Closed on Body Tamper with Unchanged Manifest:
   When chunk body changes but pointer/manifest remains unchanged, resolution strictly
   fails closed (SourceRevisionResolutionError) rather than silently projecting corrupted content.
2. Mixed Large Sessions & Many Sessions Peak Memory Bounded:
   Sequential materialization of 30 sessions (mixed normal + large synthetic bodies) runs
   with bounded memory overhead (verified via tracemalloc peak allocation).
3. Transient Execution Memory Budget & Determinism:
   Measures heap peak memory of materialize_and_project across synthesized large inputs,
   asserting adherence to transient memory budget and exact determinism of output hashes.
   (Note: Cross-version benchmark conducted independently via isolated worktree subprocesses).
4. Production Args Full Cycle Simulation (--limit 10):
   Simulates actual prod CronJob parameters (limit=10) with mixed projected/unprojected
   sessions, verifying exact selection cap and bounded peak memory across the entire cycle.
5. Drift Protection & Lossless Reprocessing:
   Modified source revision (via chunk revision) is detected by canonical selector and
   re-queued for projection without loss.
6. Representation Drift Re-queueing:
   Representation switch (session_v1 -> segment_v1) strictly re-queues already-projected sessions.
7. Safe Failure Isolation & Data Retention:
   Failing projection records FAILED status in projection_state backend receipts without
   corrupting, deleting, or dropping source transcript documents.
"""

from __future__ import annotations

import hashlib
import tracemalloc

import pytest

from agent_knowledge.couchdb_source import build_cli, document_model as dm
from agent_knowledge.couchdb_source.document_model import TranscriptChunk
from agent_knowledge.couchdb_source.build_cli import (
    _select_sessions_needing_projection,
)
from agent_knowledge.couchdb_source.session_memory_materializer import (
    materialize_and_project,
    materialize_session_memory,
    project_session_memory,
    update_coverage_with_tool_evidence,
)
from agent_knowledge.couchdb_source.source_revision import (
    SourceRevisionResolutionError,
    activate_source_revision,
    resolve_active_source_revision,
    resolve_active_source_revision_from_snapshot,
)
from agent_knowledge.couchdb_source.source_store import (
    InMemoryCouchDBSourceStore,
    SourceStoreError,
)
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

    # Seed 25 normal sessions + 5 large sessions (total 30 sessions) with synthesized text
    for i in range(30):
        is_large = (i % 6 == 0)
        body = ("synthetic token stream " * 25_000) if is_large else f"synthetic turn content {i}"
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
    # Peak heap allocation during loop must remain bounded under 15MB
    assert peak_mem < 15 * 1024 * 1024, f"Peak memory {peak_mem / 1024 / 1024:.2f}MB exceeded 15MB budget"


def test_materialize_and_project_transient_memory_budget():
    """Verify transient memory overhead and output determinism for materialize_and_project.

    Contract & Scope:
    - This in-process test checks that materialize_and_project runs within a bounded
      transient heap memory budget (< 15MB) for a ~1.5MB synthetic session and yields
      deterministic, fail-closed outputs (content_hash, source_hash).
    - Note: tracemalloc is started after seeding; initial in-memory CouchDB document storage
      is intentionally not part of the transient execution delta.
    - Note on Cross-Version Comparison: Cross-version comparison between origin/main and HEAD
      requires isolated subprocess execution across worktrees. Independent subprocess benchmarking
      revealed that current in-memory optimizations (StringIO and del) produce only minimal peak
      reduction (~5MB in single sessions, ~0MB in multi-session cycles), confirming that they do
      NOT solve the 1Gi cgroup OOM issue. Real OOM resolution requires addressing selector full-scan
      amplification, CouchDB JSON transport limits, and cgroup ceiling.
    """
    store_1 = InMemoryCouchDBSourceStore()
    sid_1 = _build_synthetic_session(
        store_1,
        provider="hermes",
        project="neurons",
        raw_id="deterministic-memory-session",
        body="synthetic large token payload " * 15_000,
    )

    store_2 = InMemoryCouchDBSourceStore()
    sid_2 = _build_synthetic_session(
        store_2,
        provider="hermes",
        project="neurons",
        raw_id="deterministic-memory-session",
        body="synthetic large token payload " * 15_000,
    )

    class DummyProjector:
        def project(self, *, target_profile: str, document: dict) -> str:
            return "projected-ok"

    # Run 1: Measure execution peak
    tracemalloc.start()
    tracemalloc.reset_peak()
    res_1 = materialize_and_project(
        session_id_hash=sid_1,
        store=store_1,
        projector=DummyProjector(),
        backend="postgres_pgvector",
    )
    peak_1 = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    # Run 2: Measure execution peak
    tracemalloc.start()
    tracemalloc.reset_peak()
    res_2 = materialize_and_project(
        session_id_hash=sid_2,
        store=store_2,
        projector=DummyProjector(),
        backend="postgres_pgvector",
    )
    peak_2 = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    # 1. Deterministic output identity across independent runs
    assert res_1["content_hash"] == res_2["content_hash"]
    assert res_1["source_hash"] == res_2["source_hash"]
    assert res_1["conversation_chunk_count"] == res_2["conversation_chunk_count"] == 1
    assert res_1["fully_materialized"] is True

    # 2. Transient execution peak memory adheres to safety ceiling
    assert peak_1 < 15 * 1024 * 1024, f"Peak {peak_1 / 1024 / 1024:.2f}MB exceeded budget"
    assert peak_2 < 15 * 1024 * 1024, f"Peak {peak_2 / 1024 / 1024:.2f}MB exceeded budget"


def test_prod_args_limit_10_full_cycle_simulation():
    """Simulate actual prod CronJob execution parameters (limit=10) with full selector path."""
    store = InMemoryCouchDBSourceStore()

    # Seed 15 unprojected sessions + 15 projected sessions (total 30)
    for i in range(15):
        s_unproj = _build_synthetic_session(
            store,
            provider="hermes",
            project="neurons",
            raw_id=f"prod-unproj-{i}",
            body="synthetic turn " * 1000,
        )
        s_proj = _build_synthetic_session(
            store,
            provider="hermes",
            project="neurons",
            raw_id=f"prod-proj-{i}",
            body="synthetic turn " * 1000,
        )
        _mark_projected(store, s_proj, "hermes", "neurons")

    class RecordingProjector:
        def __init__(self) -> None:
            self.projected_sids: list[str] = []
        def project(self, *, target_profile: str, document: dict) -> str:
            self.projected_sids.append(document.get("session_id_hash", ""))
            return "ok"

    projector = RecordingProjector()

    tracemalloc.start()
    tracemalloc.reset_peak()

    # 1. Selector path with prod limit=10
    selected = _select_sessions_needing_projection(
        store,
        limit=10,
        backend="postgres_pgvector",
        representation="session_v1",
    )
    # Strict limit adherence
    assert len(selected) == 10

    # 2. Execution loop simulation
    for session in selected:
        sid = session["session_id_hash"]
        result = materialize_and_project(
            session_id_hash=sid,
            store=store,
            projector=projector,
            backend="postgres_pgvector",
        )
        assert result["fully_materialized"] is True
        result = None

    peak_mem = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    assert len(projector.projected_sids) == 10
    # Peak memory across the entire prod-like cycle remains comfortably bounded
    assert peak_mem < 20 * 1024 * 1024, f"Prod cycle peak memory {peak_mem / 1024 / 1024:.2f}MB exceeded 20MB"


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


def test_concurrent_revision_advance_during_coverage_put_re_reads_and_converges():
    """Deterministic RED test proving stale snapshot reuse vs fresh re-read.

    When store.put writes the coverage manifest, a concurrent chunk arrival or
    active revision change must be detected by the subsequent re-read (_coverage_snapshot).
    If a stale snapshot is reused (as in commit f945b66), the function immediately returns
    a stale manifest (1 chunk) instead of re-reading and converging on the latest revision (2 chunks).
    """
    store = InMemoryCouchDBSourceStore()
    sid = _build_synthetic_session(store, provider="hermes", project="neurons", raw_id="concurrent-rev-test")
    activated_v1 = activate_source_revision(store=store, session_id_hash=sid)

    # Hook store.put: on the first coverage manifest write, insert chunk 2 and advance active revision
    class HookedStore(InMemoryCouchDBSourceStore):
        def __init__(self, base: InMemoryCouchDBSourceStore) -> None:
            super().__init__()
            self._docs = base._docs
            self._changes_log = base._changes_log
            self.mutation_fired = False

        def put(self, doc: dict) -> dict:
            res = super().put(doc)
            if doc.get("doc_type") == dm.SourceDocType.COVERAGE_MANIFEST and not self.mutation_fired:
                self.mutation_fired = True
                # Add a second chunk to the session concurrently
                chunk = TranscriptChunk.from_text(
                    chunk_id="chunk_01",
                    session_id_hash=sid,
                    provider="hermes",
                    project="neurons",
                    turn_start_index=1,
                    turn_end_index=1,
                    text="turn 2 body",
                )
                chunk_doc = dm.build_conversation_chunk_document(chunk=chunk)
                super().put(chunk_doc)
                # Advance active revision to rev 2 (now with 2 chunks)
                activate_source_revision(store=self, session_id_hash=sid)
            return res

    hooked_store = HookedStore(store)

    # Resolve initial revision
    initial_resolved = resolve_active_source_revision(session_id_hash=sid, store=hooked_store)
    assert initial_resolved.source_hash == activated_v1.source_hash

    # Calling update_coverage_with_tool_evidence without stale snapshot
    # Fresh re-read contract must re-read, detect the mismatch, and converge to chunk_count == 2.
    cov = update_coverage_with_tool_evidence(
        session_id_hash=sid,
        store=hooked_store,
    )
    assert cov is not None
    # With proper re-read, it converges on the latest revision with 2 chunks.
    assert cov["conversation_chunk_count"] == 2, (
        f"Expected coverage to converge to 2 chunks after concurrent advance, got {cov.get('conversation_chunk_count')}"
    )


def test_continuous_concurrent_mutation_fails_closed():
    """Verify that continuous concurrent source changes fail closed with SourceStoreError.

    If snapshot reuse bypasses re-reading, it never detects continuous concurrency and falsely succeeds.
    """
    store = InMemoryCouchDBSourceStore()
    sid = _build_synthetic_session(store, provider="hermes", project="neurons", raw_id="fail-closed-test")
    activate_source_revision(store=store, session_id_hash=sid)

    class AlwaysAdvancingStore(InMemoryCouchDBSourceStore):
        def __init__(self, base: InMemoryCouchDBSourceStore) -> None:
            super().__init__()
            self._docs = base._docs
            self._changes_log = base._changes_log
            self.advance_count = 0

        def put(self, doc: dict) -> dict:
            res = super().put(doc)
            if doc.get("doc_type") == dm.SourceDocType.COVERAGE_MANIFEST:
                self.advance_count += 1
                chunk = TranscriptChunk.from_text(
                    chunk_id=f"chunk_{self.advance_count + 1:02d}",
                    session_id_hash=sid,
                    provider="hermes",
                    project="neurons",
                    turn_start_index=self.advance_count + 1,
                    turn_end_index=self.advance_count + 1,
                    text=f"continuous turn {self.advance_count}",
                )
                chunk_doc = dm.build_conversation_chunk_document(chunk=chunk)
                super().put(chunk_doc)
                activate_source_revision(store=self, session_id_hash=sid)
            return res

    hooked_store = AlwaysAdvancingStore(store)
    initial_resolved = resolve_active_source_revision(session_id_hash=sid, store=hooked_store)

    # Must fail closed with SourceStoreError after retry exhaustion
    with pytest.raises(SourceStoreError, match="coverage manifest did not converge after concurrent source updates"):
        update_coverage_with_tool_evidence(
            session_id_hash=sid,
            store=hooked_store,
        )

