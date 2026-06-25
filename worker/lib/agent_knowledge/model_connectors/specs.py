from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelEndpointSpec:
    """Non-secret OpenAI-compatible chat/model endpoint settings."""

    provider: str = "openai"
    model: str = ""
    small_model: str = ""
    base_url: str = field(default="", repr=False)


@dataclass(frozen=True)
class EmbeddingSpec:
    """Non-secret embedding endpoint settings."""

    provider: str = "openai"
    model: str = ""
    base_url: str = field(default="", repr=False)
    dim: int = 1024


@dataclass(frozen=True)
class RerankerSpec:
    """Non-secret reranker endpoint settings."""

    provider: str = "openai"
    model: str = ""
    base_url: str = field(default="", repr=False)


@dataclass(frozen=True)
class ModelConnectionConfig:
    """Non-secret model connector config shared by graph and mirror integrations."""

    llm: ModelEndpointSpec
    embedding: EmbeddingSpec
    reranker: RerankerSpec
    fallback_llm_model: str = ""
    fallback_small_model: str = ""
    primary_attempts: int = 1
    fallback_attempts: int = 1
