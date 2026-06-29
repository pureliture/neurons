from __future__ import annotations

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.session_memory_materializer import (
    RecordingSessionMemoryProjector,
    materialize_and_project,
    materialize_session_memory,
    project_session_memory,
    update_coverage_with_tool_evidence,
)
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


def _sid() -> str:
    return dm.build_session_id_hash("codex", "sess-1")


def _record(index: int, *, summary: str = "12 passed") -> ToolEvidenceSummaryRecord:
    return ToolEvidenceSummaryRecord(
        session_id_hash=_sid(),
        provider="codex",
        project="neurons",
        category="test_result",
        outcome="pass",
        tool_name="bash",
        command_summary="uv run pytest -q",
        redacted_summary=summary,
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


def test_bundles_carry_index_range_and_coverage_hash() -> None:
    records = [_record(i) for i in range(3)]
    docs = build_tool_evidence_bundle_documents(records)
    assert len(docs) == 1
    bundle = docs[0]
    assert bundle["doc_type"] == dm.SourceDocType.TOOL_EVIDENCE_BUNDLE
    assert bundle["evidence_index_start"] == 0
    assert bundle["evidence_index_end"] == 2
    assert bundle["evidence_count"] == 3
    assert bundle["coverage_hash"] == dm.build_coverage_hash([r.content_hash for r in records])


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
