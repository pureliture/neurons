from __future__ import annotations

import os
import time
from pathlib import Path
import subprocess
import sys

import pytest

from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core import couchdb_projection_cli
from agent_knowledge.llm_brain_core.couchdb_projection_cli import GraphProjectionDeadlineExceeded
from agent_knowledge.llm_brain_core.graphiti_adapter import GraphitiNeo4jGraphMemoryAdapter
from test_couchdb_graph_projection_cli import (
    _EntityFlagFakeGraph,
    _project,
    _seed_session,
)
from test_graphiti_neo4j_adapter import _SlowAddGraphiti, _episode


def test_dead_lock_holder_is_recovered_on_restart(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    ready = tmp_path / "ready"

    script = (
        "import pathlib,sys;"
        "from agent_knowledge.llm_brain_core.couchdb_projection_cli import _acquire_runtime_lock;"
        "handle,report=_acquire_runtime_lock(pathlib.Path(sys.argv[1]));"
        "sys.exit(2 if report is not None else 0)"
    )
    subprocess.run(
        [sys.executable, "-c", script, str(runtime)],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        timeout=30,
    )
    ready.write_text("held", encoding="utf-8")

    store = InMemoryCouchDBSourceStore()
    _seed_session(store, raw_id="dead-holder")
    report = _project(
        tmp_path=tmp_path,
        store=store,
        graph_adapter=_EntityFlagFakeGraph(),
        limit=1,
        runtime_dir=runtime,
    )

    assert report["runtime_lock"]["acquired"] is True
    assert report["projection"]["projected"] == 1


def test_hung_llm_call_is_bounded_by_configured_timeout(monkeypatch) -> None:
    monkeypatch.delenv("LLM_BRAIN_GRAPH_PRIMARY_ATTEMPT_TIMEOUT_SECONDS", raising=False)
    default_env = couchdb_projection_cli._graph_environ(
        extract_entities=True, reextract_entities=False
    )
    assert float(default_env["LLM_BRAIN_GRAPH_PRIMARY_ATTEMPT_TIMEOUT_SECONDS"]) == (
        couchdb_projection_cli.DEFAULT_GRAPH_LLM_ATTEMPT_TIMEOUT_SECONDS
    )

    monkeypatch.setenv("LLM_BRAIN_GRAPH_PRIMARY_ATTEMPT_TIMEOUT_SECONDS", "0.05")
    env = couchdb_projection_cli._graph_environ(
        extract_entities=True, reextract_entities=False
    )
    timeout = float(env["LLM_BRAIN_GRAPH_PRIMARY_ATTEMPT_TIMEOUT_SECONDS"])
    assert timeout == 0.05

    primary = _SlowAddGraphiti(delay_seconds=1.0)
    adapter = GraphitiNeo4jGraphMemoryAdapter(
        primary,
        extract_entities=True,
        primary_attempts=1,
        primary_attempt_timeout_seconds=timeout,
    )
    episode = _episode("Task", "task:hung-llm", {"brain_id": "/project/neurons", "task": "hung"})

    started = time.monotonic()
    with pytest.raises(Exception):
        adapter.upsert_episode(episode)
    assert time.monotonic() - started < 1.0


def test_no_progress_run_exceeds_deadline_and_releases_lock(tmp_path: Path) -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_session(store, raw_id="hung-session")
    runtime = tmp_path / "runtime"

    class _BlockedGraph(_EntityFlagFakeGraph):
        def upsert_episode(self, episode):  # type: ignore[no-untyped-def]
            time.sleep(5)
            return super().upsert_episode(episode)

    with pytest.raises(GraphProjectionDeadlineExceeded):
        couchdb_projection_cli.run_couchdb_projection(
            ledger_path=tmp_path / "deadline-ledger.sqlite3",
            source_store=store,
            limit=1,
            project="neurons",
            provider="codex",
            enable_graph=True,
            graph_required=False,
            extract_entities=True,
            graph_adapter=_BlockedGraph(),
            runtime_dir=runtime,
            runtime_deadline_seconds=0.2,
        )

    handle, report = couchdb_projection_cli._acquire_runtime_lock(runtime)
    assert report is None
    couchdb_projection_cli._release_runtime_lock(handle)
