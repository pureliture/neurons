"""OpenCode session header contract for the shared model connectors.

The OpenCode zen/go relay rejects requests without ``x-opencode-session``
(HTTP 400 ``MissingSessionID``). Every OpenAI-compatible client path in the
worker must attach that header for OpenCode endpoints only -- the internal
gateway, Vertex and Ollama must never receive it.
"""

from __future__ import annotations

import pytest

from agent_knowledge.model_connectors import (
    GRAPH_WORKER_SESSION_ID,
    OPENCODE_SESSION_HEADER,
    EmbeddingSpec,
    ModelConnectionConfig,
    ModelEndpointSpec,
    OpenAICompatibleRerankerClient,
    RerankerSpec,
    is_opencode_base_url,
    opencode_session_headers,
    opencode_session_id,
)
from agent_knowledge.model_connectors.openai_compatible import (
    build_openai_compatible_graphiti_components,
)

OPENCODE_BASE_URLS = [
    "https://opencode.ai/zen/go/v1",
    "https://opencode.ai/zen/v1",
    "http://opencode.ai/zen/go/v1",
]

NON_OPENCODE_BASE_URLS = [
    "http://model-gateway-proxy:8930/v1",
    "https://generativelanguage.googleapis.com/v1beta/openai/",
    "http://localhost:11434/v1",
    "https://api.openai.com/v1",
    # Hostname look-alikes must not be treated as OpenCode endpoints.
    "https://opencode.ai.evil.test/v1",
    "https://proxy.test/opencode.ai/v1",
]


@pytest.mark.parametrize("base_url", OPENCODE_BASE_URLS)
def test_opencode_base_urls_are_detected(base_url):
    assert is_opencode_base_url(base_url) is True


@pytest.mark.parametrize("base_url", NON_OPENCODE_BASE_URLS)
def test_non_opencode_base_urls_are_not_detected(base_url):
    assert is_opencode_base_url(base_url) is False


def test_empty_base_url_is_not_an_opencode_target():
    assert is_opencode_base_url("") is False
    assert opencode_session_headers("") == {}


def test_opencode_session_headers_only_appear_for_opencode_endpoints():
    assert opencode_session_headers("https://opencode.ai/zen/go/v1") == {
        OPENCODE_SESSION_HEADER: opencode_session_id()
    }
    assert opencode_session_headers("http://model-gateway-proxy:8930/v1") == {}


def test_opencode_session_id_is_stable_within_the_process():
    assert opencode_session_id() == opencode_session_id()
    assert opencode_session_headers("https://opencode.ai/zen/go/v1") == (
        opencode_session_headers("https://opencode.ai/zen/go/v1")
    )


def test_explicit_session_id_overrides_the_process_default():
    headers = opencode_session_headers("https://opencode.ai/zen/go/v1", session_id="lane-a")
    assert headers == {OPENCODE_SESSION_HEADER: "lane-a"}


def test_graph_worker_session_id_is_a_stable_lane_label():
    assert GRAPH_WORKER_SESSION_ID == "neurons-graph-worker"


def _graphiti_config(base_url: str) -> ModelConnectionConfig:
    return ModelConnectionConfig(
        llm=ModelEndpointSpec(provider="openai-compatible", model="llm", base_url=base_url),
        embedding=EmbeddingSpec(provider="openai-compatible", model="embed", base_url=base_url),
        reranker=RerankerSpec(provider="openai-compatible", model="llm", base_url=base_url),
    )


def test_graphiti_llm_client_sends_session_header_for_opencode_endpoint():
    config = _graphiti_config("https://opencode.ai/zen/go/v1")

    llm_client, _embedder, _cross_encoder = build_openai_compatible_graphiti_components(
        config,
        llm_api_key="test-key",
        embedding_api_key="test-key",
    )

    assert llm_client.client.default_headers.get(OPENCODE_SESSION_HEADER) == GRAPH_WORKER_SESSION_ID


def test_graphiti_llm_client_omits_session_header_for_other_providers():
    config = _graphiti_config("http://model-gateway-proxy:8930/v1")

    llm_client, _embedder, _cross_encoder = build_openai_compatible_graphiti_components(
        config,
        llm_api_key="test-key",
        embedding_api_key="test-key",
    )

    assert OPENCODE_SESSION_HEADER not in llm_client.client.default_headers


def test_reranker_clients_send_session_header_for_opencode_endpoint():
    client = OpenAICompatibleRerankerClient(
        RerankerSpec(provider="openai-compatible", model="rerank", base_url="https://opencode.ai/zen/go/v1"),
        api_key="test-key",
    )

    assert client._async_client.default_headers.get(OPENCODE_SESSION_HEADER) == opencode_session_id()
    assert client._sync_client.default_headers.get(OPENCODE_SESSION_HEADER) == opencode_session_id()


def test_reranker_clients_omit_session_header_for_other_providers():
    client = OpenAICompatibleRerankerClient(
        RerankerSpec(provider="openai-compatible", model="rerank", base_url="http://model-gateway-proxy:8930/v1"),
        api_key="test-key",
    )

    assert OPENCODE_SESSION_HEADER not in client._async_client.default_headers
    assert OPENCODE_SESSION_HEADER not in client._sync_client.default_headers
