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


def test_pg_recall_local_embedding_contract_does_not_require_embedding_model() -> None:
    """Verify pg_recall validates local_emb without requiring embedding_model match."""
    from agent_knowledge.rag_ingress import pg_recall
    recall_source = inspect.getsource(pg_recall)
    assert "local_emb.get(\"embedding_model\")" not in recall_source


def test_transition_supports_historical_superseded_cards() -> None:
    """Verify backfill and cutover inspect queries include superseded cards."""
    from agent_knowledge.local_embedding import transition
    trans_source = inspect.getsource(transition)
    assert "currentness IN ('current', 'superseded')" in trans_source


def test_ensure_local_group_resolution_ready_blocks_quarantined_and_uncovered() -> None:
    """Verify local entity resolution is blocked when group is quarantined, busy, or has missing embeddings."""
    import asyncio
    from unittest.mock import AsyncMock
    import pytest
    from graphiti_core.local_profile import (
        LocalGroupResolutionBlockedError,
        ensure_local_group_resolution_ready,
    )

    async def _run() -> None:
        mock_driver = MagicMock()

        # Case 1: Quarantined
        mock_driver.execute_query = AsyncMock(
            return_value=([{"fence_status": "quarantined", "quarantine_reason": "conflict", "missing_or_dirty": 0}], None, None)
        )
        with pytest.raises(LocalGroupResolutionBlockedError, match="quarantined"):
            await ensure_local_group_resolution_ready(mock_driver, "group_q")

        # Case 2: Busy
        mock_driver.execute_query = AsyncMock(
            return_value=([{"fence_status": "backfill_in_progress", "quarantine_reason": None, "missing_or_dirty": 0}], None, None)
        )
        with pytest.raises(LocalGroupResolutionBlockedError, match="busy"):
            await ensure_local_group_resolution_ready(mock_driver, "group_busy")

        # Case 3: Missing local embeddings
        mock_driver.execute_query = AsyncMock(
            return_value=([{"fence_status": "idle", "quarantine_reason": None, "missing_or_dirty": 3}], None, None)
        )
        with pytest.raises(LocalGroupResolutionBlockedError, match="without ready local embeddings"):
            await ensure_local_group_resolution_ready(mock_driver, "group_missing")

        # Case 4: Fully covered and idle
        mock_driver.execute_query = AsyncMock(
            return_value=([{"fence_status": "idle", "quarantine_reason": None, "missing_or_dirty": 0}], None, None)
        )
        # Should not raise
        await ensure_local_group_resolution_ready(mock_driver, "group_ok")

    asyncio.run(_run())

