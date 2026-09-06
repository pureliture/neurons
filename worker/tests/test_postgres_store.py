"""DSN-gated live integration tests for PgVectorStore (M6a).

Live tests require ``LBRAIN_TEST_PG_DSN`` pointing at a disposable PostgreSQL
(pgvector >= 0.8.0). Without the DSN they skip; the static contract tests in
this file run without any database.

Shared embedding profile: 3072-dim (gemini-embedding-2 / halfvec(3072)).
``SHARED_EMBEDDING_DIM`` pins the expectation file-locally; the production
default (``VECTOR_DIMENSION``) is imported, never changed here.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import os
import uuid

import pytest

from agent_knowledge.postgres_store.pgvector_store import (
    MemoryCard,
    MemoryEdge,
    OutboxJob,
    PgVectorStore,
    SessionChunk,
    VECTOR_DIMENSION,
    compute_cosine_similarity,
    make_dummy_vector,
)

# Legacy 1536-dim profile is NOT accepted here; the shared profile is 3072-dim.
SHARED_EMBEDDING_DIM = 3072

PG_DSN = os.environ.get("LBRAIN_TEST_PG_DSN", "")
live_pg = pytest.mark.skipif(
    not PG_DSN,
    reason="LBRAIN_TEST_PG_DSN 미설정 (전용 live PostgreSQL integration gate)",
)

OFFLINE_DSN = "postgresql://invalid.example.invalid/never"


def _offline_store(**kwargs) -> PgVectorStore:
    """A store whose constructor never connects; validation raises first."""
    return PgVectorStore(dsn=OFFLINE_DSN, **kwargs)


def _row_value(row, key: str, index: int):
    if isinstance(row, dict):
        return row[key]
    return row[index]


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
    tag = f"m6a_store_{uuid.uuid4().hex[:10]}"
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


def _card(
    memory_id: str,
    project: str = "m6a-postgres",
    *,
    authorized: bool = False,
    **kwargs,
) -> MemoryCard:
    if authorized:
        kwargs.setdefault("lifecycle_state", "human_accepted")
        kwargs.setdefault("authorization_status", "active")
    return MemoryCard(
        memory_id=memory_id,
        project=project,
        card_type="decision",
        title=f"Title {memory_id}",
        summary=f"Summary {memory_id}",
        **kwargs,
    )


# ==============================================================================
# A. Static contract tests (no database)
# ==============================================================================


def test_shared_embedding_profile_is_3072():
    assert VECTOR_DIMENSION == SHARED_EMBEDDING_DIM
    vec = make_dummy_vector(1)
    assert len(vec) == SHARED_EMBEDDING_DIM


def test_schema_sql_declares_required_objects():
    schema = PgVectorStore.schema_sql()
    assert "CREATE TABLE IF NOT EXISTS memory_cards" in schema
    assert "CREATE TABLE IF NOT EXISTS session_memory_chunks" in schema
    assert "CREATE TABLE IF NOT EXISTS memory_edges" in schema
    assert "CREATE TABLE IF NOT EXISTS embedding_outbox" in schema
    assert "halfvec(3072)" in schema
    assert "halfvec_cosine_ops" in schema
    assert "idx_embedding_outbox_dedup" in schema
    assert "ON DELETE RESTRICT" in schema


def test_vector_dimension_mismatch_rejected_before_database():
    store = _offline_store()
    with pytest.raises(ValueError, match="Vector dimension mismatch"):
        store.insert_card(_card("dim_bad", embedding=[0.1] * 768))
    with pytest.raises(ValueError, match="Vector dimension mismatch"):
        store.upsert_card(_card("dim_bad_upsert", embedding=[0.1] * 1536))


def test_temporal_inversion_rejected_before_database():
    now = datetime.now(timezone.utc)
    store = _offline_store()
    with pytest.raises(ValueError, match="valid_to cannot be earlier than valid_from"):
        store.insert_card(
            _card("temporal_bad", valid_from=now, valid_to=now - timedelta(days=1))
        )


def test_enum_constraints_rejected_before_database():
    store = _offline_store()
    with pytest.raises(ValueError, match="Invalid lifecycle_state"):
        store.insert_card(_card("enum_life", lifecycle_state="invalid_lifecycle_xyz"))
    with pytest.raises(ValueError, match="Invalid authorization_status"):
        store.insert_card(_card("enum_auth", authorization_status="super_admin"))
    with pytest.raises(ValueError, match="Invalid currentness"):
        store.insert_card(_card("enum_curr", currentness="timey_wimey"))


def test_edge_endpoint_and_reltype_rejected_before_database():
    store = _offline_store()
    with pytest.raises(ValueError, match="endpoints are required"):
        store.insert_edge(MemoryEdge(src_id="", dst_id="b"))
    with pytest.raises(ValueError, match="Invalid rel_type"):
        store.insert_edge(MemoryEdge(src_id="a", dst_id="b", rel_type="likes"))


def test_outbox_lease_arguments_rejected_before_database():
    store = _offline_store()
    with pytest.raises(ValueError, match="invalid outbox lease arguments"):
        store.claim_outbox_leases("", batch_size=1)
    with pytest.raises(ValueError, match="invalid outbox lease arguments"):
        store.claim_outbox_leases("w", batch_size=0)
    with pytest.raises(ValueError, match="invalid outbox lease arguments"):
        store.claim_outbox_leases("w", batch_size=1, lease_seconds=0)


def test_cas_and_failure_owner_guards_rejected_before_database():
    store = _offline_store()
    with pytest.raises(ValueError, match="unsupported outbox target type"):
        store.cas_update_embedding(1, "t", "h", make_dummy_vector(3),
                                   target_type="nope", worker_id="w")
    with pytest.raises(ValueError, match="requires worker_id"):
        store.cas_update_embedding(1, "t", "h", make_dummy_vector(3))
    with pytest.raises(ValueError, match="requires worker_id"):
        store.mark_outbox_failed(1, "boom")
    with pytest.raises(ValueError, match="max_retries must be positive"):
        store.mark_outbox_failed(1, "boom", max_retries=0, worker_id="w")


def test_traversal_depth_bounds_rejected_before_database():
    store = _offline_store()
    with pytest.raises(ValueError, match="max_depth must be between 1 and 5"):
        store.traverse_provenance_dag("anything", max_depth=6)
    with pytest.raises(ValueError, match="bounded evidence depth and limit required"):
        store.read_authorized_evidence(project="p", root_memory_ids=["a"], max_depth=0)
    with pytest.raises(ValueError, match="1..20 evidence roots required"):
        store.read_authorized_evidence(project="p", root_memory_ids=[])


def test_hybrid_search_argument_guards_rejected_before_database():
    store = _offline_store()
    vec = make_dummy_vector(9)
    with pytest.raises(ValueError, match="limit must be positive"):
        store.hybrid_search(project="p", query_vector=vec, limit=0)
    with pytest.raises(ValueError, match="query vector"):
        store.hybrid_search(project="p", query_vector=None)
    with pytest.raises(ValueError, match="Vector dimension mismatch"):
        store.hybrid_search(project="p", query_vector=[0.1] * 1536)
    with pytest.raises(ValueError, match="as_of"):
        store.hybrid_search(project="p", query_vector=vec, as_of="bad-time")


def test_guc_relaxed_order_disabled_for_old_pgvector_without_database():
    store = _offline_store(pgvector_version="0.7.4")
    assert store.set_guc_relaxed_order() is False


def test_guc_allowlist_rejected_before_database():
    store = _offline_store()
    with pytest.raises(ValueError, match="unsupported PostgreSQL GUC"):
        store.set_local_guc("search_path", "public")
    with pytest.raises(ValueError, match="unsupported hnsw.iterative_scan value"):
        store.set_local_guc("hnsw.iterative_scan", "sometimes")


def test_cosine_similarity_is_pure_and_bounded():
    assert compute_cosine_similarity(None, make_dummy_vector(1)) == 0.0
    assert compute_cosine_similarity([0.1] * 4, [0.1] * 5) == 0.0
    vec = make_dummy_vector(42)
    assert compute_cosine_similarity(vec, list(vec)) == pytest.approx(1.0)
    assert 0.0 <= compute_cosine_similarity(vec, make_dummy_vector(43)) <= 1.0


# ==============================================================================
# B. Live tests (disposable PostgreSQL)
# ==============================================================================


@live_pg
def test_live_ddl_creates_required_tables_and_vector_index(pg_store):
    store, _tag = pg_store
    with store.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                " AND tablename IN ('memory_cards', 'session_memory_chunks',"
                " 'memory_edges', 'embedding_outbox')"
            )
            tables = {_row_value(row, "tablename", 0) for row in cur.fetchall()}
            cur.execute(
                "SELECT indexname FROM pg_indexes"
                " WHERE schemaname = 'public'"
                " AND indexname = 'idx_memory_cards_embedding'"
            )
            indexes = {_row_value(row, "indexname", 0) for row in cur.fetchall()}
    assert tables == {
        "memory_cards",
        "session_memory_chunks",
        "memory_edges",
        "embedding_outbox",
    }
    assert indexes == {"idx_memory_cards_embedding"}


@live_pg
def test_live_vector_dimension_roundtrip_is_shared_profile(pg_store):
    store, tag = pg_store
    memory_id = f"{tag}_dim"
    store.insert_card(_card(memory_id, embedding=make_dummy_vector(1)))
    retrieved = store.get_card(memory_id)
    assert retrieved is not None
    assert len(retrieved.embedding) == SHARED_EMBEDDING_DIM


@live_pg
def test_live_foreign_key_restrict_on_delete(pg_store):
    store, tag = pg_store
    store.insert_card(_card(f"{tag}_parent"))
    store.insert_card(_card(f"{tag}_child"))
    store.insert_edge(
        MemoryEdge(
            src_id=f"{tag}_child",
            rel_type="derived_from",
            dst_id=f"{tag}_parent",
            provenance_hash="sha256:prov1",
        )
    )
    with pytest.raises(ValueError, match="referenced by memory_edges"):
        store.delete_card(f"{tag}_parent")
    with pytest.raises(ValueError, match="referenced by memory_edges"):
        store.delete_card(f"{tag}_child")


@live_pg
def test_live_upsert_card_enqueues_transactional_outbox(pg_store):
    store, tag = pg_store
    memory_id = f"{tag}_pending"
    store.upsert_card(
        _card(
            memory_id,
            typed_payload={"decision": "A"},
            content_hash="sha256:hash_pending",
            embedding_state="pending",
        )
    )
    jobs = [job for job in store.list_outbox_jobs() if job.target_id == memory_id]
    assert len(jobs) == 1
    job = jobs[0]
    assert job.target_type == "memory_card"
    assert job.content_hash == "sha256:hash_pending"
    assert job.status == "queued"
    assert job.payload_text == f"Summary {memory_id}"


@live_pg
def test_live_outbox_dedup_is_idempotent_and_requeues_after_completion(pg_store):
    store, tag = pg_store
    target_id = f"{tag}_dedup"
    content_hash = "sha256:dedup_hash"
    worker_id = f"{tag}_worker"
    store.insert_card(
        _card(target_id, content_hash=content_hash, embedding_state="pending")
    )
    first_id = store.enqueue_outbox("memory_card", target_id, content_hash, "Text 1")
    second_id = store.enqueue_outbox("memory_card", target_id, content_hash, "Text 2")
    assert first_id == second_id

    claimed = store.claim_outbox_leases(worker_id, batch_size=1, lease_seconds=30)
    assert [job.outbox_id for job in claimed] == [first_id]
    assert store.cas_update_embedding(
        first_id, target_id, content_hash, make_dummy_vector(10), worker_id=worker_id
    ) is True

    third_id = store.enqueue_outbox("memory_card", target_id, content_hash, "Text 3")
    assert third_id != first_id


@live_pg
def test_live_outbox_claims_partition_across_workers(pg_store):
    store, tag = pg_store
    first = store.enqueue_outbox("memory_card", f"{tag}_m1", "sha256:1", "T1")
    second = store.enqueue_outbox("memory_card", f"{tag}_m2", "sha256:2", "T2")

    claimed_w1 = store.claim_outbox_leases(f"{tag}_w1", batch_size=1, lease_seconds=30)
    assert len(claimed_w1) == 1
    assert claimed_w1[0].outbox_id == first
    assert claimed_w1[0].worker_id == f"{tag}_w1"
    assert claimed_w1[0].status == "processing"

    claimed_w2 = store.claim_outbox_leases(f"{tag}_w2", batch_size=1, lease_seconds=30)
    assert len(claimed_w2) == 1
    assert claimed_w2[0].outbox_id == second
    assert claimed_w2[0].worker_id == f"{tag}_w2"
    assert {claimed_w1[0].outbox_id, claimed_w2[0].outbox_id} == {first, second}


@live_pg
def test_live_cas_update_embedding_matching_hash(pg_store):
    store, tag = pg_store
    memory_id = f"{tag}_cas"
    content_hash = "sha256:valid_hash"
    worker_id = f"{tag}_worker"
    store.insert_card(
        _card(memory_id, content_hash=content_hash, embedding_state="pending")
    )
    job_id = store.enqueue_outbox("memory_card", memory_id, content_hash, "Text")
    claimed = store.claim_outbox_leases(worker_id, batch_size=1, lease_seconds=30)
    assert [job.outbox_id for job in claimed] == [job_id]

    vec = make_dummy_vector(10)
    assert store.cas_update_embedding(
        job_id, memory_id, content_hash, vec, worker_id=worker_id
    ) is True

    updated = store.get_card(memory_id)
    assert updated.embedding_state == "ready"
    assert updated.embedding_revision == 2
    assert updated.embedding == pytest.approx(vec, abs=1e-4)
    assert store.get_outbox_job(job_id).status == "completed"


@live_pg
def test_live_cas_update_embedding_stale_hash_is_terminal_noop(pg_store):
    store, tag = pg_store
    memory_id = f"{tag}_stale"
    worker_id = f"{tag}_worker"
    store.insert_card(
        _card(memory_id, content_hash="sha256:hash_v2", embedding_state="pending")
    )
    job_id = store.enqueue_outbox("memory_card", memory_id, "sha256:hash_v1", "Old Text")
    store.claim_outbox_leases(worker_id, batch_size=1, lease_seconds=30)

    assert store.cas_update_embedding(
        job_id, memory_id, "sha256:hash_v1", make_dummy_vector(1), worker_id=worker_id
    ) is False

    unchanged = store.get_card(memory_id)
    assert unchanged.embedding is None
    assert unchanged.embedding_state == "pending"
    job = store.get_outbox_job(job_id)
    assert job.status == "cas_skipped"
    assert job.last_error == "CAS skip: content_hash mismatch"


@live_pg
def test_live_outbox_backoff_escalates_to_dead_letter(pg_store):
    store, tag = pg_store
    memory_id = f"{tag}_fail"
    worker_id = f"{tag}_worker"
    content_hash = "sha256:fail_case_hash"
    store.insert_card(_card(memory_id, content_hash=content_hash))
    job_id = store.enqueue_outbox("memory_card", memory_id, content_hash, "Payload")

    for attempt in range(1, 5):
        claimed = store.claim_outbox_leases(worker_id, batch_size=1, lease_seconds=30)
        assert [job.outbox_id for job in claimed] == [job_id]
        store.mark_outbox_failed(
            job_id, error_message=f"Transient timeout {attempt}",
            max_retries=5, worker_id=worker_id,
        )
        job = store.get_outbox_job(job_id)
        assert job.status == "failed"
        assert job.retry_count == attempt
        assert job.lease_until is not None
        _expire_lease(store, job_id)

    claimed = store.claim_outbox_leases(worker_id, batch_size=1, lease_seconds=30)
    assert [job.outbox_id for job in claimed] == [job_id]
    store.mark_outbox_failed(
        job_id, error_message="Fatal unrecoverable", max_retries=5, worker_id=worker_id
    )
    job = store.get_outbox_job(job_id)
    assert job.status == "dead_letter"
    assert job.retry_count == 5
    assert store.get_card(memory_id).embedding_state == "failed"


@live_pg
def test_live_guc_relaxed_order_supported_version(pg_store):
    store, _tag = pg_store
    assert store.set_guc_relaxed_order() is True


@live_pg
def test_live_hybrid_search_enforces_authority_filters(pg_store):
    store, tag = pg_store
    project = f"{tag}_proj"
    query_vec = make_dummy_vector(100)
    visible = f"{tag}_visible"
    store.insert_card(
        _card(
            visible, project,
            authorized=True,
            embedding=query_vec,
            embedding_state="ready",
        )
    )
    store.insert_card(
        _card(
            f"{tag}_disabled", project,
            lifecycle_state="human_accepted",
            authorization_status="disabled",
            embedding=query_vec,
            embedding_state="ready",
        )
    )
    store.insert_card(
        _card(
            f"{tag}_candidate", project,
            embedding=query_vec,
            embedding_state="ready",
        )
    )
    store.insert_card(
        _card(
            f"{tag}_other", f"{tag}_other_proj",
            authorized=True,
            embedding=query_vec,
            embedding_state="ready",
        )
    )

    for text_query in (None, "Title"):
        results = store.hybrid_search(
            project=project, query_vector=query_vec, limit=5, text_query=text_query
        )
        assert [row["memory_id"] for row in results] == [visible]
        assert 0.0 <= results[0]["similarity_score"] <= 1.0


@live_pg
def test_live_dag_traversal_multi_hop(pg_store):
    store, tag = pg_store
    project = f"{tag}_proj"
    nodes = [f"{tag}_node_{i}" for i in range(1, 5)]
    for node in nodes:
        store.insert_card(_card(node, project, authorized=True))
    pairs = [(nodes[0], nodes[1]), (nodes[1], nodes[2]), (nodes[2], nodes[3])]
    for src, dst in pairs:
        store.insert_edge(
            MemoryEdge(src_id=src, rel_type="derived_from", dst_id=dst,
                       provenance_hash=f"sha256:{src[-6:]}")
        )

    dag = store.traverse_provenance_dag(nodes[0], max_depth=5)
    assert len(dag) == 3
    assert [row["depth"] for row in dag] == [1, 2, 3]
    assert dag[2]["dst_id"] == nodes[3]


@live_pg
def test_live_dag_traversal_cycles_terminate(pg_store):
    store, tag = pg_store
    project = f"{tag}_proj"
    for name in (f"{tag}_A", f"{tag}_B"):
        store.insert_card(_card(name, project, authorized=True))
    store.insert_edge(
        MemoryEdge(src_id=f"{tag}_A", rel_type="derived_from", dst_id=f"{tag}_B",
                   provenance_hash="sha256:ab")
    )
    store.insert_edge(
        MemoryEdge(src_id=f"{tag}_B", rel_type="derived_from", dst_id=f"{tag}_A",
                   provenance_hash="sha256:ba")
    )
    dag = store.traverse_provenance_dag(f"{tag}_A", max_depth=5)
    # The recursive CTE prunes the revisit (B -> A) via its visited path.
    assert len(dag) == 1
    assert dag[0]["dst_id"] == f"{tag}_B"
    assert all(row["depth"] <= 5 for row in dag)


@live_pg
def test_live_dag_traversal_self_loop_is_excluded(pg_store):
    store, tag = pg_store
    project = f"{tag}_proj"
    node = f"{tag}_loop"
    store.insert_card(_card(node, project, authorized=True))
    store.insert_edge(
        MemoryEdge(src_id=node, rel_type="supports", dst_id=node,
                   provenance_hash="sha256:loop")
    )
    # The recursive CTE only walks explicit relations between distinct cards.
    assert store.traverse_provenance_dag(node, max_depth=5) == []


@live_pg
def test_live_dag_traversal_diamond_visits_both_branches(pg_store):
    store, tag = pg_store
    project = f"{tag}_proj"
    nodes = {name: f"{tag}_{name}" for name in ("A", "B", "C", "D")}
    for node in nodes.values():
        store.insert_card(_card(node, project, authorized=True))
    for src, dst in (("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")):
        store.insert_edge(
            MemoryEdge(src_id=nodes[src], rel_type="derived_from", dst_id=nodes[dst],
                       provenance_hash=f"sha256:{src}{dst}")
        )

    dag = store.traverse_provenance_dag(nodes["A"], max_depth=5)
    assert len(dag) == 4
    assert [row["dst_id"] for row in dag].count(nodes["D"]) == 2
