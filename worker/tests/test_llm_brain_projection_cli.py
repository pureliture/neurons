from __future__ import annotations

import json
from pathlib import Path

from agent_knowledge.cli import main as neuron_main
from agent_knowledge.ledger import Ledger
from agent_knowledge.llm_brain_core import BrainReadService, FakeGraphMemoryAdapter
from agent_knowledge.llm_brain_core.ledger_adapter import LedgerSourceRefCatalog


PROJECT = "neurons"


def test_brain_project_imports_dendrite_source_refs_projects_graph_and_contextpack(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    ledger_path = tmp_path / "ledger.sqlite3"
    public_jsonl = tmp_path / "dendrite-source-public.jsonl"
    public_jsonl.write_text(
        json.dumps(
            {
                "source_ref_id": "src_dendrite_projects_bootstrap",
                "device_id_hash": _h("device-a"),
                "root_id": "projects",
                "relative_path_hash": _h("neurons/worker/lib/agent_knowledge/mcp_server.py"),
                "content_hash": _h("source-content"),
                "mtime": "2026-06-19T00:00:00+00:00",
                "size": 120,
                "sync_policy": "derived_only",
                "permission_scope": "project",
                "last_seen_at": "2026-06-19T00:00:00+00:00",
                "derived_summary": "Brain MCP SourceRef bootstrap evidence for projects folder.",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    graph = FakeGraphMemoryAdapter()
    monkeypatch.setattr("agent_knowledge.llm_brain_core.projection_cli.build_graph_adapter_from_env", lambda **kwargs: graph)

    rc = neuron_main(
        [
            "brain-project",
            "--ledger",
            str(ledger_path),
            "--project",
            PROJECT,
            "--source-ref-jsonl",
            str(public_jsonl),
            "--enable-graph",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert report["schema_version"] == "llm_brain_projection.v1"
    assert report["status"] == "ok"
    assert report["source_refs_imported"] == 1
    assert report["canonical_counts"]["source_refs"] == 1
    assert report["projection"]["projected"] == 1
    assert report["raw_paths_printed"] is False

    catalog = LedgerSourceRefCatalog(Ledger(ledger_path))
    record = catalog.get("src_dendrite_projects_bootstrap")
    assert record is not None
    assert record.derived_summary == "Brain MCP SourceRef bootstrap evidence for projects folder."

    pack = BrainReadService(graph_adapter=graph).brain_context_resolve(
        repository="neurons",
        branch="codex/m14",
        current_files=[],
        current_request="SourceRef bootstrap projects folder",
        project=PROJECT,
    ).to_dict()

    assert pack["graph_status"]["status"] == "available"
    assert pack["source_refs"] == [{"source_ref_id": "src_dendrite_projects_bootstrap"}]
    assert "/Users/" not in json.dumps(report | {"pack": pack}, sort_keys=True)


def test_brain_project_reports_source_ref_import_failures_without_projecting_bad_lines(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    ledger_path = tmp_path / "ledger.sqlite3"
    public_jsonl = tmp_path / "bad-source-public.jsonl"
    public_jsonl.write_text("{}\n", encoding="utf-8")
    graph = FakeGraphMemoryAdapter()
    monkeypatch.setattr("agent_knowledge.llm_brain_core.projection_cli.build_graph_adapter_from_env", lambda **kwargs: graph)

    rc = neuron_main(
        [
            "brain-project",
            "--ledger",
            str(ledger_path),
            "--project",
            PROJECT,
            "--source-ref-jsonl",
            str(public_jsonl),
            "--enable-graph",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert report["status"] == "failed"
    assert report["source_refs_imported"] == 0
    assert report["source_ref_import_failures"][0]["line"] == 1
    assert report["projection"]["projected"] == 0


def _h(value: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()
