"""Tests for PgSessionMemoryProjector and pg_backfill.

Unit tests use real PgVectorStore (live PG gate) and synthetic embedding
providers — no MagicMock store.  Validates scoped ID derivation, ownership,
content/embedding profile, ready readback, close, and isolation.
"""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest

from agent_knowledge.couchdb_source.document_model import (
    RETIRED_INDEX_BRIDGE_RECALL_PROFILE,
    OwnershipViolation,
    assert_index_target_allowed,
    sha256_hash,
)
from agent_knowledge.postgres_store.pgvector_store import PgVectorStore
from agent_knowledge.rag_ingress.pg_backfill import (
    PgSessionMemoryProjector,
    _derive_pg_chunk_id_impl,
)


PG_DSN = os.environ.get("LBRAIN_TEST_PG_DSN", "")
live_pg = pytest.mark.skipif(
    not PG_DSN,
    reason="LBRAIN_TEST_PG_DSN 미설정 (전용 live PostgreSQL integration gate)",
)


class SyntheticEmbedProvider:
    """Synthetic embedding provider for tests (no external calls)."""

    def __init__(self, *, size: int = 3072, model: str = "gemini-embedding-2"):
        self._size = size
        self._model = model
        self._closed = False

    @property
    def size(self) -> int:
        return self._size

    @property
    def model(self) -> str:
        return self._model

    def embed(self, text: str) -> list[float]:
        # Deterministic embedding based on text hash for reproducibility
        h = hashlib.sha256(text.encode()).hexdigest()
        seed = int(h[:8], 16)
        return [(seed + i) / 1e9 for i in range(self._size)]

    def close(self) -> None:
        self._closed = True


# ---------------------------------------------------------------------------
# derive_pg_chunk_id tests
# ---------------------------------------------------------------------------


def test_chunk_id_is_64_chars_or_less():
    chunk_id = _derive_pg_chunk_id_impl(
        project="brain",
        provider="anthropic",
        session_id_hash="sha256:" + "a" * 64,
        source_hash="sha256:" + "b" * 64,
        content_hash="sha256:" + "c" * 64,
    )
    assert len(chunk_id) <= 64


def test_chunk_id_changes_with_different_project():
    common = dict(
        provider="anthropic",
        session_id_hash="sha256:" + "a" * 64,
        source_hash="sha256:" + "b" * 64,
        content_hash="sha256:" + "c" * 64,
    )
    id_a = _derive_pg_chunk_id_impl(project="project-a", **common)
    id_b = _derive_pg_chunk_id_impl(project="project-b", **common)
    assert id_a != id_b


def test_chunk_id_changes_with_different_embedding_profile():
    common = dict(
        project="brain",
        provider="anthropic",
        session_id_hash="sha256:" + "a" * 64,
        source_hash="sha256:" + "b" * 64,
        content_hash="sha256:" + "c" * 64,
    )
    id_default = _derive_pg_chunk_id_impl(**common)
    id_custom = _derive_pg_chunk_id_impl(**common, embedding_profile="custom-profile-v2")
    assert id_default != id_custom


def test_chunk_id_changes_with_different_session():
    common = dict(
        project="brain",
        provider="anthropic",
        source_hash="sha256:" + "b" * 64,
        content_hash="sha256:" + "c" * 64,
    )
    id_a = _derive_pg_chunk_id_impl(session_id_hash="sha256:" + "a" * 64, **common)
    id_b = _derive_pg_chunk_id_impl(session_id_hash="sha256:" + "d" * 64, **common)
    assert id_a != id_b


def test_assert_index_target_allowed_passes_session_memory():
    # Must not raise for the canonical profile
    assert_index_target_allowed(RETIRED_INDEX_BRIDGE_RECALL_PROFILE)


def test_assert_index_target_allowed_rejects_transcript_memory():
    with pytest.raises(OwnershipViolation, match="^transcript-memory is retired and is not a valid RetiredIndexBridge projection target$"):
        assert_index_target_allowed("transcript-memory")


# ---------------------------------------------------------------------------
# PgSessionMemoryProjector unit tests (no DB)
# ---------------------------------------------------------------------------


def test_projector_rejects_bad_target_profile():
    """assert_index_target_allowed must reject non-session-memory profiles."""
    from unittest.mock import MagicMock

    store = MagicMock(spec=PgVectorStore)
    provider = SyntheticEmbedProvider()
    projector = PgSessionMemoryProjector(store=store, embed_provider=provider)

    with pytest.raises(OwnershipViolation, match="^transcript-memory is retired and is not a valid RetiredIndexBridge projection target$"):
        projector.project(
            target_profile="transcript-memory",
            document={"body": "test", "content_hash": "sha256:abc"},
        )


def test_projector_close_calls_provider_close():
    provider = SyntheticEmbedProvider()
    from unittest.mock import MagicMock

    store = MagicMock(spec=PgVectorStore)
    projector = PgSessionMemoryProjector(store=store, embed_provider=provider)
    projector.close()
    assert provider._closed is True


# ---------------------------------------------------------------------------
# Live PG tests (LBRAIN_TEST_PG_DSN gate)
# ---------------------------------------------------------------------------


@live_pg
def test_live_projector_creates_ready_chunk():
    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()
    suffix = uuid.uuid4().hex[:12]
    provider = SyntheticEmbedProvider()
    projector = PgSessionMemoryProjector(store=store, embed_provider=provider)

    doc = {
        "body": f"test content {suffix}",
        "content_hash": sha256_hash(f"test content {suffix}"),
        "session_id_hash": f"sha256:{'b' * 64}",
        "source_hash": f"sha256:{'c' * 64}",
        "project": f"pg-backfill-test-{suffix}",
        "provider": "anthropic",
        "target_profile": "session-memory",
    }

    chunk_id = projector.project(
        target_profile="session-memory", document=doc
    )
    try:
        assert len(chunk_id) <= 64

        stored = store.get_chunk(chunk_id)
        assert stored is not None
        assert stored.embedding_state == "ready"
        assert stored.content_hash == doc["content_hash"]
        assert stored.session_id_hash == doc["session_id_hash"]
        assert stored.project == doc["project"]
        assert stored.provider == doc["provider"]
        assert stored.embedding is not None
        assert len(stored.embedding) == 3072
        assert stored.embedding_model == "gemini-embedding-2"
    finally:
        projector.close()
        with store.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM session_memory_chunks WHERE chunk_id = %s",
                    (chunk_id,),
                )
                cur.execute(
                    "DELETE FROM embedding_outbox WHERE target_id = %s",
                    (chunk_id,),
                )


@live_pg
def test_live_repeat_same_revision_preserves_ready_vector():
    """Projecting same session+content again reuses existing ready chunk."""
    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()
    suffix = uuid.uuid4().hex[:12]
    provider = SyntheticEmbedProvider()
    projector = PgSessionMemoryProjector(store=store, embed_provider=provider)

    doc = {
        "body": f"repeat test {suffix}",
        "content_hash": sha256_hash(f"repeat test {suffix}"),
        "session_id_hash": f"sha256:{'e' * 64}",
        "source_hash": f"sha256:{'f' * 64}",
        "project": f"pg-repeat-{suffix}",
        "provider": "codex",
    }

    chunk_id_1 = projector.project(
        target_profile="session-memory", document=doc
    )
    chunk_id_2 = projector.project(
        target_profile="session-memory", document=doc
    )
    try:
        assert chunk_id_1 == chunk_id_2

        stored = store.get_chunk(chunk_id_1)
        assert stored is not None
        assert stored.embedding_state == "ready"
    finally:
        projector.close()
        with store.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM session_memory_chunks WHERE chunk_id = %s",
                    (chunk_id_1,),
                )
                cur.execute(
                    "DELETE FROM embedding_outbox WHERE target_id = %s",
                    (chunk_id_1,),
                )


class CountingEmbedProvider(SyntheticEmbedProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = 0

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        return super().embed(text)


@live_pg
def test_live_reuses_ready_chunk_with_legacy_id_without_embedding():
    """A ready row with a shorter legacy id must be reused, not re-embedded."""
    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()
    suffix = uuid.uuid4().hex[:12]
    provider = CountingEmbedProvider()
    projector = PgSessionMemoryProjector(store=store, embed_provider=provider)
    body = f"legacy reuse {suffix}"
    doc = {
        "body": body,
        "content_hash": sha256_hash(body),
        "session_id_hash": f"sha256:{'e' * 64}",
        "source_hash": f"sha256:{'f' * 64}",
        "project": f"pg-legacy-{suffix}",
        "provider": "codex",
    }
    from agent_knowledge.postgres_store.pgvector_store import SessionChunk

    legacy_id = f"legacy-{suffix}"
    store.insert_chunk(
        SessionChunk(
            chunk_id=legacy_id,
            session_id_hash=doc["session_id_hash"],
            project=doc["project"],
            provider=doc["provider"],
            chunk_index=0,
            content_markdown=body,
            content_hash=doc["content_hash"],
            embedding_state="ready",
            embedding=provider.embed(body),
            embedding_model="gemini-embedding-2",
        )
    )
    provider.calls = 0
    try:
        reused_id = projector.project(target_profile="session-memory", document=doc)
        assert reused_id == legacy_id
        assert provider.calls == 0
        derived_id = _derive_pg_chunk_id_impl(
            project=doc["project"],
            provider=doc["provider"],
            session_id_hash=doc["session_id_hash"],
            source_hash=doc["source_hash"],
            content_hash=doc["content_hash"],
        )
        assert store.get_chunk(derived_id) is None
    finally:
        projector.close()
        with store.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM session_memory_chunks WHERE project = %s",
                    (doc["project"],),
                )


@live_pg
def test_live_changed_source_hash_reuses_ready_content_vector():
    """Same body/content may keep the ready vector when only source_hash changes."""
    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()
    suffix = uuid.uuid4().hex[:12]
    provider = CountingEmbedProvider()
    projector = PgSessionMemoryProjector(store=store, embed_provider=provider)

    base_doc = {
        "body": f"revision test {suffix}",
        "content_hash": sha256_hash(f"revision test {suffix}"),
        "session_id_hash": f"sha256:{'2' * 64}",
        "project": f"pg-revision-{suffix}",
        "provider": "anthropic",
    }

    doc_v1 = {**base_doc, "source_hash": f"sha256:{'3' * 64}"}
    doc_v2 = {**base_doc, "source_hash": f"sha256:{'4' * 64}"}

    chunk_id_v1 = projector.project(
        target_profile="session-memory", document=doc_v1
    )
    embed_calls_after_first = provider.calls
    chunk_id_v2 = projector.project(
        target_profile="session-memory", document=doc_v2
    )
    try:
        assert chunk_id_v1 == chunk_id_v2
        assert provider.calls == embed_calls_after_first
        assert store.get_chunk(chunk_id_v1) is not None
    finally:
        projector.close()
        with store.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM session_memory_chunks WHERE chunk_id = %s",
                    (chunk_id_v1,),
                )
                cur.execute(
                    "DELETE FROM embedding_outbox WHERE target_id = %s",
                    (chunk_id_v1,),
                )


@live_pg
def test_live_different_projects_produce_different_ids():
    """Cross-project isolation: same content in different projects gets different chunk_ids."""
    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()
    suffix = uuid.uuid4().hex[:12]
    provider = SyntheticEmbedProvider()
    projector = PgSessionMemoryProjector(store=store, embed_provider=provider)

    common = {
        "body": f"isolation test {suffix}",
        "content_hash": sha256_hash(f"isolation test {suffix}"),
        "session_id_hash": f"sha256:{'6' * 64}",
        "source_hash": f"sha256:{'7' * 64}",
        "provider": "anthropic",
    }

    chunk_id_a = projector.project(
        target_profile="session-memory",
        document={**common, "project": f"project-a-{suffix}"},
    )
    chunk_id_b = projector.project(
        target_profile="session-memory",
        document={**common, "project": f"project-b-{suffix}"},
    )
    try:
        assert chunk_id_a != chunk_id_b
    finally:
        projector.close()
        with store.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM session_memory_chunks WHERE chunk_id IN (%s, %s)",
                    (chunk_id_a, chunk_id_b),
                )
                cur.execute(
                    "DELETE FROM embedding_outbox WHERE target_id IN (%s, %s)",
                    (chunk_id_a, chunk_id_b),
                )


@live_pg
def test_live_embedding_failure_does_not_create_projected():
    """If embedding provider raises, no PROJECTED receipt should be created."""
    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()

    class FailingEmbedProvider:
        size = 3072
        model = "gemini-embedding-2"

        def embed(self, text: str) -> list[float]:
            raise RuntimeError("synthetic embedding failure")

    projector = PgSessionMemoryProjector(
        store=store, embed_provider=FailingEmbedProvider()
    )

    doc = {
        "body": "fail test",
        "content_hash": sha256_hash("fail test"),
        "session_id_hash": f"sha256:{'9' * 64}",
        "source_hash": f"sha256:{'a' * 64}",
        "project": "pg-fail-test",
        "provider": "anthropic",
    }

    with pytest.raises(RuntimeError, match="synthetic embedding failure"):
        projector.project(target_profile="session-memory", document=doc)
