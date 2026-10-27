"""source capability를 실제 reader에 결속하고 모델 점수 이전에 범위를 검사한다."""
from dataclasses import replace
import json
import uuid

import pytest

from agent_knowledge.correction_capabilities import (
    CapabilityCheckedRelation, CapabilityUnavailable, EvidenceRequirement, ObservedCapability,
    ObservedFactRead, PgFactReader, Neo4jFactReader, GitRepositoryReader,
)
from agent_knowledge.correction_relation import CorrectionRelationClient, DEFAULT_CORRECTION_MODEL
from agent_knowledge.public_safe_util import hash_payload
from test_fact_resolution import counts
from test_fact_resolution_graph import NEO4J_URI

pytest_plugins = ("test_fact_resolution_graph",)


def observation():
    return ObservedFactRead(ObservedCapability.CANONICAL_STATE, hash_payload("fixture-scope"),
        hash_payload("fixture-resource"), hash_payload("fixture-revision"), "ready", "postgres_fact_head.v1")


def supported_client(on_call=None):
    # 실제 실패 사례 sp-23의 높은 지지 확률을 대역 응답으로 고정했다.
    # 독립 모델 검증으로 세지 않으며 code gate를 우회하지 못하는지 검사한다.
    def transport(*args):
        if on_call:
            on_call()
        return 200, json.dumps({"model": DEFAULT_CORRECTION_MODEL + "-20261016", "answers": {"relation": {
            "type": "choice", "choice": "supported", "probabilities": {"supported": .8493987773338323,
                "contradicted": .04938742717855439, "insufficient": .07601906192081054,
                "unrelated": .02519473356680268}}}}).encode()
    return CorrectionRelationClient(api_key="synthetic-auth", transport=transport)


@pytest.mark.parametrize("mismatch", ["capability", "context", "resource"])
def test_incompatible_reader_never_calls_the_model(mismatch):
    observed = observation()
    requirement = EvidenceRequirement(observed.capability, observed.context_hash, observed.resource_hash)
    if mismatch == "capability": requirement = replace(requirement, capability=ObservedCapability.GRAPH_STATE)
    if mismatch == "context": requirement = replace(requirement, context_hash=hash_payload("other-context"))
    if mismatch == "resource": requirement = replace(requirement, resource_hash=hash_payload("other-resource"))
    class Reader:
        def read(self): return observed
        def assert_still_current(self, value): pytest.fail("mismatched source must be rejected before further reads")
    client = supported_client(lambda: pytest.fail("incompatible source must not call the model"))
    with pytest.raises(CapabilityUnavailable, match="capability_or_scope_mismatch"):
        CapabilityCheckedRelation(reader=Reader(), requirement=requirement, client=client).evaluate(
            scope_description="graph 실제 저장", hypothesis="값은 ready다", policy_version="fixture")


def test_source_change_during_model_call_discards_high_supported_result():
    before = observation()
    class Reader:
        current = before
        def read(self): return self.current
        def assert_still_current(self, value):
            if value != self.current: raise CapabilityUnavailable("source_read_changed")
    reader = Reader()
    client = supported_client(lambda: setattr(reader, "current", replace(before, value="failed", revision_hash=hash_payload("new-revision"))))
    requirement = EvidenceRequirement(before.capability, before.context_hash, before.resource_hash)
    with pytest.raises(CapabilityUnavailable, match="source_read_changed"):
        CapabilityCheckedRelation(reader=reader, requirement=requirement, client=client).evaluate(
            scope_description="현재 상태", hypothesis="값은 ready다", policy_version="fixture")


def test_real_git_content_is_not_dirty_worktree_or_runtime_state(tmp_path):
    from agent_knowledge.correction_sources import GitSourcePolicy, GitSourceReader
    from agent_knowledge.fact_resolution import FactScope
    from test_correction_sources import git
    root = tmp_path / "fixture-repo"
    root.mkdir()
    git(root, "init", "-b", "fixture")
    path = root / "policy.md"
    path.write_text("인증 실패는 재시도하지 않는다.")
    git(root, "add", "policy.md"); git(root, "commit", "-m", "문서 명세")
    scope = FactScope("fixture", "fixture-repo", "refs/heads/fixture", "test", "retry", "documented_policy")
    source = GitSourceReader({scope.repository: GitSourcePolicy(scope.repository, root, branch_ref=scope.branch)})
    reader = GitRepositoryReader(source, scope=scope, relative_path="policy.md")
    first = reader.read()
    git(root, "checkout", "-b", "other-branch")
    path.write_text("다른 branch의 문서 내용이다.")
    git(root, "add", "policy.md"); git(root, "commit", "-m", "다른 branch의 명세")
    assert reader.read() == first  # checkout은 바뀌어도 등록한 branch의 관측이다.
    git(root, "checkout", "fixture")
    path.write_text("인증 실패도 재시도한다.")  # 아직 commit하지 않은 내용은 다른 source다.
    assert reader.read() == first and first.value == "인증 실패는 재시도하지 않는다."
    assert not first.satisfies(EvidenceRequirement(ObservedCapability.GRAPH_STATE, first.context_hash, first.resource_hash))
    git(root, "add", "policy.md"); git(root, "commit", "-m", "후속 문서 변경")
    with pytest.raises(CapabilityUnavailable, match="source_read_changed"):
        reader.assert_still_current(first)
    assert reader.read().value == "인증 실패도 재시도한다."
    head_source = GitSourceReader({scope.repository: GitSourcePolicy(scope.repository, root, branch_ref="HEAD")})
    with pytest.raises(ValueError, match="fixed source branch"):
        GitRepositoryReader(head_source, scope=replace(scope, branch="HEAD"), relative_path="policy.md")
    complete = '# 공개 구현 설명\n' + 'x' * 12000 + '\nfeature_state = "removed"\n'
    path.write_text(complete)
    git(root, "add", "policy.md"); git(root, "commit", "-m", "원래 한도 뒤의 상태도 전체 보존")
    assert reader.read().value == complete
    assert reader.read().value.endswith('feature_state = "removed"\n')
    path.write_text("x" * 32769)
    git(root, "add", "policy.md"); git(root, "commit", "-m", "전체 문서 한도 검사")
    with pytest.raises(CapabilityUnavailable, match="bounded_full_source"):
        reader.read()


def test_auto_fact_policy_does_not_track_dynamic_head_across_branches():
    from agent_knowledge.fact_correction_coordinator import GitJsonFactPolicy
    from agent_knowledge.fact_resolution import FactScope
    scope = FactScope("fixture", "fixture-repo", "HEAD", "test", "status", "value")
    with pytest.raises(ValueError, match="explicit fixed branch"):
        GitJsonFactPolicy("fixture", scope, "상태", "typed_payload.status_value", "상태: {value}", "facts.json", "/status")
    fixed = GitJsonFactPolicy("fixture", replace(scope, branch="refs/heads/main"), "상태",
        "typed_payload.status_value", "상태: {value}", "facts.json", "/status")
    assert fixed.scope.branch == "refs/heads/main"


@pytest.mark.skipif(not NEO4J_URI, reason="작업 전용 Neo4j 실제 reader 검증이 필요하다")
@pytest.mark.parametrize("facts", ["capability-" + uuid.uuid4().hex[:12]], indirect=True)
def test_actual_pg_cannot_prove_graph_state_but_direct_neo4j_read_can(coordinator, graph_adapter):
    from agent_knowledge.postgres_store.outbox_worker import GraphProjectionWorker
    c = coordinator
    task_id = c.service.submit(c.request)["changes"][0]["task_id"]
    assert c.service.worker(project=c.f.scope.project, worker_id="capability-test").run_once(task_id=task_id)["state"] == "resolved"
    pg_reader = PgFactReader(c.f.store, scope=c.rule.scope, field_path=c.rule.field_path)
    observed = pg_reader.read()
    assert observed.capability == ObservedCapability.CANONICAL_STATE and observed.value == "ready"
    requirement = EvidenceRequirement(ObservedCapability.GRAPH_STATE, observed.context_hash, observed.resource_hash)
    blocked = CapabilityCheckedRelation(reader=pg_reader, requirement=requirement,
        client=supported_client(lambda: pytest.fail("PG evidence must be rejected before model call")))
    with pytest.raises(CapabilityUnavailable, match="capability_or_scope_mismatch"):
        blocked.evaluate(scope_description="실제 graph 저장 값", hypothesis="graph 값은 ready다", policy_version="fixture")
    reader = Neo4jFactReader(c.f.store, graph_adapter, scope=c.rule.scope, field_path=c.rule.field_path)
    with pytest.raises(CapabilityUnavailable, match="graph_read_unavailable"):
        reader.read()  # PG의 현재 값만으로 pending graph를 인증하지 않는다.
    projector = GraphProjectionWorker(c.f.store, graph_adapter=graph_adapter, worker_id="capability-projector")
    assert projector.run_once() >= 1 and projector.last_batch_failed == 0
    graph_read = reader.read()
    assert graph_read.value == "ready" and graph_read.capability == ObservedCapability.GRAPH_STATE
    result = CapabilityCheckedRelation(reader=reader, requirement=requirement, client=supported_client()).evaluate(
        scope_description="실제 graph 저장 값", hypothesis="graph 값은 ready다", policy_version="fixture")
    assert result["observation"] == graph_read
    assert result["judgment"]["source_binding_hash"] == result["source_binding_hash"]
    assert counts(c.f)["steward_card_decisions"] == 2  # 판단 경로는 authority를 쓰지 않는다.


def test_pg_fact_reader_rejects_revoked_source_before_dispatch(coordinator):
    from test_fact_source_lifecycle import seeded
    c = coordinator
    source = seeded(c)
    reader = PgFactReader(c.f.store, scope=c.rule.scope, field_path=c.rule.field_path)
    assert reader.read().value == "ready"
    with c.f.store.transaction() as db:
        db.execute("UPDATE lbrain_fact_source_receipts SET state='revoked' WHERE source_ref_id=%s",
            (source["source_ref_id"],))
    with pytest.raises(CapabilityUnavailable, match="canonical_source_binding_unavailable"):
        reader.read()


def test_pg_fact_reader_rejects_disputed_head(facts):
    from test_fact_resolution import observation, set_current, invoke, head_card
    f = facts
    left = observation(f, 1, source="left")
    right = observation(f, 1, source="right")
    set_current(f, left)
    set_current(f, right)
    first = invoke(f, left, "ready")
    assert invoke(f, right, "failed", key="opposing", target=head_card(f, first))["action"] == "disputed"
    reader = PgFactReader(f.store, scope=f.scope, field_path=f.commit.field_path)
    with pytest.raises(CapabilityUnavailable, match="canonical_source_binding_unavailable"):
        reader.read()


def test_pg_fact_reader_retains_independent_active_support(facts):
    from test_fact_resolution import observation, set_current, invoke, head_card
    f = facts
    left = observation(f, 1, source="left")
    right = observation(f, 1, source="right")
    set_current(f, left)
    set_current(f, right)
    first = invoke(f, left, "ready")
    assert invoke(f, right, "ready", key="independent", target=head_card(f, first))["action"] == "attach_evidence"
    with f.store.transaction() as db:
        db.execute("UPDATE lbrain_fact_source_receipts SET state='revoked' WHERE source_ref_id=%s",
            (left.proof["source_ref_id"],))
    reader = PgFactReader(f.store, scope=f.scope, field_path=f.commit.field_path)
    assert reader.read().value == "ready"


def test_pg_fact_reader_discards_model_result_if_support_is_revoked_during_call(coordinator):
    from test_fact_source_lifecycle import seeded
    c = coordinator
    source = seeded(c)
    reader = PgFactReader(c.f.store, scope=c.rule.scope, field_path=c.rule.field_path)
    observed = reader.read()
    requirement = EvidenceRequirement(observed.capability, observed.context_hash, observed.resource_hash)
    def revoke():
        with c.f.store.transaction() as db:
            db.execute("UPDATE lbrain_fact_source_receipts SET state='revoked' WHERE source_ref_id=%s",
                (source["source_ref_id"],))
    gate = CapabilityCheckedRelation(reader=reader, requirement=requirement, client=supported_client(revoke))
    with pytest.raises(CapabilityUnavailable, match="canonical_source_binding_unavailable"):
        gate.evaluate(scope_description="현재 사실", hypothesis="값은 ready다", policy_version="fixture")
