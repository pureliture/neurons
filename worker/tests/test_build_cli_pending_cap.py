from __future__ import annotations

from agent_knowledge.couchdb_source.build_cli import (
    MAX_PENDING_ATTEMPTS,
    _apply_pending_attempt_cap,
)


def test_cap_drops_one_session_that_never_finishes() -> None:
    sessions = [
        {"session_id_hash": "sha256:aaa"},
        {"session_id_hash": "sha256:bbb"},
    ]
    attempts = {"sha256:aaa": MAX_PENDING_ATTEMPTS, "sha256:bbb": 1}

    surviving, updated, dropped = _apply_pending_attempt_cap(sessions, attempts)

    assert [s["session_id_hash"] for s in surviving] == ["sha256:bbb"]
    assert dropped == 1
    assert updated == {"sha256:bbb": 2}


def test_cap_increments_and_preserves_attempt_counts() -> None:
    sessions = [{"session_id_hash": "sha256:ccc"}]

    surviving, updated, dropped = _apply_pending_attempt_cap(sessions, {})

    assert dropped == 0
    assert surviving == sessions
    assert updated == {"sha256:ccc": 1}


def test_pending_state_sweep_is_bounded_per_shard() -> None:
    from agent_knowledge.couchdb_source import document_model as dm
    from agent_knowledge.couchdb_source.build_cli import _pending_state_session_ids
    from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
    from agent_knowledge.session_memory.transcript_model import TranscriptChunk

    class _RecordingStore(InMemoryCouchDBSourceStore):
        def __init__(self) -> None:
            super().__init__()
            self.iter_calls: list[dict] = []

        def iter_by_type(self, doc_type, **kwargs):
            self.iter_calls.append({"doc_type": doc_type, **kwargs})
            return super().iter_by_type(doc_type, **kwargs)

    store = _RecordingStore()
    session_ids = [
        dm.sha256_hash(f"pending-sweep-session-{idx}") for idx in range(4)
    ]
    for idx, sid in enumerate(session_ids):
        store.put(
            dm.build_projection_state_document(
                session_id_hash=sid,
                provider="codex",
                project="neurons",
                projection_status="pending",
                active_content_hash=dm.sha256_hash(f"artifact-{idx}"),
            )
        )
        store.put(
            dm.build_conversation_chunk_document(
                chunk=TranscriptChunk.from_text(
                    chunk_id=f"chunk_{idx}",
                    session_id_hash=sid,
                    provider="codex",
                    project="neurons",
                    turn_start_index=0,
                    turn_end_index=0,
                    text=f"body {idx}",
                )
            )
        )

    selected = _pending_state_session_ids(
        store,
        limit=2,
        project="",
        provider="",
        ready_content_lookup=lambda **_: False,
        exhausted={},
        shard_index=0,
        shard_count=3,
    )

    sweeps = [
        call
        for call in store.iter_calls
        if call["doc_type"] == dm.SourceDocType.PROJECTION_STATE
    ]
    assert len(sweeps) == 1
    assert sweeps[0]["limit"] == 6  # limit * shard_count, never the old unbounded 0
    assert sweeps[0]["page_size"] == 256
    assert "_id" in sweeps[0]["fields"]
    assert len(selected) <= 2

    store.iter_calls.clear()
    _pending_state_session_ids(
        store,
        limit=2,
        project="",
        provider="",
        ready_content_lookup=lambda **_: False,
        exhausted={},
        shard_index=0,
        shard_count=1,
    )
    assert [
        call["limit"]
        for call in store.iter_calls
        if call["doc_type"] == dm.SourceDocType.PROJECTION_STATE
    ] == [2]
