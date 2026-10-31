"""명시적 opt-in에서 실제 OpenRouter/Git/PG의 교정·수집·복구를 검사한다."""
import copy
import os
import time

import pytest

from agent_knowledge.fact_correction_coordinator import FactCorrectionCoordinator, GitSemanticFactPolicy
from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
from agent_knowledge.public_safe_util import sha256_text
from test_correction_sources import git
from test_fact_resolution import counts
import test_fact_resolution
import test_observation_provenance

actual_couch_source=test_observation_provenance.actual_couch_source
facts=test_fact_resolution.facts
coordinator=test_fact_resolution.coordinator


pytestmark = pytest.mark.skipif(not (os.environ.get("LBRAIN_TEST_LIVE_CORRECTION") == "1"
    and os.environ.get("OPENROUTER_API_KEY") and os.environ.get("LBRAIN_TEST_SEMANTIC_PROFILE")
    and os.environ.get("LBRAIN_TEST_SEMANTIC_PROFILE_HASH")), reason="실제 모델 교정 opt-in과 검증된 profile 필요")


def test_live_model_correction_late_intake_and_commit_response_recovery(coordinator):
    c = coordinator
    text = "이 파일의 현재 기능 상태 기록은 ready다. 실제 운영 배포 여부를 주장하는 문서는 아니다."
    (c.root / "facts.json").write_text(text)
    git(c.root, "add", "facts.json"); git(c.root, "commit", "-m", "실제 의미 교정용 합성 원천")
    rule = GitSemanticFactPolicy("status", c.rule.scope, c.rule.target_title, c.rule.field_path,
        c.rule.render_template, "facts.json", "등록 branch의 이 파일에 적힌 현재 기능 상태 기록",
        (("ready", "이 파일의 현재 기능 상태 기록은 ready다."), ("failed", "이 파일의 현재 기능 상태 기록은 failed다.")),
        "repository_content", os.environ["LBRAIN_TEST_SEMANTIC_PROFILE"], os.environ["LBRAIN_TEST_SEMANTIC_PROFILE_HASH"])
    # client 대역을 전달하지 않는다. worker가 실제 OpenRouter client를 사용한다.
    service = FactCorrectionCoordinator(store=c.f.store, git_reader=c.service.git_reader, policies=[rule])
    request = copy.deepcopy(c.request)
    request["changes"][0]["evidence"] = {"revision": git(c.root, "rev-parse", "HEAD"), "content_hash": sha256_text(text)}
    task_id = service.submit(request)["changes"][0]["task_id"]
    worker = service.worker(project=c.f.scope.project, worker_id="live-semantic", retry_seconds=.1)
    apply = worker.apply
    def lose_response(*arguments):
        outcome = apply(*arguments)
        assert outcome.state == "resolved"
        raise RuntimeError("synthetic_response_loss_after_commit")
    worker.apply = lose_response
    first = worker.run_once(task_id=task_id)
    assert first["state"] == "waiting", first
    receipt = service.result(task_id)["resolution"]
    assert receipt["applied_value"] == "ready" and receipt["requested_value"] == "caller-wrong"
    assert receipt["admission"]["profile_hash"] == rule.validation_profile_hash
    assert counts(c.f)["steward_card_decisions"] == 2
    offline = c.root.with_name("source-offline-after-commit")
    c.root.rename(offline)
    try:
        worker.apply = apply
        time.sleep(.12)
        assert worker.run_once(task_id=task_id)["state"] == "resolved"
        assert counts(c.f)["steward_card_decisions"] == 2
    finally:
        offline.rename(c.root)
    late = copy.deepcopy(request)
    late["idempotency_key"] = "live-late-intake"
    change = late["changes"][0]
    change.pop("target_memory_id"); change.pop("expected_content_hash")
    change.update(target_description=rule.target_title, old_value="ready", proposed_value="outdated-proposal")
    late_id = service.submit(late, lane="intake")["changes"][0]["task_id"]
    late_run = worker.run_once(task_id=late_id)
    assert late_run["state"] == "resolved", late_run
    assert service.result(late_id)["resolution"]["action"] == "attach_evidence"
    assert counts(c.f)["steward_card_decisions"] == 2
    assert counts(c.f)["lbrain_fact_assertions"] == 1
    next_text = "이 파일의 현재 기능 상태 기록은 failed다. 이전 ready는 현재 기록이 아니다."
    # 독립 source가 후속 변경되면 같은 값 유지로 고정하지 않는다.
    (c.root / "facts.json").write_text(next_text)
    git(c.root, "add", "facts.json"); git(c.root, "commit", "-m", "합성 후속 원천 상태")
    following = copy.deepcopy(late)
    following["idempotency_key"] = "live-next-state"
    following["changes"][0]["evidence"] = {"revision": git(c.root, "rev-parse", "HEAD"), "content_hash": sha256_text(next_text)}
    next_id = service.submit(following, lane="intake")["changes"][0]["task_id"]
    next_run = worker.run_once(task_id=next_id)
    assert next_run["state"] == "resolved", next_run
    assert service.result(next_id)["resolution"]["applied_value"] == "failed"
    assert counts(c.f)["steward_card_decisions"] == 3
    old = service.result(task_id)["resolution"]
    assert old["decision_time_current"]["value"] == "ready" and old["current"]["value"] == "failed"
    page = GraphFirstResolver(store=c.f.store).resolve(project=c.f.scope.project, mode="context")
    assert len(page["statuses"]) == 1 and "기능 상태: failed" in page["statuses"][0]["status"]
    assert "ready" not in page["statuses"][0]["status"]
    listing = GraphFirstResolver(store=c.f.store).resolve(project=c.f.scope.project, mode="list")
    assert len(listing["items"]) == 1 and listing["items"][0]["typed_payload"]["status_value"] == "failed"


def test_live_semantic_configuration_through_real_mcp_http_cli(coordinator, tmp_path):
    from dataclasses import asdict
    from test_fact_resolution import test_real_cli_http_configuration_source_read_and_current_query
    c = coordinator
    text = "이 파일에서 현재 기능 상태는 ready로 명시되어 있다."
    (c.root / "facts.json").write_text(text)
    git(c.root, "add", "facts.json"); git(c.root, "commit", "-m", "실제 HTTP 의미 교정 원천")
    c.rule = GitSemanticFactPolicy("status", c.rule.scope, c.rule.target_title, c.rule.field_path,
        c.rule.render_template, "facts.json", "등록 branch의 이 파일에 명시된 현재 기능 상태",
        (("ready", "이 파일의 현재 기능 상태는 ready다."), ("failed", "이 파일의 현재 기능 상태는 failed다.")),
        "repository_content", os.environ["LBRAIN_TEST_SEMANTIC_PROFILE"], os.environ["LBRAIN_TEST_SEMANTIC_PROFILE_HASH"])
    c.request["changes"][0]["evidence"] = {"revision": git(c.root, "rev-parse", "HEAD"), "content_hash": sha256_text(text)}
    test_real_cli_http_configuration_source_read_and_current_query(c, tmp_path,
        policy_descriptor={**asdict(c.rule), "adapter": "git_semantic.v1"},
        additional_env={"OPENROUTER_API_KEY": os.environ["OPENROUTER_API_KEY"]})


def test_live_source_revocation_worker_restart_and_model_repair(coordinator, tmp_path):
    from dataclasses import asdict
    from test_fact_source_lifecycle import test_real_lifecycle_process_restart_and_independent_monitor_recover_same_review
    c = coordinator
    text = "이 파일은 기능 상태를 ready로 기록한다."
    (c.root / "facts.json").write_text(text)
    git(c.root, "add", "facts.json"); git(c.root, "commit", "-m", "철회 실증의 의미 근거")
    c.rule = GitSemanticFactPolicy("status", c.rule.scope, c.rule.target_title, c.rule.field_path,
        c.rule.render_template, "facts.json", "등록 branch 파일의 기능 상태 기록",
        (("ready", "이 파일의 기능 상태 기록은 ready다."), ("failed", "이 파일의 기능 상태 기록은 failed다.")),
        "repository_content", os.environ["LBRAIN_TEST_SEMANTIC_PROFILE"], os.environ["LBRAIN_TEST_SEMANTIC_PROFILE_HASH"])
    c.service = FactCorrectionCoordinator(store=c.f.store, git_reader=c.service.git_reader, policies=[c.rule])
    c.request["changes"][0]["evidence"] = {"revision": git(c.root, "rev-parse", "HEAD"), "content_hash": sha256_text(text)}
    test_real_lifecycle_process_restart_and_independent_monitor_recover_same_review(c, tmp_path,
        policy_descriptor={**asdict(c.rule), "adapter": "git_semantic.v1"},
        additional_env={"OPENROUTER_API_KEY": os.environ["OPENROUTER_API_KEY"]}, lease_seconds=10)


def test_live_model_expiry_recheck_and_stopped_worker_recovery(coordinator, tmp_path):
    from dataclasses import asdict
    from test_fact_verification import test_expiry_worker_stop_monitor_and_automatic_restart_reverify_and_update
    c = coordinator
    text = "이 파일은 기능 상태를 ready로 기록한다."
    (c.root / "facts.json").write_text(text)
    git(c.root, "add", "facts.json"); git(c.root, "commit", "-m", "기한 검증의 의미 근거")
    c.rule = GitSemanticFactPolicy("status", c.rule.scope, c.rule.target_title, c.rule.field_path,
        c.rule.render_template, "facts.json", "등록 branch 파일의 기능 상태 기록",
        (("ready", "이 파일의 기능 상태 기록은 ready다."), ("failed", "이 파일의 기능 상태 기록은 failed다.")),
        "repository_content", os.environ["LBRAIN_TEST_SEMANTIC_PROFILE"], os.environ["LBRAIN_TEST_SEMANTIC_PROFILE_HASH"],
        max_age_seconds=1)
    c.service = FactCorrectionCoordinator(store=c.f.store, git_reader=c.service.git_reader, policies=[c.rule])
    c.request["changes"][0]["evidence"] = {"revision": git(c.root, "rev-parse", "HEAD"), "content_hash": sha256_text(text)}
    test_expiry_worker_stop_monitor_and_automatic_restart_reverify_and_update(c, tmp_path,
        policy_descriptor={**asdict(c.rule), "adapter": "git_semantic.v1"},
        additional_env={"OPENROUTER_API_KEY": os.environ["OPENROUTER_API_KEY"]}, lease_seconds=10)


def test_live_model_competing_sources_retain_conflict_then_resolve_actual_agreement(coordinator, tmp_path):
    from test_fact_source_competition import test_real_worker_keeps_unchanged_conflict_then_automatically_resolves_new_agreement
    c = coordinator
    c.rule = GitSemanticFactPolicy("status", c.rule.scope, c.rule.target_title, c.rule.field_path,
        c.rule.render_template, "facts.json", "등록 branch의 이 파일에 적힌 현재 기능 상태 기록",
        (("ready", "이 파일의 현재 기능 상태 기록은 ready다."), ("failed", "이 파일의 현재 기능 상태 기록은 failed다.")),
        "repository_content", os.environ["LBRAIN_TEST_SEMANTIC_PROFILE"], os.environ["LBRAIN_TEST_SEMANTIC_PROFILE_HASH"])
    test_real_worker_keeps_unchanged_conflict_then_automatically_resolves_new_agreement(c, tmp_path,
        additional_env={"OPENROUTER_API_KEY": os.environ["OPENROUTER_API_KEY"]})


def test_live_model_concurrent_opposing_sources_on_one_card(coordinator):
    from test_fact_concurrency import test_concurrent_opposing_current_files_do_not_choose_by_lock_order
    c = coordinator
    c.rule = GitSemanticFactPolicy("status", c.rule.scope, c.rule.target_title, c.rule.field_path,
        c.rule.render_template, "facts.json", "등록 branch의 이 파일에 적힌 현재 기능 상태 기록",
        (("ready", "이 파일의 현재 기능 상태 기록은 ready다."), ("failed", "이 파일의 현재 기능 상태 기록은 failed다.")),
        "repository_content", os.environ["LBRAIN_TEST_SEMANTIC_PROFILE"], os.environ["LBRAIN_TEST_SEMANTIC_PROFILE_HASH"])
    test_concurrent_opposing_current_files_do_not_choose_by_lock_order(c)


def test_live_source_observe_http_correction_then_native_cli_restart_join(coordinator,actual_couch_source,tmp_path):
    from dataclasses import asdict
    from test_source_observation import test_actual_http_observe_correct_then_cli_worker_discovers_late_session
    c=coordinator
    text='이 등록 파일의 현재 기능 상태 기록은 ready다. 운영 배포 상태에 관한 주장은 포함하지 않는다.'
    (c.root/'facts.json').write_text(text)
    git(c.root,'add','facts.json');git(c.root,'commit','-m','실제 관측 참조와 지연 세션 합류용 원천')
    c.rule=GitSemanticFactPolicy('status',c.rule.scope,c.rule.target_title,c.rule.field_path,c.rule.render_template,
        'facts.json','등록 branch 파일에 명시된 현재 기능 상태 기록',
        (('ready','이 파일의 현재 기능 상태 기록은 ready다.'),('failed','이 파일의 현재 기능 상태 기록은 failed다.')),
        'repository_content',os.environ['LBRAIN_TEST_SEMANTIC_PROFILE'],os.environ['LBRAIN_TEST_SEMANTIC_PROFILE_HASH'])
    c.service=FactCorrectionCoordinator(store=c.f.store,git_reader=c.service.git_reader,policies=[c.rule])
    test_actual_http_observe_correct_then_cli_worker_discovers_late_session(c,actual_couch_source,tmp_path,
        policy_descriptor={**asdict(c.rule),'adapter':'git_semantic.v1'},
        additional_env={'OPENROUTER_API_KEY':os.environ['OPENROUTER_API_KEY']})


def target_profile():
    if not (os.environ.get("LBRAIN_TEST_TARGET_PROFILE") and os.environ.get("LBRAIN_TEST_TARGET_PROFILE_HASH")):
        pytest.skip("실제 대상 식별 독립 평가 profile 필요")
    return {
        "validation_profile_path": os.environ["LBRAIN_TEST_TARGET_PROFILE"],
        "validation_profile_hash": os.environ["LBRAIN_TEST_TARGET_PROFILE_HASH"],
    }


@pytest.mark.parametrize("facts", [{"title": "기억 교정 도구의 구현 상태",
    "render_template": "기억 교정 도구 구현 상태: {value}"}], indirect=True)
def test_live_target_and_source_judgments_through_real_http_cli(coordinator, tmp_path):
    """실제 두 모델 판단과 source-observe/MCP/CLI/PG를 함께 통과한다."""
    from dataclasses import asdict
    c = coordinator
    descriptor = target_profile()
    text = "이 파일에서 기억 교정 도구의 현재 구현 상태는 ready다. 운영 배포 여부는 다루지 않는다."
    (c.root / "facts.json").write_text(text)
    git(c.root, "add", "facts.json"); git(c.root, "commit", "-m", "대상 식별과 값 교정의 실제 원천")
    c.rule = GitSemanticFactPolicy("status", c.rule.scope, c.rule.target_title, c.rule.field_path,
        "기억 교정 도구 구현 상태: {value}", "facts.json", "등록 파일에 명시된 기억 교정 도구의 구현 상태",
        (("ready", "이 파일의 기억 교정 도구 구현 상태는 ready다."),
         ("failed", "이 파일의 기억 교정 도구 구현 상태는 failed다.")),
        "repository_content", os.environ["LBRAIN_TEST_SEMANTIC_PROFILE"], os.environ["LBRAIN_TEST_SEMANTIC_PROFILE_HASH"])
    change = c.request["changes"][0]
    change.pop("target_memory_id"); change.pop("expected_content_hash")
    change["target_description"] = "기억 내용을 고치는 기능이 구현되어 있는지"
    change["evidence"] = {"revision": git(c.root, "rev-parse", "HEAD"), "content_hash": sha256_text(text)}
    test_fact_resolution.test_real_cli_http_configuration_source_read_and_current_query(c, tmp_path,
        policy_descriptor={**asdict(c.rule), "adapter": "git_semantic.v1"},
        target_search_descriptor=descriptor,
        additional_env={"OPENROUTER_API_KEY": os.environ["OPENROUTER_API_KEY"]})
    with c.f.store._scope() as db:
        row = db.execute("SELECT snapshot,result,resolution_id FROM lbrain_fact_target_decisions").fetchone()
    assert row["resolution_id"] and row["result"]["memory_id"] == c.f.old["memory_id"]
    assert row["snapshot"]["description"] == change["target_description"]


def test_live_full_source_over_eight_kilobytes_keeps_final_value(coordinator):
    """기존 8 KiB 경계 뒤의 값도 source를 자르지 않고 실제 모델로 확인한다."""
    c = coordinator
    text = ("# 공개 검증용 주석이다. 이 행은 기능 상태를 주장하지 않는다.\n" * 180
        + "FEATURE_STATUS = 'failed'\n")
    assert 8192 < len(text.encode()) <= 32768
    (c.root / "facts.json").write_text(text)
    git(c.root, "add", "facts.json"); git(c.root, "commit", "-m", "전체 파일 끝부분 값 검증")
    rule = GitSemanticFactPolicy("status", c.rule.scope, c.rule.target_title, c.rule.field_path,
        c.rule.render_template, "facts.json", "등록 파일의 FEATURE_STATUS 상수 값",
        (("ready", "이 파일의 FEATURE_STATUS 상수 값은 ready다."),
         ("failed", "이 파일의 FEATURE_STATUS 상수 값은 failed다.")),
        "repository_content", os.environ["LBRAIN_TEST_SEMANTIC_PROFILE"], os.environ["LBRAIN_TEST_SEMANTIC_PROFILE_HASH"])
    service = FactCorrectionCoordinator(store=c.f.store, git_reader=c.service.git_reader, policies=[rule])
    change = c.request["changes"][0]
    change["evidence"] = {"revision": git(c.root, "rev-parse", "HEAD"), "content_hash": sha256_text(text)}
    task_id = service.submit(c.request)["changes"][0]["task_id"]
    run = service.worker(project=c.f.scope.project, worker_id="live-full-file").run_once(task_id=task_id)
    assert run["state"] == "resolved", run
    receipt = service.result(task_id)["resolution"]
    assert receipt["applied_value"] == "failed" and counts(c.f)["steward_card_decisions"] == 2
    with c.f.store._scope() as db:
        contents = [row["redacted_content"] for row in db.execute("SELECT redacted_content FROM lbrain_fact_source_receipts")]
    assert text in contents


@pytest.mark.parametrize("facts", [{"title": "기억 교정 도구의 구현 상태",
    "render_template": "기억 교정 도구 구현 상태: {value}"}], indirect=True)
def test_live_target_uncertainty_keeps_recheck_and_never_changes_the_anchor(coordinator):
    """확실히 구분하지 못한 실제 모델 응답을 대상 부재로 바꾸지 않는다."""
    from agent_knowledge.correction_target import TargetSearchPolicy
    c = coordinator
    service = FactCorrectionCoordinator(store=c.f.store, git_reader=c.service.git_reader,
        policies=[c.rule], target_search=TargetSearchPolicy(**target_profile()))
    change = c.request["changes"][0]
    change.pop("target_memory_id"); change.pop("expected_content_hash")
    change["target_description"] = "기억 교정 도구 프로세스의 현재 가동 여부"
    task_id = service.submit(c.request)["changes"][0]["task_id"]
    worker = service.worker(project=c.f.scope.project, worker_id="live-target-uncertain", retry_seconds=.1)
    outcome = worker.run_once(task_id=task_id)
    assert outcome["state"] == "waiting" and outcome["reason_code"] == "target_identity_uncertain", outcome
    assert service.result(task_id)["automatic_recheck_scheduled"]
    time.sleep(.12)
    repeated = worker.run_once(task_id=task_id)
    assert repeated["state"] == "waiting" and repeated["judgment_reused"], repeated
    assert counts(c.f)["steward_card_decisions"] == 1


@pytest.mark.parametrize("facts", [{"title": "기억 교정 도구의 구현 상태",
    "render_template": "기억 교정 도구 구현 상태: {value}"}], indirect=True)
def test_live_unrelated_target_absence_never_creates_a_fact(coordinator):
    from agent_knowledge.correction_target import TargetSearchPolicy
    c = coordinator
    service = FactCorrectionCoordinator(store=c.f.store, git_reader=c.service.git_reader,
        policies=[c.rule], target_search=TargetSearchPolicy(**target_profile()))
    change = c.request["changes"][0]
    change.pop("target_memory_id"); change.pop("expected_content_hash")
    change["target_description"] = "사용자 화면의 글꼴 크기 설정"
    task_id = service.submit(c.request)["changes"][0]["task_id"]
    outcome = service.worker(project=c.f.scope.project, worker_id="live-target-absence").run_once(task_id=task_id)
    assert outcome["state"] == "resolved", outcome
    receipt = service.result(task_id)["target_resolution"]
    assert receipt["reason_code"] == "target_not_found" and receipt["search_complete"]
    assert not receipt["authority_write_performed"] and counts(c.f)["steward_card_decisions"] == 1


def test_live_policy_reinterprets_one_immutable_source_and_late_native_stays_history(
        coordinator,actual_couch_source,tmp_path):
    """실제 모델로 같은 Git 관측을 새 정책으로 재해석하고 옛 native는 별도로 보존한다."""
    from dataclasses import replace
    from agent_knowledge.fact_policy_lifecycle import FactPolicyLifecycle
    from agent_knowledge.source_observation import SourceObservationService
    from test_source_observation import capture,native_tasks
    c = coordinator
    text = "FEATURE_STATE = 'ready'\nOBSERVED_STATUS = 'failed'\n"
    (c.root/"facts.json").write_text(text)
    git(c.root,"add","facts.json");git(c.root,"commit","-m","같은 관측의 정책 해석 변경 검증")
    old = GitSemanticFactPolicy("status",c.rule.scope,c.rule.target_title,c.rule.field_path,c.rule.render_template,
        "facts.json","이 파일의 FEATURE_STATE 상수 값",
        (("ready","이 파일의 FEATURE_STATE 값은 ready다."),("failed","이 파일의 FEATURE_STATE 값은 failed다.")),
        "repository_content",os.environ["LBRAIN_TEST_SEMANTIC_PROFILE"],os.environ["LBRAIN_TEST_SEMANTIC_PROFILE_HASH"])
    c.rule = old
    c.service = FactCorrectionCoordinator(store=c.f.store,git_reader=c.service.git_reader,policies=[old])
    read = SourceObservationService(c.service).observe(project=old.scope.project,repository=old.scope.repository,
        policy_id=old.policy_id,idempotency_key="before-policy-publication")
    request = copy.deepcopy(c.request)
    request["changes"][0]["evidence"] = read["evidence"]
    original = c.service.submit(request)["changes"][0]["task_id"]
    old_run = c.service.worker(project=c.f.scope.project,worker_id="live-old-policy").run_once(task_id=original)
    assert old_run["state"]=="resolved",old_run
    original_resolution = c.service.result(original)["resolution"]["resolution_id"]
    new = replace(old,scope_description="이 파일의 OBSERVED_STATUS 상수 값",
        claims=(("ready","이 파일의 OBSERVED_STATUS 값은 ready다."),("failed","이 파일의 OBSERVED_STATUS 값은 failed다.")),
        publication_revision=1)
    current = FactCorrectionCoordinator(store=c.f.store,git_reader=c.service.git_reader,policies=[new])
    lifecycle = FactPolicyLifecycle(current)
    published = lifecycle.publish(project=c.f.scope.project,policy_id=new.policy_id,expected_policy_version=old.version,
        request_key="live-policy-publication",actor="live-fixture")
    assert not published["canonical_write_performed"]
    lifecycle.dispatch_once(project=c.f.scope.project)
    with c.f.store._scope() as db:
        child = db.execute("SELECT task_id FROM lbrain_fact_policy_reviews").fetchone()["task_id"]
    new_run = current.worker(project=c.f.scope.project,worker_id="live-new-policy").run_once(task_id=child)
    assert new_run["state"]=="resolved",new_run
    assert current.result(child)["resolution"]["applied_value"]=="failed"
    assert lifecycle.dispatch_once(project=c.f.scope.project)["verified"]==1
    bridge = capture(c,actual_couch_source,tmp_path,read)
    assert bridge.dispatch_once(project=c.f.scope.project,batch_size=100)["queued"]==1
    [native] = native_tasks(c)
    late = c.service.worker(project=c.f.scope.project,worker_id="live-late-old-policy").run_once(task_id=native["task_id"])
    assert late["state"]=="resolved",late
    result = c.service.result(native["task_id"])
    assert result["policy_review"]["native_source_read"]["historical_resolution_id"]==original_resolution
    assert not result["policy_review"]["original_change_applied"]
    assert result["review_resolution"]["current"]["value"]=="failed"
    assert counts(c.f)["steward_card_decisions"]==3 and counts(c.f)["lbrain_fact_assertions"]==2
    with c.f.store._scope() as db:
        row = db.execute("SELECT count(*) AS interpretations,count(DISTINCT observation_id) AS observations FROM lbrain_fact_observations").fetchone()
    assert row["interpretations"]==2 and row["observations"]==1
