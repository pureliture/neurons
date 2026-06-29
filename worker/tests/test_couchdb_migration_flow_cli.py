from __future__ import annotations

import json
from pathlib import Path

from agent_knowledge.couchdb_source.migration_flow_cli import (
    COUCHDB_MIGRATION_FLOW_OPERATION,
    run_migration_flow,
)


def _approval(path: Path, *, argv: list[str], operation: str = COUCHDB_MIGRATION_FLOW_OPERATION) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": "agent_knowledge_live_approval.v1",
                "operation": operation,
                "operator_approval": {"approved": True},
                "redaction_required": True,
                "rollback_or_abort_criteria": ["abort on error"],
                "timeout_seconds": 60,
                "command": {"argv": argv},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_dry_run_returns_plan_without_child_execution(tmp_path):
    ledger = tmp_path / "ledger.sqlite3"

    report = run_migration_flow(
        ledger_path=ledger,
        limit=25,
        project="neurons",
        provider="codex",
        execute=False,
        session_memory_main=_exploding_child,
        graph_main=_exploding_child,
    )

    rendered = json.dumps(report, sort_keys=True)
    assert report["status"] == "dry_run"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
    assert report["plan"]["step_count"] == 2
    assert [step["name"] for step in report["plan"]["steps"]] == [
        "session_memory_build",
        "graph_project",
    ]
    assert str(tmp_path) not in rendered
    assert report["raw_paths_printed"] is False


def test_execute_without_outer_approval_fails_closed_before_children(tmp_path):
    ledger = tmp_path / "ledger.sqlite3"

    report = run_migration_flow(
        ledger_path=ledger,
        limit=1,
        execute=True,
        session_memory_approval=tmp_path / "child-approval.json",
        command_argv=["--ledger", str(ledger), "--execute"],
        session_memory_main=_exploding_child,
        graph_main=_exploding_child,
    )

    assert report["status"] == "blocked"
    assert report["reason_code"] == "approval_rejected"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_execute_runs_session_memory_then_graph_with_existing_approvals(tmp_path):
    ledger = tmp_path / "ledger.sqlite3"
    outer_approval = tmp_path / "flow-approval.json"
    child_approval = tmp_path / "session-approval.json"
    command_argv = [
        "--ledger",
        str(ledger),
        "--execute",
        "--approval",
        str(outer_approval),
        "--session-memory-approval",
        str(child_approval),
    ]
    _approval(outer_approval, argv=command_argv)
    calls: list[tuple[str, list[str]]] = []

    def _session(argv):  # type: ignore[no-untyped-def]
        calls.append(("session", list(argv or [])))
        print(json.dumps({"schema_version": "child.session", "projected": 1}))
        return 0

    def _graph(argv):  # type: ignore[no-untyped-def]
        calls.append(("graph", list(argv or [])))
        print(json.dumps({"schema_version": "child.graph", "projection": {"projected": 1}}))
        return 0

    report = run_migration_flow(
        ledger_path=ledger,
        limit=1,
        execute=True,
        approval=outer_approval,
        session_memory_approval=child_approval,
        command_argv=command_argv,
        session_memory_main=_session,
        graph_main=_graph,
    )

    assert report["status"] == "ok"
    assert [name for name, _ in calls] == ["session", "graph"]
    assert calls[0][1][:4] == ["--limit", "1", "--approval", str(child_approval)]
    assert "--extract-entities" in calls[1][1]
    assert "--enable-graph" in calls[1][1]
    assert [step["exit_code"] for step in report["steps"]] == [0, 0]


def test_execute_stops_before_graph_when_session_memory_fails(tmp_path):
    ledger = tmp_path / "ledger.sqlite3"
    outer_approval = tmp_path / "flow-approval.json"
    child_approval = tmp_path / "session-approval.json"
    command_argv = ["--ledger", str(ledger), "--execute", "--approval", str(outer_approval)]
    _approval(outer_approval, argv=command_argv)
    calls = {"graph": 0}

    def _session(argv):  # type: ignore[no-untyped-def]
        _ = argv
        print(json.dumps({"schema_version": "child.session", "failed": 1}))
        return 1

    def _graph(argv):  # type: ignore[no-untyped-def]
        _ = argv
        calls["graph"] += 1
        return 0

    report = run_migration_flow(
        ledger_path=ledger,
        limit=1,
        execute=True,
        approval=outer_approval,
        session_memory_approval=child_approval,
        command_argv=command_argv,
        session_memory_main=_session,
        graph_main=_graph,
    )

    assert report["status"] == "partial"
    assert len(report["steps"]) == 1
    assert report["steps"][0]["name"] == "session_memory_build"
    assert report["steps"][0]["exit_code"] == 1
    assert calls["graph"] == 0


def test_execute_child_general_exception_is_reported_not_crashed(tmp_path):
    ledger = tmp_path / "ledger.sqlite3"
    outer_approval = tmp_path / "flow-approval.json"
    child_approval = tmp_path / "session-approval.json"
    command_argv = ["--ledger", str(ledger), "--execute", "--approval", str(outer_approval)]
    _approval(outer_approval, argv=command_argv)

    def _session(argv):  # type: ignore[no-untyped-def]
        _ = argv
        raise ConnectionError("retired_index_bridge unreachable")

    def _graph(argv):  # type: ignore[no-untyped-def]
        _ = argv
        raise AssertionError("graph should not run after session crash")

    report = run_migration_flow(
        ledger_path=ledger,
        limit=1,
        execute=True,
        approval=outer_approval,
        session_memory_approval=child_approval,
        command_argv=command_argv,
        session_memory_main=_session,
        graph_main=_graph,
    )

    assert report["status"] == "partial"
    assert len(report["steps"]) == 1
    assert report["steps"][0]["name"] == "session_memory_build"
    assert report["steps"][0]["exit_code"] == 1
    assert report["steps"][0]["error_class"] == "ConnectionError"
    assert report["steps"][0]["stderr_present"] is True
    rendered = json.dumps(report, sort_keys=True)
    assert str(tmp_path) not in rendered


def test_execute_scopes_session_memory_child_with_project_and_provider(tmp_path):
    ledger = tmp_path / "ledger.sqlite3"
    outer_approval = tmp_path / "flow-approval.json"
    child_approval = tmp_path / "session-approval.json"
    command_argv = [
        "--ledger",
        str(ledger),
        "--execute",
        "--approval",
        str(outer_approval),
        "--session-memory-approval",
        str(child_approval),
        "--project",
        "neurons",
        "--provider",
        "codex",
    ]
    _approval(outer_approval, argv=command_argv)
    calls: list[list[str]] = []

    def _session(argv):  # type: ignore[no-untyped-def]
        calls.append(list(argv or []))
        print(json.dumps({"schema_version": "child.session", "projected": 1}))
        return 0

    def _graph(argv):  # type: ignore[no-untyped-def]
        print(json.dumps({"schema_version": "child.graph", "projection": {"projected": 1}}))
        return 0

    run_migration_flow(
        ledger_path=ledger,
        limit=1,
        project="neurons",
        provider="codex",
        execute=True,
        approval=outer_approval,
        session_memory_approval=child_approval,
        command_argv=command_argv,
        session_memory_main=_session,
        graph_main=_graph,
    )

    argv = calls[0]
    assert "--project" in argv and argv[argv.index("--project") + 1] == "neurons"
    assert "--provider" in argv and argv[argv.index("--provider") + 1] == "codex"


def _exploding_child(argv):  # type: ignore[no-untyped-def]
    _ = argv
    raise AssertionError("child command should not be called")
