"""Sync/async, single/multi document streaming equivalence and bounded memory.

Reference vectors come from the existing canonical functions
(split_utf8_chunks_v1, plan_request_batches_v1, aggregate_chunk_vectors_v1).
All transports are in-memory fakes; no network or DB.
"""

from __future__ import annotations

import asyncio
import tracemalloc

import pytest

from agent_knowledge.local_embedding.canonical import (
    aggregate_chunk_vectors_v1,
    compute_input_hash,
    plan_request_batches_v1,
    split_utf8_chunks_v1,
)
from agent_knowledge.local_embedding.client import (
    CanonicalLocalEmbeddingClient,
    LocalModel2VecGraphitiEmbedder,
)


def _vec_for(index: int, dim: int = 256) -> list[float]:
    return [((index * 37 + j * 11) % 997 + 1) / 1000.0 for j in range(dim)]


class _SyncTransport:
    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.batches: list[list[str]] = []

    def __call__(self, batch: list[str]):
        self.batches.append(list(batch))
        vectors = []
        for chunk in batch:
            self.chunks.append(chunk)
            vectors.append(_vec_for(len(self.chunks) - 1))
        return vectors, len(batch) * 7


class _AsyncTransport(_SyncTransport):
    async def __call__(self, batch: list[str]):
        return super().__call__(batch)


class _CountSyncTransport:
    def __init__(self) -> None:
        self.chunk_count = 0

    def __call__(self, batch: list[str]):
        vectors = []
        for _chunk in batch:
            vectors.append(_vec_for(self.chunk_count))
            self.chunk_count += 1
        return vectors, len(batch)


def _client(transport) -> CanonicalLocalEmbeddingClient:
    client = CanonicalLocalEmbeddingClient(
        base_url="http://127.0.0.1:1/v1", max_retries=0
    )
    client._send_chunk_batch_sync = transport  # type: ignore[method-assign]
    return client


def _reference(texts: list[str]):
    per_doc_chunks = [list(split_utf8_chunks_v1(text)) for text in texts]
    flat = [chunk for chunks in per_doc_chunks for chunk in chunks]
    batches = plan_request_batches_v1(flat)
    vectors = [_vec_for(i) for i in range(len(flat))]
    cursor = 0
    results = []
    for chunks in per_doc_chunks:
        doc_vectors = vectors[cursor:cursor + len(chunks)]
        cursor += len(chunks)
        results.append(
            aggregate_chunk_vectors_v1(
                doc_vectors, [len(chunk.encode("utf-8")) for chunk in chunks]
            )
        )
    return flat, batches, results


TEXTS = [
    ["a" * 300_000],
    ["a" * 300_000, "가" * 120_000],
    ["가" * 120_000, "😀" * 40_000, "tail"],
    ["x" * 8192],
    ["x", "y"],
]


@pytest.mark.parametrize("texts", TEXTS, ids=[str(len(t)) + "-" + str(i) for i, t in enumerate(TEXTS)])
def test_sync_multi_document_streaming_matches_reference(texts):
    transport = _SyncTransport()
    result = _client(transport).embed_documents(texts)
    flat, batches, expected = _reference(texts)

    assert transport.chunks == flat
    assert transport.batches == batches
    assert [r.vector for r in result] == expected
    assert [r.chunk_count for r in result] == [
        len(split_utf8_chunks_v1(text)) for text in texts
    ]
    assert [r.input_hash for r in result] == [compute_input_hash(t) for t in texts]
    assert all(r.prompt_tokens == (len(flat) * 7 if len(texts) == 1 else 0) for r in result)


def test_async_single_and_multi_match_sync_results():
    for texts in TEXTS:
        sync_transport = _SyncTransport()
        async_transport = _AsyncTransport()
        sync = _client(sync_transport).embed_documents(texts)
        async_client = _client(_SyncTransport())
        async_client._send_chunk_batch_async = async_transport  # type: ignore[method-assign]
        async_results = asyncio.run(async_client.aembed_documents(texts))

        assert [r.vector for r in async_results] == [r.vector for r in sync]
        assert [r.input_hash for r in async_results] == [r.input_hash for r in sync]
        assert async_transport.batches == sync_transport.batches


def test_async_graphiti_embedder_batch_uses_streaming_path():
    client = _client(_SyncTransport())
    client._send_chunk_batch_async = _AsyncTransport()  # type: ignore[method-assign]
    embedder = LocalModel2VecGraphitiEmbedder(canonical_client=client)
    texts = ["a" * 200_000, "가" * 80_000]

    vectors = asyncio.run(embedder.create_batch(texts))

    _, _, expected = _reference(texts)
    assert vectors == expected


def test_multi_document_streaming_peak_is_bounded():
    texts = ["a" * 600_000, "가" * 600_000]
    transport = _CountSyncTransport()
    client = _client(transport)

    tracemalloc.start()
    tracemalloc.reset_peak()
    result = client.embed_documents(texts)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert [r.chunk_count for r in result] == [
        len(split_utf8_chunks_v1(text)) for text in texts
    ]
    assert transport.chunk_count == sum(r.chunk_count for r in result)
    assert peak < 64 * 1024 * 1024


def test_streaming_does_not_truncate_or_reorder_utf8_bytes():
    texts = ["가😀ab", "x" * 8193, "끝"]
    transport = _SyncTransport()
    _client(transport).embed_documents(texts)

    joined = "".join(transport.chunks).encode("utf-8")
    assert joined == "".join(texts).encode("utf-8")
    assert all(len(chunk.encode("utf-8")) <= 8192 for chunk in transport.chunks)
