"""Disposable PostgreSQL + real Neo4j Graphiti card flow; no external model APIs.

Requires LBRAIN_TEST_PG_DSN (fixture-local PG) and LBRAIN_TEST_NEO4J_URI
(pointing at a disposable loopback-only Neo4j instance).
"""
from __future__ import annotations

import hashlib
import os
import uuid

import pytest
from neo4j import GraphDatabase

from agent_knowledge.llm_brain_core.graphiti_adapter import (
    GraphitiNeo4jGraphMemoryAdapter,
    _AsyncLoopRunner,
)
from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
from agent_knowledge.postgres_store.outbox_worker import GraphProjectionWorker
from agent_knowledge.session_memory.brain_steward import BrainStewardService


PG_DSN = os.environ.get("LBRAIN_TEST_PG_DSN", "")
NEO4J_URI = os.environ.get("LBRAIN_TEST_NEO4J_URI", "")
pytestmark = pytest.mark.skipif(
    not PG_DSN or not NEO4J_URI,
    reason="disposable PostgreSQL and Neo4j test endpoints required",
)


class _NoCardLedger:
    read_only = False

    def __getattr__(self, name):
        if name.startswith(("get_llm_brain", "list_llm_brain", "upsert_llm_brain", "_transaction")):
            raise AssertionError("SQLite card lane must not be accessed")
        raise AttributeError(name)


def test_approved_card_projects_to_real_neo4j_and_resolves_from_pg(isolated_pg_store, tmp_path):

    assert NEO4J_URI.startswith("bolt://127.0.0.1:")  # never a remote/live Neo4j
    store = isolated_pg_store
    project = "pg-neo4j-" + uuid.uuid4().hex[:12]
    content_hash = "sha256:" + hashlib.sha256(project.encode()).hexdigest()
    span = {
        "card_type": "preference", "project": project, "provider": "hermes", "scope": "project",
        "title": "PostgreSQL approved card", "redacted_summary": "Use PostgreSQL for card storage",
        "typed_payload": {"preference": "Use PostgreSQL for card storage", "explicitness": "explicit",
                          "repeated_count": 1, "confirmation_status": "confirmed", "applies_to": "card_storage"},
        "source_ref": {"source_id": "synthetic-source"}, "span_ref": {"span_id": "synthetic-span"},
        "content_hash": content_hash, "confidence": 0.9, "confidence_basis": "synthetic test",
    }
    steward = BrainStewardService(_NoCardLedger(), pgvector_store=store, allow_restricted=True)
    candidate_id = steward.candidate_create(source_span=span)["memory_id"]
    assert steward.review_queue_list(project=project)["count"] == 1
    assert GraphFirstResolver(store=store).resolve(project=project, mode="list")["items"] == []
    assert not [job for job in store.list_graph_projection_jobs() if job.source_id == candidate_id]
    steward.candidate_approve(candidate_memory_id=candidate_id, approved_by="test", decision_id="synthetic-approval")
    assert store.get_card(candidate_id).authorization_status == "active"
    assert store.get_steward_decision("synthetic-approval")["memory_id"] == candidate_id

    # Test-only local embedder/reranker and a network-forbidden LLM ensure
    # that real graph search cannot call an external model API.
    from graphiti_core import Graphiti
    from graphiti_core.driver.neo4j_driver import Neo4jDriver
    from graphiti_core.embedder import EmbedderClient
    from graphiti_core.cross_encoder import CrossEncoderClient
    from graphiti_core.llm_client import LLMClient
    from agent_knowledge.postgres_store.pgvector_store import make_dummy_vector
    from agent_knowledge.knowledge_search_service import KnowledgeSearchService, DisabledRetiredIndexBridgeClient
    from agent_knowledge.ledger import Ledger
    from agent_knowledge.mcp_jsonrpc import dispatch_tool_call

    class LocalEmbedder(EmbedderClient):
        async def create(self, input_data):
            return make_dummy_vector(42)

    class LocalReranker(CrossEncoderClient):
        async def rank(self, query, passages):
            return [(passage, 0.0) for passage in passages]

    class NoNetworkLLM(LLMClient):
        def __init__(self):
            super().__init__(config=None)

        async def _generate_response(self, messages, response_model=None, max_tokens=16384, model_size=None):
            raise AssertionError("external model call forbidden in PG/Neo4j test")

    runner = _AsyncLoopRunner()
    graph = Graphiti(graph_driver=Neo4jDriver(NEO4J_URI, None, None),
                     llm_client=NoNetworkLLM(),
                     embedder=LocalEmbedder(), cross_encoder=LocalReranker())
    runner.run(lambda: graph.build_indices_and_constraints(), timeout=30)
    adapter = GraphitiNeo4jGraphMemoryAdapter(graph, runner=runner, extract_entities=False)
    worker = GraphProjectionWorker(store, graph_adapter=adapter, worker_id=project)
    try:
        class SkippedGraph:
            def upsert_episode(self, episode):
                return "skipped_disabled"

        skipped = GraphProjectionWorker(store, graph_adapter=SkippedGraph(), worker_id=project + "-skip")
        assert skipped.run_once() == 1
        pending = [job for job in store.list_graph_projection_jobs() if job.source_id == candidate_id]
        assert len(pending) == 1 and pending[0].status == "failed"
        assert skipped.last_batch_failed == 1
        # Force only this disposable job's backoff deadline to expire; the
        # subsequent real Graphiti adapter must persist before PG can ack it.
        with store.transaction() as db:
            db.execute("UPDATE graph_projection_outbox SET lease_until = NOW() - INTERVAL '1 second' WHERE source_id = %s", (candidate_id,))
        assert worker.run_once() == 1
        jobs = [job for job in store.list_graph_projection_jobs() if job.source_id == candidate_id]
        assert len(jobs) == 1 and jobs[0].status == "completed"
        with GraphDatabase.driver(NEO4J_URI, auth=None) as driver:
            with driver.session(database="neo4j") as session:
                rows = session.run(
                    "MATCH (e:Episodic) WHERE e.content CONTAINS $id "
                    "RETURN e.group_id AS graph_group, e.content AS body", id=candidate_id
                ).data()
        assert len(rows) == 1 and '"authority_memory_id"' in rows[0]["body"]
        assert content_hash in rows[0]["body"]
        from agent_knowledge.llm_brain_core.graph_scope import graph_group_id
        assert rows[0]["graph_group"] == graph_group_id(f"/project/{project}")
        graph_read = adapter.search_context(brain_id=f"/project/{project}", query="card storage", limit=5)
        assert graph_read.status == "available", (graph_read.status, graph_read.details, len(graph_read.episodes))
        result = GraphFirstResolver(store=store, graph_adapter=adapter).resolve(project=project, mode="query", query="card storage")
        assert result["metadata"]["retrieval_path"] == "graph_neo4j", repr(result.get("metadata")) + " " + repr(result.get("error")) + " items=" + repr(len(result.get("items") or []))
        assert result["metadata"]["authority_join_status"] == "verified"
        assert [item["id"] for item in result["items"]] == [candidate_id]
        listed = GraphFirstResolver(store=store, graph_adapter=adapter).resolve(project=project, mode="list")
        assert [item["id"] for item in listed["items"]] == [candidate_id]
        # Exercise the real public MCP dispatch + service wiring, not only the resolver.
        service = KnowledgeSearchService(
            ledger=Ledger(tmp_path / "mcp-ledger.sqlite"),
            retired_index_bridge=DisabledRetiredIndexBridgeClient(), dataset_ids=[],
            pgvector_store=store, graph_adapter=adapter,
        )
        for mode, query in (("list", ""), ("query", "card storage")):
            public = dispatch_tool_call(
                {"name": "brain.resolve", "arguments": {"project": project, "mode": mode, "query": query}},
                service, surface="agent",
            )["structuredContent"]
            assert [item["id"] for item in public["items"]] == [candidate_id]
            assert public["metadata"]["authority_join_status"] == "verified"
            if mode == "query":
                assert public["metadata"]["retrieval_path"] == "graph_neo4j"
        # The graph is shared, but an unrelated project must not see this card.
        unrelated = GraphFirstResolver(store=store, graph_adapter=adapter).resolve(
            project="unrelated-project", mode="query", query="card storage"
        )
        assert unrelated["items"] == []
        stale_id = steward.stale_mark(memory_id=candidate_id, reason="synthetic expiry")["memory_id"]
        steward.stale_commit(proposal_memory_id=stale_id, approved_by="test", decision_id="synthetic-stale")
        # The old graph episode still exists; PG currentness must exclude it immediately.
        after_stale = GraphFirstResolver(store=store, graph_adapter=adapter).resolve(
            project=project, mode="query", query="card storage"
        )
        assert after_stale["items"] == []
        assert GraphFirstResolver(store=store, graph_adapter=adapter).resolve(
            project=project, mode="list"
        )["items"] == []
    finally:
        runner.shutdown()
        # Data is synthetic and the server is disposable; no production graph is used.
