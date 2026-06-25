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
        embedding_provider="ollama",
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
    assert shared.embedding.provider == "ollama"
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
        extract_entities=True,
    )

    with pytest.raises((ModelConnectorConfigError, PolicyViolation)):
        build_graphiti_from_config(config)


def test_episode_only_graphiti_build_does_not_require_model_components(monkeypatch):
    built: list[tuple[object, object]] = []

    def _fail_build_components(*_args, **_kwargs):
        raise AssertionError("episode-only build must not resolve model components")

    def _fake_backend_build(self, graph_store, components, **_kwargs):
        _ = self
        built.append((graph_store, components))
        return object()

    monkeypatch.setattr(
        "agent_knowledge.llm_brain_core.graphiti_backend.build_graphiti_component_bundle",
        _fail_build_components,
    )
    monkeypatch.setattr(
        "agent_knowledge.llm_brain_core.graphiti_backend.GraphitiBackendBuilder.build",
        _fake_backend_build,
    )

    build_graphiti_from_config(GraphitiNeo4jConfig(extract_entities=False))

    assert built
    assert built[0][1] is None


def test_episode_only_graphiti_build_does_not_require_openai_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)

    graphiti = build_graphiti_from_config(GraphitiNeo4jConfig(extract_entities=False))

    assert graphiti.driver is not None
