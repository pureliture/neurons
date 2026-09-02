"""Tests for Dual-Read Shadow Verification Harness & Recall@5 Benchmark (Milestone 4).

Tests dual-read execution, Top-K overlap, Recall@5 threshold gating (>= 0.95),
latency percentile calculations, and report generation.
"""

from __future__ import annotations

import pytest

from agent_knowledge.postgres_store.pgvector_store import (
    PgVectorStore,
    MemoryCard,
    make_dummy_vector,
)
from agent_knowledge.postgres_store.dual_read_shadow import (
    DualReadShadowHarness,
    BenchmarkQuery,
    BenchmarkSummary,
    DualReadComparisonResult,
)


class MockQdrantClient:
    def __init__(self):
        self.vectors: dict[str, tuple[list[float], dict]] = {}

    def upsert(self, point_id: str, vector: list[float], payload: dict):
        self.vectors[point_id] = (vector, payload)


@pytest.fixture
def stores():
    qdrant = MockQdrantClient()
    pg = PgVectorStore(use_in_memory=True)
    pg.execute_ddl()

    # Populate 20 identical cards in both stores
    for i in range(20):
        vec = make_dummy_vector(i)
        card_id = f"card_{i}"
        payload = {
            "memory_id": card_id,
            "project": "neurons",
            "card_type": "decision",
            "title": f"Decision {i}",
            "summary": f"Summary {i}",
            "typed_payload": {"index": i},
            "content_hash": f"sha256:{i:064d}",
            "lifecycle_state": "human_accepted",
            "authorization_status": "active",
            "currentness": "current",
        }
        qdrant.upsert(card_id, vec, payload)
        pg.insert_card(
            MemoryCard(
                memory_id=card_id,
                project="neurons",
                card_type="decision",
                title=f"Decision {i}",
                summary=f"Summary {i}",
                typed_payload={"index": i},
                content_hash=f"sha256:{i:064d}",
                lifecycle_state="human_accepted",
                authorization_status="active",
                currentness="current",
                embedding=vec,
                embedding_state="ready",
            )
        )

    return qdrant, pg


def test_dual_read_single_query(stores):
    qdrant, pg = stores
    harness = DualReadShadowHarness(qdrant_client=qdrant, pg_store=pg, default_limit=5)
    
    query_vec = make_dummy_vector(0)
    res = harness.execute_query(query_vec, project="neurons", limit=5)

    assert res.k == 5
    assert res.recall_at_k >= 0.95
    assert len(res.qdrant_top_ids) == 5
    assert len(res.pgvector_top_ids) == 5
    assert res.overlap_count >= 4


def test_dual_read_benchmark_corpus(stores):
    qdrant, pg = stores
    harness = DualReadShadowHarness(
        qdrant_client=qdrant,
        pg_store=pg,
        default_limit=5,
        recall_gate_threshold=0.95,
        p95_latency_gate_ms=20.0,
    )

    # 10 queries
    fixtures = [make_dummy_vector(i * 2) for i in range(10)]
    summary = harness.run_benchmark(fixtures, project="neurons")

    assert summary.total_queries == 10
    assert summary.mean_recall_at_k >= 0.95
    assert summary.recall_gate_passed is True
    assert summary.p95_pgvector_latency_ms <= 20.0
    assert summary.latency_gate_passed is True
    assert summary.overall_gate_passed is True

    # Report generation
    report = harness.generate_report(summary)
    assert "# Dual-Read Shadow Benchmark Report" in report
    assert "**Gate Status**: **PASSED**" in report



def test_dual_read_divergence_detection(stores):
    qdrant, pg = stores
    harness = DualReadShadowHarness(
        qdrant_client=qdrant,
        pg_store=pg,
        default_limit=5,
        recall_gate_threshold=0.99,  # High threshold to trigger discrepancy reporting
    )

    # Corrupt one store by removing an item
    del pg.cards["card_0"]

    query_vec = make_dummy_vector(0)
    res = harness.execute_query(query_vec, project="neurons", limit=5)

    # Since card_0 was missing in pgvector, recall dropped and discrepancy recorded
    assert res.qdrant_top_ids[0] == "card_0"
    assert "card_0" not in res.pgvector_top_ids
    assert len(res.discrepancies) > 0
