"""Unit and contract tests for quarantined group fence safety and dead-letter protection."""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from graphiti_core.driver.driver import GraphProvider
from graphiti_core.models.edges.edge_db_queries import (
    get_entity_edge_save_bulk_query,
    get_entity_edge_save_query,
)
from graphiti_core.models.nodes.node_db_queries import (
    get_entity_node_save_bulk_query,
    get_entity_node_save_query,
)
from agent_knowledge.postgres_store.pgvector_store import OUTBOX_RETRY_LIMIT, PgVectorStore
from agent_knowledge.postgres_store.outbox_worker import LocalEmbeddingOutboxWorker


def test_node_and_edge_save_queries_protect_quarantined_fence() -> None:
    """Verify local save queries reject modifications on quarantined groups and preserve status."""
    node_query = get_entity_node_save_query(GraphProvider.NEO4J, labels=["Entity"], local_mode=True)
    assert "fence_check.status <> 'quarantined'" in node_query
    assert "CASE WHEN fence.status = 'quarantined' THEN 'quarantined' ELSE 'pending' END" in node_query

    node_bulk = get_entity_node_save_bulk_query(GraphProvider.NEO4J, nodes=[], local_mode=True)
    assert "fence_check.status <> 'quarantined'" in node_bulk
    assert "CASE WHEN fence.status = 'quarantined' THEN 'quarantined' ELSE 'pending' END" in node_bulk

    edge_query = get_entity_edge_save_query(GraphProvider.NEO4J, local_mode=True)
    assert "fence_check.status <> 'quarantined'" in edge_query
    assert "CASE WHEN fence.status = 'quarantined' THEN 'quarantined' ELSE 'pending' END" in edge_query

    edge_bulk = get_entity_edge_save_bulk_query(GraphProvider.NEO4J, local_mode=True)
    assert "fence_check.status <> 'quarantined'" in edge_bulk
    assert "CASE WHEN fence.status = 'quarantined' THEN 'quarantined' ELSE 'pending' END" in edge_bulk


def test_claim_local_outbox_leases_signature_and_worker_propagation() -> None:
    """Verify claim_local_outbox_leases accepts max_retries and worker propagates it."""
    sig = inspect.signature(PgVectorStore.claim_local_outbox_leases)
    assert "max_retries" in sig.parameters
    assert sig.parameters["max_retries"].default == OUTBOX_RETRY_LIMIT

    mock_store = MagicMock()
    mock_store.claim_local_outbox_leases.return_value = []
    worker = LocalEmbeddingOutboxWorker(
        store=mock_store,
        worker_id="test_worker",
        batch_size=5,
        lease_seconds=15,
        max_retries=10,
        embedding_fn=lambda text: [0.1] * 256,
    )
    worker.run_once()

    mock_store.claim_local_outbox_leases.assert_called_once_with(
        worker_id="test_worker",
        batch_size=5,
        lease_seconds=15,
        profile_id="lbrain-memory-model2vec-256-v1",
        max_retries=10,
    )


def test_enqueue_local_outbox_preserves_dead_letter_sql() -> None:
    """Verify _enqueue_local_outbox_on SQL preserves existing dead_letter state."""
    # Check that _enqueue_local_outbox_on implementation checks dead_letter
    code = inspect.getsource(PgVectorStore._enqueue_local_outbox_on)
    assert "THEN 'dead_letter'" in code
    assert "'dead_letter'" in code
    assert "status IN ('queued', 'processing', 'failed', 'dead_letter')" in code
