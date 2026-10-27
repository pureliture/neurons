"""실제 PostgreSQL에서 canonical commit·관측·resolution의 원자성을 검증한다.

코어 검사에는 명시적 admission 대역을 쓴다. coordinator 검사는 실제 Git adapter와
PostgreSQL을 사용한다. 전체 수집·graph·실제 모델의 runtime 실증으로 확대 해석하지 않는다.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
import time

import pytest

from agent_knowledge.fact_resolution import (
    FactResolutionError, FactResolutionService, FactScope, StewardFactCommit, VerifiedObservation,
)
from agent_knowledge.ledger import Ledger
from agent_knowledge.public_safe_util import hash_payload, sha256_text, utc_now_iso
from agent_knowledge.session_memory.brain_steward import BrainStewardService


@pytest.fixture
def facts(tmp_path, isolated_pg_store, request):
    store = isolated_pg_store
    ledger = Ledger(tmp_path / "ledger.sqlite")
    steward = BrainStewardService(ledger, pgvector_store=store, allow_restricted=True)
    parameter = getattr(request, "param", None)
    metadata = parameter if isinstance(parameter, dict) else {}
    project = parameter if isinstance(parameter, str) else metadata.get("project", "fact-test")
    render_template = metadata.get("render_template", "기능 상태: {value}")
    scope = FactScope(project, "neurons", "main", "test", "correction", "status")
    span = {"card_type": "status", "project": scope.project, "provider": "codex", "scope": "project",
        "title": metadata.get("title", "교정 상태"),
        "redacted_summary": render_template.format(value="missing") + "; 배포는 별도 확인이 필요하다.",
        "typed_payload": {"status_value": "missing", "observed_at": utc_now_iso(),
            "expires_at": "", "current_authority": "structured-facts"},
        "source_ref": {"source_id": "fixture-old"}, "span_ref": {"span_id": "fixture-span"},
        "content_hash": sha256_text("seed"), "confidence": .95, "confidence_basis": "명시적 사실"}
    memory_id = steward.candidate_create(source_span=span)["memory_id"]
    steward.candidate_approve(candidate_memory_id=memory_id, approved_by="fixture", decision_id="seed")
    f = SimpleNamespace(store=store, ledger=ledger, scope=scope, old=store.get_steward_card(memory_id),
        resolver=FactResolutionService(store), current={}, calls=0, span=span)
    f.commit = StewardFactCommit(field_path="typed_payload.status_value",
        render_template=render_template, policy_id="fixture-status")
    yield f


def observation(f, revision, *, source="git", event=None, current=True, interpreter="v1"):
    from psycopg.types.json import Jsonb
    body = f"source-{source}-{revision}"
    source_ref = "src_" + sha256_text(source + str(revision))[7:31]
    # admission callback은 대역이지만 근거 참조/내용 보존은 실제 PG 계약을 따른다.
    with f.store.transaction() as db:
        db.execute("""INSERT INTO lbrain_fact_source_receipts(source_ref_id,content_hash,redacted_content,proof)
            VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""", (source_ref, sha256_text(body), body, Jsonb({"fixture": True})))
    return VerifiedObservation(observation_id=sha256_text(event or f"{source}-{revision}"),
        project=f.scope.project, source_id=sha256_text(source), source_revision=str(revision),
        content_hash=sha256_text(f"source-{source}-{revision}"), context_hash=f.scope.context_hash,
        interpretation_version=interpreter, current_verified=current,
        observed_at="2026-10-25T01:00:00+00:00",
        proof={"source_ref_id": source_ref})


def invoke(f, obs, value, *, key="request", target=None, request_hash=None, commit=None, scope=None):
    target = target or f.resolver.project_cards([f.store.get_steward_card(f.old["memory_id"])])[0]
    def compare(old, incoming):
        return "same" if old.source_revision == incoming.source_revision else (
            "incoming_after" if int(incoming.source_revision) > int(old.source_revision) else "incoming_before")
    return f.resolver.reconcile(scope=scope or f.scope, memory_id=target["memory_id"],
        field_path="typed_payload.status_value", render_template="기능 상태: {value}",
        expected_content_hash=target["content_hash"], value=value, observation=obs,
        request_key=key, request_hash=request_hash or hash_payload([key, value]), requested_value="caller-proposal",
        is_current=lambda item: f.current.get(item.source_id) == (item.source_revision, item.interpretation_version),
        compare=compare, commit=commit or f.commit)


def set_current(f, obs):
    f.current[obs.source_id] = (obs.source_revision, obs.interpretation_version)


def head_card(f, result):
    card = f.store.get_steward_card(result["current"]["memory_id"])
    return f.resolver.project_cards([card])[0]


def counts(f):
    with f.store._scope() as db:
        return {name: db.execute(f"SELECT count(*) AS n FROM {name}").fetchone()["n"] for name in (
            "lbrain_fact_observations", "lbrain_fact_assertions", "lbrain_fact_resolutions",
            "lbrain_fact_resolution_events", "steward_card_decisions")}


def test_one_commit_retries_independent_keys_and_late_same_value_attach(facts):
    f = facts
    obs = observation(f, 2)
    set_current(f, obs)
    result = invoke(f, obs, "ready")
    assert result["action"] == "replace_current" and result["authority_write_performed"]
    assert result["requested_value"] == "caller-proposal" and result["applied_value"] == "ready"
    retry = invoke(f, obs, "ready")
    assert retry["duplicate"] and not retry["authority_write_performed"]
    # 같은 원래 target을 본 다른 동시 요청도 새 canonical card를 만들지 않는다.
    second = invoke(f, obs, "ready", key="another", target=f.old)
    assert second["action"] == "attach_evidence" and not second["authority_write_performed"]
    assert counts(f)["lbrain_fact_assertions"] == 1
    assert counts(f)["steward_card_decisions"] == 2  # seed 승인 + 교정 한 번
    card = head_card(f, result)
    assert card["summary"] == "기능 상태: ready; 배포는 별도 확인이 필요하다."
    assert card["canonical_content_hash"] != card["content_hash"]
    assert f.store.get_steward_card(f.old["memory_id"])["typed_payload"]["status_value"] == "missing"


def test_delayed_ancestor_and_actual_rollback_are_distinct(facts):
    f = facts
    now = observation(f, 2)
    set_current(f, now)
    first = invoke(f, now, "ready")
    delayed = observation(f, 1, current=False)
    late = invoke(f, delayed, "missing", key="late", target=head_card(f, first))
    assert late["action"] == "retain_history" and late["current"]["value"] == "ready"
    # 같은 commit hash로 실제 되돌아갔어도 새로운 관측이면 현재 확인 뒤 교정한다.
    rollback = observation(f, 1, event="new-rollback-observation")
    set_current(f, rollback)
    back = invoke(f, rollback, "missing", key="rollback", target=head_card(f, first))
    assert back["action"] == "replace_current" and back["generation"] == 2
    # 과거 성공 요청 replay가 이후의 현재 값을 덮어쓰지 않는다.
    replay = invoke(f, now, "ready")
    assert replay["action"] == "replace_current" and replay["applied_value"] == "ready"
    assert replay["current"]["value"] == "missing" and not replay["authority_write_performed"]


def test_independent_current_conflict_cannot_be_cleared_by_repeating_old_value(facts):
    f = facts
    a, b = observation(f, 1, source="repo"), observation(f, 7, source="runtime")
    set_current(f, a); set_current(f, b)
    first = invoke(f, a, "ready")
    conflict = invoke(f, b, "failed", key="runtime", target=head_card(f, first))
    assert conflict["action"] == "disputed" and conflict["current"]["value"] == "ready"
    repeated = invoke(f, a, "ready", key="repeat", target=head_card(f, first))
    assert repeated["action"] == "disputed" and repeated["current"]["state"] == "disputed"
    from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
    context = GraphFirstResolver(store=f.store).resolve(project=f.scope.project, mode="context")
    assert context["statuses"][0]["disputed_fields"] == ["typed_payload.status_value"]
    assert counts(f)["steward_card_decisions"] == 2
    # 원천 A가 유효성을 잃으면 같은 보류 요청을 자동 재검토할 수 있다.
    f.current.pop(a.source_id)
    resolved = invoke(f, b, "failed", key="runtime", target=head_card(f, first))
    assert resolved["action"] == "replace_current" and resolved["current"]["state"] == "current"


def test_same_value_observations_kept_without_extra_authority(facts):
    f = facts
    a, b = observation(f, 2, source="repo"), observation(f, 3, source="runtime")
    set_current(f, a); set_current(f, b)
    first = invoke(f, a, "ready")
    attached = invoke(f, b, "ready", key="independent", target=head_card(f, first))
    assert attached["action"] == "attach_evidence"
    assert counts(f)["lbrain_fact_observations"] == 2
    assert counts(f)["lbrain_fact_assertions"] == 2
    assert counts(f)["steward_card_decisions"] == 2


def test_interpretation_change_needs_current_version_admission(facts):
    f = facts
    a = observation(f, 2)
    set_current(f, a)
    first = invoke(f, a, "ready")
    b = replace(a, interpretation_version="v2")
    # callback이 v2를 현행 해석으로 인정하지 않으면 새 값으로 바꾸지 않는다.
    held = invoke(f, b, "failed", key="reinterpret", target=head_card(f, first))
    assert held["action"] == "pending_evidence"
    set_current(f, b)
    changed = invoke(f, b, "failed", key="reinterpret", target=head_card(f, first))
    assert changed["action"] == "replace_current"
    assert counts(f)["lbrain_fact_observations"] == 2


@pytest.mark.parametrize("failure", ["after_authority", "source_changed"])
def test_transaction_rolls_back_authority_and_resolution_together(facts, failure):
    f = facts
    obs = observation(f, 2)
    set_current(f, obs)
    before = counts(f)
    def interrupted(**kwargs):
        result = f.commit(**kwargs)
        if failure == "after_authority":
            raise RuntimeError("injected crash before resolution receipt")
        f.current.clear()
        return result
    with pytest.raises((RuntimeError, FactResolutionError)):
        invoke(f, obs, "ready", commit=interrupted)
    assert counts(f) == before
    assert f.store.get_steward_card(f.old["memory_id"])["currentness"] == "current"


def test_scope_request_collision_and_immutable_witness_fail_closed(facts):
    f = facts
    obs = observation(f, 2)
    set_current(f, obs)
    with pytest.raises(FactResolutionError, match="observation_scope_mismatch"):
        invoke(f, obs, "ready", scope=replace(f.scope, environment="production"))
    result = invoke(f, obs, "ready")
    with pytest.raises(FactResolutionError, match="request_key_conflict"):
        invoke(f, obs, "ready", request_hash=sha256_text("different-payload"))
    with pytest.raises(FactResolutionError, match="observation_version_conflict"):
        invoke(f, replace(obs, observed_at="2026-10-25T02:00:00+00:00"), "ready",
            key="different-receipt", target=head_card(f, result))


def test_parallel_request_keys_produce_one_successor(facts):
    f = facts
    obs = observation(f, 2)
    set_current(f, obs)
    def bounded_client_retry(key):
        for attempt in range(8):
            try:
                return invoke(f, obs, "ready", key=key, target=f.old)
            except FactResolutionError as exc:
                if not exc.retryable or attempt == 7:
                    raise
                time.sleep(.05 * (attempt + 1))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(bounded_client_retry, ["one", "two", "three", "four"]))
    assert sum(item["authority_write_performed"] for item in results) == 1
    assert counts(f)["lbrain_fact_assertions"] == 1
    assert counts(f)["steward_card_decisions"] == 2


def test_public_list_context_consistency_token_and_old_card_view(facts):
    from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
    f = facts
    obs = observation(f, 2)
    set_current(f, obs)
    result = invoke(f, obs, "ready")
    reader = GraphFirstResolver(store=f.store)
    for mode in ("list", "context"):
        page = reader.resolve(project=f.scope.project, mode=mode, limit=20)
        assert page["metadata"]["fact_resolution"] == "postgres_current"
        assert page["metadata"]["fact_generation"] >= 1
        if mode == "context":
            assert "기능 상태: ready" in page["statuses"][0]["status"]
            assert "missing" not in page["statuses"][0]["status"]
        else:
            assert page["items"][0]["typed_payload"]["status_value"] == "ready"
            assert page["items"][0]["fact_views"][0]["generation"] == 1
            assert page["items"][0]["content_hash"] == head_card(f, result)["content_hash"]
    gap = reader.resolve(project=f.scope.project, mode="list", min_fact_generation=1000)
    assert not gap["items"] and gap["error_code"] == "fact_generation_unavailable"
    # 과거 canonical record를 명시적으로 투영하더라도 같은 현재 fact를 표시한다.
    old = f.store.get_steward_card(f.old["memory_id"])
    view = f.resolver.project_cards([old])[0]
    assert view["typed_payload"]["status_value"] == "ready"
    assert view["freshness_state"] == "unchecked"
    assert old["typed_payload"]["status_value"] == "missing"


def test_read_race_returns_gap_instead_of_old_card(facts, monkeypatch):
    from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
    f = facts
    obs = observation(f, 2)
    set_current(f, obs)
    original = f.store.resolve_fact_views
    def changed_after_search(**kwargs):
        invoke(f, obs, "ready")
        return original(**kwargs)
    monkeypatch.setattr(f.store, "resolve_fact_views", changed_after_search)
    page = GraphFirstResolver(store=f.store).resolve(project=f.scope.project, mode="list")
    assert not page["items"] and page["error_code"] == "fact_resolution_unavailable"


@pytest.fixture
def coordinator(facts, tmp_path):
    from agent_knowledge.correction_sources import GitSourcePolicy, GitSourceReader
    from agent_knowledge.fact_correction_coordinator import FactCorrectionCoordinator, GitJsonFactPolicy
    from test_correction_sources import git
    f = facts
    root = tmp_path / "source-repository"
    root.mkdir()
    git(root, "init", "-b", "fixture")
    (root / "facts.json").write_text('{"status":"missing"}')
    git(root, "add", "facts.json"); git(root, "commit", "-m", "초기 명시적 상태")
    previous = git(root, "rev-parse", "HEAD")
    (root / "facts.json").write_text('{"status":"ready"}')
    git(root, "add", "facts.json"); git(root, "commit", "-m", "후속 명시적 상태")
    current = git(root, "rev-parse", "HEAD")
    scope = replace(f.scope, branch="refs/heads/fixture")
    rule = GitJsonFactPolicy("status", scope, f.old["title"], "typed_payload.status_value",
        f.commit.render_template, "facts.json", "/status")
    service = FactCorrectionCoordinator(store=f.store,
        git_reader=GitSourceReader({scope.repository: GitSourcePolicy(scope.repository, root, branch_ref=scope.branch)}),
        policies=[rule])
    request = {"schema_version": "brain_object_correction.v2", "project": scope.project,
        "repository": scope.repository, "idempotency_key": "batch-1", "changes": [{
            "change_id": "status", "policy_id": "status", "target_memory_id": f.old["memory_id"],
            "expected_content_hash": f.old["content_hash"], "old_value": "missing", "proposed_value": "caller-wrong",
            "discrepancy": "현재 source의 상태와 조회 결과가 다르다.",
            "evidence": {"revision": current, "content_hash": sha256_text('{"status":"ready"}')}}]}
    return SimpleNamespace(f=f, service=service, request=request, root=root, current=current, previous=previous, rule=rule)


def test_real_git_source_correction_then_regular_intake_share_resolution(coordinator):
    import copy
    c = coordinator
    receipt = c.service.submit(c.request)
    task_id = receipt["changes"][0]["task_id"]
    worker = c.service.worker(project=c.f.scope.project, worker_id="fixture-worker", retry_seconds=.1)
    run = worker.run_once(task_id=task_id)
    assert run["state"] == "resolved", run
    result = c.service.result(task_id)["resolution"]
    assert result["applied_value"] == "ready" and result["requested_value"] == "caller-wrong"
    assert counts(c.f)["steward_card_decisions"] == 2
    assert c.service.submit(c.request)["duplicate"]
    # 수집 경로도 같은 원천 관측을 새 MemoryCard 승인 대신 공통 resolution에 전달한다.
    intake = copy.deepcopy(c.request)
    intake["idempotency_key"] = "intake-1"
    change = intake["changes"][0]
    change.pop("target_memory_id"); change.pop("expected_content_hash")
    change.update(target_description=c.rule.target_title, old_value="ready", proposed_value="ready")
    second = c.service.submit(intake, lane="intake")["changes"][0]["task_id"]
    assert worker.run_once(task_id=second)["state"] == "resolved"
    assert c.service.result(second)["resolution"]["action"] == "attach_evidence"
    assert counts(c.f)["lbrain_fact_assertions"] == 1
    assert counts(c.f)["steward_card_decisions"] == 2
    # 늦게 도착한 옛 commit도 수집 순서와 무관하게 history에 연결한다.
    intake["idempotency_key"] = "intake-old"
    change["evidence"] = {"revision": c.previous, "content_hash": sha256_text('{"status":"missing"}')}
    third = c.service.submit(intake, lane="intake")["changes"][0]["task_id"]
    assert worker.run_once(task_id=third)["state"] == "resolved"
    late = c.service.result(third)["resolution"]
    assert late["action"] == "retain_history" and late["current"]["value"] == "ready"


def test_unavailable_source_is_scheduled_and_can_recover_without_new_request(coordinator):
    c = coordinator
    task_id = c.service.submit(c.request)["changes"][0]["task_id"]
    worker = c.service.worker(project=c.f.scope.project, worker_id="fixture-worker", retry_seconds=.1)
    hidden = c.root.with_name("source-offline")
    c.root.rename(hidden)
    try:
        held = worker.run_once(task_id=task_id)
        assert held["state"] == "blocked"
        assert c.service.result(task_id)["automatic_recheck_scheduled"]
        assert counts(c.f)["steward_card_decisions"] == 1
    finally:
        hidden.rename(c.root)
    time.sleep(.12)  # 내구성 queue의 실제 next_check_at 이후에 재검토한다.
    assert worker.run_once(task_id=task_id)["state"] == "resolved"
    assert c.service.result(task_id)["attempt_count"] == 2


def test_commit_receipt_recovers_after_crash_even_when_source_is_gone(coordinator, monkeypatch):
    c = coordinator
    task_id = c.service.submit(c.request)["changes"][0]["task_id"]
    worker = c.service.worker(project=c.f.scope.project, worker_id="fixture-worker", retry_seconds=.1)
    original_finish = c.service.queue.finish
    def crash(*args, **kwargs):
        raise SystemExit(91)
    monkeypatch.setattr(c.service.queue, "finish", crash)
    with pytest.raises(SystemExit):
        worker.run_once(task_id=task_id)
    assert counts(c.f)["steward_card_decisions"] == 2
    assert c.service.queue.get(task_id)["result_ref"]
    monkeypatch.setattr(c.service.queue, "finish", original_finish)
    with c.f.store.transaction() as db:
        db.execute("UPDATE lbrain_recheck_tasks SET lease_until=clock_timestamp()-interval '1 second' WHERE task_id=%s", (task_id,))
    hidden = c.root.with_name("source-offline")
    c.root.rename(hidden)
    try:
        recovered = worker.run_once(task_id=task_id)
        assert recovered["state"] == "resolved" and recovered["reason_code"] == "fact_result_recovered"
    finally:
        hidden.rename(c.root)
    assert counts(c.f)["steward_card_decisions"] == 2


def test_v2_mcp_actual_batch_reports_verified_value_and_preserves_legacy_surface(coordinator):
    from agent_knowledge.knowledge_search_service import KnowledgeSearchService, DisabledRetiredIndexBridgeClient
    from agent_knowledge.mcp_jsonrpc import handle_jsonrpc_message, CORRECTION_AUTH_IDENTITY
    c = coordinator
    service = KnowledgeSearchService(ledger=c.f.ledger, retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[], pgvector_store=c.f.store, allow_evidence_backed_object_corrections=True,
        fact_correction_coordinator=c.service)
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "brain_object_correct", "arguments": c.request}}
    response = handle_jsonrpc_message(message, service, surface="correction", auth_token=CORRECTION_AUTH_IDENTITY)
    assert "error" not in response, response
    result = response["result"]["structuredContent"]
    assert result["authority_write_performed"]
    assert result["changes"][0]["outcome"]["resolution"]["applied_value"] == "ready"
    retry = handle_jsonrpc_message(message, service, surface="correction", auth_token=CORRECTION_AUTH_IDENTITY)
    assert retry["result"]["structuredContent"]["duplicate"]
    assert not retry["result"]["structuredContent"]["authority_write_performed"]
    assert counts(c.f)["steward_card_decisions"] == 2


def test_autopilot_managed_delayed_candidate_uses_shared_resolution_without_new_approval(coordinator):
    from agent_knowledge.session_memory.autopilot_loop import run_autopilot_cycle
    c = coordinator
    task_id = c.service.submit(c.request)["changes"][0]["task_id"]
    worker = c.service.worker(project=c.f.scope.project, worker_id="fixture-worker")
    assert worker.run_once(task_id=task_id)["state"] == "resolved"
    span = {**c.f.span, "source_ref": {"source_id": "late-native-observation"},
        "span_ref": {"span_id": "late-native-span"}, "content_hash": sha256_text("late-native")}
    steward = BrainStewardService(c.f.ledger, pgvector_store=c.f.store)
    late_id = steward.candidate_create(source_span=span)["memory_id"]
    candidate = c.f.store.get_steward_card(late_id)
    cycle = run_autopilot_cycle(candidates=[candidate], ledger=c.f.ledger, pgvector_store=c.f.store,
        refresh_watermark="native-late-fixture", fact_coordinator=c.service)
    assert not cycle["accepted"] and len(cycle["needs_review"]) == 1
    queued = cycle["fact_resolutions"][0]["results"][0]["task_id"]
    assert worker.run_once(task_id=queued)["state"] == "resolved"
    assert c.service.result(queued)["resolution"]["action"] == "attach_evidence"
    assert counts(c.f)["steward_card_decisions"] == 2
    assert c.f.store.get_steward_card(late_id)["authorization_status"] == "disabled"


def test_two_fields_from_same_old_revision_preserve_each_other(facts):
    f = facts
    steward = BrainStewardService(f.ledger, pgvector_store=f.store, allow_restricted=True)
    span = {**f.span, "title": "두 사실 상태", "redacted_summary": "기능 상태: missing; 배포 상태: pending",
        "typed_payload": {**f.span["typed_payload"], "deployment_status": "pending"},
        "source_ref": {"source_id": "two-facts"}, "content_hash": sha256_text("two-facts")}
    target_id = steward.candidate_create(source_span=span)["memory_id"]
    steward.candidate_approve(candidate_memory_id=target_id, approved_by="fixture", decision_id="seed-two")
    f.old = f.store.get_steward_card(target_id)
    a, b = observation(f, 2, source="code"), observation(f, 3, source="deployment")
    set_current(f, a); set_current(f, b)
    first = invoke(f, a, "ready", target=f.old)
    result = f.resolver.reconcile(scope=replace(f.scope, predicate="deployment_status"),
        memory_id=f.old["memory_id"], field_path="typed_payload.deployment_status",
        render_template="배포 상태: {value}", expected_content_hash=f.old["content_hash"],
        value="deployed", observation=b, request_key="deployment", request_hash=sha256_text("deployment"),
        requested_value="deployed", is_current=lambda obs: f.current.get(obs.source_id) == (obs.source_revision, obs.interpretation_version),
        compare=lambda old, new: "unknown", commit=StewardFactCommit(field_path="typed_payload.deployment_status",
            render_template="배포 상태: {value}", policy_id="deployment"))
    card = head_card(f, result)
    assert card["summary"] == "기능 상태: ready; 배포 상태: deployed"
    assert card["typed_payload"]["status_value"] == "ready"
    assert card["typed_payload"]["deployment_status"] == "deployed"
    assert {item["head_memory_id"] for item in card["fact_views"]} == {card["memory_id"]}
    assert first["current"]["memory_id"] != card["memory_id"]


def test_duplicate_card_fact_reads_same_value_and_retains_unrelated_text(facts):
    from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
    f = facts
    obs = observation(f, 2)
    set_current(f, obs)
    first = invoke(f, obs, "ready")
    steward = BrainStewardService(f.ledger, pgvector_store=f.store, allow_restricted=True)
    span = {**f.span, "title": "동일 사실의 다른 기록", "redacted_summary": "기능 상태: missing; 별도 담당자가 있다.",
        "source_ref": {"source_id": "independent-record"}, "content_hash": sha256_text("duplicate-fact")}
    duplicate_id = steward.candidate_create(source_span=span)["memory_id"]
    steward.candidate_approve(candidate_memory_id=duplicate_id, approved_by="fixture", decision_id="seed-duplicate")
    duplicate = f.store.get_steward_card(duplicate_id)
    attached = invoke(f, obs, "ready", key="duplicate-card", target=duplicate)
    assert attached["action"] == "attach_evidence" and not attached["authority_write_performed"]
    page = GraphFirstResolver(store=f.store).resolve(project=f.scope.project, mode="list", limit=20)
    assert len(page["items"]) == 2
    assert {item["typed_payload"]["status_value"] for item in page["items"]} == {"ready"}
    duplicated_view = next(item for item in page["items"] if item["id"] == duplicate_id)
    assert duplicated_view["summary"] == "기능 상태: ready; 별도 담당자가 있다."
    assert f.store.get_steward_card(duplicate_id)["typed_payload"]["status_value"] == "missing"
    assert attached["current"]["memory_id"] == first["current"]["memory_id"]
    reader = GraphFirstResolver(store=f.store)
    page_one = reader.resolve(project=f.scope.project, mode="list", limit=1)
    assert page_one["next_cursor"]
    invoke(f, obs, "ready", key="new-fact-generation", target=head_card(f, first))
    with pytest.raises(ValueError, match="cursor"):
        reader.resolve(project=f.scope.project, mode="list", limit=1, cursor=page_one["next_cursor"])


def test_real_cli_http_configuration_source_read_and_current_query(coordinator, tmp_path,
        policy_descriptor=None, additional_env=None, return_read=False, target_search_descriptor=None):
    """실제 CLI subprocess·HTTP·Git·PG 경로를 통과한다. graph/모델 대역은 넣지 않는다."""
    import asyncio
    import copy
    from dataclasses import asdict
    import json
    import os
    from pathlib import Path
    import secrets
    import signal
    import socket
    import subprocess
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    c = coordinator
    config_file = tmp_path / "fact-policy.json"
    configuration = {"schema_version": "fact_correction_config.v1", "repositories": [{
        "repository": c.rule.scope.repository, "root": str(c.root), "branch_ref": c.rule.scope.branch}],
        "rules": [policy_descriptor if policy_descriptor is not None else asdict(c.rule)]}
    if target_search_descriptor is not None:
        configuration["target_search"] = target_search_descriptor
    config_file.write_text(json.dumps(configuration, ensure_ascii=False))
    config_file.chmod(0o600)
    ledger_path = tmp_path / "http-ledger.sqlite"
    Ledger(ledger_path)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    token = secrets.token_urlsafe(32)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "lib"),
        "NEURON_LBRAIN_PGVECTOR_DSN": c.f.store.dsn, "LLM_BRAIN_CORRECTION_TOKEN": token}
    env.update(additional_env or {})
    argv = ["uv", "run", "--no-project", "neuron-knowledge", "mcp-http", "--surface", "correction",
        "--ledger", str(ledger_path), "--allow-evidence-backed-object-corrections",
        "--fact-correction-policy-file", str(config_file), "--host", "127.0.0.1", "--port", str(port)]
    base = f"http://127.0.0.1:{port}"
    async def roundtrip():
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=90) as client:
            async with streamable_http_client(base + "/mcp", http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert {tool.name for tool in tools.tools} == {"brain.resolve", "brain_object_correct", "brain_source_observe"}
                    observed = await session.call_tool("brain_source_observe", {
                        "project": c.rule.scope.project,"repository": c.rule.scope.repository,
                        "policy_id":c.rule.policy_id,"idempotency_key":"http-source-read"})
                    assert not observed.isError and observed.structuredContent['canonical_write_performed'] is False
                    request=copy.deepcopy(c.request)
                    request['changes'][0]['evidence']=observed.structuredContent['evidence']
                    changed = await session.call_tool("brain_object_correct", request)
                    assert not changed.isError
                    outcome = changed.structuredContent["changes"][0]["outcome"]
                    assert outcome.get("state") == "resolved", {key: outcome.get(key)
                        for key in ("state", "reason_code", "automatic_recheck_scheduled")}
                    receipt = outcome["resolution"]
                    assert receipt["applied_value"] == "ready"
                    current = await session.call_tool("brain.resolve", {"project": c.f.scope.project,
                        "mode": "list", "response_mode": "slim", "min_fact_generation": 1})
                    assert not current.isError
                    assert current.structuredContent["items"][0]["typed_payload"]["status_value"] == "ready"
                    return observed.structuredContent
    with (tmp_path / "http-process.log").open("wb") as log:
        process = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL, stderr=log, start_new_session=True)
        try:
            deadline = time.monotonic() + 20
            while True:
                if process.poll() is not None:
                    pytest.fail("isolated correction HTTP process exited before readiness")
                try:
                    ready = httpx.get(base + "/healthz", timeout=.2).status_code == 200
                except httpx.HTTPError:
                    ready = False
                if ready:
                    break
                if time.monotonic() >= deadline:
                    pytest.fail("isolated correction HTTP readiness timed out")
                time.sleep(.05)
            assert httpx.post(base + "/mcp", timeout=2).status_code == 401
            read_receipt=asyncio.run(roundtrip())
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)
    assert counts(c.f)["steward_card_decisions"] == 2
    if return_read:
        return read_receipt


def test_legacy_authority_writer_cannot_bypass_managed_fact_resolution(facts):
    f = facts
    obs = observation(f, 2)
    set_current(f, obs)
    first = invoke(f, obs, "ready")
    card = f.store.get_steward_card(first["current"]["memory_id"])
    def forbidden_transform(*args):
        pytest.fail("managed authority bypass must fail before transform")
    with pytest.raises(ValueError, match="managed_fact_resolution_required"):
        f.store.steward_decision(decision_id="bypass", memory_id=card["memory_id"], action="stale",
            actor="legacy-writer", content_hash=card["content_hash"], transform=forbidden_transform)
    assert f.store.get_steward_card(card["memory_id"])["currentness"] == "current"
    following = observation(f, 3)
    set_current(f, following)
    changed = invoke(f, following, "failed", key="next", target=head_card(f, first))
    assert changed["action"] == "replace_current" and changed["generation"] == 2


def test_intake_before_correction_joins_verified_same_fact_without_second_commit(coordinator):
    c = coordinator
    worker = c.service.worker(project=c.f.scope.project, worker_id="fixture-worker")
    first = c.service.submit(c.request, lane="intake")["changes"][0]["task_id"]
    assert worker.run_once(task_id=first)["state"] == "resolved"
    # 교정 LLM이 수집 직전 조회한 원래 revision으로 요청해도 독립 source가 같은 현재 값을
    # 입증하면 근거만 합류한다. 상이한 새 값이면 이 stale target으로 갱신하지 않는다.
    second = c.service.submit(c.request)["changes"][0]["task_id"]
    assert worker.run_once(task_id=second)["state"] == "resolved"
    assert c.service.result(second)["resolution"]["action"] == "attach_evidence"
    assert counts(c.f)["steward_card_decisions"] == 2


def test_real_worker_process_restart_and_independent_monitor_resolve_durable_request(coordinator, tmp_path):
    from dataclasses import asdict
    import json
    import os
    from pathlib import Path
    import signal
    import subprocess
    c = coordinator
    config_file = tmp_path / "worker-policy.json"
    config_file.write_text(json.dumps({"schema_version": "fact_correction_config.v1", "repositories": [{
        "repository": c.rule.scope.repository, "root": str(c.root), "branch_ref": c.rule.scope.branch}],
        "rules": [asdict(c.rule)]}, ensure_ascii=False))
    config_file.chmod(0o600)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "lib"), "NEURON_LBRAIN_PGVECTOR_DSN": c.f.store.dsn}
    common = ["--config", str(config_file), "--project", c.f.scope.project]
    module = ["uv", "run", "--no-project", "-m", "agent_knowledge.fact_correction_coordinator"]
    argv = [*module, "worker", *common, "--worker-id", "daemon-fixture", "--iterations", "1000",
        "--poll-seconds", ".1", "--retry-seconds", ".2"]
    task_id = c.service.submit(c.request)["changes"][0]["task_id"]
    hidden = c.root.with_name("source-offline")
    c.root.rename(hidden)
    process = None
    def stop():
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)
    def await_state(state):
        deadline = time.monotonic() + 15
        while c.service.queue.get(task_id)["state"] != state:
            assert process.poll() is None, "isolated worker exited unexpectedly"
            assert time.monotonic() < deadline, "automatic recheck deadline exceeded"
            time.sleep(.05)
    with (tmp_path / "worker-process.log").open("wb") as log:
        try:
            process = subprocess.Popen(argv, env=env, stdout=log, stderr=log, start_new_session=True)
            await_state("blocked")
            assert counts(c.f)["steward_card_decisions"] == 1
            stop()
            monitor = subprocess.run([*module, "monitor", *common, "--heartbeat-grace", ".1"],
                env=env, capture_output=True, timeout=20, check=True)
            health = json.loads(monitor.stdout)
            assert health["attention_required"] and not health["heartbeat_recent"]
            hidden.rename(c.root)
            process = subprocess.Popen(argv, env=env, stdout=log, stderr=log, start_new_session=True)
            await_state("resolved")  # 수동 run_once/새 요청/시각 조작 없이 저장된 동일 작업을 처리한다.
            assert c.service.result(task_id)["resolution"]["applied_value"] == "ready"
            assert counts(c.f)["steward_card_decisions"] == 2
        finally:
            stop()
            if hidden.exists():
                hidden.rename(c.root)
