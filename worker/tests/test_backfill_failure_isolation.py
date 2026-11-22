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
READY_HASH = "sha256:" + "b" * 64


class _PgStore:
    def __init__(self, ready_ids: set[str]) -> None:
        self.ready_ids = ready_ids
        self.get_calls = 0
        self.insert_calls = 0

    def get_chunk(self, chunk_id: str):
        self.get_calls += 1
        sid = chunk_id.removeprefix("ready-")
        if not any(ready.endswith(sid) for ready in self.ready_ids):
            return None
        return SimpleNamespace(content_hash=READY_HASH, embedding_state="ready")

    def find_ready_chunk_by_identity(self, **kwargs):
        self.get_calls += 1
        sid = str(kwargs.get("session_id_hash") or "")
        if sid not in self.ready_ids:
            return None
        return SimpleNamespace(content_hash=READY_HASH)

    def insert_chunk(self, chunk, **kwargs):
        del chunk
        self.insert_calls += 1
        self.ready_ids.add(str(kwargs.get("session_id_hash") or ""))
        return "inserted"


class _Projector:
    embedding_profile = ""

    def __init__(self, *, fail_ids: set[str], pg: _PgStore) -> None:
        self.fail_ids = fail_ids
        self.pg = pg
        self.last_segments_total = 1
        self.last_segments_reused = 0
        self.last_segments_embedded = 0

    def project(self, *, target_profile: str, document: dict) -> str:
        del target_profile
        sid = str(document.get("session_id_hash") or "")
        if sid in self.fail_ids:
            raise RuntimeError("simulated per-session projection failure")
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
        projection_status=dm.ProjectionStatus.PROJECTED,
        projected_source_hash="sha256:" + "c" * 64,
        active_content_hash=ACTIVE_HASH,
    )
    state["backend_receipts"] = {
        "postgres_pgvector": {
            "projection_status": dm.ProjectionStatus.PROJECTED,
            "session_memory_knowledge_id": "ready-" + sid[-8:],
        }
    }
    store.put(state)
    return sid


def _pending_ids(checkpoint: Path) -> list[str]:
    return build_cli._pending_session_ids(build_cli._read_selection_checkpoint(str(checkpoint)))


def _run_build(store, *, fail_ids: set[str], pg: _PgStore, checkpoint: Path):
    argv = ["--limit", "50", "--approval", str(checkpoint.parent / "approval.json")]
    (checkpoint.parent / "approval.json").write_text(json.dumps({
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
        "PG_EMBEDDING_CYCLE_CAP_ENABLED": "1",
        "SESSION_MEMORY_SELECTION_MODE": "full",
        "SESSION_MEMORY_SELECTION_CHECKPOINT": str(checkpoint),
    }
    stdout, stderr = io.StringIO(), io.StringIO()
    code = None
    error = None
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env, clear=True))
        stack.enter_context(patch("sys.stdout", stdout))
        stack.enter_context(patch("sys.stderr", stderr))
        stack.enter_context(
            patch("agent_knowledge.couchdb_source.couchdb_http_store.CouchDBHttpSourceStore",
                  lambda **kwargs: store)
        )
        stack.enter_context(
            patch("agent_knowledge.couchdb_source.build_cli._build_pg_projector",
                  lambda environ: _Projector(fail_ids=fail_ids, pg=pg))
        )
        stack.enter_context(
            patch("agent_knowledge.couchdb_source.build_cli._build_pg_dead_letter_lookup",
                  lambda environ: pg)
        )
        try:
            code = build_cli.main(argv)
        except BaseException as exc:  # noqa: BLE001 - reproduction harness
            error = exc
    lines = stdout.getvalue().splitlines()
    report = json.loads(lines[-1]) if lines else None
    return code, report, stderr.getvalue(), error


def test_full_rescan_isolates_failures_and_drains_completed(tmp_path: Path) -> None:
    store = InMemoryCouchDBSourceStore()
    sids = [_seed(store, f"session-{index}") for index in range(55)]
    selected_ids = [
        str(row["session_id_hash"])
        for row in build_cli._select_sessions_for_cycle(
            store,
            limit=50,
            project=PROJECT,
            provider=PROVIDER,
            backend="postgres_pgvector",
            mode="full",
            persist=False,
        )
    ]
    fail_ids = set(selected_ids[:2])
    missing_pg_ids = set(selected_ids[2:4])
    ready_ids = set(sids) - missing_pg_ids - fail_ids
    pg = _PgStore(ready_ids=ready_ids)
    remaining_ids = set(sids) - set(selected_ids)
    checkpoint = tmp_path / "selection-checkpoint.json"
    build_cli._write_selection_checkpoint(
        str(checkpoint), "0", full_walk_in_progress=True,
        embedding_profile="", representation="session_v1",
    )

    first_code, first, first_stderr, first_error = _run_build(
        store, fail_ids=fail_ids, pg=pg, checkpoint=checkpoint
    )
    first_pending = _pending_ids(checkpoint)
    second_code, second, second_stderr, second_error = _run_build(
        store, fail_ids=fail_ids, pg=pg, checkpoint=checkpoint
    )
    second_pending = _pending_ids(checkpoint)

    assert first_error is None, first_stderr[-2000:]
    assert second_error is None, second_stderr[-2000:]
    # Failures are isolated per session, but a run that had failures still
    # reports failure: monitoring depends on this exit-code contract.
    assert first_code == 1
    assert second_code == 1
    assert first["selected"] == 32
    assert first["projected"] == 30
    assert first["failed"] == 2
    assert first["skipped"] == 0
    assert first["segments_reused"] > 0
    assert first["segments_embedded"] > 0
    assert pg.insert_calls >= 2
    assert set(first_pending) == fail_ids
    assert sorted(
        {row["session_id_hash"] for row in second.get("sessions", [])}
        & remaining_ids
    ) == sorted(remaining_ids)
    assert set(second_pending) == fail_ids
