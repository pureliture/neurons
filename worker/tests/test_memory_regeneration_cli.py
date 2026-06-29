from __future__ import annotations

import json

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.memory_regeneration_cli import main
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession, TranscriptTurn

PROJECT = "workspace-index-advisor"
PROVIDER = "codex"


def _ledger_with_turns(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite3")
    session = TranscriptSession(
        session_id_hash="sha256:cli-session",
        provider=PROVIDER,
        project=PROJECT,
        started_at="2026-06-13T10:00:00+09:00",
        ended_at="2026-06-13T10:03:00+09:00",
        source_status="indexed_transcript_memory",
        source_locator_hash="sha256:cli-source",
    )
    ledger.upsert_transcript_session(session)
    for turn_index, role, text in (
        (1, "user", "Need project memory moved to neurons."),
        (2, "assistant", "Plan dry-run CLI first and keep live sync blocked."),
    ):
        ledger.upsert_transcript_turn(
            TranscriptTurn(
                turn_id_hash=f"sha256:cli-turn-{turn_index}",
                session_id_hash=session.session_id_hash,
                turn_index=turn_index,
                role=role,
                observed_at=f"2026-06-13T10:0{turn_index}:00+09:00",
                redacted_text=text,
            )
        )
    chunk = TranscriptChunk(
        chunk_id="chunk_cli_indexed",
        session_id_hash=session.session_id_hash,
        provider=session.provider,
        project=session.project,
        turn_start_index=1,
        turn_end_index=2,
        redacted_text="user: Need project memory moved to neurons.\nassistant: Plan dry-run CLI first.",
        content_hash="sha256:cli-indexed-chunk",
        source_status=session.source_status,
    )
    row = ledger.upsert_transcript_chunk(knowledge_id="kn_cli_indexed", chunk=chunk)
    ledger.mark_uploaded(row["knowledge_id"], dataset_id="ds_transcript", document_id="doc_transcript", run="DONE")
    ledger.mark_indexed(row["knowledge_id"], run="DONE")
    return ledger, session


def test_memory_regeneration_cli_run_project_memory_dry_run(tmp_path, capsys):
    ledger, _session = _ledger_with_turns(tmp_path)

    rc = main([
        "run",
        "--output",
        "project-memory",
        "--ledger",
        str(ledger.path),
        "--project",
        PROJECT,
        "--provider",
        PROVIDER,
        "--dry-run",
    ])

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["mode"] == "dry_run"
    assert report["datasetRole"] == "project-memory"
    assert report["projects_seen"] == 1
    assert report["snapshots_planned"] == 1
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_memory_regeneration_cli_build_session_memory_dry_run(tmp_path, capsys):
    ledger, session = _ledger_with_turns(tmp_path)

    rc = main([
        "memory-regeneration",
        "build-session-memory",
        "--ledger",
        str(ledger.path),
        "--project",
        PROJECT,
        "--provider",
        PROVIDER,
        "--session-id-hash",
        session.session_id_hash,
    ])

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["mode"] == "dry_run"
    assert report["datasetRole"] == "session-memory"
    assert report["sessions_seen"] == 1
    assert report["memory_documents_planned"] == 1
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_memory_regeneration_cli_process_dirty_project_memory_dry_run(tmp_path, capsys):
    ledger, _session = _ledger_with_turns(tmp_path)
    ledger.mark_project_memory_dirty(
        provider=PROVIDER,
        project=PROJECT,
        reason="test_dirty_project_memory",
    )

    rc = main([
        "process-dirty",
        "--output",
        "project-memory",
        "--ledger",
        str(ledger.path),
        "--quiet-period-seconds",
        "0",
    ])

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["mode"] == "dry_run"
    assert report["dirty_projects_seen"] == 1
    assert report["processed_count"] == 1
    assert report["mutation_performed"] is False


def test_memory_regeneration_cli_live_enqueue_is_fail_closed(tmp_path, capsys):
    ledger, _session = _ledger_with_turns(tmp_path)

    rc = main([
        "run",
        "--output",
        "project-memory",
        "--ledger",
        str(ledger.path),
        "--enqueue",
        "--ingress-url",
        "http://127.0.0.1:18080",
    ])

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "blocked_live_execution"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
