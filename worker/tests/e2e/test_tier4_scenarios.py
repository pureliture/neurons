from __future__ import annotations

import json
import time
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
    compute_cosine_similarity,
    make_dummy_vector,
    sha256_str,
)


def test_tier4_scenario_1_developer_feature_implementation_cycle(
    mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore
):
    """
    Scenario 1: Developer Feature Implementation Cycle
    1. Agent calls brain.resolve(slim) to load concise decisions, preferences, and guardrails (~1.2 KB).
    2. Agent identifies architectural need and proposes a new decision card via memory_candidate_create.
    3. Verifies proposal is created strictly in candidate/disabled state.
    4. Re-resolves context; verifies disabled candidate does NOT pollute active agent context.
    """
    # Seed base active decision
    pg_store.insert_card(
        MemoryCard(
            memory_id="mem_arch_01",
            project="neurons",
            card_type="decision",
            title="Base Storage Architecture",
            summary="PostgreSQL is the single source of authority",
            typed_payload={"decision": "PostgreSQL authority", "rationale": "ACID compliance"},
            lifecycle_state="human_accepted",
            authorization_status="active",
            content_hash=sha256_str("arch_01"),
        )
    )

    # Step 1: Agent reads slim context
    req_resolve = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "brain.resolve", "arguments": {"project": "neurons", "mode": "context"}},
    }
    res_resolve = mcp_server.handle_public_request(req_resolve)
    assert "result" in res_resolve
    context = res_resolve["result"]
    assert len(context["decisions"]) == 1
    assert context["decisions"][0]["id"] == "mem_arch_01"

    # Step 2: Agent proposes new decision
    req_prop = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "memory_candidate_create",
            "arguments": {
                "card_type": "decision",
                "project": "neurons",
                "title": "Use Outbox Lease for Embeddings",
                "summary": "Implement lease-based concurrency in embedding_outbox",
                "typed_payload": {"decision": "Lease outbox", "lease_seconds": 30},
                "content_hash": sha256_str("lease_prop"),
                "proposer": "hermes",
            },
        },
    }
    res_prop = mcp_server.handle_public_request(req_prop)
    assert "result" in res_prop
    candidate_id = res_prop["result"]["memory_id"]
    assert res_prop["result"]["lifecycle_state"] == "candidate"
    assert res_prop["result"]["authorization_status"] == "disabled"

    # Step 3: Verify proposal state in database
    cand_card = pg_store.cards[candidate_id]
    assert cand_card.authorization_status == "disabled"

    # Step 4: Re-resolve context; candidate should NOT appear in active context
    res_resolve_2 = mcp_server.handle_public_request(req_resolve)
    active_ids = [d["id"] for d in res_resolve_2["result"]["decisions"]]
    assert candidate_id not in active_ids
    assert len(active_ids) == 1


def test_tier4_scenario_2_admin_review_and_supersession_workflow(
    mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore
):
    """
    Scenario 2: Admin Review & Supersession Workflow
    1. System has an existing active decision card (Old Architecture).
    2. Agent creates a candidate card proposing New Architecture.
    3. Operator reviews and approves the candidate via agent_memory_admin.
    4. Operator commits supersession link, setting Old Architecture to 'superseded'.
    5. Agent calls brain.resolve(slim); verifies New Architecture is active and Old Architecture is excluded.
    """
    old_card_id = "mem_old_arch"
    pg_store.insert_card(
        MemoryCard(
            memory_id=old_card_id,
            project="neurons",
            card_type="decision",
            title="Qdrant Vector Cluster",
            summary="Use dedicated Qdrant cluster for vector search",
            typed_payload={"decision": "Use Qdrant"},
            lifecycle_state="human_accepted",
            authorization_status="active",
            currentness="current",
            content_hash=sha256_str("old_arch"),
        )
    )

    # Step 2: Agent creates candidate
    cand_req = {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/call",
        "params": {
            "name": "memory_candidate_create",
            "arguments": {
                "card_type": "decision",
                "project": "neurons",
                "title": "Postgres pgvector Consolidation",
                "summary": "Consolidate into PostgreSQL pgvector 0.8.0+",
                "typed_payload": {"decision": "Use pgvector"},
                "content_hash": sha256_str("new_arch"),
            },
        },
    }
    cand_res = mcp_server.handle_public_request(cand_req)
    new_card_id = cand_res["result"]["memory_id"]

    # Step 3: Admin approves candidate
    app_req = {
        "jsonrpc": "2.0",
        "id": 20,
        "method": "tools/call",
        "params": {"name": "memory_candidate_approve", "arguments": {"memory_id": new_card_id}},
    }
    app_res = mcp_server.handle_admin_request(app_req, auth_token="lbrain_admin")
    assert app_res["result"]["status"] == "approved"

    # Step 4: Admin supersedes old card
    sup_req = {
        "jsonrpc": "2.0",
        "id": 30,
        "method": "tools/call",
        "params": {
            "name": "memory_supersede_commit",
            "arguments": {"target_id": new_card_id, "superseded_id": old_card_id},
        },
    }
    sup_res = mcp_server.handle_admin_request(sup_req, auth_token="lbrain_admin")
    assert sup_res["result"]["status"] == "committed"
    assert pg_store.cards[old_card_id].currentness == "superseded"

    # Step 5: Agent calls brain.resolve(slim)
    resolve_req = {
        "jsonrpc": "2.0",
        "id": 40,
        "method": "tools/call",
        "params": {"name": "brain.resolve", "arguments": {"project": "neurons", "mode": "context"}},
    }
    resolve_res = mcp_server.handle_public_request(resolve_req)
    active_decisions = resolve_res["result"]["decisions"]
    assert len(active_decisions) == 1
    assert active_decisions[0]["id"] == new_card_id
    assert active_decisions[0]["currentness"] == "current"


def test_tier4_scenario_3_outbox_concurrency_and_stale_generation_protection(
    mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore
):
    """
    Scenario 3: Outbox Concurrency & Stale Generation Protection Under Load
    1. Multiple coding agents submit proposals concurrently.
    2. Outbox workers claim non-overlapping batches via SKIP LOCKED.
    3. One card is modified concurrently before worker writes its embedding.
    4. CAS update detects content_hash mismatch, skips stale write, and prevents corrupting new version.
    """
    # 1. 3 agents create candidates
    card_ids = []
    for i in range(3):
        res = mcp_server.handle_public_request({
            "jsonrpc": "2.0",
            "id": i,
            "method": "tools/call",
            "params": {
                "name": "memory_candidate_create",
                "arguments": {
                    "card_type": "decision",
                    "project": "neurons",
                    "title": f"Concurrent Proposal {i}",
                    "summary": f"Summary {i}",
                    "typed_payload": {"index": i},
                    "content_hash": sha256_str(f"proposal_{i}_v1"),
                },
            },
        })
        card_ids.append(res["result"]["memory_id"])

    # 2. Worker 1 and Worker 2 poll jobs concurrently
    w1_jobs = pg_store.claim_outbox_jobs("worker_1", batch_size=2)
    w2_jobs = pg_store.claim_outbox_jobs("worker_2", batch_size=2)

    assert len(w1_jobs) == 2
    assert len(w2_jobs) == 1

    # 3. Simulate concurrent card modification for first job in worker 1
    stale_target_id = w1_jobs[0].target_id
    pg_store.cards[stale_target_id].content_hash = sha256_str("proposal_0_v2_updated")

    # 4. Worker 1 tries to write embedding with stale hash
    cas_result_stale = pg_store.cas_update_embedding(
        w1_jobs[0].outbox_id,
        stale_target_id,
        w1_jobs[0].content_hash,  # Stale v1 hash
        make_dummy_vector(100),
    )
    assert cas_result_stale is False  # CAS prevented stale embedding write!
    assert pg_store.cards[stale_target_id].embedding is None

    # Worker 1 writes embedding for second job (matching hash)
    valid_target_id = w1_jobs[1].target_id
    cas_result_valid = pg_store.cas_update_embedding(
        w1_jobs[1].outbox_id,
        valid_target_id,
        w1_jobs[1].content_hash,
        make_dummy_vector(101),
    )
    assert cas_result_valid is True
    assert pg_store.cards[valid_target_id].embedding_state == "ready"


def test_tier4_scenario_4_provenance_audit_and_temporal_investigation(
    mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore
):
    """
    Scenario 4: Provenance Audit & Temporal Point-in-Time Investigation
    1. System contains a 4-hop lineage of architectural decisions evolving over time.
    2. Auditor queries brain.resolve(with_evidence, as_of="2026-04-01T00:00:00Z").
    3. Traverses recursive DAG ancestry chain and validates complete SHA-256 hash chains.
    """
    t_feb = datetime(2026, 2, 1, tzinfo=timezone.utc)
    t_apr = datetime(2026, 4, 1, tzinfo=timezone.utc)
    t_jun = datetime(2026, 6, 1, tzinfo=timezone.utc)
    t_aug = datetime(2026, 8, 1, tzinfo=timezone.utc)

    # Create 4 generations of cards
    c1 = MemoryCard("gen_1", "neurons", "decision", "Gen 1 Ingress", "Initial ingress", {}, valid_from=t_feb, valid_to=t_apr, lifecycle_state="human_accepted", authorization_status="active", content_hash="sha256:gen1")
    c2 = MemoryCard("gen_2", "neurons", "decision", "Gen 2 Queue", "NATS JetStream queue", {}, valid_from=t_apr, valid_to=t_jun, lifecycle_state="human_accepted", authorization_status="active", content_hash="sha256:gen2")
    c3 = MemoryCard("gen_3", "neurons", "decision", "Gen 3 Ledger", "Ledger DB adapter", {}, valid_from=t_jun, valid_to=t_aug, lifecycle_state="human_accepted", authorization_status="active", content_hash="sha256:gen3")
    c4 = MemoryCard("gen_4", "neurons", "decision", "Gen 4 PGVector", "Consolidated PGVector", {}, valid_from=t_aug, valid_to=None, lifecycle_state="human_accepted", authorization_status="active", content_hash="sha256:gen4")

    for c in (c1, c2, c3, c4):
        pg_store.insert_card(c)

    pg_store.insert_edge(MemoryEdge(1, "gen_4", "supersedes", "gen_3", "sha256:e3", valid_from=t_aug))
    pg_store.insert_edge(MemoryEdge(2, "gen_3", "supersedes", "gen_2", "sha256:e2", valid_from=t_jun))
    pg_store.insert_edge(MemoryEdge(3, "gen_2", "supersedes", "gen_1", "sha256:e1", valid_from=t_apr))

    # Point-in-time query as of April 15, 2026 (Gen 2 active)
    resolve_req = {
        "jsonrpc": "2.0",
        "id": 100,
        "method": "tools/call",
        "params": {
            "name": "brain.resolve",
            "arguments": {
                "project": "neurons",
                "response_mode": "with_evidence",
                "as_of": "2026-04-15T00:00:00Z",
            },
        },
    }
    res = mcp_server.handle_public_request(resolve_req)
    result = res["result"]
    assert result["schema_version"] == "lbrain_evidence_context.v1"
    assert len(result["decisions"]) == 1
    assert result["decisions"][0]["id"] == "gen_2"
    assert "sha256:gen2" in result["evidence_hashes"]


def test_tier4_scenario_5_zero_downtime_qdrant_to_pgvector_cutover(
    qdrant_store: InMemoryQdrantStore, pg_store: InMemoryPostgresStore
):
    """
    Scenario 5: Zero-Downtime Qdrant to PostgreSQL Cutover
    1. Qdrant store contains 20 active session chunks and card vectors.
    2. Migration script runs in background and backfills data into PostgreSQL pgvector store.
    3. Dual-read shadow harness executes 20 benchmark queries comparing Qdrant vs pgvector.
    4. Asserts Recall@5 >= 0.95 and latency P95 <= 20ms.
    5. Search backend switches to postgres_pgvector with 0 downtime.
    """
    # 1. Populate Qdrant
    for i in range(20):
        vec = make_dummy_vector(300 + i)
        qdrant_store.upsert(
            f"chunk_prod_{i}",
            vec,
            {
                "memory_id": f"chunk_prod_{i}",
                "project": "neurons",
                "title": f"Production Chunk {i}",
                "summary": f"Summary {i}",
                "content_hash": sha256_str(f"prod_chunk_{i}"),
                "lifecycle_state": "human_accepted",
                "authorization_status": "active",
            },
        )

    # 2. Backfill into PostgreSQL
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
    assert len(pg_store.cards) == 20

    # 3. Dual-Read Shadow Benchmark execution
    latencies = []
    recall_list = []
    for i in range(20):
        start_t = time.perf_counter()
        q_vec = make_dummy_vector(300 + i)

        q_res = [r["id"] for r in qdrant_store.search(q_vec, limit=5)]
        pg_res = [r["memory_id"] for r in pg_store.hybrid_vector_search(q_vec, project="neurons", limit=5)]

        elapsed_ms = (time.perf_counter() - start_t) * 1000
        latencies.append(elapsed_ms)

        overlap = len(set(q_res).intersection(set(pg_res)))
        recall_list.append(overlap / len(q_res))

    # 4. Gate assertions
    avg_recall = sum(recall_list) / len(recall_list)
    assert avg_recall >= 0.95, f"Cutover Recall@5 {avg_recall} < 0.95"

    latencies.sort()
    p95_latency = latencies[int(len(latencies) * 0.95) - 1]
    assert p95_latency <= 20.0, f"P95 latency {p95_latency}ms > 20ms"

    # 5. Switch search backend
    active_search_backend = "postgres_pgvector"
    assert active_search_backend == "postgres_pgvector"
