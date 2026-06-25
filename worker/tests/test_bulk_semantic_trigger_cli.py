from __future__ import annotations

import json

from agent_knowledge.llm_brain_core.bulk_semantic_trigger_cli import (
    run_bulk_semantic_trigger,
)


def test_bulk_semantic_trigger_dry_run_reports_bounded_plan_without_child_call(tmp_path):
    report = run_bulk_semantic_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        limit=17,
        execute=False,
        bulk_main=_exploding_child,
    )

    rendered = json.dumps(report, sort_keys=True)
    assert report["status"] == "dry_run"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
    assert report["plan"]["bounded"] is True
    assert report["plan"]["limit"] == 17
    assert report["plan"]["child_command"] == "couchdb-graph-bulk-semantic"
    assert report["plan"]["runtime_lock"] == "graph-project.lock"
    assert report["raw_paths_printed"] is False
    assert str(tmp_path) not in rendered


def test_bulk_semantic_trigger_execute_calls_child_with_lock_and_redacted_plan(tmp_path):
    calls: list[list[str]] = []

    def _child(argv):  # type: ignore[no-untyped-def]
        calls.append(list(argv or []))
        print(json.dumps({"schema_version": "child.bulk", "status": "ok"}))
        return 0

    report = run_bulk_semantic_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        limit=3,
        project="neurons",
        provider="codex",
        max_projects=5,
        execute=True,
        bulk_main=_child,
    )

    assert report["status"] == "ok"
    assert report["mutation_performed"] is True
    assert report["network_used"] is True
    assert len(calls) == 1
    argv = calls[0]
    assert ["--limit", "3"] == argv[2:4]
    assert "--runtime-dir" in argv
    assert "--max-projects" in argv
    # bulk lane never runs Graphiti per-session entity extraction.
    assert "--enable-graph" not in argv
    assert "--extract-entities" not in argv
    # per-call sizing is child<-env authority unless explicitly forwarded.
    assert "--max-sessions-per-call" not in argv
    assert "--max-session-chars" not in argv
    assert "neurons" not in json.dumps(report, sort_keys=True)


def test_bulk_semantic_trigger_forwards_optional_per_call_caps_when_set(tmp_path):
    calls: list[list[str]] = []

    def _child(argv):  # type: ignore[no-untyped-def]
        calls.append(list(argv or []))
        print(json.dumps({"schema_version": "child.bulk", "status": "ok"}))
        return 0

    run_bulk_semantic_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        max_sessions_per_call=7,
        max_session_chars=900,
        execute=True,
        bulk_main=_child,
    )

    argv = calls[0]
    assert ["--max-sessions-per-call", "7"] == argv[argv.index("--max-sessions-per-call"):argv.index("--max-sessions-per-call") + 2]
    assert ["--max-session-chars", "900"] == argv[argv.index("--max-session-chars"):argv.index("--max-session-chars") + 2]


def test_bulk_semantic_trigger_already_running_is_not_counted_as_mutation(tmp_path):
    def _child(argv):  # type: ignore[no-untyped-def]
        _ = argv
        print(json.dumps({"schema_version": "child.bulk", "status": "already_running"}))
        return 0

    report = run_bulk_semantic_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        execute=True,
        bulk_main=_child,
    )

    assert report["status"] == "already_running"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_bulk_semantic_trigger_failed_child_returns_failed_status(tmp_path):
    def _child(argv):  # type: ignore[no-untyped-def]
        _ = argv
        print(json.dumps({"schema_version": "child.bulk", "status": "failed"}))
        return 1

    report = run_bulk_semantic_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        execute=True,
        bulk_main=_child,
    )

    assert report["status"] == "failed"
    assert report["step"]["exit_code"] == 1


def _exploding_child(argv):  # type: ignore[no-untyped-def]
    _ = argv
    raise AssertionError("child command should not be called")
