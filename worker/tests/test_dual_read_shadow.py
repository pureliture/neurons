"""Tests for Dual-Read Shadow Verification Harness & Recall@5 Benchmark (Milestone 4).

Tests dual-read execution, Top-K overlap, Recall@5 threshold gating (>= 0.95),
latency percentile calculations, and report generation.
"""

from __future__ import annotations

import pytest
import os
import uuid
from datetime import datetime, timezone
from qdrant_client import QdrantClient, models

from agent_knowledge.postgres_store.pgvector_store import (
    MemoryCard,
    PgVectorStore,
    compute_cosine_similarity,
    make_dummy_vector,
)
from agent_knowledge.postgres_store.dual_read_shadow import (
    DualReadShadowHarness,
    BenchmarkQuery,
    BenchmarkSummary,
    DualReadComparisonResult,
)


class FakePgVectorStore:
    """Explicit test double; production ``PgVectorStore`` remains SQL-only."""

    def __init__(self):
        self.cards: dict[str, MemoryCard] = {}

    def insert_card(self, card: MemoryCard) -> str:
        self.cards[card.memory_id] = card
        return card.memory_id

    def hybrid_search(self, *, project: str, query_vector: list[float], limit: int, **_kwargs):
        scored = []
        for card in self.cards.values():
            if card.project != project or card.authorization_status != "active" or card.currentness != "current":
                continue
            scored.append((compute_cosine_similarity(query_vector, card.embedding), card.memory_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [{"memory_id": memory_id, "score": score} for score, memory_id in scored[:limit]]


@pytest.fixture
def stores():
    qdrant = QdrantClient(":memory:")
    qdrant.create_collection("memory_cards", vectors_config=models.VectorParams(size=3072, distance=models.Distance.COSINE))
    pg = FakePgVectorStore()

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
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_to": None,
        }
        qdrant.upsert("memory_cards", [models.PointStruct(id=i, vector=vec, payload=payload)])
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

    yield qdrant, pg
    qdrant.close()


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

    # The cutover gate requires at least 50 query fixtures, even in the
    # deterministic test harness.
    fixtures = [make_dummy_vector(i * 2) for i in range(50)]
    summary = harness.run_benchmark(fixtures, project="neurons")

    assert summary.total_queries == 50
    assert summary.mean_recall_at_k >= 0.95
    assert summary.recall_gate_passed is True
    assert summary.p95_pgvector_latency_ms <= 20.0
    assert summary.latency_gate_passed is True
    assert summary.overall_gate_passed is False
    assert "test_harness_not_cutover_evidence" in summary.cutover_blockers
    assert summary.sample_size_gate_passed is True

    # Report generation
    report = harness.generate_report(summary)
    assert "# Dual-Read Shadow Benchmark Report" in report
    assert "**Gate Status**: **FAILED**" in report



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


def test_local_qdrant_cannot_claim_live_and_duplicate_fixtures_do_not_count(stores):
    qdrant, pg = stores
    with pytest.raises(ValueError, match="in-memory"):
        DualReadShadowHarness(qdrant, pg, evidence_class="live_cutover")
    harness = DualReadShadowHarness(qdrant, pg, minimum_queries_for_cutover=1)
    summary = harness.run_benchmark([
        BenchmarkQuery(query_id=str(i), query_vector=make_dummy_vector(0)) for i in range(50)
    ])
    assert summary.sample_size_gate_passed is False
    assert summary.overall_gate_passed is False


def test_source_filters_authority_and_half_open_time_before_top_k(stores):
    qdrant, pg = stores
    vector = make_dummy_vector(0)
    base = {"project": "neurons", "lifecycle_state": "accepted", "authorization_status": "active",
            "currentness": "current", "valid_from": "2026-01-01T00:00:00Z", "valid_to": None}
    variations = [
        {"memory_id": "valid"}, {"memory_id": "foreign", "project": "other"},
        {"memory_id": "revoked", "authorization_status": "disabled"},
        {"memory_id": "candidate", "lifecycle_state": "candidate"},
        {"memory_id": "expired", "valid_to": "2026-02-01T00:00:00Z"},
        {"memory_id": "future", "valid_from": "2026-03-01T00:00:00Z"},
        {"memory_id": "historical", "currentness": "superseded", "valid_to": "2026-03-01T00:00:00Z"},
    ]
    qdrant.delete("memory_cards", models.FilterSelector(filter=models.Filter(must=[])))
    qdrant.upsert("memory_cards", [models.PointStruct(id=i, vector=vector, payload={**base, **v}) for i, v in enumerate(variations)])
    ids, _, error = DualReadShadowHarness(qdrant, pg)._query_qdrant(vector, "neurons", 20, as_of="2026-02-01T00:00:00Z")
    assert error is None
    assert set(ids) == {"valid", "historical"}


def test_backend_failure_is_redacted_and_not_empty_success(stores, caplog):
    _, pg = stores
    class FailingClient:
        def query_points(self, **kwargs):
            raise RuntimeError("private-transcript-and-secret")
    result = DualReadShadowHarness(FailingClient(), pg).execute_query(make_dummy_vector(0))
    assert result.qdrant_error == "qdrant_query_failed"
    assert result.recall_at_k == 0
    assert "private-transcript-and-secret" not in caplog.text
    assert "private-transcript-and-secret" not in repr(result)


@pytest.mark.skipif(not os.environ.get("LBRAIN_TEST_PG_DSN"), reason="전용 PostgreSQL integration DSN 필요")
@pytest.mark.parametrize("as_of", [None, "2026-02-01T00:00:00Z"])
def test_shadow_filter_parity_with_real_postgres(stores, as_of):
    qdrant, _ = stores
    pg = PgVectorStore(dsn=os.environ["LBRAIN_TEST_PG_DSN"])
    pg.execute_ddl()
    project = "shadow-" + uuid.uuid4().hex[:10]
    vector = make_dummy_vector(12)
    base = {"project": project, "lifecycle_state": "human_accepted", "authorization_status": "active",
            "currentness": "current", "valid_from": "2026-01-01T00:00:00Z", "valid_to": None}
    variations = [
        {}, {"project": project + "-other"}, {"authorization_status": "disabled"},
        {"lifecycle_state": "candidate"}, {"valid_to": "2026-02-01T00:00:00Z"},
        {"valid_from": "2099-01-01T00:00:00Z"},
        {"currentness": "superseded", "valid_to": "2026-03-01T00:00:00Z"},
    ]
    ids = []
    try:
        for i, variation in enumerate(variations):
            memory_id = f"{project}-{i}"
            ids.append(memory_id)
            payload = {**base, **variation, "memory_id": memory_id}
            qdrant.upsert("memory_cards", [models.PointStruct(id=100 + i, vector=vector, payload=payload)])
            pg.insert_card(MemoryCard(
                memory_id=memory_id, project=payload["project"], card_type="decision", title="검증", summary="검증",
                content_hash=f"sha256:{i:064d}", lifecycle_state=payload["lifecycle_state"],
                authorization_status=payload["authorization_status"], currentness=payload["currentness"],
                valid_from=datetime.fromisoformat(payload["valid_from"]),
                valid_to=datetime.fromisoformat(payload["valid_to"]) if payload["valid_to"] else None,
                embedding=vector, embedding_state="ready",
            ))
        result = DualReadShadowHarness(qdrant, pg).execute_query(BenchmarkQuery(
            query_id="parity", query_vector=vector, project=project, as_of=as_of, limit=20,
        ))
        expected = {ids[0], ids[6]} if as_of else {ids[0]}
        assert set(result.qdrant_top_ids) == expected
        assert set(result.pgvector_top_ids) == expected
        assert result.recall_at_k == 1.0
        assert result.qdrant_error is result.pgvector_error is None
    finally:
        with pg.transaction() as conn:
            conn.execute("DELETE FROM memory_cards WHERE memory_id = ANY(%s)", (ids,))
