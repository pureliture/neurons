"""Qdrant searchable mirror용 OpenAI-compatible ``EmbeddingProvider``.

Mirror는 worker가 만든 vector가 필요하고, RAGFlow는 server-side embedding을
쓴다. 새 model을 고르지 않고 Graphiti adapter와 같은
``LLM_BRAIN_EMBEDDING_*`` env, ``OPENAI_*`` fallback, 기본 dim 1024를
재사용한다. 새 secret은 만들지 않는다.

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

from .qdrant_docling_mirror import SearchableMirrorUnavailable

EmbedFn = Callable[[str], list[float]]


class OpenAICompatibleEmbeddingProvider:
    """주입된 OpenAI-compatible embed function 기반 ``EmbeddingProvider``.

    :class:`QdrantDoclingMirrorAdapter`가 소비하는 ``EmbeddingProvider`` protocol
    (``size`` + ``embed``)을 만족한다. 반환 vector 길이는 선언된 ``size``와
    맞춰 검증해서 잘못 설정된 endpoint는 fail closed 처리한다.
    """

    def __init__(self, *, embed_fn: EmbedFn, size: int, model: str = "") -> None:
        if int(size) <= 0:
            raise ValueError("embedding size must be positive")
        self._embed_fn = embed_fn
        self._size = int(size)
        self._model = str(model or "")

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


def resolve_embedding_config(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """Env에서 embedding 연결 config를 해석한다.

    ``GraphitiNeo4jConfig.from_env``와 같은 우선순위를 써서 mirror와 graph adapter가
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
    """Env에서 mirror embedding provider를 만든다.

    ``embed_fn``은 test/local 주입용이다. 생략하면 실제 OpenAI-compatible embeddings
    endpoint를 lazily 연결한다.
    """

    import os

    env = environ if environ is not None else os.environ
    try:
        ModelPolicy().validate(resolve_embedding_spec(env), capability="embedding")
    except PolicyViolation as exc:
        raise SearchableMirrorUnavailable(str(exc)) from exc
    config = resolve_embedding_config(env)
    model = str(config["model"])
    size = int(config["dim"])
    if embed_fn is None:
        api_key = env.get("LLM_BRAIN_EMBEDDING_API_KEY") or env.get("OPENAI_API_KEY") or ""
        embed_fn = _openai_embed_fn(model=model, base_url=str(config["base_url"]), api_key=api_key)
    return OpenAICompatibleEmbeddingProvider(embed_fn=embed_fn, size=size, model=model)


def _openai_embed_fn(*, model: str, base_url: str, api_key: str) -> EmbedFn:
    import os

    if not model:
        raise SearchableMirrorUnavailable(
            "embedding model not configured (set LLM_BRAIN_EMBEDDING_MODEL)"
        )
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - exercised only on the live path
        raise SearchableMirrorUnavailable(
            "openai client is not installed; install the searchable mirror dependencies"
        ) from exc
    try:
        timeout_seconds = float(os.environ.get("LLM_BRAIN_EMBEDDING_TIMEOUT_SECONDS", "30"))
    except ValueError:
        timeout_seconds = 30.0
    if timeout_seconds <= 0:
        timeout_seconds = 30.0
    client = OpenAI(base_url=base_url or None, api_key=api_key or "", timeout=timeout_seconds)

    def _embed(text: str) -> list[float]:
        response = client.embeddings.create(model=model, input=text)
        return list(response.data[0].embedding)

    return _embed


__all__ = [
    "DEFAULT_EMBEDDING_DIM",
    "OpenAICompatibleEmbeddingProvider",
    "build_openai_embedding_provider",
    "resolve_embedding_config",
]
