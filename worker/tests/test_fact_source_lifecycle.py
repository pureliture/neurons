"""실제 PG에서 근거 참조 보존과 철회 전파를 검사한다."""
from dataclasses import asdict, replace

import psycopg
from psycopg.types.json import Jsonb
import pytest

from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
from agent_knowledge.fact_source_lifecycle import FactSourceLifecycle
from agent_knowledge.fact_resolution import FactResolutionError
from agent_knowledge.public_safe_util import hash_payload, sha256_text
from test_fact_resolution import facts, coordinator, observation, invoke, set_current, head_card


def seeded(c):
    task = c.service.submit(c.request)["changes"][0]["task_id"]
    assert c.service.worker(project=c.f.scope.project, worker_id="source-seed").run_once(task_id=task)["state"] == "resolved"
    with c.f.store._scope() as db:
        source = db.execute("SELECT * FROM lbrain_fact_source_receipts").fetchone()
    return source


def test_correction_history_prevents_receipt_deletion(coordinator):
    c = coordinator
    source = seeded(c)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with c.f.store.transaction() as db:
            db.execute("DELETE FROM lbrain_fact_source_receipts WHERE source_ref_id=%s", (source["source_ref_id"],))
    with c.f.store._scope() as db:
        assert db.execute("SELECT content_hash FROM lbrain_fact_source_receipts").fetchone()["content_hash"] == source["content_hash"]


def test_source_content_cannot_be_overwritten_in_place(coordinator):
    c = coordinator
    source = seeded(c)
    with pytest.raises(psycopg.errors.CheckViolation, match="fact_source_immutable"):
        with c.f.store.transaction() as db:
            db.execute("UPDATE lbrain_fact_source_receipts SET redacted_content=%s WHERE source_ref_id=%s",
                ("수정된 원문", source["source_ref_id"]))


def test_revocation_is_visible_to_new_reads_before_recheck_worker_runs(coordinator):
    c = coordinator
    source = seeded(c)
    with c.f.store.transaction() as db:
        db.execute("UPDATE lbrain_fact_source_receipts SET state='revoked' WHERE source_ref_id=%s", (source["source_ref_id"],))
    page = GraphFirstResolver(store=c.f.store).resolve(project=c.f.scope.project, mode="list")
    assert not page["items"] and page["metadata"]["source_evidence_gap_fields"] == 1
    with c.f.store._scope() as db:
        task_id = db.execute("SELECT task_id FROM lbrain_recheck_tasks").fetchone()["task_id"]
    status = c.service.result(task_id)["resolution"]
    assert status["decision_time_current"]["state"] == "current" and status["current"]["state"] == "invalidated"


def test_observation_without_saved_source_cannot_enter_authority(facts):
    f = facts
    obs = observation(f, 2)
    obs = replace(obs, proof={"source_ref_id": "source-not-registered"})
    with pytest.raises(psycopg.errors.CheckViolation, match="fact_observation_source_required"):
        with f.store.transaction() as db:
            db.execute("""INSERT INTO lbrain_fact_observations
                (version_id,observation_id,project,source_id,source_revision,interpretation_version,observation)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (obs.version_id, obs.observation_id, obs.project, obs.source_id, obs.source_revision,
                    obs.interpretation_version, Jsonb(asdict(obs))))


def test_revocation_is_idempotent_and_cannot_cross_project_or_revision(coordinator):
    c = coordinator
    source = seeded(c)
    service = FactSourceLifecycle(c.f.store)
    args = dict(project=c.f.scope.project, source_ref_id=source["source_ref_id"],
        expected_content_hash=source["content_hash"], request_key="withdraw")
    with pytest.raises(FactResolutionError, match="source_project_mismatch"):
        service.revoke(**{**args, "project": "other-project"})
    with pytest.raises(FactResolutionError, match="source_revision_conflict"):
        service.revoke(**{**args, "expected_content_hash": sha256_text("not-the-source")})
    generation = c.f.store.fact_project_generation(c.f.scope.project)
    result = service.revoke(**args)
    assert service.revoke(**args) == {**result, "duplicate": True}
    assert c.f.store.fact_project_generation(c.f.scope.project) == generation+1
    with c.f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_source_events").fetchone()["n"] == 1
    with pytest.raises(FactResolutionError, match="source_command_key_conflict"):
        service.revoke(**{**args, "expected_content_hash": sha256_text("changed-request")})


def test_withdrawal_dispatches_once_then_recovers_from_new_current_source(coordinator):
    import time
    from test_correction_sources import git
    c = coordinator
    source = seeded(c)
    service = FactSourceLifecycle(c.f.store)
    service.revoke(project=c.f.scope.project, source_ref_id=source["source_ref_id"],
        expected_content_hash=source["content_hash"], request_key="withdraw")
    assert service.health(project=c.f.scope.project)["pending_events"] == 1
    dispatched = service.dispatch_once(project=c.f.scope.project, retry_seconds=.1)
    assert dispatched["scheduled"] == 1
    assert service.dispatch_once(project=c.f.scope.project)["scheduled"] == 0
    with c.f.store._scope() as db:
        target = db.execute("SELECT * FROM lbrain_fact_source_targets").fetchone()
        assert db.execute("SELECT state FROM lbrain_fact_scopes").fetchone()["state"] == "invalidated"
    worker = c.service.worker(project=c.f.scope.project, worker_id="source-recheck", retry_seconds=.1)
    first = worker.run_once(task_id=target["task_id"])
    assert first["state"] == "blocked" and first["reason_code"] == "source_receipt_unavailable"
    assert not GraphFirstResolver(store=c.f.store).resolve(project=c.f.scope.project, mode="context")["items"]
    (c.root / "facts.json").write_text('{"status":"failed"}')
    git(c.root, "add", "facts.json"); git(c.root, "commit", "-m", "철회 후 독립 현재 근거")
    time.sleep(.12)
    result = worker.run_once(task_id=target["task_id"])
    assert result["state"] == "resolved", result
    assert c.service.result(target["task_id"])["resolution"]["applied_value"] == "failed"
    assert not service.health(project=c.f.scope.project)["attention_required"]
    assert c.f.store.get_steward_card(c.f.old["memory_id"])["currentness"] == "superseded"
    with c.f.store._scope() as db:
        assert db.execute("SELECT state FROM lbrain_fact_source_receipts WHERE source_ref_id=%s",
            (source["source_ref_id"],)).fetchone()["state"] == "revoked"


def test_false_current_callback_cannot_promote_a_revoked_source(facts):
    f = facts
    obs = observation(f, 2)
    set_current(f, obs)
    first = invoke(f, obs, "ready")
    with f.store.transaction() as db:
        db.execute("UPDATE lbrain_fact_source_receipts SET state='revoked' WHERE source_ref_id=%s", (obs.proof["source_ref_id"],))
    # callback은 아직 True라고 주장해도 DB의 실제 source 유효성이 우선한다.
    result = invoke(f, obs, "failed", key="invalid-again", target=head_card(f, first))
    assert not result["authority_write_performed"] and result["current"]["value"] == "ready"


def test_orphan_cleanup_is_atomic_but_referenced_history_cannot_be_cleaned(facts):
    f = facts
    orphan = observation(f, 9)
    with f.store.transaction() as db:
        db.execute("""INSERT INTO lbrain_fact_observations
            (version_id,observation_id,project,source_id,source_revision,interpretation_version,observation)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""", (orphan.version_id, orphan.observation_id, orphan.project,
                orphan.source_id, orphan.source_revision, orphan.interpretation_version, Jsonb(asdict(orphan))))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with f.store.transaction() as db:
            db.execute("DELETE FROM lbrain_fact_observation_sources WHERE observation_version=%s", (orphan.version_id,))
    with f.store.transaction() as db:
        db.execute("DELETE FROM lbrain_fact_observations WHERE version_id=%s", (orphan.version_id,))
        db.execute("DELETE FROM lbrain_fact_observation_sources WHERE observation_version=%s", (orphan.version_id,))
        db.execute("DELETE FROM lbrain_fact_source_receipts WHERE source_ref_id=%s", (orphan.proof["source_ref_id"],))
    obs = observation(f, 2)
    set_current(f, obs)
    invoke(f, obs, "ready")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with f.store.transaction() as db:
            db.execute("DELETE FROM lbrain_fact_observation_sources WHERE observation_version=%s", (obs.version_id,))
            db.execute("DELETE FROM lbrain_fact_observations WHERE version_id=%s", (obs.version_id,))
    f.store.execute_ddl()


def test_real_lifecycle_process_restart_and_independent_monitor_recover_same_review(coordinator, tmp_path,
        policy_descriptor=None, additional_env=None, lease_seconds=.5):
    import json
    import os
    import signal
    import subprocess
    import time
    from pathlib import Path
    from test_correction_sources import git
    c = coordinator
    source = seeded(c)
    lifecycle = FactSourceLifecycle(c.f.store)
    lifecycle.revoke(project=c.f.scope.project, source_ref_id=source["source_ref_id"],
        expected_content_hash=source["content_hash"], request_key="daemon-withdraw")
    config = tmp_path / "source-policy.json"
    config.write_text(json.dumps({"schema_version": "fact_correction_config.v1", "repositories": [{
        "repository": c.rule.scope.repository, "root": str(c.root), "branch_ref": c.rule.scope.branch}],
        "rules": [policy_descriptor if policy_descriptor is not None else asdict(c.rule)]}, ensure_ascii=False))
    config.chmod(0o600)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "lib"), "NEURON_LBRAIN_PGVECTOR_DSN": c.f.store.dsn}
    env.update(additional_env or {})
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
    def await_state(state):
        deadline = time.monotonic()+max(15, lease_seconds+20)
        while True:
            assert process.poll() is None, "lifecycle worker exited"
            with c.f.store._scope() as db:
                row = db.execute("SELECT task_id FROM lbrain_fact_source_targets WHERE task_id IS NOT NULL").fetchone()
            if row and c.service.queue.get(row["task_id"])["state"] == state: return row["task_id"]
            assert time.monotonic()<deadline, "lifecycle automatic transition timed out"
            time.sleep(.05)
    with (tmp_path / "source-worker.log").open("wb") as log:
        try:
            process = subprocess.Popen(argv, env=env, stdout=log, stderr=log, start_new_session=True)
            task_id = await_state("blocked")
            stop()
            time.sleep(.06)
            monitor = subprocess.run([*module, "monitor", "--project", c.f.scope.project,
                "--heartbeat-grace", ".05", "--overdue-grace", "0"], env=env, capture_output=True, timeout=20, check=True)
            health = json.loads(monitor.stdout)
            assert health["attention_required"] and not health["reviews"]["heartbeat_recent"]
            (c.root / "facts.json").write_text('{"status":"failed"}')
            git(c.root, "add", "facts.json"); git(c.root, "commit", "-m", "worker 중단 중 대체 근거")
            process = subprocess.Popen(argv, env=env, stdout=log, stderr=log, start_new_session=True)
            assert await_state("resolved") == task_id
            assert c.service.result(task_id)["resolution"]["applied_value"] == "failed"
            with c.f.store._scope() as db:
                assert db.execute("SELECT count(*) AS n FROM lbrain_fact_source_targets").fetchone()["n"] == 1
                assert db.execute("SELECT count(*) AS n FROM steward_card_decisions").fetchone()["n"] == 3
        finally: stop()


def test_gc_plan_before_new_assertion_cannot_delete_its_evidence(facts):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    f = facts
    obs = observation(f, 2)
    set_current(f, obs)
    with f.store.transaction() as db:
        db.execute("""INSERT INTO lbrain_fact_observations
            (version_id,observation_id,project,source_id,source_revision,interpretation_version,observation)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""", (obs.version_id, obs.observation_id, obs.project,
                obs.source_id, obs.source_revision, obs.interpretation_version, Jsonb(asdict(obs))))
    plan_ready, start_gc, written, finish = (threading.Event() for _ in range(4))
    gc_pid = []
    def clean():
        try:
            with f.store.transaction() as db:
                gc_pid.append(db.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
                assert db.execute("SELECT count(*) AS n FROM lbrain_fact_assertions WHERE observation_version=%s",
                    (obs.version_id,)).fetchone()["n"] == 0
                plan_ready.set()
                assert start_gc.wait(5)
                db.execute("DELETE FROM lbrain_fact_observations WHERE version_id=%s", (obs.version_id,))
                db.execute("DELETE FROM lbrain_fact_observation_sources WHERE observation_version=%s", (obs.version_id,))
                db.execute("DELETE FROM lbrain_fact_source_receipts WHERE source_ref_id=%s", (obs.proof["source_ref_id"],))
            return "deleted"
        except psycopg.errors.ForeignKeyViolation: return "rejected"
    def commit(**arguments):
        result = f.commit(**arguments)
        written.set()
        assert finish.wait(5)
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        cleaner = pool.submit(clean)
        assert plan_ready.wait(5)
        corrector = pool.submit(lambda: invoke(f, obs, "ready", commit=commit))
        try:
            assert written.wait(5)
            start_gc.set()
            deadline = time.monotonic()+3
            while True:
                with f.store._scope() as db:
                    waiting = db.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (gc_pid[0],)).fetchone()
                if waiting and waiting["wait_event_type"] == "Lock": break
                assert time.monotonic()<deadline, "GC did not wait for the authority FK lock"
                time.sleep(.02)
        finally:
            start_gc.set(); finish.set()
        assert corrector.result(timeout=10)["current"]["value"] == "ready"
        assert cleaner.result(timeout=10) == "rejected"
    with f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_observation_sources").fetchone()["n"] == 1
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_source_receipts").fetchone()["n"] == 1


def test_independent_remaining_support_keeps_value_and_exposes_only_valid_current_evidence(facts):
    f = facts
    a, b = observation(f, 2), observation(f, 1, source="independent")
    set_current(f, a); set_current(f, b)
    first = invoke(f, a, "ready")
    assert invoke(f, b, "ready", key="independent", target=head_card(f, first))["action"] == "attach_evidence"
    lifecycle = FactSourceLifecycle(f.store)
    lifecycle.revoke(project=f.scope.project, source_ref_id=a.proof["source_ref_id"],
        expected_content_hash=a.content_hash, request_key="withdraw-one-of-two")
    assert lifecycle.dispatch_once(project=f.scope.project)["scheduled"] == 0
    page = GraphFirstResolver(store=f.store).resolve(project=f.scope.project, mode="list")
    assert page["items"][0]["typed_payload"]["status_value"] == "ready"
    view = page["items"][0]["fact_views"][0]
    assert {ref["source_ref_id"] for ref in view["current_evidence"]} == {b.proof["source_ref_id"]}
    projected = f.resolver.project_cards([f.store.get_steward_card(first["current"]["memory_id"])])[0]
    assert projected["field_verifications"]["typed_payload.status_value"]["source_hash"] == hash_payload([
        {**b.proof, "content_hash": b.content_hash, "observed_at": b.observed_at}])
    with f.store._scope() as db:
        assert db.execute("SELECT state FROM lbrain_fact_source_targets").fetchone()["state"] == "covered"
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_source_receipts").fetchone()["n"] == 2
    assert not lifecycle.health(project=f.scope.project)["attention_required"]


def test_invalid_cards_do_not_consume_pages_or_hide_independent_valid_cards(facts):
    import json
    from agent_knowledge.postgres_store.pgvector_store import make_dummy_vector, DEFAULT_EMBEDDING_MODEL
    from agent_knowledge.session_memory.brain_steward import BrainStewardService
    f = facts
    steward = BrainStewardService(f.ledger, pgvector_store=f.store, allow_restricted=True)
    managed = []
    for index in range(4):
        span = {**f.span, "title": f"독립 상태 {index}", "source_ref": {"source_id": f"independent-{index}"},
            "content_hash": sha256_text(f"independent-{index}")}
        target_id = steward.candidate_create(source_span=span)["memory_id"]
        steward.candidate_approve(candidate_memory_id=target_id, approved_by="fixture", decision_id=f"seed-{index}")
        obs = observation(f, 2, source=f"independent-{index}")
        set_current(f, obs)
        result = invoke(f, obs, "ready", key=f"independent-{index}", target=f.store.get_steward_card(target_id),
            scope=replace(f.scope, subject=f"independent-{index}"))
        managed.append((result["current"]["memory_id"], obs))
    # 예전 일반 seed는 이 검사의 조회 대상에서 제외한다.
    with f.store.transaction() as db:
        db.execute("UPDATE memory_cards SET authorization_status='disabled' WHERE memory_id=%s", (f.old["memory_id"],))
    managed.sort()
    vector = make_dummy_vector(42)
    with f.store.transaction() as db:
        db.execute("""UPDATE memory_cards SET embedding=%s::halfvec,embedding_model=%s,
            embedding_state='ready',embedding_revision=1 WHERE memory_id=ANY(%s)""",
            (json.dumps(vector), DEFAULT_EMBEDDING_MODEL, [m[0] for m in managed]))
    before = f.store.list_authorized_cards(project=f.scope.project)
    lifecycle = FactSourceLifecycle(f.store)
    for memory_id, obs in managed[:-1]:
        lifecycle.revoke(project=f.scope.project, source_ref_id=obs.proof["source_ref_id"],
            expected_content_hash=obs.content_hash, request_key=memory_id)
    assert [c["memory_id"] for c in f.store.list_authorized_cards(project=f.scope.project, limit=1)] == [managed[-1][0]]
    ranked = f.store.hybrid_search(project=f.scope.project, query_vector=vector, limit=1)
    assert [c["memory_id"] for c in ranked] == [managed[-1][0]]
    # 검색과 철회가 경합해도 read snapshot이 영향받지 않은 card는 보존한다.
    view = f.resolver.read_view(project=f.scope.project, cards=before)
    assert [c["memory_id"] for c in view["cards"]] == [managed[-1][0]]
    assert view["metadata"]["source_evidence_gap_fields"] == 3
    for mode in ("list", "context"):
        page = GraphFirstResolver(store=f.store).resolve(project=f.scope.project, mode=mode, limit=1)
        assert [c["id"] for c in page["items"]] == [managed[-1][0]], page
        assert page["metadata"]["source_evidence_gap_fields"] == 3


def _two_fields(f):
    from agent_knowledge.session_memory.brain_steward import BrainStewardService
    from agent_knowledge.fact_resolution import StewardFactCommit
    steward = BrainStewardService(f.ledger, pgvector_store=f.store, allow_restricted=True)
    span = {**f.span, "title": "두 사실 상태", "redacted_summary": "기능 상태: missing; 배포 상태: pending",
        "typed_payload": {**f.span["typed_payload"], "deployment_status": "pending"},
        "source_ref": {"source_id": "two-facts"}, "content_hash": sha256_text("two-facts")}
    target_id = steward.candidate_create(source_span=span)["memory_id"]
    steward.candidate_approve(candidate_memory_id=target_id, approved_by="fixture", decision_id="seed-two")
    with f.store.transaction() as db:
        db.execute("UPDATE memory_cards SET authorization_status='disabled' WHERE memory_id=%s", (f.old["memory_id"],))
    f.old = f.store.get_steward_card(target_id)
    a, b = observation(f, 2, source="code"), observation(f, 3, source="deployment")
    set_current(f, a); set_current(f, b)
    invoke(f, a, "ready", target=f.old)
    result = f.resolver.reconcile(scope=replace(f.scope, predicate="deployment_status"),
        memory_id=f.old["memory_id"], field_path="typed_payload.deployment_status",
        render_template="배포 상태: {value}", expected_content_hash=f.old["content_hash"],
        value="deployed", observation=b, request_key="deployment", request_hash=sha256_text("deployment"),
        requested_value="deployed", is_current=lambda obs: f.current.get(obs.source_id) == (obs.source_revision, obs.interpretation_version),
        compare=lambda old, new: "unknown", commit=StewardFactCommit(field_path="typed_payload.deployment_status",
            render_template="배포 상태: {value}", policy_id="deployment"))
    return a, b, result


def test_one_revoked_field_does_not_hide_the_other_verified_field(facts):
    f = facts
    a, b, result = _two_fields(f)
    FactSourceLifecycle(f.store).revoke(project=f.scope.project, source_ref_id=a.proof["source_ref_id"],
        expected_content_hash=a.content_hash, request_key="withdraw-first-field")
    for mode in ("list", "context"):
        page = GraphFirstResolver(store=f.store).resolve(project=f.scope.project, mode=mode)
        assert len(page["items"]) == 1, page
        if mode == "list":
            assert page["items"][0]["summary"] == "배포 상태: deployed"
            assert "status_value" not in page["items"][0]["typed_payload"]
            assert page["items"][0]["typed_payload"]["deployment_status"] == "deployed"
        else:
            assert page["statuses"][0]["status"] == "배포 상태: deployed"
            assert "status_state" not in page["statuses"][0]
        assert page["metadata"]["source_evidence_partial_cards"] == 1
    canonical = f.store.get_steward_card(result["current"]["memory_id"])
    assert canonical["typed_payload"]["status_value"] == "ready"
    assert canonical["summary"] == "기능 상태: ready; 배포 상태: deployed"


def test_previous_field_verification_cannot_keep_revoked_proof_after_another_field_was_corrected(facts):
    f = facts
    a, b, result = _two_fields(f)
    alternate = observation(f, 4, source="alternative-code")
    set_current(f, alternate)
    assert invoke(f, alternate, "ready", key="alternative", target=head_card(f, result))["action"] == "attach_evidence"
    FactSourceLifecycle(f.store).revoke(project=f.scope.project, source_ref_id=a.proof["source_ref_id"],
        expected_content_hash=a.content_hash, request_key="withdraw-previous-field")
    card = f.resolver.project_cards([f.store.get_steward_card(result["current"]["memory_id"])])[0]
    assert card["field_verifications"]["typed_payload.status_value"]["source_hash"] == hash_payload([
        {**alternate.proof, "content_hash": alternate.content_hash, "observed_at": alternate.observed_at}])
    assert "typed_payload.deployment_status" in card["field_verifications"]
    page = GraphFirstResolver(store=f.store).resolve(project=f.scope.project, mode="list")
    assert page["items"][0]["typed_payload"]["status_value"] == "ready"
    assert page["metadata"]["source_evidence_gap_fields"] == 0


def test_source_tombstone_cannot_be_reactivated_or_claimed_by_another_project(coordinator):
    c = coordinator
    source = seeded(c)
    with pytest.raises(psycopg.errors.CheckViolation, match="fact_source_project_conflict"):
        obs = observation(c.f, 7)
        other = replace(obs, project="other-project")
        with c.f.store.transaction() as db:
            # 먼저 원래 project 소유권을 확정한다.
            db.execute("""INSERT INTO lbrain_fact_observations
                (version_id,observation_id,project,source_id,source_revision,interpretation_version,observation)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""", (obs.version_id, obs.observation_id, obs.project,
                    obs.source_id, obs.source_revision, obs.interpretation_version, Jsonb(asdict(obs))))
            db.execute("""INSERT INTO lbrain_fact_observations
                (version_id,observation_id,project,source_id,source_revision,interpretation_version,observation)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""", (other.version_id, other.observation_id, other.project,
                    other.source_id, other.source_revision, other.interpretation_version, Jsonb(asdict(other))))
    with c.f.store.transaction() as db:
        db.execute("UPDATE lbrain_fact_source_receipts SET state='revoked' WHERE source_ref_id=%s", (source["source_ref_id"],))
    with pytest.raises(psycopg.errors.CheckViolation, match="fact_source_immutable"):
        with c.f.store.transaction() as db:
            db.execute("UPDATE lbrain_fact_source_receipts SET state='active' WHERE source_ref_id=%s", (source["source_ref_id"],))


def test_legacy_missing_evidence_aborts_migration_without_deleting_history(facts):
    f = facts
    obs = replace(observation(f, 4), proof={"source_ref_id": "missing-legacy-proof"})
    with f.store.transaction() as db:
        # 이전 schema의 dangling 관측을 합성 fixture에서만 재현한다.
        db.execute("ALTER TABLE lbrain_fact_observations DROP CONSTRAINT lbrain_observation_has_source")
        db.execute("DROP TRIGGER lbrain_link_observation_source ON lbrain_fact_observations")
        db.execute("""INSERT INTO lbrain_fact_observations
            (version_id,observation_id,project,source_id,source_revision,interpretation_version,observation)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""", (obs.version_id, obs.observation_id, obs.project,
                obs.source_id, obs.source_revision, obs.interpretation_version, Jsonb(asdict(obs))))
    with pytest.raises(psycopg.errors.CheckViolation, match="fact_observation_source_required"):
        f.store.execute_ddl()
    with f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_observations WHERE version_id=%s", (obs.version_id,)).fetchone()["n"] == 1


def test_revocation_fanout_is_bounded_and_not_truncated(facts):
    from agent_knowledge.session_memory.brain_steward import BrainStewardService
    f = facts
    obs = observation(f, 2)
    set_current(f, obs)
    steward = BrainStewardService(f.ledger, pgvector_store=f.store, allow_restricted=True)
    for index in range(5):
        span = {**f.span, "title": "분할 재검토 " + str(index), "source_ref": {"source_id": "fanout-" + str(index)}}
        memory_id = steward.candidate_create(source_span=span)["memory_id"]
        steward.candidate_approve(candidate_memory_id=memory_id, approved_by="fixture", decision_id="fanout-"+str(index))
        original = f.store.get_steward_card(memory_id)
        scope = replace(f.scope, subject="fanout-" + str(index))
        result = invoke(f, obs, "ready", key="fanout-" + str(index), target=original, scope=scope)
        with f.store.transaction() as db:
            # 이 검사는 queue 분할만 검사하며 fake policy의 의미 판단을 실행하지 않는다.
            db.execute("""INSERT INTO lbrain_fact_admission_rules
                (project,policy_id,policy_version,fact_key,target_memory_id,target_title,card_type,field_path)
                VALUES (%s,%s,%s,%s,%s,%s,'status','typed_payload.status_value')""",
                (f.scope.project, "fanout-"+str(index), sha256_text("policy-"+str(index)), scope.key,
                    result["current"]["memory_id"], span["title"]))
    lifecycle = FactSourceLifecycle(f.store)
    lifecycle.revoke(project=f.scope.project, source_ref_id=obs.proof["source_ref_id"],
        expected_content_hash=obs.content_hash, request_key="fanout")
    for _ in range(4):
        result = lifecycle.dispatch_once(project=f.scope.project, batch_size=2)
        assert result["expanded"]<=2 and result["scheduled"]<=2
    with f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_source_targets WHERE state='queued'").fetchone()["n"] == 5
        assert db.execute("SELECT count(*) AS n FROM lbrain_recheck_tasks").fetchone()["n"] == 5
        assert db.execute("SELECT expansion_done FROM lbrain_fact_source_events").fetchone()["expansion_done"]


def test_missing_policy_target_is_not_abandoned_when_configuration_returns(coordinator):
    import time
    from psycopg.rows import dict_row
    c = coordinator
    source = seeded(c)
    with c.f.store.transaction() as db:
        db.execute("DELETE FROM lbrain_fact_admission_rules WHERE project=%s", (c.f.scope.project,))
    lifecycle = FactSourceLifecycle(c.f.store)
    lifecycle.revoke(project=c.f.scope.project, source_ref_id=source["source_ref_id"],
        expected_content_hash=source["content_hash"], request_key="missing-policy")
    assert lifecycle.dispatch_once(project=c.f.scope.project, retry_seconds=.1)["blocked"] == 1
    assert lifecycle.health(project=c.f.scope.project)["attention_required"]
    with c.f.store.transaction() as db, db.cursor(row_factory=dict_row) as cur:
        c.f.store._lock_steward_authority_on(db)
        head = db.execute("SELECT memory_id FROM lbrain_fact_scopes").fetchone()
        c.service._register_rule(cur, c.rule, c.f.store.get_steward_card(head["memory_id"], conn=db))
    # event 확장 완료 이후의 정책 추가도 DB event 연결로 발견한다.
    assert lifecycle.dispatch_once(project=c.f.scope.project, retry_seconds=.1)["scheduled"] == 1
    time.sleep(.12)
    lifecycle.dispatch_once(project=c.f.scope.project, retry_seconds=.1)
    with c.f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_recheck_tasks").fetchone()["n"] == 2
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_source_targets WHERE state='expanded'").fetchone()["n"] == 1


def test_old_repeatable_read_cannot_claim_another_projects_source(facts):
    f = facts
    obs = observation(f, 2)
    set_current(f, obs)
    other = replace(obs, project="other-project")
    with pytest.raises(psycopg.errors.SerializationFailure):
        with f.store.transaction() as stale:
            stale.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            assert stale.execute("SELECT owner_project FROM lbrain_fact_source_receipts").fetchone()["owner_project"] is None
            invoke(f, obs, "ready")
            stale.execute("""INSERT INTO lbrain_fact_observations
                (version_id,observation_id,project,source_id,source_revision,interpretation_version,observation)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""", (other.version_id, other.observation_id, other.project,
                    other.source_id, other.source_revision, other.interpretation_version, Jsonb(asdict(other))))
    with f.store._scope() as db:
        assert db.execute("SELECT owner_project FROM lbrain_fact_source_receipts").fetchone()["owner_project"] == f.scope.project


def test_legacy_corrupt_receipt_aborts_migration_without_inventing_replacement(coordinator):
    c = coordinator
    source = seeded(c)
    with c.f.store.transaction() as db:
        db.execute("DROP TRIGGER lbrain_preserve_fact_source ON lbrain_fact_source_receipts")
        db.execute("UPDATE lbrain_fact_source_receipts SET redacted_content=%s WHERE source_ref_id=%s",
            ("합성 legacy 손상", source["source_ref_id"]))
    with pytest.raises(psycopg.errors.CheckViolation, match="fact_source_content_hash_mismatch"):
        c.f.store.execute_ddl()
    with c.f.store._scope() as db:
        row = db.execute("SELECT redacted_content,content_hash FROM lbrain_fact_source_receipts").fetchone()
        assert row["redacted_content"] == "합성 legacy 손상" and row["content_hash"] == source["content_hash"]


def test_actual_git_ref_rollback_revalidates_history_without_replaying_old_request(coordinator):
    import copy
    from test_correction_sources import git
    c = coordinator
    seeded(c)
    original = copy.deepcopy(c.request)
    (c.root / "facts.json").write_text('{"status":"failed"}')
    git(c.root, "add", "facts.json"); git(c.root, "commit", "-m", "합성 후속 변경")
    next_request = copy.deepcopy(c.request)
    next_request["idempotency_key"] = "before-ref-rollback"
    change = next_request["changes"][0]
    change.pop("target_memory_id"); change.pop("expected_content_hash")
    change.update(target_description=c.rule.target_title, old_value="ready", proposed_value="failed",
        evidence={"revision": git(c.root, "rev-parse", "HEAD"), "content_hash": sha256_text('{"status":"failed"}')})
    second = c.service.submit(next_request)["changes"][0]["task_id"]
    worker = c.service.worker(project=c.f.scope.project, worker_id="ref-rollback")
    assert worker.run_once(task_id=second)["state"] == "resolved"
    assert c.service.submit(original)["duplicate"]  # 기존 A 요청은 B를 덮어쓰지 않는다.
    assert c.service.result(second)["resolution"]["current"]["value"] == "failed"
    git(c.root, "reset", "--hard", c.current)  # 작업 전용 임시 Git fixture만 되돌린다.
    back = copy.deepcopy(next_request)
    back["idempotency_key"] = "after-ref-rollback"
    back["changes"][0].update(old_value="failed", proposed_value="ready", evidence=original["changes"][0]["evidence"])
    third = c.service.submit(back)["changes"][0]["task_id"]
    result = worker.run_once(task_id=third)
    assert result["state"] == "resolved", result
    resolution = c.service.result(third)["resolution"]
    assert resolution["action"] == "replace_current" and resolution["current"]["value"] == "ready"
    with c.f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_observations").fetchone()["n"] == 2
        assert db.execute("SELECT count(*) AS n FROM steward_card_decisions").fetchone()["n"] == 4
