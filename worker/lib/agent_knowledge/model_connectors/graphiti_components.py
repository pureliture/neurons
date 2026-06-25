from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .openai_compatible import build_openai_compatible_graphiti_components
from .policy import ModelConnectorConfigError
from .specs import ModelConnectionConfig

_OPENAI_COMPATIBLE_GRAPHITI_PROVIDERS = {
    "gemma4-maas",
    "ollama",
    "openai",
    "openai-compatible",
    "openai_compatible",
}


@dataclass(frozen=True)
class GraphitiComponentBundle:
    llm_client: Any
    embedder: Any
    cross_encoder: Any | None = None


def build_graphiti_component_bundle(
    config: ModelConnectionConfig,
    *,
    llm_api_key: str = "",
    embedding_api_key: str = "",
) -> GraphitiComponentBundle | None:
    provider = config.llm.provider
    if provider not in _OPENAI_COMPATIBLE_GRAPHITI_PROVIDERS:
        raise ModelConnectorConfigError(f"model connector unsupported graphiti provider: {provider}")
    llm_client, embedder, cross_encoder = build_openai_compatible_graphiti_components(
        config,
        llm_api_key=llm_api_key,
        embedding_api_key=embedding_api_key,
    )
    return GraphitiComponentBundle(
        llm_client=llm_client,
        embedder=embedder,
        cross_encoder=cross_encoder,
    )
