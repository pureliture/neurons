from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

from agent_knowledge.couchdb_source import build_cli
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from test_pending_state_sweep import (
    PROVIDER,
    PROJECT,
    _PgStore,
    _Projector,
    _ready_lookup,
    _seed,
    _write_checkpoint,
)


def _run(store, pg, path: Path, *, cycle_chunks: int, per_session: int = 16):
    argv = ["--limit", "50", "--approval", str(path.parent / "approval.json")]
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
        "PG_EMBEDDING_CHUNKS_PER_CYCLE": str(cycle_chunks),
        "PG_EMBEDDING_CHUNKS_PER_SESSION": str(per_session),
    }
    stdout, stderr = io.StringIO(), io.StringIO()
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
                  lambda environ: _Projector(pg))
        )
        stack.enter_context(
            patch("agent_knowledge.couchdb_source.build_cli._build_pg_dead_letter_lookup",
                  lambda environ: pg)
        )
        code = build_cli.main(argv)
    report = json.loads(stdout.getvalue().splitlines()[-1])
    return code, report, stderr.getvalue()


def test_cycle_cap_truncates_batch_and_emits_pending_state_sweep_log(tmp_path: Path, monkeypatch) -> None:
    store = InMemoryCouchDBSourceStore()
    sids = [_seed(store, f"cap-{index}") for index in range(10)]
    pg = _PgStore(ready_ids=set())
    path = tmp_path / "selection-checkpoint.json"
    _write_checkpoint(path)
    monkeypatch.setenv("SESSION_MEMORY_SELECTION_CHECKPOINT", str(path))

    code, report, stderr = _run(store, pg, path, cycle_chunks=96)

    assert code == 0
    assert report["selected"] == 6
    assert report["embedding_cycle_limit"] == 96
    assert report["embedding_capped"] is True
    assert "pending_state_sweep scanned=" in stderr
    assert "missing_pg=" in stderr
    assert {row["session_id_hash"] for row in report["sessions"]} <= set(sids)


def test_remainder_is_processed_on_next_cycle(tmp_path: Path, monkeypatch) -> None:
    store = InMemoryCouchDBSourceStore()
    sids = [_seed(store, f"carry-{index}") for index in range(5)]
    pg = _PgStore(ready_ids=set())
    path = tmp_path / "selection-checkpoint.json"
    _write_checkpoint(path)
    monkeypatch.setenv("SESSION_MEMORY_SELECTION_CHECKPOINT", str(path))

    first_code, first, first_stderr = _run(store, pg, path, cycle_chunks=32)
    second_code, second, second_stderr = _run(store, pg, path, cycle_chunks=32)

    assert first_code == 0 and second_code == 0
    assert first["selected"] == 2
    assert second["selected"] == 2
    assert "pending_state_sweep scanned=" in first_stderr
    assert "pending_state_sweep scanned=" in second_stderr
    first_ids = {row["session_id_hash"] for row in first["sessions"]}
    second_ids = {row["session_id_hash"] for row in second["sessions"]}
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids <= set(sids)
    assert pg.insert_calls == 4
