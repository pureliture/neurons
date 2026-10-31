"""공통 사실을 일반 writer나 직접 SQL이 우회하지 못하는지 실제 PG에서 검사한다."""
from dataclasses import replace

import psycopg
import pytest

from agent_knowledge.postgres_store.pgvector_store import make_dummy_vector
from agent_knowledge.public_safe_util import sha256_text
from test_fact_resolution import facts, coordinator, observation, set_current, invoke, head_card


def managed(f):
    obs = observation(f, 2)
    set_current(f, obs)
    return invoke(f, obs, "ready")


def test_direct_sql_cannot_change_managed_card_even_with_a_past_resolution_id(facts):
    f = facts
    first = managed(f)
    card_id = first["current"]["memory_id"]
    original = f.store.get_steward_card(card_id)
    with pytest.raises(psycopg.errors.CheckViolation, match="managed_fact_resolution_required"):
        with f.store.transaction() as db:
            db.execute("SELECT set_config('neurons.fact_resolution_id',%s,true)", (first["resolution_id"],))
            db.execute("UPDATE memory_cards SET summary=%s WHERE memory_id=%s", ("우회한 내용", card_id))
    assert f.store.get_steward_card(card_id) == original
    assert f.store.get_card(card_id).summary == original["summary"]


@pytest.mark.parametrize("method", ["insert_card", "upsert_card"])
def test_generic_writer_cannot_create_a_new_current_copy_of_a_known_fact(facts, method):
    f = facts
    first = managed(f)
    original = f.store.get_card(first["current"]["memory_id"])
    duplicate = replace(original, memory_id="unadmitted_duplicate", summary="기능 상태: missing",
        content_hash=sha256_text("unadmitted-copy"), typed_payload={**original.typed_payload, "status_value": "missing"})
    with pytest.raises(psycopg.errors.CheckViolation, match="managed_fact_resolution_required"):
        getattr(f.store, method)(duplicate)
    assert f.store.get_card(duplicate.memory_id) is None
    with f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM embedding_outbox WHERE target_id=%s", (duplicate.memory_id,)).fetchone()["n"] == 0
        assert db.execute("SELECT count(*) AS n FROM graph_projection_outbox WHERE source_id=%s", (duplicate.memory_id,)).fetchone()["n"] == 0


def test_managed_source_history_cannot_be_deleted_by_generic_sql(facts):
    f = facts
    managed(f)
    with pytest.raises(psycopg.errors.CheckViolation, match="managed_fact_history_retention_required"):
        with f.store.transaction() as db:
            db.execute("DELETE FROM memory_cards WHERE memory_id=%s", (f.old["memory_id"],))
    assert f.store.get_steward_card(f.old["memory_id"])["currentness"] == "superseded"


def test_existing_embedding_lease_path_and_next_verified_correction_still_work(facts):
    f = facts
    first = managed(f)
    card_id = first["current"]["memory_id"]
    job = next(job for job in f.store.claim_outbox_leases(worker_id="guard-embedding", batch_size=10)
        if job.target_id == card_id)
    assert f.store.cas_update_embedding(job.outbox_id, card_id, job.content_hash,
        make_dummy_vector(7), worker_id="guard-embedding")
    assert f.store.get_card(card_id).embedding_state == "ready"
    following = observation(f, 3)
    set_current(f, following)
    result = invoke(f, following, "failed", key="following", target=head_card(f, first))
    assert result["action"] == "replace_current" and result["current"]["value"] == "failed"


def test_generic_writer_cannot_rename_an_unmanaged_current_card_into_a_managed_fact(facts):
    f = facts
    first = managed(f)
    current = f.store.get_card(first["current"]["memory_id"])
    independent = replace(current, memory_id="independent", title="다른 사실")
    f.store.insert_card(independent)
    with pytest.raises(psycopg.errors.CheckViolation, match="managed_fact_resolution_required"):
        with f.store.transaction() as db:
            db.execute("UPDATE memory_cards SET title=%s WHERE memory_id=%s", (current.title, independent.memory_id))
    assert f.store.get_card(independent.memory_id).title == "다른 사실"


def test_same_title_in_other_project_or_card_type_is_not_globally_blocked(facts):
    f = facts
    first = managed(f)
    current = f.store.get_card(first["current"]["memory_id"])
    for changes in ({"project": "independent-project"}, {"card_type": "task"}):
        card = replace(current, memory_id="independent_" + next(iter(changes)), **changes)
        f.store.insert_card(card)
        assert f.store.get_card(card.memory_id).authorization_status == "active"


def test_old_repeatable_read_snapshot_cannot_miss_a_new_fact_registration(facts):
    f = facts
    with pytest.raises(psycopg.errors.SerializationFailure):
        with f.store.transaction() as stale:
            stale.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            assert stale.execute("SELECT count(*) AS n FROM lbrain_fact_members").fetchone()["n"] == 0
            first = managed(f)
            current = f.store.get_card(first["current"]["memory_id"])
            from agent_knowledge.postgres_store.pgvector_store import PgVectorStore
            PgVectorStore(connection=stale).insert_card(replace(current, memory_id="stale_snapshot_duplicate"))
    assert f.store.get_card("stale_snapshot_duplicate") is None
    assert f.store.get_steward_card(first["current"]["memory_id"])["currentness"] == "current"


def test_canonical_failure_does_not_leave_a_mutation_intent_or_successor(facts):
    from agent_knowledge.fact_resolution import FactResolutionError
    f = facts
    obs = observation(f, 2)
    set_current(f, obs)
    def failing(**arguments):
        f.commit(**arguments)
        raise FactResolutionError("test_after_canonical_failure")
    with pytest.raises(FactResolutionError, match="test_after_canonical_failure"):
        invoke(f, obs, "ready", commit=failing)
    with f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_mutation_intents").fetchone()["n"] == 0
        assert db.execute("SELECT count(*) AS n FROM memory_cards").fetchone()["n"] == 1
    assert f.store.get_steward_card(f.old["memory_id"])["currentness"] == "current"


def test_queued_first_correction_already_guards_the_target_and_new_current_copies(coordinator):
    c = coordinator
    with c.f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_admission_rules").fetchone()["n"] == 0
    receipt = c.service.submit(c.request)
    with c.f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_scopes").fetchone()["n"] == 0
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_admission_rules").fetchone()["n"] == 1
    with pytest.raises(psycopg.errors.CheckViolation, match="managed_fact_resolution_required"):
        c.f.store.insert_card(replace(c.f.store.get_card(c.f.old["memory_id"]), memory_id="queued_duplicate"))
    with pytest.raises(psycopg.errors.CheckViolation, match="managed_fact_resolution_required"):
        with c.f.store.transaction() as db:
            db.execute("UPDATE memory_cards SET title=%s WHERE memory_id=%s", ("다른 이름", c.f.old["memory_id"]))
    task_id = receipt["changes"][0]["task_id"]
    assert c.service.worker(project=c.f.scope.project, worker_id="queued-guard").run_once(task_id=task_id)["state"] == "resolved"
    assert c.service.result(task_id)["resolution"]["applied_value"] == "ready"


def test_stale_worker_and_submitter_cannot_reactivate_a_revoked_registered_rule(coordinator):
    import copy
    c = coordinator
    task_id = c.service.submit(c.request)["changes"][0]["task_id"]
    # 관리 설정 철회를 재현한다. 이 SQL은 product의 활성화 API를 입증하지 않는다.
    with c.f.store.transaction() as db:
        db.execute("UPDATE lbrain_fact_admission_rules SET active=FALSE WHERE project=%s", (c.f.scope.project,))
    result = c.service.worker(project=c.f.scope.project, worker_id="stale-guard").run_once(task_id=task_id)
    assert result["state"] == "blocked" and result["reason_code"] == "fact_policy_changed"
    request = copy.deepcopy(c.request)
    request["idempotency_key"] = "after-rule-revocation"
    rejected = c.service.submit(request)["changes"][0]
    assert rejected["status"] == "rejected" and rejected["reason_code"] == "fact_policy_changed"
    with c.f.store._scope() as db:
        assert db.execute("SELECT active FROM lbrain_fact_admission_rules").fetchone()["active"] is False
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_mutation_intents").fetchone()["n"] == 0
    assert c.f.store.get_steward_card(c.f.old["memory_id"])["typed_payload"]["status_value"] == "missing"


def test_committed_intent_cannot_be_rebound_or_deleted_and_schema_reapply_preserves_it(facts):
    f = facts
    first = managed(f)
    f.store.execute_ddl()
    with f.store._scope() as db:
        intent = db.execute("SELECT * FROM lbrain_fact_mutation_intents").fetchone()
        assert intent["resolution_id"] == first["resolution_id"]
        assert intent["target_memory_id"] == f.old["memory_id"]
        assert intent["candidate_memory_id"] == first["current"]["memory_id"]
    for statement in ("UPDATE lbrain_fact_mutation_intents SET transaction_id=pg_current_xact_id()::text",
                      "DELETE FROM lbrain_fact_mutation_intents"):
        with pytest.raises(psycopg.errors.CheckViolation, match="managed_fact_history_retention_required"):
            with f.store.transaction() as db:
                db.execute(statement)
    retry = invoke(f, observation(f, 2), "ready")
    assert retry["duplicate"] and not retry["authority_write_performed"]


def test_intent_cannot_commit_with_a_different_successor_in_the_resolution_result(facts):
    from psycopg.types.json import Jsonb
    f = facts
    first = managed(f)
    resolution_id = "factres_" + "f" * 40
    wrong_result = {"action": "replace_current", "authority_write_performed": True,
        "successor_memory_id": f.old["memory_id"], "current": {"memory_id": f.old["memory_id"]}}
    with pytest.raises(psycopg.errors.CheckViolation, match="managed_fact_resolution_result_required"):
        with f.store.transaction() as db:
            db.execute("""INSERT INTO lbrain_fact_resolutions
                (resolution_id,fact_key,request_hash,terminal,result) VALUES (%s,%s,%s,TRUE,%s)""",
                (resolution_id, f.scope.key, sha256_text("invalid-result"), Jsonb(wrong_result)))
            db.execute("""INSERT INTO lbrain_fact_mutation_intents
                (resolution_id,fact_key,transaction_id,target_memory_id,candidate_memory_id)
                VALUES (%s,%s,pg_current_xact_id()::text,%s,%s)""",
                (resolution_id, f.scope.key, first["current"]["memory_id"], first["current"]["memory_id"]))
    with f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_fact_resolutions WHERE resolution_id=%s",
            (resolution_id,)).fetchone()["n"] == 0
