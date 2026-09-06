"""DSN-gated live challenger tests for Milestone 3 (M6a).

Adversarial coverage for the PostgreSQL store, outbox leases, CAS races,
dead-letter limits, and DAG cycle handling against a disposable PostgreSQL
(``LBRAIN_TEST_PG_DSN``). Without the DSN the live tests skip; the static
contract tests in this file run without any database.

Shared embedding profile: 3072-dim (gemini-embedding-2 / halfvec(3072)).
``SHARED_EMBEDDING_DIM`` pins the expectation file-locally; the production
default is imported, never changed here.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
import threading
import uuid

import pytest

from agent_knowledge.postgres_store.pgvector_store import (
    PgVectorStore,
    MemoryCard,
    MemoryEdge,
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
    tag = f"m6a_chal_{uuid.uuid4().hex[:10]}"
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


def _card(memory_id: str, project: str, **kwargs) -> MemoryCard:
    kwargs.setdefault("content_hash", f"sha256:{memory_id[-8:]}")
    return MemoryCard(
        memory_id=memory_id,
        project=project,
        card_type="decision",
        title=f"Title {memory_id}",
        summary=f"Summary {memory_id}",
        **kwargs,
    )


def _authorized_card(memory_id: str, project: str, **kwargs) -> MemoryCard:
    kwargs.setdefault("lifecycle_state", "human_accepted")
    kwargs.setdefault("authorization_status", "active")
    return _card(memory_id, project, **kwargs)


# ==============================================================================
# A. Static contract tests (no database)
# ==============================================================================


def test_shared_embedding_profile_is_3072():
    assert VECTOR_DIMENSION == SHARED_EMBEDDING_DIM


def test_challenger_static_validation_rejects_corrupt_state_without_database():
    store = PgVectorStore(dsn=OFFLINE_DSN)
    with pytest.raises(ValueError, match="valid_to cannot be earlier"):
        store.insert_card(
            _card(
                "bad_temp", "p",
                valid_from=datetime(2026, 8, 1, tzinfo=timezone.utc),
                valid_to=datetime(2026, 7, 1, tzinfo=timezone.utc),
            )
        )
    with pytest.raises(ValueError, match="Vector dimension mismatch"):
        store.insert_card(_card("bad_vec", "p", embedding=[0.1] * 768))
    with pytest.raises(ValueError, match="Invalid rel_type"):
        store.insert_edge(MemoryEdge(src_id="a", dst_id="b", rel_type="cycles_to"))
    with pytest.raises(ValueError, match="confidence must be between"):
        store.insert_card(_card("bad_conf", "p", confidence=2.0))


# ==============================================================================
# B. Live challenger tests (disposable PostgreSQL)
# ==============================================================================


@live_pg
def test_live_challenger_ddl_and_constraints(pg_store):
    store, tag = pg_store
    project = f"{tag}_proj"
    store.insert_card(_authorized_card(f"{tag}_c1", project))
    assert store.get_card(f"{tag}_c1") is not None

    with pytest.raises(ValueError, match="valid_to cannot be earlier"):
        store.insert_card(
            _card(
                f"{tag}_bad_temp", project,
                valid_from=datetime(2026, 8, 1, tzinfo=timezone.utc),
                valid_to=datetime(2026, 7, 1, tzinfo=timezone.utc),
            )
        )
    with pytest.raises(ValueError, match="Vector dimension mismatch"):
        store.insert_card(_card(f"{tag}_bad_vec", project, embedding=[0.1] * 768))


@live_pg
def test_live_challenger_dag_cycle_diamond_and_depth_bound(pg_store):
    store, tag = pg_store
    project = f"{tag}_proj"
    # Diamond (A -> B/C -> D) with a back edge (D -> A) plus a deep chain.
    nodes = [f"{tag}_{name}" for name in ("A", "B", "C", "D", "E", "F", "G", "H", "I")]
    by_name = dict(zip(("A", "B", "C", "D", "E", "F", "G", "H", "I"), nodes))
    for node in nodes:
        store.insert_card(_authorized_card(node, project))
    for src, dst, rel in (
        ("A", "B", "derived_from"),
        ("A", "C", "derived_from"),
        ("B", "D", "supports"),
        ("C", "D", "supports"),
        ("D", "A", "supersedes"),  # cycle back to the root
        ("D", "E", "derived_from"),
        ("E", "F", "derived_from"),
        ("F", "G", "derived_from"),
        ("G", "H", "derived_from"),
        ("H", "I", "derived_from"),
    ):
        store.insert_edge(
            MemoryEdge(
                src_id=by_name[src], rel_type=rel, dst_id=by_name[dst],
                provenance_hash=f"sha256:{src}{dst}",
            )
        )

    traversal = store.traverse_provenance_dag(by_name["A"], max_depth=5)
    assert len(traversal) > 0
    assert max(row["depth"] for row in traversal) <= 5
    # A->B, A->C, B->D, C->D, D->E, E->F, F->G; the D->A revisit is pruned
    # and G->H would exceed max_depth.
    assert len(traversal) == 7
    assert all(row["depth"] <= 5 for row in traversal)


@live_pg
def test_live_challenger_outbox_concurrency_no_double_processing(pg_store):
    store, tag = pg_store
    project = f"{tag}_proj"
    for i in range(10):
        store.upsert_card(
            _card(
                f"{tag}_concurrent_{i}", project,
                content_hash=f"sha256:{i:064d}",
                embedding_state="pending",
            )
        )

    workers = [
        OutboxWorker(
            store, worker_id=f"{tag}_worker_{w}", batch_size=4, lease_seconds=10,
            embedding_fn=generate_deterministic_embedding,
        )
        for w in range(3)
    ]
    results = [0, 0, 0]

    def worker_run(w_idx: int):
        results[w_idx] = workers[w_idx].run_once()

    threads = [threading.Thread(target=worker_run, args=(w,)) for w in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(results) == 10
    for i in range(10):
        card = store.get_card(f"{tag}_concurrent_{i}")
        assert card.embedding_state == "ready"
        assert card.embedding is not None
        assert len(card.embedding) == SHARED_EMBEDDING_DIM


@live_pg
def test_live_challenger_cas_stale_hash_protection(pg_store):
    store, tag = pg_store
    memory_id = f"{tag}_stale_race"
    project = f"{tag}_proj"
    store.upsert_card(
        _card(memory_id, project, content_hash="sha256:initial", embedding_state="pending")
    )

    worker = OutboxWorker(
        store, worker_id=f"{tag}_cas_race",
        embedding_fn=generate_deterministic_embedding,
    )
    jobs = store.claim_outbox_leases(f"{tag}_cas_race", batch_size=1, lease_seconds=30)
    assert len(jobs) == 1
    job = jobs[0]

    with store.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE memory_cards SET content_hash = %s WHERE memory_id = %s",
                ("sha256:mutated", memory_id),
            )

    assert worker.process_job(job) is False

    reloaded = store.get_card(memory_id)
    assert reloaded.embedding_state == "pending"
    assert reloaded.embedding is None
    assert store.get_outbox_job(job.outbox_id).status == "cas_skipped"


@live_pg
def test_live_challenger_lease_timeout_and_recovery(pg_store):
    store, tag = pg_store
    memory_id = f"{tag}_lease"
    project = f"{tag}_proj"
    store.upsert_card(
        _card(memory_id, project, content_hash="sha256:lease", embedding_state="pending")
    )

    jobs = store.claim_outbox_leases(f"{tag}_crashed", batch_size=1, lease_seconds=30)
    assert len(jobs) == 1
    job = jobs[0]
    assert store.get_outbox_job(job.outbox_id).status == "processing"

    _expire_lease(store, job.outbox_id)

    jobs2 = store.claim_outbox_leases(f"{tag}_recovery", batch_size=1, lease_seconds=30)
    assert len(jobs2) == 1
    assert jobs2[0].outbox_id == job.outbox_id
    assert jobs2[0].worker_id == f"{tag}_recovery"

    recovery = OutboxWorker(
        store, worker_id=f"{tag}_recovery",
        embedding_fn=generate_deterministic_embedding,
    )
    assert recovery.process_job(jobs2[0]) is True
    assert store.get_outbox_job(job.outbox_id).status == "completed"
    assert store.get_card(memory_id).embedding_state == "ready"


@live_pg
def test_live_challenger_dead_letter_escalation(pg_store):
    store, tag = pg_store
    memory_id = f"{tag}_failing"
    project = f"{tag}_proj"

    def failing_embed_fn(_text: str) -> list[float]:
        raise RuntimeError("Embedding model API quota exceeded")

    store.upsert_card(
        _card(memory_id, project, content_hash="sha256:failing", embedding_state="pending")
    )
    worker = OutboxWorker(
        store, worker_id=f"{tag}_flaky", embedding_fn=failing_embed_fn, max_retries=3
    )

    for _ in range(3):
        jobs = [job for job in store.list_outbox_jobs() if job.target_id == memory_id]
        _expire_lease(store, jobs[0].outbox_id)
        worker.run_once()

    outbox_job = [job for job in store.list_outbox_jobs() if job.target_id == memory_id][0]
    assert outbox_job.status == "dead_letter"
    assert outbox_job.retry_count == 3
    assert "Embedding model API quota exceeded" in str(outbox_job.last_error)
    assert store.get_card(memory_id).embedding_state == "failed"
