from __future__ import annotations

import hashlib
import json
import os

from agent_knowledge.ledger import Ledger
from agent_knowledge.rag_ingress.backfill_apply import apply_backfill_to_state_db
from agent_knowledge.rag_ingress.server_runtime import job_id_for_payload
from agent_knowledge.rag_ingress.state_cli import main
from agent_knowledge.rag_ingress.state_db import RAGIngressStateDB
from agent_knowledge.session_memory.transcript_model import TranscriptChunk

DEFAULT_TRANSCRIPT_TARGET_PROFILE = "index-transcript-memory"


def _payload(*, key: str = "k1", body: str = "hello body") -> dict:
    return {
        "schemaVersion": "rag_ingress_enqueue.v1",
        "source": {"host": "mac_mini", "producer": "test", "provider": "codex", "project": "p"},
        "payload": {
            "kind": "redacted_rag_ready_document",
            "redactionVersion": "redaction.v2",
            "document": {
                "filename": "doc.md",
                "contentType": "text/markdown",
                "body": body,
                "metadata": {"type": "conversation_chunk", "knowledge_id": f"kn_{key}"},
            },
        },
        "contentHash": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "targetProfile": DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        "kind": "conversation_chunk",
        "idempotencyKey": key,
    }


def _write_queue(tmp_path, *payloads: dict):
    root = tmp_path / "queue"
    pending = root / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    for index, payload in enumerate(payloads):
        (pending / f"job_{index}.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


def _state_db_path(tmp_path):
    private = tmp_path / "private"
    private.mkdir(parents=True, exist_ok=True)
    os.chmod(private, 0o700)
    return private / "state.sqlite"


def _seed_state_db(tmp_path, *payloads: dict):
    state_db = RAGIngressStateDB(_state_db_path(tmp_path))
    result = apply_backfill_to_state_db(state_db=state_db, payloads=list(payloads), dry_run=False)
    assert result["conflict_count"] == 0
    return state_db


def _replay_requested_ledger(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite3")
    chunk = TranscriptChunk(
        chunk_id="chunk_cli_replay",
        session_id_hash="sha256:cli_replay",
        provider="codex",
        project="workspace-index-advisor",
        turn_start_index=1,
        turn_end_index=2,
        redacted_text="replay chunk redacted text",
        content_hash="sha256:ignored",
    )
    item = ledger.upsert_transcript_chunk(knowledge_id="kn_cli_replay", chunk=chunk)
    ledger.mark_enqueued(
        item["knowledge_id"],
        target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        job_id="job_original",
    )
    row = next(
        row
        for row in ledger.list_queued_documents(
            document_type="conversation_chunk",
            target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
            limit=50,
        )
        if row["knowledge_id"] == item["knowledge_id"]
    )
    ledger.mark_replay_requested_if_queued(
        item["knowledge_id"],
        reason="cli_replay_test",
        expected_target_profile=row["target_profile"],
        expected_ingress_job_id=row["ingress_job_id"],
        expected_updated_at=row["updated_at"],
    )
    return ledger.path


def test_state_cli_backfill_apply_dry_run_does_not_create_state_db(tmp_path, capsys):
    queue_root = _write_queue(tmp_path, _payload(key="k1", body="b1"), _payload(key="k2", body="b2"))
    state_db_path = tmp_path / "private" / "state.sqlite"

    rc = main([
        "backfill-apply",
        "--state-db",
        str(state_db_path),
        "--queue-root",
        str(queue_root),
        "--redact-paths",
        "--dry-run",
    ])

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["dry_run"] is True
    assert report["planned_count"] == 2
    assert report["mutation_performed"] is False
    assert state_db_path.exists() is False


def test_state_cli_backfill_apply_live_is_fail_closed(tmp_path, capsys):
    queue_root = _write_queue(tmp_path, _payload(key="k1", body="b1"))
    state_db_path = tmp_path / "private" / "state.sqlite"

    rc = main([
        "backfill-apply",
        "--state-db",
        str(state_db_path),
        "--queue-root",
        str(queue_root),
        "--redact-paths",
    ])

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "blocked_live_execution"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
    assert state_db_path.exists() is False


def test_state_cli_replay_deliver_dry_run_uses_read_only_ledger(tmp_path, capsys):
    ledger_path = _replay_requested_ledger(tmp_path)

    rc = main([
        "rag-ingress-state",
        "replay-deliver",
        "--ledger",
        str(ledger_path),
        "--ingress-url",
        "http://127.0.0.1:18080",
        "--reason",
        "cli_replay_test",
        "--redact-paths",
        "--dry-run",
    ])

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["dry_run"] is True
    assert report["selected_count"] == 1
    assert report["network_used"] is False
    assert report["mutation_performed"] is False


def test_state_cli_drain_deliveries_dry_run(tmp_path, capsys):
    _seed_state_db(tmp_path, _payload(key="drain1", body="drain body"))

    rc = main([
        "drain-deliveries",
        "--state-db",
        str(_state_db_path(tmp_path)),
        "--redact-paths",
        "--dry-run",
    ])

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["execution_status"] == "dry_run"
    assert report["selected_count"] == 1
    assert report["payload_available_count"] == 1
    assert report["network_used"] is False
    assert report["mutation_performed"] is False


def test_state_cli_reconcile_deliveries_dry_run(tmp_path, capsys):
    payload = _payload(key="rec1", body="reconcile body")
    state_db = _seed_state_db(tmp_path, payload)
    job_id = job_id_for_payload(payload)
    assert state_db.claim_delivery_job(job_id, lease_owner="worker")
    assert state_db.mark_delivery_executing(job_id, lease_owner="worker")

    rc = main([
        "reconcile-deliveries",
        "--state-db",
        str(_state_db_path(tmp_path)),
        "--redact-paths",
        "--dry-run",
    ])

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["execution_status"] == "dry_run"
    assert report["selected_count"] == 1
    assert report["selected_status_counts"] == {
        "executing": 1,
        "failed_retryable": 0,
        "replayable": 0,
    }
    assert report["network_used"] is False
    assert report["mutation_performed"] is False
