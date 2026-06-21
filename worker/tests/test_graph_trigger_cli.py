from __future__ import annotations

import json
from pathlib import Path

from agent_knowledge.llm_brain_core.graph_trigger_cli import run_graph_trigger


def test_graph_trigger_dry_run_reports_bounded_plan_without_child_call(tmp_path):
    report = run_graph_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        limit=17,
        execute=False,
        graph_main=_exploding_child,
    )

    rendered = json.dumps(report, sort_keys=True)
    assert report["status"] == "dry_run"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
    assert report["plan"]["bounded"] is True
    assert report["plan"]["limit"] == 17
    assert report["plan"]["child_command"] == "couchdb-graph-project"
    assert str(tmp_path) not in rendered
    assert report["raw_paths_printed"] is False


def test_graph_trigger_execute_calls_graph_project_with_lock_and_outputs_redacted_plan(tmp_path):
    calls: list[list[str]] = []

    def _child(argv):  # type: ignore[no-untyped-def]
        calls.append(list(argv or []))
        print(json.dumps({"schema_version": "child.graph", "status": "ok"}))
        return 0

    report = run_graph_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        limit=3,
        project="neurons",
        provider="codex",
        execute=True,
        graph_main=_child,
    )

    assert report["status"] == "ok"
    assert report["mutation_performed"] is True
    assert report["network_used"] is True
    assert len(calls) == 1
    argv = calls[0]
    assert "--runtime-dir" in argv
    assert "--enable-graph" in argv
    assert "--extract-entities" in argv
    assert ["--limit", "3"] == argv[2:4]
    assert "neurons" not in json.dumps(report, sort_keys=True)


def test_graph_trigger_already_running_is_not_counted_as_mutation(tmp_path):
    def _child(argv):  # type: ignore[no-untyped-def]
        _ = argv
        print(json.dumps({"schema_version": "child.graph", "status": "already_running"}))
        return 0

    report = run_graph_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        execute=True,
        graph_main=_child,
    )

    assert report["status"] == "already_running"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_graph_trigger_failed_child_returns_failed_status(tmp_path):
    def _child(argv):  # type: ignore[no-untyped-def]
        _ = argv
        print(json.dumps({"schema_version": "child.graph", "status": "failed"}))
        return 1

    report = run_graph_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        execute=True,
        graph_main=_child,
    )

    assert report["status"] == "failed"
    assert report["step"]["exit_code"] == 1


def _exploding_child(argv):  # type: ignore[no-untyped-def]
    _ = argv
    raise AssertionError("child command should not be called")
