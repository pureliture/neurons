from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent_knowledge.rag_ingress.idempotency import IdempotencyOutcome, classify_idempotency
from agent_knowledge.rag_ingress.rag_ready_document import build_ingress_enqueue_payload, build_rag_ready_document
from agent_knowledge.rag_ingress.server_runtime import IngressJobQueue, RagIngressRuntime
from agent_knowledge.rag_ingress.state_db import (
    CommandResultSpec,
    DeliveryJobSpec,
    DomainRecordSpec,
    InjectedTransactionFailure,
    RAGIngressStateDB,
    StateDBError,
    ValidationRejected,
)
from agent_knowledge.spool import Spool


NOW = datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)


def _db(tmp_path) -> RAGIngressStateDB:
    return RAGIngressStateDB(tmp_path / "private" / "rag-ingress-state.sqlite")


def _payload(*, body: str = "# Canary\n\nbounded ingress document", key: str | None = None) -> dict:
    document = build_rag_ready_document(
        target_profile="transcript-memory",
        document_kind="conversation_chunk",
        source_namespace="codex",
        source_alias="workspace-index-advisor/session",
        privacy_class="private",
        body=body,
        filename="canary.md",
        metadata={"project": "workspace-index-advisor", "privacy_class": "private"},
    )
    payload = build_ingress_enqueue_payload(
        document,
        source={"provider": "codex", "source_alias": "workspace-index-advisor/session"},
    )
    if key is not None:
        payload["idempotencyKey"] = key
    return payload


def _job(
    job_id: str = "job_1",
    *,
    idempotency_key: str = "idem_1",
    payload_hash: str = "sha256:payload",
) -> DeliveryJobSpec:
    return DeliveryJobSpec(
        job_id=job_id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        target_profile="transcript-memory",
        document_kind="conversation_chunk",
    )


def _domain_record(domain_record_id: str = "domain_1") -> DomainRecordSpec:
    return DomainRecordSpec(
        domain_record_id=domain_record_id,
        domain_kind="delivery_projection",
        lifecycle_status="prepared",
        resource_id_hash="resource_1",
        payload_hash="sha256:payload",
        projection={"target_profile": "transcript-memory"},
    )


def _run_transaction(db: RAGIngressStateDB, *, inject_failure_at: str = ""):
    return db.command_transaction().execute(
        command_id="cmd_1",
        command_type="transcript_ingest",
        idempotency_key="idem_1",
        payload_hash="sha256:payload",
        result=CommandResultSpec(decision="accepted"),
        domain_records=[_domain_record()],
        delivery_jobs=[_job()],
        mutate=lambda tx: {"knowledge_item": 1},
        now=NOW,
        inject_failure_at=inject_failure_at,
    )


def test_state_db_creates_m2_tables(tmp_path):
    db = _db(tmp_path)

    assert db.list_rows("inbox_events") == []
    assert db.list_rows("commands") == []
    assert db.list_rows("command_results") == []
    assert db.list_rows("delivery_jobs") == []


def test_idempotency_classifier_distinguishes_duplicate_conflict_and_preserved_states():
    assert (
        classify_idempotency(None, idempotency_key="k", payload_hash="h").outcome
        == IdempotencyOutcome.ACCEPTED
    )
    assert (
        classify_idempotency(
            {"idempotency_key": "k", "payload_hash": "h"},
            idempotency_key="k",
            payload_hash="h",
        ).outcome
        == IdempotencyOutcome.DUPLICATE
    )
    assert (
        classify_idempotency(
            {"idempotency_key": "k", "payload_hash": "h1"},
            idempotency_key="k",
            payload_hash="h2",
        ).outcome
        == IdempotencyOutcome.CONFLICT
    )
    assert (
        classify_idempotency(
            {"idempotency_key": "k", "payload_hash": "h", "status": "replayable"},
            idempotency_key="k",
            payload_hash="h",
        ).outcome
        == IdempotencyOutcome.REPLAYABLE
    )


def test_inbox_shadow_records_accepted_duplicate_and_conflict(tmp_path):
    db = _db(tmp_path)
    first = _payload(key="idem-shared")
    duplicate = _payload(key="idem-shared")
    conflict = _payload(body="# Canary\n\nchanged", key="idem-shared")

    assert db.record_inbox_shadow(first, now=NOW).outcome == "accepted"
    assert db.record_inbox_shadow(duplicate, now=NOW + timedelta(seconds=1)).outcome == "duplicate"
    assert db.record_inbox_shadow(conflict, now=NOW + timedelta(seconds=2)).outcome == "conflict"

    assert [row["accept_outcome"] for row in db.list_rows("inbox_events")] == [
        "accepted",
        "duplicate",
        "conflict",
    ]


def test_command_transaction_commits_result_and_delivery_jobs_atomically(tmp_path):
    db = _db(tmp_path)

    result = _run_transaction(db)

    assert result.status == "completed"
    assert result.delivery_job_ids == ("job_1",)
    assert db.get_row("commands", "command_id", "cmd_1")["status"] == "completed"
    assert db.list_rows("command_results")[0]["decision"] == "accepted"
    assert db.get_row("delivery_jobs", "job_id", "job_1")["status"] == "pending"


@pytest.mark.parametrize("stage", ["claim", "validate", "mutate", "domain", "result", "jobs", "commit"])
def test_command_transaction_failure_injection_rolls_back_partial_state(tmp_path, stage):
    db = _db(tmp_path)

    with pytest.raises(InjectedTransactionFailure):
        _run_transaction(db, inject_failure_at=stage)

    assert db.list_rows("commands") == []
    assert db.list_rows("command_results") == []
    assert db.list_rows("domain_records") == []
    assert db.list_rows("delivery_jobs") == []


def test_command_transaction_validation_failure_records_decision_without_delivery_job(tmp_path):
    db = _db(tmp_path)

    result = db.command_transaction().execute(
        command_id="cmd_validation",
        command_type="transcript_ingest",
        idempotency_key="idem_validation",
        payload_hash="sha256:payload",
        result=CommandResultSpec(decision="accepted"),
        delivery_jobs=[_job("job_validation")],
        validate=lambda _tx: (_ for _ in ()).throw(ValidationRejected(error_class="bad_input")),
        now=NOW,
    )

    assert result.status == "validation_failed"
    assert db.get_row("commands", "command_id", "cmd_validation")["status"] == "validation_failed"
    assert db.list_rows("command_results")[0]["error_class"] == "bad_input"
    assert db.list_rows("delivery_jobs") == []


def test_inbox_consume_and_command_insert_are_one_transaction(tmp_path):
    db = _db(tmp_path)
    db.record_inbox_shadow(_payload(), event_id="inbox_1", now=NOW)

    with pytest.raises(InjectedTransactionFailure):
        db.command_transaction().execute(
            command_id="cmd_inbox",
            command_type="transcript_ingest",
            idempotency_key="idem_1",
            payload_hash="sha256:payload",
            result=CommandResultSpec(decision="accepted"),
            delivery_jobs=[_job("job_inbox")],
            inbox_event_id="inbox_1",
            now=NOW,
            inject_failure_at="claim",
        )

    assert db.get_row("inbox_events", "event_id", "inbox_1")["consumed_by_command_id"] == ""
    assert db.list_rows("commands") == []

    db.command_transaction().execute(
        command_id="cmd_inbox",
        command_type="transcript_ingest",
        idempotency_key="idem_1",
        payload_hash="sha256:payload",
        result=CommandResultSpec(decision="accepted"),
        delivery_jobs=[_job("job_inbox")],
        inbox_event_id="inbox_1",
        now=NOW,
    )

    inbox = db.get_row("inbox_events", "event_id", "inbox_1")
    assert inbox["consumed_by_command_id"] == "cmd_inbox"
    assert inbox["consumed_at"]


def test_command_transaction_commits_domain_records_with_result_and_delivery_job(tmp_path):
    db = _db(tmp_path)

    result = db.command_transaction().execute(
        command_id="cmd_domain",
        command_type="transcript_ingest",
        idempotency_key="idem_domain",
        payload_hash="sha256:payload",
        result=CommandResultSpec(decision="accepted"),
        domain_records=[_domain_record("domain_1")],
        delivery_jobs=[_job("job_domain", idempotency_key="delivery_domain")],
        now=NOW,
    )

    assert result.status == "completed"
    domain = db.get_domain_record("domain_1")
    assert domain["command_id"] == "cmd_domain"
    assert domain["version"] == 1
    assert domain["lifecycle_status"] == "prepared"
    result_payload = json.loads(db.list_rows("command_results")[0]["domain_versions_written"])
    assert result_payload["domain_records"][0]["domain_record_id"] == "domain_1"
    assert db.get_row("delivery_jobs", "job_id", "job_domain")["command_id"] == "cmd_domain"


def test_consume_inbox_with_command_adds_domain_projection_atomically(tmp_path):
    db = _db(tmp_path)
    db.record_inbox_shadow(_payload(), event_id="inbox_domain", now=NOW)

    db.consume_inbox_with_command(
        inbox_event_id="inbox_domain",
        command_id="cmd_inbox_domain",
        command_type="transcript_ingest",
        idempotency_key="idem_inbox_domain",
        payload_hash="sha256:payload",
        result=CommandResultSpec(decision="accepted"),
        domain_records=[_domain_record("domain_inbox")],
        delivery_jobs=[_job("job_inbox_domain", idempotency_key="delivery_inbox_domain")],
        resource_id_hash="resource_1",
        now=NOW,
    )

    assert db.get_row("inbox_events", "event_id", "inbox_domain")["consumed_by_command_id"] == "cmd_inbox_domain"
    assert db.get_domain_record("domain_inbox")["command_id"] == "cmd_inbox_domain"
    assert db.get_row("delivery_jobs", "job_id", "job_inbox_domain")["command_id"] == "cmd_inbox_domain"


def test_domain_record_failure_injection_rolls_back_inbox_command_and_outbox(tmp_path):
    db = _db(tmp_path)
    db.record_inbox_shadow(_payload(), event_id="inbox_rollback", now=NOW)

    with pytest.raises(InjectedTransactionFailure):
        db.consume_inbox_with_command(
            inbox_event_id="inbox_rollback",
            command_id="cmd_rollback",
            command_type="transcript_ingest",
            idempotency_key="idem_rollback",
            payload_hash="sha256:payload",
            result=CommandResultSpec(decision="accepted"),
            domain_records=[_domain_record("domain_rollback")],
            delivery_jobs=[_job("job_rollback", idempotency_key="delivery_rollback")],
            now=NOW,
            inject_failure_at="domain",
        )

    assert db.get_row("inbox_events", "event_id", "inbox_rollback")["consumed_by_command_id"] == ""
    assert db.list_rows("commands") == []
    assert db.list_rows("domain_records") == []
    assert db.list_rows("command_results") == []
    assert db.list_rows("delivery_jobs") == []


def test_command_lease_reclaim_and_stale_owner_rejection_are_recorded(tmp_path):
    db = _db(tmp_path)
    db.create_command(
        command_id="cmd_lease",
        command_type="transcript_ingest",
        idempotency_key="idem_lease",
        payload_hash="sha256:payload",
        now=NOW,
    )

    assert db.claim_command("cmd_lease", lease_owner="owner_1", now=NOW, lease_seconds=10)
    assert not db.claim_command("cmd_lease", lease_owner="owner_2", now=NOW + timedelta(seconds=5))
    assert db.claim_command("cmd_lease", lease_owner="owner_2", now=NOW + timedelta(seconds=11))
    assert not db.complete_claimed_command("cmd_lease", lease_owner="owner_1", now=NOW + timedelta(seconds=12))
    assert db.get_row("commands", "command_id", "cmd_lease")["last_error_class"] == "stale_owner_rejected"


def test_delivery_replayable_attempts_have_terminal_cap(tmp_path):
    db = _db(tmp_path)
    db.create_command(
        command_id="cmd_replay",
        command_type="transcript_ingest",
        idempotency_key="cmd_replay",
        payload_hash="sha256:payload",
        now=NOW,
    )
    db.create_delivery_job(
        job_id="job_replay",
        command_id="cmd_replay",
        idempotency_key="idem_replay",
        payload_hash="sha256:payload",
        target_profile="transcript-memory",
        document_kind="conversation_chunk",
        now=NOW,
    )

    assert db.record_replayable_attempt("job_replay", now=NOW, max_attempts=3) == "replayable"
    assert db.record_replayable_attempt("job_replay", now=NOW + timedelta(seconds=1), max_attempts=3) == "replayable"
    assert db.record_replayable_attempt("job_replay", now=NOW + timedelta(seconds=2), max_attempts=3) == "quarantined"
    row = db.get_row("delivery_jobs", "job_id", "job_replay")
    assert row["last_error_class"] == "replay_attempt_limit"
    assert row["next_retry_at"] == ""


def test_delivery_job_cannot_be_orphaned_from_committed_command(tmp_path):
    db = _db(tmp_path)

    with pytest.raises(StateDBError):
        db.create_delivery_job(
            job_id="job_orphan",
            command_id="cmd_missing",
            idempotency_key="idem_orphan",
            payload_hash="sha256:payload",
            target_profile="transcript-memory",
            document_kind="conversation_chunk",
            now=NOW,
        )

    assert db.list_rows("delivery_jobs") == []


def test_create_delivery_job_converges_duplicate_and_rejects_conflict(tmp_path):
    db = _db(tmp_path)
    db.create_command(
        command_id="cmd_a",
        command_type="transcript_ingest",
        idempotency_key="cmd_a",
        payload_hash="sha256:payload",
        now=NOW,
    )
    db.create_command(
        command_id="cmd_b",
        command_type="transcript_ingest",
        idempotency_key="cmd_b",
        payload_hash="sha256:payload",
        now=NOW,
    )
    db.create_delivery_job(
        job_id="job_a",
        command_id="cmd_a",
        idempotency_key="delivery_key",
        payload_hash="sha256:payload",
        target_profile="transcript-memory",
        document_kind="conversation_chunk",
        now=NOW,
    )
    db.create_delivery_job(
        job_id="job_duplicate",
        command_id="cmd_b",
        idempotency_key="delivery_key",
        payload_hash="sha256:payload",
        target_profile="transcript-memory",
        document_kind="conversation_chunk",
        now=NOW,
    )
    assert [row["job_id"] for row in db.list_rows("delivery_jobs")] == ["job_a"]

    with pytest.raises(StateDBError):
        db.create_delivery_job(
            job_id="job_conflict",
            command_id="cmd_b",
            idempotency_key="delivery_key",
            payload_hash="sha256:different",
            target_profile="transcript-memory",
            document_kind="conversation_chunk",
            now=NOW,
        )

    assert [row["job_id"] for row in db.list_rows("delivery_jobs")] == ["job_a"]


def test_command_transaction_duplicate_delivery_intent_converges_without_new_job(tmp_path):
    db = _db(tmp_path)
    db.command_transaction().execute(
        command_id="cmd_first",
        command_type="transcript_ingest",
        idempotency_key="cmd_first",
        payload_hash="sha256:payload",
        result=CommandResultSpec(decision="accepted"),
        delivery_jobs=[_job("job_first", idempotency_key="delivery_key")],
        now=NOW,
    )

    result = db.command_transaction().execute(
        command_id="cmd_second",
        command_type="transcript_ingest",
        idempotency_key="cmd_second",
        payload_hash="sha256:payload",
        result=CommandResultSpec(decision="accepted"),
        delivery_jobs=[_job("job_second", idempotency_key="delivery_key")],
        mutate=lambda _tx: (_ for _ in ()).throw(AssertionError("duplicate must not mutate")),
        now=NOW + timedelta(seconds=1),
    )

    assert result.delivery_job_ids == ("job_first",)
    assert [row["job_id"] for row in db.list_rows("delivery_jobs")] == ["job_first"]
    assert db.get_row("commands", "command_id", "cmd_second")["status"] == "completed"
    assert db.list_rows("command_results")[-1]["decision"] == "duplicate"


def test_command_transaction_conflicting_delivery_intent_quarantines_without_job_or_mutation(tmp_path):
    db = _db(tmp_path)
    db.command_transaction().execute(
        command_id="cmd_first",
        command_type="transcript_ingest",
        idempotency_key="cmd_first",
        payload_hash="sha256:payload",
        result=CommandResultSpec(decision="accepted"),
        delivery_jobs=[_job("job_first", idempotency_key="delivery_key", payload_hash="sha256:payload")],
        now=NOW,
    )

    result = db.command_transaction().execute(
        command_id="cmd_conflict",
        command_type="transcript_ingest",
        idempotency_key="cmd_conflict",
        payload_hash="sha256:different",
        result=CommandResultSpec(decision="accepted"),
        delivery_jobs=[_job("job_conflict", idempotency_key="delivery_key", payload_hash="sha256:different")],
        mutate=lambda _tx: (_ for _ in ()).throw(AssertionError("conflict must not mutate")),
        now=NOW + timedelta(seconds=1),
    )

    assert result.status == "quarantined"
    assert [row["job_id"] for row in db.list_rows("delivery_jobs")] == ["job_first"]
    assert db.get_row("commands", "command_id", "cmd_conflict")["status"] == "quarantined"
    last_result = db.list_rows("command_results")[-1]
    assert last_result["decision"] == "conflict"
    assert last_result["error_class"] == "idempotency_conflict"


def test_runtime_optional_inbox_shadow_runs_after_file_queue_persist_without_response_drift(tmp_path):
    db = _db(tmp_path)
    runtime = RagIngressRuntime(
        event_spool=Spool(tmp_path / "events"),
        job_queue=IngressJobQueue(tmp_path / "jobs"),
        inbox_shadow=lambda payload, job: db.record_inbox_shadow(payload, payload_ref=str(job.path), now=NOW),
    )

    response = runtime.enqueue_document(_payload())

    assert response == {
        "accepted": True,
        "status": "queued",
        "jobId": response["jobId"],
    }
    assert response["jobId"].startswith("job_")
    assert runtime.job_queue.depth_counts()["pending"] == 1
    inbox = db.list_rows("inbox_events")[0]
    assert inbox["accept_outcome"] == "accepted"
    assert inbox["payload_ref"].endswith(f"{response['jobId']}.json")


def test_runtime_inbox_shadow_failure_does_not_change_existing_ack_contract(tmp_path):
    runtime = RagIngressRuntime(
        event_spool=Spool(tmp_path / "events"),
        job_queue=IngressJobQueue(tmp_path / "jobs"),
        inbox_shadow=lambda _payload, _job: (_ for _ in ()).throw(RuntimeError("shadow down")),
    )

    response = runtime.enqueue_document(_payload())

    assert response["accepted"] is True
    assert response["status"] == "queued"
    assert response["jobId"].startswith("job_")
    queued_files = sorted((tmp_path / "jobs" / "pending").glob("*.json"))
    assert len(queued_files) == 1
    assert json.loads(queued_files[0].read_text(encoding="utf-8"))["idempotencyKey"]
