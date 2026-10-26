"""실제 Git에서 무관한 commit이 독립된 반대 근거를 제거하지 않아야 한다."""
import copy
from dataclasses import replace
import pytest

from agent_knowledge.fact_correction_coordinator import FactCorrectionCoordinator
from agent_knowledge.fact_verification import FactVerificationService
from agent_knowledge.public_safe_util import hash_payload, sha256_text
from agent_knowledge.session_memory.brain_steward import BrainStewardService
from test_correction_sources import git
from test_fact_resolution import facts, coordinator, observation, invoke, set_current, head_card


def disputed_pair(c):
    (c.root / "independent.json").write_text('{"status":"failed"}')
    git(c.root, "add", "independent.json"); git(c.root, "commit", "-m", "같은 사실에 대한 독립 반대 근거")
    revision = git(c.root, "rev-parse", "HEAD")
    second_rule = replace(c.rule, policy_id="independent", target_title="독립 원천의 기능 상태",
        relative_path="independent.json")
    c.service = FactCorrectionCoordinator(store=c.f.store, git_reader=c.service.git_reader,
        policies=[c.rule, second_rule])
    c.request["changes"][0]["evidence"]["revision"] = revision
    first = c.service.submit(c.request)["changes"][0]["task_id"]
    worker = c.service.worker(project=c.f.scope.project, worker_id="source-competition", retry_seconds=.1)
    assert worker.run_once(task_id=first)["state"] == "resolved"
    steward = BrainStewardService(c.f.ledger, pgvector_store=c.f.store, allow_restricted=True)
    span = {**c.f.span, "title": second_rule.target_title,
        "source_ref": {"source_id": "independent-file"}, "content_hash": sha256_text("independent-file")}
    alias = steward.candidate_create(source_span=span)["memory_id"]
    steward.candidate_approve(candidate_memory_id=alias, approved_by="fixture", decision_id="independent-seed")
    request = copy.deepcopy(c.request)
    request["idempotency_key"] = "independent-disagreement"
    request["changes"][0].update(policy_id=second_rule.policy_id, target_memory_id=alias,
        expected_content_hash=c.f.store.get_steward_card(alias)["content_hash"],
        evidence={"revision": revision, "content_hash": sha256_text('{"status":"failed"}')})
    disagreement = c.service.submit(request)["changes"][0]["task_id"]
    assert worker.run_once(task_id=disagreement)["state"] == "waiting"
    with c.f.store._scope() as db:
        original = db.execute("SELECT value,state FROM lbrain_fact_scopes").fetchone()
        decisions = db.execute("SELECT count(*) AS n FROM steward_card_decisions").fetchone()["n"]
    assert original == {"value": "ready", "state": "disputed"}
    return worker, original, decisions, second_rule


def review_second(c, worker, second_rule):
    FactVerificationService(c.f.store).dispatch_once(project=c.f.scope.project)
    with c.f.store._scope() as db:
        task = db.execute("SELECT task_id FROM lbrain_fact_check_schedules WHERE policy_id=%s", (second_rule.policy_id,)).fetchone()["task_id"]
    return worker.run_once(task_id=task)


def test_unrelated_commit_cannot_erase_a_still_valid_competing_file(coordinator):
    c = coordinator
    worker, original, decisions, second_rule = disputed_pair(c)
    (c.root / "notes.txt").write_text("이 commit은 두 사실 파일을 수정하지 않는다.")
    git(c.root, "add", "notes.txt"); git(c.root, "commit", "-m", "사실과 무관한 변경")
    # 기존 head를 단순 무효 취급하면 새 timer의 server_current 입력만
    # 유효하다고 판단해 잘못 단독 채택할 수 있다.
    outcome = review_second(c, worker, second_rule)
    with c.f.store._scope() as db:
        current = db.execute("SELECT value,state FROM lbrain_fact_scopes").fetchone()
        after = db.execute("SELECT count(*) AS n FROM steward_card_decisions").fetchone()["n"]
    assert outcome["state"] == "waiting" and current == original and after == decisions, {
        "outcome": outcome, "before": original, "after": current, "authority_decision_delta": after-decisions}


def test_changed_competing_source_is_scheduled_before_any_unilateral_promotion(coordinator):
    c = coordinator
    worker, original, decisions, second_rule = disputed_pair(c)
    (c.root / "facts.json").write_text('{"status":"paused"}')
    git(c.root, "add", "facts.json"); git(c.root, "commit", "-m", "반대 근거가 다른 값으로 변경")
    assert review_second(c, worker, second_rule)["state"] == "waiting"
    with c.f.store._scope() as db:
        assert db.execute("SELECT value,state FROM lbrain_fact_scopes").fetchone() == original
        due = db.execute("SELECT next_check_at<=clock_timestamp() AS due FROM lbrain_fact_check_schedules WHERE policy_id=%s",
            (c.rule.policy_id,)).fetchone()
        assert due["due"]
        assert db.execute("SELECT count(*) AS n FROM steward_card_decisions").fetchone()["n"] == decisions
    assert FactVerificationService(c.f.store).dispatch_once(project=c.f.scope.project)["scheduled"] == 1
    with c.f.store._scope() as db:
        other = db.execute("SELECT task_id FROM lbrain_fact_check_schedules WHERE policy_id=%s", (c.rule.policy_id,)).fetchone()["task_id"]
    assert worker.run_once(task_id=other)["state"] == "waiting"
    with c.f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM steward_card_decisions").fetchone()["n"] == decisions


def test_unavailable_competing_source_is_not_treated_as_disproved(coordinator):
    c = coordinator
    worker, original, decisions, second_rule = disputed_pair(c)
    git(c.root, "rm", "facts.json"); git(c.root, "commit", "-m", "현재 확인할 수 없는 반대 source")
    assert review_second(c, worker, second_rule)["state"] == "waiting"
    with c.f.store._scope() as db:
        assert db.execute("SELECT value,state FROM lbrain_fact_scopes").fetchone() == original
        assert db.execute("SELECT count(*) AS n FROM steward_card_decisions").fetchone()["n"] == decisions


def test_real_file_rollback_can_reactivate_a_historical_competing_claim(coordinator):
    import time
    c = coordinator
    worker, original, decisions, second_rule = disputed_pair(c)
    (c.root / "facts.json").write_text('{"status":"failed"}')
    git(c.root, "add", "facts.json"); git(c.root, "commit", "-m", "독립 원천의 합의")
    assert review_second(c, worker, second_rule)["state"] == "waiting"
    assert FactVerificationService(c.f.store).dispatch_once(project=c.f.scope.project)["scheduled"] == 1
    with c.f.store._scope() as db:
        first_source_task = db.execute("SELECT task_id FROM lbrain_fact_check_schedules WHERE policy_id=%s", (c.rule.policy_id,)).fetchone()["task_id"]
        second_source_task = db.execute("SELECT task_id FROM lbrain_fact_check_schedules WHERE policy_id=%s", (second_rule.policy_id,)).fetchone()["task_id"]
    assert worker.run_once(task_id=first_source_task)["state"] == "resolved"
    (c.root / "facts.json").write_text('{"status":"ready"}')
    git(c.root, "add", "facts.json"); git(c.root, "commit", "-m", "실제 파일 내용 rollback")
    time.sleep(.12)
    outcome = worker.run_once(task_id=second_source_task)
    with c.f.store._scope() as db:
        current = db.execute("SELECT value,state FROM lbrain_fact_scopes").fetchone()
    assert outcome["state"] == "waiting" and current["value"] == "failed" and current["state"] in {"pending", "disputed"}, {
        "outcome": outcome, "current": current}
    FactVerificationService(c.f.store).dispatch_once(project=c.f.scope.project)
    with c.f.store._scope() as db:
        recheck = db.execute("SELECT task_id FROM lbrain_fact_check_schedules WHERE policy_id=%s", (c.rule.policy_id,)).fetchone()["task_id"]
    assert worker.run_once(task_id=recheck)["state"] == "waiting"
    with c.f.store._scope() as db:
        assert db.execute("SELECT value,state FROM lbrain_fact_scopes").fetchone() == {"value": "failed", "state": "disputed"}
    from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
    page = GraphFirstResolver(store=c.f.store).resolve(project=c.f.scope.project, mode="list")
    assert len(page["items"]) == 2
    assert all(item["typed_payload"]["status_value"] == "failed" for item in page["items"])


def test_real_worker_keeps_unchanged_conflict_then_automatically_resolves_new_agreement(
        coordinator, tmp_path, additional_env=None):
    import json
    import os
    from pathlib import Path
    import signal
    import subprocess
    import time
    from agent_knowledge.fact_correction_coordinator import GitSemanticFactPolicy
    from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
    c = coordinator
    worker, original, decisions, second_rule = disputed_pair(c)
    (c.root / "notes.txt").write_text("두 상태 파일과 무관한 변경")
    git(c.root, "add", "notes.txt"); git(c.root, "commit", "-m", "무관한 변경 후 자동 재검토")
    config = tmp_path / "competing-policy.json"
    def descriptor(rule):
        from dataclasses import asdict
        return {**asdict(rule), **({"adapter": "git_semantic.v1"} if isinstance(rule, GitSemanticFactPolicy) else {})}
    config.write_text(json.dumps({"schema_version": "fact_correction_config.v1", "repositories": [{
        "repository": c.rule.scope.repository, "root": str(c.root), "branch_ref": c.rule.scope.branch}],
        "rules": [descriptor(c.rule), descriptor(second_rule)]}))
    config.chmod(0o600)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "lib"), "NEURON_LBRAIN_PGVECTOR_DSN": c.f.store.dsn,
        **(additional_env or {})}
    args = ["uv", "run", "--no-project", "-m", "agent_knowledge.fact_source_lifecycle", "worker",
        "--project", c.f.scope.project, "--config", str(config), "--iterations", "1000", "--poll-seconds", ".05",
        "--retry-seconds", ".1"]
    process = None
    def stop():
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)
    def wait_for(predicate):
        deadline = time.monotonic()+60
        while True:
            assert process.poll() is None, "competing source worker exited"
            with c.f.store._scope() as db:
                if predicate(db): return
            assert time.monotonic()<deadline, "competing source automatic review timed out"
            time.sleep(.05)
    with (tmp_path / "competing-worker.log").open("wb") as log:
        try:
            process = subprocess.Popen(args, env=env, stdout=log, stderr=log, start_new_session=True)
            wait_for(lambda db: db.execute("""SELECT 1 FROM lbrain_recheck_tasks
                WHERE request->>'policy_id'=%s AND request#>>'{trigger,kind}'='verification_due'
                    AND attempt_count>=1 AND state='waiting'""", (second_rule.policy_id,)).fetchone())
            with c.f.store._scope() as db:
                assert db.execute("SELECT value,state FROM lbrain_fact_scopes").fetchone() == original
                assert db.execute("SELECT count(*) AS n FROM steward_card_decisions").fetchone()["n"] == decisions
            # 실제 살아 있는 worker의 재시도/미래 backoff도 장기 미해결 경고를
            # 가리지 않는다. 짧은 테스트 임계값을 실제로 기다리며 DB 시각을 바꾸지 않는다.
            time.sleep(.15)
            monitor = subprocess.run(["uv", "run", "--no-project", "-m", "agent_knowledge.fact_source_lifecycle",
                "monitor", "--project", c.f.scope.project, "--pending-age-seconds", ".1"],
                env=env, capture_output=True, text=True, timeout=10)
            assert monitor.returncode == 0, "independent conflict monitor failed"
            health = json.loads(monitor.stdout)
            assert health["attention_required"] and health["reviews"]["aged"] >= 1
            assert health["reviews"]["heartbeat_recent"]
            # 실제 두 source가 같은 값을 지지하게 된 뒤에는 보류에 방치하지 않는다.
            (c.root / "facts.json").write_text('{"status":"failed"}')
            git(c.root, "add", "facts.json"); git(c.root, "commit", "-m", "두 독립 source의 새로운 합의")
            wait_for(lambda db: db.execute("SELECT 1 FROM lbrain_fact_scopes WHERE value='\"failed\"'::jsonb AND state='current'").fetchone())
            wait_for(lambda db: db.execute("SELECT count(*) AS n FROM lbrain_recheck_tasks WHERE state<>'resolved'").fetchone()["n"] == 0)
            stop()
            from agent_knowledge.fact_source_lifecycle import FactSourceLifecycle
            assert FactSourceLifecycle(c.f.store).health(project=c.f.scope.project,
                pending_age_seconds=.1)["reviews"]["aged"] == 0
            with c.f.store._scope() as db:
                assert db.execute("SELECT count(*) AS n FROM steward_card_decisions").fetchone()["n"] == decisions+1
            page = GraphFirstResolver(store=c.f.store).resolve(project=c.f.scope.project, mode="list")
            assert len(page["items"]) == 2
            assert {item["typed_payload"]["status_value"] for item in page["items"]} == {"failed"}
        finally: stop()


def test_same_value_source_change_cannot_extend_verification_or_retire_valid_history(facts):
    from agent_knowledge.fact_resolution import FactResolutionError
    f = facts
    obs = observation(f, 2); set_current(f, obs)
    first = invoke(f, obs, "ready")
    target = head_card(f, first)
    checks = iter((True, True, None))
    with f.store._scope() as db:
        before = db.execute("SELECT * FROM lbrain_fact_verifications").fetchall()
        generation = db.execute("SELECT generation FROM lbrain_fact_project_versions").fetchone()["generation"]
    with pytest.raises(FactResolutionError, match="source_changed_during_resolution"):
        f.resolver.reconcile(scope=f.scope, memory_id=target["memory_id"], field_path=f.commit.field_path,
            render_template=f.commit.render_template, expected_content_hash=target["content_hash"],
            value="ready", observation=obs, request_key="changed-mid-recheck", request_hash=hash_payload(["mid-recheck"]),
            requested_value="ready", is_current=lambda item: next(checks), compare=lambda a,b: "same", commit=f.commit)
    with f.store._scope() as db:
        assert db.execute("SELECT * FROM lbrain_fact_verifications").fetchall() == before
        assert db.execute("SELECT generation FROM lbrain_fact_project_versions").fetchone()["generation"] == generation


def test_later_corroboration_keeps_evidence_after_its_original_pending_record_is_considered_for_gc(coordinator):
    import psycopg
    c = coordinator
    worker, original, decisions, second_rule = disputed_pair(c)
    (c.root / "facts.json").write_text('{"status":"failed"}')
    git(c.root, "add", "facts.json"); git(c.root, "commit", "-m", "두 근거의 실제 합의")
    assert review_second(c, worker, second_rule)["state"] == "waiting"
    FactVerificationService(c.f.store).dispatch_once(project=c.f.scope.project)
    with c.f.store._scope() as db:
        task = db.execute("SELECT task_id FROM lbrain_fact_check_schedules WHERE policy_id=%s", (c.rule.policy_id,)).fetchone()["task_id"]
    assert worker.run_once(task_id=task)["state"] == "resolved"
    with c.f.store._scope() as db:
        support = db.execute("SELECT * FROM lbrain_fact_corroborations").fetchone()
    assert support
    with pytest.raises(psycopg.errors.CheckViolation, match="fact_corroboration_retention_required"):
        with c.f.store.transaction() as db: db.execute("DELETE FROM lbrain_fact_corroborations")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with c.f.store.transaction() as db:
            # 최초 보류 판단만 보고 정리하면, 나중 채택에서 사용한 근거를
            # 지울 수 있다. 나중 결정의 참조가 실제 FK로 그 삭제를 막는다.
            db.execute("DELETE FROM lbrain_fact_resolution_events WHERE assertion_id=%s", (support["assertion_id"],))
            db.execute("DELETE FROM lbrain_fact_resolutions WHERE result->>'assertion_id'=%s", (support["assertion_id"],))
            db.execute("DELETE FROM lbrain_fact_assertions WHERE assertion_id=%s", (support["assertion_id"],))
    c.f.store.execute_ddl()
