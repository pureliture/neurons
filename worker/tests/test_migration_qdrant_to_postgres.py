"""Tests for Qdrant to PostgreSQL Migration & Backfill (Milestone 4).

Tests chunk and card migration, dry-run mode, checkpointing, missing vector outbox enqueueing,
and quarantined corrupted records.
"""

from __future__ import annotations

import json
import os
import pytest
import tempfile

from agent_knowledge.postgres_store.pgvector_store import (
    PgVectorStore,
    MemoryCard,
    SessionChunk,
    make_dummy_vector,
)
from agent_knowledge.postgres_store.migration_qdrant_to_postgres import (
    QdrantToPostgresMigrator,
    MigrationResult,
    FullMigrationSummary,
)


class MockQdrantClient:
    """Mock Qdrant client supporting both collections dict and scroll API."""

    def __init__(self):
        self.collections: dict[str, dict[str, Any]] = {
            "session_chunks": {},
            "memory_cards": {},
        }

    def add_chunk(self, chunk_id: str, vector: list[float], payload: dict):
        self.collections["session_chunks"][chunk_id] = (vector, payload)

    def add_card(self, card_id: str, vector: list[float] | None, payload: dict):
        self.collections["memory_cards"][card_id] = (vector, payload)


@pytest.fixture
def mock_qdrant():
    client = MockQdrantClient()
    # Populate chunks
    for i in range(5):
        client.add_chunk(
            f"chunk_{i}",
            make_dummy_vector(i),
            {
                "chunk_id": f"chunk_{i}",
                "session_id_hash": f"sha256:session_{i}",
                "project": "neurons",
                "provider": "codex",
                "chunk_index": i,
                "content_markdown": f"Session content {i}",
                "embedding_model": "text-embedding-3-small",
            },
        )
    # Populate cards (3 with vectors, 2 without vectors)
    for i in range(3):
        client.add_card(
            f"card_{i}",
            make_dummy_vector(i + 10),
            {
                "memory_id": f"card_{i}",
                "project": "neurons",
                "card_type": "decision",
                "title": f"Decision {i}",
                "summary": f"Summary {i}",
                "typed_payload": {"rule": f"Rule {i}"},
                "content_hash": f"sha256:{i:064d}",
                "lifecycle_state": "human_accepted",
                "authorization_status": "active",
            },
        )
    for i in range(3, 5):
        client.add_card(
            f"card_{i}",
            None,
            {
                "memory_id": f"card_{i}",
                "project": "neurons",
                "card_type": "preference",
                "title": f"Preference {i}",
                "summary": f"Summary {i}",
                "typed_payload": {"pref": f"Pref {i}"},
                "content_hash": f"sha256:{i:064d}",
                "lifecycle_state": "candidate",
                "authorization_status": "disabled",
            },
        )
    return client


@pytest.fixture
def target_store():
    store = PgVectorStore(use_in_memory=True)
    store.execute_ddl()
    return store


def test_migration_dry_run_mode(mock_qdrant, target_store):
    migrator = QdrantToPostgresMigrator(
        qdrant_client=mock_qdrant,
        target_store=target_store,
        dry_run=True,
    )
    summary = migrator.run_full_migration(project="neurons")
    
    assert summary.dry_run is True
    assert summary.total_migrated == 10  # 5 chunks + 5 cards scanned and mapped
    assert len(target_store.chunks) == 0  # No records written in dry run
    assert len(target_store.cards) == 0
    assert len(target_store.outbox) == 0


def test_migration_live_execution(mock_qdrant, target_store):
    migrator = QdrantToPostgresMigrator(
        qdrant_client=mock_qdrant,
        target_store=target_store,
        dry_run=False,
    )
    summary = migrator.run_full_migration(project="neurons")

    assert summary.dry_run is False
    assert summary.total_migrated == 10
    assert len(target_store.chunks) == 5
    assert len(target_store.cards) == 5
    # Cards without vectors (card_3 and card_4) should have outbox tasks enqueued
    assert summary.total_outbox_enqueued == 2
    assert len(target_store.outbox) == 2


def test_migration_checkpointing(mock_qdrant, target_store):
    with tempfile.TemporaryDirectory() as tmpdir:
        chk_file = os.path.join(tmpdir, "checkpoint.json")
        migrator = QdrantToPostgresMigrator(
            qdrant_client=mock_qdrant,
            target_store=target_store,
            batch_size=2,
            checkpoint_file=chk_file,
        )
        res = migrator.migrate_session_chunks(collection_name="session_chunks")
        assert res.total_migrated == 5
        assert os.path.exists(chk_file)

        with open(chk_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            assert "session_chunks_offset" in data


def test_migration_quarantine_corrupted_vector(mock_qdrant, target_store):
    # Add corrupted vector (dimension 512 instead of 1536)
    mock_qdrant.add_chunk(
        "bad_chunk",
        [0.1] * 512,
        {"project": "neurons", "title": "Corrupted vector"},
    )

    migrator = QdrantToPostgresMigrator(
        qdrant_client=mock_qdrant,
        target_store=target_store,
    )
    res = migrator.migrate_session_chunks(collection_name="session_chunks")

    assert res.total_quarantined == 1
    assert len(res.quarantined_records) == 1
    assert "Invalid vector dimension" in res.quarantined_records[0]["reason"]
    assert "bad_chunk" not in target_store.chunks
