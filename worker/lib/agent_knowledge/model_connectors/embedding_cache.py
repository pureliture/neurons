from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from collections import OrderedDict
from concurrent.futures import Future
from threading import RLock
from typing import Any

DEFAULT_EMBEDDING_CACHE_ENTRIES = 256
EMBEDDING_CACHE_ENABLED_ENV = "LLM_BRAIN_EMBEDDING_CACHE_ENABLED"


def embedding_cache_enabled(environ: dict[str, str] | None = None) -> bool:
    source = os.environ if environ is None else environ
    raw = source.get(EMBEDDING_CACHE_ENABLED_ENV, "true").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{EMBEDDING_CACHE_ENABLED_ENV} must be a boolean")


def embedding_cache_namespace(
    *,
    provider: str,
    model: str,
    base_url: str,
    embedding_dim: int,
    api_key: str,
) -> str:
    """Return a secret-safe cache namespace for one embedding profile."""

    profile = {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "embedding_dim": int(embedding_dim),
        "api_key_digest": hashlib.sha256(api_key.encode("utf-8")).hexdigest(),
    }
    serialized = json.dumps(profile, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class CachedEmbedder:
    """Bounded process-local cache and single-flight wrapper for Graphiti embedders."""

    def __init__(
        self,
        backend: Any,
        *,
        namespace: str,
        embedding_dim: int,
        max_entries: int = DEFAULT_EMBEDDING_CACHE_ENTRIES,
        enabled: bool = True,
    ) -> None:
        if not namespace:
            raise ValueError("embedding cache namespace is required")
        if embedding_dim < 1:
            raise ValueError("embedding dimension must be positive")
        if max_entries < 1:
            raise ValueError("embedding cache capacity must be positive")
        self._backend = backend
        self._namespace = namespace
        self._embedding_dim = int(embedding_dim)
        self._max_entries = int(max_entries)
        self._enabled = bool(enabled)
        self._cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._inflight: dict[str, Future[tuple[float, ...]]] = {}
        self._lock = RLock()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)

    async def create(self, input_data: Any) -> list[float]:
        if not self._enabled:
            return await self._backend.create(input_data)
        if not isinstance(input_data, str):
            return await self._backend.create(input_data)

        key = self._cache_key(input_data)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return list(cached)
            future = self._inflight.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._inflight[key] = future

        if not owner:
            return list(await asyncio.shield(asyncio.wrap_future(future)))

        try:
            vector = self._validate_vector(await self._backend.create(input_data))
        except BaseException as exc:
            self._fail_inflight(key, future, exc)
            raise

        self._cache_inflight(key, future, vector)
        return list(vector)

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        if not self._enabled:
            return await self._backend.create_batch(input_data_list)
        if not input_data_list:
            return []
        if any(not isinstance(text, str) for text in input_data_list):
            return await self._backend.create_batch(input_data_list)

        keys = [self._cache_key(text) for text in input_data_list]
        unique_inputs = dict(zip(keys, input_data_list))
        resolved: dict[str, tuple[float, ...]] = {}
        owned: dict[str, tuple[str, Future[tuple[float, ...]]]] = {}
        waiting: dict[str, Future[tuple[float, ...]]] = {}

        with self._lock:
            for key in unique_inputs:
                cached = self._cache.get(key)
                if cached is not None:
                    self._cache.move_to_end(key)
                    resolved[key] = cached
                    continue
                existing = self._inflight.get(key)
                if existing is not None:
                    waiting[key] = existing
                    continue
                future: Future[tuple[float, ...]] = Future()
                self._inflight[key] = future
                owned[key] = (unique_inputs[key], future)

        if owned:
            try:
                inputs = [text for text, _ in owned.values()]
                vectors = await self._backend.create_batch(inputs)
                if len(vectors) != len(inputs):
                    raise ValueError("embedding batch result count mismatch")
                normalized = [self._validate_vector(vector) for vector in vectors]
            except BaseException as exc:
                for key, (_, future) in owned.items():
                    self._fail_inflight(key, future, exc)
                raise

            for (key, (_, future)), vector in zip(owned.items(), normalized):
                self._cache_inflight(key, future, vector)
                resolved[key] = vector

        for key, future in waiting.items():
            resolved[key] = await asyncio.shield(asyncio.wrap_future(future))

        return [list(resolved[key]) for key in keys]

    def _cache_key(self, text: str) -> str:
        digest = hashlib.sha256()
        digest.update(self._namespace.encode("utf-8"))
        digest.update(b"\0")
        digest.update(text.encode("utf-8"))
        return digest.hexdigest()

    def _cache_inflight(
        self,
        key: str,
        future: Future[tuple[float, ...]],
        vector: tuple[float, ...],
    ) -> None:
        with self._lock:
            self._cache[key] = vector
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)
            if self._inflight.get(key) is future:
                del self._inflight[key]
            future.set_result(vector)

    def _fail_inflight(
        self,
        key: str,
        future: Future[tuple[float, ...]],
        exc: BaseException,
    ) -> None:
        with self._lock:
            if self._inflight.get(key) is future:
                del self._inflight[key]
            future.set_exception(exc)

    def _validate_vector(self, vector: Any) -> tuple[float, ...]:
        try:
            normalized = tuple(float(value) for value in vector)
        except (TypeError, ValueError) as exc:
            raise ValueError("embedding vector is invalid") from exc
        if len(normalized) != self._embedding_dim:
            raise ValueError("embedding vector dimension mismatch")
        if not all(math.isfinite(value) for value in normalized):
            raise ValueError("embedding vector contains non-finite values")
        return normalized