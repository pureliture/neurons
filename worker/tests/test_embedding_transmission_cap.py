from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_knowledge.couchdb_source.document_model import INDEX_RECALL_PROFILE, sha256_hash
from agent_knowledge.rag_ingress.pg_backfill import PgSessionMemoryProjector
from agent_knowledge.rag_ingress.pg_embedding_chunks import EmbeddingChunksPending
from agent_knowledge.session_memory.chunk_overlap import ChunkView
from test_pg_backfill_local_identity import (
    InMemoryLocalPgStore,
    SegmentEmbedProvider,
    _document,
)

CAP = 5


class CappedStore(InMemoryLocalPgStore):
    def __init__(self) -> None:
        super().__init__()
        self.insert_count = 0

    def insert_chunk_with_local_outbox(self, chunk, **_kwargs):
        self.rows[chunk.chunk_id] = chunk
        self.local_embeddings[chunk.chunk_id] = {
            "state": "pending",
            "source_content_hash": chunk.content_hash,
            "input_hash": "test-input",
            "vector": None,
        }
        self.insert_count += 1
        return chunk.chunk_id, self.insert_count

    def has_dead_letter_local_outbox(self, **_kwargs):
        return False


class CappedWorker:
    last_batch_attempted = 0
    last_batch_failed = 0
    batch_size = 1

    def __init__(self, *, store, embedding_fn, **_kwargs) -> None:
        self.store = store
        self.embedding_fn = embedding_fn

    def run_once(self, *, batch_size=None):
        pending = [
            chunk_id
            for chunk_id, local in self.store.local_embeddings.items()
            if local["state"] == "pending"
        ]
        take = min(int(batch_size or self.batch_size), len(pending))
        for chunk_id in pending[:take]:
            text = self.store.rows[chunk_id].content_markdown
            self.store.local_embeddings[chunk_id].update(
                state="ready", vector=self.embedding_fn(text)
            )
        self.last_batch_attempted = take
        return take


def _document_with_views(count: int) -> dict:
    views = []
    for index in range(count):
        text = f"segment {index} " + ("x" * 64)
        views.append(
            ChunkView(
                content_hash=sha256_hash(f"view-{index}-{text}"),
                turn_start_index=index,
                turn_end_index=index,
                part_index=1,
                part_count=1,
                char_start=0,
                char_end=len(text),
                redaction_version="test",
                text=text,
            )
        )
    return {
        **_document("synthetic session body"),
        "conversation_views": tuple(views),
        "tool_evidence_bodies": (),
    }


def _projector(store, provider):
    return PgSessionMemoryProjector(
        store, provider, representation="segment_v1", segment_max_bytes=80
    )


def test_actual_embedding_egress_never_exceeds_cycle_cap(monkeypatch) -> None:
    monkeypatch.setenv("PG_EMBEDDING_CHUNKS_PER_CYCLE", str(CAP))
    monkeypatch.setattr(
        "agent_knowledge.postgres_store.outbox_worker.LocalEmbeddingOutboxWorker",
        CappedWorker,
    )
    store = CappedStore()
    provider = SegmentEmbedProvider()
    document = _document_with_views(20)

    with pytest.raises(EmbeddingChunksPending):
        _projector(store, provider).project(
            target_profile=INDEX_RECALL_PROFILE, document=document
        )

    assert provider.calls == CAP


def test_remaining_egress_is_processed_on_next_projector(monkeypatch) -> None:
    monkeypatch.setenv("PG_EMBEDDING_CHUNKS_PER_CYCLE", str(CAP))
    monkeypatch.setattr(
        "agent_knowledge.postgres_store.outbox_worker.LocalEmbeddingOutboxWorker",
        CappedWorker,
    )
    store = CappedStore()
    provider = SegmentEmbedProvider()
    document = _document_with_views(12)

    with pytest.raises(EmbeddingChunksPending):
        _projector(store, provider).project(
            target_profile=INDEX_RECALL_PROFILE, document=document
        )
    first = provider.calls
    with pytest.raises(EmbeddingChunksPending):
        _projector(store, provider).project(
            target_profile=INDEX_RECALL_PROFILE, document=document
        )
    second = provider.calls

    assert first == CAP
    assert second == CAP * 2
