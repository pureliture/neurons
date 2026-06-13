from __future__ import annotations

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
