from __future__ import annotations

from pathlib import Path

from agent_knowledge.couchdb_source import build_cli
from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core.runtime import session_source_revision_from_couchdb_source
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

PROJECT = "neurons"
PROVIDER = "claude"


def _seed(store: InMemoryCouchDBSourceStore, raw_id: str, *, status: str = "") -> str:
    sid = dm.build_session_id_hash(PROVIDER, raw_id)
    session = TranscriptSession(
        session_id_hash=sid,
        provider=PROVIDER,
        project=PROJECT,
        started_at="2026-06-17T00:00:00Z",
    )
    store.put(dm.build_transcript_session_document(session=session))
    chunk = TranscriptChunk.from_text(
        chunk_id="chunk_00",
        session_id_hash=sid,
        provider=PROVIDER,
        project=PROJECT,
        turn_start_index=0,
        turn_end_index=0,
        text=f"body for {raw_id}",
    )
    chunk_doc = dm.build_conversation_chunk_document(chunk=chunk)
    store.put(chunk_doc)
    store.put(
        dm.build_coverage_manifest_document(
            session_id_hash=sid,
            provider=PROVIDER,
            project=PROJECT,
            conversation_chunk_count=1,
            tool_evidence_bundle_count=0,
            conversation_content_hashes=[chunk_doc["content_hash"]],
            tool_evidence_coverage_hashes=[],
            conversation_revision_tokens=[
                dm.build_source_revision_token(chunk_doc, material_hash_field="content_hash")
            ],
            observed_at_start=session.started_at,
            observed_at_end=session.started_at,
            project_authority={
                "project": PROJECT,
                "ambiguous": False,
                "eligible_for_retirement": True,
            },
        )
    )
    if status:
        source_hash = session_source_revision_from_couchdb_source(
            session_id_hash=sid, source_store=store
        )
        state = dm.build_projection_state_document(
            session_id_hash=sid,
            provider=PROVIDER,
            project=PROJECT,
            projection_status=status,
            projected_source_hash=(
                "sha256:" + "b" * 64
                if status == dm.ProjectionStatus.PROJECTED
                else source_hash
            ),
            active_content_hash="sha256:" + "a" * 64,
            source_hash=source_hash,
            failure_reason="boom" if status == dm.ProjectionStatus.FAILED else "",
        )
        if status == dm.ProjectionStatus.PROJECTED:
            state["backend_receipts"] = {
                "postgres_pgvector": {
                    "projection_status": status,
                    "projected_source_hash": "sha256:" + "b" * 64,
                }
            }
        store.put(state)
    return sid


def _source_hash(store, sid: str) -> str:
    return session_source_revision_from_couchdb_source(session_id_hash=sid, source_store=store)


def _checkpoint(tmp_path: Path, *, pending: list[str], attempts=None) -> Path:
    path = tmp_path / "selection-checkpoint.json"
    build_cli._write_selection_checkpoint(
        str(path),
        "0",
        pending_session_ids=pending,
        full_walk_in_progress=True,
        pending_attempts=attempts,
        embedding_profile="",
        representation="session_v1",
    )
    return path


def _cycle(store, *, limit: int = 10, ready_content_lookup=None) -> list[dict]:
    return build_cli._select_sessions_for_cycle(
        store,
        limit=limit,
        project=PROJECT,
        provider=PROVIDER,
        backend="postgres_pgvector",
        mode="incremental",
        ready_content_lookup=ready_content_lookup,
    )


def test_source_hash_drift_with_same_content_is_repaired_and_drained() -> None:
    store = InMemoryCouchDBSourceStore()
    sid = _seed(store, "drifted", status=dm.ProjectionStatus.PROJECTED)
    expected = _source_hash(store, sid)

    needs, _ = build_cli._evaluate_session_change(
        store,
        sid,
        project=PROJECT,
        provider=PROVIDER,
        ready_content_lookup=lambda **_: True,
    )
    repaired = store.get(dm.projection_state_doc_id(sid))

    assert needs is False
    assert repaired is not None
    assert repaired["projected_source_hash"] == expected
    assert repaired["backend_receipts"]["postgres_pgvector"]["projected_source_hash"] == expected


def test_changed_content_stays_pending() -> None:
    store = InMemoryCouchDBSourceStore()
    sid = _seed(store, "changed", status=dm.ProjectionStatus.PROJECTED)

    needs, _ = build_cli._evaluate_session_change(
        store,
        sid,
        project=PROJECT,
        provider=PROVIDER,
        ready_content_lookup=lambda **_: False,
    )

    assert needs is True


def test_failed_session_stays_pending() -> None:
    store = InMemoryCouchDBSourceStore()
    sid = _seed(store, "failed", status=dm.ProjectionStatus.FAILED)

    pending = build_cli._resolve_pending_sessions(store, [sid], project=PROJECT, provider=PROVIDER)

    assert [row["session_id_hash"] for row in pending] == [sid]


def test_attempt_cap_is_persisted_and_exhausted_sessions_are_not_reselected(tmp_path: Path, monkeypatch) -> None:
    store = InMemoryCouchDBSourceStore()
    sid = _seed(store, "capped", status=dm.ProjectionStatus.FAILED)
    path = _checkpoint(tmp_path, pending=[sid], attempts={sid: 6})
    monkeypatch.setenv("SESSION_MEMORY_SELECTION_CHECKPOINT", str(path))

    first = _cycle(store)
    checkpoint = build_cli._read_selection_checkpoint(str(path))
    assert first == []
    assert checkpoint["pending_session_attempts"][sid] == 6
    assert sid in checkpoint["exhausted_session_ids"]

    second = _cycle(store)
    assert second == []


def test_new_session_selected_after_completed_pending_drains(tmp_path: Path, monkeypatch) -> None:
    store = InMemoryCouchDBSourceStore()
    completed = _seed(store, "completed", status=dm.ProjectionStatus.PROJECTED)
    fresh = _seed(store, "fresh")
    path = _checkpoint(tmp_path, pending=[completed])
    monkeypatch.setenv("SESSION_MEMORY_SELECTION_CHECKPOINT", str(path))

    selected = _cycle(store, ready_content_lookup=lambda **_: True)

    assert [row["session_id_hash"] for row in selected] == [fresh]
