"""명시적 opt-in에서 실제 OpenRouter/Git/PG의 교정·수집·복구를 검사한다."""
import copy
import os
import time

import pytest

from agent_knowledge.fact_correction_coordinator import FactCorrectionCoordinator, GitSemanticFactPolicy
from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
from agent_knowledge.public_safe_util import sha256_text
from test_correction_sources import git
from test_fact_resolution import facts, coordinator, counts


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
