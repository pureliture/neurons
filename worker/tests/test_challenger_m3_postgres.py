"""Challenger Adversarial Test Suite for Milestone 3 (PostgreSQL Store, Outbox & DAG).

Tests deep boundary conditions, cyclic graphs, lease contention, CAS races,
dead letter limits, and GUC isolation under high concurrency and corrupted state.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import threading
import time
import pytest

from agent_knowledge.postgres_store.pgvector_store import (
    PgVectorStore,
    MemoryCard,
    MemoryEdge,
    SessionChunk,
    OutboxJob,
    make_dummy_vector,
    compute_cosine_similarity,
)
from agent_knowledge.postgres_store.outbox_worker import (
    OutboxWorker,
    generate_deterministic_embedding,
)


@pytest.fixture
def store():
    s = PgVectorStore(use_in_memory=True)
    s.execute_ddl()
    return s


def test_challenger_m3_ddl_and_constraints(store: PgVectorStore):
    # Test valid card creation
    card = MemoryCard(
        memory_id="card_c1",
        project="test_proj",
        card_type="decision",
        title="Title 1",
        summary="Summary 1",
        content_hash="sha256:1111111111111111111111111111111111111111111111111111111111111111",
    )
    store.insert_card(card)
    assert store.get_card("card_c1") is not None

    # Temporal inversion error
    bad_card = MemoryCard(
        memory_id="bad_temp",
        project="test_proj",
        card_type="decision",
        title="Bad",
        summary="Bad",
        content_hash="sha256:1111111111111111111111111111111111111111111111111111111111111111",
        valid_from=datetime(2026, 8, 1, tzinfo=timezone.utc),
        valid_to=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(ValueError, match="valid_to cannot be earlier"):
        store.insert_card(bad_card)

    # Vector dim mismatch
    bad_vec_card = MemoryCard(
        memory_id="bad_vec",
        project="test_proj",
        card_type="decision",
        title="Bad",
        summary="Bad",
        content_hash="sha256:1111111111111111111111111111111111111111111111111111111111111111",
        embedding=[0.1] * 768,
    )
    with pytest.raises(ValueError, match="Vector dimension mismatch"):
        store.insert_card(bad_vec_card)


def test_challenger_m3_dag_cycle_and_diamond_graph(store: PgVectorStore):
    # Setup Diamond + Cycle:
    # A -> B -> D
    # A -> C -> D
    # D -> A (cycle back to root!)
    # D -> E -> F -> G -> H -> I (deep chain > 5)
    for node_id in ["A", "B", "C", "D", "E", "F", "G", "H", "I"]:
        store.insert_card(MemoryCard(
            memory_id=node_id,
            project="test_proj",
            card_type="decision",
            title=f"Node {node_id}",
            summary=f"Summary {node_id}",
            content_hash=f"sha256:{node_id.lower() * 64}",
        ))

    # Edges
    edges = [
        ("A", "B", "derived_from"),
        ("A", "C", "derived_from"),
        ("B", "D", "supports"),
        ("C", "D", "supports"),
        ("D", "A", "supersedes"),  # Cycle!
        ("D", "E", "derived_from"),
        ("E", "F", "derived_from"),
        ("F", "G", "derived_from"),
        ("G", "H", "derived_from"),
        ("H", "I", "derived_from"),
    ]
    for src, dst, rel in edges:
        store.insert_edge(MemoryEdge(
            src_id=src,
            dst_id=dst,
            rel_type=rel,
            provenance_hash=f"sha256:edge_{src}_{dst}" + "0" * (64 - len(f"edge_{src}_{dst}")),
        ))

    # Traversal from A with max_depth=5
    traversal = store.traverse_provenance_dag(root_memory_id="A", max_depth=5)
    assert len(traversal) > 0

    # Ensure no infinite loop occurred
    depths = [item["depth"] for item in traversal]
    assert max(depths) <= 5

    # Check cycle prevention: D -> A is in queue, but D cannot visit A again because A is in visited path
    for item in traversal:
        assert item["depth"] <= 5


def test_challenger_m3_outbox_concurrency_and_cas_races(store: PgVectorStore):
    # Insert 10 cards with pending embeddings
    for i in range(10):
        card = MemoryCard(
            memory_id=f"concurrent_card_{i}",
            project="test_proj",
            card_type="decision",
            title=f"Concurrent {i}",
            summary=f"Summary {i}",
            content_hash=f"sha256:{i:064d}",
            embedding_state="pending",
        )
        store.upsert_card(card)

    # 3 concurrent workers processing the outbox simultaneously
    workers = [
        OutboxWorker(store, worker_id=f"worker_{w}", batch_size=4, lease_seconds=10)
        for w in range(3)
    ]

    results = [0, 0, 0]

    def worker_run(w_idx: int):
        results[w_idx] = workers[w_idx].run_once()

    threads = [threading.Thread(target=worker_run, args=(w,)) for w in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Sum of processed items across workers should equal total 10 jobs without double-processing
    assert sum(results) == 10
    for i in range(10):
        c = store.get_card(f"concurrent_card_{i}")
        assert c.embedding_state == "ready"
        assert c.embedding is not None
        assert len(c.embedding) == 1536


def test_challenger_m3_cas_stale_hash_protection(store: PgVectorStore):
    card = MemoryCard(
        memory_id="stale_race_card",
        project="test_proj",
        card_type="decision",
        title="Initial Title",
        summary="Initial Summary",
        content_hash="sha256:initial_hash_111111111111111111111111111111111111111111111111111111",
        embedding_state="pending",
    )
    store.upsert_card(card)

    worker = OutboxWorker(store, worker_id="worker_cas_race")
    jobs = store.claim_outbox_leases(worker_id="worker_cas_race", batch_size=1)
    assert len(jobs) == 1
    job = jobs[0]

    # Concurrently modify card content_hash
    card_mutated = store.get_card("stale_race_card")
    card_mutated.content_hash = "sha256:mutated_hash_222222222222222222222222222222222222222222222222222222"
    store.cards["stale_race_card"] = card_mutated

    # Worker now executes CAS update with old enqueued hash
    success = worker.process_job(job)
    assert success is False, "CAS should fail because content_hash was mutated concurrently"

    # Verify card embedding was NOT updated to stale vector
    reloaded_card = store.get_card("stale_race_card")
    assert reloaded_card.embedding_state == "pending"
    assert reloaded_card.embedding is None


def test_challenger_m3_lease_timeout_and_recovery(store: PgVectorStore):
    card = MemoryCard(
        memory_id="lease_timeout_card",
        project="test_proj",
        card_type="decision",
        title="Lease Timeout Test",
        summary="Lease Timeout Summary",
        content_hash="sha256:lease_timeout_hash_111111111111111111111111111111111111111111111111",
        embedding_state="pending",
    )
    store.upsert_card(card)

    # Worker 1 claims job with 1 second lease
    jobs = store.claim_outbox_leases(worker_id="crashed_worker", batch_size=1, lease_seconds=1)
    assert len(jobs) == 1
    job = jobs[0]
    assert store.outbox[job.outbox_id].status == "processing"

    # Manually expire lease
    store.outbox[job.outbox_id].lease_until = datetime.now(timezone.utc) - timedelta(seconds=10)

    # Worker 2 claims leases -> should reclaim expired job
    worker2 = OutboxWorker(store, worker_id="recovery_worker")
    jobs2 = store.claim_outbox_leases(worker_id="recovery_worker", batch_size=1)
    assert len(jobs2) == 1
    assert jobs2[0].outbox_id == job.outbox_id
    assert jobs2[0].worker_id == "recovery_worker"

    # Worker 2 successfully completes it
    worker2.process_job(jobs2[0])
    assert store.outbox[job.outbox_id].status == "completed"
    assert store.get_card("lease_timeout_card").embedding_state == "ready"


def test_challenger_m3_dead_letter_escalation(store: PgVectorStore):
    def failing_embed_fn(text: str) -> list[float]:
        raise RuntimeError("Embedding model API quota exceeded")

    card = MemoryCard(
        memory_id="failing_card",
        project="test_proj",
        card_type="decision",
        title="Failing Test",
        summary="Failing Summary",
        content_hash="sha256:failing_card_hash_11111111111111111111111111111111111111111111111111",
        embedding_state="pending",
    )
    store.upsert_card(card)

    worker = OutboxWorker(store, worker_id="flaky_worker", embedding_fn=failing_embed_fn, max_retries=3)

    # Run retries until dead_letter
    for _ in range(3):
        # Reset lease_until to past so retry can claim immediately
        for j in store.outbox.values():
            j.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        worker.run_once()

    outbox_job = list(store.outbox.values())[0]
    assert outbox_job.status == "dead_letter"
    assert outbox_job.retry_count == 3
    assert "Embedding model API quota exceeded" in str(outbox_job.last_error)
    assert store.get_card("failing_card").embedding_state == "failed"
