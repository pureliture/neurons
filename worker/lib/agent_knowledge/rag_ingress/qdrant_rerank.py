"""Optional Qdrant mirror rerank consumer for shared model connectors."""

from __future__ import annotations

import os
from collections.abc import Mapping

from agent_knowledge.model_connectors import (
    CandidateReranker,
    FunctionRerankerClient,
    ModelConnectorConfigError,
    RankFn,
    build_reranker_client,
    resolve_model_connection_config,
    resolve_reranker_config as _resolve_reranker_config,
)
from agent_knowledge.rag_ingress.qdrant_docling_mirror import SearchableMirrorUnavailable


class OpenAICompatibleReranker(CandidateReranker):
    """Back-compat Qdrant facade over the shared CandidateReranker."""

    def __init__(
        self,
        *,
        rank_fn: RankFn,
        text_key: str = "summary",
        timeout_seconds: float | None = None,
    ) -> None:
        super().__init__(FunctionRerankerClient(rank_fn, timeout_seconds=timeout_seconds), text_key=text_key)

    def rerank(self, **kwargs):
        try:
            return super().rerank(**kwargs)
        except TimeoutError as exc:
            raise SearchableMirrorUnavailable("reranker timed out") from exc


class _QdrantLiveReranker(CandidateReranker):
    def rerank(self, **kwargs):
        try:
            return super().rerank(**kwargs)
        except TimeoutError as exc:
            raise SearchableMirrorUnavailable("reranker timed out") from exc
        except ModelConnectorConfigError as exc:
            raise SearchableMirrorUnavailable(str(exc)) from exc


def resolve_reranker_config(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Back-compat wrapper for shared reranker connection config."""

    return _resolve_reranker_config(environ)


def build_openai_reranker(
    *,
    environ: Mapping[str, str] | None = None,
    rank_fn: RankFn | None = None,
) -> CandidateReranker:
    """Build the shared candidate reranker for Qdrant mirror hits."""

    env = os.environ if environ is None else environ
    timeout_seconds = _positive_float(env.get("LLM_BRAIN_RERANK_TIMEOUT_SECONDS"))
    if rank_fn is not None:
        return OpenAICompatibleReranker(rank_fn=rank_fn, timeout_seconds=timeout_seconds)
    if not _truthy(env.get("LLM_BRAIN_QDRANT_RERANK_ENABLED", "")):
        raise SearchableMirrorUnavailable(
            "reranker disabled (set LLM_BRAIN_QDRANT_RERANK_ENABLED=1)"
        )
    try:
        client = build_reranker_client(
            resolve_model_connection_config(env),
            api_key=_value(env, "LLM_BRAIN_LLM_API_KEY", "OPENAI_API_KEY"),
            timeout_seconds=timeout_seconds,
        )
    except ModelConnectorConfigError as exc:
        raise SearchableMirrorUnavailable(str(exc)) from exc
    return _QdrantLiveReranker(client)


def _value(env: Mapping[str, str], primary: str, fallback: str) -> str:
    return str(env.get(primary) or env.get(fallback) or "")


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _positive_float(value: str | None) -> float | None:
    try:
        parsed = float(str(value or "").strip()) if str(value or "").strip() else 30.0
    except ValueError:
        return 30.0
    return parsed if parsed > 0 else 30.0


__all__ = [
    "OpenAICompatibleReranker",
    "build_openai_reranker",
    "resolve_reranker_config",
]
