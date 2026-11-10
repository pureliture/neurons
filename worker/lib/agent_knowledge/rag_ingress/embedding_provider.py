"""OpenAI-compatible ``EmbeddingProvider``와 연결 설정.

Graph adapter, PostgreSQL projection 및 recall이 같은
``LLM_BRAIN_EMBEDDING_*`` 환경 설정을 재사용한다.

Test에서는 ``embed_fn``을 주입해 network와 optional ``openai`` dependency 없이
검증한다. 실제 endpoint는 :func:`build_openai_embedding_provider`의 live path에서만
lazily 연결한다.
"""

from __future__ import annotations

from typing import Callable, Mapping

from agent_knowledge.model_connectors import (
    DEFAULT_EMBEDDING_DIM,
    ModelPolicy,
    PolicyViolation,
    resolve_embedding_spec,
)

EmbedFn = Callable[[str], list[float]]


class EmbeddingProviderUnavailable(RuntimeError):
    """Embedding provider configuration or connection is unavailable."""


class OpenAICompatibleEmbeddingProvider:
    """주입된 OpenAI-compatible embed function 기반 ``EmbeddingProvider``.

    반환 vector 길이는 선언된 ``size``와 맞춰 검증해서 설정 오류를
    fail closed 처리한다.
    """

    def __init__(self, *, embed_fn: EmbedFn, size: int, model: str = "") -> None:
        if int(size) <= 0:
            raise ValueError("embedding size must be positive")
        self._embed_fn = embed_fn
        self._size = int(size)
        self._model = str(model or "")
        self._close_fn = getattr(embed_fn, "close", None)

    @property
    def size(self) -> int:
        return self._size

    @property
    def model(self) -> str:
        return self._model

    def embed(self, text: str) -> list[float]:
        vector = [float(value) for value in self._embed_fn(str(text or ""))]
        if len(vector) != self._size:
            raise ValueError("embedding endpoint returned wrong vector size")
        return vector

    def close(self) -> None:
        if callable(self._close_fn):
            self._close_fn()


def resolve_embedding_config(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """Env에서 embedding 연결 config를 해석한다.

    ``GraphitiNeo4jConfig.from_env``와 같은 우선순위를 써서 embedding consumer가
    같은 endpoint를 보게 한다. api_key는 의도적으로 반환하지 않는다. base_url은
    host를 식별할 수 있으므로 production path에서 redaction 없이 log하지 않는다.
    """

    spec = resolve_embedding_spec(environ)
    return {
        "provider": spec.provider,
        "model": spec.model,
        "base_url": spec.base_url,
        "dim": spec.dim,
    }


def build_openai_embedding_provider(
    *,
    environ: Mapping[str, str] | None = None,
    embed_fn: EmbedFn | None = None,
) -> OpenAICompatibleEmbeddingProvider:
    """환경 설정에서 embedding provider를 만든다.

    ``embed_fn``은 test/local 주입용이다. 생략하면 실제 OpenAI-compatible embeddings
    endpoint를 lazily 연결한다.
    """

    import os

    env = environ if environ is not None else os.environ
    try:
        ModelPolicy().validate(resolve_embedding_spec(env), capability="embedding")
    except PolicyViolation as exc:
        raise EmbeddingProviderUnavailable(str(exc)) from exc
    config = resolve_embedding_config(env)
    model = str(config["model"])
    size = int(config["dim"])
    if embed_fn is None:
        api_key = env.get("LLM_BRAIN_EMBEDDING_API_KEY") or env.get("OPENAI_API_KEY") or ""
        embed_fn = _openai_embed_fn(model=model, base_url=str(config["base_url"]), api_key=api_key, environ=env)
    return OpenAICompatibleEmbeddingProvider(embed_fn=embed_fn, size=size, model=model)


def _openai_embed_fn(*, model: str, base_url: str, api_key: str, environ: Mapping[str, str] | None = None) -> EmbedFn:
    import os
    import math

    env = environ if environ is not None else os.environ

    if not model:
        raise EmbeddingProviderUnavailable(
            "embedding model not configured (set LLM_BRAIN_EMBEDDING_MODEL)"
        )
    normalized_model = model.strip().lower()
    provider_raw = str(
        env.get("LLM_BRAIN_EMBEDDING_PROVIDER") or env.get("EMBEDDING_PROVIDER") or ""
    ).strip().lower()
    profile_raw = str(env.get("LLM_BRAIN_EMBEDDING_PROFILE_ID") or "").strip()
    if (
        normalized_model in {"local-model2vec-256-v1", "minishlab/potion-multilingual-128m"}
        or provider_raw in {"model2vec", "local-model2vec", "local_model2vec"}
        or profile_raw == "lbrain-memory-model2vec-256-v1"
    ):
        if not str(base_url or "").strip():
            raise EmbeddingProviderUnavailable(
                "local embedding base_url not configured (set LLM_BRAIN_EMBEDDING_BASE_URL)"
            )
        from agent_knowledge.local_embedding.client import CanonicalLocalEmbeddingClient
        from agent_knowledge.local_embedding.profile import LOCAL_MODEL2VEC_MODEL_ALIAS, LOCAL_MODEL2VEC_PROFILE_ID
        from agent_knowledge.model_connectors.policy import validate_local_embedding_write_or_query_policy

        # Fail closed: a local profile can never target a paid endpoint or paid model.
        try:
            validate_local_embedding_write_or_query_policy(
                profile_id=LOCAL_MODEL2VEC_PROFILE_ID,
                model=LOCAL_MODEL2VEC_MODEL_ALIAS,
                base_url=str(base_url),
                dimension=256,
            )
        except PolicyViolation as exc:
            raise EmbeddingProviderUnavailable(str(exc)) from exc

        try:
            timeout_seconds = float(env.get("LLM_BRAIN_EMBEDDING_TIMEOUT_SECONDS", "30"))
        except (TypeError, ValueError):
            timeout_seconds = 30.0
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            timeout_seconds = 30.0
        local_client = CanonicalLocalEmbeddingClient(
            base_url=str(base_url),
            api_key=api_key or "local-model2vec",
            timeout_seconds=timeout_seconds,
            use_openai_sdk=True,
        )

        def _embed_local(text: str) -> list[float]:
            return list(local_client.embed_document(text).vector)

        _embed_local.close = local_client.close  # type: ignore[attr-defined]
        return _embed_local
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - exercised only on the live path
        raise EmbeddingProviderUnavailable(
            "openai client is not installed; install the embedding provider dependency"
        ) from exc
    try:
        timeout_seconds = float(env.get("LLM_BRAIN_EMBEDDING_TIMEOUT_SECONDS", "30"))
    except (TypeError, ValueError):
        timeout_seconds = 30.0
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        timeout_seconds = 30.0
    # Keep the retry budget explicit for the consumer's lease calculation.
    client = OpenAI(base_url=base_url or None, api_key=api_key or "", timeout=timeout_seconds, max_retries=2)

    def _embed(text: str) -> list[float]:
        response = client.embeddings.create(model=model, input=text)
        return list(response.data[0].embedding)

    close = getattr(client, "close", None)
    if callable(close):
        _embed.close = close  # type: ignore[attr-defined]

    return _embed


__all__ = [
    "DEFAULT_EMBEDDING_DIM",
    "OpenAICompatibleEmbeddingProvider",
    "build_openai_embedding_provider",
    "resolve_embedding_config",
]
