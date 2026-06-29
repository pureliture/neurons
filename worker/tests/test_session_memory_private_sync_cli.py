from __future__ import annotations

import json
from pathlib import Path

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.session_memory_private_sync_cli import main
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession, TranscriptTurn

PROJECT = "workspace-index-advisor"
PROVIDER = "codex"


def _ledger_with_dirty_session(tmp_path: Path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite3")
    session = TranscriptSession(
        session_id_hash="sha256:private-sync-session",
        provider=PROVIDER,
        project=PROJECT,
        started_at="2026-06-13T11:00:00+09:00",
        ended_at="2026-06-13T11:03:00+09:00",
        source_status="indexed_transcript_memory",
        source_locator_hash="sha256:private-sync-source",
    )
    ledger.upsert_transcript_session(session)
    for turn_index, role, text in (
        (1, "user", "Move session memory sync ownership to neurons."),
        (2, "assistant", "Keep live sync blocked until the server runtime lane exists."),
    ):
        ledger.upsert_transcript_turn(
            TranscriptTurn(
                turn_id_hash=f"sha256:private-sync-turn-{turn_index}",
                session_id_hash=session.session_id_hash,
                turn_index=turn_index,
                role=role,
                observed_at=f"2026-06-13T11:0{turn_index}:00+09:00",
                redacted_text=text,
            )
        )
    chunk = TranscriptChunk(
        chunk_id="chunk_private_sync_indexed",
        session_id_hash=session.session_id_hash,
        provider=session.provider,
        project=session.project,
        turn_start_index=1,
        turn_end_index=2,
        redacted_text=(
            "user: Move session memory sync ownership to neurons.\n"
            "assistant: Keep live sync blocked until the server runtime lane exists."
        ),
        content_hash="sha256:private-sync-indexed-chunk",
        source_status=session.source_status,
    )
    row = ledger.upsert_transcript_chunk(knowledge_id="kn_private_sync_indexed", chunk=chunk)
    ledger.mark_uploaded(row["knowledge_id"], dataset_id="ds_transcript", document_id="doc_transcript", run="DONE")
    ledger.mark_indexed(row["knowledge_id"], run="DONE")
    ledger.mark_session_memory_dirty(
        session_id_hash=session.session_id_hash,
        provider=PROVIDER,
        project=PROJECT,
        reason="test_private_sync_dry_run",
        source_knowledge_id=row["knowledge_id"],
    )
    return ledger, session


def test_session_memory_private_sync_dry_run_plans_dirty_sessions(tmp_path, capsys):
    ledger, _session = _ledger_with_dirty_session(tmp_path)

    rc = main([
        "session-memory-private-sync",
        "--dry-run",
        "--ledger",
        str(ledger.path),
        "--quiet-period-seconds",
        "0",
    ])

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["status"] == "dry_run_complete"
    assert report["mode"] == "dry_run"
    assert report["dirty_sessions_seen"] == 1
    assert report["sessions_seen"] == 1
    assert report["memory_documents_planned"] == 1
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
    assert report["raw_paths_printed"] is False
    assert report["candidate_reports"][0]["session_id_fragment"] == "private-sync"


def test_session_memory_private_sync_legacy_live_invocation_is_blocked(capsys):
    rc = main([
        "--ledger",
        "/tmp/ledger.sqlite3",
        "--retired-index-bridge-url",
        "http://127.0.0.1:19380",
        "--retired-index-bridge-token-env",
        "RETIRED_INDEX_BRIDGE_API_KEY",
        "--approval",
        "/tmp/approval.json",
    ])

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "blocked_retired_legacy_entrypoint"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
    assert report["index_write_performed"] is False


def test_session_memory_private_sync_dry_run_rejects_live_arguments(tmp_path, capsys):
    ledger, _session = _ledger_with_dirty_session(tmp_path)

    rc = main([
        "--dry-run",
        "--ledger",
        str(ledger.path),
        "--retired-index-bridge-url",
        "http://127.0.0.1:19380",
    ])

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "blocked_live_execution"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_session_memory_private_sync_cli_has_no_live_client_imports():
    source = Path("lib/agent_knowledge/session_memory/session_memory_private_sync_cli.py").read_text(encoding="utf-8")
    for forbidden in (
        "RetiredIndexBridgeHttpClient",
        "os.environ",
        "upload_document",
        "request_parse",
        "urlopen",
        "requests.",
    ):
        assert forbidden not in source
