"""M1: OpenAI-compatible EmbeddingProvider reuse for the Qdrant mirror.

No network and no optional dependency: the provider wraps an injected embed_fn.
Proves config reuse (LLM_BRAIN_EMBEDDING_* with OPENAI_* fallback, dim 1024),
size fail-closed, determinism, and that it satisfies the adapter's
EmbeddingProvider protocol end to end.
"""

from __future__ import annotations

import pytest

from agent_knowledge.rag_ingress.index_backend import IndexStatus
from agent_knowledge.rag_ingress.qdrant_docling_mirror import (
    PassthroughMarkdownNormalizer,
    QdrantDoclingMirrorAdapter,
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


def test_resolve_embedding_config_prefers_llm_brain_then_openai_then_default():
    cfg = resolve_embedding_config(
        {
            "LLM_BRAIN_EMBEDDING_MODEL": "bge-m3",
            "LLM_BRAIN_EMBEDDING_BASE_URL": "http://127.0.0.1:8930/v1",
            "OPENAI_API_KEY": "fallback-key",
            "LLM_BRAIN_EMBEDDING_DIM": "1024",
        }
    )
    assert cfg["model"] == "bge-m3"
    assert cfg["base_url"] == "http://127.0.0.1:8930/v1"
    assert cfg["api_key"] == "fallback-key"  # OPENAI_* fallback when LLM_BRAIN_* absent
    assert cfg["dim"] == 1024


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


def test_provider_satisfies_adapter_embedding_protocol_end_to_end():
    client = InMemoryQdrantClient()
    provider = build_openai_embedding_provider(
        environ={"LLM_BRAIN_EMBEDDING_MODEL": "bge-m3", "LLM_BRAIN_EMBEDDING_DIM": "16"},
        embed_fn=_fake_embed(16),
    )
    adapter = QdrantDoclingMirrorAdapter(
        client=client,
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
