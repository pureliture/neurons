"""Backend-neutral LLM-brain core contracts."""

from .artifact_store import InMemorySessionMemoryArtifactStore
from .authority_bundle import build_markdown_authority_bundle, check_markdown_authority_bundle_drift
from .authority_projection import authority_episodes_from_context_pack
from .central_federation import NEURONS_CENTRAL_FEDERATION_SCHEMA, federate_neurons_local_artifacts
from .context import BrainReadService
from .document_authority import (
    DocumentAuthorityCard,
    DocumentEvidenceEdge,
    document_authority_cards_from_memory_cards,
)
from .document_bridge import DisabledDocumentBridge, DocumentBridgeResult
from .event_replay import BrainEventReplayStore
from .graph import FakeGraphMemoryAdapter, NullGraphMemoryAdapter, UnavailableGraphMemoryAdapter
from .graphiti_adapter import (
    GraphitiNeo4jConfig,
    GraphitiNeo4jGraphMemoryAdapter,
    probe_graphiti_connectivity,
)
from .hybrid_graph import (
    HybridTextMirrorHit,
    InMemoryHybridTextMirror,
    METADATA_FIRST_HYBRID_GRAPH_SCHEMA,
    MetadataFirstHybridGraphAdapter,
    metadata_first_episode,
)
from .infra_baseline import (
    COMPOSE_BASELINE_REPORT_SCHEMA,
    K3S_POC_CANARY_PLAN_SCHEMA,
    K3S_POC_EXECUTION_EVIDENCE_SCHEMA,
    K3S_POC_MANIFEST_BUNDLE_SCHEMA,
    K3S_POC_OPERATOR_APPROVAL_PACKET_SCHEMA,
    compose_baseline_report,
    k3s_poc_canary_manifest_bundle,
    k3s_poc_execution_evidence,
    k3s_poc_canary_plan,
    k3s_poc_operator_approval_packet,
)
from .ledger_adapter import (
    LedgerGraphProjectionStateStore,
    LedgerSessionMemoryArtifactStore,
    LedgerSourceRefCatalog,
)
from .local_evidence import (
    LocalEvidenceEdge,
    local_evidence_edges_from_capture,
    local_evidence_episodes_from_capture,
)
from .local_brain import (
    NEURONS_LOCAL_SYNC_ARTIFACT_SCHEMA,
    build_neurons_local_sync_artifact,
    resolve_neurons_local_context,
)
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
from .preference_authority import PreferenceRuleCard, preference_rule_cards_from_memory_cards
from .projection import GraphProjectionReport, GraphProjectionWorker
from .repo_style_profile import REPO_STYLE_PROFILE_SCHEMA, repo_style_profile_from_memory_cards
from .runtime_graph import build_graph_adapter_from_env, graph_env_enabled, metadata_first_hybrid_enabled
from .source_ref import SourceRefResolver
from .sync_shadow import CentralBrainShadowRebuilder, CentralShadowRebuildReport
from .workflow_authority import (
    SkillEvolutionCard,
    WorkflowContractCard,
    WorkflowDefaultCard,
    skill_evolution_cards_from_memory_cards,
    workflow_contract_cards_from_memory_cards,
    workflow_default_cards_from_memory_cards,
)

__all__ = [
    "BrainEventEnvelope",
    "BrainEventReplayStore",
    "BrainReadService",
    "CentralBrainShadowRebuilder",
    "CentralShadowRebuildReport",
    "COMPOSE_BASELINE_REPORT_SCHEMA",
    "ContextPack",
    "DisabledDocumentBridge",
    "DocumentBridgeResult",
    "DocumentAuthorityCard",
    "DocumentEvidenceEdge",
    "EvidenceRequest",
    "EvidenceResponse",
    "FakeGraphMemoryAdapter",
    "GraphMemoryResult",
    "GraphProjectionReport",
    "GraphProjectionWorker",
    "GraphitiNeo4jConfig",
    "GraphitiNeo4jGraphMemoryAdapter",
    "HybridTextMirrorHit",
    "InMemorySessionMemoryArtifactStore",
    "LedgerGraphProjectionStateStore",
    "InMemoryHybridTextMirror",
    "LedgerSessionMemoryArtifactStore",
    "LedgerSourceRefCatalog",
    "LocalEvidenceEdge",
    "METADATA_FIRST_HYBRID_GRAPH_SCHEMA",
    "K3S_POC_CANARY_PLAN_SCHEMA",
    "K3S_POC_EXECUTION_EVIDENCE_SCHEMA",
    "K3S_POC_MANIFEST_BUNDLE_SCHEMA",
    "K3S_POC_OPERATOR_APPROVAL_PACKET_SCHEMA",
    "MetadataFirstHybridGraphAdapter",
    "NullGraphMemoryAdapter",
    "NEURONS_CENTRAL_FEDERATION_SCHEMA",
    "NEURONS_LOCAL_SYNC_ARTIFACT_SCHEMA",
    "OntologyEpisode",
    "OntologyEpisodeBatch",
    "PreferenceRuleCard",
    "REPO_STYLE_PROFILE_SCHEMA",
    "SessionMemoryArtifact",
    "SkillEvolutionCard",
    "SourceRefRecord",
    "SourceRefResolver",
    "UnavailableGraphMemoryAdapter",
    "WorkflowContractCard",
    "WorkflowDefaultCard",
    "authority_episodes_from_context_pack",
    "build_markdown_authority_bundle",
    "build_ontology_episode_batch",
    "check_markdown_authority_bundle_drift",
    "compose_baseline_report",
    "document_authority_cards_from_memory_cards",
    "preference_rule_cards_from_memory_cards",
    "skill_evolution_cards_from_memory_cards",
    "workflow_contract_cards_from_memory_cards",
    "workflow_default_cards_from_memory_cards",
    "build_ontology_episode_batch_report",
    "build_graph_adapter_from_env",
    "episode_from_session_artifact",
    "episode_from_source_ref",
    "federate_neurons_local_artifacts",
    "graph_env_enabled",
    "k3s_poc_canary_manifest_bundle",
    "k3s_poc_execution_evidence",
    "k3s_poc_canary_plan",
    "k3s_poc_operator_approval_packet",
    "build_neurons_local_sync_artifact",
    "local_evidence_edges_from_capture",
    "local_evidence_episodes_from_capture",
    "metadata_first_hybrid_enabled",
    "metadata_first_episode",
    "probe_graphiti_connectivity",
    "resolve_neurons_local_context",
    "repo_style_profile_from_memory_cards",
]
