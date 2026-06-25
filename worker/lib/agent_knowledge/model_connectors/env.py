from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace

from .specs import EmbeddingSpec, ModelConnectionConfig, ModelEndpointSpec, RerankerSpec

DEFAULT_EMBEDDING_DIM = 1024


def resolve_model_connection_config(
    environ: Mapping[str, str] | None = None,
) -> ModelConnectionConfig:
    """Resolve non-secret model connector config from canonical and legacy env."""

    env = os.environ if environ is None else environ
    provider = _value(env, "LLM_BRAIN_GRAPH_LLM_PROVIDER", "GRAPHITI_LLM_PROVIDER", default="openai").lower()
    llm_model = _value(env, "LLM_BRAIN_LLM_MODEL", "MODEL_NAME")
    llm_base_url = _value(env, "LLM_BRAIN_LLM_BASE_URL", "OPENAI_BASE_URL")
    embedding = resolve_embedding_spec(env)
    if not _has_value(env, "LLM_BRAIN_EMBEDDING_PROVIDER", "EMBEDDING_PROVIDER") and not _has_value(
        env, "LLM_BRAIN_EMBEDDING_BASE_URL"
    ):
        embedding = replace(embedding, provider=provider)
    if not _has_value(env, "LLM_BRAIN_EMBEDDING_BASE_URL"):
        embedding = replace(embedding, base_url=llm_base_url)
    return ModelConnectionConfig(
        llm=ModelEndpointSpec(
            provider=provider,
            model=llm_model,
            small_model=_value(env, "LLM_BRAIN_SMALL_LLM_MODEL", "SMALL_MODEL_NAME"),
            base_url=llm_base_url,
        ),
        embedding=embedding,
        reranker=resolve_reranker_spec(env, provider=provider, model=llm_model, base_url=llm_base_url),
        fallback_llm_model=_value(
            env,
            "LLM_BRAIN_LLM_FALLBACK_MODEL",
            "LLM_BRAIN_GRAPH_FALLBACK_LLM_MODEL",
        ),
        fallback_small_model=_value(
            env,
            "LLM_BRAIN_SMALL_LLM_FALLBACK_MODEL",
            "LLM_BRAIN_GRAPH_FALLBACK_SMALL_LLM_MODEL",
        ),
        primary_attempts=_positive_int(_value(env, "LLM_BRAIN_GRAPH_PRIMARY_ATTEMPTS"), default=1),
        fallback_attempts=_positive_int(_value(env, "LLM_BRAIN_GRAPH_FALLBACK_ATTEMPTS"), default=1),
    )


def resolve_embedding_spec(environ: Mapping[str, str] | None = None) -> EmbeddingSpec:
    """Resolve the shared non-secret embedding spec; API keys stay at build edges."""

    env = os.environ if environ is None else environ
    return EmbeddingSpec(
        provider=_value(env, "LLM_BRAIN_EMBEDDING_PROVIDER", "EMBEDDING_PROVIDER", default="openai").lower(),
        model=_value(env, "LLM_BRAIN_EMBEDDING_MODEL", "EMBEDDING_MODEL"),
        base_url=_value(env, "LLM_BRAIN_EMBEDDING_BASE_URL", "OPENAI_BASE_URL"),
        dim=_positive_int(_value(env, "LLM_BRAIN_EMBEDDING_DIM"), default=DEFAULT_EMBEDDING_DIM),
    )


def resolve_reranker_spec(
    environ: Mapping[str, str] | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> RerankerSpec:
    """Resolve the shared non-secret reranker spec from the LLM endpoint."""

    env = os.environ if environ is None else environ
    return RerankerSpec(
        provider=(provider or _value(env, "LLM_BRAIN_GRAPH_LLM_PROVIDER", "GRAPHITI_LLM_PROVIDER", default="openai")).lower(),
        model=model if model is not None else _value(env, "LLM_BRAIN_LLM_MODEL", "MODEL_NAME"),
        base_url=base_url if base_url is not None else _value(env, "LLM_BRAIN_LLM_BASE_URL", "OPENAI_BASE_URL"),
    )


def _value(env: Mapping[str, str], primary: str, fallback: str | None = None, *, default: str = "") -> str:
    primary_value = str(env.get(primary) or "").strip()
    if primary_value:
        return primary_value
    if fallback is not None:
        fallback_value = str(env.get(fallback) or "").strip()
        if fallback_value:
            return fallback_value
    return default


def _has_value(env: Mapping[str, str], *names: str) -> bool:
    return any(str(env.get(name) or "").strip() for name in names)


def _positive_int(value: str, *, default: int) -> int:
    try:
        parsed = int(str(value or "").strip()) if str(value or "").strip() else int(default)
    except ValueError:
        return int(default)
    return parsed if parsed > 0 else int(default)
