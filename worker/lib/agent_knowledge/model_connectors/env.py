from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace

from .specs import EmbeddingSpec, ModelConnectionConfig, ModelEndpointSpec, RerankerSpec

DEFAULT_EMBEDDING_MODEL = "gemini-embedding-2"
DEFAULT_EMBEDDING_DIM = 3072
DEFAULT_EMBEDDING_PROFILE_ID = "lbrain-memory-gemini-embedding-2-v1"
_OLLAMA_DEFAULT_EMBEDDING_MODEL = "nomic-embed-text"
_OLLAMA_DEFAULT_EMBEDDING_DIM = 768


def resolve_model_connection_config(
    environ: Mapping[str, str] | None = None,
) -> ModelConnectionConfig:
    """Resolve non-secret model connector config from canonical and legacy env."""

    env = os.environ if environ is None else environ
    provider = _value(env, "LLM_BRAIN_GRAPH_LLM_PROVIDER", "GRAPHITI_LLM_PROVIDER", default="openai").lower()
    llm_model = _value(env, "LLM_BRAIN_LLM_MODEL", "MODEL_NAME")
    llm_base_url = _value(env, "LLM_BRAIN_LLM_BASE_URL", "OPENAI_BASE_URL")
    embedding = resolve_embedding_spec(env)
    embedding_model_is_explicit = _has_value(env, "LLM_BRAIN_EMBEDDING_MODEL", "EMBEDDING_MODEL")
    embedding_dim_is_explicit = _has_value(env, "LLM_BRAIN_EMBEDDING_DIM")
    if not _has_value(env, "LLM_BRAIN_EMBEDDING_PROVIDER", "EMBEDDING_PROVIDER") and not _has_value(
        env, "LLM_BRAIN_EMBEDDING_BASE_URL"
    ):
        embedding = replace(embedding, provider=provider)
        if not embedding_model_is_explicit:
            embedding = replace(embedding, model=_default_embedding_model(provider))
        if not embedding_dim_is_explicit:
            embedding = replace(
                embedding,
                dim=_default_embedding_dim(provider, embedding.model),
            )
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
    provider = _value(env, "LLM_BRAIN_EMBEDDING_PROVIDER", "EMBEDDING_PROVIDER", default="openai").lower()
    model = _value(
        env,
        "LLM_BRAIN_EMBEDDING_MODEL",
        "EMBEDDING_MODEL",
        default=_default_embedding_model(provider),
    )
    return EmbeddingSpec(
        provider=provider,
        model=model,
        base_url=_value(env, "LLM_BRAIN_EMBEDDING_BASE_URL", "OPENAI_BASE_URL"),
        dim=_positive_int(
            _value(env, "LLM_BRAIN_EMBEDDING_DIM"),
            default=_default_embedding_dim(provider, model),
        ),
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


def _default_embedding_model(provider: str) -> str:
    return _OLLAMA_DEFAULT_EMBEDDING_MODEL if provider == "ollama" else DEFAULT_EMBEDDING_MODEL


def _default_embedding_dim(provider: str, model: str) -> int:
    if provider == "ollama" and model.strip().lower() == _OLLAMA_DEFAULT_EMBEDDING_MODEL:
        return _OLLAMA_DEFAULT_EMBEDDING_DIM
    if model.strip().lower() == DEFAULT_EMBEDDING_MODEL:
        return DEFAULT_EMBEDDING_DIM
    return DEFAULT_EMBEDDING_DIM


def _positive_int(value: str, *, default: int) -> int:
    try:
        parsed = int(str(value or "").strip()) if str(value or "").strip() else int(default)
    except ValueError:
        return int(default)
    return parsed if parsed > 0 else int(default)
