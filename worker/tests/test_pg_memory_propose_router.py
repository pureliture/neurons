"""Atlas-owned routing proof; no model, database, or network access."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from agent_knowledge import cli
from agent_knowledge.session_memory import candidate_intake_cli as intake
from agent_knowledge.session_memory import extraction_llm


def test_pg_memory_propose_is_lazy_and_preserves_argv_and_exit(monkeypatch):
    calls = []

    def fake_import(module_name, package=None):
        calls.append((module_name, package))
        return SimpleNamespace(main=lambda argv: 7 if argv == ["--sentinel"] else 99)

    monkeypatch.setattr(cli, "import_module", fake_import)
    assert cli.main(["pg-memory-propose", "--sentinel"]) == 7
    assert calls == [(".session_memory.candidate_intake_cli", "agent_knowledge")]


def test_router_help_advertises_pg_memory_propose(capsys):
    assert cli.main(["--help"]) == 0
    output = capsys.readouterr().out
    assert "pg-memory-propose" in output


@pytest.mark.parametrize(
    "argv,status,code",
    [
        ([], "dry_run", 0),
        (["--enable"], "dry_run", 0),
        (["--mode", "dry-run", "--enable"], "dry_run", 0),
        (["--mode", "once"], "disabled", 0),
        (["--mode", "continuous"], "continuous_mode_not_enabled", 2),
        (["--mode", "continuous", "--enable"], "continuous_mode_not_enabled", 2),
    ],
)
def test_router_disabled_paths_never_build_model_store_or_checkpoint(
    argv, status, code, monkeypatch, tmp_path, capsys
):
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled path attempted an external operation")

    monkeypatch.setattr(extraction_llm, "build_vertex_wrapper_completion_fn", forbidden)
    monkeypatch.setattr(intake, "_build_store_from_env", forbidden)
    monkeypatch.setattr(intake, "save_checkpoint", forbidden)
    source = tmp_path / "redacted-source.json"
    source.write_text("[]", encoding="utf-8")
    checkpoint = tmp_path / "checkpoint.json"
    args = ["pg-memory-propose", "--project", "router-proof", "--source-json", str(source),
            "--checkpoint", str(checkpoint), *argv]
    assert cli.main(args) == code
    result = json.loads(capsys.readouterr().out)
    assert result.get("status", result.get("error_code")) == status
    assert not checkpoint.exists()


def test_router_proposal_help_is_dispatched(capsys):
    assert cli.main(["pg-memory-propose", "--help"]) == 0
    output = capsys.readouterr().out
    assert "--enable" in output
    assert "--source-json" in output
    assert "--checkpoint" in output
