"""DSN-gated live integration tests for OutboxWorker (M6a).

Live tests require ``LBRAIN_TEST_PG_DSN`` pointing at a disposable PostgreSQL
(pgvector >= 0.8.0). Without the DSN they skip; the static contract tests in
this file run without any database.

Shared embedding profile: 3072-dim (gemini-embedding-2 / halfvec(3072)).
``SHARED_EMBEDDING_DIM`` pins the expectation file-locally; the production
default is imported, never changed here.
"""

from __future__ import annotations

import os
import uuid

import pytest

from agent_knowledge.postgres_store.pgvector_store import (
    MemoryCard,
    OutboxJob,
    PgVectorStore,
    SessionChunk,
    VECTOR_DIMENSION,
    make_dummy_vector,
)
from agent_knowledge.postgres_store.outbox_worker import (
    OutboxWorker,
    generate_deterministic_embedding,
)

SHARED_EMBEDDING_DIM = 3072

PG_DSN = os.environ.get("LBRAIN_TEST_PG_DSN", "")
live_pg = pytest.mark.skipif(
    not PG_DSN,
    reason="LBRAIN_TEST_PG_DSN 미설정 (전용 live PostgreSQL integration gate)",
)

OFFLINE_DSN = "postgresql://invalid.example.invalid/never"


def _cleanup(store: PgVectorStore, tag: str) -> None:
    like = tag + "%"
    with store.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM embedding_outbox WHERE target_id LIKE %s", (like,))
            cur.execute(
                "DELETE FROM memory_edges WHERE src_id LIKE %s OR dst_id LIKE %s",
                (like, like),
            )
            cur.execute(
                "DELETE FROM session_memory_chunks WHERE chunk_id LIKE %s", (like,)
            )
            cur.execute(
                "DELETE FROM memory_cards WHERE memory_id LIKE %s", (like,)
            )


@pytest.fixture
def pg_store():
    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()
    tag = f"m6a_worker_{uuid.uuid4().hex[:10]}"
    yield store, tag
    _cleanup(store, tag)


def _expire_lease(store: PgVectorStore, outbox_id: int) -> None:
    with store.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE embedding_outbox SET lease_until = NOW() - INTERVAL '1 second'"
                " WHERE outbox_id = %s",
                (outbox_id,),
            )


def _pending_card(memory_id: str, project: str, content_hash: str, **kwargs) -> MemoryCard:
    return MemoryCard(
        memory_id=memory_id,
        project=project,
        card_type="decision",
        title=f"Title {memory_id}",
        summary=f"Summary {memory_id}",
        content_hash=content_hash,
        embedding_state="pending",
        **kwargs,
    )


def _worker(store: PgVectorStore, worker_id: str, **kwargs) -> OutboxWorker:
    kwargs.setdefault("embedding_fn", generate_deterministic_embedding)
    return OutboxWorker(store=store, worker_id=worker_id, **kwargs)


# ==============================================================================
# A. Static contract tests (no database)
# ==============================================================================


def test_shared_embedding_profile_is_3072():
    assert VECTOR_DIMENSION == SHARED_EMBEDDING_DIM


def test_worker_requires_explicit_embedding_provider_without_database():
    store = PgVectorStore(dsn=OFFLINE_DSN)
    with pytest.raises(ValueError, match="embedding_fn is required"):
        OutboxWorker(store=store, worker_id="w-no-provider")


def test_worker_rejects_misdimensioned_provider_output_without_database():
    store = PgVectorStore(dsn=OFFLINE_DSN)
    worker = OutboxWorker(
        store=store,
        worker_id="w-bad-dim",
        embedding_fn=lambda _text: [0.1] * 1536,
    )
    job = OutboxJob(
        outbox_id=1,
        target_type="memory_card",
        target_id="t",
        content_hash="sha256:h",
        payload_text="text",
    )
    with pytest.raises(ValueError, match="invalid vector length"):
        worker.process_job(job)


def test_deterministic_embedding_helper_uses_shared_profile():
    vec = generate_deterministic_embedding("hello")
    assert len(vec) == SHARED_EMBEDDING_DIM
    assert generate_deterministic_embedding("") == make_dummy_vector(0)
    assert generate_deterministic_embedding("a") != generate_deterministic_embedding("b")


# ==============================================================================
# B. Live tests (disposable PostgreSQL)
# ==============================================================================


@live_pg
def test_live_worker_single_job_lifecycle(pg_store):
    store, tag = pg_store
    memory_id = f"{tag}_card_1"
    store.upsert_card(_pending_card(memory_id, f"{tag}_proj", "sha256:w1_hash"))
    assert any(job.target_id == memory_id for job in store.list_outbox_jobs())

    processed = _worker(store, f"{tag}_w1", batch_size=5).run_once()

    assert processed == 1
    updated = store.get_card(memory_id)
    assert updated.embedding_state == "ready"
    assert updated.embedding is not None
    assert len(updated.embedding) == SHARED_EMBEDDING_DIM
    assert updated.embedding_revision == 2

    jobs = [job for job in store.list_outbox_jobs() if job.target_id == memory_id]
    assert len(jobs) == 1
    assert jobs[0].status == "completed"
    assert jobs[0].last_error is None


@live_pg
def test_live_worker_session_chunk_processing(pg_store):
    store, tag = pg_store
    chunk_id = f"{tag}_chk_1"
    store.insert_chunk(
        SessionChunk(
            chunk_id=chunk_id,
            session_id_hash="sha256:sess1",
            project=f"{tag}_proj",
            content_markdown="Conversation chunk content",
            content_hash="sha256:chk1",
        )
    )

    processed = _worker(store, f"{tag}_w_chunk").run_once()

    assert processed == 1
    updated = store.get_chunk(chunk_id)
    assert updated.embedding is not None
    assert len(updated.embedding) == SHARED_EMBEDDING_DIM
    jobs = [job for job in store.list_outbox_jobs() if job.target_id == chunk_id]
    assert len(jobs) == 1
    assert jobs[0].status == "completed"


@live_pg
def test_live_workers_receive_non_overlapping_jobs(pg_store):
    store, tag = pg_store
    project = f"{tag}_proj"
    for i in range(10):
        store.upsert_card(
            _pending_card(f"{tag}_multi_{i}", project, f"sha256:hash_{i}")
        )

    c1 = _worker(store, f"{tag}_w1", batch_size=3).run_once()
    c2 = _worker(store, f"{tag}_w2", batch_size=3).run_once()
    c3 = _worker(store, f"{tag}_w3", batch_size=4).run_once()

    assert (c1, c2, c3) == (3, 3, 4)
    for i in range(10):
        assert store.get_card(f"{tag}_multi_{i}").embedding_state == "ready"


@live_pg
def test_live_worker_stale_cas_interleaving(pg_store):
    """Stale job retires as cas_skipped; the newer job writes the vector."""
    store, tag = pg_store
    memory_id = f"{tag}_race"
    project = f"{tag}_proj"
    store.insert_card(_pending_card(memory_id, project, "sha256:hash_v1"))
    job1_id = store.enqueue_outbox(
        "memory_card", memory_id, "sha256:hash_v1", "Original Text"
    )

    updated = _pending_card(memory_id, project, "sha256:hash_v2")
    updated.title = "Updated"
    updated.summary = "Updated Text"
    store.upsert_card(updated)
    job2_id = store.enqueue_outbox(
        "memory_card", memory_id, "sha256:hash_v2", "Updated Text"
    )
    assert job1_id != job2_id

    def custom_embed(text: str) -> list[float]:
        if "Original" in text:
            return make_dummy_vector(1)
        return make_dummy_vector(2)

    worker = _worker(store, f"{tag}_w_race", embedding_fn=custom_embed, batch_size=1)

    assert worker.run_once() == 1
    assert store.get_outbox_job(job1_id).status == "cas_skipped"
    assert "CAS skip" in (store.get_outbox_job(job1_id).last_error or "")
    assert store.get_card(memory_id).embedding is None

    assert worker.run_once() == 1
    assert store.get_outbox_job(job2_id).status == "completed"
    assert store.get_outbox_job(job2_id).last_error is None

    final = store.get_card(memory_id)
    assert final.embedding_state == "ready"
    assert final.embedding == pytest.approx(make_dummy_vector(2), abs=1e-4)


@live_pg
def test_live_worker_crash_lease_reclaimed(pg_store):
    store, tag = pg_store
    memory_id = f"{tag}_crash"
    store.upsert_card(_pending_card(memory_id, f"{tag}_proj", "sha256:crash_hash"))

    claimed = store.claim_outbox_leases(f"{tag}_crashed", batch_size=1, lease_seconds=30)
    assert len(claimed) == 1
    job_id = claimed[0].outbox_id
    assert store.get_outbox_job(job_id).worker_id == f"{tag}_crashed"

    _expire_lease(store, job_id)

    recovery = _worker(store, f"{tag}_recovery")
    assert recovery.run_once() == 1
    job = store.get_outbox_job(job_id)
    assert job.worker_id == f"{tag}_recovery"
    assert job.status == "completed"
    assert store.get_card(memory_id).embedding_state == "ready"


@live_pg
def test_live_worker_transient_errors_escalate_to_dead_letter(pg_store):
    store, tag = pg_store
    memory_id = f"{tag}_fatal"
    store.upsert_card(_pending_card(memory_id, f"{tag}_proj", "sha256:fatal_hash"))

    def failing_embed(_text: str) -> list[float]:
        raise ConnectionResetError("Remote embedding API timeout")

    worker = _worker(store, f"{tag}_w_fail", embedding_fn=failing_embed, max_retries=5)

    for _ in range(5):
        jobs = [job for job in store.list_outbox_jobs() if job.target_id == memory_id]
        _expire_lease(store, jobs[0].outbox_id)
        worker.run_once()

    job = [job for job in store.list_outbox_jobs() if job.target_id == memory_id][0]
    assert job.status == "dead_letter"
    assert job.retry_count >= 5
    assert store.get_card(memory_id).embedding_state == "failed"


@live_pg
def test_live_worker_empty_payload_safety(pg_store):
    store, tag = pg_store
    memory_id = f"{tag}_empty"
    store.upsert_card(
        MemoryCard(
            memory_id=memory_id,
            project=f"{tag}_proj",
            card_type="decision",
            title="",
            summary="",
            content_hash="sha256:empty_hash",
            embedding_state="pending",
        )
    )

    assert _worker(store, f"{tag}_w_empty").run_once() == 1
    updated = store.get_card(memory_id)
    assert updated.embedding_state == "ready"
    assert len(updated.embedding) == SHARED_EMBEDDING_DIM


@live_pg
def test_live_worker_run_loop_bounded(pg_store):
    store, tag = pg_store
    memory_id = f"{tag}_loop"
    store.upsert_card(_pending_card(memory_id, f"{tag}_proj", "sha256:loop_hash"))

    _worker(store, f"{tag}_w_loop", poll_interval_seconds=0.01).run_loop(
        max_iterations=2
    )

    assert store.get_card(memory_id).embedding_state == "ready"


@live_pg
def test_live_graph_projection_outbox_enqueued_on_upsert(pg_store):
    store, tag = pg_store
    memory_id = f"{tag}_graph_card"
    card = _pending_card(memory_id, f"{tag}_proj", "sha256:graph_hash")
    store.upsert_card(card)

    graph_jobs = [job for job in store.list_graph_projection_jobs() if job.source_id == memory_id]
    assert len(graph_jobs) == 1
    job = graph_jobs[0]
    assert job.status == "queued"
    assert job.source_type == "memory_card"
    assert job.source_revision == "sha256:graph_hash"
    assert job.episode_payload["authority_memory_id"] == memory_id


@live_pg
def test_live_graph_projection_worker_happy_path(pg_store):
    from agent_knowledge.postgres_store.outbox_worker import GraphProjectionWorker

    store, tag = pg_store
    memory_id = f"{tag}_graph_worker"
    card = _pending_card(memory_id, f"{tag}_proj", "sha256:worker_graph_hash")
    store.upsert_card(card)

    received_payloads = []

    class MockAdapter:
        def upsert_episode(self, payload):
            received_payloads.append(payload)
            return "inserted"

    worker = GraphProjectionWorker(
        store=store,
        graph_adapter=MockAdapter(),
        worker_id=f"{tag}_gworker",
        batch_size=50,
    )

    # Process all pending queue items in the batch
    processed = worker.run_once()
    assert processed >= 1
    assert any(p.get("authority_memory_id") == memory_id for p in received_payloads)

    graph_jobs = [job for job in store.list_graph_projection_jobs() if job.source_id == memory_id]
    assert graph_jobs[0].status == "completed"


@live_pg
def test_live_graph_projection_worker_retry_to_dead_letter(pg_store):
    from agent_knowledge.postgres_store.outbox_worker import GraphProjectionWorker

    store, tag = pg_store
    memory_id = f"{tag}_graph_fail"
    card = _pending_card(memory_id, f"{tag}_proj", "sha256:fail_graph_hash")
    store.upsert_card(card)

    class FailingAdapter:
        def upsert_episode(self, payload):
            raise ConnectionError("Neo4j down")

    worker = GraphProjectionWorker(
        store=store,
        graph_adapter=FailingAdapter(),
        worker_id=f"{tag}_gfail_worker",
        max_retries=3,
    )

    for _ in range(3):
        with store._scope(write=True) as db:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE graph_projection_outbox SET lease_until = NOW() - INTERVAL '1 second' WHERE source_id = %s",
                    (memory_id,),
                )
        worker.run_once()

    jobs = [job for job in store.list_graph_projection_jobs() if job.source_id == memory_id]
    assert len(jobs) == 1
    assert jobs[0].status == "dead_letter"
    assert jobs[0].retry_count >= 3
    assert "Neo4j down" in (jobs[0].last_error or "")
