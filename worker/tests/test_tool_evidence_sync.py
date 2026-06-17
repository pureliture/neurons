"""TDD for the tool_evidence_summary ingress-queue sync runner.

Live delivery never uploads to RAGFlow directly; it enqueues redacted part
documents to the local rag-ingress-queue, which routes by target_profile. This
runner mirrors the conversation_chunk enqueue path (IngressQueueClient,
mark_enqueued) and must re-redact for the public queue and never leak.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.tool_evidence_sync import ToolEvidenceSyncRunner


PROJECT = "workspace-ragflow-advisor"
TARGET_PROFILE = "ragflow-transcript-memory"
SECRET = "synthetic-" + "sync-token-value"
LOCAL_PATH = "/Users/example/Projects/secret/run.py"


def _write_codex_source(path: Path) -> None:
    def resp(payload, ts="2026-05-27T23:21:00.000Z"):
        return {"type": "response_item", "timestamp": ts, "payload": payload}

    records = [
        {"type": "session_meta", "timestamp": "2026-05-27T23:20:47.000Z", "payload": {"id": "sync-session-1"}},
        resp({"type": "function_call", "name": "exec_command", "call_id": "c1", "arguments": json.dumps({"cmd": "uv run pytest tests -q"})}),
        resp({"type": "function_call_output", "call_id": "c1", "output": "11 passed in 1.0s raw_transcript_data"}),
        resp({"type": "function_call", "name": "exec_command", "call_id": "c2", "arguments": json.dumps({"cmd": "git commit -m wip"})}),
        resp({"type": "function_call_output", "call_id": "c2", "output": f"[codex/x abc1234] wip {LOCAL_PATH} EVIDENCE_TOKEN={SECRET}\n 2 files changed"}),
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


class _FakeIngressSink:
    def __init__(self):
        self.calls = []

    def enqueue_document(self, *, source, packed, content_hash, target_profile, kind, idempotency_key):
        self.calls.append({
            "source": source, "body": packed.body, "metadata": packed.metadata,
            "content_hash": content_hash, "target_profile": target_profile,
            "kind": kind, "idempotency_key": idempotency_key,
        })
        return {"job_id": f"job_te_{len(self.calls):03d}", "status": "queued"}


def _make(tmp_path):
    src = tmp_path / "rollout-sync.jsonl"
    _write_codex_source(src)
    ledger = Ledger(tmp_path / "ledger.sqlite")
    loc = "sha256:" + "a" * 64
    return src, ledger, loc


def test_sync_enqueues_part_documents_to_ingress_queue(tmp_path):
    src, ledger, loc = _make(tmp_path)
    sink = _FakeIngressSink()
    runner = ToolEvidenceSyncRunner(ledger=ledger, enqueue_sink=sink, target_profile=TARGET_PROFILE)
    report = runner.run(provider="codex", source_path=src, project=PROJECT, source_locator_hash=loc)
    assert report["enqueued"] >= 1
    assert report["enqueued"] == len(sink.calls)
    assert report["network_used"] is True
    assert all(c["target_profile"] == TARGET_PROFILE for c in sink.calls)
    assert all(c["kind"] == "tool_evidence_summary" for c in sink.calls)
    assert all(c["metadata"]["type"] == "tool_evidence_summary" for c in sink.calls)


def test_sync_marks_knowledge_items_queued(tmp_path):
    src, ledger, loc = _make(tmp_path)
    sink = _FakeIngressSink()
    runner = ToolEvidenceSyncRunner(ledger=ledger, enqueue_sink=sink, target_profile=TARGET_PROFILE)
    runner.run(provider="codex", source_path=src, project=PROJECT, source_locator_hash=loc)
    con = sqlite3.connect(str(tmp_path / "ledger.sqlite"))
    try:
        rows = con.execute("SELECT status, ingress_job_id, ingress_target_profile FROM knowledge_items WHERE type='tool_evidence_summary'").fetchall()
    finally:
        con.close()
    assert rows
    assert all(r[0] == "queued" for r in rows)
    assert all(r[1] for r in rows)  # job id recorded
    assert all(r[2] == TARGET_PROFILE for r in rows)


def test_sync_does_not_leak_secrets_to_queue(tmp_path):
    src, ledger, loc = _make(tmp_path)
    sink = _FakeIngressSink()
    runner = ToolEvidenceSyncRunner(ledger=ledger, enqueue_sink=sink, target_profile=TARGET_PROFILE)
    runner.run(provider="codex", source_path=src, project=PROJECT, source_locator_hash=loc)
    blob = json.dumps(sink.calls, ensure_ascii=False)
    assert SECRET not in blob
    assert LOCAL_PATH not in blob
    assert "/Users/" not in blob
    assert "raw_transcript" not in blob


def test_sync_dry_run_plans_without_enqueue(tmp_path):
    src, ledger, loc = _make(tmp_path)
    runner = ToolEvidenceSyncRunner(ledger=ledger, enqueue_sink=None, target_profile=TARGET_PROFILE)
    report = runner.run(provider="codex", source_path=src, project=PROJECT, source_locator_hash=loc)
    assert report["enqueued"] == 0
    assert report["documents_planned"] >= 1
    assert report["network_used"] is False
    con = sqlite3.connect(str(tmp_path / "ledger.sqlite"))
    try:
        n = con.execute("SELECT count(*) FROM knowledge_items WHERE status='queued'").fetchone()[0]
    finally:
        con.close()
    assert n == 0


def test_sync_is_idempotent_on_rerun(tmp_path):
    src, ledger, loc = _make(tmp_path)
    sink = _FakeIngressSink()
    runner = ToolEvidenceSyncRunner(ledger=ledger, enqueue_sink=sink, target_profile=TARGET_PROFILE)
    first = runner.run(provider="codex", source_path=src, project=PROJECT, source_locator_hash=loc)
    con = sqlite3.connect(str(tmp_path / "ledger.sqlite"))
    try:
        before = con.execute("SELECT count(*) FROM knowledge_items WHERE type='tool_evidence_summary'").fetchone()[0]
    finally:
        con.close()
    runner.run(provider="codex", source_path=src, project=PROJECT, source_locator_hash=loc)
    con = sqlite3.connect(str(tmp_path / "ledger.sqlite"))
    try:
        after = con.execute("SELECT count(*) FROM knowledge_items WHERE type='tool_evidence_summary'").fetchone()[0]
    finally:
        con.close()
    assert after == before  # same content -> no duplicate knowledge_items


def test_sync_rerun_same_ledger_is_idempotent(tmp_path):
    """2nd run against the same persistent ledger must enqueue 0 parts."""
    src, ledger, loc = _make(tmp_path)
    sink = _FakeIngressSink()
    runner = ToolEvidenceSyncRunner(ledger=ledger, enqueue_sink=sink, target_profile=TARGET_PROFILE)

    first = runner.run(provider="codex", source_path=src, project=PROJECT, source_locator_hash=loc)
    n = first["enqueued"]
    assert n >= 1, "first run must enqueue at least one part"

    second = runner.run(provider="codex", source_path=src, project=PROJECT, source_locator_hash=loc)
    assert second["enqueued"] == 0, "second run must enqueue nothing (already delivered)"
    assert second["skipped_already_indexed"] == n, "skipped count must equal first-run enqueue count"
    assert len(sink.calls) == n, "total sink calls must not exceed first-run count (no double-enqueue)"


def test_sync_stores_tool_evidence_metadata_json(tmp_path):
    src, ledger, loc = _make(tmp_path)
    sink = _FakeIngressSink()
    runner = ToolEvidenceSyncRunner(ledger=ledger, enqueue_sink=sink, target_profile=TARGET_PROFILE)

    runner.run(provider="codex", source_path=src, project=PROJECT, source_locator_hash=loc)

    con = sqlite3.connect(str(tmp_path / "ledger.sqlite"))
    try:
        rows = con.execute(
            "SELECT knowledge_id, metadata_json FROM knowledge_items WHERE type='tool_evidence_summary'"
        ).fetchall()
    finally:
        con.close()
    assert rows
    for knowledge_id, metadata_json in rows:
        metadata = json.loads(metadata_json)
        assert metadata["type"] == "tool_evidence_summary"
        assert metadata["chunk_id"] == knowledge_id
        assert metadata["provider"] == "codex"
        assert metadata["project"] == PROJECT
        assert metadata["session_id_hash"].startswith("sha256:")


def test_sync_queue_metadata_includes_final_knowledge_id(tmp_path):
    src, ledger, loc = _make(tmp_path)
    sink = _FakeIngressSink()
    runner = ToolEvidenceSyncRunner(ledger=ledger, enqueue_sink=sink, target_profile=TARGET_PROFILE)

    runner.run(provider="codex", source_path=src, project=PROJECT, source_locator_hash=loc)

    assert sink.calls
    for call in sink.calls:
        assert call["metadata"]["type"] == "tool_evidence_summary"
        assert call["metadata"]["knowledge_id"]
        assert call["metadata"]["knowledge_id"] == call["metadata"]["chunk_id"]

