"""Tests for Qdrant to PostgreSQL Migration & Backfill (M5a).

New-contract coverage only: Qdrant ``get_collection``/``scroll`` seam,
digest-keyed checkpoints (``collection_digest``/``completed``/``preflight``/
``next_offset``), redacted quarantine (``point_digest``/``reason_code``),
dry-run no-write, completed re-run safety, and collection profile
mismatch fail-closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Any

import pytest

from agent_knowledge.postgres_store.pgvector_store import (
    MemoryCard,
    SessionChunk,
    make_dummy_vector,
)
from agent_knowledge.postgres_store.migration_qdrant_to_postgres import (
    QdrantToPostgresMigrator,
)


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(str(value).encode()).hexdigest()


def _chunk_payload(i: int) -> dict[str, Any]:
    return {
        "chunk_id": f"chunk_{i}",
        "session_id_hash": f"sha256:session_{i}",
        "project": "neurons",
        "provider": "codex",
        "chunk_index": i,
        "content_markdown": f"Session content {i}",
        "token_count": 10 + i,
        "content_hash": f"sha256:{i:064d}",
        "embedding_model": "gemini-embedding-2",
    }


def _card_payload(i: int, card_type: str = "decision") -> dict[str, Any]:
    return {
        "memory_id": f"card_{i}",
        "project": "neurons",
        "card_type": card_type,
        "title": f"Title {i}",
        "summary": f"Summary {i}",
        "typed_payload": {"rule": f"Rule {i}"},
        "content_hash": f"sha256:{i:064d}",
        "lifecycle_state": "candidate",
        "authorization_status": "disabled",
        "currentness": "current",
        "confidence": 0.9,
        "valid_from": "2026-01-01T00:00:00+00:00",
        "source_ref": [{"ref": f"src_{i}"}],
        "embedding_model": "gemini-embedding-2",
    }


class MockQdrantClient:
    """Minimal Qdrant seam: get_collection + scroll only."""

    def __init__(self, size: int = 3072, distance: str = "Cosine"):
        self.collections: dict[str, dict[str, Any]] = {
            "session_chunks": {},
            "memory_cards": {},
        }
        self._profiles: dict[str, dict[str, Any]] = {
            "session_chunks": {"size": size, "distance": distance},
            "memory_cards": {"size": size, "distance": distance},
        }

    def set_profile(self, collection_name: str, *, size: int, distance: str = "Cosine") -> None:
        self._profiles[collection_name] = {"size": size, "distance": distance}

    def get_collection(self, collection_name: str) -> dict[str, Any]:
        profile = self._profiles[collection_name]
        return {
            "config": {
                "params": {
                    "vectors": {"size": profile["size"], "distance": profile["distance"]},
                },
            },
        }

    def scroll(
        self,
        collection_name: str,
        limit: int = 100,
        offset: Any = None,
        with_payload: bool = True,
        with_vectors: bool = True,
    ) -> tuple[list[dict[str, Any]], Any]:
        items = list(self.collections.get(collection_name, {}).items())
        start = offset if isinstance(offset, int) else 0
        window = items[start : start + limit]
        points = [
            {"id": point_id, "vector": vector, "payload": payload}
            for point_id, (vector, payload) in window
        ]
        end = start + len(window)
        next_offset = end if end < len(items) else None
        return points, next_offset

    def add_chunk(self, chunk_id: str, vector: list[float] | None, payload: dict):
        self.collections["session_chunks"][chunk_id] = (vector, payload)

    def add_card(self, card_id: str, vector: list[float] | None, payload: dict):
        self.collections["memory_cards"][card_id] = (vector, payload)


class FakePgVectorStore:
    """Migration test double matching the real store seam.

    Mirrors PgVectorStore: ``insert_chunk``/``upsert_card`` enqueue a
    pending-embedding outbox job when the record has no embedding.
    """

    def __init__(self):
        self.chunks: dict[str, SessionChunk] = {}
        self.cards: dict[str, MemoryCard] = {}
        self.outbox: dict[int, dict[str, Any]] = {}
        self._next_outbox_id = 1

    def _enqueue(self, *, target_type: str, target_id: str, content_hash: str) -> int:
        outbox_id = self._next_outbox_id
        self._next_outbox_id += 1
        self.outbox[outbox_id] = {
            "target_type": target_type,
            "target_id": target_id,
            "content_hash": content_hash,
        }
        return outbox_id

    def insert_chunk(self, chunk: SessionChunk) -> str:
        self.chunks[chunk.chunk_id] = chunk
        if chunk.embedding is None:
            self._enqueue(
                target_type="session_chunk",
                target_id=chunk.chunk_id,
                content_hash=chunk.content_hash,
            )
        return chunk.chunk_id

    def upsert_card(self, card: MemoryCard) -> str:
        self.cards[card.memory_id] = card
        if card.embedding is None:
            self._enqueue(
                target_type="memory_card",
                target_id=card.memory_id,
                content_hash=card.content_hash,
            )
        return card.memory_id


@pytest.fixture
def mock_qdrant():
    client = MockQdrantClient()
    for i in range(5):
        client.add_chunk(f"chunk_{i}", make_dummy_vector(i), _chunk_payload(i))
    for i in range(3):
        client.add_card(f"card_{i}", make_dummy_vector(i + 10), _card_payload(i))
    for i in range(3, 5):
        client.add_card(f"card_{i}", None, _card_payload(i, card_type="preference"))
    return client


@pytest.fixture
def target_store():
    return FakePgVectorStore()


def test_migration_dry_run_mode(mock_qdrant, target_store):
    with tempfile.TemporaryDirectory() as tmpdir:
        chk_file = os.path.join(tmpdir, "checkpoint.json")
        migrator = QdrantToPostgresMigrator(
            qdrant_client=mock_qdrant,
            target_store=target_store,
            dry_run=True,
            checkpoint_file=chk_file,
        )
        summary = migrator.run_full_migration(project="neurons")

        assert summary.dry_run is True
        assert summary.total_migrated == 10  # 5 chunks + 5 cards scanned and mapped
        assert summary.total_outbox_enqueued == 0  # dry-run never enqueues
        assert len(target_store.chunks) == 0  # No records written in dry run
        assert len(target_store.cards) == 0
        assert len(target_store.outbox) == 0
        assert not os.path.exists(chk_file)  # dry-run writes no checkpoint


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
    # Cards without vectors (card_3 and card_4) stay pending and enqueue re-embed jobs
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
        key = _digest("session_chunks")
        assert set(data["collections"].keys()) == {key}
        record = data["collections"][key]
        assert record["collection_digest"] == key
        assert record["completed"] is True
        assert record["next_offset"] is None
        assert isinstance(record["preflight"], dict)
        assert record["preflight"]["compatible"] is True


def test_migration_quarantine_corrupted_vector(mock_qdrant, target_store):
    # Corrupted vector (dimension 512 instead of 3072), otherwise valid payload.
    bad_payload = _chunk_payload(99)
    bad_payload["chunk_id"] = "bad_chunk"
    mock_qdrant.add_chunk("bad_chunk", [0.1] * 512, bad_payload)

    migrator = QdrantToPostgresMigrator(
        qdrant_client=mock_qdrant,
        target_store=target_store,
    )
    res = migrator.migrate_session_chunks(collection_name="session_chunks")

    assert res.total_quarantined == 1
    assert res.total_migrated == 5
    assert len(res.quarantined_records) == 1
    record = res.quarantined_records[0]
    assert set(record.keys()) == {"point_digest", "reason_code"}
    assert record["reason_code"] == "vector_dimension_mismatch"
    assert record["point_digest"] != "bad_chunk"  # digest, never the raw source id
    assert "bad_chunk" not in target_store.chunks


def test_migration_completed_rerun_is_safe(mock_qdrant, target_store):
    with tempfile.TemporaryDirectory() as tmpdir:
        chk_file = os.path.join(tmpdir, "checkpoint.json")
        first = QdrantToPostgresMigrator(
            qdrant_client=mock_qdrant,
            target_store=target_store,
            batch_size=2,
            checkpoint_file=chk_file,
        )
        res1 = first.migrate_session_chunks(collection_name="session_chunks")
        assert res1.total_migrated == 5

        second = QdrantToPostgresMigrator(
            qdrant_client=mock_qdrant,
            target_store=target_store,
            batch_size=2,
            checkpoint_file=chk_file,
        )
        res2 = second.migrate_session_chunks(collection_name="session_chunks")
        assert res2.total_migrated == 0
        assert res2.total_quarantined == 0
        assert not res2.errors
        assert len(target_store.chunks) == 5


def test_migration_profile_mismatch_fail_closed(mock_qdrant, target_store):
    mock_qdrant.set_profile("session_chunks", size=512, distance="Cosine")
    migrator = QdrantToPostgresMigrator(
        qdrant_client=mock_qdrant,
        target_store=target_store,
    )
    res = migrator.migrate_session_chunks(collection_name="session_chunks")

    assert "collection_profile_mismatch" in res.errors
    assert res.total_migrated == 0
    assert res.total_scanned == 0
    assert len(target_store.chunks) == 0
