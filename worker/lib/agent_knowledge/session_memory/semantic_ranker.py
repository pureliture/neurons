"""Embedding-backed semantic ranking for LLM-brain eval queries.

This module is optional at runtime: tests can inject an embedding provider, while
live eval uses the existing OpenAI-compatible ``LLM_BRAIN_EMBEDDING_*`` config.
It does not persist vectors or raw text; vectors are process-local and scoped to
the current eval run.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Protocol

_DEFAULT_SEMANTIC_RANKER_CACHE_SIZE = 512


class EmbeddingProvider(Protocol):
    @property
    def size(self) -> int: ...

    def embed(self, text: str) -> list[float]: ...


class EmbeddingSemanticRanker:
    """Rank accepted MemoryCards by cosine similarity to the query embedding."""

    def __init__(
        self,
        *,
        embedding_provider: EmbeddingProvider,
        max_cache_size: int = _DEFAULT_SEMANTIC_RANKER_CACHE_SIZE,
    ) -> None:
        self._provider = embedding_provider
        self._max_cache_size = max(1, int(max_cache_size))
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._cache_lock = threading.Lock()

    def __call__(
        self,
        *,
        query: str,
        query_terms: Sequence[str] | None = None,
        cards: list[dict],
        limit: int,
    ) -> list[dict]:
        query_text = _semantic_query_text(query=query, query_terms=query_terms)
        query_vector = self._embed(query_text)
        ranked: list[tuple[int, float, dict]] = []
        for index, card in enumerate(cards):
            card_text = _card_search_text(card)
            score = _cosine(query_vector, self._embed(card_text)) if card_text else 0.0
            enriched = dict(card)
            enriched["_semantic_score"] = score
            ranked.append((index, score, enriched))
        ranked.sort(key=lambda item: (-item[1], item[0]))
        bounded_limit = max(0, int(limit))
        return [card for _, _, card in ranked[:bounded_limit]]

    def close(self) -> None:
        close = getattr(self._provider, "close", None)
        if callable(close):
            close()

    def embed_query(self, query: str) -> list[float]:
        """Return the query vector for a database-backed semantic search.

        ``__call__`` remains the legacy in-process card ranker. The
        rationalized pgvector path only needs the query embedding; PostgreSQL
        ranks the persisted card vectors server-side.
        """

        return list(self._embed(str(query or "")))

    def _embed(self, text: str) -> list[float]:
        key = str(text or "")
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
        vector = [float(value) for value in self._provider.embed(key)]
        if len(vector) != int(self._provider.size):
            raise ValueError("embedding endpoint returned wrong vector size")
        with self._cache_lock:
            self._cache[key] = vector
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_cache_size:
                self._cache.popitem(last=False)
            return self._cache[key]


def build_embedding_semantic_ranker(*, environ: Mapping[str, str] | None = None) -> EmbeddingSemanticRanker:
    """Build a live embedding ranker from existing model connector env."""

    from agent_knowledge.rag_ingress.embedding_provider import build_openai_embedding_provider

    return EmbeddingSemanticRanker(embedding_provider=build_openai_embedding_provider(environ=environ))


def _semantic_query_text(*, query: str, query_terms: Sequence[str] | None) -> str:
    terms = [str(term).strip() for term in query_terms or [] if str(term).strip()]
    return "\n".join(terms) if terms else str(query or "")


def _card_search_text(card: Mapping[str, object]) -> str:
    payload = card.get("typed_payload")
    payload_values = payload.values() if isinstance(payload, Mapping) else []
    return " ".join(
        str(value or "")
        for value in (
            card.get("title"),
            card.get("summary"),
            card.get("render_text"),
            card.get("card_type"),
            *payload_values,
        )
    ).strip()


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(float(x) * float(y) for x, y in zip(a, b))
    norm_a = math.sqrt(sum(float(x) * float(x) for x in a))
    norm_b = math.sqrt(sum(float(y) * float(y) for y in b))
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


__all__ = [
    "EmbeddingSemanticRanker",
    "build_embedding_semantic_ranker",
]
