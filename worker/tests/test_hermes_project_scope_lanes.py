"""Hermes mixed project scope must be judged identically by both build lanes.

The graph projection lane (``llm_brain_core.runtime``) and the PG/Qdrant
session-memory lane (``couchdb_source.session_memory_materializer``) read the
same CouchDB source documents. A Hermes session whose documents disagree on
``project`` must not be admitted by one lane and refused by the other.
"""

from __future__ import annotations

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.build_cli import _select_sessions_needing_projection
from agent_knowledge.couchdb_source.hermes_project_scope import (
    HERMES_PROFILE_NAMES_ENV,
    INCONSISTENT_HERMES_PROJECT_SCOPE,
    configured_profile_names,
    decide_hermes_project_scope,
    parse_profile_names,
)
from agent_knowledge.couchdb_source.session_memory_materializer import (
    RecordingSessionMemoryProjector,
    materialize_and_project,
    materialize_session_memory,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core.artifact_store import InMemorySessionMemoryArtifactStore
from agent_knowledge.llm_brain_core.runtime import (
    InconsistentHermesProjectScope,
    materialize_artifact_from_couchdb_source,
)
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

PROVIDER = "hermes"
PROFILE_A = "profile-a"
PROFILE_B = "profile-b"
WORKSPACE = "synthetic-workspace"


@pytest.fixture(autouse=True)
def _profile_names(monkeypatch):
    monkeypatch.setenv(HERMES_PROFILE_NAMES_ENV, f"{PROFILE_A},{PROFILE_B}")


def _seed_mixed_session(
    store: InMemoryCouchDBSourceStore,
    *,
    raw_id: str,
    session_project: str,
    chunk_projects: tuple[str, ...],
) -> str:
    sid = dm.build_session_id_hash(PROVIDER, raw_id)
    store.put(
        dm.build_transcript_session_document(
            session=TranscriptSession(
                session_id_hash=sid,
                provider=PROVIDER,
                project=session_project,
                started_at="2026-06-21T00:00:00Z",
            )
        )
    )
    hashes = []
    docs = []
    for index, project in enumerate(chunk_projects):
        doc = dm.build_conversation_chunk_document(
            chunk=TranscriptChunk.from_text(
                chunk_id=f"chunk_{raw_id}_{index:02d}",
                session_id_hash=sid,
                provider=PROVIDER,
                project=project,
                turn_start_index=index,
                turn_end_index=index,
                text=f"synthetic turn {index} recorded under project label {index}",
            )
        )
        store.put(doc)
        docs.append(doc)
        hashes.append(doc["content_hash"])
    store.put(
        dm.build_coverage_manifest_document(
            session_id_hash=sid,
            provider=PROVIDER,
            project=session_project,
            conversation_chunk_count=len(chunk_projects),
            tool_evidence_bundle_count=0,
            conversation_content_hashes=hashes,
            tool_evidence_coverage_hashes=[],
            conversation_revision_tokens=[
                dm.build_source_revision_token(doc, material_hash_field="content_hash") for doc in docs
            ],
            observed_at_start="2026-06-21T00:00:00Z",
            observed_at_end="2026-06-21T00:00:00Z",
            project_authority={"project": session_project, "ambiguous": False, "eligible_for_retirement": True},
        )
    )
    return sid


# --- profile-vs-profile ------------------------------------------------------


def test_profile_vs_profile_graph_lane_rejects():
    store = InMemoryCouchDBSourceStore()
    sid = _seed_mixed_session(
        store, raw_id="pvp", session_project=PROFILE_A, chunk_projects=(PROFILE_A, PROFILE_B)
    )
    with pytest.raises(InconsistentHermesProjectScope):
        materialize_artifact_from_couchdb_source(
            session_id_hash=sid,
            source_store=store,
            artifact_store=InMemorySessionMemoryArtifactStore(),
        )


def test_profile_vs_profile_pg_lane_refuses_projection_like_graph_lane():
    store = InMemoryCouchDBSourceStore()
    sid = _seed_mixed_session(
        store, raw_id="pvp", session_project=PROFILE_A, chunk_projects=(PROFILE_A, PROFILE_B)
    )
    projector = RecordingSessionMemoryProjector()

    result = materialize_and_project(
        session_id_hash=sid, store=store, projector=projector, backend="postgres_pgvector"
    )

    assert projector.calls == []
    assert result["fully_materialized"] is False
    assert "inconsistent_hermes_project_scope" in result["notes"]
    assert result["projection"]["status"] == dm.ProjectionStatus.FAILED
    assert result["projection"]["reason"] == "inconsistent_hermes_project_scope"


def test_profile_vs_profile_pg_lane_marks_terminal_until_source_changes():
    store = InMemoryCouchDBSourceStore()
    sid = _seed_mixed_session(
        store, raw_id="pvp", session_project=PROFILE_A, chunk_projects=(PROFILE_A, PROFILE_B)
    )
    materialize_and_project(
        session_id_hash=sid,
        store=store,
        projector=RecordingSessionMemoryProjector(),
        backend="postgres_pgvector",
    )

    state = store.get(dm.projection_state_doc_id(sid))
    assert state is not None
    receipt = state["backend_receipts"]["postgres_pgvector"]
    assert receipt["projection_status"] == dm.ProjectionStatus.FAILED
    assert receipt["failure_reason"] == INCONSISTENT_HERMES_PROJECT_SCOPE
    attempt = state["projection_attempt"]["postgres_pgvector"]
    assert attempt["status"] == dm.ProjectionStatus.FAILED
    assert attempt["reason"] == INCONSISTENT_HERMES_PROJECT_SCOPE
    assert state["terminal_failure_reason"] == INCONSISTENT_HERMES_PROJECT_SCOPE
    assert _select_sessions_needing_projection(store, limit=0, backend="postgres_pgvector") == []

    # A new source revision under one profile re-queues the session.
    extra = dm.build_conversation_chunk_document(
        chunk=TranscriptChunk.from_text(
            chunk_id="chunk_pvp_extra",
            session_id_hash=sid,
            provider=PROVIDER,
            project=PROFILE_A,
            turn_start_index=5,
            turn_end_index=5,
            text="a later synthetic turn",
        )
    )
    store.put(extra)
    selected = _select_sessions_needing_projection(store, limit=0, backend="postgres_pgvector")
    assert [row["session_id_hash"] for row in selected] == [sid]


# --- workspace-vs-profile ----------------------------------------------------


def test_workspace_vs_profile_both_lanes_normalize_to_workspace():
    store = InMemoryCouchDBSourceStore()
    sid = _seed_mixed_session(
        store, raw_id="wvp", session_project=WORKSPACE, chunk_projects=(WORKSPACE, PROFILE_A)
    )

    artifact = materialize_artifact_from_couchdb_source(
        session_id_hash=sid,
        source_store=store,
        artifact_store=InMemorySessionMemoryArtifactStore(),
    )
    materialized = materialize_session_memory(session_id_hash=sid, store=store)

    assert artifact.project == WORKSPACE
    assert materialized.project == WORKSPACE
    assert materialized.fully_materialized is True
    assert "hermes_profile_normalized_to_workspace" in materialized.notes
    assert len(artifact.chunk_refs) == 2
    assert materialized.conversation_chunk_count == 2


# --- configuration ------------------------------------------------------------


def test_profile_names_come_from_environment_not_code(monkeypatch):
    monkeypatch.delenv(HERMES_PROFILE_NAMES_ENV, raising=False)
    assert configured_profile_names() == frozenset()
    assert parse_profile_names(" profile-a, ,profile-b ") == frozenset({"profile-a", "profile-b"})
    assert configured_profile_names({HERMES_PROFILE_NAMES_ENV: "x,y"}) == frozenset({"x", "y"})


def test_unconfigured_profile_names_fail_closed_in_both_lanes(monkeypatch):
    monkeypatch.delenv(HERMES_PROFILE_NAMES_ENV, raising=False)
    store = InMemoryCouchDBSourceStore()
    sid = _seed_mixed_session(
        store, raw_id="wvp", session_project=WORKSPACE, chunk_projects=(WORKSPACE, PROFILE_A)
    )
    with pytest.raises(InconsistentHermesProjectScope):
        materialize_artifact_from_couchdb_source(
            session_id_hash=sid,
            source_store=store,
            artifact_store=InMemorySessionMemoryArtifactStore(),
        )
    materialized = materialize_session_memory(session_id_hash=sid, store=store)
    assert materialized.fully_materialized is False
    assert INCONSISTENT_HERMES_PROJECT_SCOPE in materialized.notes


def test_profile_session_mixed_with_workspace_is_inconsistent():
    decision = decide_hermes_project_scope(
        [{"_id": "s", "project": PROFILE_A}, {"_id": "c", "project": WORKSPACE}],
        provider=PROVIDER,
        session_project=PROFILE_A,
        session_doc_ids={"s"},
        profile_names={PROFILE_A, PROFILE_B},
    )
    assert decision.inconsistent is True
    assert decision.reason == INCONSISTENT_HERMES_PROJECT_SCOPE
    assert decision.canonical_project == PROFILE_A


def test_workspace_vs_profile_decision_rewrites_only_non_session_documents():
    decision = decide_hermes_project_scope(
        [{"_id": "s", "project": WORKSPACE}, {"_id": "c", "project": PROFILE_A}],
        provider=PROVIDER,
        session_project=WORKSPACE,
        session_doc_ids={"s"},
        profile_names={PROFILE_A},
    )
    assert decision.inconsistent is False
    assert decision.canonical_project == WORKSPACE
    assert decision.normalized_projects == (PROFILE_A,)
    assert [doc["project"] for doc in decision.documents] == [WORKSPACE, WORKSPACE]


# --- non-hermes providers are untouched -------------------------------------


def test_non_hermes_consistent_session_has_no_scope_notes():
    store = InMemoryCouchDBSourceStore()
    sid = dm.build_session_id_hash("codex", "plain")
    store.put(
        dm.build_transcript_session_document(
            session=TranscriptSession(
                session_id_hash=sid, provider="codex", project="neurons", started_at="2026-06-21T00:00:00Z"
            )
        )
    )
    doc = dm.build_conversation_chunk_document(
        chunk=TranscriptChunk.from_text(
            chunk_id="chunk_plain",
            session_id_hash=sid,
            provider="codex",
            project="neurons",
            turn_start_index=0,
            turn_end_index=0,
            text="plain synthetic turn",
        )
    )
    store.put(doc)
    materialized = materialize_session_memory(session_id_hash=sid, store=store)
    assert materialized.project == "neurons"
    assert not any(note.startswith("hermes") or "scope" in note for note in materialized.notes)
