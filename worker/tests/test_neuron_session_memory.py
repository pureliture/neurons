from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.neuron_session_memory import (
    main,
    read_recent_transcript_deliveries,
    read_watermark,
    seed_dirty_session_memory_from_deliveries,
    write_watermark,
)


def _meta(**overrides):
    meta = {
        "knowledge_id": "kn_x",
        "result_type": "conversation_chunk",
        "type": "conversation_chunk",
        "provider": "codex",
        "project": "workspace-ragflow-advisor",
        "session_id_hash": "sha256:sess",
    }
    meta.update(overrides)
    return meta


class _FakeRagflow:
    def __init__(self, docs):
        self._docs = docs
        self.meta_calls = []

    def get_document_meta(self, dataset_id, document_id):
        self.meta_calls.append((dataset_id, document_id))
        return self._docs.get(document_id)


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
        ("k1", "h1", "conv", "ragflow-transcript-memory", "delivered", "", "doc1", 1, "t0", "2026-06-13T00:02:00Z"),
        ("k2", "h2", "conv", "ragflow-transcript-memory", "delivered", "", "doc2", 1, "t0", "2026-06-13T00:01:00Z"),
        ("k3", "h3", "conv", "ragflow-session-memory", "delivered", "", "docX", 1, "t0", "2026-06-13T00:05:00Z"),
        ("k4", "h4", "conv", "ragflow-transcript-memory", "pending", "", "", 0, "t0", "2026-06-13T00:06:00Z"),
        ("k5", "h5", "conv", "ragflow-transcript-memory", "delivered", "", "doc5", 1, "t0", "2026-06-13T00:00:30Z"),
    ]
    connection.executemany("INSERT INTO shadow_ingest_log VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    connection.commit()
    connection.close()
    return db


def test_seed_marks_distinct_sessions_and_advances_watermark(tmp_path):
    ledger = Ledger(tmp_path / "neuron.sqlite")
    ragflow = _FakeRagflow(
        docs={
            "doc_a": {"id": "doc_a", "meta_fields": _meta(session_id_hash="sha256:s1", knowledge_id="kn_a")},
            "doc_b": {"id": "doc_b", "meta_fields": _meta(session_id_hash="sha256:s2", knowledge_id="kn_b")},
            "doc_a2": {"id": "doc_a2", "meta_fields": _meta(session_id_hash="sha256:s1", knowledge_id="kn_a2")},
        }
    )
    deliveries = [
        {"document_ref": "doc_a", "updated_at": "2026-06-13T00:01:00Z"},
        {"document_ref": "doc_b", "updated_at": "2026-06-13T00:02:00Z"},
        {"document_ref": "doc_a2", "updated_at": "2026-06-13T00:03:00Z"},
    ]

    report = seed_dirty_session_memory_from_deliveries(deliveries, ragflow=ragflow, ledger=ledger, dataset_ids=["ds_1"])

    assert report["seeded_sessions"] == 2
    assert report["new_watermark"] == "2026-06-13T00:03:00Z"
    assert sorted(report["session_id_hashes"]) == ["sha256:s1", "sha256:s2"]
    assert ledger.get_dirty_session_memory("sha256:s1")["status"] == "pending"
    assert ledger.get_dirty_session_memory("sha256:s2")["provider"] == "codex"


def test_seed_skips_non_conversation_and_missing_meta(tmp_path):
    ledger = Ledger(tmp_path / "neuron.sqlite")
    ragflow = _FakeRagflow(
        docs={
            "doc_ok": {"id": "doc_ok", "meta_fields": _meta(session_id_hash="sha256:ok")},
            "doc_other": {
                "id": "doc_other",
                "meta_fields": _meta(type="project_memory", result_type="project_memory", session_id_hash="sha256:x"),
            },
        }
    )
    deliveries = [
        {"document_ref": "doc_ok", "updated_at": "2026-06-13T00:01:00Z"},
        {"document_ref": "doc_other", "updated_at": "2026-06-13T00:02:00Z"},
        {"document_ref": "doc_missing", "updated_at": "2026-06-13T00:03:00Z"},
        {"document_ref": "", "updated_at": "2026-06-13T00:04:00Z"},
    ]

    report = seed_dirty_session_memory_from_deliveries(deliveries, ragflow=ragflow, ledger=ledger, dataset_ids=["ds_1"])

    assert report["session_id_hashes"] == ["sha256:ok"]
    assert report["new_watermark"] == "2026-06-13T00:04:00Z"
    assert ledger.get_dirty_session_memory("sha256:x") is None


def test_seed_empty_deliveries_is_noop(tmp_path):
    ledger = Ledger(tmp_path / "neuron.sqlite")
    ragflow = _FakeRagflow(docs={})

    report = seed_dirty_session_memory_from_deliveries([], ragflow=ragflow, ledger=ledger, dataset_ids=["ds_1"])

    assert report["seeded_sessions"] == 0
    assert report["session_id_hashes"] == []
    assert ragflow.meta_calls == []


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


def test_neuron_session_memory_build_live_invocation_is_blocked(capsys):
    rc = main([
        "--ledger",
        "/tmp/neuron.sqlite",
        "--shadow-db",
        "/tmp/ingest.sqlite",
        "--watermark-file",
        "/tmp/watermark.txt",
        "--ragflow-url",
        "http://127.0.0.1:19380",
        "--token-env",
        "RAGFLOW_API_KEY",
        "--approval",
        "/tmp/approval.json",
    ])

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "blocked_live_execution"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_neuron_session_memory_build_dry_run_rejects_live_arguments(tmp_path, capsys):
    db = _shadow_db(tmp_path)
    watermark = tmp_path / "watermark.txt"

    rc = main([
        "--dry-run",
        "--shadow-db",
        str(db),
        "--watermark-file",
        str(watermark),
        "--ragflow-url",
        "http://127.0.0.1:19380",
    ])

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "blocked_live_execution"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
