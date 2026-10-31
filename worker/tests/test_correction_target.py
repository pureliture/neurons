"""대상 의미 판단의 경계 검사. 주입 응답은 모델 정확도 증거가 아니다."""

import copy
import json
from types import SimpleNamespace

import pytest

from agent_knowledge import correction_target as target
from agent_knowledge.correction_capabilities import CapabilityUnavailable
from agent_knowledge.correction_target_identity import (
    CorrectionTargetClient,
    DEFAULT_CORRECTION_MODEL,
    questions,
)
from agent_knowledge.correction_target_identity import TargetIdentityPolicy
from agent_knowledge.public_safe_util import hash_payload, sha256_text
import test_fact_resolution as fixtures

facts = fixtures.facts
coordinator = fixtures.coordinator
MODEL = DEFAULT_CORRECTION_MODEL + "-20261016"


def policy():
    return TargetIdentityPolicy(0.8, "unit-only", MODEL, hash_payload(questions()))


def client(labels):
    calls = []

    def transport(url, body, key, timeout):
        data = json.loads(body)
        calls.append(data)
        text = data["state"]["candidate_fact"]
        label = labels(text) if callable(labels) else labels
        label = {
            "supported": "same_fact",
            "insufficient": "uncertain",
            "unrelated": "different_fact",
            "contradicted": "different_fact",
        }.get(label, label)
        scores = {name: 0.01 for name in ("same_fact", "different_fact", "uncertain")}
        scores[label] = 0.98
        return 200, json.dumps(
            {
                "model": MODEL,
                "answers": {
                    "identity": {
                        "type": "choice",
                        "choice": label,
                        "probabilities": scores,
                    }
                },
            }
        ).encode()

    return SimpleNamespace(
        value=CorrectionTargetClient(api_key="unit-only", transport=transport),
        calls=calls,
    )


def snapshot(*titles):
    return {
        "schema_version": "target_inventory.v1",
        "complete": True,
        "description": "교정 기능의 현재 구현 상태",
        "property": {"subject": "correction", "predicate": "implementation_status",
            "field_path": "typed_payload.status_value", "render_template": "교정 구현 상태: {value}"},
        "context": {
            "project": "test",
            "repository": "neurons",
            "branch": "refs/heads/main",
            "environment": "test",
        },
        "candidates": [
            {
                "memory_id": f"mem_{i}",
                "project": "test",
                "card_type": "status",
                "title": title,
                "summary": "기존 상태는 missing이다.",
                "content_hash": sha256_text(title),
                "fact_key": None,
                "context": {},
            }
            for i, title in enumerate(titles)
        ],
    }


def test_one_match_and_confident_distinct_candidates_are_required():
    data = snapshot("교정 상태", "다른 항목")
    fake = client(
        lambda text: "supported" if "title: 교정 상태" in text else "unrelated"
    )
    selected = target.select_target(
        data, client=fake.value, policy=policy(), policy_version="unit-v1"
    )
    assert selected["memory_id"] == "mem_0" and len(fake.calls) == 2
    assert target.verify_selection(data, selected, policy=policy(), policy_version="unit-v1") == "mem_0"


def test_registered_other_fact_is_excluded_without_call_and_unknown_scope_is_still_judged():
    data = snapshot("교정 구현", "교정 worker 가동", "미식별 메모")
    data["candidates"][1]["context"] = {"subject": "correction", "predicate": "runtime_status"}
    fake = client(lambda text: "supported" if "title: 교정 구현" in text else "insufficient")
    selected = target.select_target(data, client=fake.value, policy=policy(), policy_version="unit-v1")
    assert len(fake.calls) == 2 and selected["memory_id"] is None
    excluded = selected["judgments"]["mem_1"]
    assert excluded["mismatched_fields"] == ["predicate"]
    excluded["mismatched_fields"] = []
    with pytest.raises(CapabilityUnavailable, match="target_registered_scope_binding_mismatch"):
        target.verify_selection(data, selected, policy=policy(), policy_version="unit-v1")


@pytest.mark.parametrize(
    "label,reason",
    [
        ("supported", "target_ambiguous"),
        ("insufficient", "target_identity_uncertain"),
        ("unrelated", "target_not_found"),
    ],
)
def test_conflict_uncertainty_and_absence_are_distinct(label, reason):
    selected = target.select_target(
        snapshot("a", "b"),
        client=client(label).value,
        policy=policy(),
        policy_version="unit-v1",
    )
    assert selected["memory_id"] is None and selected["reason_code"] == reason


def test_truncated_scope_never_becomes_a_unique_target():
    data = snapshot("교정 상태")
    data["complete"] = False
    fake = client("supported")
    with pytest.raises(CapabilityUnavailable, match="target_search_incomplete"):
        target.select_target(
            data, client=fake.value, policy=policy(), policy_version="unit-v1"
        )
    assert fake.calls == []


@pytest.mark.parametrize(
    "tamper", ["inventory", "selection", "model", "question", "scope"]
)
def test_cached_target_judgment_binding_is_rechecked(tamper):
    data = snapshot("교정 상태")
    selected = target.select_target(
        data,
        client=client("supported").value,
        policy=policy(),
        policy_version="unit-v1",
    )
    if tamper == "inventory":
        data["candidates"][0]["summary"] = "달라진 내용"
    if tamper == "selection":
        selected["memory_id"] = "mem_foreign"
    if tamper == "model":
        selected["judgments"]["mem_0"]["resolved_model"] = "other-model"
    if tamper == "question":
        selected["judgments"]["mem_0"]["question_version"] = "other-question"
    if tamper == "scope":
        data["context"]["project"] = "other"
    with pytest.raises(CapabilityUnavailable):
        target.verify_selection(
            data, selected, policy=policy(), policy_version="unit-v1"
        )


@pytest.fixture
def configured(coordinator, monkeypatch):
    c = coordinator
    c.service.target_search = target.TargetSearchPolicy(
        "unit-only", sha256_text("unit-only"), max_candidates=32
    )
    fake = client("supported")
    c.service.target_client = fake.value
    monkeypatch.setattr(
        target, "load_profile", lambda config: (policy(), {"unit_only": True})
    )
    request = copy.deepcopy(c.request)
    change = request["changes"][0]
    change.pop("target_memory_id")
    change.pop("expected_content_hash")
    change["target_description"] = "불일치한 기억을 근거로 고치는 기능의 구현 여부"
    return SimpleNamespace(c=c, request=request, fake=fake)


def test_description_is_queued_then_verified_outside_authority_lock(configured):
    c, request, fake = configured.c, configured.request, configured.fake
    receipt = c.service.submit(request)
    task_id = receipt["changes"][0]["task_id"]
    assert fake.calls == [] and fixtures.counts(c.f)["steward_card_decisions"] == 1
    worker = c.service.worker(
        project=c.f.scope.project, worker_id="target-worker", retry_seconds=0.1
    )
    original = worker.judge

    def judge(task, snap):
        with c.f.store.transaction() as db:
            db.execute("LOCK TABLE memory_cards IN SHARE ROW EXCLUSIVE MODE NOWAIT")
            assert db.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended('lbrain-steward-authority-write-v1',0)) AS acquired"
            ).fetchone()["acquired"]
        return original(task, snap)

    worker.judge = judge
    result = worker.run_once(task_id=task_id)
    assert result["state"] == "resolved", result
    resolution = c.service.result(task_id)["resolution"]
    assert resolution["applied_value"] == "ready"
    assert resolution["admission"]["target_search"]["memory_id"] == c.f.old["memory_id"]
    assert fixtures.counts(c.f)["steward_card_decisions"] == 2
    assert len(fake.calls) == 1


def test_semantic_uncertainty_is_durable_and_reuses_same_judgment(configured):
    import time

    f = configured
    f.fake.value._transport = client("insufficient").value._transport
    task = f.c.service.submit(f.request)["changes"][0]["task_id"]
    worker = f.c.service.worker(
        project=f.c.f.scope.project, worker_id="target-worker", retry_seconds=0.1
    )
    first = worker.run_once(task_id=task)
    assert (
        first["state"] == "waiting"
        and first["reason_code"] == "target_identity_uncertain"
    ), first
    time.sleep(0.12)
    second = worker.run_once(task_id=task)
    assert second["judgment_reused"] and second["state"] == "waiting"
    assert fixtures.counts(f.c.f)["steward_card_decisions"] == 1


def test_real_inventory_known_other_subject_cannot_be_selected_by_target_model(configured):
    from dataclasses import replace
    from agent_knowledge.session_memory.brain_steward import BrainStewardService
    f = configured
    other_rule = replace(f.c.rule, policy_id="other-subject", target_title="다른 주체의 상태",
        scope=replace(f.c.rule.scope, subject="other_subject"))
    f.c.service.policies[other_rule.policy_id] = other_rule
    steward = BrainStewardService(f.c.f.ledger, pgvector_store=f.c.f.store, allow_restricted=True)
    other = steward.candidate_create(source_span={**f.c.f.span, "title": other_rule.target_title,
        "content_hash": sha256_text("other-registered-subject")})["memory_id"]
    steward.candidate_approve(candidate_memory_id=other, approved_by="fixture", decision_id="other-seed")
    # 대역은 무엇이든 same_fact로 판단한다. 등록된 다른 주체는 호출조차 받지 않는다.
    task = f.c.service.submit(f.request)["changes"][0]["task_id"]
    run = f.c.service.worker(project=f.c.f.scope.project, worker_id="scope-gate").run_once(task_id=task)
    assert run["state"] == "resolved", run
    assert len(f.fake.calls) == 1
    assert f.c.f.store.get_steward_card(other)["typed_payload"]["status_value"] == "missing"
    with f.c.f.store._scope() as db:
        judgment = db.execute("SELECT judgment FROM lbrain_recheck_judgments WHERE task_id=%s", (task,)).fetchone()["judgment"]
    assert judgment["target_selection"]["judgments"][other]["mismatched_fields"] == ["subject"]


def test_complete_negative_search_resolves_absence_without_creating_card(configured):
    f = configured
    f.c.service.target_client = client("unrelated").value
    task = f.c.service.submit(f.request)["changes"][0]["task_id"]
    result = f.c.service.worker(
        project=f.c.f.scope.project, worker_id="target-worker"
    ).run_once(task_id=task)
    assert (
        result["state"] == "resolved" and result["reason_code"] == "target_not_found"
    ), result
    assert fixtures.counts(f.c.f)["steward_card_decisions"] == 1


def test_inventory_changes_after_model_block_apply_and_keep_recheck(
    configured, monkeypatch
):
    f = configured
    task = f.c.service.submit(f.request)["changes"][0]["task_id"]
    worker = f.c.service.worker(
        project=f.c.f.scope.project, worker_id="target-worker", retry_seconds=0.1
    )
    original = worker.apply

    def changed(task, snapshot, judgment):
        from agent_knowledge.session_memory.brain_steward import BrainStewardService

        span = {
            **f.c.f.span,
            "title": "모델 판정 이후 새 후보",
            "content_hash": sha256_text("added-later"),
        }
        steward = BrainStewardService(
            f.c.f.ledger, pgvector_store=f.c.f.store, allow_restricted=True
        )
        candidate = steward.candidate_create(source_span=span)["memory_id"]
        steward.candidate_approve(
            candidate_memory_id=candidate,
            approved_by="fixture",
            decision_id="new-candidate",
        )
        return original(task, snapshot, judgment)

    monkeypatch.setattr(worker, "apply", changed)
    result = worker.run_once(task_id=task)
    assert (
        result["state"] == "blocked"
        and result["reason_code"] == "target_inventory_changed"
    ), result
    assert f.c.service.result(task)["automatic_recheck_scheduled"]
    assert (
        f.c.f.store.get_steward_card(f.c.f.old["memory_id"])["currentness"] == "current"
    )


def test_candidate_limit_is_explicit_and_no_model_is_called(configured):
    from agent_knowledge.session_memory.brain_steward import BrainStewardService

    f = configured
    steward = BrainStewardService(
        f.c.f.ledger, pgvector_store=f.c.f.store, allow_restricted=True
    )
    candidate = steward.candidate_create(
        source_span={
            **f.c.f.span,
            "title": "추가 현재 후보",
            "content_hash": sha256_text("second"),
        }
    )["memory_id"]
    steward.candidate_approve(
        candidate_memory_id=candidate, approved_by="fixture", decision_id="second"
    )
    f.c.service.target_search = target.TargetSearchPolicy(
        "unit-only", sha256_text("unit-only"), max_candidates=1
    )
    task = f.c.service.submit(f.request)["changes"][0]["task_id"]
    result = f.c.service.worker(
        project=f.c.f.scope.project, worker_id="target-worker"
    ).run_once(task_id=task)
    assert (
        result["state"] == "blocked"
        and result["reason_code"] == "target_search_incomplete"
    ), result
    assert f.fake.calls == []


def test_exact_target_does_not_call_target_model(configured):
    f = configured
    task = f.c.service.submit(f.c.request)["changes"][0]["task_id"]
    result = f.c.service.worker(
        project=f.c.f.scope.project, worker_id="target-worker"
    ).run_once(task_id=task)
    assert result["state"] == "resolved", result
    assert f.fake.calls == []


def test_absence_commit_survives_finish_failure_without_model_or_source(
    configured, monkeypatch
):
    f = configured
    f.c.service.target_client = client("unrelated").value
    task = f.c.service.submit(f.request)["changes"][0]["task_id"]
    worker = f.c.service.worker(
        project=f.c.f.scope.project, worker_id="target-worker", retry_seconds=0.1
    )
    original = f.c.service.queue.finish

    def crash(*args, **kwargs):
        raise SystemExit(91)

    monkeypatch.setattr(f.c.service.queue, "finish", crash)
    with pytest.raises(SystemExit):
        worker.run_once(task_id=task)
    assert f.c.service.queue.get(task)["result_ref"].startswith("targetdec_")
    monkeypatch.setattr(f.c.service.queue, "finish", original)
    with f.c.f.store.transaction() as db:
        db.execute(
            "UPDATE lbrain_recheck_tasks SET lease_until=clock_timestamp()-interval '1 second' WHERE task_id=%s",
            (task,),
        )
    hidden = f.c.root.with_name("source-offline")
    f.c.root.rename(hidden)
    f.c.service.target_search = None
    try:
        assert worker.run_once(task_id=task)["state"] == "resolved"
    finally:
        hidden.rename(f.c.root)
    result = f.c.service.result(task)
    assert result["target_resolution"]["reason_code"] == "target_not_found"
    assert result["target_resolution"]["search_complete"]
    assert fixtures.counts(f.c.f)["steward_card_decisions"] == 1


def test_used_target_metadata_and_model_result_are_immutable(configured):
    from psycopg.errors import CheckViolation, ForeignKeyViolation

    f = configured
    task = f.c.service.submit(f.request)["changes"][0]["task_id"]
    assert (
        f.c.service.worker(
            project=f.c.f.scope.project, worker_id="target-worker"
        ).run_once(task_id=task)["state"]
        == "resolved"
    )
    with f.c.f.store._scope() as db:
        proof = db.execute(
            "SELECT * FROM lbrain_fact_target_decisions WHERE task_id=%s", (task,)
        ).fetchone()
    assert proof["resolution_id"]
    assert proof["snapshot"]["candidates"][0]["summary"] == f.c.f.old["summary"]
    with pytest.raises(CheckViolation), f.c.f.store.transaction() as db:
        db.execute(
            "UPDATE lbrain_fact_target_decisions SET result='{}'::jsonb WHERE task_id=%s",
            (task,),
        )
    with (
        pytest.raises((CheckViolation, ForeignKeyViolation)),
        f.c.f.store.transaction() as db,
    ):
        db.execute("DELETE FROM lbrain_recheck_judgments WHERE task_id=%s", (task,))
    with pytest.raises(ForeignKeyViolation), f.c.f.store.transaction() as db:
        db.execute(
            "DELETE FROM lbrain_fact_resolutions WHERE resolution_id=%s",
            (proof["resolution_id"],),
        )


def test_stale_reported_value_does_not_prevent_same_fact_description_join(configured):
    f = configured
    first = f.c.service.submit(f.c.request)["changes"][0]["task_id"]
    worker = f.c.service.worker(project=f.c.f.scope.project, worker_id="target-worker")
    assert worker.run_once(task_id=first)["state"] == "resolved"
    f.request["idempotency_key"] = "description-after-exact"
    second = f.c.service.submit(f.request)["changes"][0]["task_id"]
    assert worker.run_once(task_id=second)["state"] == "resolved"
    receipt = f.c.service.result(second)["resolution"]
    assert (
        receipt["action"] == "attach_evidence"
        and receipt["authority_write_performed"] is False
    )
    assert fixtures.counts(f.c.f)["steward_card_decisions"] == 2


def test_unsupported_current_records_are_not_silently_omitted(configured):
    from psycopg.types.json import Jsonb

    f = configured
    with f.c.f.store.transaction() as db:
        db.execute(
            """INSERT INTO memory_cards(memory_id,project,card_type,title,summary,typed_payload,
            lifecycle_state,authorization_status,currentness,content_hash)
            VALUES ('legacy-current',%s,'status','기존 형식 기록','기존 형식의 상태',%s,'accepted','active','current',%s)""",
            (
                f.c.f.scope.project,
                Jsonb({"status_value": "missing"}),
                sha256_text("legacy"),
            ),
        )
    task = f.c.service.submit(f.request)["changes"][0]["task_id"]
    result = f.c.service.worker(
        project=f.c.f.scope.project, worker_id="target-worker"
    ).run_once(task_id=task)
    assert (
        result["reason_code"] == "target_search_unsupported_records"
        and result["state"] == "blocked"
    ), result
    assert f.fake.calls == []
