from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import urllib.parse


class PolicyViolation(ValueError):
    """Raised when a model provider is not allowed for a capability."""


class PaidEmbeddingFallbackBlockedError(PolicyViolation):
    """Raised when local Model2Vec mode detects a missing local URL or paid cloud fallback."""


class ModelConnectorConfigError(ValueError):
    """Raised when a live model connector is missing required non-secret config."""


_FORBIDDEN_PAID_EMBEDDING_HOSTS = frozenset(
    {
        "generativelanguage.googleapis.com",
        "aiplatform.googleapis.com",
        "api.openai.com",
        "openrouter.ai",
        "api.cohere.ai",
        "api.voyageai.com",
        "api.anthropic.com",
    }
)
_FORBIDDEN_PAID_EMBEDDING_MODELS = frozenset(
    {
        "gemini-embedding-2",
        "gemini-embedding-001",
        "text-embedding-004",
        "text-embedding-3-small",
        "text-embedding-3-large",
        "text-embedding-ada-002",
    }
)
_ALLOWED_LOCAL_EMBEDDING_MODELS = frozenset(
    {
        "local-model2vec-256-v1",
        "minishlab/potion-multilingual-128m",
        "lbrain-memory-model2vec-256-v1",
    }
)


def validate_local_embedding_write_or_query_policy(
    *,
    profile_id: str,
    model: str,
    base_url: str | None,
    dimension: int = 256,
) -> None:
    """Enforce zero paid fallback when operating under the local Model2Vec profile."""
    normalized_profile = str(profile_id or "").strip()
    normalized_model = str(model or "").strip().lower()
    if (
        normalized_profile != "lbrain-memory-model2vec-256-v1"
        and normalized_model not in _ALLOWED_LOCAL_EMBEDDING_MODELS
    ):
        return

    if int(dimension) != 256:
        raise PaidEmbeddingFallbackBlockedError(
            f"local Model2Vec profile requires 256 dimensions, got {dimension}"
        )

    if (
        "gemini" in normalized_model
        or normalized_model in _FORBIDDEN_PAID_EMBEDDING_MODELS
        or normalized_model not in _ALLOWED_LOCAL_EMBEDDING_MODELS
    ):
        raise PaidEmbeddingFallbackBlockedError(
            "paid or non-local embedding model is forbidden under local Model2Vec profile"
        )

    raw_url = str(base_url or "").strip()
    if not raw_url:
        raise PaidEmbeddingFallbackBlockedError(
            "local embedding base_url is required under local Model2Vec profile (paid fallback blocked)"
        )
    parsed = urllib.parse.urlparse(raw_url)
    host = (parsed.hostname or "").strip().lower()
    if not host or parsed.scheme.lower() not in {"http", "https"}:
        raise PaidEmbeddingFallbackBlockedError(
            "local embedding base_url must be a valid http(s) URL"
        )
    if host in _FORBIDDEN_PAID_EMBEDDING_HOSTS or host.endswith(".googleapis.com") or host.endswith(".openai.com"):
        raise PaidEmbeddingFallbackBlockedError(
            "paid cloud embedding endpoint is forbidden under local Model2Vec profile"
        )


@dataclass(frozen=True)
class ModelPolicy:
    """Capability-level model policy with provider-specific guardrails."""

    denied_chat_providers: frozenset[str] = frozenset({"gemini", "google-gemini"})
    chat_providers: frozenset[str] = frozenset(
        {"openai", "openai-compatible", "openai_compatible", "ollama", "gemma4-maas"}
    )
    embedding_providers: frozenset[str] = frozenset(
        {
            "openai",
            "openai-compatible",
            "openai_compatible",
            "ollama",
            "gemma4-maas",
            "model2vec",
            "local-model2vec",
            "local_model2vec",
        }
    )

    def validate(self, spec: Any, *, capability: str) -> None:
        provider = _provider(spec)
        capability_name = str(capability or "").strip().lower().replace("-", "_")
        if provider in self.denied_chat_providers and capability_name in {
            "chat",
            "structured_extraction",
            "rerank",
        }:
            raise PolicyViolation(f"provider not allowed for {capability_name}")
        if capability_name in {"chat", "structured_extraction", "rerank"}:
            if provider not in self.chat_providers:
                raise PolicyViolation(f"provider not allowed for {capability_name}")
        elif capability_name == "embedding" and provider:
            if provider not in self.embedding_providers:
                raise PolicyViolation("provider not allowed for embedding")


def _provider(spec: Any) -> str:
    provider = getattr(spec, "provider", None)
    if provider is None and isinstance(spec, dict):
        provider = spec.get("provider")
    return str(provider or "").strip().lower()
