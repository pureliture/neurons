"""응답 대역으로 source/profile 결속을 검사한다. 실제 모델 실증과 구분한다."""
from dataclasses import asdict, replace
import json
import time
from pathlib import Path

import pytest

from agent_knowledge import correction_semantic as semantic
from agent_knowledge.correction_capabilities import CapabilityUnavailable, EvidenceRequirement, ObservedCapability, ObservedFactRead
from agent_knowledge.correction_relation import CorrectionRelationClient, DEFAULT_CORRECTION_MODEL, questions
from agent_knowledge.correction_relation_policy import RelationSupportPolicy
from agent_knowledge.public_safe_util import hash_payload, sha256_text
from test_fact_resolution import facts, coordinator, counts  # noqa: F401

MODEL = DEFAULT_CORRECTION_MODEL + "-20261016"


def client_for(predicate, on_call=lambda state: None):
    def transport(url, body, key, timeout):
        state = json.loads(body)["state"]
        on_call(state)
        chosen = "supported" if predicate(state) else "contradicted"
        probabilities = {label: .02 for label in ("supported", "contradicted", "insufficient", "unrelated")}
        probabilities[chosen] = .94
        return 200, json.dumps({"model": MODEL, "answers": {"relation": {"type": "choice",
            "choice": chosen, "probabilities": probabilities}}}).encode()
    return CorrectionRelationClient(api_key="synthetic-auth", transport=transport)


class Reader:
    def __init__(self, value="단위 자료"):
        self.observed = ObservedFactRead(ObservedCapability.REPOSITORY_CONTENT, hash_payload("unit-context"),
            hash_payload("unit-resource"), hash_payload(value), value, "git_repository_content.v1")
    def read(self): return self.observed
    def assert_still_current(self, value):
        if value != self.observed: raise CapabilityUnavailable("source_read_changed")


def candidate_policy():
    return RelationSupportPolicy(.8, "unit-only", MODEL, hash_payload(questions()))


def report_fixture():
    # 단위 전용 자료와 대역 응답이다. 실제 holdout을 사용하지 않는다.
    # execution 필드 자체는 실호출의 암호학적 증명이 아니며 artifact는 서버 신뢰 경계다.
    rows = []
    for index in range(40):
        observed = Reader("단위 관측 " + str(index)).read()
        requirement = EvidenceRequirement(observed.capability, observed.context_hash, observed.resource_hash)
        case = {"id": "unit-" + str(index), "content": observed.value, "scope_description": "단위 범위",
            "claims": [["a", "값은 A다"], ["b", "값은 B다"]],
            "required_capability": "repository_content", "expected_value": "a" if index < 20 else None}
        selection = semantic.select_value(reader=Reader(observed.value), requirement=requirement,
            scope_description=case["scope_description"], claims=case["claims"], policy=candidate_policy(),
            policy_version=semantic.EVALUATION_POLICY, client=client_for(lambda state: index < 20 and state["hypothesis"] == "값은 A다"))
        rows.append({"case": case, "case_hash": hash_payload(case), "observed": asdict(observed),
            "requirement": asdict(requirement), "selection": selection, "source_revalidated": True})
    return {"schema_version": semantic.VALIDATION_SCHEMA, "identity": semantic.runtime_identity(),
        "execution": "live_openrouter", "support_min": .8, "policy_version": semantic.EVALUATION_POLICY,
        "development_case_hashes": [hash_payload("unit development")], "resolved_model": MODEL, "results": rows}


@pytest.fixture
def unit_profile(tmp_path):
    path = tmp_path / "unit-validation.json"
    path.write_text(json.dumps(report_fixture(), ensure_ascii=False))
    return path, sha256_text(path.read_text())


def test_profile_recomputes_criteria_and_rejects_runtime_changes(unit_profile, monkeypatch):
    path, digest = unit_profile
    assert semantic.load_validated_profile(path, expected_hash=digest).summary["positive_permits"] == 20
    monkeypatch.setattr(semantic, "runtime_identity", lambda: {})
    with pytest.raises(CapabilityUnavailable, match="semantic_profile_unvalidated"):
        semantic.load_validated_profile(path, expected_hash=digest)


@pytest.mark.parametrize("change", ["bytes", "false_positive", "overlap", "missing_result", "wrong_model", "development"])
def test_profile_rejects_unproven_validation(unit_profile, change):
    path, digest = unit_profile
    report = json.loads(path.read_text())
    if change == "bytes": path.write_text(path.read_text() + " ")
    else:
        if change == "false_positive":
            report["results"][0]["case"]["expected_value"] = None
            report["results"][0]["case_hash"] = hash_payload(report["results"][0]["case"])
        elif change == "overlap": report["development_case_hashes"].append(report["results"][0]["case_hash"])
        elif change == "missing_result": report["results"][30]["selection"]["judgments"]["a"].pop("relation")
        elif change == "wrong_model": report["resolved_model"] = DEFAULT_CORRECTION_MODEL + "-other"
        elif change == "development": report["execution"] = "development_openrouter"
        path.write_text(json.dumps(report, ensure_ascii=False))
        digest = sha256_text(path.read_text())
    with pytest.raises(CapabilityUnavailable, match="semantic_profile_unvalidated"):
        semantic.load_validated_profile(path, expected_hash=digest)


def test_multiple_supported_values_are_not_resolved_by_candidate_order():
    reader = Reader()
    observed = reader.read()
    selection = semantic.select_value(reader=reader,
        requirement=EvidenceRequirement(observed.capability, observed.context_hash, observed.resource_hash),
        scope_description="단위 범위", claims=[["a", "값은 A다"], ["b", "값은 B다"]],
        policy=candidate_policy(), policy_version="unit", client=client_for(lambda state: True))
    assert selection["value"] is None and selection["reason_code"] == "semantic_values_conflict"


@pytest.mark.parametrize("source", [
    "selected_name = primary_name if external_mode else secondary_name\n",
    "if flags.enabled:\n    result = 'enabled'\nelse:\n    result = 'disabled'\n",
    "result = supplied_value or 'default'\n",
])
def test_conditional_python_is_held_before_the_model_even_if_it_would_support_a_value(source):
    reader = Reader(source)
    observed = reader.read()
    with pytest.raises(CapabilityUnavailable, match="source_conditional_execution_unverifiable"):
        semantic.select_value(reader=reader,
            requirement=EvidenceRequirement(observed.capability, observed.context_hash, observed.resource_hash),
            scope_description="실제 선택 결과", claims=[["a", "결과는 A다"], ["b", "결과는 B다"]],
            policy=candidate_policy(), policy_version="unit", client=client_for(lambda state: True,
                on_call=lambda state: pytest.fail("분기 원천에서 모델 호출 금지")))


def test_json_wrapped_yaml_does_not_become_a_false_windows_path():
    reader = Reader("features:\n  enabled: false\n")
    observed = reader.read()
    chosen = semantic.select_value(reader=reader,
        requirement=EvidenceRequirement(observed.capability, observed.context_hash, observed.resource_hash),
        scope_description="파일의 설정", claims=[["off", "설정은 비활성이다"], ["on", "설정은 활성이다"]],
        policy=candidate_policy(), policy_version="unit", client=client_for(lambda state: state["hypothesis"] == "설정은 비활성이다"))
    assert chosen["value"] == "off"


@pytest.mark.parametrize("private", ["C:\\private\\secret.txt", "/Users/private/secret.txt", "Bearer private-token-value"])
def test_structured_premise_still_rejects_private_leaf_before_transport(private):
    client = client_for(lambda state: pytest.fail("private data must not reach provider"))
    with pytest.raises(ValueError, match="private or raw"):
        client.evaluate({"scope": "단위 범위", "hypothesis": "상태가 있다", "premise": json.dumps({
            "observed_capability": "repository_content", "observed_value": private})},
            policy_version="unit", source_binding_hash=hash_payload("unit"))


@pytest.fixture
def semantic_coordinator(coordinator, unit_profile):  # noqa: F811
    from agent_knowledge.fact_correction_coordinator import FactCorrectionCoordinator, GitSemanticFactPolicy
    from test_correction_sources import git
    c = coordinator
    path, digest = unit_profile
    text = "파일은 현재 기능 상태를 ready로 명시한다. 운영 배포 검증을 의미하지 않는다."
    (c.root / "facts.json").write_text(text)
    git(c.root, "add", "facts.json"); git(c.root, "commit", "-m", "합성 의미 관측")
    c.rule = GitSemanticFactPolicy("status", c.rule.scope, c.rule.target_title, c.rule.field_path,
        c.rule.render_template, "facts.json", "등록 branch의 이 파일에 명시된 기능 상태",
        (("ready", "이 파일은 현재 상태를 ready로 명시한다."), ("failed", "이 파일은 현재 상태를 failed로 명시한다.")),
        "repository_content", str(path), digest)
    c.calls = []
    def matching(state):
        value = json.loads(state["premise"])["observed_value"]
        return ("ready" in state["hypothesis"] and "ready" in value) or ("failed" in state["hypothesis"] and "failed" in value)
    c.matching = matching
    c.service = FactCorrectionCoordinator(store=c.f.store, git_reader=c.service.git_reader,
        policies=[c.rule], relation_client=client_for(matching, c.calls.append))
    c.request["changes"][0]["evidence"] = {"revision": git(c.root, "rev-parse", "HEAD"), "content_hash": sha256_text(text)}
    return c


def test_actual_pg_worker_uses_verified_value_not_proposal_and_reuses_result(semantic_coordinator):
    c = semantic_coordinator
    task_id = c.service.submit(c.request)["changes"][0]["task_id"]
    worker = c.service.worker(project=c.f.scope.project, worker_id="semantic-unit")
    run = worker.run_once(task_id=task_id)
    assert run["state"] == "resolved", run
    result = c.service.result(task_id)["resolution"]
    assert result["applied_value"] == "ready" and result["requested_value"] == "caller-wrong"
    assert result["admission"]["kind"] == "semantic" and result["admission"]["profile_hash"] == c.rule.validation_profile_hash
    assert len(c.calls) == 2 and counts(c.f)["steward_card_decisions"] == 2
    assert c.service.submit(c.request)["duplicate"]
    assert worker.run_once(task_id=task_id) == {"claimed": False}
    assert len(c.calls) == 2


def test_source_change_blocks_write_then_same_task_rechecks_current_source(semantic_coordinator):
    from test_correction_sources import git
    c = semantic_coordinator
    def change_once(state):
        c.calls.append(state)
        if len(c.calls) == 1:
            (c.root / "facts.json").write_text("파일은 현재 기능 상태를 failed로 명시한다.")
            git(c.root, "add", "facts.json"); git(c.root, "commit", "-m", "합성 후속 상태")
    c.service.relation_client = client_for(c.matching, change_once)
    task_id = c.service.submit(c.request)["changes"][0]["task_id"]
    worker = c.service.worker(project=c.f.scope.project, worker_id="semantic-change", retry_seconds=.1)
    first = worker.run_once(task_id=task_id)
    assert first["state"] == "blocked" and first["reason_code"] == "source_read_changed"
    assert counts(c.f)["steward_card_decisions"] == 1
    time.sleep(.12)
    second = worker.run_once(task_id=task_id)
    assert second["state"] == "resolved", second
    assert c.service.result(task_id)["resolution"]["applied_value"] == "failed"


@pytest.mark.parametrize("boundary", ["profile", "capability"])
def test_source_or_profile_boundary_prevents_authority(semantic_coordinator, boundary):
    c = semantic_coordinator
    if boundary == "capability":
        c.rule = replace(c.rule, required_capability="http_response")
        c.service.policies = {c.rule.policy_id: c.rule}
    else:
        def tamper(state):
            c.calls.append(state)
            if len(c.calls) == 2: Path(c.rule.validation_profile_path).write_text("{}")
        c.service.relation_client = client_for(c.matching, tamper)
    task_id = c.service.submit(c.request)["changes"][0]["task_id"]
    result = c.service.worker(project=c.f.scope.project, worker_id="semantic-boundary").run_once(task_id=task_id)
    assert result["state"] == "blocked", result
    assert result["reason_code"] == ("source_capability_or_scope_mismatch" if boundary == "capability" else "semantic_profile_unvalidated")
    assert counts(c.f)["steward_card_decisions"] == 1
    if boundary == "capability": assert c.calls == []
