"""OpenAI-compatible EmbeddingProvider for the Qdrant searchable mirror.

The mirror needs worker-produced vectors (RAGFlow embedded server-side; Qdrant
does not). Rather than choose a new model, this reuses the *same* OpenAI-compatible
embedding endpoint the Graphiti adapter already uses -- the ``LLM_BRAIN_EMBEDDING_*``
(falling back to ``OPENAI_*``) env, default dim 1024. No new secret is introduced.

Testability: the provider wraps an injected ``embed_fn``, so unit tests run with a
fake embedder and need no network and no optional ``openai`` dependency. The real
endpoint is wired lazily in :func:`build_openai_embedding_provider`, exercised only
on the live path (mirrors the optional-dependency pattern in
``qdrant_docling_mirror``).
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
    """``EmbeddingProvider`` over an injected OpenAI-compatible embed function.

    Satisfies the ``EmbeddingProvider`` protocol (``size`` + ``embed``) consumed by
    :class:`QdrantDoclingMirrorAdapter`. The returned vector length is validated
    against the declared ``size`` so a misconfigured endpoint fails closed.
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
    """Resolve the embedding connection config from env.

    Reuses the same precedence as ``GraphitiNeo4jConfig.from_env`` so the mirror and
    the graph adapter speak to one endpoint. The api_key is deliberately NOT
    returned here. The returned base_url can still identify a host; do not log this
    object on production paths without redaction.
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
    """Build the mirror embedding provider from env.

    ``embed_fn`` is injectable for tests/local. When omitted, the real
    OpenAI-compatible embeddings endpoint is wired lazily.
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
    client = OpenAI(base_url=base_url or None, api_key=api_key or "")

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
