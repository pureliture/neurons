import hashlib
import json
import os

from agent_knowledge.rag_ingress.backfill import state_db_counts
from agent_knowledge.rag_ingress.backfill_apply import (
    apply_backfill_to_state_db,
    read_queue_payloads,
)
from agent_knowledge.rag_ingress.state_db import RAGIngressStateDB


def _payload(*, key, body="hello chunk body", target_profile="ragflow-transcript-memory", kind="conversation_chunk"):
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
                "metadata": {"type": "conversation_chunk"},
            },
        },
        "contentHash": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "targetProfile": target_profile,
        "kind": kind,
        "idempotencyKey": key,
    }


def _private_db_path(tmp_path):
    priv = tmp_path / "private"
    priv.mkdir(parents=True, exist_ok=True)
    os.chmod(priv, 0o700)
    return priv / "state.sqlite"


def _write_queue(tmp_path, payloads):
    root = tmp_path / "queue"
    pending = root / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    for i, payload in enumerate(payloads):
        (pending / f"job_{i}.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


# ---- dry-run ----

def test_backfill_apply_dry_run_no_state_db_write(tmp_path):
    payloads = [_payload(key="k1", body="b1"), _payload(key="k2", body="b2")]
    result = apply_backfill_to_state_db(state_db=None, payloads=payloads, dry_run=True)
    assert result["dry_run"] is True
    assert result["planned_count"] == 2
    assert result["applied_count"] == 0
    assert result["mutation_performed"] is False


# ---- live apply ----

def test_backfill_apply_live_seeds_state_db(tmp_path):
    state_db = RAGIngressStateDB(_private_db_path(tmp_path))
    payloads = [_payload(key="k1", body="b1"), _payload(key="k2", body="b2")]

    result = apply_backfill_to_state_db(state_db=state_db, payloads=payloads, dry_run=False)

    assert result["applied_count"] == 2
    assert result["mutation_performed"] is True
    counts = state_db_counts(state_db)
    assert counts["commands"] == 2
    assert counts["delivery_jobs"] == 2
    assert counts["inbox_events"] >= 2


def test_backfill_apply_is_idempotent_on_rerun(tmp_path):
    state_db = RAGIngressStateDB(_private_db_path(tmp_path))
    payloads = [_payload(key="k1", body="b1"), _payload(key="k2", body="b2")]
    apply_backfill_to_state_db(state_db=state_db, payloads=payloads, dry_run=False)

    rerun = apply_backfill_to_state_db(state_db=state_db, payloads=payloads, dry_run=False)

    assert rerun["applied_count"] == 0
    assert rerun["already_present_count"] == 2
    assert rerun["mutation_performed"] is False
    # the candidate did not grow new commands/delivery jobs
    counts = state_db_counts(state_db)
    assert counts["commands"] == 2
    assert counts["delivery_jobs"] == 2


def test_backfill_apply_skips_within_run_duplicate(tmp_path):
    state_db = RAGIngressStateDB(_private_db_path(tmp_path))
    # same idempotencyKey twice in one run
    payloads = [_payload(key="dupe", body="same"), _payload(key="dupe", body="same")]

    result = apply_backfill_to_state_db(state_db=state_db, payloads=payloads, dry_run=False)

    assert result["applied_count"] == 1
    assert result["already_present_count"] == 1
    assert state_db_counts(state_db)["commands"] == 1


# ---- queue reading ----

def test_read_queue_payloads_reads_json_files(tmp_path):
    root = _write_queue(tmp_path, [_payload(key="k1", body="b1"), _payload(key="k2", body="b2")])
    payloads = read_queue_payloads(root)
    assert len(payloads) == 2
    assert {p["idempotencyKey"] for p in payloads} == {"k1", "k2"}


def test_read_queue_payloads_rejects_symlink_payload_file(tmp_path):
    import pytest

    root = _write_queue(tmp_path, [_payload(key="k1", body="b1")])
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_payload(key="sneaky", body="outside body")), encoding="utf-8")
    (root / "pending" / "job_link.json").symlink_to(outside)

    with pytest.raises(ValueError) as excinfo:
        read_queue_payloads(root)
    # fail-closed, and the error never echoes the raw path
    assert str(outside) not in str(excinfo.value)
    assert "symlink" in str(excinfo.value)


def test_backfill_apply_conflict_same_key_different_body(tmp_path):
    state_db = RAGIngressStateDB(_private_db_path(tmp_path))
    apply_backfill_to_state_db(state_db=state_db, payloads=[_payload(key="kx", body="original")], dry_run=False)

    # same idempotencyKey, mutated body -> genuine conflict, not a clean sync
    conflict = apply_backfill_to_state_db(
        state_db=state_db, payloads=[_payload(key="kx", body="mutated")], dry_run=False
    )
    assert conflict["conflict_count"] == 1
    assert conflict["applied_count"] == 0
    assert conflict["already_present_count"] == 0
    assert conflict["cutover_status"] == "cutover_blocked"
