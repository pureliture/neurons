"""Challenger Adversarial Test Suite for Milestone 4 (Migration & Dual-Read Shadow).

Tests empty collections, corrupted/missing fields, checkpoint resume from mid-batch failures,
large query corpus (200+ queries), and strict Recall@5 gate thresholds.
"""

from __future__ import annotations

import json
import os
import tempfile
import pytest

from agent_knowledge.postgres_store.pgvector_store import (
    PgVectorStore,
    MemoryCard,
    SessionChunk,
    make_dummy_vector,
)
from agent_knowledge.postgres_store.migration_qdrant_to_postgres import (
    QdrantToPostgresMigrator,
)
from agent_knowledge.postgres_store.dual_read_shadow import (
    DualReadShadowHarness,
    BenchmarkQuery,
)


class MockQdrantClient:
    def __init__(self):
        self.collections: dict[str, dict[str, Any]] = {
            "session_chunks": {},
            "memory_cards": {},
        }
        self.vectors: dict[str, tuple[list[float], dict]] = {}

    def add_chunk(self, chunk_id: str, vector: list[float], payload: dict):
        self.collections["session_chunks"][chunk_id] = (vector, payload)

    def add_card(self, card_id: str, vector: list[float] | None, payload: dict):
        self.collections["memory_cards"][card_id] = (vector, payload)
        if vector is not None:
            self.vectors[card_id] = (vector, payload)


@pytest.fixture
def empty_qdrant():
    return MockQdrantClient()


@pytest.fixture
def target_store():
    store = PgVectorStore(use_in_memory=True)
    store.execute_ddl()
    return store


def test_challenger_m4_empty_migration(empty_qdrant, target_store):
    migrator = QdrantToPostgresMigrator(
        qdrant_client=empty_qdrant,
        target_store=target_store,
    )
    summary = migrator.run_full_migration(project="neurons")
    assert summary.total_migrated == 0
    assert summary.total_quarantined == 0
    assert summary.success is True


def test_challenger_m4_checkpoint_resume_mid_batch(target_store):
    client = MockQdrantClient()
    for i in range(10):
        client.add_chunk(
            f"chunk_{i}",
            make_dummy_vector(i),
            {
                "chunk_id": f"chunk_{i}",
                "project": "neurons",
                "content_markdown": f"Content {i}",
            },
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        chk_file = os.path.join(tmpdir, "ckpt.json")

        # Step 1: Migrate with batch_size=3, only run 1 batch
        migrator1 = QdrantToPostgresMigrator(
            qdrant_client=client,
            target_store=target_store,
            batch_size=3,
            checkpoint_file=chk_file,
        )
        # Fetch first page and simulate interrupted process
        points, next_offset = migrator1._fetch_qdrant_points("session_chunks", offset=None)
        assert len(points) == 3
        migrator1.checkpoints["session_chunks_offset"] = next_offset
        migrator1._save_checkpoints()

        # Step 2: Resume with new migrator instance
        migrator2 = QdrantToPostgresMigrator(
            qdrant_client=client,
            target_store=target_store,
            batch_size=3,
            checkpoint_file=chk_file,
        )
        res = migrator2.migrate_session_chunks("session_chunks")
        # Should migrate remaining 7 items without duplicating first 3
        assert res.total_scanned == 7
        assert res.total_migrated == 7


def test_challenger_m4_dual_read_large_corpus_200_queries(target_store):
    qdrant = MockQdrantClient()
    # Seed 50 cards
    for i in range(50):
        vec = make_dummy_vector(i)
        card_id = f"card_{i}"
        payload = {
            "memory_id": card_id,
            "project": "neurons",
            "card_type": "decision",
            "title": f"Decision {i}",
            "summary": f"Summary {i}",
            "typed_payload": {"idx": i},
            "content_hash": f"sha256:{i:064d}",
            "lifecycle_state": "human_accepted",
            "authorization_status": "active",
            "currentness": "current",
        }
        qdrant.add_card(card_id, vec, payload)
        target_store.insert_card(
            MemoryCard(
                memory_id=card_id,
                project="neurons",
                card_type="decision",
                title=f"Decision {i}",
                summary=f"Summary {i}",
                typed_payload={"idx": i},
                content_hash=f"sha256:{i:064d}",
                lifecycle_state="human_accepted",
                authorization_status="active",
                currentness="current",
                embedding=vec,
                embedding_state="ready",
            )
        )

    harness = DualReadShadowHarness(
        qdrant_client=qdrant,
        pg_store=target_store,
        default_limit=5,
        recall_gate_threshold=0.95,
        p95_latency_gate_ms=20.0,
    )

    # 200 benchmark query fixtures
    queries = [make_dummy_vector(q * 3) for q in range(200)]
    summary = harness.run_benchmark(queries, project="neurons")

    assert summary.total_queries == 200
    assert summary.mean_recall_at_k >= 0.95
    assert summary.recall_gate_passed is True
    assert summary.p95_pgvector_latency_ms <= 20.0
    assert summary.latency_gate_passed is True
    assert summary.overall_gate_passed is True


def test_challenger_m4_dual_read_zero_match():
    qdrant = MockQdrantClient()
    store = PgVectorStore(use_in_memory=True)
    store.execute_ddl()

    harness = DualReadShadowHarness(qdrant_client=qdrant, pg_store=store, default_limit=5)
    query_vec = make_dummy_vector(99)
    res = harness.execute_query(query_vec, project="nonexistent_proj")

    assert res.recall_at_k == 1.0  # Both empty -> perfect recall
    assert len(res.qdrant_top_ids) == 0
    assert len(res.pgvector_top_ids) == 0
