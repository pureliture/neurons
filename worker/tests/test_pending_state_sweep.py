from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_knowledge.couchdb_source import build_cli
from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_revision import activate_source_revision
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

PROJECT = "neurons"
PROVIDER = "claude"
ACTIVE_HASH = "sha256:" + "a" * 64


class _PgStore:
    def __init__(self, ready_ids: set[str]) -> None:
        self.ready_ids = ready_ids
        self.insert_calls = 0

    def get_chunk(self, chunk_id: str):
        sid = chunk_id.removeprefix("ready-")
        if not any(ready.endswith(sid) for ready in self.ready_ids):
            return None
        return SimpleNamespace(content_hash=ACTIVE_HASH, embedding_state="ready")

    def find_ready_chunk_by_identity(self, **kwargs):
        sid = str(kwargs.get("session_id_hash") or "")
        if sid not in self.ready_ids:
            return None
        return SimpleNamespace(content_hash=ACTIVE_HASH)

    def insert_chunk(self, chunk, **kwargs):
        del chunk
        self.insert_calls += 1
        self.ready_ids.add(str(kwargs.get("session_id_hash") or ""))
        return "inserted"


class _Projector:
    embedding_profile = ""

    def __init__(self, pg: _PgStore, *, fail: bool = False) -> None:
        self.pg = pg
        self.fail = fail
        self.last_segments_total = 1
        self.last_segments_reused = 0
        self.last_segments_embedded = 0

    def project(self, *, target_profile: str, document: dict) -> str:
        del target_profile
        if self.fail:
            raise RuntimeError("simulated projection failure")
        sid = str(document.get("session_id_hash") or "")
        if sid in self.pg.ready_ids:
            self.last_segments_reused, self.last_segments_embedded = 1, 0
            return "ready-row"
        self.pg.insert_chunk(None, session_id_hash=sid)
        self.last_segments_reused, self.last_segments_embedded = 0, 1
        return "new-row"


def _seed(store: InMemoryCouchDBSourceStore, raw_id: str) -> str:
    sid = dm.build_session_id_hash(PROVIDER, raw_id)
    session = TranscriptSession(
        session_id_hash=sid,
        provider=PROVIDER,
        project=PROJECT,
        started_at="2026-06-17T00:00:00Z",
    )
    session_doc = dm.build_transcript_session_document(session=session)
    store.put(session_doc)
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
    activate_source_revision(
        store=store,
        session_id_hash=sid,
        source_document_ids=(session_doc["_id"], chunk_doc["_id"]),
    )
    state = dm.build_projection_state_document(
        session_id_hash=sid,
        provider=PROVIDER,
        project=PROJECT,
        projection_status=dm.ProjectionStatus.PENDING,
        active_content_hash=ACTIVE_HASH,
    )
    state["backend_receipts"] = {
        "postgres_pgvector": {
            "projection_status": dm.ProjectionStatus.PENDING,
            "session_memory_knowledge_id": "ready-" + sid[-8:],
        }
    }
    store.put(state)
    return sid


def _ready_lookup(pg: _PgStore):
    def lookup(*, session_id_hash, project, provider, content_hash, chunk_id=""):
        del project, provider, content_hash, chunk_id
        return session_id_hash in pg.ready_ids

    return lookup


def _write_checkpoint(path: Path, *, last_seq: str = "100") -> None:
    build_cli._write_selection_checkpoint(
        str(path),
        last_seq,
        full_walk_in_progress=False,
        embedding_profile="",
        representation="session_v1",
    )


def _run_cli(store, pg: _PgStore, path: Path, *, fail: bool = False):
    argv = ["--limit", "1", "--approval", str(path.parent / "approval.json")]
    (path.parent / "approval.json").write_text(json.dumps({
        "schema_version": "agent_knowledge_live_approval.v1",
        "operation": "couchdb_session_memory_build",
        "operator_approval": {"approved": True},
        "redaction_required": True,
        "rollback_or_abort_criteria": ["abort on error"],
        "timeout_seconds": 60,
        "command": {"argv": argv},
    }), encoding="utf-8")
    env = {
        "COUCHDB_URL": "http://localhost:5984",
        "COUCHDB_USER": "",
        "COUCHDB_PASSWORD": "",
        "COUCHDB_DB": "transcript_source",
        "SESSION_MEMORY_PROJECTION_BACKEND": "postgres_pgvector",
        "SESSION_MEMORY_SELECTION_MODE": "incremental",
        "SESSION_MEMORY_SELECTION_CHECKPOINT": str(path),
    }
    stdout = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env, clear=True))
        stack.enter_context(patch("sys.stdout", stdout))
        stack.enter_context(
            patch("agent_knowledge.couchdb_source.couchdb_http_store.CouchDBHttpSourceStore",
                  lambda **kwargs: store)
        )
        stack.enter_context(
            patch("agent_knowledge.couchdb_source.build_cli._build_pg_projector",
                  lambda environ: _Projector(pg, fail=fail))
        )
        stack.enter_context(
            patch("agent_knowledge.couchdb_source.build_cli._build_pg_dead_letter_lookup",
                  lambda environ: pg)
        )
        code = build_cli.main(argv)
    return code, json.loads(stdout.getvalue().splitlines()[-1])


def test_pending_state_sweep_counts_couchdb_chunks_without_pg_rows() -> None:
    store = InMemoryCouchDBSourceStore()
    sids = [_seed(store, f"pending-{index}") for index in range(5)]
    pg = _PgStore(ready_ids=set())

    counted = build_cli._pending_state_session_ids(
        store,
        limit=0,
        project=PROJECT,
        provider=PROVIDER,
        ready_content_lookup=_ready_lookup(pg),
        exhausted={},
    )

    assert sorted(counted) == sorted(sids)


def test_incremental_selection_picks_pending_state_sessions(tmp_path: Path, monkeypatch) -> None:
    store = InMemoryCouchDBSourceStore()
    sids = [_seed(store, f"pending-{index}") for index in range(5)]
    pg = _PgStore(ready_ids=set())
    path = tmp_path / "selection-checkpoint.json"
    _write_checkpoint(path)
    monkeypatch.setenv("SESSION_MEMORY_SELECTION_CHECKPOINT", str(path))

    selected = build_cli._select_sessions_for_cycle(
        store,
        limit=3,
        project=PROJECT,
        provider=PROVIDER,
        backend="postgres_pgvector",
        mode="incremental",
        persist=False,
        ready_content_lookup=_ready_lookup(pg),
    )

    assert len(selected) == 3
    assert {row["session_id_hash"] for row in selected} <= set(sids)


def test_selected_pending_session_creates_new_pg_chunk(tmp_path: Path, monkeypatch) -> None:
    store = InMemoryCouchDBSourceStore()
    _seed(store, "new-pg-chunk")
    pg = _PgStore(ready_ids=set())
    path = tmp_path / "selection-checkpoint.json"
    _write_checkpoint(path)
    monkeypatch.setenv("SESSION_MEMORY_SELECTION_CHECKPOINT", str(path))

    code, report = _run_cli(store, pg, path)

    assert code == 0
    assert report["selected"] == 1
    assert report["projected"] == 1
    assert pg.insert_calls == 1


def test_session_failure_keeps_exit_code_one(tmp_path: Path, monkeypatch) -> None:
    store = InMemoryCouchDBSourceStore()
    _seed(store, "failing-session")
    pg = _PgStore(ready_ids=set())
    path = tmp_path / "selection-checkpoint.json"
    _write_checkpoint(path)
    monkeypatch.setenv("SESSION_MEMORY_SELECTION_CHECKPOINT", str(path))

    code, report = _run_cli(store, pg, path, fail=True)

    assert code == 1
    assert report["failed"] == 1
