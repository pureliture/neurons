"""Unit and contract tests for Milestone M6: R6 Metrics Endpoint, Anomaly Handling, and Chunk Fallback.

Covers:
- FR-AGE-064: Temporal coverage metrics (pgvector_store & knowledge_search_service)
- FR-AGE-022: Chunk recall as_of and fallback with fallback_reason="no_current_match"
- FR-AGE-026: Future as_of rejection (> 5min tolerance) in resolver and pg_recall
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_knowledge.knowledge_search_service import KnowledgeSearchService
from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
from agent_knowledge.postgres_store.pgvector_store import PgVectorStore
from agent_knowledge.rag_ingress import pg_recall


# ==============================================================================
# Suite 1: FR-AGE-064 Temporal Coverage Metrics
# ==============================================================================

class _FakeCursor:
    def __init__(self, row: tuple[int, int] | None):
        self._row = row

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.last_sql = sql
        self.last_params = params

    def fetchone(self) -> tuple[int, int] | None:
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
    def __init__(self, total: int, with_observed: int):
        self._fake_cursor = _FakeCursor((total, with_observed) if total > 0 else (0, 0))

    @contextmanager
    def _scope(self, conn: Any = None):
        yield _FakeDb(self._fake_cursor)


def test_pgvector_store_temporal_coverage_metrics_calculation():
    """FR-AGE-064: PgVectorStore calculates total, with_observed, missing, and ratios correctly."""
    store = _MockPgStore(total=100, with_observed=75)
    metrics = store.get_temporal_coverage_metrics("test-proj")

    assert metrics["project"] == "test-proj"
    assert metrics["total_items"] == 100
    assert metrics["with_observed_at"] == 75
    assert metrics["missing_observed_at"] == 25
    assert metrics["unknown_ratio"] == 0.25
    assert metrics["coverage_ratio"] == 0.75


def test_pgvector_store_temporal_coverage_metrics_zero_division_safe():
    """FR-AGE-064: Zero items in project yields 0.0 ratios without ZeroDivisionError."""
    store = _MockPgStore(total=0, with_observed=0)
    metrics = store.get_temporal_coverage_metrics("empty-proj")

    assert metrics["project"] == "empty-proj"
    assert metrics["total_items"] == 0
    assert metrics["with_observed_at"] == 0
    assert metrics["missing_observed_at"] == 0
    assert metrics["unknown_ratio"] == 0.0
    assert metrics["coverage_ratio"] == 0.0


def test_knowledge_search_service_delegates_to_pgvector_store():
    """FR-AGE-064: KnowledgeSearchService delegates to pgvector_store when present."""
    mock_pg = _MockPgStore(total=50, with_observed=40)
    service = KnowledgeSearchService(
        ledger=MagicMock(),
        retired_index_bridge=None,
        dataset_ids=[],
        pgvector_store=mock_pg,
    )
    metrics = service.get_temporal_coverage_metrics("proj-alpha")

    assert metrics["total_items"] == 50
    assert metrics["with_observed_at"] == 40
    assert metrics["missing_observed_at"] == 10
    assert metrics["unknown_ratio"] == 0.2
    assert metrics["coverage_ratio"] == 0.8


def test_knowledge_search_service_fallback_memory_model():
    """FR-AGE-064: KnowledgeSearchService falls back to memory model cards when pgvector_store is None."""
    service = KnowledgeSearchService(
        ledger=MagicMock(),
        retired_index_bridge=None,
        dataset_ids=[],
        pgvector_store=None,
    )
    mock_cards = [
        {"memory_id": "c1", "observed_at": "2026-10-24T00:00:00Z"},
        {"memory_id": "c2", "observed_at": "2026-10-23T00:00:00Z"},
        {"memory_id": "c3", "observed_at": None},
        {"memory_id": "c4"},  # missing key
    ]
    mock_read_model = MagicMock()
    mock_read_model.list_accepted_cards.return_value = mock_cards
    service._brain_card_cache._read_model = mock_read_model

    metrics = service.get_temporal_coverage_metrics("fallback-proj")

    assert metrics["total_items"] == 4
    assert metrics["with_observed_at"] == 2
    assert metrics["missing_observed_at"] == 2
    assert metrics["unknown_ratio"] == 0.5
    assert metrics["coverage_ratio"] == 0.5


# ==============================================================================
# Suite 2: FR-AGE-026 Future as_of Rejection
# ==============================================================================

def test_resolver_rejects_as_of_more_than_5_minutes_in_future():
    """FR-AGE-026: Resolver rejects as_of queries beyond 5min in future."""
    now = datetime.now(timezone.utc)
    future_6min = (now + timedelta(minutes=6)).isoformat()
    resolver = GraphFirstResolver(MagicMock(), None, None)

    with pytest.raises(ValueError, match=r"(invalid_as_of_future|future)"):
        resolver.resolve(project="neurons", query="test", as_of=future_6min)


def test_resolver_accepts_as_of_within_5_minutes_future_tolerance():
    """FR-AGE-026: Resolver permits clock skew within 5min tolerance."""
    now = datetime.now(timezone.utc)
    future_2min = (now + timedelta(minutes=2)).isoformat()
    mock_store = MagicMock()
    mock_store.list_authorized_cards.return_value = []
    resolver = GraphFirstResolver(mock_store, None, None)

    res = resolver.resolve(project="neurons", mode="list", query="test", as_of=future_2min)
    assert res["items"] == []
    assert res.get("temporal_view") == "as_of" or res.get("metadata", {}).get("temporal_view") == "as_of"


# ==============================================================================
# Suite 3: FR-AGE-022 Chunk Recall as_of and Fallback
# ==============================================================================

def _setup_mock_pg_recall(mock_hits: list[dict[str, Any]]):
    mock_store = MagicMock()

    def _search_chunks(query_vector: list[float], project: str, limit: int):
        candidates = []
        for h in mock_hits:
            content = h.get("content", f"Content {h['chunk_id']}")
            chash = pg_recall.sha256_hash(content)
            candidates.append({
                "chunk_id": h["chunk_id"],
                "distance": 0.1,
                "session_id_hash": "sess-hash-123",
                "content_hash": chash,
                "content_markdown": content,
                "provider": "test-provider",
                "project": project,
            })
        return candidates

    mock_store.search_session_chunks.side_effect = _search_chunks

    def _get_chunk(chunk_id: str):
        for h in mock_hits:
            if h["chunk_id"] == chunk_id:
                row = MagicMock()
                row.chunk_id = h["chunk_id"]
                row.embedding_state = "ready"
                row.embedding_model = "test-model"
                row.embedding = [0.1] * 16
                row.project = "test-proj"
                row.content_markdown = h.get("content", f"Content {chunk_id}")
                row.content_hash = pg_recall.sha256_hash(row.content_markdown)
                row.session_id_hash = "sess-hash-123"
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


def test_chunk_recall_future_as_of_rejection():
    """FR-AGE-026: pg_recall rejects as_of more than 5 minutes in future."""
    now = datetime.now(timezone.utc)
    future_10min = (now + timedelta(minutes=10)).isoformat()
    mock_store, mock_embed, environ = _setup_mock_pg_recall([])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pg_recall, "PgVectorStore", lambda **kw: mock_store)
        mp.setattr("agent_knowledge.rag_ingress.qdrant_embedding.build_openai_embedding_provider", lambda **kw: mock_embed)
        mp.setattr(pg_recall, "validated_pg_embedding_profile", lambda *a: None)
        mp.setattr(pg_recall, "assert_pg_embedding_egress_safe", lambda *a: None)

        search = pg_recall.build_pg_brain_query_search_from_env(environ)
        assert search is not None

        with pytest.raises(ValueError, match=r"invalid_as_of_future"):
            search("query", "/project/test-proj", as_of=future_10min)


def test_chunk_recall_falls_back_to_past_with_reason():
    """FR-AGE-022: When current matches are absent, past lane is returned with fallback_reason='no_current_match'."""
    now = datetime.now(timezone.utc)
    past_chunk = {
        "chunk_id": "chunk-past-1",
        "content": "Past superseded knowledge chunk",
        "observed_at": (now - timedelta(days=20)).isoformat(),
        "created_at": (now - timedelta(days=20)).isoformat(),
        "authority_currentness": "superseded",
    }
    mock_store, mock_embed, environ = _setup_mock_pg_recall([past_chunk])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pg_recall, "PgVectorStore", lambda **kw: mock_store)
        mp.setattr("agent_knowledge.rag_ingress.qdrant_embedding.build_openai_embedding_provider", lambda **kw: mock_embed)
        mp.setattr(pg_recall, "validated_pg_embedding_profile", lambda *a: None)
        mp.setattr(pg_recall, "assert_pg_embedding_egress_safe", lambda *a: None)

        search = pg_recall.build_pg_brain_query_search_from_env(environ)
        assert search is not None

        # Query with as_of now; current matches are empty, so it should fall back to past
        results = search("knowledge", "/project/test-proj", as_of=now.isoformat())
        assert len(results) == 1
        hit = results[0]
        assert hit["memory_id"] == "chunk-past-1"
        assert hit["fallback_reason"] == "no_current_match"
        # Invariant: currentness is preserved as superseded, NEVER disguised as current
        assert hit["currentness"] == "superseded"


def test_chunk_recall_current_match_does_not_set_fallback_reason():
    """FR-AGE-022: When current matches exist, fallback_reason is NOT set."""
    now = datetime.now(timezone.utc)
    current_chunk = {
        "chunk_id": "chunk-current-1",
        "content": "Current fresh chunk",
        "observed_at": (now - timedelta(hours=2)).isoformat(),
        "created_at": (now - timedelta(hours=2)).isoformat(),
        "authority_currentness": "current",
    }
    past_chunk = {
        "chunk_id": "chunk-past-1",
        "content": "Past chunk",
        "observed_at": (now - timedelta(days=20)).isoformat(),
        "created_at": (now - timedelta(days=20)).isoformat(),
        "authority_currentness": "superseded",
    }
    mock_store, mock_embed, environ = _setup_mock_pg_recall([current_chunk, past_chunk])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pg_recall, "PgVectorStore", lambda **kw: mock_store)
        mp.setattr("agent_knowledge.rag_ingress.qdrant_embedding.build_openai_embedding_provider", lambda **kw: mock_embed)
        mp.setattr(pg_recall, "validated_pg_embedding_profile", lambda *a: None)
        mp.setattr(pg_recall, "assert_pg_embedding_egress_safe", lambda *a: None)

        search = pg_recall.build_pg_brain_query_search_from_env(environ)
        assert search is not None

        results = search("knowledge", "/project/test-proj", as_of=now.isoformat())
        assert len(results) == 1
        hit = results[0]
        assert hit["memory_id"] == "chunk-current-1"
        assert hit["currentness"] == "current"
        assert "fallback_reason" not in hit
