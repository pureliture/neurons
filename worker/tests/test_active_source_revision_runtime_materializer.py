"""Focused contract tests for active-source-revision consumer read paths."""

from __future__ import annotations

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.session_memory_materializer import (
    RecordingSessionMemoryProjector,
    materialize_and_project,
    materialize_session_memory,
    project_session_memory,
)
from agent_knowledge.couchdb_source.source_revision import (
    SourceRevisionResolutionError,
    activate_source_revision,
    resolve_active_source_revision,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core.artifact_store import (
    InMemorySessionMemoryArtifactStore,
)
from agent_knowledge.llm_brain_core.runtime import (
    extraction_text_from_couchdb_chunks,
    materialize_artifact_from_couchdb_source,
    session_episode_from_couchdb_source,
    session_source_revision_from_couchdb_source,
)
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession


PROJECT = "active-revision-consumer"
PROVIDER = "codex"


def _session_id_hash() -> str:
    return dm.build_session_id_hash(PROVIDER, "active-revision-consumer-session")


def _put_session(store: InMemoryCouchDBSourceStore) -> dict:
    document = dm.build_transcript_session_document(
        session=TranscriptSession(
            session_id_hash=_session_id_hash(),
            provider=PROVIDER,
            project=PROJECT,
            started_at="2026-08-04T00:00:00Z",
            ended_at="2026-08-04T00:02:00Z",
        )
    )
    store.put(document)
    return document


def _put_chunk(
    store: InMemoryCouchDBSourceStore,
    *,
    chunk_id: str,
    turn_index: int,
    text: str,
) -> dict:
    document = dm.build_conversation_chunk_document(
        chunk=TranscriptChunk.from_text(
            chunk_id=chunk_id,
            session_id_hash=_session_id_hash(),
            provider=PROVIDER,
            project=PROJECT,
            turn_start_index=turn_index,
            turn_end_index=turn_index,
            text=text,
            observed_at_start="2026-08-04T00:01:00Z",
            observed_at_end="2026-08-04T00:01:00Z",
        )
    )
    store.put(document)
    return document


def _put_bundle(
    store: InMemoryCouchDBSourceStore,
    *,
    part_index: int,
    body: str,
) -> dict:
    document = dm.build_tool_evidence_bundle_document(
        session_id_hash=_session_id_hash(),
        provider=PROVIDER,
        project=PROJECT,
        part_index=part_index,
        part_count=part_index,
        evidence_index_start=part_index - 1,
        evidence_index_end=part_index - 1,
        record_content_hashes=[dm.sha256_hash(f"evidence-{part_index}")],
        body=body,
        observed_at_start="2026-08-04T00:01:00Z",
        observed_at_end="2026-08-04T00:01:00Z",
    )
    store.put(document)
    return document


def _seed_source(
    store: InMemoryCouchDBSourceStore,
    *,
    activate: bool,
    include_legacy_only: bool,
) -> dict[str, dict]:
    documents = {
        "session": _put_session(store),
        "active_chunk": _put_chunk(
            store,
            chunk_id="active-member",
            turn_index=1,
            text="Active member supplies graph extraction facts.",
        ),
        "active_bundle": _put_bundle(
            store,
            part_index=1,
            body="Active member tool evidence.",
        ),
    }
    if activate:
        activate_source_revision(store=store, session_id_hash=_session_id_hash())
    if include_legacy_only:
        documents["legacy_chunk"] = _put_chunk(
            store,
            chunk_id="legacy-only-member",
            turn_index=2,
            text="Legacy-only graph extraction must not be used.",
        )
        documents["legacy_bundle"] = _put_bundle(
            store,
            part_index=2,
            body="Legacy-only tool evidence must not be materialized.",
        )
    return documents


def test_active_pointer_uses_only_manifest_members_for_all_consumer_outputs() -> None:
    store = InMemoryCouchDBSourceStore()
    documents = _seed_source(store, activate=True, include_legacy_only=True)
    resolved = resolve_active_source_revision(
        store=store,
        session_id_hash=_session_id_hash(),
    )
    artifact_store = InMemorySessionMemoryArtifactStore()

    artifact = materialize_artifact_from_couchdb_source(
        session_id_hash=_session_id_hash(),
        source_store=store,
        artifact_store=artifact_store,
    )
    episode = session_episode_from_couchdb_source(
        session_id_hash=_session_id_hash(),
        source_store=store,
        artifact_store=artifact_store,
    )
    extraction_text = extraction_text_from_couchdb_chunks(
        session_id_hash=_session_id_hash(),
        source_store=store,
    )
    result = materialize_and_project(
        session_id_hash=_session_id_hash(),
        store=store,
        projector=RecordingSessionMemoryProjector(),
    )
    materialized = materialize_session_memory(
        session_id_hash=_session_id_hash(),
        store=store,
    )
    coverage = store.get(dm.coverage_manifest_doc_id(_session_id_hash()))

    assert resolved.is_legacy_unpinned is False
    assert artifact.source_revision == resolved.source_hash
    assert session_source_revision_from_couchdb_source(
        session_id_hash=_session_id_hash(),
        source_store=store,
    ) == resolved.source_hash
    assert artifact.chunk_refs == tuple(
        document["_id"] for document in resolved.conversation_chunks
    )
    assert artifact.tool_evidence_refs == tuple(
        document["_id"] for document in resolved.tool_evidence_bundles
    )
    assert artifact.chunk_refs != (documents["active_chunk"]["_id"],)
    assert artifact.tool_evidence_refs != (documents["active_bundle"]["_id"],)
    assert "Active member supplies graph extraction facts." in extraction_text
    assert "Legacy-only graph extraction must not be used." not in extraction_text
    assert "Active member supplies graph extraction facts." in episode.extraction_text
    assert "Legacy-only graph extraction must not be used." not in episode.extraction_text
    assert result["projection"]["status"] == dm.ProjectionStatus.PROJECTED
    assert materialized.fully_materialized is True
    assert materialized.conversation_chunk_count == 1
    assert materialized.tool_evidence_bundle_count == 1
    assert materialized.source_hash == resolved.source_hash
    assert "Legacy-only graph extraction must not be used." not in materialized.body
    assert "Legacy-only tool evidence must not be materialized." not in materialized.body
    assert coverage is not None
    assert coverage["conversation_chunk_count"] == 1
    assert coverage["tool_evidence_bundle_count"] == 1
    assert coverage["source_hash"] == resolved.source_hash
    assert coverage["active_source_manifest_id"] == resolved.manifest_id


def test_pointer_absence_keeps_legacy_all_document_compatibility() -> None:
    store = InMemoryCouchDBSourceStore()
    documents = _seed_source(store, activate=False, include_legacy_only=True)

    resolved = resolve_active_source_revision(
        store=store,
        session_id_hash=_session_id_hash(),
    )
    artifact = materialize_artifact_from_couchdb_source(
        session_id_hash=_session_id_hash(),
        source_store=store,
    )
    result = materialize_and_project(
        session_id_hash=_session_id_hash(),
        store=store,
    )
    materialized = materialize_session_memory(
        session_id_hash=_session_id_hash(),
        store=store,
    )

    assert resolved.is_legacy_unpinned is True
    assert artifact.chunk_refs == (
        documents["active_chunk"]["_id"],
        documents["legacy_chunk"]["_id"],
    )
    assert artifact.tool_evidence_refs == (
        documents["active_bundle"]["_id"],
        documents["legacy_bundle"]["_id"],
    )
    assert result["fully_materialized"] is True
    assert materialized.conversation_chunk_count == 2
    assert materialized.tool_evidence_bundle_count == 2
    assert "Legacy-only graph extraction must not be used." in materialized.body
    assert "Legacy-only tool evidence must not be materialized." in materialized.body
    coverage = store.get(dm.coverage_manifest_doc_id(_session_id_hash()))
    assert coverage is not None
    assert "active_source_manifest_id" not in coverage


def test_active_pointer_rejects_coverage_bound_to_another_manifest() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_source(store, activate=True, include_legacy_only=False)
    materialize_and_project(session_id_hash=_session_id_hash(), store=store)
    coverage = store.get(dm.coverage_manifest_doc_id(_session_id_hash()))
    assert coverage is not None
    coverage["active_source_manifest_id"] = "source_revision_manifest:wrong"
    store.put(coverage)

    materialized = materialize_session_memory(
        session_id_hash=_session_id_hash(),
        store=store,
    )

    assert materialized.fully_materialized is False
    assert "coverage_active_source_manifest_mismatch" in materialized.notes


def test_projection_commit_rechecks_new_active_pointer_membership() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_source(store, activate=True, include_legacy_only=False)
    materialize_and_project(session_id_hash=_session_id_hash(), store=store)
    materialized = materialize_session_memory(
        session_id_hash=_session_id_hash(),
        store=store,
    )

    _put_chunk(
        store,
        chunk_id="new-active-member",
        turn_index=2,
        text="A newer active source revision arrived.",
    )
    _put_bundle(
        store,
        part_index=2,
        body="Newer active tool evidence.",
    )
    activate_source_revision(store=store, session_id_hash=_session_id_hash())
    projector = RecordingSessionMemoryProjector()

    result = project_session_memory(
        materialized=materialized,
        store=store,
        projector=projector,
    )

    assert result["status"] == dm.ProjectionStatus.FAILED
    assert result["reason"] == "source_revision_changed"
    assert len(projector.calls) == 1
    assert store.get(dm.projection_state_doc_id(_session_id_hash())) is None


@pytest.mark.parametrize(
    "corruption",
    ("malformed_pointer", "missing_manifest"),
)
def test_invalid_active_pointer_fails_closed_before_artifact_or_projection(
    corruption: str,
) -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_source(store, activate=True, include_legacy_only=False)
    pointer = next(
        document
        for document in store.all_docs()
        if document["doc_type"] == dm.SourceDocType.ACTIVE_SOURCE_REVISION
    )
    if corruption == "malformed_pointer":
        pointer.pop("active_revision")
        store.put(pointer)
    elif corruption == "missing_manifest":
        pointer["active_revision"] = dm.sha256_hash("missing-source-revision")
        store.put(pointer)

    artifact_store = InMemorySessionMemoryArtifactStore()
    projector = RecordingSessionMemoryProjector()

    with pytest.raises(SourceRevisionResolutionError):
        resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())
    with pytest.raises(SourceRevisionResolutionError):
        session_source_revision_from_couchdb_source(
            session_id_hash=_session_id_hash(),
            source_store=store,
        )
    with pytest.raises(SourceRevisionResolutionError):
        extraction_text_from_couchdb_chunks(
            session_id_hash=_session_id_hash(),
            source_store=store,
        )
    with pytest.raises(SourceRevisionResolutionError):
        materialize_artifact_from_couchdb_source(
            session_id_hash=_session_id_hash(),
            source_store=store,
            artifact_store=artifact_store,
        )
    with pytest.raises(SourceRevisionResolutionError):
        materialize_and_project(
            session_id_hash=_session_id_hash(),
            store=store,
            projector=projector,
        )

    assert artifact_store.get_latest_for_session(
        project=PROJECT,
        session_id_hash=_session_id_hash(),
    ) is None
    assert projector.calls == []
    assert store.get(dm.coverage_manifest_doc_id(_session_id_hash())) is None
    assert store.get(dm.projection_state_doc_id(_session_id_hash())) is None
