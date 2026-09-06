"""Tier 3 Feature Combination Coverage Test Suite (Simulation).

Pure simulation suite running against InMemoryPostgresStore, InMemoryQdrantStore,
and MockMCPServer with isolated legacy 1536-dim simulation profile.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from .conftest import (
    InMemoryPostgresStore,
    InMemoryQdrantStore,
    MemoryCard,
    MemoryEdge,
    MockMCPServer,
    SessionChunk,
    SlimSerializer,
    make_dummy_vector,
    sha256_str,
)

pytestmark = [pytest.mark.simulation, pytest.mark.legacy_profile]


def test_tier3_01_f1_f4_f6_slim_rationalized_query(mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore):
    """
    Combination: F1 (brain.resolve) + F4 (Slim Serializer) + F6 (Schema Rationalization)
    Public brain.resolve(slim) produces rationalized payload <= 1.2 KB with zero empty lanes.
    """
    for i in range(3):
        pg_store.insert_card(
            MemoryCard(
                memory_id=f"mem_combo_{i}",
                project="neurons",
                card_type="decision",
                title=f"Architectural Decision {i}",
                summary=f"Decision summary {i}",
                typed_payload={"decision": f"Decision {i}", "rationale": "High performance"},
                lifecycle_state="human_accepted",
                authorization_status="active",
                content_hash=sha256_str(f"combo_{i}"),
            )
        )

    req = {
        "jsonrpc": "2.0",
        "id": 301,
        "method": "tools/call",
        "params": {
            "name": "brain.resolve",
            "arguments": {"project": "neurons", "mode": "context", "response_mode": "slim"},
        },
    }
    res = mcp_server.handle_public_request(req)
    assert "result" in res
    result = res["result"]

    # Assert slim schema and size
    raw_json = json.dumps(result)
    assert len(raw_json.encode("utf-8")) <= 1200
    assert result["schema_version"] == "lbrain_slim_context.v1"

    # Assert rationalization: 0 empty lanes, no route_spec, no duplicate tasks
    for lane_idx in range(1, 8):
        assert f"lane_{lane_idx}" not in result
    assert "route_spec" not in result
    assert "current_task" not in result


def test_tier3_02_f1_f5_f10_with_evidence_recursive_dag(mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore):
    """
    Combination: F1 (brain.resolve) + F5 (With-Evidence) + F10 (Recursive DAG)
    brain.resolve(with_evidence) traverses recursive DAG to return full multi-hop provenance chains.
    """
    # 3-level ancestry
    c1 = MemoryCard("root_dec", "neurons", "decision", "Root", "Root", {}, lifecycle_state="human_accepted", authorization_status="active", content_hash="sha256:root")
    c2 = MemoryCard("mid_dec", "neurons", "decision", "Mid", "Mid", {}, lifecycle_state="human_accepted", authorization_status="active", content_hash="sha256:mid")
    c3 = MemoryCard("leaf_dec", "neurons", "decision", "Leaf", "Leaf", {}, lifecycle_state="human_accepted", authorization_status="active", content_hash="sha256:leaf")
    for c in (c1, c2, c3):
        pg_store.insert_card(c)

    pg_store.insert_edge(MemoryEdge(1, "leaf_dec", "derived_from", "mid_dec", "sha256:e1"))
    pg_store.insert_edge(MemoryEdge(2, "mid_dec", "derived_from", "root_dec", "sha256:e2"))

    req = {
        "jsonrpc": "2.0",
        "id": 302,
        "method": "tools/call",
        "params": {
            "name": "brain.resolve",
            "arguments": {"project": "neurons", "response_mode": "with_evidence"},
        },
    }
    res = mcp_server.handle_public_request(req)
    result = res["result"]
    assert result["schema_version"] == "lbrain_evidence_context.v1"
    assert len(result["edges"]) == 2
    assert "sha256:root" in result["evidence_hashes"]
    assert "sha256:leaf" in result["evidence_hashes"]


def test_tier3_03_f2_f3_candidate_proposal_admin_approval(mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore):
    """
    Combination: F2 (memory_candidate_create) + F3 (agent_memory_admin isolation)
    Candidate created by agent in candidate/disabled state, public agent cannot approve, admin approves.
    """
    create_req = {
        "jsonrpc": "2.0",
        "id": 303,
        "method": "tools/call",
        "params": {
            "name": "memory_candidate_create",
            "arguments": {
                "card_type": "decision",
                "project": "neurons",
                "title": "Proposed Migration",
                "summary": "Propose PGVector migration",
                "typed_payload": {"decision": "Migrate"},
                "content_hash": "sha256:prop1",
            },
        },
    }
    create_res = mcp_server.handle_public_request(create_req)
    card_id = create_res["result"]["memory_id"]
    assert pg_store.cards[card_id].authorization_status == "disabled"

    # Agent attempts to approve via public endpoint -> Rejected
    agent_approve = {
        "jsonrpc": "2.0",
        "id": 3031,
        "method": "tools/call",
        "params": {"name": "memory_candidate_approve", "arguments": {"memory_id": card_id}},
    }
    reject_res = mcp_server.handle_public_request(agent_approve)
    assert "error" in reject_res
    assert reject_res["error"]["code"] == -32601

    # Admin approves via admin endpoint
    admin_approve = {
        "jsonrpc": "2.0",
        "id": 3032,
        "method": "tools/call",
        "params": {"name": "memory_candidate_approve", "arguments": {"memory_id": card_id}},
    }
    app_res = mcp_server.handle_admin_request(admin_approve, auth_token="lbrain_admin")
    assert app_res["result"]["status"] == "approved"
    assert pg_store.cards[card_id].authorization_status == "active"


def test_tier3_04_f2_f9_candidate_outbox_cas_flow(mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore):
    """
    Combination: F2 (memory_candidate_create) + F9 (Outbox & CAS)
    Candidate creation automatically enqueues outbox job; worker claims via SKIP LOCKED and applies CAS.
    """
    create_req = {
        "jsonrpc": "2.0",
        "id": 304,
        "method": "tools/call",
        "params": {
            "name": "memory_candidate_create",
            "arguments": {
                "card_type": "decision",
                "project": "neurons",
                "title": "Outbox Trigger Decision",
                "summary": "Summary",
                "typed_payload": {"decision": "Outbox"},
                "content_hash": "sha256:outbox_flow",
            },
        },
    }
    res = mcp_server.handle_public_request(create_req)
    card_id = res["result"]["memory_id"]

    # Verify outbox job was enqueued
    assert len(pg_store.outbox) == 1
    job = list(pg_store.outbox.values())[0]
    assert job.target_id == card_id
    assert job.status == "queued"

    # Worker claims job
    claimed = pg_store.claim_outbox_jobs("worker_embed_1", batch_size=1)
    assert len(claimed) == 1

    # Worker executes CAS write-back
    vec = make_dummy_vector(55)
    cas_ok = pg_store.cas_update_embedding(claimed[0].outbox_id, card_id, "sha256:outbox_flow", vec)
    assert cas_ok is True
    assert pg_store.cards[card_id].embedding_state == "ready"
    assert pg_store.outbox[claimed[0].outbox_id].status == "completed"


def test_tier3_05_f7_f8_f1_pgvector_ddl_relaxed_order_resolve(mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore):
    """
    Combination: F7 (Postgres DDL) + F8 (relaxed_order) + F1 (brain.resolve)
    Postgres schema queried via hybrid search using relaxed_order and resolved via brain.resolve.
    """
    pg_store.execute_ddl("CREATE TABLE memory_cards (...)")
    pg_store.set_local_guc("hnsw.iterative_scan", "relaxed_order")

    v = make_dummy_vector(77)
    pg_store.insert_card(
        MemoryCard(
            memory_id="mem_ddl_guc",
            project="neurons",
            card_type="decision",
            title="DDL GUC Decision",
            summary="Tested under iterative scan",
            typed_payload={"decision": "Use GUC"},
            lifecycle_state="human_accepted",
            authorization_status="active",
            embedding=v,
            embedding_state="ready",
            content_hash=sha256_str("ddl_guc"),
        )
    )

    # 1. Direct PG hybrid vector search
    results = pg_store.hybrid_vector_search(v, project="neurons", limit=5)
    assert len(results) == 1
    assert results[0]["similarity_score"] == 1.0

    # 2. Public brain.resolve context query
    req = {
        "jsonrpc": "2.0",
        "id": 305,
        "method": "tools/call",
        "params": {"name": "brain.resolve", "arguments": {"project": "neurons", "mode": "context"}},
    }
    res = mcp_server.handle_public_request(req)
    assert len(res["result"]["decisions"]) == 1
    assert res["result"]["decisions"][0]["id"] == "mem_ddl_guc"


def test_tier3_06_f7_f9_f10_concurrent_card_update_dag_cas(pg_store: InMemoryPostgresStore):
    """
    Combination: F7 (memory_cards) + F9 (Outbox CAS) + F10 (memory_edges)
    Concurrent card modification during outbox embedding update preserves DAG edge consistency and CAS integrity.
    """
    # Card 1 and Card 2 in DAG
    c1 = MemoryCard("node_v1", "neurons", "decision", "Node V1", "S", {}, content_hash="sha256:v1", embedding_state="pending")
    c2 = MemoryCard("node_parent", "neurons", "decision", "Parent", "S", {}, content_hash="sha256:p", embedding_state="ready")
    pg_store.insert_card(c1)
    pg_store.insert_card(c2)
    pg_store.insert_edge(MemoryEdge(1, "node_v1", "derived_from", "node_parent", "sha256:edge1"))

    # Outbox job created for v1
    job_id = pg_store.enqueue_outbox("memory_card", "node_v1", "sha256:v1", "V1 text")

    # Concurrent card update to v2 occurs
    pg_store.cards["node_v1"].content_hash = "sha256:v2"
    pg_store.cards["node_v1"].title = "Node V2"

    # Worker tries to write embedding for v1
    cas_ok = pg_store.cas_update_embedding(job_id, "node_v1", "sha256:v1", make_dummy_vector(1))
    assert cas_ok is False  # Stale write blocked by CAS!

    # Verify DAG traversal remains valid
    tree = pg_store.recursive_dag_traversal("node_v1")
    assert len(tree) == 1
    assert tree[0]["dst_id"] == "node_parent"


def test_tier3_07_f11_f12_backfill_and_dual_read_benchmark(qdrant_store: InMemoryQdrantStore, pg_store: InMemoryPostgresStore):
    """
    Combination: F11 (Qdrant backfill) + F12 (Dual-Read Shadow Benchmark)
    Backfilled data from Qdrant into Postgres verified with Recall@5 >= 0.95 benchmark.
    """
    # Seed 15 items in Qdrant
    for i in range(15):
        vec = make_dummy_vector(200 + i)
        qdrant_store.upsert(
            f"chunk_{i}",
            vec,
            {
                "memory_id": f"chunk_{i}",
                "project": "neurons",
                "title": f"Chunk Title {i}",
                "summary": f"Summary {i}",
                "content_hash": sha256_str(f"chunk_{i}"),
                "lifecycle_state": "human_accepted",
                "authorization_status": "active",
            },
        )

    # Execute backfill migration
    for pid, (vec, p) in qdrant_store.vectors.items():
        pg_store.insert_card(
            MemoryCard(
                memory_id=pid,
                project=p["project"],
                card_type="decision",
                title=p["title"],
                summary=p["summary"],
                typed_payload={},
                content_hash=p["content_hash"],
                lifecycle_state=p["lifecycle_state"],
                authorization_status=p["authorization_status"],
                embedding=vec,
                embedding_state="ready",
            )
        )

    # Run Dual-Read Shadow Benchmark across 15 query vectors
    recall_values = []
    for i in range(15):
        query_v = make_dummy_vector(200 + i)
        q_results = [r["id"] for r in qdrant_store.search(query_v, limit=5)]
        pg_results = [r["memory_id"] for r in pg_store.hybrid_vector_search(query_v, project="neurons", limit=5)]
        overlap = len(set(q_results).intersection(set(pg_results)))
        recall_values.append(overlap / len(q_results))

    avg_recall = sum(recall_values) / len(recall_values)
    assert avg_recall >= 0.95, f"Dual-read benchmark Recall@5 {avg_recall} < 0.95"


def test_tier3_08_f1_f2_f3_f7_f8_f9_full_lifecycle(mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore):
    """
    Combination: Full Knowledge Lifecycle (F1, F2, F3, F7, F8, F9)
    1. Agent proposes card via memory_candidate_create.
    2. Outbox worker claims job and writes embedding with CAS.
    3. Admin approves candidate via agent_memory_admin.
    4. pgvector stores indexed card and activates it.
    5. Agent queries brain.resolve(slim) and retrieves active card.
    """
    # 1. Proposal
    create_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "memory_candidate_create",
            "arguments": {
                "card_type": "decision",
                "project": "neurons",
                "title": "Full Lifecycle Architecture",
                "summary": "Unified pipeline verification",
                "typed_payload": {"decision": "Rationalize architecture"},
                "content_hash": "sha256:lifecycle_1",
            },
        },
    }
    create_res = mcp_server.handle_public_request(create_req)
    card_id = create_res["result"]["memory_id"]

    # 2. Outbox Embedding with CAS
    claimed = pg_store.claim_outbox_jobs("embed_worker", batch_size=1)
    assert len(claimed) == 1
    vec = make_dummy_vector(999)
    cas_ok = pg_store.cas_update_embedding(claimed[0].outbox_id, card_id, "sha256:lifecycle_1", vec)
    assert cas_ok is True

    # 3. Admin Approval
    admin_req = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "memory_candidate_approve", "arguments": {"memory_id": card_id}},
    }
    app_res = mcp_server.handle_admin_request(admin_req, auth_token="lbrain_admin")
    assert app_res["result"]["status"] == "approved"

    # 4. Resolve via brain.resolve
    resolve_req = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "brain.resolve", "arguments": {"project": "neurons", "mode": "context"}},
    }
    resolve_res = mcp_server.handle_public_request(resolve_req)
    assert "result" in resolve_res
    decisions = resolve_res["result"]["decisions"]
    assert any(d["id"] == card_id for d in decisions)
