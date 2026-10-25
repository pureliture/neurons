"""공통 교정과 실제 Graphiti/Neo4j의 합류. 외부 의미 모델/embedding은 별도 gate다."""
import os
import uuid
import time

import pytest

from test_fact_resolution import facts, coordinator
from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
from agent_knowledge.llm_brain_core.graphiti_adapter import GraphitiNeo4jGraphMemoryAdapter, _AsyncLoopRunner
from agent_knowledge.postgres_store.outbox_worker import GraphProjectionWorker
from agent_knowledge.postgres_store.pgvector_store import make_dummy_vector


NEO4J_URI = os.environ.get("LBRAIN_TEST_NEO4J_URI", "")
pytestmark = pytest.mark.skipif(not NEO4J_URI or not os.environ.get("LBRAIN_TEST_PG_DSN"),
    reason="작업 전용 PostgreSQL과 Neo4j가 모두 필요하다")


@pytest.fixture
def graph_adapter():
    from graphiti_core import Graphiti
    from graphiti_core.driver.neo4j_driver import Neo4jDriver
    from graphiti_core.embedder import EmbedderClient
    from graphiti_core.cross_encoder import CrossEncoderClient
    from graphiti_core.llm_client import LLMClient
    assert NEO4J_URI.startswith("bolt://127.0.0.1:")
    class LocalEmbedder(EmbedderClient):
        async def create(self, input_data):
            return make_dummy_vector(42)
    class LocalReranker(CrossEncoderClient):
        async def rank(self, query, passages):
            return [(passage, 0.0) for passage in passages]
    class NoNetworkLLM(LLMClient):
        def __init__(self):
            super().__init__(config=None)
        async def _generate_response(self, *args, **kwargs):
            raise AssertionError("이 graph 정합성 검사에서 외부 LLM 호출 금지")
    runner = _AsyncLoopRunner()
    graph = Graphiti(graph_driver=Neo4jDriver(NEO4J_URI, None, None), llm_client=NoNetworkLLM(),
        embedder=LocalEmbedder(), cross_encoder=LocalReranker())
    runner.run(lambda: graph.build_indices_and_constraints(), timeout=30)
    adapter = GraphitiNeo4jGraphMemoryAdapter(graph, runner=runner, extract_entities=False,
        read_timeout_seconds=3, write_timeout_seconds=3)
    try:
        yield adapter
    finally:
        runner.shutdown()


@pytest.mark.parametrize("facts", ["fact-graph-" + uuid.uuid4().hex[:12]], indirect=True)
def test_current_fact_remains_queryable_before_new_graph_or_embedding_projection(coordinator, facts, graph_adapter):
    c = coordinator
    projector = GraphProjectionWorker(c.f.store, graph_adapter=graph_adapter, worker_id=c.f.scope.project)
    assert projector.run_once() == 1
    assert projector.last_batch_failed == 0
    reader = GraphFirstResolver(store=c.f.store, graph_adapter=graph_adapter,
        embed_query=lambda query: make_dummy_vector(42))
    before = reader.resolve(project=c.f.scope.project, mode="query", query="기능 상태")
    assert before["items"][0]["typed_payload"]["status_value"] == "missing"
    task_id = c.service.submit(c.request)["changes"][0]["task_id"]
    assert c.service.worker(project=c.f.scope.project, worker_id="correction").run_once(task_id=task_id)["state"] == "resolved"
    receipt = c.service.result(task_id)["resolution"]
    assert c.f.store.get_card(receipt["current"]["memory_id"]).embedding_state == "pending"
    # graph에는 옛 episode만 있고 successor embedding도 아직 없다.
    lagging = reader.resolve(project=c.f.scope.project, mode="query", query="기능 상태",
        min_fact_generation=receipt["project_generation"])
    assert [item["typed_payload"]["status_value"] for item in lagging["items"]] == ["ready"], lagging
    assert lagging["metadata"]["fact_fallback_used"]
    assert "missing" not in str(lagging["statuses"])
    no_embedding = GraphFirstResolver(store=c.f.store, graph_adapter=graph_adapter).resolve(
        project=c.f.scope.project, mode="query", query="기능 상태")
    assert [item["typed_payload"]["status_value"] for item in no_embedding["items"]] == ["ready"]
    assert no_embedding["metadata"]["retrieval_path"] == "pg_fact_fallback"
    assert no_embedding["metadata"]["embedding_search_status"] == "unavailable"
    conversational = GraphFirstResolver(store=c.f.store, graph_adapter=graph_adapter).resolve(
        project=c.f.scope.project, mode="query", query="기능 상태가 지금 어떻게 돼?")
    assert [item["typed_payload"]["status_value"] for item in conversational["items"]] == ["ready"]
    assert projector.run_once() >= 1
    assert projector.last_batch_failed == 0
    after = reader.resolve(project=c.f.scope.project, mode="query", query="기능 상태")
    assert [item["typed_payload"]["status_value"] for item in after["items"]] == ["ready"]


@pytest.mark.skipif(not os.environ.get("LBRAIN_TEST_GRAPH_CONTROL_DIR"), reason="별도 task graph 종료/복구 제어 필요")
@pytest.mark.parametrize("facts", ["fact-outage-" + uuid.uuid4().hex[:12]], indirect=True)
def test_real_graph_outage_and_recovery_keeps_current_fact(coordinator, facts, graph_adapter):
    """외부 검증기가 task 전용 Neo4j만 종료/복구한다. 테스트에는 Docker 권한을 주지 않는다."""
    import copy
    from pathlib import Path
    from test_correction_sources import git
    from agent_knowledge.public_safe_util import sha256_text
    c = coordinator
    control = Path(os.environ["LBRAIN_TEST_GRAPH_CONTROL_DIR"])
    assert control.is_dir()
    def wait(name):
        deadline = time.monotonic() + 60
        while not (control / name).exists():
            assert time.monotonic() < deadline, "task graph 제어 deadline 초과"
            time.sleep(.1)
    projector = GraphProjectionWorker(c.f.store, graph_adapter=graph_adapter, worker_id=c.f.scope.project)
    assert projector.run_once() == 1 and projector.last_batch_failed == 0
    task_id = c.service.submit(c.request)["changes"][0]["task_id"]
    correction = c.service.worker(project=c.f.scope.project, worker_id="correction")
    assert correction.run_once(task_id=task_id)["state"] == "resolved"
    assert projector.run_once() >= 1 and projector.last_batch_failed == 0
    (control / "ready_for_stop").touch()
    wait("graph_stopped")
    try:
        # graph 장애 중 새 실제 source revision의 변경도 canonical authority에 반영한다.
        text = '{"status":"failed"}'
        (c.root / "facts.json").write_text(text)
        git(c.root, "add", "facts.json"); git(c.root, "commit", "-m", "graph 장애 중 실제 상태 변경")
        changed_request = copy.deepcopy(c.request)
        changed_request["idempotency_key"] = "while-graph-offline"
        change = changed_request["changes"][0]
        change.pop("target_memory_id"); change.pop("expected_content_hash")
        change.update(target_description=c.rule.target_title, old_value="ready", proposed_value="failed",
            evidence={"revision": git(c.root, "rev-parse", "HEAD"), "content_hash": sha256_text(text)})
        changed_id = c.service.submit(changed_request)["changes"][0]["task_id"]
        assert correction.run_once(task_id=changed_id)["state"] == "resolved"
        claimed = projector.run_once()
        assert claimed >= 1 and projector.last_batch_failed == claimed
        assert c.f.store.list_graph_projection_jobs(status="failed")
        offline = GraphFirstResolver(store=c.f.store, graph_adapter=graph_adapter).resolve(
            project=c.f.scope.project, mode="query", query="기능 상태")
        assert [item["typed_payload"]["status_value"] for item in offline["items"]] == ["failed"]
        assert offline["metadata"]["graph_status"] in {"unavailable", "degraded"}
        assert offline["metadata"]["fact_fallback_used"]
    finally:
        (control / "offline_observed").touch()
    wait("graph_restarted")
    deadline = time.monotonic() + 30
    while any(job.status != "completed" for job in c.f.store.list_graph_projection_jobs()):
        assert time.monotonic() < deadline, "graph 복구 후 projection 수렴 deadline 초과"
        projector.run_once()
        time.sleep(.1)
    online = GraphFirstResolver(store=c.f.store, graph_adapter=graph_adapter).resolve(
        project=c.f.scope.project, mode="query", query="기능 상태")
    assert [item["typed_payload"]["status_value"] for item in online["items"]] == ["failed"]
    assert online["metadata"]["retrieval_path"] == "graph_neo4j"
    assert not online["metadata"]["fallback_used"]


@pytest.mark.parametrize("facts", ["fact-reverse-" + uuid.uuid4().hex[:12]], indirect=True)
def test_real_graph_reverse_duplicate_and_expired_same_worker_lease(coordinator, facts, graph_adapter):
    c = coordinator
    projector = GraphProjectionWorker(c.f.store, graph_adapter=graph_adapter, worker_id="projector")
    old = c.f.store.claim_graph_projection_leases(worker_id="same-worker", lease_seconds=1)[0]
    time.sleep(1.05)
    with pytest.raises(ValueError, match="not owned"):
        c.f.store.mark_graph_projection_completed(old.projection_id, "same-worker", old.lease_generation)
    with pytest.raises(ValueError, match="not owned"):
        c.f.store.mark_graph_projection_failed(old.projection_id, "expired", worker_id="same-worker",
            lease_generation=old.lease_generation)
    current_lease = c.f.store.claim_graph_projection_leases(worker_id="same-worker", lease_seconds=30)[0]
    assert current_lease.lease_generation == old.lease_generation + 1
    with pytest.raises(ValueError, match="not owned"):
        c.f.store.mark_graph_projection_completed(old.projection_id, "same-worker", old.lease_generation)
    projector.process_job(current_lease)
    c.f.store.mark_graph_projection_completed(current_lease.projection_id, "same-worker", current_lease.lease_generation)
    task_id = c.service.submit(c.request)["changes"][0]["task_id"]
    assert c.service.worker(project=c.f.scope.project, worker_id="correct").run_once(task_id=task_id)["state"] == "resolved"
    head = c.service.result(task_id)["resolution"]["current"]["memory_id"]
    jobs = c.f.store.claim_graph_projection_leases(worker_id="reverse", lease_seconds=30)
    assert len(jobs) >= 2
    jobs.sort(key=lambda job: (job.source_id != head, job.projection_id))
    for job in jobs:
        projector.process_job(job)
        projector.process_job(job)  # 같은 실제 Neo4j projection을 중복 전달한다.
        c.f.store.mark_graph_projection_completed(job.projection_id, "reverse", job.lease_generation)
    # 가장 오래된 current episode가 나중에 재생돼도 PG의 현재 사실로만 답해야 한다.
    projector.process_job(old)
    with pytest.raises(ValueError, match="not owned"):
        c.f.store.mark_graph_projection_completed(old.projection_id, "same-worker", old.lease_generation)
    result = GraphFirstResolver(store=c.f.store, graph_adapter=graph_adapter).resolve(
        project=c.f.scope.project, mode="query", query="기능 상태")
    assert [item["typed_payload"]["status_value"] for item in result["items"]] == ["ready"]
    assert result["items"][0]["id"] == head


@pytest.mark.parametrize("facts", ["fact-old-query-" + uuid.uuid4().hex[:12]], indirect=True)
def test_old_fact_query_does_not_return_superseded_session_claim(coordinator, facts, graph_adapter):
    from agent_knowledge.postgres_store.pgvector_store import SessionChunk
    from agent_knowledge.public_safe_util import sha256_text
    c = coordinator
    projector = GraphProjectionWorker(c.f.store, graph_adapter=graph_adapter, worker_id="old-query")
    assert projector.run_once() == 1
    task_id = c.service.submit(c.request)["changes"][0]["task_id"]
    assert c.service.worker(project=c.f.scope.project, worker_id="correct").run_once(task_id=task_id)["state"] == "resolved"
    projector.run_once()
    head = c.service.result(task_id)["resolution"]["current"]["memory_id"]
    body = "기능 상태: missing"
    c.f.store.insert_chunk(SessionChunk(chunk_id="old_session_claim", project=c.f.scope.project,
        session_id_hash=sha256_text(c.f.scope.project), content_markdown=body, content_hash=sha256_text(body),
        embedding=make_dummy_vector(42), embedding_state="ready"))
    result = GraphFirstResolver(store=c.f.store, graph_adapter=graph_adapter,
        embed_query=lambda query: make_dummy_vector(42)).resolve(project=c.f.scope.project, mode="query", query="missing")
    assert [item["id"] for item in result["items"]] == [head], result
    assert result["items"][0]["typed_payload"]["status_value"] == "ready"
