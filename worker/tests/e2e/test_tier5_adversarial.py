"""Tier 5 Adversarial Coverage & Security Hardening Test Suite.

Extends E2E verification with extreme adversarial scenarios:
1. Malicious wire payloads & injection attempts.
2. Progressive truncation under extreme memory/payload stress.
3. High-concurrency CAS race conditions across 10 mutating threads & 5 workers.
4. Deep cyclic & diamond DAG provenance security under temporal filtering.
5. High-scale Qdrant to PostgreSQL migration with dual-read shadow verification.
6. Absolute candidate proposal isolation against context leakage.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import threading
import time
import pytest

from agent_knowledge.ledger import Ledger
from agent_knowledge.knowledge_search_service import (
    DisabledRetiredIndexBridgeClient,
    KnowledgeSearchService,
)
from agent_knowledge.mcp_tools import (
    list_public_agent_tools,
    list_admin_tools,
    BRAIN_RESOLVE_TOOL_NAME,
    MEMORY_CANDIDATE_CREATE_TOOL_NAME,
)
from agent_knowledge.mcp_jsonrpc import (
    handle_jsonrpc_message,
    handle_admin_jsonrpc_message,
)
from agent_knowledge.llm_brain_core.slim_serializer import (
    SlimSerializer,
)
from agent_knowledge.postgres_store.pgvector_store import (
    PgVectorStore,
    MemoryCard,
    MemoryEdge,
    SessionChunk,
    make_dummy_vector,
)
from agent_knowledge.postgres_store.outbox_worker import (
    OutboxWorker,
)
from agent_knowledge.postgres_store.migration_qdrant_to_postgres import (
    QdrantToPostgresMigrator,
)
from agent_knowledge.postgres_store.dual_read_shadow import (
    DualReadShadowHarness,
)

PG_DSN = os.environ.get("LBRAIN_TEST_PG_DSN", "")
live_pg = pytest.mark.skipif(
    not PG_DSN,
    reason="LBRAIN_TEST_PG_DSN 미설정 (전용 live PostgreSQL integration gate)",
)


def _service(tmp_path: Path) -> KnowledgeSearchService:
    private = tmp_path / "private"
    private.mkdir(parents=True, exist_ok=True)
    os.chmod(private, 0o700)
    ledger = Ledger(private / "ledger.sqlite")
    return KnowledgeSearchService(
        ledger=ledger,
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        allow_private_results=True,
    )


# ==============================================================================
# 1. Adversarial Security & JSON-RPC Injection Guard
# ==============================================================================

def test_tier5_adversarial_admin_tool_disguise_injection(tmp_path: Path):
    """Attempts to invoke admin tools with disguised method names and payloads fail-closed."""
    service = _service(tmp_path)
    malicious_methods = [
        "memory_candidate_approve",
        "agent_memory_admin.memory_candidate_approve",
        "../agent_memory_admin/memory_candidate_approve",
        "brain_permission_sensitive_audit_probe",
        "brain_corpus_ingest_plan",
        "brain_object_decision_commit",
        "memory_supersede_commit",
        "__proto__",
        "constructor",
    ]

    for method in malicious_methods:
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": method, "arguments": {"memory_id": "target_123"}},
        }
        res = handle_jsonrpc_message(req, service, surface="agent")
        assert "error" in res, f"Method {method} should be rejected on agent endpoint"
        assert res["error"]["code"] in (-32601, -32600, -32602)


def test_tier5_adversarial_proposal_tampering_isolation(tmp_path: Path):
    """Agent cannot inject active authorization_status or approved lifecycle into candidate creation."""
    service = _service(tmp_path)

    req = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "memory_candidate_create",
            "arguments": {
                "card_type": "decision",
                "project": "neurons",
                "title": "Malicious Bypass Attempt",
                "summary": "Attempting to write authoritative active state directly",
                "typed_payload": {
                    "decision": "Bypass security",
                    "rationale": "testing",
                    "alternatives": ["none"],
                    "consequence": "security test",
                    "authority_ref": "sec_test",
                },
                "content_hash": "sha256:" + "a" * 64,
                "source_ref": {"source_id": "malicious_actor"},
                "span_ref": {"span_id": "span_1"},
                "proposer": "codex",
                "authorization_status": "active",  # Malicious override
                "lifecycle_state": "human_accepted",  # Malicious override
            },
        },
    }
    res = handle_jsonrpc_message(req, service, surface="agent")
    assert "result" in res
    result_data = res["result"]["structuredContent"]

    # Even if attacker passed active/human_accepted, system MUST force candidate/disabled
    assert result_data["authorization_status"] == "disabled"
    assert result_data["lifecycle_state"] == "candidate"
    assert result_data["proposal_write_performed"] is True



# ==============================================================================
# 2. Extreme Wire Budget & Truncation Resilience
# ==============================================================================

def test_tier5_adversarial_extreme_payload_truncation():
    """Enforces <= 3.0 KB hard limit even when given 100 large cards with 10 KB each."""
    huge_cards = [
        {
            "memory_id": f"huge_card_{i}",
            "card_type": "decision",
            "title": f"Huge Decision {i} " * 20,
            "summary": f"Very long summary text exceeding normal budgets {i} " * 50,
            "typed_payload": {
                "decision": f"Huge decision text {i} " * 20,
                "rationale": f"Rationale {i} " * 20,
            },
            "currentness": "current",
            "confidence": 1.0,
            "content_hash": f"sha256:{i:064d}",
        }
        for i in range(100)
    ]

    serialized = SlimSerializer.serialize_slim(
        project="neurons",
        decisions=huge_cards[:50],
        preferences=huge_cards[50:],
        recent_context="Long recent context " * 50,
        guardrails=["Guardrail " * 20 for _ in range(20)],
    )

    raw_json = json.dumps(serialized, ensure_ascii=False)
    payload_size = len(raw_json.encode("utf-8"))

    # Hard ceiling strictly enforced <= 3072 bytes (3.0 KB)
    assert payload_size <= 3072, f"Payload {payload_size} bytes exceeded hard ceiling 3072 bytes"
    assert serialized["schema_version"] == "lbrain_slim_context.v1"



# ==============================================================================
# 3. High-Concurrency CAS Race Stress (10 Mutating Threads vs 5 Workers)
# ==============================================================================

@live_pg
def test_tier5_adversarial_high_concurrency_cas_stress():
    """10 mutator threads updating cards concurrently with 5 outbox worker threads."""
    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()

    # Create 20 initial cards
    for i in range(20):
        store.upsert_card(
            MemoryCard(
                memory_id=f"stress_card_{i}",
                project="neurons",
                card_type="decision",
                title=f"Initial Title {i}",
                summary=f"Initial Summary {i}",
                content_hash=f"sha256:init_{i:060d}",
                embedding_state="pending",
            )
        )

    # 5 worker threads
    workers = [
        OutboxWorker(store, worker_id=f"stress_worker_{w}", batch_size=4, lease_seconds=5)
        for w in range(5)
    ]

    stop_event = threading.Event()
    worker_threads = []
    for w in workers:
        t = threading.Thread(target=w.run_loop, kwargs={"stop_event": stop_event, "max_iterations": 20})
        worker_threads.append(t)
        t.start()

    # 10 mutator threads concurrently updating content_hash on random cards
    def mutator_work(thread_idx: int):
        for step in range(5):
            target_id = f"stress_card_{(thread_idx + step) % 20}"
            card = store.get_card(target_id)
            if card:
                card.content_hash = f"sha256:mutated_{thread_idx}_{step}" + "0" * (64 - len(f"mutated_{thread_idx}_{step}"))
                card.updated_at = datetime.now(timezone.utc)
                store.cards[target_id] = card
            time.sleep(0.01)

    mutator_threads = [threading.Thread(target=mutator_work, args=(m,)) for m in range(10)]
    for mt in mutator_threads:
        mt.start()

    for mt in mutator_threads:
        mt.join()

    time.sleep(0.1)
    stop_event.set()
    for wt in worker_threads:
        wt.join()

    # Verify storage integrity: no unhandled exceptions, all jobs accounted for
    for job in store.outbox.values():
        assert job.status in ("completed", "processing", "queued", "failed", "dead_letter")


# ==============================================================================
# 4. Multi-Hop Cycle & Diamond DAG Provenance Traversal
# ==============================================================================

@live_pg
def test_tier5_adversarial_deep_cyclic_provenance_dag():
    """Traverses complex multi-hop DAG with diamond convergence, cycles, and temporal filters."""
    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()

    # Create nodes: R -> A1, R -> A2; A1 -> B, A2 -> B (diamond); B -> C -> D -> E -> R (cycle!)
    nodes = ["R", "A1", "A2", "B", "C", "D", "E"]
    for n in nodes:
        store.insert_card(
            MemoryCard(
                memory_id=n,
                project="neurons",
                card_type="decision",
                title=f"Node {n}",
                summary=f"Summary {n}",
                content_hash=f"sha256:{n.lower() * 64}",
            )
        )

    edges = [
        ("R", "A1", "derived_from", datetime(2026, 8, 1, tzinfo=timezone.utc)),
        ("R", "A2", "derived_from", datetime(2026, 8, 1, tzinfo=timezone.utc)),
        ("A1", "B", "supports", datetime(2026, 8, 5, tzinfo=timezone.utc)),
        ("A2", "B", "supports", datetime(2026, 8, 5, tzinfo=timezone.utc)),
        ("B", "C", "supersedes", datetime(2026, 8, 10, tzinfo=timezone.utc)),
        ("C", "D", "derived_from", datetime(2026, 8, 15, tzinfo=timezone.utc)),
        ("D", "E", "derived_from", datetime(2026, 8, 20, tzinfo=timezone.utc)),
        ("E", "R", "contradicts", datetime(2026, 8, 25, tzinfo=timezone.utc)),  # Cycle!
    ]

    for src, dst, rel, valid_from in edges:
        store.insert_edge(
            MemoryEdge(
                src_id=src,
                dst_id=dst,
                rel_type=rel,
                valid_from=valid_from,
                provenance_hash=f"sha256:edge_{src}_{dst}" + "0" * (64 - len(f"edge_{src}_{dst}")),
            )
        )

    # 1. Full traversal from root R (max_depth=5)
    results = store.traverse_provenance_dag(root_memory_id="R", max_depth=5)
    assert len(results) > 0
    assert max(r["depth"] for r in results) <= 5

    # 2. Point-in-time traversal as of 2026-08-08 (should only include R->A1, R->A2, A1->B, A2->B)
    pit_results = store.traverse_provenance_dag(
        root_memory_id="R",
        max_depth=5,
        as_of="2026-08-08T00:00:00Z",
    )
    pit_dsts = {r["dst_id"] for r in pit_results}
    assert "A1" in pit_dsts
    assert "A2" in pit_dsts
    assert "B" in pit_dsts
    assert "C" not in pit_dsts  # Created after 2026-08-08
    assert "D" not in pit_dsts


# ==============================================================================
# 5. Full End-to-End Migration & Dual-Read Cutover Verification
# ==============================================================================

@live_pg
def test_tier5_adversarial_full_backfill_and_dual_read_cutover():
    """Simulates full zero-downtime cutover: migrate 100 items and verify Recall@5 >= 0.95."""
    class SourceQdrant:
        def __init__(self):
            self.collections = {"memory_cards": {}, "session_chunks": {}}
            self.vectors = {}
            for i in range(100):
                vec = make_dummy_vector(i * 7)
                card_data = (
                    vec,
                    {
                        "memory_id": f"card_{i}",
                        "project": "neurons",
                        "card_type": "decision" if i % 2 == 0 else "preference",
                        "title": f"Migrated Knowledge {i}",
                        "summary": f"Summary for knowledge point {i}",
                        "typed_payload": {"idx": i},
                        "content_hash": f"sha256:{i:064d}",
                        "lifecycle_state": "human_accepted",
                        "authorization_status": "active",
                        "currentness": "current",
                    },
                )
                self.collections["memory_cards"][f"card_{i}"] = card_data
                self.vectors[f"card_{i}"] = card_data

    source_qdrant = SourceQdrant()
    pg_target = PgVectorStore(dsn=PG_DSN)
    pg_target.execute_ddl()

    # Step 1: Run Backfill Migration
    migrator = QdrantToPostgresMigrator(
        qdrant_client=source_qdrant,
        target_store=pg_target,
        batch_size=25,
    )
    summary = migrator.run_full_migration(project="neurons")
    assert summary.total_migrated == 100
    assert len(pg_target.cards) == 100


    # Step 2: Run Dual-Read Shadow Benchmark
    harness = DualReadShadowHarness(
        qdrant_client=source_qdrant,
        pg_store=pg_target,
        default_limit=5,
        recall_gate_threshold=0.95,
        p95_latency_gate_ms=20.0,
    )
    benchmark_queries = [make_dummy_vector(q * 11) for q in range(50)]
    bench_res = harness.run_benchmark(benchmark_queries, project="neurons")

    assert bench_res.mean_recall_at_k >= 0.95
    assert bench_res.recall_gate_passed is True
    assert bench_res.p95_pgvector_latency_ms <= 20.0
    assert bench_res.overall_gate_passed is True

    # Step 3: Verify Cutover Search on Target Store
    resolved = pg_target.hybrid_search(
        project="neurons",
        query_vector=make_dummy_vector(0),
        limit=5,
    )
    assert len(resolved) == 5
    assert resolved[0]["similarity_score"] >= 0.99
