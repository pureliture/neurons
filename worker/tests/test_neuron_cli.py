from __future__ import annotations

import json

from agent_knowledge.cli import BOUNDARY, COMMAND_HANDLERS, main


def test_neuron_knowledge_help_lists_server_owned_commands(capsys):
    assert main(["--help"]) == 0
    output = capsys.readouterr().out
    assert "usage: neuron-knowledge" in output
    for command in (
        "rag-ingress-state",
        "memory-regeneration",
        "session-memory-private-sync",
        "neuron-session-memory-build",
        "native-memory-sync",
        "session-memory-gc",
        "transcript-backfill",
        "session-entry-recall",
        "transcript-resources",
        "transcript-quality",
        "transcript-retrieval",
        "transcript-migration",
        "transcript-memory-gc",
        "transcript-session-gc",
        "transcript-volume-gc",
        "backfill",
        "memory",
        "context-for-prompt",
        "mcp-stdio",
        "eval",
        "derived-memory-resources",
        "session-memory-quarantine-terminal-skipped",
        "session-memory-repair-zombie-snapshots",
        "brain-context-resolve",
        "brain-regression-gate",
        "couchdb-migration-flow",
        "couchdb-graph-trigger",
        "couchdb-graph-project",
        "couchdb-graph-status",
    ):
        assert command in COMMAND_HANDLERS
        assert command in output


def test_neuron_knowledge_boundary_is_server_owned(capsys):
    assert main(["--show-boundary"]) == 0
    assert capsys.readouterr().out.strip() == BOUNDARY


def test_neuron_knowledge_rejects_dendrite_command(capsys):
    assert main(["capture", "--help"]) == 2
    assert "unknown neurons command: capture" in capsys.readouterr().err


def test_neuron_knowledge_pending_server_command_fails_closed(capsys):
    assert main(["transcript-resources", "--help"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == "neuron_knowledge_pending_command.v1"
    assert report["status"] == "blocked_pending_server_extraction"
    assert report["command"] == "transcript-resources"
    assert report["destination"] == "neurons"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_neuron_knowledge_delegates_memory_regeneration_help(capsys):
    assert main(["memory-regeneration", "--help"]) == 0
    assert "usage: memory-regeneration" in capsys.readouterr().out


def test_neuron_knowledge_delegates_session_private_sync_help(capsys):
    assert main(["session-memory-private-sync", "--help"]) == 0
    assert "usage: session-memory-private-sync" in capsys.readouterr().out


def test_neuron_knowledge_memory_regeneration_live_args_fail_closed(tmp_path, capsys):
    rc = main(
        [
            "memory-regeneration",
            "run",
            "--output",
            "project-memory",
            "--ledger",
            str(tmp_path / "missing-ledger.sqlite3"),
            "--enqueue",
            "--ingress-url",
            "http://127.0.0.1:18080",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "blocked_live_execution"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_neuron_knowledge_native_memory_execute_fails_closed(tmp_path, capsys):
    rc = main(
        [
            "native-memory-sync",
            "--ledger",
            str(tmp_path / "missing-ledger.sqlite3"),
            "--native-memory-id",
            "mem_test",
            "--execute",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "blocked_live_execution"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_neuron_knowledge_session_memory_gc_execute_requires_approval(tmp_path, capsys):
    # GC executor는 벤더링됐지만 live --execute는 approval 게이트 뒤다. 유효 approval
    # 없이 --execute하면 네트워크/뮤테이션 전에 fail closed(approval error, rc!=0).
    rc = main(
        [
            "session-memory-gc",
            "--ledger",
            str(tmp_path / "ledger.sqlite3"),
            "--dataset-id",
            "ds_session",
            "--retired-index-bridge-url",
            "http://127.0.0.1:19380",
            "--execute",
        ]
    )

    captured = capsys.readouterr()
    assert rc != 0
    assert not captured.out  # fails closed: no GC report emitted without the full live contract (token+approval)


def test_neuron_knowledge_transcript_memory_gc_execute_disable_fails_closed(tmp_path, capsys):
    rc = main(
        [
            "transcript-memory-gc",
            "--ledger",
            str(tmp_path / "ledger.sqlite3"),
            "--dataset-id",
            "ds_transcript",
            "--retired-index-bridge-url",
            "http://127.0.0.1:19380",
            "--execute-disable",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "blocked_live_execution"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
    assert report["hard_delete_performed"] is False


def test_neuron_knowledge_delegates_transcript_backfill_help(capsys):
    assert main(["transcript-backfill", "--help"]) == 0
    assert "usage: transcript-backfill" in capsys.readouterr().out


def test_neuron_knowledge_transcript_volume_gc_execute_requires_approval(tmp_path, capsys):
    rc = main(
        [
            "transcript-volume-gc",
            "--ledger",
            str(tmp_path / "ledger.sqlite3"),
            "--transcript-dataset-id",
            "ds_transcript",
            "--retired-index-bridge-url",
            "http://127.0.0.1:19380",
            "--execute",
        ]
    )

    captured = capsys.readouterr()
    assert rc != 0
    assert not captured.out  # fails closed: no GC report emitted without the full live contract (token+approval)


def test_neuron_knowledge_transcript_session_gc_execute_requires_approval(tmp_path, capsys):
    rc = main(
        [
            "transcript-session-gc",
            "--transcript-dataset-id",
            "ds_transcript",
            "--session-memory-dataset-id",
            "ds_session_memory",
            "--retired-index-bridge-url",
            "http://127.0.0.1:19380",
            "--backup-dir",
            str(tmp_path / "backup"),
            "--execute",
        ]
    )

    captured = capsys.readouterr()
    assert rc != 0
    assert not captured.out  # fails closed: no GC report emitted without the full live contract (token+approval)
