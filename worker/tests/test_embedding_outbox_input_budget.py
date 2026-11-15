"""Behavioral regression for oversized local outbox payloads.

Before this change the offline consumer rejected every local payload above
524288 UTF-8 bytes with a bare ValueError, so LocalEmbeddingOutboxWorker
recorded ``terminal_error:ValueError`` and dead-lettered the job. The provider
already had the canonical 8 KiB chunker; the fix streams that chunker instead
of rejecting the document.

All transports here are in-memory fakes. No network call, no DB write, and no
production data is used. Synthetic payloads contain no real secrets.
"""

from __future__ import annotations

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
    LocalEmbeddingClientError,
)
from agent_knowledge.model_connectors import (
    DEFAULT_EMBEDDING_PROFILE_ID,
    LOCAL_EMBEDDING_PROFILE_ID,
)
from agent_knowledge.postgres_store.embedding_outbox_cli import (
    assert_embedding_input_budget,
    build_embed_fn,
)
from agent_knowledge.postgres_store.outbox_worker import LocalEmbeddingOutboxWorker
from agent_knowledge.postgres_store.pgvector_store import LocalOutboxJob
from agent_knowledge.rag_ingress.embedding_provider import (
    OpenAICompatibleEmbeddingProvider,
    build_openai_embedding_provider,
)


def _vec_for(index: int, dim: int = 256) -> list[float]:
    return [((index * 37 + j * 11) % 997 + 1) / 1000.0 for j in range(dim)]


class _MockTransport:
    """Deterministic in-memory replacement for _send_chunk_batch_sync."""

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


def _streaming_client(transport: _MockTransport) -> CanonicalLocalEmbeddingClient:
    client = CanonicalLocalEmbeddingClient(
        base_url="http://127.0.0.1:1/v1", max_retries=0
    )
    client._send_chunk_batch_sync = transport  # type: ignore[method-assign]
    return client


class _RecordingProvider:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    size = 256
    model = "local-model2vec-256-v1"

    def embed(self, text: str) -> list[float]:
        self.inputs.append(text)
        return _vec_for(len(self.inputs))


def test_old_policy_rejected_but_local_cli_now_processes_oversized_payload():
    oversized = "a" * (524288 + 1)
    provider = _RecordingProvider()
    report = {"model_calls": 0}

    # The old policy still exists as a function and still rejects this size...
    with pytest.raises(LocalEmbeddingClientError) as excinfo:
        assert_embedding_input_budget(oversized, LOCAL_EMBEDDING_PROFILE_ID)
    assert excinfo.value.code == "embedding_input_budget_exceeded"

    # ...but the local outbox path no longer applies it.
    embed = build_embed_fn(provider, LOCAL_EMBEDDING_PROFILE_ID, report)
    vector = embed(oversized)

    assert len(vector) == 256
    assert provider.inputs == [oversized]  # streaming happens inside the provider
    assert report["model_calls"] == 1


def test_non_local_profile_keeps_paid_egress_budget():
    provider = _RecordingProvider()
    report = {"model_calls": 0}
    embed = build_embed_fn(provider, DEFAULT_EMBEDDING_PROFILE_ID, report)

    with pytest.raises(LocalEmbeddingClientError) as excinfo:
        embed("a" * (32768 + 1))

    assert excinfo.value.code == "embedding_input_budget_exceeded"
    assert provider.inputs == []


def test_privacy_gate_still_runs_before_any_embedding():
    provider = _RecordingProvider()
    report = {"model_calls": 0}
    embed = build_embed_fn(provider, LOCAL_EMBEDDING_PROFILE_ID, report)
    synthetic_token = "sk-proj-" + "A" * 24

    with pytest.raises(ValueError):
        embed("note: " + synthetic_token)

    assert provider.inputs == []
    assert report["model_calls"] == 0


@pytest.mark.parametrize(
    "text",
    [
        "a" * 600_000,
        ("한글😀ab" * 60_000)[:600_000],
        "a" * 8191,
        "a" * 8192,
        "a" * 8193,
        "가" * 4097,
    ],
    ids=["ascii600k", "multibyte600k", "8191", "8192", "8193", "korean4097"],
)
def test_streamed_vector_matches_existing_weighted_aggregate(text):
    transport = _MockTransport()
    client = _streaming_client(transport)

    result = client.embed_document(text)

    chunks = split_utf8_chunks_v1(text)
    assert transport.chunks == list(chunks)
    assert transport.batches == plan_request_batches_v1(list(chunks))
    assert all(len(chunk.encode("utf-8")) <= 8192 for chunk in transport.chunks)
    assert result.chunk_count == len(chunks)
    assert result.input_hash == compute_input_hash(text)
    expected = aggregate_chunk_vectors_v1(
        [_vec_for(i) for i in range(len(chunks))],
        [len(chunk.encode("utf-8")) for chunk in chunks],
    )
    assert result.vector == expected
    assert result.prompt_tokens == len(chunks) * 7


def test_invalid_utf8_fails_before_any_egress():
    transport = _MockTransport()
    client = _streaming_client(transport)

    with pytest.raises(LocalEmbeddingClientError) as excinfo:
        client.embed_document("ok-\ud800-bad")

    assert excinfo.value.code == "invalid_utf8"
    assert transport.batches == []


def test_empty_input_fails_before_any_egress():
    transport = _MockTransport()
    client = _streaming_client(transport)

    with pytest.raises(LocalEmbeddingClientError) as excinfo:
        client.embed_document("")

    assert excinfo.value.code == "empty_input"
    assert transport.batches == []


def test_single_chunk_is_not_normalized_twice():
    transport = _MockTransport()
    client = _streaming_client(transport)

    result = client.embed_document("hello")

    assert result.chunk_count == 1
    assert result.vector == _vec_for(0)


class _CountingTransport:
    """Validates 8 KiB inputs without retaining the 113 MB worth of chunks."""

    def __init__(self) -> None:
        self.chunk_count = 0
        self.total_bytes = 0
        self.max_chunk_bytes = 0
        self.max_batch_items = 0
        self.batches = 0
        self.index = 0

    def __call__(self, batch: list[str]):
        self.batches += 1
        self.max_batch_items = max(self.max_batch_items, len(batch))
        vectors = []
        for chunk in batch:
            size = len(chunk.encode("utf-8"))
            assert size <= 8192
            self.chunk_count += 1
            self.total_bytes += size
            self.max_chunk_bytes = max(self.max_chunk_bytes, size)
            vectors.append(_vec_for(self.index))
            self.index += 1
        return vectors, len(batch)


def test_max_production_size_is_streamed_with_bounded_memory_and_8k_inputs():
    # 113_783_649 bytes == 3 * 37_927_883 Korean characters (the largest failed body).
    text = "가" * 37_927_883
    assert len(text.encode("utf-8")) == 113_783_649
    transport = _CountingTransport()
    client = CanonicalLocalEmbeddingClient(
        base_url="http://127.0.0.1:1/v1", max_retries=0
    )
    client._send_chunk_batch_sync = transport  # type: ignore[method-assign]
    expected_hash = compute_input_hash(text)

    tracemalloc.start()
    tracemalloc.reset_peak()
    result = client.embed_document(text)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert transport.total_bytes == 113_783_649
    assert result.input_hash == expected_hash
    assert transport.chunk_count == result.chunk_count
    assert transport.max_chunk_bytes <= 8192
    assert transport.max_batch_items <= 16
    assert transport.batches <= 900  # ~113.8 MB / (16 * 8 KiB) request bound
    assert abs(sum(v * v for v in result.vector) - 1.0) < 1e-12
    # One batch of vectors + one 256-dim accumulator; the 113 MB source is
    # excluded because it was allocated before reset_peak().
    assert peak < 64 * 1024 * 1024


def test_real_local_provider_builds_streaming_transport(monkeypatch):
    chunks: list[str] = []

    def fake_send(self, batch):
        chunks.extend(batch)
        return [_vec_for(len(chunks) - len(batch) + i) for i in range(len(batch))], len(batch)

    monkeypatch.setattr(CanonicalLocalEmbeddingClient, "_send_chunk_batch_sync", fake_send)
    env = {
        "LLM_BRAIN_EMBEDDING_PROVIDER": "model2vec",
        "LLM_BRAIN_EMBEDDING_MODEL": "local-model2vec-256-v1",
        "LLM_BRAIN_EMBEDDING_PROFILE_ID": "lbrain-memory-model2vec-256-v1",
        "LLM_BRAIN_EMBEDDING_DIM": "256",
        "LLM_BRAIN_EMBEDDING_BASE_URL": "http://127.0.0.1:1/v1",
    }
    provider = build_openai_embedding_provider(environ=env)
    text = "가" * 300_000

    vector = provider.embed(text)

    assert len(vector) == 256
    assert len(chunks) == len(split_utf8_chunks_v1(text))
    assert all(len(chunk.encode("utf-8")) <= 8192 for chunk in chunks)


class _CapturingStore:
    def __init__(self, job: LocalOutboxJob) -> None:
        self._jobs = [job]
        self.completed: list[list[float]] = []

    def claim_local_outbox_leases(self, **_kwargs):
        jobs, self._jobs = self._jobs, []
        return jobs

    def complete_local_outbox_cas(self, *, vector, **_kwargs) -> bool:
        self.completed.append(list(vector))
        return True


def test_worker_completes_oversized_local_job_without_dead_letter():
    transport = _MockTransport()
    client = _streaming_client(transport)
    provider = OpenAICompatibleEmbeddingProvider(
        embed_fn=lambda text: client.embed_document(text).vector,
        size=256,
        model="local-model2vec-256-v1",
    )
    report = {"model_calls": 0}
    payload = ("한글😀ab" * 80_000)[:600_000]
    job = LocalOutboxJob(
        outbox_id=11,
        target_type="session_chunk",
        target_id="seg_" + "0" * 60,
        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
        source_content_hash="sha256:" + "a" * 64,
        input_hash="sha256:" + "b" * 64,
        payload_text=payload,
        fencing_token=1,
    )
    store = _CapturingStore(job)
    worker = LocalEmbeddingOutboxWorker(
        store=store,
        embedding_fn=build_embed_fn(provider, LOCAL_EMBEDDING_PROFILE_ID, report),
        batch_size=1,
        lease_seconds=60,
    )

    worker.run_once()

    chunks = split_utf8_chunks_v1(payload)
    expected = aggregate_chunk_vectors_v1(
        [_vec_for(i) for i in range(len(chunks))],
        [len(chunk.encode("utf-8")) for chunk in chunks],
    )
    assert worker.last_batch_failed == 0
    assert worker.last_batch_completed == 1
    assert store.completed == [expected]
    assert all(len(chunk.encode("utf-8")) <= 8192 for chunk in transport.chunks)
    assert report["model_calls"] == 1
