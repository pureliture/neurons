"""Graphiti OpenAIEmbedder local mode must follow `canonical_embedding_v1` on every call shape.

Regression tests for PR #349 review findings:
- batches larger than the service item limit must be split (P1),
- ``create([text])`` must apply the same canonical splitting as ``create(text)`` (P2),
- multi-chunk vectors must use UTF-8 byte-length weights on every path (P2).

A fake client enforces the real service limits and raises (like HTTP 413) on any violation.
"""

from __future__ import annotations

import asyncio
import json
import math
from types import SimpleNamespace

import pytest

from agent_knowledge.local_embedding.profile import (
    LOCAL_MODEL2VEC_DIMENSIONS,
    LOCAL_MODEL2VEC_MAX_BATCH_ITEMS,
    LOCAL_MODEL2VEC_MAX_BATCH_UTF8_BYTES,
    LOCAL_MODEL2VEC_MAX_CHUNK_BYTES,
    LOCAL_MODEL2VEC_MAX_REQUEST_BODY_BYTES,
    LOCAL_MODEL2VEC_MODEL_ALIAS,
)
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig

DIM = LOCAL_MODEL2VEC_DIMENSIONS


class ServiceLimitError(RuntimeError):
    """Stand-in for the service's HTTP 413 `input_too_large`."""


class LimitEnforcingFakeClient:
    """Fake OpenAI client mirroring the local service limits.

    Each distinct chunk text maps to its own orthonormal basis vector, so the aggregate
    weights are directly observable in the returned vector components.
    """

    def __init__(self) -> None:
        self.requests: list[list[str]] = []
        self._basis: dict[str, int] = {}
        self.embeddings = SimpleNamespace(create=self._create)

    async def _create(self, *, input, model):  # noqa: A002 - mirrors the SDK signature
        items = [input] if isinstance(input, str) else list(input)
        if not items or len(items) > LOCAL_MODEL2VEC_MAX_BATCH_ITEMS:
            raise ServiceLimitError('too many items')
        total = 0
        for item in items:
            size = len(item.encode('utf-8'))
            if size > LOCAL_MODEL2VEC_MAX_CHUNK_BYTES:
                raise ServiceLimitError('item too large')
            total += size
        if total > LOCAL_MODEL2VEC_MAX_BATCH_UTF8_BYTES:
            raise ServiceLimitError('batch too large')
        body = json.dumps({'model': model, 'input': items}, ensure_ascii=False).encode('utf-8')
        if len(body) > LOCAL_MODEL2VEC_MAX_REQUEST_BODY_BYTES:
            raise ServiceLimitError('body too large')

        self.requests.append(items)
        data = []
        for index, item in enumerate(items):
            slot = self._basis.setdefault(item, len(self._basis))
            assert slot < DIM, 'test uses more distinct chunks than basis dimensions'
            vector = [0.0] * DIM
            vector[slot] = 1.0
            data.append(SimpleNamespace(index=index, embedding=vector))
        return SimpleNamespace(data=data)

    def slot_of(self, text: str) -> int:
        return self._basis[text]


@pytest.fixture
def client() -> LimitEnforcingFakeClient:
    return LimitEnforcingFakeClient()


@pytest.fixture
def embedder(client: LimitEnforcingFakeClient) -> OpenAIEmbedder:
    config = OpenAIEmbedderConfig(
        embedding_model=LOCAL_MODEL2VEC_MODEL_ALIAS,
        embedding_dim=DIM,
        api_key='unused',
    )
    return OpenAIEmbedder(config=config, client=client)  # type: ignore[arg-type]


def _run(coro):
    return asyncio.run(coro)


def test_create_batch_splits_beyond_service_item_limit(embedder, client):
    texts = [f'entity-{index}' for index in range(40)]

    vectors = _run(embedder.create_batch(texts))

    assert len(vectors) == len(texts)
    assert all(len(vector) == DIM for vector in vectors)
    assert max(len(request) for request in client.requests) <= LOCAL_MODEL2VEC_MAX_BATCH_ITEMS
    assert len(client.requests) >= math.ceil(len(texts) / LOCAL_MODEL2VEC_MAX_BATCH_ITEMS)
    # Order is preserved: each text maps back to its own basis vector.
    for text, vector in zip(texts, vectors):
        assert vector[client.slot_of(text)] == pytest.approx(1.0)


def test_create_with_single_element_list_uses_canonical_splitting(embedder, client):
    long_text = 'a' * (LOCAL_MODEL2VEC_MAX_CHUNK_BYTES + 1)

    from_list = _run(embedder.create([long_text]))
    from_str = _run(embedder.create(long_text))

    assert from_list == pytest.approx(from_str)
    assert all(
        len(item.encode('utf-8')) <= LOCAL_MODEL2VEC_MAX_CHUNK_BYTES
        for request in client.requests
        for item in request
    )


@pytest.mark.parametrize('path', ['create_str', 'create_list', 'create_batch'])
def test_multi_chunk_vectors_use_utf8_byte_length_weights(embedder, client, path):
    head = 'a' * LOCAL_MODEL2VEC_MAX_CHUNK_BYTES
    text = head + 'b'  # canonical chunks: 8192 bytes + 1 byte

    if path == 'create_str':
        vector = _run(embedder.create(text))
    elif path == 'create_list':
        vector = _run(embedder.create([text]))
    else:
        vector = _run(embedder.create_batch([text]))[0]

    norm = math.sqrt(LOCAL_MODEL2VEC_MAX_CHUNK_BYTES**2 + 1)
    assert vector[client.slot_of(head)] == pytest.approx(LOCAL_MODEL2VEC_MAX_CHUNK_BYTES / norm)
    assert vector[client.slot_of('b')] == pytest.approx(1 / norm)
    assert math.sqrt(sum(component * component for component in vector)) == pytest.approx(1.0)


def test_all_call_shapes_agree_for_the_same_input(embedder):
    text = 'm' * (LOCAL_MODEL2VEC_MAX_CHUNK_BYTES * 2 + 17)

    by_str = _run(embedder.create(text))
    by_list = _run(embedder.create([text]))
    by_batch = _run(embedder.create_batch([text]))[0]

    assert by_list == pytest.approx(by_str)
    assert by_batch == pytest.approx(by_str)
