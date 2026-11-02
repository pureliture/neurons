"""Shared model connector configuration and policy helpers."""

from .env import (
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_PROFILE_ID,
    LOCAL_EMBEDDING_DIM,
    LOCAL_EMBEDDING_MODEL,
    LOCAL_EMBEDDING_PROFILE_ID,
    resolve_embedding_profile_id,
    resolve_embedding_spec,
    resolve_model_connection_config,
    resolve_reranker_spec,
)
from .policy import (
    ModelConnectorConfigError,
    ModelPolicy,
    PaidEmbeddingFallbackBlockedError,
    PolicyViolation,
    validate_local_embedding_write_or_query_policy,
)
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
from .session_headers import (
    GRAPH_WORKER_SESSION_ID,
    OPENCODE_SESSION_HEADER,
    is_opencode_base_url,
    opencode_session_headers,
    opencode_session_id,
)
from .specs import EmbeddingSpec, ModelConnectionConfig, ModelEndpointSpec, RerankerSpec

__all__ = [
    "CandidateReranker",
    "DEFAULT_EMBEDDING_DIM",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_EMBEDDING_PROFILE_ID",
    "LOCAL_EMBEDDING_DIM",
    "LOCAL_EMBEDDING_MODEL",
    "LOCAL_EMBEDDING_PROFILE_ID",
    "EmbeddingSpec",
    "FunctionRerankerClient",
    "GRAPH_WORKER_SESSION_ID",
    "GraphitiCrossEncoderAdapter",
    "ModelConnectionConfig",
    "ModelEndpointSpec",
    "ModelConnectorConfigError",
    "ModelPolicy",
    "OPENCODE_SESSION_HEADER",
    "OpenAICompatibleRerankerClient",
    "PolicyViolation",
    "RankFn",
    "RerankerClient",
    "RerankerSpec",
    "build_candidate_reranker",
    "build_reranker_client",
    "is_opencode_base_url",
    "opencode_session_headers",
    "opencode_session_id",
    "resolve_embedding_profile_id",
    "resolve_embedding_spec",
    "resolve_model_connection_config",
    "resolve_reranker_spec",
    "resolve_reranker_config",
]
