from __future__ import annotations

from typing import Any

from .policy import ModelConnectorConfigError, ModelPolicy
from .reranker import GraphitiCrossEncoderAdapter, build_reranker_client
from .specs import ModelConnectionConfig

_STRUCTURED_KEY_ALIASES = {
    "entity_name": "name",
    "entity": "name",
    "entity_value": "name",
    "entity_text": "name",
}


def normalize_structured_response(value: Any, response_model: Any = None) -> Any:
    normalized = _normalize_structured_keys(value)
    if not isinstance(normalized, list) or response_model is None:
        return normalized
    fields = getattr(response_model, "model_fields", {}) or {}
    list_field_names = [
        name for name, field in fields.items()
        if str(getattr(field, "annotation", "")).startswith("list[")
    ]
    if len(list_field_names) == 1:
        return {list_field_names[0]: normalized}
    return normalized


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

    provider = config.llm.provider
    if provider != "ollama":
        _require_configured(config.llm.model, "LLM_BRAIN_LLM_MODEL")
        _require_configured(config.embedding.model, "LLM_BRAIN_EMBEDDING_MODEL")

    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

    base_url = config.llm.base_url or ("http://localhost:11434/v1" if provider == "ollama" else "")
    api_key = llm_api_key or ("ollama" if provider == "ollama" else "")
    llm_config = LLMConfig(
        api_key=api_key,
        model=config.llm.model or ("deepseek-r1:7b" if provider == "ollama" else None),
        small_model=config.llm.small_model or config.llm.model or ("deepseek-r1:7b" if provider == "ollama" else None),
        base_url=base_url or None,
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
            api_key=embedding_api_key or api_key,
            embedding_model=config.embedding.model or ("nomic-embed-text" if provider == "ollama" else "text-embedding-3-small"),
            embedding_dim=config.embedding.dim,
            base_url=config.embedding.base_url or base_url or None,
        )
    )
    reranker = build_reranker_client(config, api_key=api_key, openai_client=llm_client.client, policy=validator)
    return llm_client, embedder, GraphitiCrossEncoderAdapter(reranker)


def _require_configured(value: str, env_name: str) -> None:
    if not str(value or "").strip():
        raise ModelConnectorConfigError(f"model connector missing required config: {env_name}")


def _normalize_structured_keys(value: Any) -> Any:
    if isinstance(value, list):
        return [_normalize_structured_keys(item) for item in value]
    if isinstance(value, dict):
        result = {key: _normalize_structured_keys(val) for key, val in value.items()}
        for alias, canonical in _STRUCTURED_KEY_ALIASES.items():
            if alias in result and canonical not in result:
                result[canonical] = result.pop(alias)
        if "name" in result and "entity_type_id" not in result:
            result["entity_type_id"] = 0
        if isinstance(result.get("episode_indices"), list):
            indices: list[int] = []
            for item in result["episode_indices"]:
                try:
                    indices.append(int(item))
                except (TypeError, ValueError):
                    indices.append(0)
            result["episode_indices"] = indices
        return result
    return value
