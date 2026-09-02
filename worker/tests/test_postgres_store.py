"""Unit and integration test suite for PgVectorStore (Milestone 3).

Verifies PostgreSQL DDL, HNSW cosine index configuration, table constraints,
relaxed_order GUC execution, CAS outbox concurrency, and cycle-safe recursive CTE DAG traversal.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import pytest

from agent_knowledge.postgres_store.pgvector_store import (
    MemoryCard,
    MemoryEdge,
    SessionChunk,
    OutboxJob,
    PgVectorStore,
    compute_cosine_similarity,
    make_dummy_vector,
)


@pytest.fixture
def store() -> PgVectorStore:
    s = PgVectorStore(use_in_memory=True)
    s.execute_ddl()
    return s


# ==============================================================================
# 1. DDL, Schema & Extension Tests
# ==============================================================================

def test_pgvector_schema_ddl_loaded(store: PgVectorStore):
    """Verify DDL schema loads and contains all required tables and indexes."""
    assert len(store.executed_ddl) >= 1
    ddl_content = store.executed_ddl[0]
    assert "memory_cards" in ddl_content
    assert "session_memory_chunks" in ddl_content
    assert "memory_edges" in ddl_content
    assert "embedding_outbox" in ddl_content
    assert "vector_cosine_ops" in ddl_content
    assert "idx_embedding_outbox_dedup" in ddl_content
    assert "ON DELETE RESTRICT" in ddl_content


def test_vector_dimension_validation(store: PgVectorStore):
    """Vector columns strictly validate 1536 dimensions."""
    valid_vec = make_dummy_vector(1, dim=1536)
    card_valid = MemoryCard(
        memory_id="card_1536",
        project="neurons",
        card_type="decision",
        title="1536 Dim",
        summary="Valid",
        embedding=valid_vec,
        embedding_state="ready",
    )
    store.insert_card(card_valid)
    retrieved = store.get_card("card_1536")
    assert retrieved is not None
    assert len(retrieved.embedding) == 1536

    # 768 dimensions should fail
    invalid_vec = [0.1] * 768
    card_invalid = MemoryCard(
        memory_id="card_768",
        project="neurons",
        card_type="decision",
        title="768 Dim",
        summary="Invalid",
        embedding=invalid_vec,
    )
    with pytest.raises(ValueError, match="Vector dimension mismatch"):
        store.insert_card(card_invalid)


def test_temporal_inversion_validation(store: PgVectorStore):
    """valid_to < valid_from constraint check."""
    now = datetime.now(timezone.utc)
    card_inv = MemoryCard(
        memory_id="card_inv",
        project="neurons",
        card_type="decision",
        title="Inverted",
        summary="Inv",
        valid_from=now,
        valid_to=now - timedelta(days=1),
    )
    with pytest.raises(ValueError, match="valid_to cannot be earlier than valid_from"):
        store.insert_card(card_inv)


def test_enum_constraints_validation(store: PgVectorStore):
    """Enum check constraints for lifecycle_state, authorization_status, currentness."""
    # Invalid lifecycle_state
    card_bad_life = MemoryCard(
        memory_id="c_bad_life",
        project="neurons",
        card_type="decision",
        title="Bad Life",
        summary="Bad",
        lifecycle_state="invalid_lifecycle_xyz",
    )
    with pytest.raises(ValueError, match="Invalid lifecycle_state"):
        store.insert_card(card_bad_life)

    # Invalid authorization_status
    card_bad_auth = MemoryCard(
        memory_id="c_bad_auth",
        project="neurons",
        card_type="decision",
        title="Bad Auth",
        summary="Bad",
        authorization_status="super_admin",
    )
    with pytest.raises(ValueError, match="Invalid authorization_status"):
        store.insert_card(card_bad_auth)


def test_foreign_key_restrict_on_delete(store: PgVectorStore):
    """FK ON DELETE RESTRICT prevents deleting cards referenced in memory_edges."""
    c1 = MemoryCard(memory_id="parent_c", project="neurons", card_type="decision", title="P", summary="P")
    c2 = MemoryCard(memory_id="child_c", project="neurons", card_type="decision", title="C", summary="C")
    store.insert_card(c1)
    store.insert_card(c2)

    store.insert_edge(
        MemoryEdge(
            edge_id=1,
            src_id="child_c",
            rel_type="derived_from",
            dst_id="parent_c",
            provenance_hash="sha256:prov1",
        )
    )

    with pytest.raises(ValueError, match="Foreign key constraint violation"):
        store.delete_card("parent_c")

    with pytest.raises(ValueError, match="Foreign key constraint violation"):
        store.delete_card("child_c")


# ==============================================================================
# 2. Transactional Outbox & CAS Tests
# ==============================================================================

def test_upsert_card_transactional_outbox_enqueue(store: PgVectorStore):
    """upsert_card automatically enqueues outbox task if embedding is pending."""
    card = MemoryCard(
        memory_id="card_pending",
        project="neurons",
        card_type="decision",
        title="Pending Card",
        summary="Pending summary",
        typed_payload={"decision": "A"},
        content_hash="sha256:hash_pending",
        embedding_state="pending",
    )
    store.upsert_card(card)

    assert len(store.outbox) == 1
    job = list(store.outbox.values())[0]
    assert job.target_id == "card_pending"
    assert job.content_hash == "sha256:hash_pending"
    assert job.status == "queued"
    assert job.payload_text == "Pending summary"


def test_outbox_partial_unique_index_dedup(store: PgVectorStore):
    """idx_embedding_outbox_dedup prevents duplicate queued/processing entries."""
    store.enqueue_outbox("memory_card", "mem_1", "sha256:h1", "Text 1")

    with pytest.raises(ValueError, match="Unique constraint violation"):
        store.enqueue_outbox("memory_card", "mem_1", "sha256:h1", "Text 2")

    # If first job completes, re-enqueueing is permitted
    job_id = list(store.outbox.keys())[0]
    store.outbox[job_id].status = "completed"

    new_id = store.enqueue_outbox("memory_card", "mem_1", "sha256:h1", "Text 3")
    assert new_id != job_id


def test_outbox_claim_leases_skip_locked(store: PgVectorStore):
    """claim_outbox_leases assigns batch with lease_until and worker_id."""
    id1 = store.enqueue_outbox("memory_card", "m1", "sha256:1", "T1")
    id2 = store.enqueue_outbox("memory_card", "m2", "sha256:2", "T2")

    claimed_w1 = store.claim_outbox_leases("worker_1", batch_size=1, lease_seconds=30)
    assert len(claimed_w1) == 1
    assert claimed_w1[0].outbox_id == id1
    assert store.outbox[id1].worker_id == "worker_1"
    assert store.outbox[id1].status == "processing"

    claimed_w2 = store.claim_outbox_leases("worker_2", batch_size=1, lease_seconds=30)
    assert len(claimed_w2) == 1
    assert claimed_w2[0].outbox_id == id2
    assert store.outbox[id2].worker_id == "worker_2"


def test_cas_update_embedding_matching_hash(store: PgVectorStore):
    """CAS update writes vector when content_hash matches."""
    card = MemoryCard(
        memory_id="mem_cas",
        project="neurons",
        card_type="decision",
        title="CAS",
        summary="CAS",
        content_hash="sha256:valid_hash",
        embedding_state="pending",
    )
    store.insert_card(card)
    job_id = store.enqueue_outbox("memory_card", "mem_cas", "sha256:valid_hash", "Text")

    vec = make_dummy_vector(10)
    success = store.cas_update_embedding(job_id, "mem_cas", "sha256:valid_hash", vec)

    assert success is True
    updated_card = store.get_card("mem_cas")
    assert updated_card.embedding_state == "ready"
    assert updated_card.embedding_revision == 2
    assert updated_card.embedding == vec
    assert store.outbox[job_id].status == "completed"


def test_cas_update_embedding_stale_hash_graceful_discard(store: PgVectorStore):
    """CAS update safely discards stale vector on hash mismatch without corrupting card."""
    card = MemoryCard(
        memory_id="mem_stale_cas",
        project="neurons",
        card_type="decision",
        title="Stale",
        summary="Stale",
        content_hash="sha256:hash_v2",  # Already modified to v2
        embedding_state="pending",
    )
    store.insert_card(card)
    job_id = store.enqueue_outbox("memory_card", "mem_stale_cas", "sha256:hash_v1", "Old Text")

    vec_v1 = make_dummy_vector(1)
    success = store.cas_update_embedding(job_id, "mem_stale_cas", "sha256:hash_v1", vec_v1)

    assert success is False
    unchanged_card = store.get_card("mem_stale_cas")
    assert unchanged_card.embedding is None  # Stale vector was not written!
    assert unchanged_card.embedding_state == "pending"
    assert store.outbox[job_id].status == "completed"
    assert "CAS skip" in store.outbox[job_id].last_error


def test_outbox_mark_failed_exponential_backoff_and_dead_letter(store: PgVectorStore):
    """Failed outbox jobs calculate backoff until max_retries, then escalate to dead_letter."""
    card = MemoryCard(memory_id="card_fail", project="neurons", card_type="decision", title="F", summary="F")
    store.insert_card(card)
    job_id = store.enqueue_outbox("memory_card", "card_fail", "sha256:fail", "Payload")

    for i in range(1, 5):
        store.mark_outbox_failed(job_id, error_message=f"Transient timeout {i}", max_retries=5)
        assert store.outbox[job_id].status == "failed"
        assert store.outbox[job_id].retry_count == i
        assert store.outbox[job_id].lease_until is not None

    # 5th failure -> dead_letter
    store.mark_outbox_failed(job_id, error_message="Fatal unrecoverable", max_retries=5)
    assert store.outbox[job_id].status == "dead_letter"
    assert store.outbox[job_id].retry_count == 5
    assert store.cards["card_fail"].embedding_state == "failed"


# ==============================================================================
# 3. Hybrid Search & GUC Tests
# ==============================================================================

def test_guc_relaxed_order_execution_and_fallback(store: PgVectorStore):
    """relaxed_order GUC sets correctly for >= 0.8.0 and falls back for < 0.8.0."""
    store.pgvector_version = "0.8.0"
    assert store.set_guc_relaxed_order() is True
    assert store.local_guc.get("hnsw.iterative_scan") == "relaxed_order"

    store.pgvector_version = "0.7.4"
    assert store.set_guc_relaxed_order() is False


def test_hybrid_search_metadata_filtering_and_scoring(store: PgVectorStore):
    """hybrid_search enforces project isolation, active status, currentness, and cosine scoring."""
    v1 = make_dummy_vector(100)
    v2 = make_dummy_vector(200)

    # Active card in project neurons
    store.insert_card(
        MemoryCard(
            memory_id="c_act_neurons",
            project="neurons",
            card_type="decision",
            title="Active Neurons",
            summary="Summary 1",
            authorization_status="active",
            currentness="current",
            embedding=v1,
            embedding_state="ready",
        )
    )

    # Disabled card in project neurons
    store.insert_card(
        MemoryCard(
            memory_id="c_dis_neurons",
            project="neurons",
            card_type="decision",
            title="Disabled Neurons",
            summary="Summary 2",
            authorization_status="disabled",
            currentness="current",
            embedding=v1,
            embedding_state="ready",
        )
    )

    # Active card in project alpha
    store.insert_card(
        MemoryCard(
            memory_id="c_act_alpha",
            project="alpha",
            card_type="decision",
            title="Active Alpha",
            summary="Summary 3",
            authorization_status="active",
            currentness="current",
            embedding=v1,
            embedding_state="ready",
        )
    )

    # Search project neurons
    res = store.hybrid_search(project="neurons", query_vector=v1, limit=5)
    assert len(res) == 1
    assert res[0]["memory_id"] == "c_act_neurons"
    assert 0.0 <= res[0]["similarity_score"] <= 1.0


# ==============================================================================
# 4. Cycle-Safe Recursive CTE DAG Traversal Tests
# ==============================================================================

def test_dag_traversal_multi_hop(store: PgVectorStore):
    """Multi-hop lineage from root down 3 levels."""
    for i in range(1, 5):
        store.insert_card(MemoryCard(f"node_{i}", "neurons", "decision", f"N{i}", "S"))

    store.insert_edge(MemoryEdge(1, "node_1", "derived_from", "node_2", "sha256:1"))
    store.insert_edge(MemoryEdge(2, "node_2", "derived_from", "node_3", "sha256:2"))
    store.insert_edge(MemoryEdge(3, "node_3", "derived_from", "node_4", "sha256:3"))

    dag = store.traverse_provenance_dag("node_1", max_depth=5)
    assert len(dag) == 3
    assert [d["depth"] for d in dag] == [1, 2, 3]
    assert dag[2]["dst_id"] == "node_4"


def test_dag_traversal_cycle_prevention_2node_and_3node(store: PgVectorStore):
    """Cycle prevention prunes 2-node cycle (A <-> B) and 3-node cycle (A -> B -> C -> A)."""
    # 2-node cycle
    for name in ("A", "B"):
        store.insert_card(MemoryCard(name, "neurons", "decision", name, "S"))
    store.insert_edge(MemoryEdge(1, "A", "derived_from", "B", "sha256:1"))
    store.insert_edge(MemoryEdge(2, "B", "derived_from", "A", "sha256:2"))

    dag_2node = store.traverse_provenance_dag("A", max_depth=5)
    assert len(dag_2node) == 2  # A->B, B->A, then halts
    assert dag_2node[0]["dst_id"] == "B"

    # 3-node cycle
    s3 = PgVectorStore(use_in_memory=True)
    for name in ("X", "Y", "Z"):
        s3.insert_card(MemoryCard(name, "neurons", "decision", name, "S"))
    s3.insert_edge(MemoryEdge(1, "X", "derived_from", "Y", "sha256:1"))
    s3.insert_edge(MemoryEdge(2, "Y", "derived_from", "Z", "sha256:2"))
    s3.insert_edge(MemoryEdge(3, "Z", "derived_from", "X", "sha256:3"))

    dag_3node = s3.traverse_provenance_dag("X", max_depth=5)
    assert len(dag_3node) == 3  # X->Y, Y->Z, Z->X, then halts


def test_dag_traversal_self_loop(store: PgVectorStore):
    """Self-loop (A -> A) terminates safely at depth 1."""
    store.insert_card(MemoryCard("loop_node", "neurons", "decision", "L", "S"))
    store.insert_edge(MemoryEdge(1, "loop_node", "supports", "loop_node", "sha256:loop"))

    dag = store.traverse_provenance_dag("loop_node", max_depth=5)
    assert len(dag) == 1


def test_dag_traversal_diamond_convergence(store: PgVectorStore):
    """Diamond graph (A -> B -> D, A -> C -> D) visits D via both branches."""
    for n in ("A", "B", "C", "D"):
        store.insert_card(MemoryCard(n, "neurons", "decision", n, "S"))
    store.insert_edge(MemoryEdge(1, "A", "derived_from", "B", "sha256:1"))
    store.insert_edge(MemoryEdge(2, "A", "derived_from", "C", "sha256:2"))
    store.insert_edge(MemoryEdge(3, "B", "derived_from", "D", "sha256:3"))
    store.insert_edge(MemoryEdge(4, "C", "derived_from", "D", "sha256:4"))

    dag = store.traverse_provenance_dag("A", max_depth=5)
    assert len(dag) == 4
    dsts = [d["dst_id"] for d in dag]
    assert dsts.count("D") == 2
