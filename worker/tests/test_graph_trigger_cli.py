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
        print(
            json.dumps(
                {
                    "schema_version": "child.graph",
                    "status": "ok",
                    "projection": {"projected": 3, "duplicates": 0, "failed": 0},
                }
            )
        )
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
    # hot path is episode-only by default: no per-session Graphiti entity extraction.
    assert "--extract-entities" not in argv
    assert ["--limit", "3"] == argv[2:4]
    assert "neurons" not in json.dumps(report, sort_keys=True)


def test_graph_trigger_extract_entities_opt_in_adds_flag(tmp_path):
    calls: list[list[str]] = []

    def _child(argv):  # type: ignore[no-untyped-def]
        calls.append(list(argv or []))
        print(json.dumps({"schema_version": "child.graph", "status": "ok"}))
        return 0

    report = run_graph_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        execute=True,
        extract_entities=True,
        graph_main=_child,
    )

    assert report["status"] == "ok"
    argv = calls[0]
    # debug/manual opt-in restores per-session entity extraction.
    assert "--extract-entities" in argv


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


def test_graph_trigger_partial_projection_then_failure_still_reports_side_effects(tmp_path):
    # 일부 projected 후 child가 실패(status=failed, exit 1)해도 그래프 기록 신호가
    # 있으면 mutation/network를 거짓 음성으로 숨기지 않는다.
    def _child(argv):  # type: ignore[no-untyped-def]
        _ = argv
        print(
            json.dumps(
                {
                    "schema_version": "child.graph",
                    "status": "failed",
                    "projection": {"projected": 1, "duplicates": 0, "failed": 2},
                }
            )
        )
        return 1

    report = run_graph_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        execute=True,
        graph_main=_child,
    )

    assert report["status"] == "failed"
    assert report["mutation_performed"] is True
    assert report["network_used"] is True


def test_graph_trigger_ok_with_zero_projection_reports_no_mutation(tmp_path):
    # status=ok이지만 projected=0이면 mutation을 거짓 양성으로 보고하지 않는다.
    # duplicates만 있으면 그래프 백엔드에 접촉했으므로 network는 True다.
    def _child(argv):  # type: ignore[no-untyped-def]
        _ = argv
        print(
            json.dumps(
                {
                    "schema_version": "child.graph",
                    "status": "ok",
                    "projection": {"projected": 0, "duplicates": 4, "failed": 0},
                }
            )
        )
        return 0

    report = run_graph_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        execute=True,
        graph_main=_child,
    )

    assert report["status"] == "ok"
    assert report["mutation_performed"] is False
    assert report["network_used"] is True


def _exploding_child(argv):  # type: ignore[no-untyped-def]
    _ = argv
    raise AssertionError("child command should not be called")
