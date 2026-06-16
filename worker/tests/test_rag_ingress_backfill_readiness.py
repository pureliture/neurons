from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from agent_knowledge.rag_ingress.backfill import (
    evaluate_delivery_readiness,
    plan_backfill_from_payloads,
    snapshot_queue_files,
    state_db_counts,
)
from agent_knowledge.rag_ingress.rag_ready_document import build_ingress_enqueue_payload, build_rag_ready_document
from agent_knowledge.rag_ingress.state_db import RAGIngressStateDB


NOW = datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)


def _payload(*, body="# Body", key: str | None = None):
    document = build_rag_ready_document(
        target_profile="transcript-memory",
        document_kind="conversation_chunk",
        source_namespace="codex",
        source_alias="workspace-ragflow-advisor/session",
        privacy_class="private",
        body=body,
        filename="canary.md",
        metadata={"project": "workspace-ragflow-advisor", "privacy_class": "private"},
    )
    payload = build_ingress_enqueue_payload(
        document,
        source={"provider": "codex", "source_alias": "workspace-ragflow-advisor/session"},
    )
    if key is not None:
        payload["idempotencyKey"] = key
    return payload


def test_backfill_dry_run_does_not_mutate_queue_files_or_state_db(tmp_path):
    queue = tmp_path / "queue" / "pending"
    queue.mkdir(parents=True)
    payload = _payload()
    source = queue / "job_1.json"
    source.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    state_db = RAGIngressStateDB(tmp_path / "private" / "state.sqlite")

    before_files = snapshot_queue_files(tmp_path / "queue")
    before_counts = state_db_counts(state_db)
    report = plan_backfill_from_payloads([payload])
    after_files = snapshot_queue_files(tmp_path / "queue")
    after_counts = state_db_counts(state_db)

    assert report["cutover_status"] == "migration_ready_pending_approval"
    assert len(report["planned_rows"]) == 1
    assert before_files == after_files
    assert before_counts == after_counts


def test_backfill_duplicate_converges_without_second_delivery_job():
    payload = _payload(key="shared-key")

    report = plan_backfill_from_payloads([payload, payload])

    assert report["cutover_status"] == "migration_ready_pending_approval"
    assert len(report["planned_rows"]) == 1
    assert report["replay"][0]["outcome"] == "duplicate"
    assert report["replay"][0]["converges_to_job_id"] == report["planned_rows"][0]["delivery_job"]["job_id"]


def test_backfill_conflict_blocks_cutover_and_quarantines():
    report = plan_backfill_from_payloads(
        [
            _payload(body="# Original", key="shared-key"),
            _payload(body="# Changed", key="shared-key"),
        ]
    )

    assert report["cutover_status"] == "cutover_blocked"
    assert report["blockers"][0]["code"] == "idempotency_conflict"
    assert report["quarantine"][0]["outcome"] == "conflict"


def test_readiness_blocks_replayable_async_fail_quarantine_and_stale_projection():
    report = evaluate_delivery_readiness(
        [
            {"job_id": "job_replay", "status": "replayable", "last_reconciled_at": NOW.isoformat()},
            {"job_id": "job_fail", "status": "failed_retryable", "last_reconciled_at": NOW.isoformat()},
            {"job_id": "job_quarantine", "status": "quarantined", "last_reconciled_at": NOW.isoformat()},
            {"job_id": "job_stale", "status": "succeeded", "last_reconciled_at": (NOW - timedelta(seconds=3601)).isoformat()},
        ],
        now=NOW,
    )

    codes = {blocker["code"] for blocker in report["blockers"]}
    assert report["cutover_status"] == "cutover_blocked"
    assert codes == {
        "replayable_unresolved",
        "async_fail_unresolved",
        "quarantined_unresolved",
        "stale_projection",
    }


def test_fresh_succeeded_projection_is_pending_approval_not_cutover():
    report = evaluate_delivery_readiness(
        [{"job_id": "job_ok", "status": "succeeded", "last_reconciled_at": NOW.isoformat()}],
        now=NOW,
    )

    assert report["cutover_status"] == "migration_ready_pending_approval"
    assert report["blockers"] == []
    assert report["rollback_owner"] == "neurons"
