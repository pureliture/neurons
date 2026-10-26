"""source capability를 실제 reader에 결속하고 모델 점수 이전에 범위를 검사한다."""
from dataclasses import replace
import json
import os
import uuid

import pytest

from agent_knowledge.correction_capabilities import (
    CapabilityCheckedRelation, CapabilityUnavailable, EvidenceRequirement, ObservedCapability,
    ObservedFactRead, PgFactReader, Neo4jFactReader,
)
from agent_knowledge.correction_relation import CorrectionRelationClient, DEFAULT_CORRECTION_MODEL
from agent_knowledge.public_safe_util import hash_payload
from test_fact_resolution import facts, coordinator, counts
from test_fact_resolution_graph import graph_adapter, NEO4J_URI


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
