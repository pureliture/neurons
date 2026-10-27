"""실제 PG에서 공표 CAS·효력 분리·옛 실행 차단을 검사한다. 모델 정확도 증거는 아니다."""

from dataclasses import replace

import pytest

from agent_knowledge.fact_correction_coordinator import FactCorrectionCoordinator
from agent_knowledge.fact_policy_lifecycle import FactPolicyLifecycle
from agent_knowledge.fact_resolution import FactResolutionError
from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
from agent_knowledge.recheck_scheduler import RecheckBlocked
from test_correction_sources import git
import test_fact_resolution as fixtures
import test_observation_provenance as observation_fixtures

facts = fixtures.facts
coordinator = fixtures.coordinator
actual_couch_source = observation_fixtures.actual_couch_source


def publisher(c, rule):
    service = FactCorrectionCoordinator(store=c.f.store, git_reader=c.service.git_reader, policies=[rule])
    return FactPolicyLifecycle(service)


def publish(c, rule, *, expected=None, key="publish-v1", active=True):
    return publisher(c, rule).publish(project=rule.scope.project, policy_id=rule.policy_id,
        expected_policy_version=expected or c.rule.version, request_key=key, actor="fixture-admin", active=active)


def seed(c):
    task_id = c.service.submit(c.request)["changes"][0]["task_id"]
    result = c.service.worker(project=c.f.scope.project, worker_id="policy-seed").run_once(task_id=task_id)
    assert result["state"] == "resolved", result
    return task_id


def test_publish_invalidates_old_interpretation_without_revoking_source_or_rewriting_result(coordinator):
    c = coordinator
    original_task = seed(c)
    original = c.service.result(original_task)["resolution"]
    rule = replace(c.rule, json_pointer="/next_status", publication_revision=1)
    receipt = publish(c, rule)
    assert not receipt["canonical_write_performed"] and receipt["recheck_scheduled"]
    assert fixtures.counts(c.f)["steward_card_decisions"] == 2
    assert not GraphFirstResolver(store=c.f.store).resolve(project=c.f.scope.project, mode="list")["items"]
    after = c.service.result(original_task)["resolution"]
    assert after["decision_time_current"] == original["decision_time_current"]
    assert after["current"]["state"] == "invalidated"
    with c.f.store._scope() as db:
        assert db.execute("SELECT state FROM lbrain_fact_source_receipts").fetchone()["state"] == "active"
        assert db.execute("SELECT state FROM lbrain_fact_assertions").fetchone()["state"] == "revoked"
        assert db.execute("SELECT state FROM lbrain_fact_policy_reviews").fetchone()["state"] == "scheduled"
    duplicate = publish(c, rule)
    assert duplicate["duplicate"] and duplicate["publication_id"] == receipt["publication_id"]
    with pytest.raises(FactResolutionError, match="policy_publication_key_conflict"):
        publish(c, replace(rule,max_age_seconds=60))


def test_policy_change_before_first_resolution_keeps_an_explicit_review_plan(coordinator):
    c = coordinator
    c.service.submit(c.request)
    receipt = publish(c, replace(c.rule,publication_revision=1))
    assert receipt["recheck_scheduled"]
    assert fixtures.counts(c.f)["lbrain_fact_resolutions"] == 0
    assert fixtures.counts(c.f)["steward_card_decisions"] == 1


def test_old_loaded_worker_cannot_apply_after_publication_commit(coordinator):
    c = coordinator
    task_id = c.service.submit(c.request)["changes"][0]["task_id"]
    worker = c.service.worker(project=c.f.scope.project, worker_id="old-loaded")
    claimed = c.service.queue.claim(project=c.f.scope.project, worker_id="old-loaded", lease_seconds=60,
        task_id=task_id)
    snapshot = worker.load(claimed)
    judgment = worker.judge(claimed,snapshot)
    publish(c, replace(c.rule,publication_revision=1))
    with pytest.raises(RecheckBlocked, match="fact_policy_changed"):
        worker.apply(claimed,snapshot,judgment)
    assert fixtures.counts(c.f)["steward_card_decisions"] == 1
    assert fixtures.counts(c.f)["lbrain_fact_resolutions"] == 0


@pytest.mark.parametrize("damage,reason",[("scope","policy_publication_scope_mismatch"),
    ("display","policy_publication_display_scope_changed"),("sequence","policy_publication_sequence_required")])
def test_policy_publication_does_not_expand_fact_or_display_scope(coordinator,damage,reason):
    c = coordinator
    c.service.submit(c.request)
    rule = replace(c.rule,publication_revision=1)
    if damage == "scope":
        rule = replace(rule,scope=replace(rule.scope,environment="other-environment"))
    elif damage == "display":
        rule = replace(rule,render_template="다른 사실 상태: {value}")
    else:
        rule = replace(rule,publication_revision=0,json_pointer="/other")
    with pytest.raises(FactResolutionError,match=reason):
        publish(c,rule)
    with c.f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_policy_publications").fetchone()["n"] == 0


def test_cas_publication_rollback_is_a_new_version_and_history_cannot_be_erased(coordinator):
    from psycopg.errors import CheckViolation
    c = coordinator
    seed(c)
    one = replace(c.rule,max_age_seconds=60,publication_revision=1)
    first = publish(c,one)
    with pytest.raises(FactResolutionError,match="policy_publication_revision_conflict"):
        publish(c,replace(c.rule,publication_revision=2),key="stale-cas")
    rolled_back = replace(c.rule,publication_revision=2)
    second = publish(c,rolled_back,expected=one.version,key="rollback")
    assert second["policy_version"] != c.rule.version and second["publication_revision"] == 2
    with pytest.raises(CheckViolation,match="policy_publication_retention_required"),c.f.store.transaction() as db:
        db.execute("DELETE FROM lbrain_fact_policy_publications WHERE publication_id=%s",(first["publication_id"],))


def test_disable_has_a_durable_receipt_and_does_not_claim_to_verify_a_new_value(coordinator):
    c = coordinator
    seed(c)
    receipt = publish(c,replace(c.rule,publication_revision=1),active=False)
    assert not receipt["canonical_write_performed"] and not receipt["recheck_scheduled"]
    with c.f.store._scope() as db:
        assert db.execute("SELECT active FROM lbrain_fact_admission_rules").fetchone()["active"] is False
        assert db.execute("SELECT state FROM lbrain_fact_policy_reviews").fetchone()["state"] == "disabled"
    assert not GraphFirstResolver(store=c.f.store).resolve(project=c.f.scope.project,mode="list")["items"]


def source_with_new_field(c, value="failed"):
    import json
    (c.root/"facts.json").write_text(json.dumps({"status":"ready","next_status":value}))
    git(c.root,"add","facts.json");git(c.root,"commit","-m","새 정책이 읽을 합성 field")


def test_dispatch_revalidates_new_source_and_keeps_old_worker_blocked(coordinator):
    c = coordinator
    seed(c)
    source_with_new_field(c)
    rule = replace(c.rule,json_pointer="/next_status",publication_revision=1)
    lifecycle = publisher(c,rule)
    publish(c,rule)
    assert lifecycle.dispatch_once(project=c.f.scope.project)["scheduled"]==1
    assert lifecycle.dispatch_once(project=c.f.scope.project)["scheduled"]==0
    with c.f.store._scope() as db:
        task_id = db.execute("SELECT task_id FROM lbrain_fact_policy_reviews").fetchone()["task_id"]
    outcome = lifecycle.coordinator.worker(project=c.f.scope.project,worker_id="new-policy").run_once(task_id=task_id)
    assert outcome["state"]=="resolved",outcome
    result = lifecycle.coordinator.result(task_id)["resolution"]
    assert result["requested_value"]=="ready" and result["applied_value"]=="failed"
    assert lifecycle.dispatch_once(project=c.f.scope.project)["verified"]==1
    page = GraphFirstResolver(store=c.f.store).resolve(project=c.f.scope.project,mode="list")
    assert page["items"][0]["typed_payload"]["status_value"]=="failed"
    import copy
    old_request = copy.deepcopy(c.request)
    old_request["idempotency_key"]="old-worker-new-attempt"
    rejected = c.service.submit(old_request)["changes"][0]
    assert rejected["status"]=="rejected" and rejected["reason_code"]=="fact_policy_changed"


def test_unavailable_new_source_keeps_same_review_then_recovers(coordinator):
    import time
    c = coordinator
    seed(c)
    source_with_new_field(c)
    rule = replace(c.rule,json_pointer="/next_status",publication_revision=1)
    lifecycle = publisher(c,rule)
    publish(c,rule)
    lifecycle.dispatch_once(project=c.f.scope.project,retry_seconds=.1)
    with c.f.store._scope() as db:
        task_id = db.execute("SELECT task_id FROM lbrain_fact_policy_reviews").fetchone()["task_id"]
    worker = lifecycle.coordinator.worker(project=c.f.scope.project,worker_id="new-policy",retry_seconds=.1)
    hidden = c.root.with_name("new-policy-source-offline")
    c.root.rename(hidden)
    try:
        outcome = worker.run_once(task_id=task_id)
        assert outcome["state"]=="blocked",outcome
        assert lifecycle.coordinator.result(task_id)["automatic_recheck_scheduled"]
        assert lifecycle.dispatch_once(project=c.f.scope.project)["scheduled"]==0
    finally:
        hidden.rename(c.root)
    time.sleep(.12)
    assert worker.run_once(task_id=task_id)["state"]=="resolved"
    assert lifecycle.dispatch_once(project=c.f.scope.project)["verified"]==1
    with c.f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_policy_review_attempts").fetchone()["n"]==1


def test_superseded_publication_is_not_dispatched_as_current(coordinator):
    c = coordinator
    seed(c)
    source_with_new_field(c)
    one = replace(c.rule,json_pointer="/next_status",publication_revision=1)
    two = replace(c.rule,publication_revision=2)
    publish(c,one)
    publish(c,two,expected=one.version,key="publication-two")
    lifecycle = publisher(c,two)
    result = lifecycle.dispatch_once(project=c.f.scope.project)
    assert result["superseded"]==1 and result["scheduled"]==1
    with c.f.store._scope() as db:
        attempts = db.execute("SELECT * FROM lbrain_fact_policy_review_attempts").fetchall()
    assert len(attempts)==1
    task = lifecycle.coordinator.queue.get(attempts[0]["task_id"])
    assert task["request"]["policy_version"]==two.version


def test_concurrent_dispatchers_create_one_review_attempt(coordinator):
    from concurrent.futures import ThreadPoolExecutor
    c = coordinator
    c.service.submit(c.request)
    rule = replace(c.rule,publication_revision=1)
    lifecycle = publisher(c,rule)
    publish(c,rule)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _:lifecycle.dispatch_once(project=c.f.scope.project),range(2)))
    assert sum(row.get("scheduled",0) for row in results)==1
    with c.f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_policy_review_attempts").fetchone()["n"]==1


def reviewed_original(c):
    original = c.service.submit(c.request)["changes"][0]["task_id"]
    source_with_new_field(c)
    rule = replace(c.rule,json_pointer="/next_status",publication_revision=1)
    lifecycle = publisher(c,rule)
    publication = publish(c,rule)
    lifecycle.dispatch_once(project=c.f.scope.project,retry_seconds=.1)
    with c.f.store._scope() as db:
        child = db.execute("SELECT task_id FROM lbrain_fact_policy_reviews").fetchone()["task_id"]
    outcome = lifecycle.coordinator.worker(project=c.f.scope.project,worker_id="new-policy").run_once(task_id=child)
    assert outcome["state"]=="resolved",outcome
    return original,child,lifecycle,publication


def test_old_request_finishes_as_superseded_without_pretending_child_has_its_request_hash(coordinator):
    c = coordinator
    original = c.service.submit(c.request)["changes"][0]["task_id"]
    source_with_new_field(c)
    rule = replace(c.rule,json_pointer="/next_status",publication_revision=1)
    lifecycle = publisher(c,rule)
    publish(c,rule)
    lifecycle.dispatch_once(project=c.f.scope.project,retry_seconds=.1)
    worker = c.service.worker(project=c.f.scope.project,worker_id="old-request",retry_seconds=.1)
    first = worker.run_once(task_id=original)
    assert first["state"]=="waiting" and first["reason_code"]=="policy_revalidation_pending",first
    assert c.service.result(original)["automatic_recheck_scheduled"]
    with c.f.store._scope() as db:
        child = db.execute("SELECT task_id FROM lbrain_fact_policy_reviews").fetchone()["task_id"]
    assert lifecycle.coordinator.worker(project=c.f.scope.project,worker_id="new-policy").run_once(task_id=child)["state"]=="resolved"
    import time
    time.sleep(.12)
    after = worker.run_once(task_id=original)
    assert after["state"]=="resolved" and after["reason_code"]=="policy_revalidated",after
    result = c.service.result(original)
    assert "resolution" not in result
    assert result["policy_review"]["status"]=="superseded" and not result["policy_review"]["original_change_applied"]
    assert result["review_resolution"]["applied_value"]=="failed"
    assert c.service.queue.get(original)["request"]["change"]["proposed_value"]=="caller-wrong"
    assert fixtures.counts(c.f)["steward_card_decisions"]==2
    assert not result["automatic_recheck_scheduled"] and result["consistency_token"]["min_fact_generation"]>=1


def test_disabled_policy_resolves_old_request_as_explicit_non_applied_result(coordinator):
    c = coordinator
    original = c.service.submit(c.request)["changes"][0]["task_id"]
    publish(c,replace(c.rule,publication_revision=1),active=False)
    outcome = c.service.worker(project=c.f.scope.project,worker_id="old-disabled").run_once(task_id=original)
    assert outcome["state"]=="resolved" and outcome["reason_code"]=="policy_disabled",outcome
    result = c.service.result(original)
    assert not result["policy_review"]["canonical_write_performed"]
    assert "review_resolution" not in result and fixtures.counts(c.f)["steward_card_decisions"]==1


def test_superseded_result_survives_finish_loss_without_source_or_policy_configuration(coordinator,monkeypatch):
    c = coordinator
    original,child,lifecycle,publication = reviewed_original(c)
    worker = c.service.worker(project=c.f.scope.project,worker_id="lost-response")
    finish = c.service.queue.finish
    def crash(*args,**kwargs):
        raise SystemExit(91)
    monkeypatch.setattr(c.service.queue,"finish",crash)
    with pytest.raises(SystemExit):
        worker.run_once(task_id=original)
    result_ref = c.service.queue.get(original)["result_ref"]
    assert result_ref.startswith("policyrev_")
    monkeypatch.setattr(c.service.queue,"finish",finish)
    with c.f.store.transaction() as db:
        db.execute("UPDATE lbrain_recheck_tasks SET lease_until=clock_timestamp()-interval '1 second' WHERE task_id=%s",(original,))
    hidden = c.root.with_name("policy-result-source-offline")
    c.root.rename(hidden)
    c.service.policies = {}
    try:
        outcome = worker.run_once(task_id=original)
        assert outcome["state"]=="resolved",outcome
    finally:
        hidden.rename(c.root)
    assert c.service.result(original)["policy_review"]["review_task_id"]==child
    assert fixtures.counts(c.f)["steward_card_decisions"]==2


def test_used_policy_review_judgment_and_publication_result_are_immutable(coordinator):
    from psycopg.errors import CheckViolation
    c = coordinator
    original,child,lifecycle,publication = reviewed_original(c)
    outcome = c.service.worker(project=c.f.scope.project,worker_id="old-request").run_once(task_id=original)
    assert outcome["state"]=="resolved",outcome
    with pytest.raises(CheckViolation,match="policy_review_judgment_retention_required"),c.f.store.transaction() as db:
        db.execute("UPDATE lbrain_recheck_judgments SET judgment='{}'::jsonb WHERE task_id=%s",(original,))
    with pytest.raises(CheckViolation,match="policy_publication_retention_required"),c.f.store.transaction() as db:
        db.execute("DELETE FROM lbrain_fact_policy_review_results WHERE task_id=%s",(original,))


def test_unvalidated_semantic_policy_is_rejected_before_invalidating_old_fact(coordinator):
    from agent_knowledge.fact_correction_coordinator import GitSemanticFactPolicy
    from agent_knowledge.public_safe_util import sha256_text
    c = coordinator
    seed(c)
    rule = GitSemanticFactPolicy("status",c.rule.scope,c.rule.target_title,c.rule.field_path,c.rule.render_template,
        "facts.json","파일의 기능 상태",(("ready","기능 상태는 ready다."),("failed","기능 상태는 failed다.")),
        "repository_content","missing-private-profile",sha256_text("missing-private-profile"),publication_revision=1)
    with pytest.raises(FactResolutionError,match="policy_admission_profile_unvalidated"):
        publish(c,rule)
    page = GraphFirstResolver(store=c.f.store).resolve(project=c.f.scope.project,mode="list")
    assert page["items"][0]["typed_payload"]["status_value"]=="ready"
    with c.f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_policy_publications").fetchone()["n"]==0


def test_intentional_disable_stops_checks_but_unrecorded_disable_still_requires_attention(coordinator):
    from agent_knowledge.fact_verification import FactVerificationService
    from agent_knowledge.fact_source_lifecycle import FactSourceLifecycle
    c = coordinator
    seed(c)
    publish(c,replace(c.rule,publication_revision=1),active=False)
    with c.f.store.transaction() as db:
        db.execute("UPDATE lbrain_fact_check_schedules SET next_check_at=clock_timestamp()-interval '1 day'")
    assert FactVerificationService(c.f.store).dispatch_once(project=c.f.scope.project)["scheduled"]==0
    health = FactSourceLifecycle(c.f.store).health(project=c.f.scope.project,overdue_grace=.1)
    assert not health["verifications"]["attention_required"]
    assert not health["policy_reviews"]["attention_required"]
    assert not health["attention_required"]
    with c.f.store.transaction() as db:
        db.execute("UPDATE lbrain_fact_admission_rules SET policy_version=%s",(c.rule.version,))
    # 공표 기록 없이 비활성화된 설정은 의도한 종료로 오인하지 않는다.
    assert FactVerificationService(c.f.store).dispatch_once(project=c.f.scope.project)["blocked"]>=1
    assert FactSourceLifecycle(c.f.store).health(project=c.f.scope.project)["verifications"]["attention_required"]


def test_real_cli_publication_worker_stop_monitor_restart_and_receipt_without_config(coordinator,tmp_path):
    import copy
    import json
    import os
    from pathlib import Path
    import signal
    import subprocess
    import time
    from dataclasses import asdict
    c = coordinator
    seed(c)
    old_request = copy.deepcopy(c.request)
    old_request["idempotency_key"]="old-pending-at-publication"
    original = c.service.submit(old_request)["changes"][0]["task_id"]
    source_with_new_field(c)
    rule = replace(c.rule,json_pointer="/next_status",publication_revision=1)
    config = tmp_path/"new-policy.json"
    config.write_text(json.dumps({"schema_version":"fact_correction_config.v1","repositories":[{
        "repository":rule.scope.repository,"root":str(c.root),"branch_ref":rule.scope.branch}],"rules":[asdict(rule)]}))
    config.chmod(0o600)
    env = {"PATH":os.environ["PATH"],"HOME":str(tmp_path),"UV_CACHE_DIR":str(tmp_path/"uv-cache"),
        "PYTHONPATH":str(Path(__file__).resolve().parents[1]/"lib"),"NEURON_LBRAIN_PGVECTOR_DSN":c.f.store.dsn}
    module = ["uv","run","--no-project","-m","agent_knowledge.fact_policy_lifecycle"]
    common = ["--project",c.f.scope.project,"--policy-id",rule.policy_id,"--config",str(config)]
    plan = subprocess.run([*module,"plan",*common],env=env,capture_output=True,timeout=20,check=True)
    assert json.loads(plan.stdout)["publication_performed"] is False
    published = subprocess.run([*module,"publish",*common,"--expected-policy-version",c.rule.version,
        "--request-key","actual-publication","--actor","fixture-admin"],env=env,capture_output=True,timeout=20,check=True)
    publication_id = json.loads(published.stdout)["publication_id"]
    assert fixtures.counts(c.f)["steward_card_decisions"]==2
    hidden = c.root.with_name("publication-source-offline")
    c.root.rename(hidden)
    process = None
    worker_module = ["uv","run","--no-project","-m","agent_knowledge.fact_source_lifecycle"]
    argv = [*worker_module,"worker","--project",c.f.scope.project,"--config",str(config),
        "--iterations","1000","--poll-seconds",".1","--retry-seconds",".2","--lease-seconds","2"]
    def stop():
        if process and process.poll() is None:
            os.killpg(process.pid,signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5)
    def review():
        with c.f.store._scope() as db:
            return db.execute("SELECT * FROM lbrain_fact_policy_reviews WHERE publication_id=%s",(publication_id,)).fetchone()
    def wait_for(predicate):
        deadline = time.monotonic()+20
        while not predicate():
            assert process.poll() is None,"policy lifecycle process exited"
            assert time.monotonic()<deadline,"policy lifecycle automatic recovery deadline exceeded"
            time.sleep(.05)
    with (tmp_path/"policy-worker.log").open("wb") as log:
        try:
            process = subprocess.Popen(argv,env=env,stdout=log,stderr=log,start_new_session=True)
            wait_for(lambda:review()["task_id"] and c.service.queue.get(review()["task_id"])["state"]=="blocked")
            stop()
            time.sleep(.12)
            monitor = subprocess.run([*worker_module,"monitor","--project",c.f.scope.project,
                "--heartbeat-grace",".1","--overdue-grace",".1","--pending-age-seconds",".1"],
                env=env,capture_output=True,timeout=20,check=True)
            unhealthy = json.loads(monitor.stdout)
            assert unhealthy["attention_required"] and unhealthy["policy_reviews"]["unfinished"]==1
            assert not unhealthy["reviews"]["heartbeat_recent"]
            hidden.rename(c.root)
            process = subprocess.Popen(argv,env=env,stdout=log,stderr=log,start_new_session=True)
            wait_for(lambda:c.service.queue.get(original)["state"]=="resolved" and review()["state"]=="verified")
            result = c.service.result(original)
            assert result["review_resolution"]["current"]["value"]=="failed"
            assert not result["policy_review"]["original_change_applied"]
            assert fixtures.counts(c.f)["steward_card_decisions"]==3
        finally:
            stop()
            if hidden.exists():
                hidden.rename(c.root)
    config.rename(tmp_path/"removed-policy-config")
    receipt = subprocess.run([*module,"receipt","--project",c.f.scope.project,"--request-key","actual-publication"],
        env=env,capture_output=True,timeout=20,check=True)
    assert json.loads(receipt.stdout)["review"]["state"]=="verified"


def test_history_only_review_is_not_misreported_as_current_policy_verification(coordinator,monkeypatch):
    import copy
    import time
    c = coordinator
    seed(c)
    old_request = copy.deepcopy(c.request)
    old_request["idempotency_key"]="history-only-pending"
    original = c.service.submit(old_request)["changes"][0]["task_id"]
    source_with_new_field(c,"ready")
    rule = replace(c.rule,json_pointer="/next_status",publication_revision=1)
    lifecycle = publisher(c,rule)
    publish(c,rule)
    lifecycle.dispatch_once(project=c.f.scope.project,retry_seconds=.1)
    with c.f.store._scope() as db:
        first = db.execute("SELECT task_id FROM lbrain_fact_policy_reviews").fetchone()["task_id"]
    worker = lifecycle.coordinator.worker(project=c.f.scope.project,worker_id="moving-source")
    original_head = c.service.git_reader._head
    moved = False
    def move_between_reader_head_checks(policy):
        nonlocal moved
        revision = original_head(policy)
        if not moved:
            moved = True
            source_with_new_field(c,"failed")
        return revision
    # 실제 reader의 두 HEAD 확인 사이에 ref를 바꾼다. 관측의 current_verified를
    # 직접 조작하지 않고 실제 옛 blob 읽기를 history로 판정하게 한다.
    monkeypatch.setattr(c.service.git_reader,"_head",move_between_reader_head_checks)
    outcome = worker.run_once(task_id=first)
    assert outcome["state"]=="resolved" and outcome["reason_code"]=="retain_history",outcome
    assert lifecycle.review_snapshot(c.service.queue.get(original))["status"]=="pending"
    assert lifecycle.dispatch_once(project=c.f.scope.project,retry_seconds=.1)["verified"]==0
    time.sleep(.12)
    assert lifecycle.dispatch_once(project=c.f.scope.project,retry_seconds=.1)["scheduled"]==1
    with c.f.store._scope() as db:
        second = db.execute("SELECT task_id FROM lbrain_fact_policy_reviews").fetchone()["task_id"]
    assert second!=first
    monkeypatch.setattr(c.service.git_reader,"_head",original_head)
    assert worker.run_once(task_id=second)["state"]=="resolved"
    assert lifecycle.review_snapshot(c.service.queue.get(original))["status"]=="verified"
    with c.f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_policy_review_attempts").fetchone()["n"]==2


def test_original_review_cannot_complete_against_an_older_publication(coordinator,monkeypatch):
    c = coordinator
    original,child,lifecycle,publication = reviewed_original(c)
    worker = c.service.worker(project=c.f.scope.project,worker_id="old-review")
    apply = worker.apply
    def publish_before_apply(*arguments):
        next_rule = replace(lifecycle.coordinator.policies[c.rule.policy_id],publication_revision=2)
        publish(c,next_rule,expected=lifecycle.coordinator.policies[c.rule.policy_id].version,key="newer-publication")
        return apply(*arguments)
    monkeypatch.setattr(worker,"apply",publish_before_apply)
    outcome = worker.run_once(task_id=original)
    assert outcome["state"]=="blocked" and outcome["reason_code"]=="policy_review_changed",outcome
    with c.f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_policy_review_results").fetchone()["n"]==0


def test_concurrent_cas_publications_do_not_silently_overwrite_each_other(coordinator):
    from concurrent.futures import ThreadPoolExecutor
    c = coordinator
    seed(c)
    rules = [replace(c.rule,publication_revision=1,max_age_seconds=age) for age in (60,90)]
    def race(index):
        try:
            return publish(c,rules[index],key="racing-publisher-"+str(index))
        except FactResolutionError as exc:
            return {"reason_code":exc.reason_code}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(race,range(2)))
    assert sum("publication_id" in row for row in results)==1
    assert sum(row.get("reason_code")=="policy_publication_revision_conflict" for row in results)==1
    with c.f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_policy_publications").fetchone()["n"]==1


def test_publication_keeps_actual_previous_definition_and_new_definition(coordinator):
    from agent_knowledge.public_safe_util import hash_payload
    c = coordinator
    seed(c)
    rule = replace(c.rule,json_pointer="/next_status",publication_revision=1)
    publish(c,rule)
    with c.f.store._scope() as db:
        row = db.execute("SELECT previous_descriptor,descriptor FROM lbrain_fact_policy_publications").fetchone()
    assert hash_payload(row["previous_descriptor"])==c.rule.version
    assert hash_payload(row["descriptor"])==rule.version
    assert row["previous_descriptor"]["json_pointer"]=="/status" and row["descriptor"]["json_pointer"]=="/next_status"


def test_legacy_missing_definition_does_not_invent_history_or_block_verified_reinterpretation(coordinator):
    c = coordinator
    seed(c)
    with c.f.store.transaction() as db:
        db.execute("UPDATE lbrain_fact_admission_rules SET policy_descriptor=NULL")
    rule = replace(c.rule,json_pointer="/next_status",publication_revision=1)
    publish(c,rule)
    with c.f.store._scope() as db:
        row = db.execute("SELECT previous_version,previous_descriptor FROM lbrain_fact_policy_publications").fetchone()
    assert row["previous_version"]==c.rule.version and row["previous_descriptor"] is None


def test_legacy_previous_config_is_accepted_only_when_its_hash_matches_the_registered_version(coordinator):
    c = coordinator
    seed(c)
    with c.f.store.transaction() as db:
        db.execute("UPDATE lbrain_fact_admission_rules SET policy_descriptor=NULL")
    rule = replace(c.rule,publication_revision=1)
    lifecycle = publisher(c,rule)
    arguments = dict(project=c.f.scope.project,policy_id=rule.policy_id,
        expected_policy_version=c.rule.version,request_key="backfill-previous-definition",actor="fixture-admin")
    with pytest.raises(FactResolutionError,match="policy_previous_definition_mismatch"):
        lifecycle.publish(**arguments,previous_descriptor=rule.public_identity())
    result = lifecycle.publish(**arguments,previous_descriptor=c.rule.public_identity())
    assert not result["canonical_write_performed"]
    with c.f.store._scope() as db:
        assert db.execute("SELECT previous_descriptor FROM lbrain_fact_policy_publications").fetchone()["previous_descriptor"] is not None


def native_after_publication(c,source,tmp_path, *, correct_before=True, activate=True):
    from test_source_observation import observe,correct,run,capture,native_tasks
    read = observe(c)
    historical = None
    if correct_before:
        original = correct(c,read)["changes"][0]["task_id"]
        assert run(c,original)["state"]=="resolved"
        historical = c.service.result(original)["resolution"]["resolution_id"]
    source_with_new_field(c)
    rule = replace(c.rule,json_pointer="/next_status",publication_revision=1)
    lifecycle = publisher(c,rule)
    publication = publish(c,rule)
    lifecycle.dispatch_once(project=c.f.scope.project)
    with c.f.store._scope() as db:
        child = db.execute("SELECT task_id FROM lbrain_fact_policy_reviews").fetchone()["task_id"]
    assert lifecycle.coordinator.worker(project=c.f.scope.project,worker_id="new-policy").run_once(task_id=child)["state"]=="resolved"
    bridge = capture(c,source,tmp_path,read,activate=activate)
    assert bridge.dispatch_once(project=c.f.scope.project,batch_size=100)["queued"]==1
    [native] = native_tasks(c)
    return native,read,historical,lifecycle,publication


@pytest.mark.parametrize("correct_before",[False,True])
def test_old_policy_native_receipt_is_history_and_never_independent_support_for_new_policy(
        coordinator,actual_couch_source,tmp_path,correct_before):
    from psycopg.errors import ForeignKeyViolation
    c = coordinator
    native,read,historical,lifecycle,publication = native_after_publication(
        c,actual_couch_source,tmp_path,correct_before=correct_before)
    outcome = c.service.worker(project=c.f.scope.project,worker_id="late-old-policy").run_once(task_id=native["task_id"])
    assert outcome["state"]=="resolved" and outcome["reason_code"]=="policy_revalidated",outcome
    result = c.service.result(native["task_id"])
    joined = result["policy_review"]["native_source_read"]
    assert joined["historical_resolution_id"]==historical and joined["join_only"]
    assert not joined["authority_write_performed"] and not result["policy_review"]["original_change_applied"]
    assert result["review_resolution"]["current"]["value"]=="failed"
    assert fixtures.counts(c.f)["steward_card_decisions"]==2+correct_before
    assert fixtures.counts(c.f)["lbrain_fact_assertions"]==1+correct_before
    with c.f.store._scope() as db:
        saved = db.execute("""SELECT n.*,r.redacted_content FROM lbrain_fact_native_policy_read_links n
            JOIN lbrain_fact_source_receipts r ON r.source_ref_id=n.native_source_ref_id""").fetchone()
        assert saved["observation_ref"]==read["observation_ref"] and saved["historical_resolution_id"]==historical
        assert read["observation_ref"] in saved["redacted_content"]
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_native_read_links").fetchone()["n"]==0
    with pytest.raises(ForeignKeyViolation),c.f.store.transaction() as db:
        db.execute("DELETE FROM lbrain_fact_source_read_tasks WHERE task_id=%s",(native["task_id"],))


def test_inactive_late_native_is_not_admitted_just_because_new_policy_was_verified(
        coordinator,actual_couch_source,tmp_path):
    import time
    from agent_knowledge.couchdb_source.source_revision import activate_source_revision
    c = coordinator
    native,read,historical,lifecycle,publication = native_after_publication(c,actual_couch_source,tmp_path,activate=False)
    worker = c.service.worker(project=c.f.scope.project,worker_id="late-inactive",retry_seconds=.1)
    outcome = worker.run_once(task_id=native["task_id"])
    assert outcome["state"]=="blocked" and outcome["reason_code"]=="native_source_not_ready",outcome
    with c.f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_native_policy_read_links").fetchone()["n"]==0
    activate_source_revision(store=actual_couch_source,
        session_id_hash=native["request"]["native_source_read"]["session_id_hash"])
    time.sleep(.12)
    assert worker.run_once(task_id=native["task_id"])["state"]=="resolved"
    assert fixtures.counts(c.f)["steward_card_decisions"]==3


def test_native_policy_history_and_wrapper_commit_together_and_recover_without_catalog(
        coordinator,actual_couch_source,tmp_path,monkeypatch):
    c = coordinator
    native,read,historical,lifecycle,publication = native_after_publication(c,actual_couch_source,tmp_path)
    worker = c.service.worker(project=c.f.scope.project,worker_id="native-policy-response-loss")
    finish = c.service.queue.finish
    def crash(*args,**kwargs):
        raise SystemExit(91)
    monkeypatch.setattr(c.service.queue,"finish",crash)
    with pytest.raises(SystemExit):
        worker.run_once(task_id=native["task_id"])
    monkeypatch.setattr(c.service.queue,"finish",finish)
    with c.f.store.transaction() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_native_policy_read_links").fetchone()["n"]==1
        db.execute("UPDATE lbrain_recheck_tasks SET lease_until=clock_timestamp()-interval '1 second' WHERE task_id=%s",
            (native["task_id"],))
    c.service.native_catalog = None
    hidden = c.root.with_name("native-policy-source-offline")
    c.root.rename(hidden)
    c.service.policies = {}
    try:
        outcome = worker.run_once(task_id=native["task_id"])
        assert outcome["state"]=="resolved",outcome
    finally:
        hidden.rename(c.root)
    assert c.service.result(native["task_id"])["policy_review"]["native_source_read"]["historical_resolution_id"]==historical
    assert fixtures.counts(c.f)["steward_card_decisions"]==3


def test_policy_reinterpretation_keeps_independent_support_and_cannot_choose_through_a_real_conflict(coordinator):
    import copy
    from agent_knowledge.public_safe_util import sha256_text
    c = coordinator
    seed(c)
    second_text = '{"status":"ready"}'
    (c.root/"second.json").write_text(second_text)
    git(c.root,"add","second.json");git(c.root,"commit","-m","독립 합성 지지 원천")
    second_rule = replace(c.rule,policy_id="independent",relative_path="second.json")
    combined = FactCorrectionCoordinator(store=c.f.store,git_reader=c.service.git_reader,policies=[c.rule,second_rule])
    request = copy.deepcopy(c.request)
    request["idempotency_key"]="second-support"
    request["changes"][0]["policy_id"]=second_rule.policy_id
    request["changes"][0]["evidence"]={"revision":git(c.root,"rev-parse","HEAD"),"content_hash":sha256_text(second_text)}
    second = combined.submit(request)["changes"][0]["task_id"]
    assert combined.worker(project=c.f.scope.project,worker_id="independent").run_once(task_id=second)["state"]=="resolved"
    source_with_new_field(c)
    new_rule = replace(c.rule,json_pointer="/next_status",publication_revision=1)
    service = FactCorrectionCoordinator(store=c.f.store,git_reader=c.service.git_reader,policies=[new_rule,second_rule])
    lifecycle = FactPolicyLifecycle(service)
    lifecycle.publish(project=c.f.scope.project,policy_id=new_rule.policy_id,expected_policy_version=c.rule.version,
        request_key="reinterpret-one-source",actor="fixture-admin")
    page = GraphFirstResolver(store=c.f.store).resolve(project=c.f.scope.project,mode="list")
    assert page["items"][0]["typed_payload"]["status_value"]=="ready"
    lifecycle.dispatch_once(project=c.f.scope.project)
    with c.f.store._scope() as db:
        task = db.execute("SELECT task_id FROM lbrain_fact_policy_reviews").fetchone()["task_id"]
    outcome = service.worker(project=c.f.scope.project,worker_id="new-conflicting-policy").run_once(task_id=task)
    assert outcome["state"]=="waiting" and outcome["reason_code"]=="disputed",outcome
    assert fixtures.counts(c.f)["steward_card_decisions"]==2
    assert service.result(task)["resolution"]["current"]["value"]=="ready"
