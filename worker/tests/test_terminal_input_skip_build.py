from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
from unittest.mock import patch
from pathlib import Path

from agent_knowledge.couchdb_source import build_cli
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.rag_ingress.pg_embedding_privacy import EGRESS_POLICY_ERROR
from test_pending_state_sweep import PROJECT, _PgStore, _seed, _write_checkpoint


class _Projector:
    embedding_profile = ""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.last_segments_total = 1
        self.last_segments_reused = 0
        self.last_segments_embedded = 0

    def project(self, *, target_profile: str, document: dict) -> str:
        del target_profile, document
        if self.mode == "terminal":
            raise ValueError(EGRESS_POLICY_ERROR)
        if self.mode == "error":
            raise RuntimeError("simulated real error")
        return "ready-row"

    def close(self) -> None:
        return None


def _run(store, pg, path: Path, audit_path: Path, mode: str):
    argv = ["--limit", "2", "--approval", str(path.parent / "approval.json")]
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
        "SESSION_MEMORY_SELECTION_MODE": "full",
        "SESSION_MEMORY_SELECTION_CHECKPOINT": str(path),
        "LLM_BRAIN_LEDGER_PATH": str(audit_path),
    }
    stdout = io.StringIO()
    stderr = io.StringIO()
    projector = _Projector(mode)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env, clear=True))
        stack.enter_context(patch("sys.stdout", stdout))
        stack.enter_context(patch("sys.stderr", stderr))
        stack.enter_context(patch(
            "agent_knowledge.couchdb_source.couchdb_http_store.CouchDBHttpSourceStore",
            lambda **kwargs: store,
        ))
        stack.enter_context(patch(
            "agent_knowledge.couchdb_source.build_cli._build_pg_projector",
            lambda environ: projector,
        ))
        stack.enter_context(patch(
            "agent_knowledge.couchdb_source.build_cli._build_pg_dead_letter_lookup",
            lambda environ: pg,
        ))
        code = build_cli.main(argv)
    lines = stdout.getvalue().splitlines()
    return code, json.loads(lines[-1]), stderr.getvalue()


def _audit_count(path: Path) -> int:
    if not path.exists():
        return 0
    with sqlite3.connect(path) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_memory_terminal_skipped_audit'"
        ).fetchone()
        if not exists:
            return 0
        return int(connection.execute(
            "SELECT count(*) FROM session_memory_terminal_skipped_audit"
        ).fetchone()[0])


def test_terminal_reason_is_skipped_audited_and_not_reselected(tmp_path: Path) -> None:
    store = InMemoryCouchDBSourceStore()
    _seed(store, "terminal-session")
    pg = _PgStore(ready_ids=set())
    path = tmp_path / "checkpoint.json"
    audit = tmp_path / "ledger.sqlite3"
    _write_checkpoint(path)

    code, report, stderr = _run(store, pg, path, audit, "terminal")

    assert code == 0
    assert report["failed"] == 0
    assert report["terminal_skipped"] == 1
    assert report["terminal_audit_written"] == 1
    assert report["terminal_skipped_reason_counts"] == {"secret_egress_policy": 1}
    assert "terminal_input_skip_summary" in stderr
    assert "secret_egress_policy" in stderr
    assert _audit_count(audit) == 1

    code2, report2, _stderr2 = _run(store, pg, path, audit, "error")
    assert report2["selected"] == 0
    assert code2 == 0
    assert _audit_count(audit) == 1


def test_real_error_still_fails_job(tmp_path: Path) -> None:
    store = InMemoryCouchDBSourceStore()
    _seed(store, "real-error-session")
    pg = _PgStore(ready_ids=set())
    path = tmp_path / "checkpoint.json"
    audit = tmp_path / "ledger.sqlite3"
    _write_checkpoint(path)

    code, report, _stderr = _run(store, pg, path, audit, "error")

    assert code == 1
    assert report["failed"] == 1
    assert report.get("terminal_skipped", 0) == 0
    assert _audit_count(audit) == 0


def test_success_path_is_unchanged(tmp_path: Path) -> None:
    store = InMemoryCouchDBSourceStore()
    _seed(store, "success-session")
    pg = _PgStore(ready_ids=set())
    path = tmp_path / "checkpoint.json"
    audit = tmp_path / "ledger.sqlite3"
    _write_checkpoint(path)

    code, report, _stderr = _run(store, pg, path, audit, "success")

    assert code == 0
    assert report["projected"] == 1
    assert report["failed"] == 0
    assert report.get("terminal_skipped", 0) == 0
    assert _audit_count(audit) == 0
