from __future__ import annotations

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.session_memory_materializer import (
    RecordingSessionMemoryProjector,
    mark_projection_pending_if_source_changed,
    materialize_and_project,
    materialize_session_memory,
    _observed_bounds,
    project_session_memory,
    update_coverage_with_tool_evidence,
)
from agent_knowledge.couchdb_source.build_cli import _select_sessions_needing_projection
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.couchdb_source.tool_evidence_bundler import (
    build_tool_evidence_bundle_documents,
    store_tool_evidence_bundles,
)
from agent_knowledge.session_memory.transcript_model import (
    TranscriptChunk,
    TranscriptSession,
    ToolEvidenceSummaryRecord,
)
from agent_knowledge.llm_brain_core.couchdb_projection_cli import (
    _select_sessions as _select_graph_sessions,
    _session_natural_id,
)
from agent_knowledge.llm_brain_core.runtime import (
    session_source_revision_from_couchdb_source,
)


def _sid() -> str:
    return dm.build_session_id_hash("codex", "sess-1")


def _record(
    index: int,
    *,
    summary: str = "12 passed",
    observed_at: str = "",
) -> ToolEvidenceSummaryRecord:
    return ToolEvidenceSummaryRecord(
        session_id_hash=_sid(),
        provider="codex",
        project="neurons",
        category="test_result",
        outcome="pass",
        tool_name="bash",
        command_summary="uv run pytest -q",
        redacted_summary=summary,
        observed_at=observed_at,
        evidence_index=index,
    )


def _seed_session(store, *, chunk_texts=("user asked", "assistant answered")):
    session = TranscriptSession(
        session_id_hash=_sid(), provider="codex", project="neurons", started_at="2026-06-17T01:00:00Z"
    )
    store.put(dm.build_transcript_session_document(session=session))
    conv_hashes = []
    for i, text in enumerate(chunk_texts):
        chunk = TranscriptChunk.from_text(
            chunk_id=f"chunk_{i:02d}",
            session_id_hash=_sid(),
            provider="codex",
            project="neurons",
            turn_start_index=i,
            turn_end_index=i,
            text=text,
        )
        doc = dm.build_conversation_chunk_document(chunk=chunk)
        store.put(doc)
        conv_hashes.append(doc["content_hash"])
    cov = dm.build_coverage_manifest_document(
        session_id_hash=_sid(),
        provider="codex",
        project="neurons",
        conversation_chunk_count=len(chunk_texts),
        tool_evidence_bundle_count=0,
        conversation_content_hashes=conv_hashes,
        tool_evidence_coverage_hashes=[],
        project_authority={"project": "neurons", "ambiguous": False, "eligible_for_retirement": True},
    )
    store.put(cov)


# --- tool evidence bundling ---------------------------------------------------


def test_bundles_split_distinct_event_times_to_preserve_term_time_binding() -> None:
    records = [
        _record(0, observed_at="2026-07-15T10:00:00Z"),
        _record(1, observed_at="2026-07-15T10:05:00Z"),
        _record(2, observed_at="2026-07-15T10:10:00Z"),
    ]
    docs = build_tool_evidence_bundle_documents(records)
    assert len(docs) == 3
    assert [bundle["evidence_index_start"] for bundle in docs] == [0, 1, 2]
    assert [bundle["evidence_index_end"] for bundle in docs] == [0, 1, 2]
    assert all(
        bundle["doc_type"] == dm.SourceDocType.TOOL_EVIDENCE_BUNDLE
        for bundle in docs
    )
    assert all(bundle["evidence_count"] == 1 for bundle in docs)
    assert [bundle["coverage_hash"] for bundle in docs] == [
        dm.build_coverage_hash([record.content_hash]) for record in records
    ]
    assert [bundle["observed_at_start"] for bundle in docs] == [
        record.observed_at for record in records
    ]
    assert [bundle["observed_at_end"] for bundle in docs] == [
        record.observed_at for record in records
    ]


def test_bundles_do_not_let_missing_or_malformed_time_borrow_a_valid_bound() -> None:
    docs = build_tool_evidence_bundle_documents(
        [
            _record(0, summary="bounded alpha", observed_at="2026-07-15T10:00:00Z"),
            _record(1, summary="untimed beta", observed_at=""),
            _record(2, summary="malformed gamma", observed_at="not-a-time"),
        ]
    )

    assert len(docs) == 2
    assert docs[0]["observed_at_start"] == "2026-07-15T10:00:00Z"
    assert docs[0]["observed_at_end"] == "2026-07-15T10:00:00Z"
    assert docs[1]["evidence_count"] == 2
    assert docs[1]["observed_at_start"] == ""
    assert docs[1]["observed_at_end"] == ""


def test_large_evidence_splits_into_multiple_bounded_bundles() -> None:
    records = [_record(i, summary="x" * 900) for i in range(20)]
    docs = build_tool_evidence_bundle_documents(records)
    assert len(docs) > 1  # bounded -> multiple parts
    assert [d["part_index"] for d in docs] == list(range(1, len(docs) + 1))
    assert all(d["part_count"] == len(docs) for d in docs)
    # every record covered exactly once across parts
    total = sum(d["evidence_count"] for d in docs)
    assert total == 20


def test_bundling_rejects_mixed_sessions() -> None:
    other = ToolEvidenceSummaryRecord(
        session_id_hash=dm.build_session_id_hash("codex", "other"),
        provider="codex",
        project="neurons",
        category="test_result",
        outcome="pass",
        tool_name="bash",
        command_summary="x",
        redacted_summary="y",
        evidence_index=0,
    )
    with pytest.raises(ValueError):
        build_tool_evidence_bundle_documents([_record(0), other])


# --- coverage update + materialization ----------------------------------------


def test_update_coverage_records_tool_evidence_counts() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_session(store)
    store_tool_evidence_bundles([_record(i) for i in range(3)], store=store)
    cov = update_coverage_with_tool_evidence(session_id_hash=_sid(), store=store)
    assert cov["tool_evidence_bundle_count"] == 1
    # project_authority preserved from the import-time manifest
    assert cov["project_authority"]["project"] == "neurons"


def test_distinct_tool_coverage_revision_invalidates_session_and_graph_currentness() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_session(store)
    original = dm.build_tool_evidence_bundle_document(
        session_id_hash=_sid(),
        provider="codex",
        project="neurons",
        part_index=1,
        part_count=1,
        evidence_index_start=0,
        evidence_index_end=0,
        record_content_hashes=[dm.sha256_hash("record-a")],
        body="stable public evidence body",
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
        body="stable public evidence body",
    )
    store.put(original)
    first_coverage = update_coverage_with_tool_evidence(
        session_id_hash=_sid(), store=store
    )
    store.put(
        dm.build_projection_state_document(
            session_id_hash=_sid(),
            provider="codex",
            project="neurons",
            projection_status=dm.ProjectionStatus.PROJECTED,
            active_content_hash=dm.sha256_hash("first-artifact"),
            source_hash=first_coverage["source_hash"],
            projected_source_hash=first_coverage["source_hash"],
        )
    )

    revision = store.put(changed)
    current_coverage = update_coverage_with_tool_evidence(
        session_id_hash=_sid(), store=store
    )
    mark_projection_pending_if_source_changed(
        session_id_hash=_sid(),
        provider="codex",
        project="neurons",
        source_hash=current_coverage["source_hash"],
        store=store,
        source_changed=revision.outcome != "duplicate",
    )

    assert revision.outcome == "conflict_resolved"
    assert current_coverage["source_hash"] != first_coverage["source_hash"]
    assert session_source_revision_from_couchdb_source(
        session_id_hash=_sid(), source_store=store
    ) == current_coverage["source_hash"]
    assert [
        row["session_id_hash"]
        for row in _select_sessions_needing_projection(store, limit=0)
    ] == [_sid()]

    class _ProjectedFirstRevision:
        def list_projected_source_hash_sets(self, *args, **kwargs):
            return {_session_natural_id(_sid()): {first_coverage["source_hash"]}}

    graph_selected = _select_graph_sessions(
        store,
        project="neurons",
        provider="codex",
        limit=0,
        projection_state_store=_ProjectedFirstRevision(),
        extraction_level="episodic",
    )
    assert [row["session_id_hash"] for row in graph_selected] == [_sid()]


def test_tool_evidence_body_revision_invalidates_both_projection_lanes_but_duplicate_does_not() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_session(store)
    record_hash = dm.sha256_hash("stable-record")
    original = dm.build_tool_evidence_bundle_document(
        session_id_hash=_sid(),
        provider="codex",
        project="neurons",
        part_index=1,
        part_count=1,
        evidence_index_start=0,
        evidence_index_end=0,
        record_content_hashes=[record_hash],
        body="first public evidence body",
        observed_at_start="2026-07-15T10:00:00Z",
        observed_at_end="2026-07-15T10:00:00Z",
    )
    changed = dm.build_tool_evidence_bundle_document(
        session_id_hash=_sid(),
        provider="codex",
        project="neurons",
        part_index=1,
        part_count=1,
        evidence_index_start=0,
        evidence_index_end=0,
        record_content_hashes=[record_hash],
        body="changed public evidence body",
        observed_at_start="2026-07-15T10:00:00Z",
        observed_at_end="2026-07-15T10:00:00Z",
    )
    store.put(original)
    first_coverage = update_coverage_with_tool_evidence(
        session_id_hash=_sid(), store=store
    )
    store.put(
        dm.build_projection_state_document(
            session_id_hash=_sid(),
            provider="codex",
            project="neurons",
            projection_status=dm.ProjectionStatus.PROJECTED,
            active_content_hash=dm.sha256_hash("first-artifact"),
            source_hash=first_coverage["source_hash"],
            projected_source_hash=first_coverage["source_hash"],
        )
    )

    duplicate = store.put(original)
    duplicate_coverage = update_coverage_with_tool_evidence(
        session_id_hash=_sid(), store=store
    )
    assert duplicate.outcome == "duplicate"
    assert duplicate_coverage["source_hash"] == first_coverage["source_hash"]
    assert _select_sessions_needing_projection(store, limit=0) == []

    class _ProjectedFirstRevision:
        def list_projected_source_hash_sets(self, *args, **kwargs):
            return {_session_natural_id(_sid()): {first_coverage["source_hash"]}}

    assert _select_graph_sessions(
        store,
        project="neurons",
        provider="codex",
        limit=0,
        projection_state_store=_ProjectedFirstRevision(),
        extraction_level="episodic",
    ) == []

    revision = store.put(changed)
    current_coverage = update_coverage_with_tool_evidence(
        session_id_hash=_sid(), store=store
    )
    mark_projection_pending_if_source_changed(
        session_id_hash=_sid(),
        provider="codex",
        project="neurons",
        source_hash=current_coverage["source_hash"],
        store=store,
        source_changed=revision.outcome != "duplicate",
    )

    assert revision.outcome == "conflict_resolved"
    assert current_coverage["source_hash"] != first_coverage["source_hash"]
    assert [
        row["session_id_hash"]
        for row in _select_sessions_needing_projection(store, limit=0)
    ] == [_sid()]
    assert [
        row["session_id_hash"]
        for row in _select_graph_sessions(
            store,
            project="neurons",
            provider="codex",
            limit=0,
            projection_state_store=_ProjectedFirstRevision(),
            extraction_level="episodic",
        )
    ] == [_sid()]


def test_update_coverage_converges_after_concurrent_distinct_chunk_write() -> None:
    class _ConcurrentChunkStore(InMemoryCouchDBSourceStore):
        armed = False
        injected = False

        def put(self, document):
            if (
                self.armed
                and not self.injected
                and document.get("doc_type") == dm.SourceDocType.COVERAGE_MANIFEST
            ):
                self.injected = True
                concurrent = TranscriptChunk.from_text(
                    chunk_id="chunk_concurrent",
                    session_id_hash=_sid(),
                    provider="codex",
                    project="neurons",
                    turn_start_index=99,
                    turn_end_index=99,
                    text="concurrent distinct source",
                )
                super().put(dm.build_conversation_chunk_document(chunk=concurrent))
            return super().put(document)

    store = _ConcurrentChunkStore()
    _seed_session(store)
    store.armed = True

    coverage = update_coverage_with_tool_evidence(session_id_hash=_sid(), store=store)

    persisted = store.get(dm.coverage_manifest_doc_id(_sid()))
    session = store.get(dm.session_doc_id(_sid()))
    assert store.injected is True
    assert coverage["conversation_chunk_count"] == 3
    assert persisted["conversation_chunk_count"] == 3
    assert persisted["source_hash"] == coverage["source_hash"]
    assert session["source_hash"] == coverage["source_hash"]


def test_coverage_session_merge_preserves_concurrent_projector_fields() -> None:
    concurrent_source_hash = dm.sha256_hash("concurrent-projector")

    class _ConcurrentProjectorStore(InMemoryCouchDBSourceStore):
        armed = False

        def _inject_projection(self) -> None:
            self.armed = False
            current = dict(super().get(dm.session_doc_id(_sid())))
            current.update(
                {
                    "source_hash": concurrent_source_hash,
                    "materialized_at": "2026-07-16T03:00:00Z",
                    "source_status": "materialized",
                }
            )
            super().put(current)

        def put(self, document):
            if self.armed and document.get("doc_type") == dm.SourceDocType.TRANSCRIPT_SESSION:
                self._inject_projection()
            return super().put(document)

        def merge_transcript_session_aggregate(self, **kwargs):
            if self.armed:
                self._inject_projection()
            return super().merge_transcript_session_aggregate(**kwargs)

    store = _ConcurrentProjectorStore()
    _seed_session(store)
    store.armed = True

    coverage = update_coverage_with_tool_evidence(session_id_hash=_sid(), store=store)

    current = store.get(dm.session_doc_id(_sid()))
    assert current["source_hash"] == coverage["source_hash"]
    assert current["materialized_at"] == "2026-07-16T03:00:00Z"
    assert current["source_status"] == "materialized"


def test_observed_bounds_compare_mixed_offsets_chronologically() -> None:
    start, end = _observed_bounds(
        sessions=[],
        chunks=[
            {
                "observed_at_start": "2026-07-09T23:30:00-04:00",
                "observed_at_end": "2026-07-10T00:30:00-04:00",
            },
            {
                "observed_at_start": "2026-07-10T02:00:00Z",
                "observed_at_end": "2026-07-10T03:00:00Z",
            },
        ],
    )

    assert start == "2026-07-10T02:00:00Z"
    assert end == "2026-07-10T00:30:00-04:00"


def test_materialized_session_memory_embeds_tool_evidence() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_session(store)
    store_tool_evidence_bundles([_record(0, summary="12 passed unique-marker")], store=store)
    update_coverage_with_tool_evidence(session_id_hash=_sid(), store=store)
    mat = materialize_session_memory(session_id_hash=_sid(), store=store)
    assert mat.target_profile == dm.RETIRED_INDEX_BRIDGE_RECALL_PROFILE
    assert mat.fully_materialized is True
    # tool evidence summary is embedded for RetiredIndexBridge-only recall
    assert "unique-marker" in mat.body
    assert "## tool_evidence_summary" in mat.body
    assert "## conversation" in mat.body


def test_projection_goes_to_session_memory_only() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_session(store)
    store_tool_evidence_bundles([_record(0)], store=store)
    projector = RecordingSessionMemoryProjector()
    report = materialize_and_project(session_id_hash=_sid(), store=store, projector=projector)
    assert report["projection"]["status"] == dm.ProjectionStatus.PROJECTED
    assert len(projector.calls) == 1
    assert projector.calls[0]["target_profile"] == dm.RETIRED_INDEX_BRIDGE_RECALL_PROFILE
    # projection_state recorded in the store
    state = store.get(dm.projection_state_doc_id(_sid()))
    assert state["projection_status"] == dm.ProjectionStatus.PROJECTED
    assert state["target_profile"] == dm.RETIRED_INDEX_BRIDGE_RECALL_PROFILE
    session = store.get(dm.session_doc_id(_sid()))
    assert session["source_hash"] == state["projected_source_hash"]
    assert session["materialized_at"] == state["materialized_at"]
    assert session["materialized_at"]


def _advance_source_after_materialization(store: InMemoryCouchDBSourceStore) -> dict:
    later = TranscriptChunk.from_text(
        chunk_id="chunk_later",
        session_id_hash=_sid(),
        provider="codex",
        project="neurons",
        turn_start_index=99,
        turn_end_index=99,
        text="a distinct source revision after materialization",
    )
    store.put(dm.build_conversation_chunk_document(chunk=later))
    coverage = update_coverage_with_tool_evidence(session_id_hash=_sid(), store=store)
    mark_projection_pending_if_source_changed(
        session_id_hash=_sid(),
        provider="codex",
        project="neurons",
        source_hash=coverage["source_hash"],
        store=store,
        source_changed=True,
    )
    return coverage


def test_old_successful_materialization_does_not_overwrite_newer_pending_source() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_session(store)
    update_coverage_with_tool_evidence(session_id_hash=_sid(), store=store)
    stale_materialization = materialize_session_memory(session_id_hash=_sid(), store=store)
    current_coverage = _advance_source_after_materialization(store)

    result = project_session_memory(
        materialized=stale_materialization,
        store=store,
        projector=RecordingSessionMemoryProjector(),
    )

    state = store.get(dm.projection_state_doc_id(_sid()))
    session = store.get(dm.session_doc_id(_sid()))
    assert result["status"] == dm.ProjectionStatus.FAILED
    assert result["reason"] == "source_revision_changed"
    assert state["projection_status"] == dm.ProjectionStatus.PENDING
    assert state["source_hash"] == current_coverage["source_hash"]
    assert session["source_hash"] == current_coverage["source_hash"]


def test_stale_pending_request_does_not_overwrite_current_source_revision() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_session(store)
    first_coverage = update_coverage_with_tool_evidence(
        session_id_hash=_sid(), store=store
    )
    current_coverage = _advance_source_after_materialization(store)

    mark_projection_pending_if_source_changed(
        session_id_hash=_sid(),
        provider="codex",
        project="neurons",
        source_hash=first_coverage["source_hash"],
        store=store,
        source_changed=True,
    )

    state = store.get(dm.projection_state_doc_id(_sid()))
    assert state["projection_status"] == dm.ProjectionStatus.PENDING
    assert state["source_hash"] == current_coverage["source_hash"]


def test_old_failed_materialization_does_not_overwrite_newer_pending_source() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_session(store)
    update_coverage_with_tool_evidence(session_id_hash=_sid(), store=store)
    stale_materialization = materialize_session_memory(session_id_hash=_sid(), store=store)
    current_coverage = _advance_source_after_materialization(store)

    class _BoomProjector:
        def project(self, *, target_profile, document):
            raise RuntimeError("backend down")

    result = project_session_memory(
        materialized=stale_materialization,
        store=store,
        projector=_BoomProjector(),
    )

    state = store.get(dm.projection_state_doc_id(_sid()))
    assert result["status"] == dm.ProjectionStatus.FAILED
    assert result["reason"] == "RuntimeError"
    assert result["state_write_skipped"] == "source_revision_changed"
    assert state["projection_status"] == dm.ProjectionStatus.PENDING
    assert state["source_hash"] == current_coverage["source_hash"]


def test_materialization_loss_blocks_projection() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_session(store, chunk_texts=("only one",))
    # coverage manifest claims 2 chunks but only 1 stored -> loss
    cov = dm.build_coverage_manifest_document(
        session_id_hash=_sid(),
        provider="codex",
        project="neurons",
        conversation_chunk_count=2,
        tool_evidence_bundle_count=0,
        conversation_content_hashes=["sha256:" + "a" * 64, "sha256:" + "b" * 64],
        tool_evidence_coverage_hashes=[],
    )
    store.put(cov)
    mat = materialize_session_memory(session_id_hash=_sid(), store=store)
    assert mat.fully_materialized is False
    assert "materialization_loss" in mat.notes
    projector = RecordingSessionMemoryProjector()
    result = project_session_memory(materialized=mat, store=store, projector=projector)
    assert result["status"] == dm.ProjectionStatus.FAILED
    assert projector.calls == []  # never projected
    state = store.get(dm.projection_state_doc_id(_sid()))
    assert state["projection_status"] == dm.ProjectionStatus.FAILED


def test_projector_failure_keeps_source_and_marks_failed() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_session(store)
    store_tool_evidence_bundles([_record(0)], store=store)
    update_coverage_with_tool_evidence(session_id_hash=_sid(), store=store)
    mat = materialize_session_memory(session_id_hash=_sid(), store=store)

    class _BoomProjector:
        def project(self, *, target_profile, document):
            raise RuntimeError("backend down")

    result = project_session_memory(materialized=mat, store=store, projector=_BoomProjector())
    assert result["status"] == dm.ProjectionStatus.FAILED
    assert result["reason"] == "RuntimeError"
    # source docs still intact
    assert store.find_by_session(session_id_hash=_sid(), doc_type=dm.SourceDocType.CONVERSATION_CHUNK)
