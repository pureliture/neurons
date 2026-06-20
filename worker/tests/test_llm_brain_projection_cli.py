from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_knowledge.cli import main as neuron_main
from agent_knowledge.ledger import Ledger
from agent_knowledge.llm_brain_core import BrainReadService, FakeGraphMemoryAdapter
from agent_knowledge.llm_brain_core.ledger_adapter import LedgerSourceRefCatalog
from agent_knowledge.llm_brain_core.runtime import source_ref_from_catalog_event


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
    LedgerSourceRefCatalog(Ledger(ledger_path)).register(
        source_ref_from_catalog_event(
            {
                "source_ref_id": "src_other_project_preexisting",
                "device_id_hash": _h("device-a"),
                "root_id": "documents",
                "relative_path_hash": _h("other/project.md"),
                "content_hash": _h("other-source-content"),
                "mtime": "2026-06-19T00:00:00+00:00",
                "size": 64,
                "sync_policy": "derived_only",
                "derived_summary": "Other project SourceRef must not be projected into neurons.",
            }
        )
    )
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
    assert graph.search_context(
        brain_id="/project/neurons",
        query="Other documents",
        entity_types=["SourceRef"],
    ).episodes == ()
    assert "/Users/" not in json.dumps(report | {"pack": pack}, sort_keys=True)


def test_brain_project_signals_memory_card_truncation_at_limit_boundary(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    # More accepted cards than the --limit window: the run re-projects only the
    # newest `--limit` cards, so the report must flag truncated.memory_cards so a
    # partial-window run is not mistaken for full-project coverage.
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    for index in range(3):
        ledger.upsert_llm_brain_memory_card(_accepted_task_card(f"mem_truncate_{index}", index))
    graph = FakeGraphMemoryAdapter()
    monkeypatch.setattr(
        "agent_knowledge.llm_brain_core.projection_cli.build_graph_adapter_from_env",
        lambda **kwargs: graph,
    )

    rc = neuron_main(
        [
            "brain-project",
            "--ledger",
            str(tmp_path / "ledger.sqlite3"),
            "--project",
            PROJECT,
            "--limit",
            "2",
            "--skip-source-refs",
            "--enable-graph",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert report["limit"] == 2
    assert report["canonical_counts"]["memory_cards"] == 2
    assert report["truncated"]["memory_cards"] is True
    assert report["truncated"]["any"] is True


def test_brain_project_does_not_flag_truncation_below_limit(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.upsert_llm_brain_memory_card(_accepted_task_card("mem_one", 0))
    graph = FakeGraphMemoryAdapter()
    monkeypatch.setattr(
        "agent_knowledge.llm_brain_core.projection_cli.build_graph_adapter_from_env",
        lambda **kwargs: graph,
    )

    rc = neuron_main(
        [
            "brain-project",
            "--ledger",
            str(tmp_path / "ledger.sqlite3"),
            "--project",
            PROJECT,
            "--limit",
            "10",
            "--skip-source-refs",
            "--enable-graph",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert report["canonical_counts"]["memory_cards"] == 1
    assert report["truncated"]["memory_cards"] is False
    assert report["truncated"]["any"] is False


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
    assert report["projection"]["attempted"] == 0
    assert report["projection"]["projected"] == 0


def test_brain_project_source_ref_import_is_all_or_nothing_on_partial_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    # A JSONL whose first line is valid and second line is malformed must NOT
    # leave the first (valid) record committed: a partial import would survive
    # across re-runs and silently widen recall scope. The whole import rolls back.
    ledger_path = tmp_path / "ledger.sqlite3"
    mixed_jsonl = tmp_path / "mixed-source-public.jsonl"
    valid_line = json.dumps(
        {
            "source_ref_id": "src_valid_first_line",
            "device_id_hash": _h("device-a"),
            "root_id": "projects",
            "relative_path_hash": _h("neurons/worker/lib/x.py"),
            "content_hash": _h("source-content"),
            "mtime": "2026-06-19T00:00:00+00:00",
            "size": 10,
            "sync_policy": "derived_only",
            "last_seen_at": "2026-06-19T00:00:00+00:00",
            "derived_summary": "Valid first line that must not be partially committed.",
        },
        sort_keys=True,
    )
    mixed_jsonl.write_text(valid_line + "\n" + "{}\n", encoding="utf-8")
    graph = FakeGraphMemoryAdapter()
    monkeypatch.setattr(
        "agent_knowledge.llm_brain_core.projection_cli.build_graph_adapter_from_env",
        lambda **kwargs: graph,
    )

    rc = neuron_main(
        [
            "brain-project",
            "--ledger",
            str(ledger_path),
            "--project",
            PROJECT,
            "--source-ref-jsonl",
            str(mixed_jsonl),
            "--enable-graph",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert report["status"] == "failed"
    assert report["source_refs_imported"] == 0
    assert report["source_ref_import_failures"][0]["line"] == 2
    # The valid first line must not have been registered: all-or-nothing.
    catalog = LedgerSourceRefCatalog(Ledger(ledger_path))
    assert catalog.get("src_valid_first_line") is None
    assert catalog.list_all() == []


def test_brain_project_resume_skips_listed_episode_ids(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    # A resume file of already-projected episode_ids makes the re-run skip those
    # episodes (counted as skipped_resumed) instead of re-projecting the window.
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.upsert_llm_brain_memory_card(_accepted_task_card("mem_resume_cli", 0))
    graph = FakeGraphMemoryAdapter()
    monkeypatch.setattr(
        "agent_knowledge.llm_brain_core.projection_cli.build_graph_adapter_from_env",
        lambda **kwargs: graph,
    )

    rc_first = neuron_main(
        [
            "brain-project",
            "--ledger",
            str(tmp_path / "ledger.sqlite3"),
            "--project",
            PROJECT,
            "--skip-source-refs",
            "--enable-graph",
        ]
    )
    first = json.loads(capsys.readouterr().out)
    assert rc_first == 0
    assert first["projection"]["projected"] == 1
    projected_id = first["projection"]["episode_ids"][0]

    resume_file = tmp_path / "projected.txt"
    resume_file.write_text(projected_id + "\n", encoding="utf-8")

    rc_second = neuron_main(
        [
            "brain-project",
            "--ledger",
            str(tmp_path / "ledger.sqlite3"),
            "--project",
            PROJECT,
            "--skip-source-refs",
            "--enable-graph",
            "--resume-projected-ids",
            str(resume_file),
        ]
    )
    second = json.loads(capsys.readouterr().out)

    assert rc_second == 0
    assert second["projection"]["skipped_resumed"] == 1
    assert second["projection"]["projected"] == 0


def test_register_all_rolls_back_whole_batch_on_mid_batch_write_error(tmp_path: Path):
    # register_all must commit the batch as one transaction: a write error on a
    # later record rolls back the earlier ones, so no partial catalog survives.
    ledger_path = tmp_path / "ledger.sqlite3"
    catalog = LedgerSourceRefCatalog(Ledger(ledger_path))
    records = [
        source_ref_from_catalog_event(
            {
                "source_ref_id": f"src_batch_{index}",
                "device_id_hash": _h("device-a"),
                "root_id": "projects",
                "relative_path_hash": _h(f"file-{index}.py"),
                "content_hash": _h(f"content-{index}"),
                "size": 1,
                "sync_policy": "derived_only",
                "last_seen_at": "2026-06-19T00:00:00+00:00",
            }
        )
        for index in range(2)
    ]

    original = catalog._register_on_connection
    calls = {"n": 0}

    def _fail_on_second(connection, record):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated write failure on second record")
        return original(connection, record)

    catalog._register_on_connection = _fail_on_second  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="simulated write failure"):
            catalog.register_all(records)
    finally:
        catalog._register_on_connection = original  # type: ignore[assignment]

    fresh = LedgerSourceRefCatalog(Ledger(ledger_path))
    assert fresh.get("src_batch_0") is None
    assert fresh.list_all() == []


def test_brain_project_reports_unreadable_source_ref_file_without_crashing(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    ledger_path = tmp_path / "ledger.sqlite3"
    missing_jsonl = tmp_path / "missing-source-public.jsonl"
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
            str(missing_jsonl),
            "--enable-graph",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert report["status"] == "failed"
    assert report["source_refs_imported"] == 0
    assert report["source_ref_import_failures"][0] == {
        "source": "missing-source-public.jsonl",
        "line": 0,
        "reason_code": "FileNotFoundError",
    }
    assert report["projection"]["attempted"] == 0


def test_brain_project_failure_output_does_not_print_raw_exception_details(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    def _raise(**kwargs):
        _ = kwargs
        raise RuntimeError("/Users/example/private TOKEN=secret")

    monkeypatch.setattr("agent_knowledge.llm_brain_core.projection_cli.run_projection", _raise)

    rc = neuron_main(
        [
            "brain-project",
            "--ledger",
            str(tmp_path / "ledger.sqlite3"),
            "--project",
            PROJECT,
            "--enable-graph",
        ]
    )
    stderr = capsys.readouterr().err
    report = json.loads(stderr)

    assert rc == 1
    assert report["error_class"] == "RuntimeError"
    assert report["message"] == "projection failed"
    assert "/Users/" not in stderr
    assert "TOKEN" not in stderr


def _accepted_task_card(memory_id: str, index: int) -> dict:
    summary = f"Truncation fixture task {index}"
    return {
        "memory_id": memory_id,
        "brain_id": f"/project/{PROJECT}",
        "card_type": "task",
        "scope": "project",
        "project": PROJECT,
        "provider": "claude",
        "title": summary,
        "summary": summary,
        "render_text": summary,
        "lifecycle_state": "accepted",
        "judgment_state": "none",
        "status": "accepted",
        "approval_state": "approved",
        "governance_tier": "medium",
        "freshness": "current",
        "currentness": "current",
        "confidence": 0.9,
        "confidence_basis": "projection cli truncation fixture",
        "source_refs": [{"source_ref_id": "src_truncation_fixture", "content_hash": _h("source-content")}],
        "evidence_refs": [],
        "evidence_hashes": [_h(memory_id)],
        "derived_from": [],
        "supersedes": [],
        "superseded_by": [],
        "conflicts": [],
        "active_until": "",
        "updated_at": f"2026-06-19T00:0{index}:00Z",
        "typed_payload": {
            "task_state": summary,
            "next_action": "Project this card",
            "blocker": "",
            "owner_hint": "neurons",
            "status": "open",
        },
    }


def _h(value: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()
