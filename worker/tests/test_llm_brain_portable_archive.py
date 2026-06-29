from __future__ import annotations

import json
import os
import tarfile
from pathlib import Path

from agent_knowledge.cli import main as neuron_main
from agent_knowledge.ledger import Ledger
from agent_knowledge.llm_brain_core.ledger_adapter import (
    LedgerSessionMemoryArtifactStore,
    LedgerSourceRefCatalog,
)
from agent_knowledge.llm_brain_core.models import SessionMemoryArtifact, SourceRefRecord
from agent_knowledge.llm_brain_core import portable as portable_module
from agent_knowledge.llm_brain_core.portable import export_llm_brain_archive, import_llm_brain_archive


def test_llm_brain_portable_archive_roundtrips_allowlisted_memory(tmp_path: Path):
    source_ledger = tmp_path / "source.sqlite3"
    target_ledger = tmp_path / "target.sqlite3"
    archive = tmp_path / "brain.tar.gz"
    _seed_ledger(source_ledger)

    export_report = export_llm_brain_archive(
        ledger_path=source_ledger,
        output_path=archive,
        repo_root=Path(__file__).resolve().parents[2],
    ).to_dict()
    import_report = import_llm_brain_archive(ledger_path=target_ledger, archive_path=archive).to_dict()

    target = Ledger(target_ledger)
    assert export_report["status"] == "exported"
    assert export_report["counts"] == {
        "memory_cards": 1,
        "session_memory_artifacts": 1,
        "source_refs": 1,
    }
    assert import_report["status"] == "imported"
    assert target.get_llm_brain_memory_card("mem_portable_task")["summary"] == "Portable export task"
    assert LedgerSessionMemoryArtifactStore(target).list_recent(project="neurons", limit=5)[0].summary == "Portable artifact"
    assert LedgerSourceRefCatalog(target).get("src_portable") is not None


def test_llm_brain_portable_archive_has_manifest_and_no_raw_tables(tmp_path: Path):
    ledger_path = tmp_path / "source.sqlite3"
    archive = tmp_path / "brain.tar.gz"
    _seed_ledger(ledger_path)

    export_llm_brain_archive(
        ledger_path=ledger_path,
        output_path=archive,
        repo_root=Path(__file__).resolve().parents[2],
    )

    with tarfile.open(archive, "r:gz") as tar:
        names = set(tar.getnames())
        manifest = json.loads(tar.extractfile("manifest.json").read().decode("utf-8"))

    assert "data/llm_brain_memory_cards.jsonl" in names
    assert "data/llm_brain_session_memory_artifacts.jsonl" in names
    assert "data/llm_brain_source_refs.jsonl" in names
    assert "transcript_chunks.jsonl" not in names
    assert "index_documents.jsonl" not in names
    assert manifest["raw_tables_included"] is False
    assert manifest["graph_db_files_included"] is False


def test_llm_brain_portable_archive_specs_redact_private_workstation_paths(tmp_path: Path):
    ledger_path = tmp_path / "source.sqlite3"
    archive = tmp_path / "brain.tar.gz"
    _seed_ledger(ledger_path)

    export_llm_brain_archive(
        ledger_path=ledger_path,
        output_path=archive,
        repo_root=Path(__file__).resolve().parents[2],
    )

    with tarfile.open(archive, "r:gz") as tar:
        spec_members = [name for name in tar.getnames() if name.startswith("specs/llm-brain-core-v1/")]
        spec_blob = "\n".join(tar.extractfile(name).read().decode("utf-8") for name in spec_members)

    # exported specs must never leak the operator's real workstation home path.
    assert os.path.expanduser("~") not in spec_blob


def test_llm_brain_portable_extract_tar_zst_uses_real_temp_path(tmp_path: Path, monkeypatch):
    raw_tar = tmp_path / "raw.tar"
    archive = tmp_path / "brain.tar.zst"
    target = tmp_path / "out"
    archive.write_bytes(b"fake zstd payload")
    with tarfile.open(raw_tar, "w") as tar:
        payload = tmp_path / "manifest.json"
        payload.write_text('{"schema_version":"x"}\n', encoding="utf-8")
        tar.add(payload, arcname="manifest.json")
    tar_bytes = raw_tar.read_bytes()

    def fake_run(argv, *, check, stdout):
        assert argv[:4] == ["/usr/bin/zstd", "-q", "-d", "-c"]
        assert check is True
        stdout.write(tar_bytes)

    monkeypatch.setattr(portable_module.shutil, "which", lambda name: "/usr/bin/zstd" if name == "zstd" else None)
    monkeypatch.setattr(portable_module.subprocess, "run", fake_run)

    portable_module._extract_archive(archive, target)

    assert (target / "manifest.json").exists()


def test_llm_brain_portable_cli_export_import(tmp_path: Path, capsys):
    source_ledger = tmp_path / "source.sqlite3"
    target_ledger = tmp_path / "target.sqlite3"
    archive = tmp_path / "brain.tar.gz"
    _seed_ledger(source_ledger)

    export_rc = neuron_main(
        [
            "brain-export",
            "--ledger",
            str(source_ledger),
            "--out",
            str(archive),
            "--repo-root",
            str(Path(__file__).resolve().parents[2]),
        ]
    )
    export_output = json.loads(capsys.readouterr().out)
    import_rc = neuron_main(["brain-import", "--ledger", str(target_ledger), "--archive", str(archive)])
    import_output = json.loads(capsys.readouterr().out)

    assert export_rc == 0
    assert export_output["status"] == "exported"
    assert import_rc == 0
    assert import_output["counts"]["memory_cards"] == 1


def _seed_ledger(path: Path) -> None:
    ledger = Ledger(path)
    ledger.upsert_llm_brain_memory_card(
        {
            "memory_id": "mem_portable_task",
            "brain_id": "/project/neurons",
            "card_type": "task",
            "scope": "project",
            "project": "neurons",
            "provider": "codex",
            "title": "Portable export task",
            "summary": "Portable export task",
            "render_text": "Portable export task",
            "lifecycle_state": "accepted",
            "judgment_state": "none",
            "status": "accepted",
            "approval_state": "approved",
            "governance_tier": "medium",
            "freshness": "current",
            "currentness": "current",
            "confidence": 0.9,
            "confidence_basis": "portable fixture",
            "source_refs": [{"source_ref_id": "src_portable", "content_hash": _h("source")}],
            "evidence_refs": [],
            "evidence_hashes": [_h("mem_portable_task")],
            "derived_from": ["evt_portable"],
            "supersedes": [],
            "superseded_by": [],
            "conflicts": [],
            "active_until": "",
            "typed_payload": {
                "task_state": "Portable export task",
                "next_action": "Import archive on another PC",
                "blocker": "",
                "owner_hint": "neurons",
                "status": "open",
            },
        }
    )
    LedgerSessionMemoryArtifactStore(ledger).upsert(
        SessionMemoryArtifact.from_summary(
            session_id_hash=_h("session"),
            project="neurons",
            provider="codex",
            summary="Portable artifact",
            source_event_ids=["evt_portable"],
        )
    )
    LedgerSourceRefCatalog(ledger).register(
        SourceRefRecord(
            source_ref_id="src_portable",
            device_id_hash=_h("device-a"),
            root_id="project-root",
            relative_path_hash=_h("docs/design.md"),
            content_hash=_h("source"),
            mtime="2026-06-19T00:00:00Z",
            size=100,
            sync_policy="metadata_only",
        )
    )


def _h(value):
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()
