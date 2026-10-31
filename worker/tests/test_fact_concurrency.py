"""동일한 원래 revision을 읽은 실제 동시 요청의 PG 정합성.

coordinator 검사는 실제 Git/JSON reader를, 복수 필드 코어 검사는 명시적
admission callback 대역을 쓴다. 실제 모델·일반 native admission 증거가 아니다.
"""
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

from agent_knowledge.fact_correction_coordinator import FactCorrectionCoordinator
from agent_knowledge.fact_resolution import StewardFactCommit
from agent_knowledge.public_safe_util import sha256_text
from agent_knowledge.session_memory.brain_steward import BrainStewardService
from test_correction_sources import git
from test_fact_resolution import facts, coordinator, observation, set_current, counts


def run_loaded_together(c, task_ids):
    barrier = Barrier(2)
    loaded = []
    def execute(task_id):
        worker = c.service.worker(project=c.f.scope.project, worker_id=task_id, retry_seconds=.1)
        load = worker.load
        def synchronized_load(task):
            snapshot = load(task)
            loaded.append(snapshot.data["observation"]["source_revision"])
            barrier.wait(timeout=5)
            return snapshot
        worker.load = synchronized_load
        return worker.run_once(task_id=task_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(execute, task_ids))
    assert loaded == [c.current, c.current]
    return outcomes


def test_concurrent_conflicting_proposals_use_one_actual_git_value(coordinator):
    c = coordinator
    tasks = []
    for value in ("ready", "failed"):
        request = copy.deepcopy(c.request)
        request["idempotency_key"] = "concurrent-proposal-" + value
        request["changes"][0]["proposed_value"] = value
        item = c.service.submit(request)["changes"][0]
        assert item["status"] == "queued", item
        tasks.append(item["task_id"])
    outcomes = run_loaded_together(c, tasks)
    assert all(item["state"] == "resolved" for item in outcomes), outcomes
    results = [c.service.result(task)["resolution"] for task in tasks]
    assert {item["requested_value"] for item in results} == {"ready", "failed"}
    assert {item["applied_value"] for item in results} == {"ready"}
    assert sum(item["authority_write_performed"] for item in results) == 1
    assert counts(c.f)["steward_card_decisions"] == 2
    assert counts(c.f)["lbrain_fact_observations"] == 1
    assert counts(c.f)["lbrain_fact_assertions"] == 1


def test_concurrent_opposing_current_files_do_not_choose_by_lock_order(coordinator):
    c = coordinator
    (c.root / "other.json").write_text('{"status":"failed"}')
    git(c.root, "add", "other.json"); git(c.root, "commit", "-m", "동일 revision의 독립 상충 근거")
    c.current = git(c.root, "rev-parse", "HEAD")
    other = replace(c.rule, policy_id="other-status", relative_path="other.json")
    c.service = FactCorrectionCoordinator(store=c.f.store, git_reader=c.service.git_reader, policies=[c.rule, other])
    tasks = []
    for rule, value in ((c.rule, "ready"), (other, "failed")):
        request = copy.deepcopy(c.request)
        request["idempotency_key"] = "concurrent-source-" + rule.policy_id
        request["changes"][0].update(policy_id=rule.policy_id, proposed_value=value,
            evidence={"revision": c.current, "content_hash": sha256_text('{"status":"' + value + '"}')})
        item = c.service.submit(request)["changes"][0]
        assert item["status"] == "queued", item
        tasks.append(item["task_id"])
    outcomes = run_loaded_together(c, tasks)
    assert all(item["state"] == "waiting" for item in outcomes), outcomes
    with c.f.store._scope() as db:
        assert db.execute("SELECT value,state FROM lbrain_fact_scopes").fetchone() == {"value": "missing", "state": "disputed"}
        assert {row["value"] for row in db.execute("SELECT value FROM lbrain_fact_assertions").fetchall()} == {"ready", "failed"}
    assert counts(c.f)["steward_card_decisions"] == 1


def test_two_actual_connections_update_different_fields_from_same_old_revision(facts):
    f = facts
    steward = BrainStewardService(f.ledger, pgvector_store=f.store, allow_restricted=True)
    span = {**f.span, "title": "동시 복수 사실", "redacted_summary": "기능 상태: missing; 배포 상태: pending",
        "typed_payload": {**f.span["typed_payload"], "deployment_status": "pending"},
        "source_ref": {"source_id": "concurrent-fields"}, "content_hash": sha256_text("concurrent-fields")}
    memory_id = steward.candidate_create(source_span=span)["memory_id"]
    steward.candidate_approve(candidate_memory_id=memory_id, approved_by="fixture", decision_id="concurrent-fields-seed")
    original = f.store.get_steward_card(memory_id)
    initial_decisions = counts(f)["steward_card_decisions"]
    a, b = observation(f, 2, source="code"), observation(f, 3, source="deployment")
    set_current(f, a); set_current(f, b)
    barrier = Barrier(2)
    backend_pids = []
    def execute(spec):
        field, predicate, template, value, obs = spec
        commit = StewardFactCommit(field_path=field, render_template=template, policy_id=predicate)
        def record_connection(**kwargs):
            backend_pids.append(kwargs["connection"].execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
            return commit(**kwargs)
        barrier.wait(timeout=5)
        return f.resolver.reconcile(scope=replace(f.scope, predicate=predicate), memory_id=memory_id,
            field_path=field, render_template=template, expected_content_hash=original["content_hash"],
            value=value, observation=obs, request_key=predicate, request_hash=sha256_text(predicate),
            requested_value=value, is_current=lambda item: f.current.get(item.source_id) == (item.source_revision, item.interpretation_version),
            compare=lambda old,new: "unknown", commit=record_connection)
    specs = [("typed_payload.status_value", "status", "기능 상태: {value}", "ready", a),
        ("typed_payload.deployment_status", "deployment_status", "배포 상태: {value}", "deployed", b)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, specs))
    assert len(set(backend_pids)) == 2
    assert all(item["authority_write_performed"] for item in results)
    with f.store._scope() as db:
        heads = db.execute("SELECT memory_id FROM lbrain_fact_scopes").fetchall()
    assert len(heads) == 2 and len({row["memory_id"] for row in heads}) == 1
    final = f.resolver.project_cards([f.store.get_steward_card(heads[0]["memory_id"])])[0]
    assert final["summary"] == "기능 상태: ready; 배포 상태: deployed"
    assert final["typed_payload"]["status_value"] == "ready" and final["typed_payload"]["deployment_status"] == "deployed"
    assert {item["head_memory_id"] for item in final["fact_views"]} == {final["memory_id"]}
    assert original["typed_payload"]["status_value"] == "missing" and original["typed_payload"]["deployment_status"] == "pending"
    assert counts(f)["steward_card_decisions"] == initial_decisions + 2


def test_multiple_source_policies_cannot_reassign_one_field_to_another_fact(coordinator):
    c = coordinator
    other = replace(c.rule, policy_id="other-fact", scope=replace(c.rule.scope, predicate="another-predicate"))
    c.service = FactCorrectionCoordinator(store=c.f.store, git_reader=c.service.git_reader, policies=[c.rule,other])
    assert c.service.submit(c.request)["changes"][0]["status"] == "queued"
    request = copy.deepcopy(c.request)
    request["idempotency_key"] = "attempt-another-fact"
    request["changes"][0]["policy_id"] = other.policy_id
    item = c.service.submit(request)["changes"][0]
    assert item["status"] == "rejected" and item["reason_code"] == "fact_policy_changed"
    with c.f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_admission_rules").fetchone()["n"] == 1
        assert db.execute("SELECT fact_key FROM lbrain_fact_admission_targets").fetchone()["fact_key"] == c.rule.scope.key


def test_legacy_single_policy_schema_upgrades_without_rewriting_existing_registration(coordinator):
    c = coordinator
    other = replace(c.rule, policy_id="second-source", relative_path="other.json")
    c.service = FactCorrectionCoordinator(store=c.f.store, git_reader=c.service.git_reader, policies=[c.rule,other])
    assert c.service.submit(c.request)["changes"][0]["status"] == "queued"
    with c.f.store.transaction() as db:
        original = db.execute("SELECT * FROM lbrain_fact_admission_rules").fetchone()
        # 작업 전용 schema에서 기존 단일 정책 제약을 재구성한다.
        db.execute("ALTER TABLE lbrain_fact_admission_rules DROP CONSTRAINT lbrain_fact_admission_target_scope")
        db.execute("DROP TRIGGER lbrain_admit_fact_target_scope ON lbrain_fact_admission_rules")
        db.execute("DROP TABLE lbrain_fact_admission_targets")
        db.execute("ALTER TABLE lbrain_fact_admission_rules ADD CONSTRAINT legacy_one_source UNIQUE(project,target_title,card_type,field_path)")
    c.f.store.execute_ddl()
    with c.f.store._scope() as db:
        assert db.execute("SELECT * FROM lbrain_fact_admission_rules").fetchone() == original
    request = copy.deepcopy(c.request)
    request["idempotency_key"] = "second-after-upgrade"
    request["changes"][0]["policy_id"] = other.policy_id
    # 등록은 source를 읽거나 새 사실을 승인하지 않는다.
    assert c.service.submit(request)["changes"][0]["status"] == "queued"
    c.f.store.execute_ddl()
    with c.f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_admission_rules").fetchone()["n"] == 2
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_admission_targets").fetchone()["n"] == 1


def test_conflicting_legacy_target_ownership_aborts_upgrade_without_reclassification(coordinator):
    import psycopg
    import pytest
    c = coordinator
    c.service.submit(c.request)
    with c.f.store.transaction() as db:
        db.execute("ALTER TABLE lbrain_fact_admission_rules DROP CONSTRAINT lbrain_fact_admission_target_scope")
        db.execute("DROP TRIGGER lbrain_admit_fact_target_scope ON lbrain_fact_admission_rules")
        db.execute("""INSERT INTO lbrain_fact_admission_rules
            (project,policy_id,policy_version,fact_key,target_memory_id,target_title,card_type,field_path)
            SELECT project,'legacy-conflict',policy_version,%s,target_memory_id,target_title,card_type,field_path
            FROM lbrain_fact_admission_rules""", (replace(c.rule.scope,predicate="another-predicate").key,))
        before = db.execute("SELECT * FROM lbrain_fact_admission_rules ORDER BY policy_id").fetchall()
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        c.f.store.execute_ddl()
    with c.f.store._scope() as db:
        assert db.execute("SELECT * FROM lbrain_fact_admission_rules ORDER BY policy_id").fetchall() == before


def test_deferred_initial_schedule_still_requires_a_real_fact_at_commit(facts):
    import psycopg
    import pytest
    f = facts
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with f.store.transaction() as db:
            db.execute("INSERT INTO lbrain_fact_check_schedules(fact_key,project,policy_id) VALUES (%s,%s,%s)",
                (f.scope.key,f.scope.project,"no-existing-head"))
    with f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_check_schedules").fetchone()["n"] == 0
