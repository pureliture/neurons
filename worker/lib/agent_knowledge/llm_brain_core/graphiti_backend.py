from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_knowledge.model_connectors import (
    EmbeddingSpec,
    ModelConnectionConfig,
    ModelEndpointSpec,
    RerankerSpec,
)
from agent_knowledge.model_connectors.graphiti_components import (
    GraphitiComponentBundle,
    build_graphiti_component_bundle,
)


@dataclass(frozen=True)
class Neo4jSpec:
    uri: str
    user: str
    password: str = field(default="", repr=False)


class GraphitiBackendBuilder:
    def build(
        self,
        graph_store: Neo4jSpec,
        components: GraphitiComponentBundle | None,
        *,
        store_raw_episode_content: bool = True,
    ) -> Any:
        from graphiti_core import Graphiti

        if components is None:
            episode_only_components = _episode_only_component_bundle()
            return Graphiti(
                graph_store.uri,
                graph_store.user,
                graph_store.password,
                llm_client=episode_only_components.llm_client,
                embedder=episode_only_components.embedder,
                cross_encoder=episode_only_components.cross_encoder,
                store_raw_episode_content=store_raw_episode_content,
            )
        return Graphiti(
            graph_store.uri,
            graph_store.user,
            graph_store.password,
            llm_client=components.llm_client,
            embedder=components.embedder,
            cross_encoder=components.cross_encoder,
            store_raw_episode_content=store_raw_episode_content,
        )


def build_graphiti_from_config(config: Any) -> Any:
    components = None
    if bool(getattr(config, "extract_entities", False)):
        model_config = model_connection_from_graphiti_config(config)
        components = build_graphiti_component_bundle(
            model_config,
            llm_api_key=str(getattr(config, "llm_api_key", "") or ""),
            embedding_api_key=str(getattr(config, "embedding_api_key", "") or ""),
        )
    return GraphitiBackendBuilder().build(
        Neo4jSpec(
            uri=str(getattr(config, "uri", "")),
            user=str(getattr(config, "user", "")),
            password=str(getattr(config, "password", "") or ""),
        ),
        components,
        store_raw_episode_content=bool(getattr(config, "store_raw_episode_content", True)),
    )


def model_connection_from_graphiti_config(config: Any) -> ModelConnectionConfig:
    provider = str(getattr(config, "llm_provider", "openai") or "openai").lower()
    llm_model = str(getattr(config, "llm_model", "") or "")
    llm_base_url = str(getattr(config, "llm_base_url", "") or "")
    return ModelConnectionConfig(
        llm=ModelEndpointSpec(
            provider=provider,
            model=llm_model,
            small_model=str(getattr(config, "small_model", "") or ""),
            base_url=llm_base_url,
        ),
        embedding=EmbeddingSpec(
            provider=str(getattr(config, "embedding_provider", "") or "openai").lower(),
            model=str(getattr(config, "embedding_model", "") or ""),
            base_url=str(getattr(config, "embedding_base_url", "") or ""),
            dim=int(getattr(config, "embedding_dim", 1024) or 1024),
        ),
        reranker=RerankerSpec(
            provider=provider,
            model=llm_model,
            base_url=llm_base_url,
        ),
        fallback_llm_model=str(getattr(config, "fallback_llm_model", "") or ""),
        fallback_small_model=str(getattr(config, "fallback_small_model", "") or ""),
        primary_attempts=max(1, int(getattr(config, "primary_attempts", 1) or 1)),
        fallback_attempts=max(1, int(getattr(config, "fallback_attempts", 1) or 1)),
    )


def _episode_only_component_bundle() -> GraphitiComponentBundle:
    from graphiti_core.cross_encoder.client import CrossEncoderClient
    from graphiti_core.embedder.client import EmbedderClient
    from graphiti_core.llm_client.client import LLMClient
    from graphiti_core.llm_client.config import LLMConfig

    class _EpisodeOnlyLLMClient(LLMClient):
        def __init__(self) -> None:
            super().__init__(
                LLMConfig(
                    api_key="episode-only",
                    model="episode-only",
                    small_model="episode-only",
                )
            )

        async def _generate_response(self, *args, **kwargs):
            _ = args
            _ = kwargs
            raise RuntimeError("graphiti episode-only backend has no llm component")

    class _EpisodeOnlyEmbedder(EmbedderClient):
        async def create(self, *args, **kwargs):
            _ = args
            _ = kwargs
            raise RuntimeError("graphiti episode-only backend has no embedder component")

        async def create_batch(self, *args, **kwargs):
            return await self.create(*args, **kwargs)

    class _EpisodeOnlyCrossEncoder(CrossEncoderClient):
        async def rank(self, *args, **kwargs):
            _ = args
            _ = kwargs
            raise RuntimeError("graphiti episode-only backend has no cross_encoder component")

    return GraphitiComponentBundle(
        llm_client=_EpisodeOnlyLLMClient(),
        embedder=_EpisodeOnlyEmbedder(),
        cross_encoder=_EpisodeOnlyCrossEncoder(),
    )
