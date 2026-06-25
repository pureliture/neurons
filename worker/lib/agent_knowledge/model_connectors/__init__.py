"""Shared model connector configuration and policy helpers."""

from .env import (
    DEFAULT_EMBEDDING_DIM,
    resolve_embedding_spec,
    resolve_model_connection_config,
    resolve_reranker_spec,
)
from .policy import ModelConnectorConfigError, ModelPolicy, PolicyViolation
from .reranker import (
    CandidateReranker,
    FunctionRerankerClient,
    GraphitiCrossEncoderAdapter,
    OpenAICompatibleRerankerClient,
    RankFn,
    RerankerClient,
    build_candidate_reranker,
    build_reranker_client,
    resolve_reranker_config,
)
from .specs import EmbeddingSpec, ModelConnectionConfig, ModelEndpointSpec, RerankerSpec

__all__ = [
    "CandidateReranker",
    "DEFAULT_EMBEDDING_DIM",
    "EmbeddingSpec",
    "FunctionRerankerClient",
    "GraphitiCrossEncoderAdapter",
    "ModelConnectionConfig",
    "ModelEndpointSpec",
    "ModelConnectorConfigError",
    "ModelPolicy",
    "OpenAICompatibleRerankerClient",
    "PolicyViolation",
    "RankFn",
    "RerankerClient",
    "RerankerSpec",
    "build_candidate_reranker",
    "build_reranker_client",
    "resolve_embedding_spec",
    "resolve_model_connection_config",
    "resolve_reranker_spec",
    "resolve_reranker_config",
]
