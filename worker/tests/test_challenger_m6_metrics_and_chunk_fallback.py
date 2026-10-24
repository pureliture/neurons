"""Adversarial challenge test suite for Milestone M6: R6 Metrics Endpoint & Chunk Fallback Invariants.

Challenger: challenger_m6_2
Commit verified: eccc5cf

Verifications:
1. get_temporal_coverage_metrics:
   - Project with 0 items: ZeroDivisionError safe (unknown_ratio=0.0, coverage_ratio=0.0)
     across both PgVectorStore (SQL row None, row (0,0), row (0, None))
     and KnowledgeSearchService (empty read_model, None read_model)
   - Project with 100% missing observed_at: unknown_ratio=1.0, coverage_ratio=0.0
   - Project with 100% present observed_at: unknown_ratio=0.0, coverage_ratio=1.0
   - Project with mixed coverage (fractional ratios with 4-decimal precision rounding)
   - Object/model card instances with observed_at attributes
2. Chunk fallback (pg_recall._search):
   - Fallback trigger: fallback_reason == 'no_current_match' when current matches are empty
   - Invariant: historical currentness (superseded, stale, retired) is NEVER mutated or faked to 'current'
   - Invariant: temporal block currentness also strictly preserves historical currentness
   - No fallback_reason when current matches exist
   - No fallback_reason when caller explicitly requests include_historical=True
   - Future/anomalous chunk exclusion (never fallback to anomalous/future chunks)
   - Empty candidate pool safety
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_knowledge.knowledge_search_service import KnowledgeSearchService
from agent_knowledge.postgres_store.pgvector_store import PgVectorStore
from agent_knowledge.rag_ingress import pg_recall


# ==============================================================================
# Helper Mock Structures
# ==============================================================================

class _FakeCursor:
    def __init__(self, row: tuple[Any, ...] | None):
        self._row = row

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.last_sql = sql
        self.last_params = params

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class _FakeDb:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor


class _MockPgStore(PgVectorStore):
    def __init__(self, row: tuple[Any, ...] | None):
        self._cursor = _FakeCursor(row)

    @contextmanager
    def _scope(self, conn: Any = None):
        yield _FakeDb(self._cursor)


def _setup_mock_pg_recall(mock_hits: list[dict[str, Any]]):
    mock_store = MagicMock()

    def _search_chunks(query_vector: list[float], project: str, limit: int):
        candidates = []
        for idx, h in enumerate(mock_hits):
            content = h.get("content", f"Content {h['chunk_id']}")
            chash = pg_recall.sha256_hash(content)
            candidates.append({
                "chunk_id": h["chunk_id"],
                "distance": float(h.get("distance", 0.1)),
                "session_id_hash": f"sess-hash-{idx}",
                "content_hash": chash,
                "content_markdown": content,
                "provider": "test-provider",
                "project": project,
            })
        return candidates

    mock_store.search_session_chunks.side_effect = _search_chunks

    def _get_chunk(chunk_id: str):
        for idx, h in enumerate(mock_hits):
            if h["chunk_id"] == chunk_id:
                row = MagicMock()
                row.chunk_id = h["chunk_id"]
                row.embedding_state = "ready"
                row.embedding_model = "test-model"
                row.embedding = [0.1] * 16
                row.project = "test-proj"
                row.content_markdown = h.get("content", f"Content {chunk_id}")
                row.content_hash = pg_recall.sha256_hash(row.content_markdown)
                row.session_id_hash = f"sess-hash-{idx}"
                row.provider = "test-provider"
                row.observed_at = h.get("observed_at")
                row.created_at = h.get("created_at")
                row.authority_currentness = h.get("authority_currentness", "current")
                return row
        return None

    mock_store.get_chunk.side_effect = _get_chunk
    mock_embed = MagicMock()
    mock_embed.model = "test-model"
    mock_embed.size = 16
    mock_embed.embed.return_value = [0.1] * 16

    environ = {
        "NEURON_LBRAIN_PGVECTOR_DSN": "postgresql://mock/db",
        "PG_RECALL_DIRECT_MIGRATED": "true",
    }
    return mock_store, mock_embed, environ


# ==============================================================================
# Suite 1: FR-AGE-064 Temporal Coverage Metrics Stress
# ==============================================================================

class TestTemporalCoverageMetrics:
    @pytest.mark.parametrize(
        "row_val,expected_total,expected_with_obs,expected_missing,expected_unknown_r,expected_cov_r",
        [
            # 1. Zero items: SQL row is None
            (None, 0, 0, 0, 0.0, 0.0),
            # 2. Zero items: SQL row is (0, 0)
            ((0, 0), 0, 0, 0, 0.0, 0.0),
            # 3. Zero items: SQL row has None count (0, None)
            ((0, None), 0, 0, 0, 0.0, 0.0),
            # 4. 100% missing observed_at
            ((50, 0), 50, 0, 50, 1.0, 0.0),
            # 5. 100% present observed_at
            ((80, 80), 80, 80, 0, 0.0, 1.0),
            # 6. Mixed coverage: 1 of 3 (unknown=2/3 -> 0.6667, cov=1/3 -> 0.3333)
            ((3, 1), 3, 1, 2, 0.6667, 0.3333),
            # 7. Mixed coverage: 3 of 7 (unknown=4/7 -> 0.5714, cov=3/7 -> 0.4286)
            ((7, 3), 7, 3, 4, 0.5714, 0.4286),
            # 8. Mixed coverage: 50 of 200 (unknown=150/200 -> 0.75, cov=50/200 -> 0.25)
            ((200, 50), 200, 50, 150, 0.75, 0.25),
        ],
    )
    def test_pgvector_store_coverage_metrics_matrix(
        self,
        row_val: tuple[Any, ...] | None,
        expected_total: int,
        expected_with_obs: int,
        expected_missing: int,
        expected_unknown_r: float,
        expected_cov_r: float,
    ):
        store = _MockPgStore(row_val)
        metrics = store.get_temporal_coverage_metrics("proj-metrics")

        assert metrics["project"] == "proj-metrics"
        assert metrics["total_items"] == expected_total
        assert metrics["with_observed_at"] == expected_with_obs
        assert metrics["missing_observed_at"] == expected_missing
        assert metrics["unknown_ratio"] == expected_unknown_r
        assert metrics["coverage_ratio"] == expected_cov_r

    def test_knowledge_search_service_zero_items_no_division_by_zero(self):
        """KnowledgeSearchService: total=0 yields unknown_ratio=0.0, coverage_ratio=0.0 without ZeroDivisionError."""
        service = KnowledgeSearchService(
            ledger=MagicMock(),
            retired_index_bridge=None,
            dataset_ids=[],
            pgvector_store=None,
        )
        mock_read_model = MagicMock()
        mock_read_model.list_accepted_cards.return_value = []
        service._brain_card_cache._read_model = mock_read_model

        metrics = service.get_temporal_coverage_metrics("empty-proj")
        assert metrics["project"] == "empty-proj"
        assert metrics["total_items"] == 0
        assert metrics["with_observed_at"] == 0
        assert metrics["missing_observed_at"] == 0
        assert metrics["unknown_ratio"] == 0.0
        assert metrics["coverage_ratio"] == 0.0

    def test_knowledge_search_service_none_read_model(self):
        """KnowledgeSearchService: absent _read_model yields empty metrics without error."""
        service = KnowledgeSearchService(
            ledger=MagicMock(),
            retired_index_bridge=None,
            dataset_ids=[],
            pgvector_store=None,
        )
        service._brain_card_cache._read_model = None

        metrics = service.get_temporal_coverage_metrics("absent-model-proj")
        assert metrics["total_items"] == 0
        assert metrics["unknown_ratio"] == 0.0
        assert metrics["coverage_ratio"] == 0.0

    def test_knowledge_search_service_100_percent_missing(self):
        """KnowledgeSearchService: 100% missing observed_at yields unknown_ratio=1.0, coverage_ratio=0.0."""
        service = KnowledgeSearchService(
            ledger=MagicMock(),
            retired_index_bridge=None,
            dataset_ids=[],
            pgvector_store=None,
        )
        cards = [{"memory_id": f"card-{i}"} for i in range(25)]
        mock_read_model = MagicMock()
        mock_read_model.list_accepted_cards.return_value = cards
        service._brain_card_cache._read_model = mock_read_model

        metrics = service.get_temporal_coverage_metrics("missing-all-proj")
        assert metrics["total_items"] == 25
        assert metrics["with_observed_at"] == 0
        assert metrics["missing_observed_at"] == 25
        assert metrics["unknown_ratio"] == 1.0
        assert metrics["coverage_ratio"] == 0.0

    def test_knowledge_search_service_mixed_coverage_with_objects(self):
        """KnowledgeSearchService: mixed card dicts and object instances with observed_at attributes."""
        class MockCardObject:
            def __init__(self, observed_at: str | None):
                self.observed_at = observed_at

        service = KnowledgeSearchService(
            ledger=MagicMock(),
            retired_index_bridge=None,
            dataset_ids=[],
            pgvector_store=None,
        )
        cards = [
            MockCardObject("2026-10-24T00:00:00Z"),
            MockCardObject(None),
            {"memory_id": "c1", "observed_at": "2026-10-23T00:00:00Z"},
            {"memory_id": "c2", "observed_at": None},
            {"memory_id": "c3"},  # key absent
        ]
        mock_read_model = MagicMock()
        mock_read_model.list_accepted_cards.return_value = cards
        service._brain_card_cache._read_model = mock_read_model

        metrics = service.get_temporal_coverage_metrics("mixed-proj")
        assert metrics["total_items"] == 5
        assert metrics["with_observed_at"] == 2
        assert metrics["missing_observed_at"] == 3
        assert metrics["unknown_ratio"] == 0.6
        assert metrics["coverage_ratio"] == 0.4


# ==============================================================================
# Suite 2: FR-AGE-022 Chunk Recall Fallback Invariants
# ==============================================================================

class TestChunkRecallFallbackInvariants:
    def test_chunk_fallback_reason_when_no_current_match(self):
        """FR-AGE-022: When no current matches exist at as_of, past chunks are returned with fallback_reason='no_current_match'."""
        now = datetime.now(timezone.utc)
        past_chunks = [
            {
                "chunk_id": "chunk-past-sup",
                "content": "Superseded past chunk",
                "observed_at": (now - timedelta(days=30)).isoformat(),
                "created_at": (now - timedelta(days=30)).isoformat(),
                "authority_currentness": "superseded",
            },
            {
                "chunk_id": "chunk-past-stale",
                "content": "Stale past chunk",
                "observed_at": (now - timedelta(days=90)).isoformat(),
                "created_at": (now - timedelta(days=90)).isoformat(),
                "authority_currentness": "stale",
            },
            {
                "chunk_id": "chunk-past-ret",
                "content": "Retired past chunk",
                "observed_at": (now - timedelta(days=200)).isoformat(),
                "created_at": (now - timedelta(days=200)).isoformat(),
                "authority_currentness": "retired",
            },
        ]
        mock_store, mock_embed, environ = _setup_mock_pg_recall(past_chunks)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pg_recall, "PgVectorStore", lambda **kw: mock_store)
            mp.setattr("agent_knowledge.rag_ingress.qdrant_embedding.build_openai_embedding_provider", lambda **kw: mock_embed)
            mp.setattr(pg_recall, "validated_pg_embedding_profile", lambda *a: None)
            mp.setattr(pg_recall, "assert_pg_embedding_egress_safe", lambda *a: None)

            search = pg_recall.build_pg_brain_query_search_from_env(environ)
            assert search is not None

            results = search("query", "/project/test-proj", as_of=now.isoformat())
            assert len(results) == 3

            for hit in results:
                # 1. Fallback reason is explicitly assigned
                assert hit["fallback_reason"] == "no_current_match"

    def test_chunk_fallback_does_not_mutate_or_fake_currentness(self):
        """FR-AGE-022 / Section 7.2 Invariant: Fallback does NOT mutate or fake historical currentness to 'current'."""
        now = datetime.now(timezone.utc)
        past_chunks = [
            {
                "chunk_id": "chunk-past-sup",
                "content": "Superseded past chunk",
                "observed_at": (now - timedelta(days=30)).isoformat(),
                "created_at": (now - timedelta(days=30)).isoformat(),
                "authority_currentness": "superseded",
            },
            {
                "chunk_id": "chunk-past-stale",
                "content": "Stale past chunk",
                "observed_at": (now - timedelta(days=90)).isoformat(),
                "created_at": (now - timedelta(days=90)).isoformat(),
                "authority_currentness": "stale",
            },
            {
                "chunk_id": "chunk-past-ret",
                "content": "Retired past chunk",
                "observed_at": (now - timedelta(days=200)).isoformat(),
                "created_at": (now - timedelta(days=200)).isoformat(),
                "authority_currentness": "retired",
            },
        ]
        mock_store, mock_embed, environ = _setup_mock_pg_recall(past_chunks)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pg_recall, "PgVectorStore", lambda **kw: mock_store)
            mp.setattr("agent_knowledge.rag_ingress.qdrant_embedding.build_openai_embedding_provider", lambda **kw: mock_embed)
            mp.setattr(pg_recall, "validated_pg_embedding_profile", lambda *a: None)
            mp.setattr(pg_recall, "assert_pg_embedding_egress_safe", lambda *a: None)

            search = pg_recall.build_pg_brain_query_search_from_env(environ)
            assert search is not None

            results = search("query", "/project/test-proj", as_of=now.isoformat())
            hits_by_id = {h["memory_id"]: h for h in results}

            # Invariant: Each past chunk strictly retains its true authority currentness
            assert hits_by_id["chunk-past-sup"]["currentness"] == "superseded"
            assert hits_by_id["chunk-past-sup"]["temporal"]["currentness"] == "superseded"

            assert hits_by_id["chunk-past-stale"]["currentness"] == "stale"
            assert hits_by_id["chunk-past-stale"]["temporal"]["currentness"] == "stale"

            assert hits_by_id["chunk-past-ret"]["currentness"] == "retired"
            assert hits_by_id["chunk-past-ret"]["temporal"]["currentness"] == "retired"

            # Strict negative invariant: NEVER 'current'
            for hit in results:
                assert hit["currentness"] != "current"
                assert hit["temporal"]["currentness"] != "current"

    def test_chunk_recall_no_fallback_reason_when_current_matches_exist(self):
        """When current matches exist, fallback_reason must NOT be present on any hit."""
        now = datetime.now(timezone.utc)
        chunks = [
            {
                "chunk_id": "chunk-curr",
                "content": "Active current chunk",
                "observed_at": (now - timedelta(hours=3)).isoformat(),
                "created_at": (now - timedelta(hours=3)).isoformat(),
                "authority_currentness": "current",
            },
            {
                "chunk_id": "chunk-past",
                "content": "Superseded past chunk",
                "observed_at": (now - timedelta(days=20)).isoformat(),
                "created_at": (now - timedelta(days=20)).isoformat(),
                "authority_currentness": "superseded",
            },
        ]
        mock_store, mock_embed, environ = _setup_mock_pg_recall(chunks)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pg_recall, "PgVectorStore", lambda **kw: mock_store)
            mp.setattr("agent_knowledge.rag_ingress.qdrant_embedding.build_openai_embedding_provider", lambda **kw: mock_embed)
            mp.setattr(pg_recall, "validated_pg_embedding_profile", lambda *a: None)
            mp.setattr(pg_recall, "assert_pg_embedding_egress_safe", lambda *a: None)

            search = pg_recall.build_pg_brain_query_search_from_env(environ)
            assert search is not None

            results = search("query", "/project/test-proj", as_of=now.isoformat())
            assert len(results) == 1
            hit = results[0]
            assert hit["memory_id"] == "chunk-curr"
            assert hit["currentness"] == "current"
            assert "fallback_reason" not in hit

    def test_chunk_recall_no_fallback_reason_under_explicit_historical(self):
        """When include_historical=True, both current and past are returned without fallback_reason."""
        now = datetime.now(timezone.utc)
        chunks = [
            {
                "chunk_id": "chunk-sup",
                "content": "Superseded chunk",
                "observed_at": (now - timedelta(days=20)).isoformat(),
                "created_at": (now - timedelta(days=20)).isoformat(),
                "authority_currentness": "superseded",
            },
        ]
        mock_store, mock_embed, environ = _setup_mock_pg_recall(chunks)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pg_recall, "PgVectorStore", lambda **kw: mock_store)
            mp.setattr("agent_knowledge.rag_ingress.qdrant_embedding.build_openai_embedding_provider", lambda **kw: mock_embed)
            mp.setattr(pg_recall, "validated_pg_embedding_profile", lambda *a: None)
            mp.setattr(pg_recall, "assert_pg_embedding_egress_safe", lambda *a: None)

            search = pg_recall.build_pg_brain_query_search_from_env(environ)
            assert search is not None

            results = search("query", "/project/test-proj", as_of=now.isoformat(), include_historical=True)
            assert len(results) == 1
            assert "fallback_reason" not in results[0]

    def test_chunk_recall_excludes_anomalous_chunk_from_fallback(self):
        """Anomalous or future chunks are strictly excluded from both current and fallback results."""
        now = datetime.now(timezone.utc)
        chunks = [
            {
                "chunk_id": "chunk-future-anom",
                "content": "Future corrupted chunk",
                "observed_at": (now + timedelta(days=10)).isoformat(),
                "created_at": now.isoformat(),
                "authority_currentness": "superseded",
            },
        ]
        mock_store, mock_embed, environ = _setup_mock_pg_recall(chunks)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pg_recall, "PgVectorStore", lambda **kw: mock_store)
            mp.setattr("agent_knowledge.rag_ingress.qdrant_embedding.build_openai_embedding_provider", lambda **kw: mock_embed)
            mp.setattr(pg_recall, "validated_pg_embedding_profile", lambda *a: None)
            mp.setattr(pg_recall, "assert_pg_embedding_egress_safe", lambda *a: None)

            search = pg_recall.build_pg_brain_query_search_from_env(environ)
            assert search is not None

            results = search("query", "/project/test-proj", as_of=now.isoformat())
            assert results == []

    def test_chunk_recall_empty_candidates_returns_empty_list(self):
        """When 0 chunks match, recall returns empty list safely."""
        mock_store, mock_embed, environ = _setup_mock_pg_recall([])

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pg_recall, "PgVectorStore", lambda **kw: mock_store)
            mp.setattr("agent_knowledge.rag_ingress.qdrant_embedding.build_openai_embedding_provider", lambda **kw: mock_embed)
            mp.setattr(pg_recall, "validated_pg_embedding_profile", lambda *a: None)
            mp.setattr(pg_recall, "assert_pg_embedding_egress_safe", lambda *a: None)

            search = pg_recall.build_pg_brain_query_search_from_env(environ)
            assert search is not None

            results = search("query", "/project/test-proj")
            assert results == []
