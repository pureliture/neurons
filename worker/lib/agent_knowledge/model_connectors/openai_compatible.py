from __future__ import annotations

from .policy import ModelConnectorConfigError, ModelPolicy
from .reranker import GraphitiCrossEncoderAdapter, build_reranker_client
from .specs import ModelConnectionConfig
from .structured_response import normalize_structured_response


def build_openai_compatible_graphiti_components(
    config: ModelConnectionConfig,
    *,
    llm_api_key: str = "",
    embedding_api_key: str = "",
    policy: ModelPolicy | None = None,
):
    """Build Graphiti OpenAI-compatible components lazily from shared specs."""

    validator = policy or ModelPolicy()
    validator.validate(config.llm, capability="structured_extraction")
    validator.validate(config.embedding, capability="embedding")
    validator.validate(config.reranker, capability="rerank")

    llm_provider = config.llm.provider
    embedding_provider = config.embedding.provider
    if llm_provider != "ollama":
        _require_configured(config.llm.model, "LLM_BRAIN_LLM_MODEL")
    if embedding_provider != "ollama":
        _require_configured(config.embedding.model, "LLM_BRAIN_EMBEDDING_MODEL")

    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

    llm_base_url = config.llm.base_url or ("http://localhost:11434/v1" if llm_provider == "ollama" else "")
    embedding_base_url = config.embedding.base_url or (
        "http://localhost:11434/v1" if embedding_provider == "ollama" else ""
    )
    api_key = llm_api_key or ("ollama" if llm_provider == "ollama" else "")
    embedding_api_key_value = embedding_api_key or ("ollama" if embedding_provider == "ollama" else api_key)
    llm_config = LLMConfig(
        api_key=api_key,
        model=config.llm.model or ("deepseek-r1:7b" if llm_provider == "ollama" else None),
        small_model=config.llm.small_model or config.llm.model or (
            "deepseek-r1:7b" if llm_provider == "ollama" else None
        ),
        base_url=llm_base_url or None,
    )

    class _NeuronStructuredClient(OpenAIGenericClient):
        async def _generate_response(self, *args, **kwargs):
            response_model = kwargs.get("response_model")
            if response_model is None and len(args) >= 2:
                response_model = args[1]
            data = await super()._generate_response(*args, **kwargs)
            return normalize_structured_response(data, response_model)

    llm_client = _NeuronStructuredClient(config=llm_config)
    embedder = OpenAIEmbedder(
        config=OpenAIEmbedderConfig(
            api_key=embedding_api_key_value,
            embedding_model=config.embedding.model or (
                "nomic-embed-text" if embedding_provider == "ollama" else None
            ),
            embedding_dim=config.embedding.dim,
            base_url=embedding_base_url or None,
        )
    )
    reranker = build_reranker_client(config, api_key=api_key, openai_client=llm_client.client, policy=validator)
    return llm_client, embedder, GraphitiCrossEncoderAdapter(reranker)


def _require_configured(value: str, env_name: str) -> None:
    if not str(value or "").strip():
        raise ModelConnectorConfigError(f"model connector missing required config: {env_name}")
