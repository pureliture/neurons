from __future__ import annotations

import pytest

from agent_knowledge.llm_brain_core.graphiti_adapter import GraphitiNeo4jConfig
from agent_knowledge.llm_brain_core.graphiti_backend import (
    build_graphiti_from_config,
    model_connection_from_graphiti_config,
)
from agent_knowledge.model_connectors import ModelConnectorConfigError, PolicyViolation


def test_model_connection_from_graphiti_config_preserves_legacy_facade_values():
    config = GraphitiNeo4jConfig(
        llm_provider="openai-compatible",
        llm_model="llm-main",
        small_model="llm-small",
        llm_base_url="http://llm/v1",
        llm_api_key="secret-llm",
        embedding_model="embed-main",
        embedding_base_url="http://embed/v1",
        embedding_api_key="secret-embed",
        embedding_dim=768,
        fallback_llm_model="fallback-llm",
        fallback_small_model="fallback-small",
        primary_attempts=3,
        fallback_attempts=2,
    )

    shared = model_connection_from_graphiti_config(config)

    assert shared.llm.provider == "openai-compatible"
    assert shared.llm.model == "llm-main"
    assert shared.llm.small_model == "llm-small"
    assert shared.llm.base_url == "http://llm/v1"
    assert shared.embedding.model == "embed-main"
    assert shared.embedding.base_url == "http://embed/v1"
    assert shared.embedding.dim == 768
    assert shared.reranker.model == "llm-main"
    assert shared.fallback_llm_model == "fallback-llm"
    assert shared.fallback_small_model == "fallback-small"
    assert shared.primary_attempts == 3
    assert shared.fallback_attempts == 2
    assert "secret" not in repr(shared)


def test_build_graphiti_from_config_fails_closed_for_unknown_provider():
    config = GraphitiNeo4jConfig(
        llm_provider="typo-provider",
        llm_model="llm-main",
        llm_base_url="http://llm/v1",
        embedding_model="embed-main",
        embedding_base_url="http://embed/v1",
    )

    with pytest.raises((ModelConnectorConfigError, PolicyViolation)):
        build_graphiti_from_config(config)
