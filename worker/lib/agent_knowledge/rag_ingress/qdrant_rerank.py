"""Optional reranker seam for Qdrant searchable-mirror candidates.

RAGFlow retrieval accepts an optional ``rerank_id``. The Qdrant mirror keeps parity
by reusing the *same* OpenAI-compatible reranker (cross-encoder) the Graphiti
adapter already wires (``OpenAIRerankerClient`` over ``LLM_BRAIN_LLM_*``). No new
model is chosen.

This is a composable post-step over mirror hits, not wired into the adapter by
default (the spec marks rerank optional). Tests inject ``rank_fn`` so they run with
no network and no optional dependency; the live endpoint is wired lazily.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Mapping

from .qdrant_docling_mirror import SearchableMirrorUnavailable

# rank_fn(query, [text, ...]) -> [score, ...] aligned with the input order.
RankFn = Callable[[str, list[str]], list[float]]


class OpenAICompatibleReranker:
    """Reorders mirror candidates by an injected relevance score function."""

    def __init__(self, *, rank_fn: RankFn, text_key: str = "summary") -> None:
        self._rank_fn = rank_fn
        self._text_key = str(text_key or "summary")

    def rerank(
        self,
        *,
        query: str,
        candidates: list[dict[str, Any]],
        top_n: int = 5,
    ) -> list[dict[str, Any]]:
        if not candidates:
            return []
        texts = [str(item.get(self._text_key) or item.get("summary") or "") for item in candidates]
        scores = list(self._rank_fn(str(query or ""), texts))
        if len(scores) != len(candidates):
            raise ValueError("reranker returned wrong score count")
        order = sorted(range(len(candidates)), key=lambda i: float(scores[i]), reverse=True)
        ranked: list[dict[str, Any]] = []
        for index in order[: max(1, int(top_n))]:
            item = dict(candidates[index])
            item["rerank_score"] = float(scores[index])
            ranked.append(item)
        return ranked


def resolve_reranker_config(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Reuse the same OpenAI-compatible LLM endpoint as the graph adapter."""

    env = environ if environ is not None else os.environ
    return {
        "model": env.get("LLM_BRAIN_LLM_MODEL") or env.get("MODEL_NAME") or "",
        "base_url": env.get("LLM_BRAIN_LLM_BASE_URL") or env.get("OPENAI_BASE_URL") or "",
        "api_key": env.get("LLM_BRAIN_LLM_API_KEY") or env.get("OPENAI_API_KEY") or "",
    }


def build_openai_reranker(
    *,
    environ: Mapping[str, str] | None = None,
    rank_fn: RankFn | None = None,
) -> OpenAICompatibleReranker:
    """Build the reranker. ``rank_fn`` is injectable for tests/local."""

    if rank_fn is None:
        rank_fn = _openai_rank_fn(resolve_reranker_config(environ))
    return OpenAICompatibleReranker(rank_fn=rank_fn)


def _openai_rank_fn(config: dict[str, str]) -> RankFn:  # pragma: no cover - live path only
    model = str(config.get("model") or "")
    if not model:
        raise SearchableMirrorUnavailable(
            "reranker model not configured (set LLM_BRAIN_LLM_MODEL)"
        )
    raise SearchableMirrorUnavailable(
        "live OpenAI-compatible reranker wiring is a follow-on; inject rank_fn for now"
    )


__all__ = [
    "OpenAICompatibleReranker",
    "build_openai_reranker",
    "resolve_reranker_config",
]
