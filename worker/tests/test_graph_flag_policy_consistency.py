"""All three brain entrypoints must turn --enable-graph/--graph-required into
the same build_graph_adapter_from_env policy:

- --enable-graph  => best-effort (enable_flag truthy, required_flag False)
- --graph-required => must-have  (required_flag True)

This locks the seam against the prior drift where projection_cli forced
required = graph_required or enable_graph while the other entrypoints used
required = bool(graph_required).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from agent_knowledge.cli import main as neuron_main
from agent_knowledge.ledger import Ledger
from agent_knowledge.llm_brain_core.graph import FakeGraphMemoryAdapter


def _capture(monkeypatch, module_path: str) -> list[dict]:
    captured: list[dict] = []

    def _fake(**kwargs):
        captured.append(dict(kwargs))
        return FakeGraphMemoryAdapter()

    monkeypatch.setattr(f"{module_path}.build_graph_adapter_from_env", _fake)
    return captured


def _normalized(kwargs: dict) -> tuple[bool, bool]:
    enable = kwargs.get("enable_flag", kwargs.get("enabled"))
    required = bool(kwargs.get("required_flag", kwargs.get("required", False)))
    return (bool(enable), required)


def _run_projection_cli(tmp_path: Path, monkeypatch, *, enable: bool, required: bool) -> dict:
    captured = _capture(monkeypatch, "agent_knowledge.llm_brain_core.projection_cli")
    ledger_path = tmp_path / "proj-ledger.sqlite3"
    Ledger(ledger_path)
    argv = ["brain-project", "--ledger", str(ledger_path), "--project", "neurons"]
    if enable:
        argv.append("--enable-graph")
    if required:
        argv.append("--graph-required")
    neuron_main(argv)
    assert captured, "projection_cli did not build a graph adapter"
    return captured[-1]


def _run_context_resolve_cli(tmp_path: Path, monkeypatch, *, enable: bool, required: bool) -> dict:
    captured = _capture(monkeypatch, "agent_knowledge.llm_brain_core.cli")
    ledger_path = tmp_path / "ctx-ledger.sqlite3"
    Ledger(ledger_path)
    argv = [
        "brain-context-resolve",
        "--ledger",
        str(ledger_path),
        "--project",
        "neurons",
        "--repository",
        "neurons",
        "--branch",
        "main",
        "--current-request",
        "graph flag policy",
    ]
    if enable:
        argv.append("--enable-graph")
    if required:
        argv.append("--graph-required")
    neuron_main(argv)
    assert captured, "brain-context-resolve did not build a graph adapter"
    return captured[-1]


def _run_mcp_stdio(tmp_path: Path, monkeypatch, *, enable: bool, required: bool) -> dict:
    captured = _capture(monkeypatch, "agent_knowledge.cli")
    ledger_path = tmp_path / "mcp-ledger.sqlite3"
    Ledger(ledger_path)
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request) + "\n"))
    argv = ["mcp-stdio", "--ledger", str(ledger_path), "--dataset-id", "ds"]
    if enable:
        argv.append("--enable-graph")
    if required:
        argv.append("--graph-required")
    neuron_main(argv)
    assert captured, "mcp-stdio did not build a graph adapter"
    return captured[-1]


def test_enable_graph_is_best_effort_across_all_entrypoints(tmp_path: Path, monkeypatch):
    proj = _normalized(_run_projection_cli(tmp_path, monkeypatch, enable=True, required=False))
    ctx = _normalized(_run_context_resolve_cli(tmp_path, monkeypatch, enable=True, required=False))
    mcp = _normalized(_run_mcp_stdio(tmp_path, monkeypatch, enable=True, required=False))

    # best-effort: enabled True, required False — identical across all three.
    assert proj == ctx == mcp == (True, False)


def test_graph_required_is_must_have_across_all_entrypoints(tmp_path: Path, monkeypatch):
    proj = _normalized(_run_projection_cli(tmp_path, monkeypatch, enable=True, required=True))
    ctx = _normalized(_run_context_resolve_cli(tmp_path, monkeypatch, enable=True, required=True))
    mcp = _normalized(_run_mcp_stdio(tmp_path, monkeypatch, enable=True, required=True))

    # must-have: required True — identical across all three.
    assert proj == ctx == mcp
    assert proj[1] is True


def test_enable_graph_alone_never_forces_required(tmp_path: Path, monkeypatch):
    # The prior projection_cli bug forced required = graph_required or enable_graph.
    # --enable-graph without --graph-required must stay best-effort everywhere.
    proj = _normalized(_run_projection_cli(tmp_path, monkeypatch, enable=True, required=False))
    ctx = _normalized(_run_context_resolve_cli(tmp_path, monkeypatch, enable=True, required=False))
    mcp = _normalized(_run_mcp_stdio(tmp_path, monkeypatch, enable=True, required=False))

    assert proj[1] is False
    assert ctx[1] is False
    assert mcp[1] is False
