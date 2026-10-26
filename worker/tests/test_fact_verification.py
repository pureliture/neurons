"""실제 PG에서 관측 시각·검증 시각·만료 자동 재검토를 구분한다."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
import time

import psycopg
import pytest

from agent_knowledge.fact_correction_coordinator import FactCorrectionCoordinator
from agent_knowledge.fact_resolution import StewardFactCommit
from agent_knowledge.fact_source_lifecycle import FactSourceLifecycle
from agent_knowledge.fact_verification import FactVerificationService
from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
from test_fact_resolution import facts, coordinator, observation, invoke, set_current, head_card
from test_fact_source_lifecycle import seeded


def short_policy(c):
    c.rule = replace(c.rule, max_age_seconds=1)
    c.service = FactCorrectionCoordinator(store=c.f.store, git_reader=c.service.git_reader, policies=[c.rule])


def rows(f):
    with f.store._scope() as db:
        return db.execute("SELECT * FROM lbrain_fact_verifications ORDER BY checked_at").fetchall()


def await_expiry(f):
    deadline = time.monotonic()+5
    while True:
        with f.store._scope() as db:
            due = db.execute("SELECT expires_at<=clock_timestamp() AS due FROM lbrain_fact_verifications ORDER BY checked_at DESC LIMIT 1").fetchone()
        if due and due["due"]: return
        assert time.monotonic()<deadline, "verification did not expire"
        time.sleep(.02)


@pytest.mark.parametrize("observed_at", ["1970-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00", ""])
def test_source_clock_and_missing_time_do_not_forge_server_verification_lifetime(facts, observed_at):
    f = facts
    obs = replace(observation(f, 2), observed_at=observed_at)
    set_current(f, obs)
    before = datetime.now(timezone.utc)
    result = invoke(f, obs, "ready", commit=StewardFactCommit(field_path=f.commit.field_path,
        render_template=f.commit.render_template, policy_id=f.commit.policy_id, max_age_seconds=1))
    verified = rows(f)[0]
    assert verified["checked_at"]>=before
    assert (verified["expires_at"]-verified["checked_at"]).total_seconds() == 1
    assert result["verification"]["checked_at"] == verified["checked_at"].isoformat()
    card = head_card(f, result)
    assert card["typed_payload"]["observed_at"] == observed_at
    field = card["field_verifications"][f.commit.field_path]
    assert field["observed_at"] == observed_at and field["verified_at"] == verified["checked_at"].isoformat()
    assert card["fact_views"][0]["verification_state"] == "verified_current"
    with f.store._scope() as db:
        assert db.execute("SELECT observation->>'observed_at' AS stamp FROM lbrain_fact_observations").fetchone()["stamp"] == observed_at


def test_terminal_replay_cannot_extend_verification_but_new_independent_check_can(facts):
    f = facts
    obs = observation(f, 2)
    set_current(f, obs)
    first = invoke(f, obs, "ready")
    original = rows(f)[0]
    replay = invoke(f, obs, "ready")
    assert replay["duplicate"] and rows(f) == [original]
    assert replay["verification"] == first["verification"]
    fresh = invoke(f, obs, "ready", key="actual-new-check", target=head_card(f, first))
    assert fresh["action"] == "attach_evidence" and len(rows(f)) == 2
    assert rows(f)[1]["checked_at"]>original["checked_at"]
    with f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_observations").fetchone()["n"] == 1
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_assertions").fetchone()["n"] == 1
        assert db.execute("SELECT count(*) AS n FROM steward_card_decisions").fetchone()["n"] == 2


def test_expiry_changes_verification_status_without_falsely_reversing_value_or_revision(coordinator):
    c = coordinator
    short_policy(c); seeded(c)
    before = GraphFirstResolver(store=c.f.store).resolve(project=c.f.scope.project, mode="list")["items"][0]
    await_expiry(c.f)
    after = GraphFirstResolver(store=c.f.store).resolve(project=c.f.scope.project, mode="list")["items"][0]
    assert before["typed_payload"]["status_value"] == after["typed_payload"]["status_value"] == "ready"
    assert before["content_hash"] == after["content_hash"]
    assert before["fact_views"][0]["verification_state"] == "verified_current"
    assert after["fact_views"][0]["verification_state"] == "recheck_due"
    context = GraphFirstResolver(store=c.f.store).resolve(project=c.f.scope.project, mode="context")
    assert context["statuses"][0]["recheck_due_fields"] == [c.rule.field_path]
    health = FactSourceLifecycle(c.f.store).health(project=c.f.scope.project, overdue_grace=0)
    assert health["attention_required"] and health["verifications"]["overdue"] == 1


def test_due_dispatch_is_idempotent_and_same_fixed_observation_is_reverified(coordinator):
    c = coordinator
    short_policy(c); seeded(c)
    original = rows(c.f)[0]
    await_expiry(c.f)
    dispatcher = FactVerificationService(c.f.store)
    assert dispatcher.dispatch_once(project=c.f.scope.project)["scheduled"] == 1
    assert dispatcher.dispatch_once(project=c.f.scope.project)["scheduled"] == 0
    with c.f.store._scope() as db:
        task_id = db.execute("SELECT task_id FROM lbrain_fact_check_schedules").fetchone()["task_id"]
    result = c.service.worker(project=c.f.scope.project, worker_id="verification-worker").run_once(task_id=task_id)
    assert result["state"] == "resolved", result
    assert rows(c.f)[1]["checked_at"]>original["expires_at"]
    with c.f.store._scope() as db:
        schedule = db.execute("SELECT * FROM lbrain_fact_check_schedules").fetchone()
        assert schedule["state"] == "scheduled" and schedule["task_id"] is None
        assert schedule["verified_resolution_id"] == rows(c.f)[1]["resolution_id"]
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_observations").fetchone()["n"] == 1
        assert db.execute("SELECT count(*) AS n FROM steward_card_decisions").fetchone()["n"] == 2


def test_failed_source_check_cannot_renew_old_verification_and_resumes_same_task(coordinator):
    c = coordinator
    short_policy(c); seeded(c); await_expiry(c.f)
    FactVerificationService(c.f.store).dispatch_once(project=c.f.scope.project)
    with c.f.store._scope() as db:
        task = db.execute("SELECT task_id FROM lbrain_fact_check_schedules").fetchone()["task_id"]
    offline = c.root.with_name("verification-offline")
    c.root.rename(offline)
    worker = c.service.worker(project=c.f.scope.project, worker_id="verification-retry", retry_seconds=.1)
    try:
        assert worker.run_once(task_id=task)["state"] == "blocked"
        assert len(rows(c.f)) == 1
    finally: offline.rename(c.root)
    time.sleep(.12)
    assert worker.run_once(task_id=task)["state"] == "resolved"
    assert len(rows(c.f)) == 2


def test_bootstrap_never_fabricates_a_check_for_existing_authority(coordinator, monkeypatch):
    c = coordinator
    original_record = FactVerificationService.record_on
    # upgrade 전 검증 영수증이 없던 authority의 복원 fixture다.
    monkeypatch.setattr(FactVerificationService, "record_on", lambda *a, **k: None)
    seeded(c)
    monkeypatch.setattr(FactVerificationService, "record_on", original_record)
    assert not rows(c.f)
    health = FactSourceLifecycle(c.f.store).health(project=c.f.scope.project)
    assert health["verifications"]["unscheduled"] == 1 and health["attention_required"]
    assert FactVerificationService(c.f.store).dispatch_once(project=c.f.scope.project)["scheduled"] == 1
    assert not rows(c.f)
    with c.f.store._scope() as db:
        task = db.execute("SELECT task_id FROM lbrain_fact_check_schedules").fetchone()["task_id"]
    assert c.service.worker(project=c.f.scope.project, worker_id="bootstrap").run_once(task_id=task)["state"] == "resolved"
    assert len(rows(c.f)) == 1


def test_verification_history_is_immutable_and_cannot_bind_wrong_resolution(facts):
    f = facts
    obs = observation(f, 2); set_current(f, obs)
    first = invoke(f, obs, "ready")
    for sql in ("UPDATE lbrain_fact_verifications SET checked_at=clock_timestamp()", "DELETE FROM lbrain_fact_verifications"):
        with pytest.raises(psycopg.errors.CheckViolation, match="fact_verification_immutable"):
            with f.store.transaction() as db: db.execute(sql)
    delayed = observation(f, 1, current=False)
    history = invoke(f, delayed, "missing", key="history-only", target=head_card(f, first))
    assert history["action"] == "retain_history"
    with pytest.raises(psycopg.errors.CheckViolation, match="fact_verification_resolution_mismatch"):
        with f.store.transaction() as db:
            db.execute("""INSERT INTO lbrain_fact_verifications
                (resolution_id,fact_key,assertion_id,policy_id,max_age_seconds)
                VALUES (%s,%s,%s,'fixture-status',300)""",
                (history["resolution_id"], f.scope.key, history["assertion_id"]))


def test_database_uses_its_clock_even_if_insert_supplies_a_future_timestamp(facts, monkeypatch):
    f = facts
    original = FactVerificationService.record_on
    monkeypatch.setattr(FactVerificationService, "record_on", lambda *a, **k: None)
    obs = observation(f, 2); set_current(f, obs)
    result = invoke(f, obs, "ready")
    monkeypatch.setattr(FactVerificationService, "record_on", original)
    with f.store.transaction() as db:
        db.execute("""INSERT INTO lbrain_fact_verifications
            (resolution_id,fact_key,assertion_id,policy_id,max_age_seconds,checked_at,expires_at)
            VALUES (%s,%s,%s,'fixture-status',300,'2099-01-01','2199-01-01')""",
            (result["resolution_id"], f.scope.key, result["assertion_id"]))
        receipt = db.execute("SELECT *,abs(extract(epoch FROM clock_timestamp()-checked_at)) AS age FROM lbrain_fact_verifications").fetchone()
    assert receipt["age"]<5 and (receipt["expires_at"]-receipt["checked_at"]).total_seconds() == 300


def test_concurrent_dispatchers_cannot_duplicate_a_due_check(coordinator):
    from concurrent.futures import ThreadPoolExecutor
    c = coordinator
    short_policy(c); seeded(c); await_expiry(c.f)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: FactVerificationService(c.f.store).dispatch_once(project=c.f.scope.project), range(2)))
    assert sum(r.get("scheduled", 0) for r in results) == 1
    with c.f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_recheck_tasks WHERE request#>>'{trigger,kind}'='verification_due'").fetchone()["n"] == 1


def test_disabled_policy_waits_and_recovers_without_losing_expired_check(coordinator):
    c = coordinator
    short_policy(c); seeded(c); await_expiry(c.f)
    with c.f.store.transaction() as db: db.execute("UPDATE lbrain_fact_admission_rules SET active=FALSE")
    dispatcher = FactVerificationService(c.f.store)
    assert dispatcher.dispatch_once(project=c.f.scope.project, retry_seconds=.1)["blocked"] == 1
    assert len(rows(c.f)) == 1
    assert FactSourceLifecycle(c.f.store).health(project=c.f.scope.project)["verifications"]["blocked"] == 1
    with c.f.store.transaction() as db: db.execute("UPDATE lbrain_fact_admission_rules SET active=TRUE")
    time.sleep(.12)
    assert dispatcher.dispatch_once(project=c.f.scope.project)["scheduled"] == 1


def test_resolved_history_only_check_does_not_permanently_end_verification_schedule(coordinator, monkeypatch):
    c = coordinator
    short_policy(c); seeded(c); await_expiry(c.f)
    dispatcher = FactVerificationService(c.f.store)
    dispatcher.dispatch_once(project=c.f.scope.project)
    with c.f.store._scope() as db:
        task = db.execute("SELECT task_id FROM lbrain_fact_check_schedules").fetchone()["task_id"]
    # load 뒤 적용 시 source가 더는 현재가 아닌 경합을 주입한다.
    monkeypatch.setattr(c.service, "_current", lambda *a: False)
    assert c.service.worker(project=c.f.scope.project, worker_id="race").run_once(task_id=task)["state"] == "resolved"
    assert len(rows(c.f)) == 1
    dispatcher.dispatch_once(project=c.f.scope.project, retry_seconds=.1)
    time.sleep(.12)
    assert dispatcher.dispatch_once(project=c.f.scope.project)["scheduled"] == 1
    with c.f.store._scope() as db:
        assert db.execute("SELECT task_id FROM lbrain_fact_check_schedules").fetchone()["task_id"] != task


def test_verification_and_schedule_rollback_with_authority(coordinator, monkeypatch):
    c = coordinator
    original = FactVerificationService.record_on
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected_failure_after_verification")
    monkeypatch.setattr(FactVerificationService, "record_on", interrupted)
    task = c.service.submit(c.request)["changes"][0]["task_id"]
    assert c.service.worker(project=c.f.scope.project, worker_id="rollback").run_once(task_id=task)["state"] == "waiting"
    with c.f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM steward_card_decisions").fetchone()["n"] == 1
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_verifications").fetchone()["n"] == 0
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_check_schedules").fetchone()["n"] == 0


def test_expiry_worker_stop_monitor_and_automatic_restart_reverify_and_update(coordinator, tmp_path,
        policy_descriptor=None, additional_env=None, lease_seconds=.5):
    import json
    import os
    import signal
    import subprocess
    from pathlib import Path
    from test_correction_sources import git
    c = coordinator
    if policy_descriptor is None: short_policy(c)
    seeded(c)
    first = rows(c.f)[0]
    config = tmp_path / "verification-policy.json"
    config.write_text(json.dumps({"schema_version": "fact_correction_config.v1", "repositories": [{
        "repository": c.rule.scope.repository, "root": str(c.root), "branch_ref": c.rule.scope.branch}],
        "rules": [policy_descriptor if policy_descriptor is not None else asdict(c.rule)]}))
    config.chmod(0o600)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "lib"), "NEURON_LBRAIN_PGVECTOR_DSN": c.f.store.dsn,
        **(additional_env or {})}
    module = ["uv", "run", "--no-project", "-m", "agent_knowledge.fact_source_lifecycle"]
    argv = [*module, "worker", "--project", c.f.scope.project, "--config", str(config),
        "--iterations", "1000", "--poll-seconds", ".05", "--retry-seconds", ".1", "--lease-seconds", str(lease_seconds)]
    process = None
    def stop():
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)
    def wait_for(test):
        deadline = time.monotonic()+60
        while True:
            assert process.poll() is None, "verification worker exited"
            if test(): return
            assert time.monotonic()<deadline, "automatic verification timed out"
            time.sleep(.05)
    def failed():
        with c.f.store._scope() as db:
            return db.execute("SELECT value FROM lbrain_fact_scopes").fetchone()["value"] == "failed"
    with (tmp_path / "verification-worker.log").open("wb") as log:
        try:
            process = subprocess.Popen(argv, env=env, stdout=log, stderr=log, start_new_session=True)
            wait_for(lambda: len(rows(c.f))>=2)
            stop()
            await_expiry(c.f); time.sleep(.06)
            monitor = subprocess.run([*module, "monitor", "--project", c.f.scope.project,
                "--heartbeat-grace", ".05", "--overdue-grace", "0"], env=env, capture_output=True, timeout=20, check=True)
            health = json.loads(monitor.stdout)
            assert health["attention_required"] and not health["reviews"]["heartbeat_recent"]
            assert health["verifications"]["overdue"] or health["reviews"]["overdue"]
            (c.root / "facts.json").write_text('{"status":"failed"}')
            git(c.root, "add", "facts.json"); git(c.root, "commit", "-m", "검증 기한 후 실제 새 상태")
            process = subprocess.Popen(argv, env=env, stdout=log, stderr=log, start_new_session=True)
            wait_for(failed)
            stop()
            history = rows(c.f)
            assert history[0] == first and len(history)>=3
            with c.f.store._scope() as db:
                assert db.execute("SELECT count(*) AS n FROM steward_card_decisions").fetchone()["n"] == 3
        finally: stop()
