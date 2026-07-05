from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from agent_knowledge.model_connectors import (
    CandidateReranker,
    DEFAULT_EMBEDDING_DIM,
    EmbeddingSpec,
    FunctionRerankerClient,
    GraphitiCrossEncoderAdapter,
    ModelConnectionConfig,
    ModelConnectorConfigError,
    ModelEndpointSpec,
    ModelPolicy,
    OpenAICompatibleRerankerClient,
    PolicyViolation,
    RerankerSpec,
    resolve_embedding_spec,
    resolve_model_connection_config,
    resolve_reranker_spec,
)
from agent_knowledge.model_connectors.openai_compatible import (
    build_openai_compatible_graphiti_components,
)


def test_shared_structured_response_normalizer_contract_is_lightweight():
    from pydantic import BaseModel

    from agent_knowledge.model_connectors.structured_response import normalize_structured_response

    class ExtractedEntities(BaseModel):
        extracted_entities: list[dict]

    class EdgeDuplicate(BaseModel):
        duplicate_facts: list[int]
        contradicted_facts: list[int]

    normalized_entities = normalize_structured_response(
        [{"entity_text": "Neo4j", "episode_indices": ["bad", "1"]}],
        ExtractedEntities,
    )
    normalized_edges = normalize_structured_response(
        {
            "duplicate_facts": [0, "2", 6, -1, "bad"],
            "contradicted_facts": [0, 2, 6],
        },
        EdgeDuplicate,
        valid_duplicate_fact_idxs={0, 2},
    )

    assert normalized_entities == {
        "extracted_entities": [{"name": "Neo4j", "entity_type_id": 0, "episode_indices": [0, 1]}]
    }
    assert normalized_edges["duplicate_facts"] == [0, 2]
    assert normalized_edges["contradicted_facts"] == [0, 2, 6]


def test_shared_structured_response_normalizer_handles_none_values_defensively():
    from pydantic import BaseModel

    from agent_knowledge.model_connectors.structured_response import (
        existing_fact_idx_values_from_messages,
        normalize_structured_response,
    )

    class ExtractedEntities(BaseModel):
        extracted_entities: list[dict]

    class EdgeDuplicate(BaseModel):
        duplicate_facts: list[int]

    normalized_entities = normalize_structured_response(
        [{"entity_text": "Neo4j", "episode_indices": [None, "1"]}],
        ExtractedEntities,
    )
    normalized_edges = normalize_structured_response(
        {"duplicate_facts": [None, "2"]},
        EdgeDuplicate,
        valid_duplicate_fact_idxs={0, 2},
    )

    assert normalized_entities["extracted_entities"][0]["episode_indices"] == [0, 1]
    assert normalized_edges["duplicate_facts"] == [2]
    assert existing_fact_idx_values_from_messages(None) is None
    assert existing_fact_idx_values_from_messages([]) is None


def test_model_connection_config_preserves_env_precedence_and_omits_secrets():
    config = resolve_model_connection_config(
        {
            "LLM_BRAIN_GRAPH_LLM_PROVIDER": "ollama",
            "GRAPHITI_LLM_PROVIDER": "should-lose",
            "LLM_BRAIN_LLM_MODEL": "primary-llm",
            "MODEL_NAME": "legacy-llm",
            "LLM_BRAIN_SMALL_LLM_MODEL": "primary-small",
            "SMALL_MODEL_NAME": "legacy-small",
            "LLM_BRAIN_LLM_BASE_URL": "http://primary/v1",
            "OPENAI_BASE_URL": "http://legacy/v1",
            "LLM_BRAIN_LLM_API_KEY": "secret-llm",
            "LLM_BRAIN_EMBEDDING_MODEL": "primary-embed",
            "EMBEDDING_MODEL": "legacy-embed",
            "LLM_BRAIN_EMBEDDING_BASE_URL": "http://embed/v1",
            "LLM_BRAIN_EMBEDDING_API_KEY": "secret-embed",
            "LLM_BRAIN_EMBEDDING_DIM": "768",
            "LLM_BRAIN_LLM_FALLBACK_MODEL": "fallback-llm",
            "LLM_BRAIN_SMALL_LLM_FALLBACK_MODEL": "fallback-small",
            "LLM_BRAIN_GRAPH_PRIMARY_ATTEMPTS": "3",
            "LLM_BRAIN_GRAPH_FALLBACK_ATTEMPTS": "2",
        }
    )

    assert config.llm.provider == "ollama"
    assert config.llm.model == "primary-llm"
    assert config.llm.small_model == "primary-small"
    assert config.llm.base_url == "http://primary/v1"
    assert config.embedding.provider == "openai"
    assert config.embedding.model == "primary-embed"
    assert config.embedding.base_url == "http://embed/v1"
    assert config.embedding.dim == 768
    assert config.reranker.model == "primary-llm"
    assert config.reranker.base_url == "http://primary/v1"
    assert config.fallback_llm_model == "fallback-llm"
    assert config.fallback_small_model == "fallback-small"
    assert config.primary_attempts == 3
    assert config.fallback_attempts == 2
    assert "secret" not in repr(config)


def test_embedding_and_reranker_specs_preserve_legacy_fallbacks():
    embedding = resolve_embedding_spec(
        {"EMBEDDING_MODEL": "legacy-embed", "OPENAI_BASE_URL": "http://legacy/v1"}
    )
    reranker = resolve_reranker_spec(
        {"MODEL_NAME": "legacy-llm", "OPENAI_BASE_URL": "http://legacy/v1"}
    )

    assert embedding.model == "legacy-embed"
    assert embedding.provider == "openai"
    assert embedding.base_url == "http://legacy/v1"
    assert embedding.dim == DEFAULT_EMBEDDING_DIM
    assert reranker.model == "legacy-llm"
    assert reranker.base_url == "http://legacy/v1"


def test_model_connection_config_inherits_llm_endpoint_for_embedding_when_embedding_endpoint_absent():
    config = resolve_model_connection_config(
        {
            "LLM_BRAIN_GRAPH_LLM_PROVIDER": "ollama",
            "LLM_BRAIN_LLM_MODEL": "llm",
            "LLM_BRAIN_LLM_BASE_URL": "http://ollama.test/v1",
            "LLM_BRAIN_EMBEDDING_MODEL": "nomic-embed-text",
        }
    )

    assert config.embedding.provider == "ollama"
    assert config.embedding.base_url == "http://ollama.test/v1"


def test_embedding_dim_defaults_on_bad_values():
    assert resolve_embedding_spec({"LLM_BRAIN_EMBEDDING_DIM": "bad"}).dim == DEFAULT_EMBEDDING_DIM
    assert resolve_embedding_spec({"LLM_BRAIN_EMBEDDING_DIM": "-1"}).dim == DEFAULT_EMBEDDING_DIM


def test_shared_reranker_client_feeds_qdrant_and_graphiti_consumers():
    shared = FunctionRerankerClient(lambda _q, texts: [float(len(text)) for text in texts])
    qdrant = CandidateReranker(shared)
    graphiti = GraphitiCrossEncoderAdapter(shared)

    qdrant_ranked = qdrant.rerank(
        query="ledger",
        candidates=[{"summary": "tiny"}, {"summary": "largest candidate"}],
        top_n=2,
    )
    graphiti_ranked = asyncio.run(graphiti.rank("ledger", ["tiny", "largest candidate"]))

    assert [item["summary"] for item in qdrant_ranked] == ["largest candidate", "tiny"]
    assert graphiti_ranked == [("largest candidate", 17.0), ("tiny", 4.0)]


def test_shared_graphiti_cross_encoder_rejects_score_count_mismatch():
    graphiti = GraphitiCrossEncoderAdapter(FunctionRerankerClient(lambda _q, _texts: [1.0]))

    with pytest.raises(ValueError, match="reranker returned wrong score count"):
        asyncio.run(graphiti.rank("q", ["a", "b"]))


def test_openai_compatible_reranker_client_preserves_duplicate_passage_scores():
    class _Choice:
        def __init__(self, content: str) -> None:
            self.message = type("_Message", (), {"content": content})()
            self.logprobs = None

    class _Response:
        def __init__(self, content: str) -> None:
            self.choices = [_Choice(content)]

    class _Completions:
        def __init__(self) -> None:
            self.calls = 0

        async def create(self, **_kwargs):
            self.calls += 1
            return _Response("True" if self.calls == 1 else "False")

    class _Client:
        def __init__(self) -> None:
            self.chat = type("_Chat", (), {"completions": _Completions()})()

    client = OpenAICompatibleRerankerClient(
        RerankerSpec(provider="openai-compatible", model="rerank", base_url="http://example.test/v1"),
        openai_client=_Client(),
    )

    assert asyncio.run(client.ascore("q", ["same", "same"])) == [1.0, 0.0]


def test_openai_compatible_reranker_client_uses_top_logprobs_true_probability():
    class _Choice:
        def __init__(self, content: str, token: str, logprob: float) -> None:
            self.message = type("_Message", (), {"content": content})()
            self.logprobs = type(
                "_Logprobs",
                (),
                {
                    "content": [type("_Content", (), {"top_logprobs": [type("_Top", (), {"token": token, "logprob": logprob})]})()]
                },
            )()

    class _Response:
        def __init__(self, content: str, token: str, logprob: float) -> None:
            self.choices = [_Choice(content, token, logprob)]

    class _Completions:
        def __init__(self) -> None:
            self.calls = 0

        async def create(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return _Response("False", "true", -0.10536051565782628)
            return _Response("False", "true", -0.6931471805599453)

    class _Client:
        def __init__(self) -> None:
            self.chat = type("_Chat", (), {"completions": _Completions()})()

    client = OpenAICompatibleRerankerClient(
        RerankerSpec(provider="openai-compatible", model="rerank", base_url="http://example.test/v1"),
        openai_client=_Client(),
    )

    assert asyncio.run(client.ascore("q", ["a", "b"])) == [0.9, 0.5]


def test_openai_compatible_reranker_client_uses_top_logprobs_false_inverted_probability():
    class _Choice:
        def __init__(self, content: str, token: str, logprob: float) -> None:
            self.message = type("_Message", (), {"content": content})()
            self.logprobs = type(
                "_Logprobs",
                (),
                {
                    "content": [type("_Content", (), {"top_logprobs": [type("_Top", (), {"token": token, "logprob": logprob})]})()]
                },
            )()

    class _Response:
        def __init__(self, content: str, token: str, logprob: float) -> None:
            self.choices = [_Choice(content, token, logprob)]

    class _Completions:
        async def create(self, **_kwargs):
            return _Response("True", "false", -1.6094379124341003)

    class _Client:
        def __init__(self) -> None:
            self.chat = type("_Chat", (), {"completions": _Completions()})()

    client = OpenAICompatibleRerankerClient(
        RerankerSpec(provider="openai-compatible", model="rerank", base_url="http://example.test/v1"),
        openai_client=_Client(),
    )

    assert asyncio.run(client.ascore("q", ["a"])) == [0.8]


def test_candidate_reranker_honors_zero_top_n():
    shared = FunctionRerankerClient(lambda _q, _texts: [1.0, 2.0])
    reranker = CandidateReranker(shared)

    assert reranker.rerank(
        query="q",
        candidates=[{"summary": "a"}, {"summary": "b"}],
        top_n=0,
    ) == []


def test_model_policy_denies_gemini_chat_but_allows_embedding_and_gemma_maas():
    policy = ModelPolicy()

    with pytest.raises(PolicyViolation):
        policy.validate(ModelEndpointSpec(provider="gemini", model="gemini-flash"), capability="structured_extraction")
    with pytest.raises(PolicyViolation):
        policy.validate(ModelEndpointSpec(provider="typo-provider", model="x"), capability="rerank")

    with pytest.raises(PolicyViolation):
        policy.validate({"provider": "gemini"}, capability="embedding")
    policy.validate(ModelEndpointSpec(provider="gemma4-maas", model="gemma"), capability="structured_extraction")


def test_openai_compatible_graphiti_components_fail_closed_when_model_missing():
    config = ModelConnectionConfig(
        llm=ModelEndpointSpec(provider="openai-compatible", base_url="http://example.test/v1"),
        embedding=EmbeddingSpec(model="embed", base_url="http://example.test/v1"),
        reranker=RerankerSpec(provider="openai-compatible", base_url="http://example.test/v1"),
    )

    with pytest.raises(ModelConnectorConfigError, match="LLM_BRAIN_LLM_MODEL"):
        build_openai_compatible_graphiti_components(config)


def test_openai_compatible_graphiti_components_fail_closed_when_embedding_missing():
    config = ModelConnectionConfig(
        llm=ModelEndpointSpec(
            provider="openai-compatible",
            model="llm",
            base_url="http://example.test/v1",
        ),
        embedding=EmbeddingSpec(base_url="http://example.test/v1"),
        reranker=RerankerSpec(
            provider="openai-compatible",
            model="llm",
            base_url="http://example.test/v1",
        ),
    )

    with pytest.raises(ModelConnectorConfigError, match="LLM_BRAIN_EMBEDDING_MODEL"):
        build_openai_compatible_graphiti_components(config)


def test_openai_compatible_graphiti_components_validate_embedding_provider_independently():
    config = ModelConnectionConfig(
        llm=ModelEndpointSpec(
            provider="ollama",
            model="llm",
            base_url="http://llm.test/v1",
        ),
        embedding=EmbeddingSpec(
            provider="openai-compatible",
            base_url="http://embed.test/v1",
        ),
        reranker=RerankerSpec(
            provider="ollama",
            model="llm",
            base_url="http://llm.test/v1",
        ),
    )

    with pytest.raises(ModelConnectorConfigError, match="LLM_BRAIN_EMBEDDING_MODEL"):
        build_openai_compatible_graphiti_components(config)


def test_openai_compatible_graphiti_components_fail_closed_for_unknown_provider():
    config = ModelConnectionConfig(
        llm=ModelEndpointSpec(provider="typo-provider", model="llm", base_url="http://example.test/v1"),
        embedding=EmbeddingSpec(model="embed", base_url="http://example.test/v1"),
        reranker=RerankerSpec(provider="typo-provider", model="llm", base_url="http://example.test/v1"),
    )

    with pytest.raises((ModelConnectorConfigError, PolicyViolation)):
        build_openai_compatible_graphiti_components(config)


def test_openai_compatible_graphiti_components_use_shared_cross_encoder_adapter():
    config = ModelConnectionConfig(
        llm=ModelEndpointSpec(provider="ollama", model="llm", base_url="http://localhost:11434/v1"),
        embedding=EmbeddingSpec(model="embed", base_url="http://localhost:11434/v1"),
        reranker=RerankerSpec(provider="ollama", model="llm", base_url="http://localhost:11434/v1"),
    )

    _llm_client, _embedder, cross_encoder = build_openai_compatible_graphiti_components(config)

    assert isinstance(cross_encoder, GraphitiCrossEncoderAdapter)


def test_importing_model_connectors_does_not_load_heavy_llm_brain_core():
    code = (
        "import sys; import agent_knowledge.model_connectors as _m; "
        "leaked = sorted(k for k in sys.modules if 'llm_brain_core' in k or 'graphiti_core' in k); "
        "assert not leaked, leaked"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
