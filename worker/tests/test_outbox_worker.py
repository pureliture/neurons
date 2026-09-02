"""Unit and integration test suite for OutboxWorker (Milestone 3).

Verifies polling loop, lease claiming, external embedding generation,
CAS write-back with graceful discard on stale updates, and retry escalation.
"""

from __future__ import annotations

import threading
import time
import pytest

from agent_knowledge.postgres_store.pgvector_store import (
    MemoryCard,
    SessionChunk,
    PgVectorStore,
    make_dummy_vector,
)
from agent_knowledge.postgres_store.outbox_worker import (
    OutboxWorker,
    generate_deterministic_embedding,
)


@pytest.fixture
def store() -> PgVectorStore:
    s = PgVectorStore(use_in_memory=True)
    s.execute_ddl()
    return s


def test_outbox_worker_single_job_lifecycle(store: PgVectorStore):
    """Worker claims job, generates 1536-dim embedding, and performs successful CAS write-back."""
    card = MemoryCard(
        memory_id="card_worker_1",
        project="neurons",
        card_type="decision",
        title="Worker Test",
        summary="Test summary for embedding generation",
        content_hash="sha256:w1_hash",
        embedding_state="pending",
    )
    store.upsert_card(card)
    assert len(store.outbox) == 1

    worker = OutboxWorker(store=store, worker_id="worker_test_1", batch_size=5)
    processed = worker.run_once()

    assert processed == 1
    updated_card = store.get_card("card_worker_1")
    assert updated_card.embedding_state == "ready"
    assert updated_card.embedding is not None
    assert len(updated_card.embedding) == 1536
    assert updated_card.embedding_revision == 2

    job = list(store.outbox.values())[0]
    assert job.status == "completed"
    assert job.last_error is None


def test_outbox_worker_session_chunk_processing(store: PgVectorStore):
    """Worker processes session_chunk target types correctly."""
    chunk = SessionChunk(
        chunk_id="chk_1",
        session_id_hash="sha256:sess1",
        project="neurons",
        content_markdown="Conversation chunk content",
        embedding_model="text-embedding-3-small",
    )
    store.insert_chunk(chunk)
    job_id = store.enqueue_outbox(
        target_type="session_chunk",
        target_id="chk_1",
        content_hash="sha256:chk1",
        payload_text="Conversation chunk content",
    )

    worker = OutboxWorker(store=store, worker_id="worker_chunk")
    processed = worker.run_once()

    assert processed == 1
    updated_chunk = store.get_chunk("chk_1")
    assert updated_chunk.embedding is not None
    assert len(updated_chunk.embedding) == 1536
    assert store.outbox[job_id].status == "completed"


def test_outbox_worker_concurrent_workers_no_overlap(store: PgVectorStore):
    """Multiple workers polling simultaneously receive distinct non-overlapping jobs."""
    for i in range(10):
        card = MemoryCard(
            memory_id=f"card_multi_{i}",
            project="neurons",
            card_type="decision",
            title=f"Multi {i}",
            summary=f"Summary {i}",
            content_hash=f"sha256:hash_{i}",
            embedding_state="pending",
        )
        store.upsert_card(card)

    w1 = OutboxWorker(store=store, worker_id="worker_1", batch_size=3)
    w2 = OutboxWorker(store=store, worker_id="worker_2", batch_size=3)
    w3 = OutboxWorker(store=store, worker_id="worker_3", batch_size=4)

    c1 = w1.run_once()
    c2 = w2.run_once()
    c3 = w3.run_once()

    assert c1 == 3
    assert c2 == 3
    assert c3 == 4

    # Verify all 10 cards processed successfully
    for i in range(10):
        c = store.get_card(f"card_multi_{i}")
        assert c.embedding_state == "ready"


def test_outbox_worker_cas_stale_update_interleaving(store: PgVectorStore):
    """
    Simulates race condition:
    1. Card C1 created (content_hash = H1) & Outbox J1 enqueued.
    2. Card C1 updated concurrently to C2 (content_hash = H2) & Outbox J2 enqueued.
    3. Worker processes J1 (with H1) -> CAS detects mismatch, discards stale vector.
    4. Worker processes J2 (with H2) -> CAS matches, writes correct vector V2.
    """
    card = MemoryCard(
        memory_id="card_race",
        project="neurons",
        card_type="decision",
        title="Original",
        summary="Original Text",
        content_hash="sha256:hash_v1",
        embedding_state="pending",
    )
    store.insert_card(card)
    job1_id = store.enqueue_outbox("memory_card", "card_race", "sha256:hash_v1", "Original Text")

    # Card updated before worker starts
    store.cards["card_race"].title = "Updated"
    store.cards["card_race"].summary = "Updated Text"
    store.cards["card_race"].content_hash = "sha256:hash_v2"

    job2_id = store.enqueue_outbox("memory_card", "card_race", "sha256:hash_v2", "Updated Text")

    # Custom embedding fn to distinguish versions
    def custom_embed(text: str) -> list[float]:
        if "Original" in text:
            return make_dummy_vector(1)
        return make_dummy_vector(2)

    worker = OutboxWorker(store=store, worker_id="worker_race", embedding_fn=custom_embed, batch_size=1)

    # Process Job 1 (stale)
    w1_processed = worker.run_once()
    assert w1_processed == 1
    assert store.outbox[job1_id].status == "completed"
    assert "CAS skip" in store.outbox[job1_id].last_error
    # Card should NOT have vector 1
    assert store.cards["card_race"].embedding is None

    # Process Job 2 (latest)
    w2_processed = worker.run_once()
    assert w2_processed == 1
    assert store.outbox[job2_id].status == "completed"
    assert store.outbox[job2_id].last_error is None

    final_card = store.get_card("card_race")
    assert final_card.embedding_state == "ready"
    assert final_card.embedding == make_dummy_vector(2)


def test_outbox_worker_crash_and_lease_expiry_reclaim(store: PgVectorStore):
    """Worker crash with expired lease is reclaimed and processed by another worker."""
    card = MemoryCard(
        memory_id="card_crash_rec",
        project="neurons",
        card_type="decision",
        title="Crash Test",
        summary="Crash summary",
        content_hash="sha256:crash_hash",
        embedding_state="pending",
    )
    store.upsert_card(card)

    # Crashed worker claimed with lease in past
    store.claim_outbox_leases(worker_id="crashed_worker", batch_size=1, lease_seconds=-10)
    job_id = list(store.outbox.keys())[0]
    assert store.outbox[job_id].worker_id == "crashed_worker"

    # Recovery worker polls and reclaims
    recovery_worker = OutboxWorker(store=store, worker_id="recovery_worker")
    processed = recovery_worker.run_once()

    assert processed == 1
    assert store.outbox[job_id].worker_id == "recovery_worker"
    assert store.outbox[job_id].status == "completed"
    assert store.get_card("card_crash_rec").embedding_state == "ready"


def test_outbox_worker_transient_error_retry_and_dead_letter(store: PgVectorStore):
    """Failing embedding generation retries up to max_retries then marks dead_letter."""
    card = MemoryCard(
        memory_id="card_fatal",
        project="neurons",
        card_type="decision",
        title="Fatal Card",
        summary="Fatal summary",
        content_hash="sha256:fatal_hash",
        embedding_state="pending",
    )
    store.upsert_card(card)

    # Embedding function that raises transient network exceptions
    def failing_embed(text: str) -> list[float]:
        raise ConnectionResetError("Remote embedding API timeout")

    worker = OutboxWorker(store=store, worker_id="w_fail", embedding_fn=failing_embed, max_retries=5)

    # Process 5 times (simulating retries after lease expiries)
    for i in range(1, 6):
        job = list(store.outbox.values())[0]
        # Force lease expiry to allow retry
        job.lease_until = None
        job.status = "failed" if i > 1 else "queued"
        worker.run_once()

    job = list(store.outbox.values())[0]
    assert job.status == "dead_letter"
    assert job.retry_count >= 5
    assert store.get_card("card_fatal").embedding_state == "failed"


def test_outbox_worker_empty_payload_safety(store: PgVectorStore):
    """Empty payload text generates valid 1536-dim vector without error."""
    card = MemoryCard(
        memory_id="card_empty",
        project="neurons",
        card_type="decision",
        title="",
        summary="",
        content_hash="sha256:empty_hash",
        embedding_state="pending",
    )
    store.upsert_card(card)

    worker = OutboxWorker(store=store, worker_id="w_empty")
    processed = worker.run_once()

    assert processed == 1
    updated_card = store.get_card("card_empty")
    assert updated_card.embedding_state == "ready"
    assert len(updated_card.embedding) == 1536


def test_outbox_worker_run_loop_bounded(store: PgVectorStore):
    """Worker run_loop runs bounded iterations cleanly."""
    card = MemoryCard(
        memory_id="card_loop",
        project="neurons",
        card_type="decision",
        title="Loop",
        summary="Loop summary",
        content_hash="sha256:loop_hash",
        embedding_state="pending",
    )
    store.upsert_card(card)

    worker = OutboxWorker(store=store, worker_id="w_loop", poll_interval_seconds=0.01)
    worker.run_loop(max_iterations=2)

    assert store.get_card("card_loop").embedding_state == "ready"
