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
import struct
import tempfile
from typing import Any

import pytest

from agent_knowledge.postgres_store.pgvector_store import (
    MemoryCard,
    PgVectorStore,
    SessionChunk,
    _vector_literal,
    make_dummy_vector,
)


class _SemanticCursor:
    def __init__(self, equal: bool | None) -> None:
        self.equal = equal
        self.executed: tuple[str, tuple[object, ...]] | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params) -> None:
        self.executed = (sql, params)

    def fetchone(self):
        return None if self.equal is None else {"embedding_equal": self.equal}


class _SemanticConnection:
    def __init__(self, equal: bool | None) -> None:
        self.cursor_value = _SemanticCursor(equal)

    def cursor(self):
        return self.cursor_value


class _TupleSemanticCursor(_SemanticCursor):
    def fetchone(self):
        return None if self.equal is None else (self.equal,)


class _TupleSemanticConnection:
    def __init__(self, equal: bool | None) -> None:
        self.cursor_value = _TupleSemanticCursor(equal)

    def cursor(self):
        return self.cursor_value


def test_postgres_semantic_embedding_equality_uses_server_halfvec_cast_and_chunk_scope():
    source = make_dummy_vector(905)
    conn = _SemanticConnection(True)
    store = PgVectorStore(connection=conn)

    assert store.chunk_embedding_equals("chunk_905", source, conn=conn) is True
    sql, params = conn.cursor_value.executed
    assert "FROM session_memory_chunks" in sql
    assert "WHERE chunk_id = %s" in sql
    assert "embedding = %s::halfvec" in sql
    assert params == (_vector_literal(source), "chunk_905")


def test_postgres_semantic_embedding_equality_supports_tuple_row():
    source = make_dummy_vector(906)
    conn = _TupleSemanticConnection(True)
    store = PgVectorStore(connection=conn)

    assert store.chunk_embedding_equals("tuple-row", source, conn=conn) is True


def test_postgres_semantic_embedding_equality_fails_closed_when_query_has_no_row():
    source = make_dummy_vector(907)
    conn = _SemanticConnection(None)
    store = PgVectorStore(connection=conn)

    assert store.chunk_embedding_equals("missing", source, conn=conn) is False


from agent_knowledge.postgres_store.migration_qdrant_to_postgres import (
    QdrantToPostgresMigrator,
)


DEFAULT_SESSION_COLLECTION = "neurons_mirror_gemini_3072_v1"


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(str(value).encode()).hexdigest()


def _chunk_payload(i: int) -> dict[str, Any]:
    return {
        "memory_id": f"chunk_{i}",
        "session_id_hash": f"sha256:session_{i}",
        "project": "neurons",
        "provider": "codex",
        "chunk_index": i,
        "text": f"Session content {i}",
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
            DEFAULT_SESSION_COLLECTION: {},
            "memory_cards": {},
        }
        self._profiles: dict[str, dict[str, Any]] = {
            "session_chunks": {"size": size, "distance": distance},
            DEFAULT_SESSION_COLLECTION: {"size": size, "distance": distance},
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

    def add_chunk(
        self,
        chunk_id: str,
        vector: list[float] | None,
        payload: dict,
        collection_name: str = DEFAULT_SESSION_COLLECTION,
    ):
        self.collections[collection_name][chunk_id] = (vector, payload)
        if collection_name != "session_chunks" and "session_chunks" in self.collections:
            self.collections["session_chunks"][chunk_id] = (vector, payload)
        if collection_name != DEFAULT_SESSION_COLLECTION and DEFAULT_SESSION_COLLECTION in self.collections:
            self.collections[DEFAULT_SESSION_COLLECTION][chunk_id] = (vector, payload)

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
        self.fail_write = False
        self.fail_write_type_error = False
        self.fail_readback = False
        self.corrupt_readback = False
        self.corrupt_vector_readback = False
        self.semantic_embedding_equal: bool | None = True
        self.semantic_calls: list[tuple[str, list[float], Any]] = []

    def chunk_embedding_equals(self, chunk_id: str, source_vector, conn: Any | None = None) -> bool:
        self.semantic_calls.append((chunk_id, list(source_vector), conn))
        return self.semantic_embedding_equal is True

    def _scope(self, *, write: bool = False):
        from contextlib import nullcontext
        return nullcontext(self)

    def _enqueue(self, *, target_type: str, target_id: str, content_hash: str) -> int:
        outbox_id = self._next_outbox_id
        self._next_outbox_id += 1
        self.outbox[outbox_id] = {
            "target_type": target_type,
            "target_id": target_id,
            "content_hash": content_hash,
        }
        return outbox_id

    def insert_chunk(self, chunk: SessionChunk, conn: Any | None = None) -> str:
        if self.fail_write_type_error:
            raise TypeError("simulated_internal_type_error")
        if self.fail_write:
            raise RuntimeError("simulated_target_write_failure")
        self.chunks[chunk.chunk_id] = chunk
        if chunk.embedding is None:
            self._enqueue(
                target_type="session_chunk",
                target_id=chunk.chunk_id,
                content_hash=chunk.content_hash,
            )
        return chunk.chunk_id

    def get_chunk(self, chunk_id: str, conn: Any | None = None) -> SessionChunk | None:
        if self.fail_readback:
            return None
        chunk = self.chunks.get(chunk_id)
        if chunk is None:
            return None
        if self.corrupt_readback:
            return SessionChunk(
                chunk_id=chunk.chunk_id,
                session_id_hash=chunk.session_id_hash,
                project=chunk.project,
                provider=chunk.provider,
                chunk_index=chunk.chunk_index,
                content_markdown=chunk.content_markdown,
                token_count=chunk.token_count,
                content_hash=chunk.content_hash,
                embedding_model=chunk.embedding_model,
                embedding_state="pending",
                embedding=None,
            )
        if self.corrupt_vector_readback:
            vector = list(chunk.embedding or [])
            vector[0] = vector[0] + 0.5
            return SessionChunk(**{**chunk.__dict__, "embedding": vector})
        return chunk

    def upsert_card(self, card: MemoryCard, conn: Any | None = None) -> str:
        if self.fail_write:
            raise RuntimeError("simulated_target_write_failure")
        self.cards[card.memory_id] = card
        if card.embedding is None:
            self._enqueue(
                target_type="memory_card",
                target_id=card.memory_id,
                content_hash=card.content_hash,
            )
        return card.memory_id

    def get_card(self, memory_id: str, conn: Any | None = None) -> MemoryCard | None:
        if self.fail_readback:
            return None
        return self.cards.get(memory_id)


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


def test_session_chunk_memory_id_wins_and_absence_quarantines(mock_qdrant, target_store):
    # Case 1: memory_id present along with different chunk_id and point id -> memory_id wins
    payload_valid = _chunk_payload(101)
    payload_valid["memory_id"] = "mem_wins"
    payload_valid["chunk_id"] = "chunk_loses"
    mock_qdrant.add_chunk("point_id_loses", make_dummy_vector(101), payload_valid)

    # Case 2: chunk_id and point id exist, but memory_id is missing -> quarantines
    payload_no_mem = _chunk_payload(102)
    payload_no_mem.pop("memory_id", None)
    payload_no_mem["chunk_id"] = "fallback_chunk_id"
    mock_qdrant.add_chunk("point_fallback", make_dummy_vector(102), payload_no_mem)

    migrator = QdrantToPostgresMigrator(qdrant_client=mock_qdrant, target_store=target_store)
    res = migrator.migrate_session_chunks(collection_name=DEFAULT_SESSION_COLLECTION)

    assert "mem_wins" in target_store.chunks
    assert target_store.chunks["mem_wins"].chunk_id == "mem_wins"
    assert "chunk_loses" not in target_store.chunks
    assert "point_id_loses" not in target_store.chunks

    assert "fallback_chunk_id" not in target_store.chunks
    assert "point_fallback" not in target_store.chunks
    q_recs = [r for r in res.quarantined_records if r["reason_code"] == "required_memory_id_missing"]
    assert len(q_recs) == 1
    assert q_recs[0]["point_digest"] == _digest("point_fallback")


def test_session_chunk_text_wins_and_absence_quarantines(mock_qdrant, target_store):
    # Case 1: text present along with content_markdown and summary -> text wins
    payload_text_wins = _chunk_payload(201)
    payload_text_wins["text"] = "authoritative_text"
    payload_text_wins["content_markdown"] = "fallback_markdown"
    payload_text_wins["summary"] = "fallback_summary"
    mock_qdrant.add_chunk("pt_text_wins", make_dummy_vector(201), payload_text_wins)

    # Case 2: text missing even though content_markdown and summary exist -> quarantines
    payload_no_text = _chunk_payload(202)
    payload_no_text.pop("text", None)
    payload_no_text["content_markdown"] = "fallback_markdown_only"
    payload_no_text["summary"] = "fallback_summary_only"
    mock_qdrant.add_chunk("pt_no_text", make_dummy_vector(202), payload_no_text)

    migrator = QdrantToPostgresMigrator(qdrant_client=mock_qdrant, target_store=target_store)
    res = migrator.migrate_session_chunks(collection_name=DEFAULT_SESSION_COLLECTION)

    assert target_store.chunks["chunk_201"].content_markdown == "authoritative_text"
    assert "chunk_202" not in target_store.chunks
    q_recs = [r for r in res.quarantined_records if r["reason_code"] == "required_text_missing"]
    assert len(q_recs) == 1
    assert q_recs[0]["point_digest"] == _digest("pt_no_text")


def test_session_chunk_vectorless_point_quarantines_no_outbox(mock_qdrant, target_store):
    payload = _chunk_payload(301)
    mock_qdrant.add_chunk("pt_vectorless", None, payload)

    migrator = QdrantToPostgresMigrator(qdrant_client=mock_qdrant, target_store=target_store)
    res = migrator.migrate_session_chunks(collection_name=DEFAULT_SESSION_COLLECTION)

    assert "chunk_301" not in target_store.chunks
    assert res.outbox_enqueued == 0
    assert len(target_store.outbox) == 0
    q_recs = [r for r in res.quarantined_records if r["reason_code"] == "vector_missing"]
    assert len(q_recs) == 1
    assert q_recs[0]["point_digest"] == _digest("pt_vectorless")


def test_memory_card_target_write_failure_is_quarantined_and_session_copy_remains_strict():
    mock_qdrant = MockQdrantClient()
    target_store = FakePgVectorStore()
    mock_qdrant.add_card("card-write-failure", make_dummy_vector(403), _card_payload(403))
    target_store.fail_write = True

    migrator = QdrantToPostgresMigrator(qdrant_client=mock_qdrant, target_store=target_store)
    result = migrator.migrate_memory_cards()

    assert result.total_migrated == 0
    assert result.total_quarantined == 1
    assert result.quarantined_records[0]["reason_code"] == "target_write_failed"


def test_session_chunk_updates_existing_differing_target_and_preserves_card_behavior():
    mock_qdrant = MockQdrantClient()
    target_store = FakePgVectorStore()
    # Pre-existing differing chunk in target store
    target_store.chunks["chunk_401"] = SessionChunk(
        chunk_id="chunk_401",
        session_id_hash="sha256:old_sess",
        project="old_proj",
        provider="old_prov",
        chunk_index=42,
        content_markdown="old_content",
        token_count=500,
        content_hash="sha256:old_hash",
        embedding_model="gemini-embedding-2",
        embedding_state="pending",
        embedding=None,
    )
    new_vec = make_dummy_vector(401)
    new_payload = _chunk_payload(401)
    new_payload["text"] = "new_authoritative_text"
    new_payload["session_id_hash"] = "sha256:new_sess"
    new_payload["project"] = "neurons"
    new_payload["provider"] = "codex"
    mock_qdrant.add_chunk("pt_401", new_vec, new_payload)

    migrator = QdrantToPostgresMigrator(qdrant_client=mock_qdrant, target_store=target_store)
    res_chunks = migrator.migrate_session_chunks(collection_name=DEFAULT_SESSION_COLLECTION)
    assert res_chunks.outbox_enqueued == 0

    updated = target_store.chunks["chunk_401"]
    assert updated.content_markdown == "new_authoritative_text"
    assert updated.session_id_hash == "sha256:new_sess"
    assert updated.project == "neurons"
    assert updated.provider == "codex"
    assert updated.chunk_index == 0
    assert updated.token_count == 0
    assert updated.embedding_state == "ready"
    assert updated.embedding == new_vec

    # Card migration preserves current pending/outbox behavior for vectorless card
    mock_qdrant.add_card("card_no_vec", None, _card_payload(402, card_type="preference"))
    res_cards = migrator.migrate_memory_cards()
    assert res_cards.outbox_enqueued == 1
    assert "card_402" in target_store.cards
    assert target_store.cards["card_402"].embedding_state == "pending"
    assert target_store.cards["card_402"].embedding is None
    assert len(target_store.outbox) == 1
    assert next(iter(target_store.outbox.values()))["target_type"] == "memory_card"


def test_session_chunk_source_vector_passed_directly_ready_no_provider_call(mock_qdrant, target_store):
    source_vec = make_dummy_vector(501)
    payload = _chunk_payload(501)
    mock_qdrant.add_chunk("pt_501", source_vec, payload)

    migrator = QdrantToPostgresMigrator(qdrant_client=mock_qdrant, target_store=target_store)
    res = migrator.migrate_session_chunks(collection_name=DEFAULT_SESSION_COLLECTION)

    assert res.outbox_enqueued == 0
    chunk = target_store.chunks["chunk_501"]
    assert chunk.embedding_state == "ready"
    assert chunk.embedding == source_vec
    assert len(chunk.embedding) == 3072
    assert chunk.chunk_index == 0
    assert chunk.token_count == 0


def test_session_chunk_migration_static_import_boundary():
    import ast
    migrator_file = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "../lib/agent_knowledge/postgres_store/migration_qdrant_to_postgres.py",
        )
    )
    with open(migrator_file, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=migrator_file)

    forbidden_modules = {"couchdb", "couch", "receipt", "pg_backfill", "pg_qdrant_import"}
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.name.lower())
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_names.add(node.module.lower())
            for alias in node.names:
                imported_names.add(alias.name.lower())

    for forbidden in forbidden_modules:
        for imported in imported_names:
            assert forbidden not in imported, f"Forbidden import '{forbidden}' found in '{imported}'"


def test_session_chunk_checkpoint_advancement_on_success_and_absent_on_target_failure():
    client = MockQdrantClient()
    store = FakePgVectorStore()
    with tempfile.TemporaryDirectory() as tmpdir:
        chk_file = os.path.join(tmpdir, "checkpoint.json")

        # Step 1: Successful batch -> checkpoint advancement occurs
        for i in range(2):
            client.add_chunk(f"chk_batch_{i}", make_dummy_vector(i), _chunk_payload(i))

        migrator = QdrantToPostgresMigrator(
            qdrant_client=client,
            target_store=store,
            batch_size=2,
            checkpoint_file=chk_file,
        )
        res = migrator.migrate_session_chunks(collection_name=DEFAULT_SESSION_COLLECTION)
        assert res.total_migrated == 2
        assert os.path.exists(chk_file)
        with open(chk_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        key = _digest(DEFAULT_SESSION_COLLECTION)
        assert data["collections"][key]["completed"] is True

        # Step 2: Target write failure -> checkpoint advancement is absent
        store.fail_write = True
        client.add_chunk("chk_fail_write", make_dummy_vector(999), _chunk_payload(999))

        chk_file2 = os.path.join(tmpdir, "checkpoint2.json")
        migrator_fail = QdrantToPostgresMigrator(
            qdrant_client=client,
            target_store=store,
            batch_size=2,
            checkpoint_file=chk_file2,
        )
        with pytest.raises(Exception):
            migrator_fail.migrate_session_chunks(collection_name=DEFAULT_SESSION_COLLECTION)

        if os.path.exists(chk_file2):
            with open(chk_file2, "r", encoding="utf-8") as f:
                data2 = json.load(f)
            assert key not in data2.get("collections", {}) or not data2["collections"][key].get("completed")
        else:
            assert not os.path.exists(chk_file2)

        # Step 3: Target readback failure -> checkpoint advancement is absent
        store.fail_write = False
        store.fail_readback = True
        chk_file3 = os.path.join(tmpdir, "checkpoint3.json")
        migrator_readback_fail = QdrantToPostgresMigrator(
            qdrant_client=client,
            target_store=store,
            batch_size=2,
            checkpoint_file=chk_file3,
        )
        with pytest.raises(Exception):
            migrator_readback_fail.migrate_session_chunks(collection_name=DEFAULT_SESSION_COLLECTION)

        if os.path.exists(chk_file3):
            with open(chk_file3, "r", encoding="utf-8") as f:
                data3 = json.load(f)
            assert key not in data3.get("collections", {}) or not data3["collections"][key].get("completed")
        else:
            assert not os.path.exists(chk_file3)


def test_session_chunk_missing_readback_capability_fails_before_checkpoint():
    class NoReadbackTarget:
        def __init__(self):
            self.chunks = {}

        def _scope(self, *, write: bool = False):
            from contextlib import nullcontext
            return nullcontext(self)

        def insert_chunk(self, chunk, conn=None):
            self.chunks[chunk.chunk_id] = chunk
            return chunk.chunk_id

    client = MockQdrantClient()
    target = NoReadbackTarget()
    with tempfile.TemporaryDirectory() as tmpdir:
        checkpoint = os.path.join(tmpdir, "checkpoint.json")
        client.add_chunk("missing-readback", make_dummy_vector(903), _chunk_payload(903))
        migrator = QdrantToPostgresMigrator(
            qdrant_client=client,
            target_store=target,
            checkpoint_file=checkpoint,
        )

        with pytest.raises(RuntimeError, match="target_readback_unavailable"):
            migrator.migrate_session_chunks(collection_name=DEFAULT_SESSION_COLLECTION)

        assert not os.path.exists(checkpoint)


@pytest.mark.skipif(
    not os.environ.get("LBRAIN_TEST_PG_DSN"),
    reason="LBRAIN_TEST_PG_DSN 미설정 (전용 live PostgreSQL integration gate)",
)
def test_live_postgres_semantic_equality_accepts_server_halfvec_when_python_binary16_differs(isolated_pg_store):
    value = 1.00048828126
    source = [value] * 3072
    assert struct.unpack("e", struct.pack("e", value))[0] == 1.0009765625

    chunk = SessionChunk(
        chunk_id="halfvec_semantic_match",
        session_id_hash="sha256:session_halfvec",
        project="neurons",
        provider="codex",
        content_markdown="halfvec semantic integration fixture",
        content_hash="sha256:" + "a" * 64,
        embedding_model="gemini-embedding-2",
        embedding_state="ready",
        embedding=source,
    )
    with isolated_pg_store.transaction() as conn:
        isolated_pg_store.insert_chunk(chunk, conn=conn)
        stored = isolated_pg_store.get_chunk(chunk.chunk_id, conn=conn)
        assert stored is not None and stored.embedding is not None
        assert stored.embedding != source
        assert isolated_pg_store.chunk_embedding_equals(chunk.chunk_id, source, conn=conn) is True


def test_session_chunk_readback_uses_postgres_semantic_equality_despite_python_representation_difference():
    client = MockQdrantClient()
    store = FakePgVectorStore()
    store.corrupt_vector_readback = True
    source = make_dummy_vector(904)
    with tempfile.TemporaryDirectory() as tmpdir:
        checkpoint = os.path.join(tmpdir, "checkpoint.json")
        client.add_chunk("corrupt-vector", source, _chunk_payload(904))
        migrator = QdrantToPostgresMigrator(
            qdrant_client=client,
            target_store=store,
            checkpoint_file=checkpoint,
        )

        result = migrator.migrate_session_chunks(collection_name=DEFAULT_SESSION_COLLECTION)

        assert result.total_migrated == 1
        assert store.semantic_calls == [("chunk_904", source, store)]
        assert os.path.exists(checkpoint)


def test_session_chunk_semantic_vector_mismatch_prevents_checkpoint():
    client = MockQdrantClient()
    store = FakePgVectorStore()
    store.semantic_embedding_equal = False
    with tempfile.TemporaryDirectory() as tmpdir:
        checkpoint = os.path.join(tmpdir, "checkpoint.json")
        client.add_chunk("corrupt-vector", make_dummy_vector(907), _chunk_payload(907))
        migrator = QdrantToPostgresMigrator(
            qdrant_client=client,
            target_store=store,
            checkpoint_file=checkpoint,
        )

        with pytest.raises(RuntimeError, match="target_readback_failed"):
            migrator.migrate_session_chunks(collection_name=DEFAULT_SESSION_COLLECTION)

        assert not os.path.exists(checkpoint)


def test_session_chunk_semantic_vector_check_unavailable_prevents_checkpoint():
    client = MockQdrantClient()
    store = FakePgVectorStore()
    store.semantic_embedding_equal = None
    with tempfile.TemporaryDirectory() as tmpdir:
        checkpoint = os.path.join(tmpdir, "checkpoint.json")
        client.add_chunk("missing-semantic", make_dummy_vector(908), _chunk_payload(908))
        migrator = QdrantToPostgresMigrator(
            qdrant_client=client,
            target_store=store,
            checkpoint_file=checkpoint,
        )

        with pytest.raises(RuntimeError, match="target_readback_failed"):
            migrator.migrate_session_chunks(collection_name=DEFAULT_SESSION_COLLECTION)

        assert not os.path.exists(checkpoint)


def test_session_chunk_readback_requires_complete_ready_vector_before_checkpoint():
    client = MockQdrantClient()
    store = FakePgVectorStore()
    store.corrupt_readback = True
    with tempfile.TemporaryDirectory() as tmpdir:
        checkpoint = os.path.join(tmpdir, "checkpoint.json")
        client.add_chunk("corrupt-readback", make_dummy_vector(902), _chunk_payload(902))
        migrator = QdrantToPostgresMigrator(
            qdrant_client=client,
            target_store=store,
            checkpoint_file=checkpoint,
        )

        with pytest.raises(RuntimeError, match="target_readback_failed"):
            migrator.migrate_session_chunks(collection_name=DEFAULT_SESSION_COLLECTION)

        assert not os.path.exists(checkpoint)


def test_session_chunk_internal_type_error_propagates_without_connection_fallback_or_checkpoint():
    client = MockQdrantClient()
    store = FakePgVectorStore()
    store.fail_write_type_error = True
    with tempfile.TemporaryDirectory() as tmpdir:
        checkpoint = os.path.join(tmpdir, "checkpoint.json")
        client.add_chunk("type-error", make_dummy_vector(901), _chunk_payload(901))
        migrator = QdrantToPostgresMigrator(
            qdrant_client=client,
            target_store=store,
            checkpoint_file=checkpoint,
        )

        with pytest.raises(TypeError, match="simulated_internal_type_error"):
            migrator.migrate_session_chunks(collection_name=DEFAULT_SESSION_COLLECTION)

        assert store.chunks == {}
        assert not os.path.exists(checkpoint)


def test_session_chunk_overlength_fields_quarantined_without_fallback_or_truncation(mock_qdrant, target_store):
    # Case 1: memory_id over 64 chars -> must not truncate to 64 or slice to 128; must not fallback to chunk_id; must quarantine and not write
    p_mem = _chunk_payload(601)
    p_mem["memory_id"] = "m" * 65
    p_mem["chunk_id"] = "valid_chunk_id"
    mock_qdrant.add_chunk("pt_mem_over", make_dummy_vector(601), p_mem)

    # Case 2: project over 64 chars -> must quarantine and not write
    p_proj = _chunk_payload(602)
    p_proj["project"] = "p" * 65
    mock_qdrant.add_chunk("pt_proj_over", make_dummy_vector(602), p_proj)

    # Case 3: provider over 32 chars -> must quarantine and not write
    p_prov = _chunk_payload(603)
    p_prov["provider"] = "x" * 33
    mock_qdrant.add_chunk("pt_prov_over", make_dummy_vector(603), p_prov)

    # Case 4: boundary values: memory_id=64, project=64, provider=32 -> valid and accepted
    p_exact = _chunk_payload(604)
    p_exact["memory_id"] = "m" * 64
    p_exact["project"] = "p" * 64
    p_exact["provider"] = "x" * 32
    mock_qdrant.add_chunk("pt_exact_boundary", make_dummy_vector(604), p_exact)

    migrator = QdrantToPostgresMigrator(qdrant_client=mock_qdrant, target_store=target_store)
    res = migrator.migrate_session_chunks(collection_name=DEFAULT_SESSION_COLLECTION)

    # Boundary valid record was migrated
    assert "m" * 64 in target_store.chunks
    chunk = target_store.chunks["m" * 64]
    assert chunk.project == "p" * 64
    assert chunk.provider == "x" * 32

    # Overlength values were NOT written, and alternate fields were NOT written
    assert "m" * 65 not in target_store.chunks
    assert "valid_chunk_id" not in target_store.chunks
    assert "chunk_602" not in target_store.chunks
    assert "chunk_603" not in target_store.chunks

    # Quarantined records carry expected reason codes and digests
    q_mem = [r for r in res.quarantined_records if r["reason_code"] == "memory_id_overlength"]
    assert len(q_mem) == 1
    assert q_mem[0]["point_digest"] == _digest("pt_mem_over")

    q_proj = [r for r in res.quarantined_records if r["reason_code"] == "project_overlength"]
    assert len(q_proj) == 1
    assert q_proj[0]["point_digest"] == _digest("pt_proj_over")

    q_prov = [r for r in res.quarantined_records if r["reason_code"] == "provider_overlength"]
    assert len(q_prov) == 1
    assert q_prov[0]["point_digest"] == _digest("pt_prov_over")


def test_session_chunk_non_string_required_fields_quarantined_without_fallback(mock_qdrant, target_store):
    # Non-string memory_id (e.g. int) must not be stringified and must not fallback to chunk_id
    p_mem = _chunk_payload(701)
    p_mem["memory_id"] = 12345
    p_mem["chunk_id"] = "fallback_chunk"
    mock_qdrant.add_chunk("pt_mem_int", make_dummy_vector(701), p_mem)

    # Non-string project (e.g. list)
    p_proj = _chunk_payload(702)
    p_proj["project"] = ["neurons"]
    mock_qdrant.add_chunk("pt_proj_list", make_dummy_vector(702), p_proj)

    # Non-string provider (e.g. dict)
    p_prov = _chunk_payload(703)
    p_prov["provider"] = {"name": "codex"}
    mock_qdrant.add_chunk("pt_prov_dict", make_dummy_vector(703), p_prov)

    # Non-string text (e.g. int) must not fallback to content_markdown
    p_text = _chunk_payload(704)
    p_text["text"] = 9999
    p_text["content_markdown"] = "fallback_content"
    mock_qdrant.add_chunk("pt_text_int", make_dummy_vector(704), p_text)

    migrator = QdrantToPostgresMigrator(qdrant_client=mock_qdrant, target_store=target_store)
    res = migrator.migrate_session_chunks(collection_name=DEFAULT_SESSION_COLLECTION)

    assert "12345" not in target_store.chunks
    assert "fallback_chunk" not in target_store.chunks
    assert "chunk_702" not in target_store.chunks
    assert "chunk_703" not in target_store.chunks
    assert "chunk_704" not in target_store.chunks

    assert any(r["reason_code"] == "memory_id_invalid" and r["point_digest"] == _digest("pt_mem_int") for r in res.quarantined_records)
    assert any(r["reason_code"] == "project_invalid" and r["point_digest"] == _digest("pt_proj_list") for r in res.quarantined_records)
    assert any(r["reason_code"] == "provider_invalid" and r["point_digest"] == _digest("pt_prov_dict") for r in res.quarantined_records)
    assert any(r["reason_code"] == "text_invalid" and r["point_digest"] == _digest("pt_text_int") for r in res.quarantined_records)


def test_session_chunk_nul_containing_text_quarantined_without_transform_or_fallback(mock_qdrant, target_store):
    p_nul = _chunk_payload(801)
    p_nul["text"] = "hello \x00 world"
    p_nul["content_markdown"] = "clean fallback without nul"
    mock_qdrant.add_chunk("pt_nul_text", make_dummy_vector(801), p_nul)

    migrator = QdrantToPostgresMigrator(qdrant_client=mock_qdrant, target_store=target_store)
    res = migrator.migrate_session_chunks(collection_name=DEFAULT_SESSION_COLLECTION)

    # Must NOT write transformed or raw text, and must NOT fallback
    assert "chunk_801" not in target_store.chunks
    for c in target_store.chunks.values():
        assert "hello" not in c.content_markdown
        assert "clean fallback" not in c.content_markdown

    q_nul = [r for r in res.quarantined_records if r["reason_code"] == "text_contains_nul"]
    assert len(q_nul) == 1
    assert q_nul[0]["point_digest"] == _digest("pt_nul_text")
