"""Backend-neutral LLM-brain core contracts."""

from .artifact_store import InMemorySessionMemoryArtifactStore
from .context import BrainReadService
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
from .projection import GraphProjectionReport, GraphProjectionWorker
from .source_ref import SourceRefResolver

__all__ = [
    "BrainEventEnvelope",
    "BrainEventReplayStore",
    "BrainReadService",
    "ContextPack",
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
]
