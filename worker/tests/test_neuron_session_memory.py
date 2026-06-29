from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.neuron_session_memory import (
    main,
    probe_transcript_delivery_meta,
    public_seed_report,
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
        "project": "workspace-index-advisor",
        "session_id_hash": "sha256:sess",
    }
    meta.update(overrides)
    return meta


class _FakeRetiredIndexBridge:
    def __init__(self, docs):
        self._docs = docs
        self.meta_calls = []

    def get_document_meta(self, dataset_id, document_id):
        self.meta_calls.append((dataset_id, document_id))
        return self._docs.get(document_id)


class _FakeProbeRetiredIndexBridge(_FakeRetiredIndexBridge):
    def list_datasets(self, *, name="", **_kwargs):
        return [{"name": name, "id": "ds_1"}]


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


def test_seed_marks_distinct_sessions_and_advances_watermark(tmp_path):
    ledger = Ledger(tmp_path / "neuron.sqlite")
    retired_index_bridge = _FakeRetiredIndexBridge(
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

    report = seed_dirty_session_memory_from_deliveries(deliveries, retired_index_bridge=retired_index_bridge, ledger=ledger, dataset_ids=["ds_1"])

    assert report["seeded_sessions"] == 2
    assert report["new_watermark"] == "2026-06-13T00:03:00Z"
    assert sorted(report["session_id_hashes"]) == ["sha256:s1", "sha256:s2"]
    assert ledger.get_dirty_session_memory("sha256:s1")["status"] == "pending"
    assert ledger.get_dirty_session_memory("sha256:s2")["provider"] == "codex"


def test_seed_skips_non_conversation_and_missing_meta(tmp_path):
    ledger = Ledger(tmp_path / "neuron.sqlite")
    retired_index_bridge = _FakeRetiredIndexBridge(
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

    report = seed_dirty_session_memory_from_deliveries(deliveries, retired_index_bridge=retired_index_bridge, ledger=ledger, dataset_ids=["ds_1"])

    assert report["session_id_hashes"] == ["sha256:ok"]
    assert report["new_watermark"] == "2026-06-13T00:04:00Z"
    assert ledger.get_dirty_session_memory("sha256:x") is None


def test_seed_empty_deliveries_is_noop(tmp_path):
    ledger = Ledger(tmp_path / "neuron.sqlite")
    retired_index_bridge = _FakeRetiredIndexBridge(docs={})

    report = seed_dirty_session_memory_from_deliveries([], retired_index_bridge=retired_index_bridge, ledger=ledger, dataset_ids=["ds_1"])

    assert report["seeded_sessions"] == 0
    assert report["session_id_hashes"] == []
    assert retired_index_bridge.meta_calls == []


def test_probe_transcript_delivery_meta_reports_counts_without_raw_ids():
    retired_index_bridge = _FakeProbeRetiredIndexBridge(
        docs={
            "doc_a": {"id": "doc_a", "meta_fields": _meta(session_id_hash="sha256:s1", knowledge_id="kn_a")},
            "doc_b": {
                "id": "doc_b",
                "meta_fields": _meta(
                    session_id_hash="sha256:s2",
                    knowledge_id="kn_b",
                    project="dendrite",
                    provider="antigravity",
                ),
            },
            "doc_other": {
                "id": "doc_other",
                "meta_fields": _meta(type="project_memory", result_type="project_memory"),
            },
        }
    )
    deliveries = [
        {"document_ref": "doc_a", "updated_at": "2026-06-13T00:01:00Z"},
        {"document_ref": "doc_b", "updated_at": "2026-06-13T00:02:00Z"},
        {"document_ref": "doc_b", "updated_at": "2026-06-13T00:03:00Z"},
        {"document_ref": "doc_other", "updated_at": "2026-06-13T00:04:00Z"},
        {"document_ref": "doc_missing", "updated_at": "2026-06-13T00:05:00Z"},
    ]

    report = probe_transcript_delivery_meta(deliveries, retired_index_bridge=retired_index_bridge, dataset_ids=["ds_1"])

    assert report["counts"]["deliveries_seen"] == 5
    assert report["counts"]["unique_document_refs"] == 4
    assert report["counts"]["conversation_chunk_meta"] == 2
    assert report["counts"]["non_conversation_meta"] == 1
    assert report["counts"]["missing_meta"] == 1
    assert {
        "project": "dendrite",
        "provider": "antigravity",
        "documents": 1,
        "sessions": 1,
    } in report["project_provider_buckets"]
    dumped = json.dumps(report)
    assert "doc_a" not in dumped
    assert "sha256:s1" not in dumped
    assert report["raw_ids_printed"] is False


def test_public_seed_report_redacts_session_hashes():
    seed = {
        "seeded_sessions": 2,
        "new_watermark": "2026-06-13T00:03:00Z",
        "session_id_hashes": ["sha256:s1", "sha256:s2"],
    }

    report = public_seed_report(seed, scanned=3)

    dumped = json.dumps(report)
    assert report["seeded_sessions"] == 2
    assert report["scanned"] == 3
    assert report["raw_ids_printed"] is False
    assert "sha256:s1" not in dumped


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


def test_neuron_session_memory_build_probe_meta_is_read_only(tmp_path, capsys, monkeypatch):
    import agent_knowledge.index_client as index_client

    db = _shadow_db(tmp_path)
    watermark = tmp_path / "state" / "watermark.txt"
    write_watermark(watermark, "2026-06-13T00:00:45Z")
    fake = _FakeProbeRetiredIndexBridge(
        docs={
            "doc1": {"id": "doc1", "meta_fields": _meta(project="neurons")},
            "doc2": {
                "id": "doc2",
                "meta_fields": _meta(project="dendrite", provider="antigravity"),
            },
        }
    )
    monkeypatch.setenv("RETIRED_INDEX_BRIDGE_API_KEY", "test-token")
    monkeypatch.setattr(index_client, "RetiredIndexBridgeHttpClient", lambda **_kwargs: fake)

    rc = main(
        [
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
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["status"] == "dry_run_complete"
    assert report["network_used"] is True
    assert report["mutation_performed"] is False
    assert report["index_write_performed"] is False
    assert report["meta_probe"]["counts"]["conversation_chunk_meta"] == 2
    assert report["meta_probe"]["counts"]["sessions_seen"] == 1
    assert "doc1" not in json.dumps(report)
    assert read_watermark(watermark) == "2026-06-13T00:00:45Z"


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
        "--retired-index-bridge-url", "http://127.0.0.1:19380",
        "--retired-index-bridge-token-env", "RETIRED_INDEX_BRIDGE_API_KEY",
        "--runtime-dir", str(tmp_path / runtime),
        "--approval", str(tmp_path / approval_name),
    ]


def test_neuron_session_memory_build_live_requires_valid_approval(tmp_path, capsys, monkeypatch):
    # 유효 approval record가 없으면 네트워크/뮤테이션 전에 fail-closed.
    monkeypatch.setenv("RETIRED_INDEX_BRIDGE_API_KEY", "test-token")
    rc = main(_live_argv(tmp_path, approval_name="missing-approval.json"))
    captured = capsys.readouterr()
    assert rc == 2
    assert "approval file not found" in captured.err


def test_neuron_session_memory_build_live_runs_with_valid_approval(tmp_path, capsys, monkeypatch):
    # 승인 contract가 맞으면 live build 경로가 실제로 seed+build를 수행한다.
    import agent_knowledge.session_memory.dirty_session_memory_sync as sync
    import agent_knowledge.session_memory.neuron_session_memory as nsm

    monkeypatch.setenv("RETIRED_INDEX_BRIDGE_API_KEY", "test-token")
    monkeypatch.setattr(sync, "resolve_dataset_id", lambda **kw: "ds-session-memory")
    argv = _live_argv(tmp_path)
    argv.extend(["--limit", "50"])

    def _stub(**kw):
        assert kw["delivery_limit"] == 50
        return {
            "seed": {"seeded_sessions": 0, "new_watermark": ""},
            "build": {"status": "ok", "processed": 2, "deferred": 0},
        }

    monkeypatch.setattr(nsm, "run_neuron_session_memory_build_once", _stub)
    (tmp_path / "approval.json").write_text(json.dumps(_live_approval(argv)), encoding="utf-8")

    rc = main(argv)
    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["status"] == "ok"
    assert report["mode"] == "live"
    assert report["build"]["status"] == "ok"


def test_neuron_session_memory_build_live_skips_when_locked(tmp_path, capsys, monkeypatch):
    # 두 번째 동시 실행은 flock에 막혀 build 없이 skip(=cron pileup 방지 회귀 가드).
    import fcntl

    import agent_knowledge.session_memory.dirty_session_memory_sync as sync
    import agent_knowledge.session_memory.neuron_session_memory as nsm

    monkeypatch.setenv("RETIRED_INDEX_BRIDGE_API_KEY", "test-token")
    monkeypatch.setattr(sync, "resolve_dataset_id", lambda **kw: "ds-session-memory")
    called = {"n": 0}

    def _stub(**kw):
        called["n"] += 1
        return {"seed": {"seeded_sessions": 0, "new_watermark": ""}, "build": {"status": "ok"}}

    monkeypatch.setattr(nsm, "run_neuron_session_memory_build_once", _stub)
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    argv = _live_argv(tmp_path)
    (tmp_path / "approval.json").write_text(json.dumps(_live_approval(argv)), encoding="utf-8")

    holder = (runtime / "run.lock").open("a+", encoding="utf-8")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        rc = main(argv)
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["status"] == "already_running"
    assert called["n"] == 0


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
