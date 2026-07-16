from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from agent_knowledge.cli import COMMAND_HANDLERS, COMMAND_METADATA
from agent_knowledge.couchdb_source.document_model import sha256_hash
from agent_knowledge.llm_brain_core import (
    InMemorySessionMemoryArtifactStore,
    SessionMemoryArtifact,
)
from agent_knowledge.llm_brain_core._util import hash_payload
from agent_knowledge.llm_brain_core.context import BrainReadService
from agent_knowledge.ledger import Ledger
from agent_knowledge.rag_ingress.state_db import (
    CommandResultSpec,
    DeliveryJobSpec,
    RAGIngressStateDB,
)
from agent_knowledge.rag_ingress.temporal_revision_rebuild import (
    REBUILD_OPERATION,
    main,
    rebuild_temporal_revisions,
)


PROJECT = "neurons"
SESSION_HASH = sha256_hash("private-session")
DATE_A = ("2026-07-09T10:00:00Z", "2026-07-09T10:30:00Z")
DATE_B = ("2026-07-15T10:00:00Z", "2026-07-15T10:30:00Z")


def _state_db(tmp_path: Path) -> RAGIngressStateDB:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    return RAGIngressStateDB(private / "state.sqlite")


def _payload(
    *,
    chunk_id: str,
    body: str,
    observed_at_start: str,
    observed_at_end: str,
    session_id_hash: str = SESSION_HASH,
    idempotency_key: str | None = None,
    turn_index: int = 0,
) -> dict:
    return {
        "schemaVersion": "rag_ingress_enqueue.v1",
        "source": {"provider": "codex", "project": PROJECT},
        "payload": {
            "kind": "redacted_rag_ready_document",
            "redactionVersion": "redaction.v2",
            "document": {
                "body": body,
                "metadata": {
                    "type": "conversation_chunk",
                    "project": PROJECT,
                    "provider": "codex",
                    "session_id_hash": session_id_hash,
                    "chunk_id": chunk_id,
                    "turn_start_index": turn_index,
                    "turn_end_index": turn_index,
                    "part_index": 1,
                    "part_count": 1,
                    "char_start": 0,
                    "char_end": len(body),
                    "observed_at_start": observed_at_start,
                    "observed_at_end": observed_at_end,
                },
            },
        },
        "contentHash": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
        "targetProfile": "index-transcript-memory",
        "kind": "conversation_chunk",
        "idempotencyKey": idempotency_key or f"wire-{chunk_id}",
    }


def _record_delivery(
    state_db: RAGIngressStateDB,
    payload: dict,
    *,
    status: str = "succeeded",
    recorded_at: datetime | None = None,
) -> None:
    assert state_db.record_delivery_payload(payload, now=recorded_at) == "recorded"
    key = str(payload["idempotencyKey"])
    payload_hash = str(payload["contentHash"])
    suffix = hashlib.sha256(key.encode()).hexdigest()[:12]
    job_id = f"job-{suffix}"
    state_db.command_transaction().execute(
        command_id=f"command-{suffix}",
        command_type="transcript_ingest",
        idempotency_key=key,
        payload_hash=payload_hash,
        result=CommandResultSpec(decision="accepted"),
        delivery_jobs=[
            DeliveryJobSpec(
                job_id=job_id,
                target_profile="index-transcript-memory",
                document_kind="conversation_chunk",
                idempotency_key=key,
                payload_hash=payload_hash,
            )
        ],
        now=recorded_at,
    )
    if status != "pending":
        state_db.record_delivery_evidence(job_id, status=status, observed_at=recorded_at)


def _record_date_a_b(state_db: RAGIngressStateDB) -> None:
    _record_delivery(
        state_db,
        _payload(
            chunk_id="chunk-a",
            body="alpha migration evidence",
            observed_at_start=DATE_A[0],
            observed_at_end=DATE_A[1],
            turn_index=1,
        ),
        recorded_at=datetime(2026, 7, 9, 11, 0, tzinfo=timezone.utc),
    )
    _record_delivery(
        state_db,
        _payload(
            chunk_id="chunk-b",
            body="beta rollout evidence",
            observed_at_start=DATE_B[0],
            observed_at_end=DATE_B[1],
            turn_index=2,
        ),
        recorded_at=datetime(2026, 7, 15, 11, 0, tzinfo=timezone.utc),
    )


def _execute_plan(
    *,
    state_db: RAGIngressStateDB,
    store: InMemorySessionMemoryArtifactStore,
    limit: int = 100,
) -> dict:
    plan = rebuild_temporal_revisions(
        state_db=state_db,
        artifact_store=store,
        project=PROJECT,
        limit=limit,
        max_runtime_seconds=30,
    )
    return rebuild_temporal_revisions(
        state_db=state_db,
        artifact_store=store,
        project=PROJECT,
        limit=limit,
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest=plan["plan_digest"],
    )


def test_succeeded_retained_deliveries_rebuild_distinct_bounded_date_revisions(
    tmp_path: Path,
) -> None:
    state_db = _state_db(tmp_path)
    _record_date_a_b(state_db)
    store = InMemorySessionMemoryArtifactStore()

    dry_run = rebuild_temporal_revisions(
        state_db=state_db,
        artifact_store=store,
        project=PROJECT,
        limit=100,
        max_runtime_seconds=30,
    )

    assert dry_run["planned_artifact_count"] == 2
    assert dry_run["total_remaining_artifact_count"] == 2
    assert dry_run["mutation_performed"] is False
    assert store.list_recent(project=PROJECT, limit=10) == []

    applied = rebuild_temporal_revisions(
        state_db=state_db,
        artifact_store=store,
        project=PROJECT,
        limit=100,
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest=dry_run["plan_digest"],
    )

    date_a = store.list_observed_interval_revisions(
        project=PROJECT,
        observed_at_start="2026-07-09T10:15:00Z",
        observed_at_end="2026-07-09T10:15:00Z",
    )
    date_b = store.list_observed_interval_revisions(
        project=PROJECT,
        observed_at_start="2026-07-15T10:15:00Z",
        observed_at_end="2026-07-15T10:15:00Z",
    )
    assert applied["inserted_artifact_count"] == 2
    assert applied["current_materialization_rebuild_required"] is True
    assert len(date_a) == len(date_b) == 1
    assert date_a[0].artifact_id != date_b[0].artifact_id
    assert date_a[0].source_revision != date_b[0].source_revision
    assert date_a[0].revision_observed_intervals == (DATE_A,)
    assert date_b[0].revision_observed_intervals == (DATE_B,)
    assert hash_payload("alpha") in date_a[0].search_term_hashes
    assert hash_payload("alpha") not in date_b[0].search_term_hashes
    assert hash_payload("beta") in date_b[0].search_term_hashes


def test_only_succeeded_hash_bound_delivery_rows_are_replay_eligible(tmp_path: Path) -> None:
    state_db = _state_db(tmp_path)
    succeeded = _payload(
        chunk_id="chunk-succeeded",
        body="eligible temporal evidence",
        observed_at_start=DATE_A[0],
        observed_at_end=DATE_A[1],
    )
    pending = _payload(
        chunk_id="chunk-pending",
        body="pending temporal evidence",
        observed_at_start=DATE_B[0],
        observed_at_end=DATE_B[1],
    )
    wrong_target = _payload(
        chunk_id="chunk-wrong-target",
        body="wrong target temporal evidence",
        observed_at_start=DATE_B[0],
        observed_at_end=DATE_B[1],
    )
    _record_delivery(state_db, succeeded)
    _record_delivery(state_db, pending, status="pending")
    _record_delivery(state_db, wrong_target)
    with state_db.connect() as connection:
        connection.execute(
            "UPDATE delivery_jobs SET payload_hash = ? WHERE idempotency_key = ?",
            (sha256_hash("different"), succeeded["idempotencyKey"]),
        )
        connection.execute(
            "UPDATE delivery_jobs SET target_profile = ? WHERE idempotency_key = ?",
            ("different-target", wrong_target["idempotencyKey"]),
        )

    report = rebuild_temporal_revisions(
        state_db=state_db,
        artifact_store=InMemorySessionMemoryArtifactStore(),
        project=PROJECT,
        limit=100,
        max_runtime_seconds=30,
    )

    assert report["planned_artifact_count"] == 0
    assert report["delivery_not_succeeded_count"] == 1
    assert report["delivery_hash_mismatch_count"] == 1
    assert report["delivery_scope_mismatch_count"] == 1
    assert report["mutation_performed"] is False


def test_payload_integrity_mismatch_fails_closed_and_report_is_public_safe(tmp_path: Path) -> None:
    state_db = _state_db(tmp_path)
    payload = _payload(
        chunk_id="private-chunk",
        body="private retained body",
        observed_at_start=DATE_A[0],
        observed_at_end=DATE_A[1],
    )
    _record_delivery(state_db, payload)
    with state_db.connect() as connection:
        row = connection.execute(
            "SELECT payload_json FROM delivery_payloads WHERE idempotency_key = ?",
            (payload["idempotencyKey"],),
        ).fetchone()
        decoded = json.loads(str(row["payload_json"]))
        decoded["payload"]["document"]["body"] = "tampered private body"
        connection.execute(
            "UPDATE delivery_payloads SET payload_json = ? WHERE idempotency_key = ?",
            (json.dumps(decoded), payload["idempotencyKey"]),
        )

    report = rebuild_temporal_revisions(
        state_db=state_db,
        artifact_store=InMemorySessionMemoryArtifactStore(),
        project=PROJECT,
        limit=100,
        max_runtime_seconds=30,
    )

    assert report["integrity_error_count"] == 1
    assert report["planned_artifact_count"] == 0
    encoded = json.dumps(report, sort_keys=True)
    assert SESSION_HASH not in encoded
    assert "private-chunk" not in encoded
    assert "private retained body" not in encoded
    assert report["raw_ids_printed"] is False
    assert report["raw_bodies_printed"] is False


def test_replay_is_additive_idempotent_and_resumable_by_exact_artifact_identity(
    tmp_path: Path,
) -> None:
    state_db = _state_db(tmp_path)
    _record_date_a_b(state_db)
    store = InMemorySessionMemoryArtifactStore()

    first = _execute_plan(state_db=state_db, store=store, limit=1)
    second = _execute_plan(state_db=state_db, store=store, limit=1)
    final = rebuild_temporal_revisions(
        state_db=state_db,
        artifact_store=store,
        project=PROJECT,
        limit=1,
        max_runtime_seconds=30,
    )

    assert first["inserted_artifact_count"] == 1
    assert second["inserted_artifact_count"] == 1
    assert final["total_remaining_artifact_count"] == 0
    assert final["planned_artifact_count"] == 0
    assert final["existing_artifact_count"] == 2
    assert final["mutation_performed"] is False
    assert final["current_materialization_rebuild_required"] is False


def test_exact_duplicate_delivery_does_not_create_an_extra_revision(tmp_path: Path) -> None:
    state_db = _state_db(tmp_path)
    first = _payload(
        chunk_id="chunk-duplicate",
        body="one exact temporal event",
        observed_at_start=DATE_A[0],
        observed_at_end=DATE_A[1],
        idempotency_key="wire-duplicate-one",
    )
    second = {**first, "idempotencyKey": "wire-duplicate-two"}
    _record_delivery(
        state_db,
        first,
        recorded_at=datetime(2026, 7, 9, 11, 0, tzinfo=timezone.utc),
    )
    _record_delivery(
        state_db,
        second,
        recorded_at=datetime(2026, 7, 9, 11, 1, tzinfo=timezone.utc),
    )

    report = rebuild_temporal_revisions(
        state_db=state_db,
        artifact_store=InMemorySessionMemoryArtifactStore(),
        project=PROJECT,
        limit=100,
        max_runtime_seconds=30,
    )

    assert report["succeeded_delivery_count"] == 2
    assert report["exact_duplicate_payload_count"] == 1
    assert report["planned_artifact_count"] == 1


def test_timeout_after_one_additive_write_is_explicit_and_resumable(tmp_path: Path) -> None:
    state_db = _state_db(tmp_path)
    _record_date_a_b(state_db)
    store = InMemorySessionMemoryArtifactStore()
    plan = rebuild_temporal_revisions(
        state_db=state_db,
        artifact_store=store,
        project=PROJECT,
        limit=100,
        max_runtime_seconds=30,
    )
    ticks = iter((0.0, 0.0, 0.0, 0.0, 31.0))

    partial = rebuild_temporal_revisions(
        state_db=state_db,
        artifact_store=store,
        project=PROJECT,
        limit=100,
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest=plan["plan_digest"],
        monotonic=lambda: next(ticks, 31.0),
    )
    remaining = rebuild_temporal_revisions(
        state_db=state_db,
        artifact_store=store,
        project=PROJECT,
        limit=100,
        max_runtime_seconds=30,
    )

    assert partial["status"] == "aborted_timeout"
    assert partial["inserted_artifact_count"] == 1
    assert partial["mutation_performed"] is True
    assert partial["current_materialization_rebuild_required"] is True
    assert remaining["total_remaining_artifact_count"] == 1


def test_execute_rejects_plan_drift_before_any_additive_write(tmp_path: Path) -> None:
    state_db = _state_db(tmp_path)
    _record_date_a_b(state_db)
    store = InMemorySessionMemoryArtifactStore()

    report = rebuild_temporal_revisions(
        state_db=state_db,
        artifact_store=store,
        project=PROJECT,
        limit=100,
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest="sha256:" + ("0" * 64),
    )

    assert report["status"] == "blocked_plan_drift"
    assert report["plan_drift_count"] == 1
    assert report["inserted_artifact_count"] == 0
    assert report["mutation_performed"] is False
    assert store.list_recent(project=PROJECT, limit=10) == []


def test_timeout_during_scan_aborts_before_planning_or_writing(tmp_path: Path) -> None:
    state_db = _state_db(tmp_path)
    _record_date_a_b(state_db)
    store = InMemorySessionMemoryArtifactStore()
    ticks = iter((0.0, 0.0, 31.0))

    report = rebuild_temporal_revisions(
        state_db=state_db,
        artifact_store=store,
        project=PROJECT,
        limit=100,
        max_runtime_seconds=30,
        execute=True,
        expected_plan_digest=sha256_hash("never-used"),
        monotonic=lambda: next(ticks, 31.0),
    )

    assert report["status"] == "aborted_timeout"
    assert report["timed_out"] is True
    assert report["planned_artifact_count"] == 0
    assert report["inserted_artifact_count"] == 0
    assert report["mutation_performed"] is False


def test_temporal_query_selects_latest_relevant_revision_not_latest_unrelated_revision() -> None:
    session_hash = sha256_hash("same-session")
    common = {
        "session_id_hash": session_hash,
        "project": PROJECT,
        "provider": "codex",
        "observed_at_start": DATE_A[0],
        "observed_at_end": DATE_A[1],
        "revision_observed_at_start": DATE_A[0],
        "revision_observed_at_end": DATE_A[1],
        "revision_observed_intervals": [DATE_A],
        "revision_temporal_evidence": "bounded",
    }
    relevant = SessionMemoryArtifact.from_summary(
        **common,
        summary="Session artifact revision one.",
        source_event_ids=["event-one"],
        source_revision=sha256_hash("revision-one"),
        search_term_hashes=[hash_payload("migration")],
        materialized_at="2026-07-09T11:00:00Z",
        materialization_revision=1,
    )
    unrelated = SessionMemoryArtifact.from_summary(
        **common,
        summary="Session artifact revision two.",
        source_event_ids=["event-one", "event-two"],
        source_revision=sha256_hash("revision-two"),
        search_term_hashes=[hash_payload("profile")],
        materialized_at="2026-07-09T12:00:00Z",
        materialization_revision=2,
    )
    store = InMemorySessionMemoryArtifactStore([relevant, unrelated])

    result = BrainReadService(artifact_store=store).brain_objects_query(
        repository=PROJECT,
        branch="main",
        query="migration",
        current_files=[],
        route="temporal_work_recall",
        as_of="2026-07-09T10:15:00Z",
    )

    objects = result["object_pack"]["objects"]
    assert len(objects) == 1
    assert objects[0]["payload"]["source_revision"] == relevant.source_revision
    assert result["object_pack"]["gaps"] == []


def test_neuron_knowledge_routes_rebuild_as_approval_gated_additive_repair() -> None:
    assert COMMAND_HANDLERS["couchdb-temporal-revision-rebuild"] is main
    assert COMMAND_METADATA["couchdb-temporal-revision-rebuild"] == {
        "runtime_category": "human_gated_additive_repair",
        "deletion_candidate": False,
        "live_mutation_requires_approval": True,
    }
    assert REBUILD_OPERATION == "couchdb_temporal_revision_rebuild"


def _write_approval(path: Path, argv: list[str]) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": "agent_knowledge_live_approval.v1",
                "operation": REBUILD_OPERATION,
                "operator_approval": {"approved": True},
                "redaction_required": True,
                "timeout_seconds": 30,
                "rollback_or_abort_criteria": [
                    "abort on artifact write or postcheck error"
                ],
                "command": {"argv": argv},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_cli_defaults_to_read_only_plan_and_execute_requires_exact_argv_approval(
    tmp_path: Path,
) -> None:
    state_db = _state_db(tmp_path)
    _record_date_a_b(state_db)
    ledger_path = state_db.path.parent / "ledger.sqlite3"
    Ledger(ledger_path)
    dry_argv = [
        "--state-db",
        str(state_db.path),
        "--ledger",
        str(ledger_path),
        "--project",
        PROJECT,
        "--limit",
        "100",
        "--max-runtime-seconds",
        "30",
    ]
    ledger_mtime_before = ledger_path.stat().st_mtime_ns

    with patch("sys.stdout", StringIO()) as output:
        assert main(dry_argv) == 0
        dry_report = json.loads(output.getvalue())
    assert dry_report["dry_run"] is True
    assert dry_report["planned_artifact_count"] == 2
    assert dry_report["mutation_performed"] is False
    assert ledger_path.stat().st_mtime_ns == ledger_mtime_before

    execute_argv = [
        *dry_argv,
        "--execute",
        "--expected-plan-digest",
        dry_report["plan_digest"],
        "--approval",
        "PLACEHOLDER",
    ]
    approval_path = state_db.path.parent / "approval.json"
    execute_argv[-1] = str(approval_path)
    _write_approval(approval_path, execute_argv)

    with patch("sys.stdout", StringIO()) as output:
        assert main(execute_argv) == 0
        applied = json.loads(output.getvalue())
    assert applied["inserted_artifact_count"] == 2
    assert applied["mutation_performed"] is True

    tampered_argv = [*execute_argv]
    tampered_argv[tampered_argv.index("100")] = "1"
    with patch("sys.stdout", StringIO()) as output:
        assert main(tampered_argv) == 2
        rejected = json.loads(output.getvalue())
    assert rejected["error"] == "approval_rejected"
    assert rejected["mutation_performed"] is False
