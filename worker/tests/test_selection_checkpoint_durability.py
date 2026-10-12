"""Durability of the session-selection checkpoint (capped walk / interruption).

Selection and projection are separate steps, so the checkpoint may only
acknowledge work that is actually done:

* a full walk stopped by ``limit`` must not claim the database-wide sequence as
  seen, otherwise the untouched tail is hidden behind an incremental feed that
  never re-emits it;
* a session chosen before projection completed must stay durable pending work,
  otherwise a failure or restart loses it.
"""
from __future__ import annotations

import os
from unittest.mock import patch

from agent_knowledge.couchdb_source import build_cli
from agent_knowledge.couchdb_source.session_memory_materializer import (
    RecordingSessionMemoryProjector,
    materialize_and_project,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from test_couchdb_build_cli import _build_synthetic_session

PROJECT = "durability-test"
PROVIDER = "claude"


def _cycle(store, limit: int, checkpoint, *, mode: str = "auto") -> list[dict]:
    with patch.dict(os.environ, {"SESSION_MEMORY_SELECTION_CHECKPOINT": str(checkpoint)}):
        return build_cli._select_sessions_for_cycle(
            store,
            limit=limit,
            project="",
            provider="",
            backend="postgres_pgvector",
            mode=mode,
        )


def _project(session_id_hash: str, store) -> None:
    result = materialize_and_project(
        session_id_hash=session_id_hash,
        store=store,
        projector=RecordingSessionMemoryProjector(),
        backend="postgres_pgvector",
    )
    assert result["projection"]["status"] == "projected"


def test_capped_full_walk_keeps_selecting_the_unexamined_tail(tmp_path) -> None:
    store = InMemoryCouchDBSourceStore()
    sids = [
        _build_synthetic_session(store, provider=PROVIDER, project=PROJECT, raw_id=f"tail-{index}")
        for index in range(3)
    ]
    checkpoint = tmp_path / "selection.json"

    first = _cycle(store, 1, checkpoint)
    assert len(first) == 1
    # A capped walk has not examined the remaining sessions, so it must not be
    # recorded as a completed change-feed watermark.
    state = build_cli._read_selection_checkpoint(str(checkpoint))
    assert state.get("full_walk_in_progress") is True
    assert state.get("last_seq") == ""

    selected: list[str] = [first[0]["session_id_hash"]]
    _project(selected[-1], store)
    for _ in range(3):
        batch = _cycle(store, 1, checkpoint)
        if not batch:
            break
        assert len(batch) == 1
        # Re-selecting an already-projected session would be an unnecessary duplicate.
        assert batch[0]["session_id_hash"] not in selected
        selected.append(batch[0]["session_id_hash"])
        _project(selected[-1], store)

    assert set(selected) == set(sids)
    assert _cycle(store, 1, checkpoint) == []
    settled = build_cli._read_selection_checkpoint(str(checkpoint))
    assert settled.get("full_walk_in_progress") is not True
    assert settled.get("last_seq")
    assert "pending_session_ids" not in settled


def test_selected_but_unprojected_session_is_reselected(tmp_path) -> None:
    store = InMemoryCouchDBSourceStore()
    sid = _build_synthetic_session(store, provider=PROVIDER, project=PROJECT, raw_id="interrupted")
    checkpoint = tmp_path / "selection.json"

    first = _cycle(store, 10, checkpoint)
    assert [row["session_id_hash"] for row in first] == [sid]
    # Simulate a crash between selection and projection: no materialize call.
    second = _cycle(store, 10, checkpoint)
    assert [row["session_id_hash"] for row in second] == [sid]
    state = build_cli._read_selection_checkpoint(str(checkpoint))
    assert state.get("pending_session_ids") == [sid]

    _project(sid, store)
    assert _cycle(store, 10, checkpoint) == []
    settled = build_cli._read_selection_checkpoint(str(checkpoint))
    assert "pending_session_ids" not in settled


def test_projected_pending_session_is_not_requeued_and_new_work_still_arrives(tmp_path) -> None:
    store = InMemoryCouchDBSourceStore()
    first_sid = _build_synthetic_session(store, provider=PROVIDER, project=PROJECT, raw_id="done")
    checkpoint = tmp_path / "selection.json"

    assert [row["session_id_hash"] for row in _cycle(store, 10, checkpoint)] == [first_sid]
    _project(first_sid, store)
    assert _cycle(store, 10, checkpoint) == []

    second_sid = _build_synthetic_session(store, provider=PROVIDER, project=PROJECT, raw_id="fresh")
    assert [row["session_id_hash"] for row in _cycle(store, 10, checkpoint)] == [second_sid]


def test_pending_work_keeps_its_own_slot_when_it_fills_the_run_cap(tmp_path) -> None:
    store = InMemoryCouchDBSourceStore()
    first_sid = _build_synthetic_session(store, provider=PROVIDER, project=PROJECT, raw_id="poison-a")
    checkpoint = tmp_path / "selection.json"

    assert [row["session_id_hash"] for row in _cycle(store, 1, checkpoint)] == [first_sid]
    # The pending session still fails to project, so it keeps occupying the only
    # slot; it must never be dropped from the checkpoint.
    for _ in range(3):
        selected = _cycle(store, 1, checkpoint)
        assert [row["session_id_hash"] for row in selected] == [first_sid]
        state = build_cli._read_selection_checkpoint(str(checkpoint))
        assert state.get("pending_session_ids") == [first_sid]

    _project(first_sid, store)
    second_sid = _build_synthetic_session(store, provider=PROVIDER, project=PROJECT, raw_id="after-poison")
    assert [row["session_id_hash"] for row in _cycle(store, 1, checkpoint)] == [second_sid]
