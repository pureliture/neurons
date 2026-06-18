"""Backend-neutral LLM-brain core contracts."""

from .artifact_store import InMemorySessionMemoryArtifactStore
from .context import BrainReadService
from .document_bridge import DisabledDocumentBridge, DocumentBridgeResult
from .event_replay import BrainEventReplayStore
from .graph import FakeGraphMemoryAdapter, NullGraphMemoryAdapter
from .graphiti_adapter import GraphitiNeo4jConfig, GraphitiNeo4jGraphMemoryAdapter
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
from .ontology import build_ontology_episode_batch, episode_from_session_artifact, episode_from_source_ref
from .projection import GraphProjectionReport, GraphProjectionWorker
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
    "SessionMemoryArtifact",
    "SourceRefRecord",
    "SourceRefResolver",
    "build_ontology_episode_batch",
    "episode_from_session_artifact",
    "episode_from_source_ref",
]
