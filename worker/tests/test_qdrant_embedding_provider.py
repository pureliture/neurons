"""M1: OpenAI-compatible EmbeddingProvider reuse for the Qdrant mirror.

No network and no optional dependency: the provider wraps an injected embed_fn.
Proves config reuse (LLM_BRAIN_EMBEDDING_* with OPENAI_* fallback, dim 1024),
size fail-closed, determinism, and that it satisfies the adapter's
EmbeddingProvider protocol end to end.
"""

from __future__ import annotations

import sys
import types

import pytest

from agent_knowledge.rag_ingress.retired_index_bridge import IndexStatus
from agent_knowledge.rag_ingress.qdrant_docling_mirror import (
    FOUNDATION_DIRECT_WRITE_CONTRACT,
    PassthroughMarkdownNormalizer,
    QdrantDoclingMirrorAdapter,
    SearchableMirrorUnavailable,
)
from agent_knowledge.rag_ingress.qdrant_docling_testing import InMemoryQdrantClient
from agent_knowledge.rag_ingress.qdrant_embedding import (
    DEFAULT_EMBEDDING_DIM,
    OpenAICompatibleEmbeddingProvider,
    build_openai_embedding_provider,
    resolve_embedding_config,
)
from agent_knowledge.rag_ingress.rag_ready_document import build_rag_ready_document


def _fake_embed(size: int):
    def _embed(text: str) -> list[float]:
        # deterministic, size-correct vector derived from the text length
        seed = (len(text) % 7) + 1
        return [float((i % seed) + 1) for i in range(size)]

    return _embed


def test_provider_embeds_and_reports_size():
    provider = OpenAICompatibleEmbeddingProvider(embed_fn=_fake_embed(8), size=8, model="m")
    vector = provider.embed("hello world")
    assert provider.size == 8
    assert provider.model == "m"
    assert len(vector) == 8
    # determinism
    assert provider.embed("hello world") == vector


def test_provider_rejects_wrong_vector_size():
    provider = OpenAICompatibleEmbeddingProvider(embed_fn=lambda _t: [0.0, 1.0], size=8, model="m")
    with pytest.raises(ValueError):
        provider.embed("x")


def test_provider_rejects_nonpositive_size():
    with pytest.raises(ValueError):
        OpenAICompatibleEmbeddingProvider(embed_fn=lambda _t: [], size=0)


def test_resolve_embedding_config_primary_wins_and_falls_back_and_omits_secret():
    # both primary (LLM_BRAIN_*) and fallback (OPENAI_*/EMBEDDING_*) present:
    # primary must win for model and base_url.
    cfg = resolve_embedding_config(
        {
            "LLM_BRAIN_EMBEDDING_MODEL": "bge-m3",
            "EMBEDDING_MODEL": "should-lose",
            "LLM_BRAIN_EMBEDDING_BASE_URL": "http://primary/v1",
            "OPENAI_BASE_URL": "http://should-lose/v1",
            "LLM_BRAIN_EMBEDDING_DIM": "1024",
        }
    )
    assert cfg["provider"] == "openai"
    assert cfg["model"] == "bge-m3"
    assert cfg["base_url"] == "http://primary/v1"
    assert cfg["dim"] == 1024
    # secret is never returned in the config dict
    assert "api_key" not in cfg

    # fallback branch when primary absent
    fb = resolve_embedding_config({"EMBEDDING_MODEL": "fallback-model", "OPENAI_BASE_URL": "http://fb/v1"})
    assert fb["model"] == "fallback-model"
    assert fb["base_url"] == "http://fb/v1"


def test_resolve_embedding_config_defaults_dim_when_missing_or_invalid():
    assert resolve_embedding_config({})["dim"] == DEFAULT_EMBEDDING_DIM
    assert resolve_embedding_config({"LLM_BRAIN_EMBEDDING_DIM": "not-int"})["dim"] == DEFAULT_EMBEDDING_DIM
    assert resolve_embedding_config({"LLM_BRAIN_EMBEDDING_DIM": "-5"})["dim"] == DEFAULT_EMBEDDING_DIM


def test_build_provider_with_injected_embed_fn_uses_env_dim_no_network():
    provider = build_openai_embedding_provider(
        environ={"LLM_BRAIN_EMBEDDING_MODEL": "bge-m3", "LLM_BRAIN_EMBEDDING_DIM": "16"},
        embed_fn=_fake_embed(16),
    )
    assert provider.size == 16
    assert provider.model == "bge-m3"
    assert len(provider.embed("abc")) == 16


def test_live_openai_client_uses_bounded_timeout(monkeypatch):
    captured = {}

    class _EmbeddingData:
        embedding = [1.0, 2.0, 3.0]

    class _EmbeddingResponse:
        data = [_EmbeddingData()]

    class _Embeddings:
        def create(self, *, model, input):
            captured["create_model"] = model
            captured["input"] = input
            return _EmbeddingResponse()

    class _OpenAI:
        def __init__(self, *, base_url, api_key, timeout):
            captured["base_url"] = base_url
            captured["api_key"] = api_key
            captured["timeout"] = timeout
            self.embeddings = _Embeddings()

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=_OpenAI))
    monkeypatch.setenv("LLM_BRAIN_EMBEDDING_TIMEOUT_SECONDS", "7.5")

    provider = build_openai_embedding_provider(
        environ={
            "LLM_BRAIN_EMBEDDING_MODEL": "bge-m3",
            "LLM_BRAIN_EMBEDDING_DIM": "3",
            "LLM_BRAIN_EMBEDDING_BASE_URL": "http://embedding/v1",
            "LLM_BRAIN_EMBEDDING_API_KEY": "test-key",
        },
    )

    assert provider.embed("bounded") == [1.0, 2.0, 3.0]
    assert captured["timeout"] == 7.5
    assert captured["base_url"] == "http://embedding/v1"
    assert captured["create_model"] == "bge-m3"


def test_build_provider_rejects_unknown_embedding_provider():
    with pytest.raises(SearchableMirrorUnavailable, match="provider not allowed"):
        build_openai_embedding_provider(
            environ={
                "LLM_BRAIN_EMBEDDING_PROVIDER": "typo-provider",
                "LLM_BRAIN_EMBEDDING_MODEL": "bge-m3",
            },
            embed_fn=_fake_embed(1024),
        )


def test_provider_satisfies_adapter_embedding_protocol_end_to_end():
    client = InMemoryQdrantClient()
    provider = build_openai_embedding_provider(
        environ={"LLM_BRAIN_EMBEDDING_MODEL": "bge-m3", "LLM_BRAIN_EMBEDDING_DIM": "16"},
        embed_fn=_fake_embed(16),
    )
    adapter = QdrantDoclingMirrorAdapter(
        client=client,
        direct_write_contract=FOUNDATION_DIRECT_WRITE_CONTRACT,
        normalizer=PassthroughMarkdownNormalizer(),
        embedding_provider=provider,
    )
    doc = build_rag_ready_document(
        target_profile="derived-memory-items",
        document_kind="approved_memory_card",
        source_namespace="workspace-neurons",
        source_alias="cards/example.md",
        privacy_class="private",
        body="Decision: the mirror is searchable only; ledger is canonical.",
        filename="example.md",
        metadata={"project": "neurons", "provider": "claude"},
    )
    result = adapter.submit_document(doc)
    assert result.status == IndexStatus.INDEXED
    assert client.point_count("neurons_searchable_mirror_poc") == 1


class _WrongSizeProvider:
    @property
    def size(self) -> int:
        return 16

    def embed(self, text: str) -> list[float]:
        return [0.0] * 8  # disagrees with declared size


def test_submit_document_size_guard_rejects_mismatched_vector_no_point_written():
    client = InMemoryQdrantClient()
    adapter = QdrantDoclingMirrorAdapter(
        client=client,
        direct_write_contract=FOUNDATION_DIRECT_WRITE_CONTRACT,
        normalizer=PassthroughMarkdownNormalizer(),
        embedding_provider=_WrongSizeProvider(),
    )
    doc = build_rag_ready_document(
        target_profile="derived-memory-items",
        document_kind="approved_memory_card",
        source_namespace="workspace-neurons",
        source_alias="cards/x.md",
        privacy_class="private",
        body="body",
        filename="x.md",
        metadata={"project": "neurons"},
    )
    with pytest.raises(ValueError):
        adapter.submit_document(doc)
    assert client.point_count("neurons_searchable_mirror_poc") == 0
