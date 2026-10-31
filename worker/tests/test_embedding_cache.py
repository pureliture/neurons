from __future__ import annotations

import asyncio

import pytest

from agent_knowledge.model_connectors.embedding_cache import (
    CachedEmbedder,
    embedding_cache_enabled,
    embedding_cache_namespace,
)


class _RecordingEmbedder:
    def __init__(self) -> None:
        self.batch_calls: list[list[str]] = []
        self.single_calls: list[object] = []
        self.fail_next_batch = False
        self.fail_next_create = False

    async def create(self, input_data):
        self.single_calls.append(input_data)
        if self.fail_next_create:
            self.fail_next_create = False
            raise RuntimeError("provider unavailable")
        return [float(len(str(input_data)))]

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        self.batch_calls.append(list(input_data_list))
        await asyncio.sleep(0.01)
        if self.fail_next_batch:
            self.fail_next_batch = False
            raise RuntimeError("provider unavailable")
        return [[float(len(text))] for text in input_data_list]


def test_cached_embedder_deduplicates_batch_and_reuses_vectors():
    backend = _RecordingEmbedder()
    embedder = CachedEmbedder(backend, namespace="model-a", embedding_dim=1)

    async def run():
        first = await embedder.create_batch(["alpha", "beta", "alpha"])
        first[0][0] = -1.0
        second = await embedder.create_batch(["alpha", "beta"])
        return first, second

    first, second = asyncio.run(run())

    assert backend.batch_calls == [["alpha", "beta"]]
    assert first == [[-1.0], [4.0], [5.0]]
    assert second == [[5.0], [4.0]]


def test_cached_embedder_coalesces_concurrent_duplicate_requests():
    backend = _RecordingEmbedder()
    embedder = CachedEmbedder(backend, namespace="model-a", embedding_dim=1)

    async def run():
        return await asyncio.gather(
            embedder.create_batch(["alpha", "beta"]),
            embedder.create_batch(["alpha", "gamma"]),
        )

    results = asyncio.run(run())

    flattened_inputs = [text for call in backend.batch_calls for text in call]
    assert sorted(flattened_inputs) == ["alpha", "beta", "gamma"]
    assert results == [[[5.0], [4.0]], [[5.0], [5.0]]]


def test_cancelled_waiter_does_not_cancel_shared_embedding():
    class _SlowEmbedder(_RecordingEmbedder):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def create(self, input_data):
            self.single_calls.append(input_data)
            self.started.set()
            await self.release.wait()
            return [float(len(str(input_data)))]

    async def run():
        backend = _SlowEmbedder()
        embedder = CachedEmbedder(backend, namespace="model-a", embedding_dim=1)
        owner = asyncio.create_task(embedder.create("alpha"))
        await backend.started.wait()
        waiter = asyncio.create_task(embedder.create("alpha"))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        backend.release.set()
        assert await owner == [5.0]
        assert await embedder.create("alpha") == [5.0]

    asyncio.run(run())


def test_cancelled_owner_retries_single_flight_waiter():
    class _FirstCallBlocksEmbedder(_RecordingEmbedder):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()

        async def create(self, input_data):
            self.single_calls.append(input_data)
            if len(self.single_calls) == 1:
                self.started.set()
                await asyncio.Event().wait()
            return [float(len(str(input_data)))]

    async def run():
        backend = _FirstCallBlocksEmbedder()
        embedder = CachedEmbedder(backend, namespace="model-a", embedding_dim=1)
        owner = asyncio.create_task(embedder.create("alpha"))
        await backend.started.wait()
        waiter = asyncio.create_task(embedder.create("alpha"))
        await asyncio.sleep(0)

        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner

        assert await waiter == [5.0]
        assert backend.single_calls == ["alpha", "alpha"]

    asyncio.run(run())


def test_cancelled_batch_owner_retries_only_unresolved_waiter_items():
    class _ControlledEmbedder(_RecordingEmbedder):
        def __init__(self) -> None:
            super().__init__()
            self.batch_started = asyncio.Event()
            self.started = {
                "beta": asyncio.Event(),
            }
            self.release = {
                "beta": asyncio.Event(),
            }

        async def create(self, input_data):
            self.single_calls.append(input_data)
            self.started[input_data].set()
            await self.release[input_data].wait()
            return [float(len(input_data))]

        async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
            self.batch_calls.append(list(input_data_list))
            if len(self.batch_calls) == 1:
                self.batch_started.set()
                await asyncio.Event().wait()
            return [[float(len(text))] for text in input_data_list]

    async def run():
        backend = _ControlledEmbedder()
        embedder = CachedEmbedder(backend, namespace="model-a", embedding_dim=1)
        alpha_owner = asyncio.create_task(embedder.create_batch(["alpha", "unused"]))
        beta_owner = asyncio.create_task(embedder.create("beta"))
        await asyncio.gather(
            backend.batch_started.wait(),
            backend.started["beta"].wait(),
        )
        waiter = asyncio.create_task(embedder.create_batch(["alpha", "beta", "alpha"]))
        await asyncio.sleep(0)

        alpha_owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await alpha_owner
        backend.release["beta"].set()
        assert await beta_owner == [4.0]

        assert await waiter == [[5.0], [4.0], [5.0]]
        assert backend.single_calls == ["beta"]
        assert backend.batch_calls == [["alpha", "unused"], ["alpha"]]

    asyncio.run(run())


def test_cached_embedder_does_not_cache_failed_requests():
    backend = _RecordingEmbedder()
    backend.fail_next_create = True
    embedder = CachedEmbedder(backend, namespace="model-a", embedding_dim=1)

    async def run():
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await embedder.create("alpha")
        return await embedder.create("alpha")

    assert asyncio.run(run()) == [5.0]
    assert backend.single_calls == ["alpha", "alpha"]
    assert backend.batch_calls == []


def test_cached_embedder_preserves_single_create_and_shares_with_batch():
    backend = _RecordingEmbedder()
    embedder = CachedEmbedder(backend, namespace="model-a", embedding_dim=1)

    async def run():
        first = await embedder.create("alpha")
        second = await embedder.create_batch(["alpha"])
        return first, second

    assert asyncio.run(run()) == ([5.0], [[5.0]])
    assert backend.single_calls == ["alpha"]
    assert backend.batch_calls == []


def test_cached_embedder_rejects_malformed_batch_without_caching_it():
    class _MalformedEmbedder(_RecordingEmbedder):
        async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
            self.batch_calls.append(list(input_data_list))
            return []

    backend = _MalformedEmbedder()
    embedder = CachedEmbedder(backend, namespace="model-a", embedding_dim=1)

    async def run():
        with pytest.raises(ValueError, match="batch result count"):
            await embedder.create_batch(["alpha"])
        with pytest.raises(ValueError, match="batch result count"):
            await embedder.create_batch(["alpha"])

    asyncio.run(run())
    assert backend.batch_calls == [["alpha"], ["alpha"]]


def test_cached_embedder_keeps_model_namespaces_isolated():
    backend = _RecordingEmbedder()
    first = CachedEmbedder(backend, namespace="model-a", embedding_dim=1)
    second = CachedEmbedder(backend, namespace="model-b", embedding_dim=1)

    async def run():
        await first.create("alpha")
        await second.create("alpha")

    asyncio.run(run())

    assert backend.single_calls == ["alpha", "alpha"]
    assert backend.batch_calls == []


def test_cached_embedder_can_be_disabled_without_changing_backend_calls():
    backend = _RecordingEmbedder()
    embedder = CachedEmbedder(
        backend,
        namespace="model-a",
        embedding_dim=1,
        enabled=False,
    )

    async def run():
        await embedder.create("alpha")
        await embedder.create("alpha")

    asyncio.run(run())

    assert backend.single_calls == ["alpha", "alpha"]
    assert backend.batch_calls == []


def test_cached_embedder_evicts_least_recently_used_vectors():
    backend = _RecordingEmbedder()
    embedder = CachedEmbedder(backend, namespace="model-a", embedding_dim=1, max_entries=2)

    async def run():
        await embedder.create_batch(["alpha", "beta"])
        await embedder.create("alpha")
        await embedder.create("gamma")
        await embedder.create("beta")

    asyncio.run(run())

    assert backend.batch_calls == [["alpha", "beta"]]
    assert backend.single_calls == ["gamma", "beta"]


def test_embedding_cache_profile_digest_does_not_expose_credentials():
    profile = {
        "provider": "openai-compatible",
        "model": "embed-v1",
        "base_url": "https://embedding.example/v1",
        "embedding_dim": 3,
    }
    namespace = embedding_cache_namespace(**profile, api_key="private-token")

    assert "private-token" not in namespace
    assert namespace != embedding_cache_namespace(**{**profile, "model": "embed-v2"}, api_key="private-token")
    assert namespace != embedding_cache_namespace(**profile, api_key="different-token")


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("off", False), ("0", False)])
def test_embedding_cache_enabled_flag(raw, expected):
    assert embedding_cache_enabled({"LLM_BRAIN_EMBEDDING_CACHE_ENABLED": raw}) is expected


def test_embedding_cache_enabled_flag_rejects_unknown_values():
    with pytest.raises(ValueError, match="must be a boolean"):
        embedding_cache_enabled({"LLM_BRAIN_EMBEDDING_CACHE_ENABLED": "sometimes"})