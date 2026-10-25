"""실제 PG에서 예약·소유권·중복 판단·장애 후 재예약을 검증한다."""
from concurrent.futures import ThreadPoolExecutor

import pytest
import psycopg

from agent_knowledge.postgres_store.pgvector_store import MemoryCard
from agent_knowledge.public_safe_util import sha256_text
from agent_knowledge.recheck_scheduler import (
    LeaseLost, PgRecheckQueue, RecheckBlocked, RecheckOutcome, RecheckSnapshot, RecheckWorker, inspect_recheck_health,
)


PROJECT = "recheck-synthetic"


@pytest.fixture
def queue(isolated_pg_store):
    isolated_pg_store.insert_card(MemoryCard(
        memory_id="recheck-original", project=PROJECT, card_type="status", title="검증 상태",
        summary="기존 상태", lifecycle_state="accepted", authorization_status="active",
        content_hash=sha256_text("original"),
    ))
    return PgRecheckQueue(isolated_pg_store)


def register(queue, **overrides):
    return queue.register(**{"project": PROJECT, "memory_id": "recheck-original",
        "request": {"model": "fixture-v1", "policy_version": "v1"}, "request_key": "same-conflict", **overrides})


def claim(queue, worker="worker-one"):
    return queue.claim(project=PROJECT, worker_id=worker)


def make_due(queue, task):
    with queue.store.transaction() as db:
        db.execute("UPDATE lbrain_recheck_tasks SET next_check_at=clock_timestamp()-interval '1 second' WHERE task_id=%s", (task["task_id"],))


def expire(queue, task):
    with queue.store.transaction() as db:
        db.execute("UPDATE lbrain_recheck_tasks SET lease_until=clock_timestamp()-interval '1 second' WHERE task_id=%s", (task["task_id"],))


def test_registration_is_scoped_idempotent_and_schedules_without_authority_change(queue):
    first = register(queue, delay_seconds=100)
    replay = register(queue, delay_seconds=0)
    assert first == replay and first["state"] == "queued" and first["next_check_at"]
    assert claim(queue) is None
    assert queue.store.get_card("recheck-original").summary == "기존 상태"
    with pytest.raises(ValueError, match="key conflict"):
        register(queue, request={"model": "changed-model"})
    with pytest.raises(ValueError, match="same-project"):
        register(queue, project="foreign-project")
    with pytest.raises(ValueError, match="same-project"):
        register(queue, memory_id="missing-original")


def test_concurrent_workers_cannot_claim_same_active_lease(queue):
    register(queue)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda index: claim(queue, f"worker-{index}"), range(6)))
    assert sum(result is not None for result in results) == 1


def test_same_conflict_concurrently_registered_has_one_task(queue):
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks = list(pool.map(lambda _: register(queue), range(4)))
    assert len({task["task_id"] for task in tasks}) == 1
    with queue.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_recheck_tasks").fetchone()["n"] == 1


def test_expired_worker_cannot_overwrite_new_owner_or_judgment(queue):
    register(queue)
    old = claim(queue)
    snapshot = RecheckSnapshot.from_data(old, {"sources": ["revision-a"]})
    expire(queue, old)
    new = claim(queue, "replacement-worker")
    assert new["generation"] == old["generation"] + 1
    with pytest.raises(LeaseLost):
        queue.finish(old, RecheckOutcome("resolved", "done", "decision:old"), fingerprint=snapshot.fingerprint)
    with pytest.raises(LeaseLost):
        queue.save_judgment(old, snapshot.fingerprint, {"action": "replace"})
    with pytest.raises(LeaseLost):
        with queue.owned_transaction(old):
            pytest.fail("만료된 작업이 authority transaction에 진입하면 안 된다")
    queue.finish(new, RecheckOutcome("waiting", "source_pending"), fingerprint=None)
    assert queue.get(old["task_id"])["state"] == "waiting"
    with queue.store._scope() as db:
        row = db.execute("SELECT state FROM lbrain_recheck_attempts WHERE task_id=%s AND generation=1", (old["task_id"],)).fetchone()
    assert row["state"] == "lease_expired"


def test_owned_transaction_prevents_competing_claim_while_authority_is_applied(queue):
    register(queue)
    task = claim(queue)
    with queue.owned_transaction(task):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(claim, queue, "competing-worker").result(timeout=3) is None


def test_authority_receipt_is_atomic_survives_reclaim_and_is_fenced(queue):
    register(queue)
    task = claim(queue)
    with pytest.raises(RuntimeError, match="rollback"):
        with queue.owned_transaction(task) as db:
            db.execute("UPDATE memory_cards SET summary='교정 값' WHERE memory_id='recheck-original'")
            queue.checkpoint_result(task, "decision:committed", connection=db)
            raise RuntimeError("rollback")
    assert queue.get(task["task_id"])["result_ref"] == ""
    assert queue.store.get_card("recheck-original").summary == "기존 상태"
    with queue.owned_transaction(task) as db:
        db.execute("UPDATE memory_cards SET summary='교정 값' WHERE memory_id='recheck-original'")
        queue.checkpoint_result(task, "decision:committed", connection=db)
    queue.finish(task, RecheckOutcome("waiting", "post_commit_error"), fingerprint=None)
    assert queue.get(task["task_id"])["result_ref"] == "decision:committed"
    make_due(queue, task)
    task = claim(queue)
    expire(queue, task)
    replacement = claim(queue, "replacement")
    assert replacement["result_ref"] == "decision:committed"
    assert queue.store.get_card("recheck-original").summary == "교정 값"
    with queue.store.transaction() as db:
        with pytest.raises(LeaseLost):
            queue.checkpoint_result(task, "decision:wrong", connection=db)
    with pytest.raises(ValueError, match="reference conflict"):
        with queue.owned_transaction(replacement) as db:
            queue.checkpoint_result(replacement, "decision:other", connection=db)


def test_waiting_rechecks_reuse_judgment_and_changed_evidence_triggers_new_judgment(queue):
    task = register(queue)
    data = {"revision": "old", "result": "conflicting"}
    calls = []

    def judge(task, snapshot):
        calls.append(snapshot.fingerprint)
        return {"choice": "resolve" if snapshot.data["result"] == "confirmed" else "wait"}

    def apply(task, snapshot, judgment):
        if judgment["choice"] == "resolve":
            return RecheckOutcome("resolved", "confirmed", "decision:durable")
        return RecheckOutcome("waiting", "source_conflict")

    def worker(name):
        return RecheckWorker(queue=queue, project=PROJECT, worker_id=name,
            load=lambda task: RecheckSnapshot.from_data(task, dict(data)), judge=judge, apply=apply)

    assert worker("initial").run_once()["state"] == "waiting"
    make_due(queue, task)
    # 다른 worker 인스턴스여도 같은 근거의 판단은 DB에서 재사용한다.
    assert worker("after-restart").run_once()["judgment_reused"]
    assert len(calls) == 1
    data.update(revision="new", result="confirmed")
    make_due(queue, task)
    assert worker("after-ingest").run_once()["state"] == "resolved"
    assert len(calls) == 2
    assert queue.get(task["task_id"])["result_ref"] == "decision:durable"


def test_crash_after_judgment_preserves_it_for_reclaimed_worker(queue):
    task = register(queue)
    crashed = claim(queue)
    snap = RecheckSnapshot.from_data(crashed, {"revision": "same-evidence"})
    queue.save_judgment(crashed, snap.fingerprint, {"choice": "wait"})
    expire(queue, crashed)

    def forbidden_judge(*args):
        pytest.fail("이미 저장한 판단을 재시작 때문에 다시 호출하면 안 된다")

    worker = RecheckWorker(queue=queue, project=PROJECT, worker_id="recovered",
        load=lambda task: RecheckSnapshot.from_data(task, snap.data), judge=forbidden_judge,
        apply=lambda *args: RecheckOutcome("waiting", "source_conflict"))
    result = worker.run_once()
    assert result["judgment_reused"] and queue.get(task["task_id"])["attempt_count"] == 2


def test_source_failure_is_rescheduled_and_does_not_leak_exception_payload(queue):
    task = register(queue)

    def source_down(task):
        raise ConnectionError("synthetic private content must not enter audit")

    worker = RecheckWorker(queue=queue, project=PROJECT, worker_id="source-down", load=source_down,
        judge=lambda *args: pytest.fail("근거를 읽지 못했는데 판단하면 안 된다"),
        apply=lambda *args: pytest.fail("근거를 읽지 못했는데 반영하면 안 된다"), retry_seconds=10)
    result = worker.run_once()
    stored = queue.get(task["task_id"])
    assert result["state"] == "waiting" and stored["next_check_at"] > stored["last_check_at"]
    assert stored["reason_code"] == "handler_connectionerror"
    assert "synthetic private content" not in str(stored)
    assert queue.store.get_card("recheck-original").summary == "기존 상태"


def test_bad_snapshot_fingerprint_cannot_reuse_prior_judgment(queue):
    task = register(queue)
    worker = RecheckWorker(queue=queue, project=PROJECT, worker_id="bad-source",
        load=lambda task: RecheckSnapshot(sha256_text("forged"), {"revision": "actual"}),
        judge=lambda *args: pytest.fail("위조된 fingerprint로 판단하면 안 된다"),
        apply=lambda *args: pytest.fail("위조된 fingerprint로 반영하면 안 된다"))
    assert worker.run_once()["reason_code"] == "handler_valueerror"
    assert queue.get(task["task_id"])["state"] == "waiting"


def test_independent_monitor_detects_stopped_worker_and_expired_lease(queue):
    task = register(queue)
    assert inspect_recheck_health(queue, project=PROJECT)["stalled"]
    queue.heartbeat(project=PROJECT, worker_id="running")
    assert not inspect_recheck_health(queue, project=PROJECT)["attention_required"]
    leased = claim(queue)
    expire(queue, leased)
    assert inspect_recheck_health(queue, project=PROJECT)["expired_leases"] == 1
    assert inspect_recheck_health(queue, project=PROJECT)["attention_required"]
    with queue.store.transaction() as db:
        db.execute("UPDATE lbrain_recheck_heartbeats SET last_seen_at=clock_timestamp()-interval '1 hour'")
    assert not inspect_recheck_health(queue, project=PROJECT)["heartbeat_recent"]


def test_blocked_job_remains_visible_and_cannot_be_silently_completed(queue):
    task = register(queue)
    lease = claim(queue)
    with pytest.raises(ValueError, match="durable result"):
        queue.finish(lease, RecheckOutcome("resolved", "unproven"), fingerprint=None)
    queue.finish(lease, RecheckOutcome("blocked", "configuration_missing"), fingerprint=None)
    assert claim(queue) is None
    assert queue.get(task["task_id"])["state"] == "blocked"
    assert inspect_recheck_health(queue, project=PROJECT)["blocked"] == 1
    assert inspect_recheck_health(queue, project=PROJECT)["attention_required"]
    make_due(queue, task)
    assert claim(queue, "configured-worker") is not None


def test_configuration_block_is_automatically_rechecked_instead_of_becoming_terminal(queue):
    task = register(queue)
    configured = False

    def load(task):
        if not configured:
            raise RecheckBlocked("configuration_missing")
        return RecheckSnapshot.from_data(task, {"revision": "confirmed"})

    worker = RecheckWorker(queue=queue, project=PROJECT, worker_id="config-aware", load=load,
        judge=lambda *args: {"choice": "apply"},
        apply=lambda *args: RecheckOutcome("resolved", "confirmed", "decision:confirmed"))
    assert worker.run_once()["state"] == "blocked"
    assert queue.get(task["task_id"])["next_check_at"]
    configured = True
    make_due(queue, task)
    assert worker.run_once()["state"] == "resolved"


def test_partial_finish_failure_rolls_back_task_and_attempt_together(queue):
    task = register(queue)
    owned = claim(queue)
    with queue.store.transaction() as db:
        db.execute("""CREATE FUNCTION reject_attempt_finish() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic finish failure'; END $$;
            CREATE TRIGGER reject_attempt_finish BEFORE UPDATE ON lbrain_recheck_attempts
            FOR EACH ROW EXECUTE FUNCTION reject_attempt_finish();""")
    with pytest.raises(psycopg.Error):
        queue.finish(owned, RecheckOutcome("resolved", "confirmed", "decision:confirmed"), fingerprint=None)
    assert queue.get(task["task_id"])["state"] == "leased"
    assert queue.get(task["task_id"])["result_ref"] == ""
    with queue.store.transaction() as db:
        db.execute("DROP TRIGGER reject_attempt_finish ON lbrain_recheck_attempts; DROP FUNCTION reject_attempt_finish();")
    queue.finish(owned, RecheckOutcome("resolved", "confirmed", "decision:confirmed"), fingerprint=None)
    assert queue.get(task["task_id"])["state"] == "resolved"


def test_storage_unavailability_is_not_reported_as_healthy():
    class Unavailable:
        def health(self, **kwargs):
            raise ConnectionError("synthetic private connection description")

    assert inspect_recheck_health(Unavailable(), project=PROJECT) == {
        "status": "unavailable", "attention_required": True, "reason_code": "recheck_storage_unavailable"}
