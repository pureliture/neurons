"""Backend-neutral LLM-brain core contracts."""

from .artifact_store import InMemorySessionMemoryArtifactStore
from .context import BrainReadService
from .document_bridge import DisabledDocumentBridge, DocumentBridgeResult
from .event_replay import BrainEventReplayStore
from .graph import FakeGraphMemoryAdapter, NullGraphMemoryAdapter, UnavailableGraphMemoryAdapter
from .graphiti_adapter import (
    GraphitiNeo4jConfig,
    GraphitiNeo4jGraphMemoryAdapter,
    probe_graphiti_connectivity,
)
from .ledger_adapter import LedgerSessionMemoryArtifactStore, LedgerSourceRefCatalog
from .models import (
    BrainEventEnvelope,
    ContextPack,
    EvidenceRequest,
    EvidenceResponse,
    GraphMemoryResult,
    OntologyEpisode,
    SessionMemoryArtifact,
    SourceRefRecord,
)
from .ontology import (
    OntologyEpisodeBatch,
    build_ontology_episode_batch,
    build_ontology_episode_batch_report,
    episode_from_session_artifact,
    episode_from_source_ref,
)
from .projection import GraphProjectionReport, GraphProjectionWorker
from .runtime_graph import build_graph_adapter_from_env, graph_env_enabled
from .source_ref import SourceRefResolver
from .sync_shadow import CentralBrainShadowRebuilder, CentralShadowRebuildReport

__all__ = [
    "BrainEventEnvelope",
    "BrainEventReplayStore",
    "BrainReadService",
    "CentralBrainShadowRebuilder",
    "CentralShadowRebuildReport",
    "ContextPack",
    "DisabledDocumentBridge",
    "DocumentBridgeResult",
    "EvidenceRequest",
    "EvidenceResponse",
    "FakeGraphMemoryAdapter",
    "GraphMemoryResult",
    "GraphProjectionReport",
    "GraphProjectionWorker",
    "GraphitiNeo4jConfig",
    "GraphitiNeo4jGraphMemoryAdapter",
    "InMemorySessionMemoryArtifactStore",
    "LedgerSessionMemoryArtifactStore",
    "LedgerSourceRefCatalog",
    "NullGraphMemoryAdapter",
    "OntologyEpisode",
    "OntologyEpisodeBatch",
    "SessionMemoryArtifact",
    "SourceRefRecord",
    "SourceRefResolver",
    "UnavailableGraphMemoryAdapter",
    "build_ontology_episode_batch",
    "build_ontology_episode_batch_report",
    "build_graph_adapter_from_env",
    "episode_from_session_artifact",
    "episode_from_source_ref",
    "graph_env_enabled",
    "probe_graphiti_connectivity",
]
