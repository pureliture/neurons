from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from agent_knowledge.couchdb_source import build_cli
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.rag_ingress.pg_backfill import PgSessionMemoryProjector
from agent_knowledge.rag_ingress.pg_embedding_chunks import BoundedPgEmbedding
from agent_knowledge.model_connectors import DEFAULT_EMBEDDING_DIM, DEFAULT_EMBEDDING_MODEL
from test_pending_state_sweep import (
    PROJECT,
    PROVIDER,
    _PgStore,
    _seed,
    _write_checkpoint,
)


class _Projector:
    embedding_profile = ""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.exempted = False
        self.last_segments_total = 1
        self.last_segments_reused = 0
        self.last_segments_embedded = 1

    def exempt_from_cycle_budget(self) -> None:
        self.exempted = True

    def project(self, *, target_profile: str, document: dict) -> str:
        del target_profile, document
        if self.fail:
            raise RuntimeError("simulated projection error")
        return "ready-row"

    def close(self) -> None:
        return None


def _run(store, pg, path: Path, *, fail: bool = False):
    argv = ["--project", PROJECT, "--approval", str(path.parent / "approval.json")]
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
    projector = _Projector(fail=fail)
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
                  lambda environ: projector)
        )
        stack.enter_context(
            patch("agent_knowledge.couchdb_source.build_cli._build_pg_dead_letter_lookup",
                  lambda environ: pg)
        )
        code = build_cli.main(argv)
    return code, json.loads(stdout.getvalue().splitlines()[-1]), projector


def test_single_session_manual_run_is_exempt_and_projected(tmp_path: Path, monkeypatch) -> None:
    store = InMemoryCouchDBSourceStore()
    _seed(store, "single-session")
    pg = _PgStore(ready_ids=set())
    path = tmp_path / "selection-checkpoint.json"
    _write_checkpoint(path)
    monkeypatch.setenv("SESSION_MEMORY_SELECTION_CHECKPOINT", str(path))

    code, report, projector = _run(store, pg, path)

    assert code == 0
    assert report["projected"] == 1
    assert report["failed"] == 0
    assert projector.exempted is True


def test_real_single_session_error_still_exits_one(tmp_path: Path, monkeypatch) -> None:
    store = InMemoryCouchDBSourceStore()
    _seed(store, "failing-single-session")
    pg = _PgStore(ready_ids=set())
    path = tmp_path / "selection-checkpoint.json"
    _write_checkpoint(path)
    monkeypatch.setenv("SESSION_MEMORY_SELECTION_CHECKPOINT", str(path))

    code, report, _projector = _run(store, pg, path, fail=True)

    assert code == 1
    assert report["failed"] == 1


def test_single_session_exemption_lifts_session_chunk_budget(tmp_path: Path) -> None:
    calls = []

    class _Delegate:
        model = DEFAULT_EMBEDDING_MODEL
        size = DEFAULT_EMBEDDING_DIM

        def embed(self, text: str) -> list[float]:
            del text
            calls.append(1)
            return [0.1] * DEFAULT_EMBEDDING_DIM

    bounded = BoundedPgEmbedding(
        _Delegate(),
        cache_path=str(tmp_path / "cache.sqlite3"),
        endpoint_scope="test",
        cycle_budget=1,
        session_budget=1,
    )
    projector = PgSessionMemoryProjector(
        SimpleNamespace(), _Delegate(), chunk_embedder=bounded
    )
    projector.exempt_from_cycle_budget()

    bounded.embed("x" * 17000, content_hash="sha256:" + "a" * 64)

    assert len(calls) >= 2  # chunk count exceeded original session_budget=1
