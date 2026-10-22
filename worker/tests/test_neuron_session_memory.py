from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.neuron_session_memory import (
    main,
    read_recent_transcript_deliveries,
    read_watermark,
    write_watermark,
)


def _shadow_db(tmp_path: Path) -> Path:
    db = tmp_path / "ingest-state.sqlite"
    connection = sqlite3.connect(db)
    connection.execute(
        """CREATE TABLE shadow_ingest_log (
            idempotency_key TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
            document_kind TEXT NOT NULL, target_profile TEXT NOT NULL, status TEXT NOT NULL,
            dataset_ref TEXT DEFAULT '', document_ref TEXT DEFAULT '',
            delivered INTEGER NOT NULL DEFAULT 0, recorded_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
    )
    rows = [
        ("k1", "h1", "conv", "index-transcript-memory", "delivered", "", "doc1", 1, "t0", "2026-06-13T00:02:00Z"),
        ("k2", "h2", "conv", "index-transcript-memory", "delivered", "", "doc2", 1, "t0", "2026-06-13T00:01:00Z"),
        ("k3", "h3", "conv", "index-session-memory", "delivered", "", "docX", 1, "t0", "2026-06-13T00:05:00Z"),
        ("k4", "h4", "conv", "index-transcript-memory", "pending", "", "", 0, "t0", "2026-06-13T00:06:00Z"),
        ("k5", "h5", "conv", "index-transcript-memory", "delivered", "", "doc5", 1, "t0", "2026-06-13T00:00:30Z"),
    ]
    connection.executemany("INSERT INTO shadow_ingest_log VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    connection.commit()
    connection.close()
    return db


def test_watermark_roundtrip_and_missing_is_empty(tmp_path):
    path = tmp_path / "state" / "watermark.txt"
    assert read_watermark(path) == ""
    write_watermark(path, "2026-06-13T00:03:00Z")
    assert read_watermark(path) == "2026-06-13T00:03:00Z"
    write_watermark(path, "2026-06-13T01:00:00Z")
    assert read_watermark(path) == "2026-06-13T01:00:00Z"


def test_read_recent_transcript_deliveries_filters_and_orders(tmp_path):
    db = _shadow_db(tmp_path)

    out = read_recent_transcript_deliveries(db, since_watermark="2026-06-13T00:00:45Z")

    assert [row["document_ref"] for row in out] == ["doc2", "doc1"]


def test_neuron_session_memory_build_dry_run_reads_shadow_log_without_ids_or_mutation(tmp_path, capsys):
    db = _shadow_db(tmp_path)
    watermark = tmp_path / "state" / "watermark.txt"
    write_watermark(watermark, "2026-06-13T00:00:45Z")

    rc = main([
        "neuron-session-memory-build",
        "--dry-run",
        "--shadow-db",
        str(db),
        "--watermark-file",
        str(watermark),
    ])

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["status"] == "dry_run_complete"
    assert report["deliveries_seen"] == 2
    assert report["planned_new_watermark"] == "2026-06-13T00:02:00Z"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
    assert "doc1" not in json.dumps(report)
    assert read_watermark(watermark) == "2026-06-13T00:00:45Z"


def test_neuron_session_memory_build_dry_run_plans_the_dirty_drain(tmp_path, capsys):
    db = _shadow_db(tmp_path)
    watermark = tmp_path / "state" / "watermark.txt"
    write_watermark(watermark, "2026-06-13T00:00:45Z")
    ledger = Ledger(tmp_path / "neuron.sqlite")

    rc = main([
        "neuron-session-memory-build",
        "--dry-run",
        "--shadow-db",
        str(db),
        "--watermark-file",
        str(watermark),
        "--ledger",
        str(tmp_path / "neuron.sqlite"),
    ])

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["status"] == "dry_run_complete"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
    assert report["build"]["status"] == "ok"
    assert report["build"]["network_used"] is False
    assert report["build"]["mutation_performed"] is False
    assert ledger.get_dirty_session_memory("sha256:absent") is None


def test_neuron_session_memory_build_probe_meta_is_retired(tmp_path, capsys):
    db = _shadow_db(tmp_path)
    watermark = tmp_path / "state" / "watermark.txt"
    write_watermark(watermark, "2026-06-13T00:00:45Z")

    rc = main([
        "neuron-session-memory-build",
        "--dry-run",
        "--probe-meta",
        "--shadow-db",
        str(db),
        "--watermark-file",
        str(watermark),
        "--retired-index-bridge-url",
        "http://127.0.0.1:19380",
        "--retired-index-bridge-token-env",
        "RETIRED_INDEX_BRIDGE_API_KEY",
    ])

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "blocked_live_execution"
    assert report["failed_error_class"] == "retired_index_bridge_metadata_probe_not_vendored"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
    assert "doc1" not in json.dumps(report)


def _live_approval(argv: list[str], *, operation: str = "neuron_session_memory_build") -> dict:
    return {
        "schema_version": "agent_knowledge_live_approval.v1",
        "operation": operation,
        "operator_approval": {"approved": True},
        "redaction_required": True,
        "timeout_seconds": 1800,
        "rollback_or_abort_criteria": "abort on partial_failed",
        "command": {"argv": ["neuron-session-memory-build", *argv]},
    }


def _live_argv(tmp_path, *, approval_name: str = "approval.json", runtime: str = "runtime") -> list[str]:
    return [
        "--ledger", str(tmp_path / "neuron.sqlite"),
        "--shadow-db", str(tmp_path / "ingest.sqlite"),
        "--watermark-file", str(tmp_path / "watermark.txt"),
        "--runtime-dir", str(tmp_path / runtime),
        "--approval", str(tmp_path / approval_name),
    ]


def test_neuron_session_memory_build_live_requires_valid_approval(tmp_path, capsys):
    # 유효 approval record가 없으면 네트워크/뮤테이션 전에 fail-closed.
    rc = main(_live_argv(tmp_path, approval_name="missing-approval.json"))
    captured = capsys.readouterr()
    assert rc == 2
    assert "approval file not found" in captured.err


def test_neuron_session_memory_build_live_is_retired(tmp_path, capsys):
    # 승인 contract가 맞아도 live write target(은퇴한 external index bridge)이
    # 없어서 구조화된 blocked 보고로 fail closed한다.
    argv = _live_argv(tmp_path)
    (tmp_path / "approval.json").write_text(json.dumps(_live_approval(argv)), encoding="utf-8")

    rc = main(argv)
    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "blocked_live_execution"
    assert report["failed_error_class"] == "live_neuron_session_memory_build_not_vendored"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
    assert report["index_write_performed"] is False


def test_neuron_session_memory_build_dry_run_rejects_live_arguments(tmp_path, capsys):
    db = _shadow_db(tmp_path)
    watermark = tmp_path / "watermark.txt"

    rc = main([
        "--dry-run",
        "--shadow-db",
        str(db),
        "--watermark-file",
        str(watermark),
        "--retired-index-bridge-url",
        "http://127.0.0.1:19380",
    ])

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "blocked_live_execution"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
