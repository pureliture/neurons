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
        "transcript-memory-gc",
        "transcript-volume-gc",
        "session-memory-quarantine-terminal-skipped",
        "session-memory-repair-zombie-snapshots",
    ):
        assert command in COMMAND_HANDLERS
        assert command in output


def test_neuron_knowledge_boundary_is_server_owned(capsys):
    assert main(["--show-boundary"]) == 0
    assert capsys.readouterr().out.strip() == BOUNDARY


def test_neuron_knowledge_rejects_dendrite_command(capsys):
    assert main(["capture", "--help"]) == 2
    assert "unknown neurons command: capture" in capsys.readouterr().err


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


def test_neuron_knowledge_session_memory_gc_execute_fails_closed(tmp_path, capsys):
    rc = main(
        [
            "session-memory-gc",
            "--ledger",
            str(tmp_path / "ledger.sqlite3"),
            "--dataset-id",
            "ds_session",
            "--ragflow-url",
            "http://127.0.0.1:19380",
            "--execute",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "blocked_live_execution"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_neuron_knowledge_transcript_memory_gc_execute_disable_fails_closed(tmp_path, capsys):
    rc = main(
        [
            "transcript-memory-gc",
            "--ledger",
            str(tmp_path / "ledger.sqlite3"),
            "--dataset-id",
            "ds_transcript",
            "--ragflow-url",
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


def test_neuron_knowledge_transcript_volume_gc_execute_fails_closed(tmp_path, capsys):
    rc = main(
        [
            "transcript-volume-gc",
            "--ledger",
            str(tmp_path / "ledger.sqlite3"),
            "--transcript-dataset-id",
            "ds_transcript",
            "--ragflow-url",
            "http://127.0.0.1:19380",
            "--execute",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "blocked_live_execution"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
