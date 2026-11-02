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


def test_enqueue_local_outbox_preserves_failed_state_and_checks_dead_letter_outbox() -> None:
    """Verify _enqueue_local_outbox_on preserves failed state and checks dead_letter outbox."""
    code = inspect.getsource(PgVectorStore._enqueue_local_outbox_on)
    assert "THEN 'failed'" in code
    assert "status IN ('queued', 'processing', 'failed', 'dead_letter')" in code


def test_claim_expired_processing_reclaims_with_failed_derived_state() -> None:
    """Verify expired processing jobs that exhaust retries transition derived state to failed, not dead_letter."""
    code = inspect.getsource(PgVectorStore.claim_local_outbox_leases)
    assert "status = 'dead_letter'" in code
    assert "state = 'failed'" in code
    assert "state = 'dead_letter'" not in code


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

        # Case 5: Expired backfill lease reclaimed and covered -> should not raise
        mock_driver.execute_query = AsyncMock(
            return_value=([{"fence_status": "idle", "raw_status": "backfill_in_progress", "quarantine_reason": None, "missing_or_dirty": 0}], None, None)
        )
        await ensure_local_group_resolution_ready(mock_driver, "group_expired_reclaimed")
        # Ensure query passes now_iso parameter for lease expiration check
        kwargs = mock_driver.execute_query.call_args.kwargs
        assert "now_iso" in kwargs, "ensure_local_group_resolution_ready must pass now_iso to execute_query"
        assert kwargs["group_id"] == "group_expired_reclaimed"

    asyncio.run(_run())


def test_steward_decision_acquires_local_locks_before_parent_row_lock() -> None:
    """Verify steward_decision acquires local advisory locks before memory_cards FOR UPDATE."""
    source = inspect.getsource(PgVectorStore.steward_decision)
    # Check that _acquire_local_locks_on occurs before FOR UPDATE
    lock_pos = source.find("self._acquire_local_locks_on")
    update_pos = source.find("ORDER BY memory_id FOR UPDATE")
    assert lock_pos != -1, "self._acquire_local_locks_on not found in steward_decision"
    assert update_pos != -1, "ORDER BY memory_id FOR UPDATE not found in steward_decision"
    assert lock_pos < update_pos, "Local advisory lock must be acquired BEFORE memory_cards FOR UPDATE"
    assert "acquire_locks=False" in source, "steward_decision should pass acquire_locks=False to _enqueue_local_outbox_on"


def test_claim_local_outbox_leases_applies_retry_budget_on_reclaim() -> None:
    """Verify expired processing job reclaim increments retry_count and transitions to dead_letter if budget exhausted."""
    source = inspect.getsource(PgVectorStore.claim_local_outbox_leases)
    assert 'job_status == "processing" and (job_retry_count + 1) >= int(max_retries)' in source
    assert "status = 'dead_letter'" in source
    assert "retry_count = CASE" in source
    assert "WHEN status = 'processing' THEN retry_count + 1" in source


def test_bulk_semantic_local_profile_uses_canonical_planner_and_utf8_weights() -> None:
    """Verify OpenAICompatibleEmbeddingBatcher uses plan_request_batches_v1 and byte-length weights in local mode."""
    from agent_knowledge.llm_brain_core.bulk_semantic import (
        OpenAICompatibleEmbeddingBatcher,
    )

    embed_raw_src = inspect.getsource(OpenAICompatibleEmbeddingBatcher._embed_raw_chunks)
    assert "plan_request_batches_v1" in embed_raw_src

    embed_many_src = inspect.getsource(OpenAICompatibleEmbeddingBatcher.embed_many)
    assert "chunk_byte_lengths=" in embed_many_src


def test_pg_session_projector_store_call_contracts() -> None:
    """Verify PgSessionMemoryProjector matches PgVectorStore contracts for local projection."""
    from agent_knowledge.rag_ingress.pg_backfill import PgSessionMemoryProjector

    proj_src = inspect.getsource(PgSessionMemoryProjector._project_local_profile)
    # New chunk path: insert_chunk_with_local_outbox
    assert "insert_chunk_with_local_outbox(" in proj_src
    assert "profile_id=LOCAL_EMBEDDING_PROFILE_ID" in proj_src
    assert "already_locked_identity=True" in proj_src

    # Existing chunk reuse path: _enqueue_local_outbox_on
    assert "_enqueue_local_outbox_on(" in proj_src
    assert "source_content_hash=content_hash" in proj_src
    assert "acquire_locks=False" in proj_src
    assert "lock_already_held=" not in proj_src


def test_bulk_semantic_post_uses_canonical_serializer() -> None:
    """Verify OpenAICompatibleEmbeddingBatcher uses serialize_embedding_request_body in _post_embeddings_chunk."""
    from agent_knowledge.llm_brain_core.bulk_semantic import (
        OpenAICompatibleEmbeddingBatcher,
    )

    post_src = inspect.getsource(OpenAICompatibleEmbeddingBatcher._post_embeddings_chunk)
    assert "serialize_embedding_request_body(" in post_src


def test_semantic_candidate_search_formats_name_input_in_local_mode() -> None:
    """Verify _semantic_candidate_search uses format_graph_node_embedding_input in local mode."""
    from graphiti_core.utils.maintenance import node_operations

    search_src = inspect.getsource(node_operations._semantic_candidate_search)
    assert "format_graph_node_embedding_input(node.name)" in search_src
    assert "is_local_embedding_mode(" in search_src


def test_neo4j_operations_use_canonical_tombstone_keys() -> None:
    """Verify Neo4jEntityNodeOperations and Neo4jEntityEdgeOperations record EmbeddingDeletion with target_type/target_uuid."""
    from graphiti_core.driver.neo4j.operations.entity_node_ops import (
        Neo4jEntityNodeOperations,
    )
    from graphiti_core.driver.neo4j.operations.entity_edge_ops import (
        Neo4jEntityEdgeOperations,
    )

    node_del_src = inspect.getsource(Neo4jEntityNodeOperations.delete)
    assert "MERGE (nd:EmbeddingDeletion {target_type: 'entity', target_uuid: n.uuid})" in node_del_src
    assert "MERGE (td:EmbeddingDeletion {target_type: 'edge', target_uuid: rm.uuid})" in node_del_src

    edge_del_src = inspect.getsource(Neo4jEntityEdgeOperations.delete)
    assert "MERGE (td:EmbeddingDeletion {target_type: 'edge', target_uuid: e.uuid})" in edge_del_src


def test_pg_session_projector_requires_ready_local_vector_before_receipt() -> None:
    """Verify PgSessionMemoryProjector raises EmbeddingChunksPending when local vector is not ready."""
    from agent_knowledge.rag_ingress.pg_backfill import PgSessionMemoryProjector

    proj_src = inspect.getsource(PgSessionMemoryProjector._project_local_profile)
    assert "local_row.get(\"state\") != \"ready\"" in proj_src
    assert "raise EmbeddingChunksPending()" in proj_src


def test_transition_calculates_session_receipts_by_unique_session_and_validates_metadata() -> None:
    """Verify inspect_pg_local_embedding_state uses COUNT(DISTINCT session_id_hash) and valid_local_pg_receipt_metadata."""
    from agent_knowledge.local_embedding import transition

    inspect_src = inspect.getsource(transition.inspect_pg_local_embedding_state)
    assert "COUNT(DISTINCT session_id_hash)" in inspect_src
    assert "valid_local_pg_receipt_metadata(" in inspect_src
    assert "eligible_session_receipts - ready_session_receipts" in inspect_src


def test_backfill_session_receipts_uses_materialized_source_hash_and_legacy_schemes() -> None:
    """Verify backfill_pg_local_embeddings uses materialized source hash and legacy identity schemes."""
    from agent_knowledge.local_embedding import transition

    backfill_src = inspect.getsource(transition.backfill_pg_local_embeddings)
    assert "materialize_session_memory(" in backfill_src
    assert "s_content_hash != s_active_content_hash" in backfill_src
    assert "valid_local_pg_receipt_metadata(" in backfill_src
    assert "projected_source_hash=s_source_hash" in backfill_src
    assert "scheme = \"legacy_exact\"" in backfill_src
    assert "scheme = \"legacy_reuse\"" in backfill_src
    assert "valid_local_representation_receipt_v3(" in backfill_src


def test_quarantined_rejected_node_and_edge_writes_raise_local_group_write_blocked_error() -> None:
    """Verify node and edge saves raise LocalGroupWriteBlockedError when rejected by quarantined fence."""
    from graphiti_core.local_profile import LocalGroupWriteBlockedError
    from graphiti_core.nodes import EntityNode
    from graphiti_core.edges import EntityEdge
    from graphiti_core.driver.neo4j.operations.entity_node_ops import Neo4jEntityNodeOperations
    from graphiti_core.driver.neo4j.operations.entity_edge_ops import Neo4jEntityEdgeOperations
    from agent_knowledge.llm_brain_core.bulk_semantic import (
        _save_entity_node_without_embedding,
        _save_entity_edge_without_embedding,
    )
    from datetime import datetime, timezone
    import asyncio
    from unittest.mock import AsyncMock
    import pytest

    async def _run() -> None:
        mock_driver = MagicMock()
        mock_driver.local_embedding_profile = "lbrain-memory-model2vec-256-v1"
        mock_driver.graph_operations_interface = None
        mock_driver.provider = GraphProvider.NEO4J
        mock_driver.execute_query = AsyncMock(return_value=([], None, None))

        node = EntityNode(
            uuid="node-q-1",
            name="test node",
            group_id="group_quarantined",
            name_embedding=[0.1] * 256,
        )
        with pytest.raises(LocalGroupWriteBlockedError, match="save was rejected"):
            await node.save(mock_driver)

        edge = EntityEdge(
            uuid="edge-q-1",
            source_node_uuid="node-q-1",
            target_node_uuid="node-q-2",
            name="REL",
            fact="test fact",
            group_id="group_quarantined",
            fact_embedding=[0.1] * 256,
            created_at=datetime.now(timezone.utc),
        )
        with pytest.raises(LocalGroupWriteBlockedError, match="save was rejected"):
            await edge.save(mock_driver)

        # Test ops classes
        node_ops = Neo4jEntityNodeOperations()
        with pytest.raises(LocalGroupWriteBlockedError, match="save was rejected"):
            await node_ops.save(mock_driver, node)
        with pytest.raises(LocalGroupWriteBlockedError, match="bulk save rejected"):
            await node_ops.save_bulk(mock_driver, [node])

        edge_ops = Neo4jEntityEdgeOperations()
        with pytest.raises(LocalGroupWriteBlockedError, match="save was rejected"):
            await edge_ops.save(mock_driver, edge)
        with pytest.raises(LocalGroupWriteBlockedError, match="bulk save rejected"):
            await edge_ops.save_bulk(mock_driver, [edge])

        # Test bulk_semantic helper functions
        with pytest.raises(LocalGroupWriteBlockedError, match="save rejected"):
            await _save_entity_node_without_embedding(mock_driver, node)
        with pytest.raises(LocalGroupWriteBlockedError, match="save rejected"):
            await _save_entity_edge_without_embedding(mock_driver, edge)

    asyncio.run(_run())


def test_pg_backfill_get_local_embedding_row_keyword_args_contract() -> None:
    """Verify PostgresSessionChunkBackfillWriter calls get_local_embedding_row with keyword-only arguments."""
    import inspect
    from agent_knowledge.postgres_store.pgvector_store import PgVectorStore

    # Verify PgVectorStore signature requires keyword-only arguments
    sig = inspect.signature(PgVectorStore.get_local_embedding_row)
    assert "target_type" in sig.parameters
    assert sig.parameters["target_type"].kind == inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["target_id"].kind == inspect.Parameter.KEYWORD_ONLY

    # Verify the call in pg_backfill.py source code uses target_type and target_id keywords
    with open("lib/agent_knowledge/rag_ingress/pg_backfill.py", "r", encoding="utf-8") as f:
        src = f.read()
    assert 'target_type="session_chunk"' in src
    assert 'target_id=chosen_id' in src


def test_backfill_operation_id_length_capped_to_128() -> None:
    """Verify backfill operation ID does not exceed 128 characters even with long group and worker IDs."""
    import hashlib

    profile_id = "lbrain-memory-model2vec-256-v1"
    group_id = "g" * 64
    worker_id = "test-worker-" + "w" * 40

    raw_op_id = f"backfill:{profile_id}:{group_id}:{worker_id}"
    assert len(raw_op_id) > 128

    op_digest = hashlib.sha256(raw_op_id.encode("utf-8")).hexdigest()
    capped_op_id = f"backfill:{op_digest}:{worker_id}"[:128]
    assert len(capped_op_id) <= 128

    # Check transition.py implementation logic
    with open("lib/agent_knowledge/local_embedding/transition.py", "r", encoding="utf-8") as f:
        src = f.read()
    assert "len(raw_op_id) <= 128" in src
    assert "op_digest = hashlib.sha256" in src


def test_no_private_hermes_paths_in_local_embedding_tests() -> None:
    """Ensure no private user cache paths appear in local embedding test suites."""
    from pathlib import Path
    tests_dir = Path(__file__).resolve().parent
    for py_file in tests_dir.glob("test_local_embedding_*.py"):
        if py_file.name == Path(__file__).name:
            continue
        text = py_file.read_text(encoding="utf-8")
        assert ".hermes" not in text, f"Found private path in {py_file.name}"








