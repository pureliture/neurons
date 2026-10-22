"""Server-owned session-memory primitives vendored into neurons.

Only the modules already moved into this worker are exported here. Additional
brain/session-memory surfaces should be added as their ownership slices land.
"""

from importlib import import_module

_EXPORT_MODULES = {
    "BrainReadModel": ".brain_query",
    "CurationService": ".curation",
    "FakeMemoryMiner": ".memory_miner",
    "LegacyLedgerBrainReadModel": ".brain_read_model",
    "LLMBrainMemoryService": ".llm_brain_service",
    "LlmMemoryMiner": ".memory_miner",
    "APPROVAL_REQUIRED_FIELDS": ".backfill",
    "DEFAULT_PROJECT_MEMORY_TARGET_PROFILE": ".memory_regeneration",
    "DEFAULT_SESSION_MEMORY_TARGET_PROFILE": ".memory_regeneration",
    "FixtureTranscriptMemorySource": ".memory_regeneration",
    "LedgerTranscriptMemorySource": ".memory_regeneration",
    "ApprovalError": ".native_memory_sync_approval",
    "PROJECT_CONTEXT_SNAPSHOT_KIND": ".memory_regeneration",
    "PROJECT_MEMORY_DATASET_ROLE": ".memory_regeneration",
    "ProjectChunkGroup": ".memory_regeneration",
    "ProjectMemoryRegenerationRunner": ".memory_regeneration",
    "RetiredIndexBridgeMemoryCardProjectionClient": ".index_projection",
    "SESSION_MEMORY_DATASET_ROLE": ".memory_regeneration",
    "SessionChunkGroup": ".memory_regeneration",
    "SessionMemoryRegenerationRunner": ".memory_regeneration",
    "apply_auto_acceptance_plan": ".memory_evaluation",
    "TerminalSkippedQuarantineRunner": ".terminal_skipped_quarantine",
    "ToolEvidenceSyncRunner": ".tool_evidence_sync",
    "TranscriptChunk": ".transcript_model",
    "TranscriptIngestResult": ".transcript_ingest",
    "TranscriptIngestWorker": ".transcript_ingest",
    "TranscriptMemoryChunkRecord": ".memory_regeneration",
    "TranscriptMemoryGcConfig": ".transcript_memory_gc",
    "TranscriptMemoryGcRunner": ".transcript_memory_gc",
    "ZombieSnapshotRepairRunner": ".zombie_snapshot_repair",
    "build_feedback_record": ".memory_promotion",
    "build_memory_candidate": ".memory_card",
    "build_memory_card": ".memory_card",
    "build_memory_card_candidate_from_source_span": ".memory_miner",
    "build_execute_plan": ".backfill",
    "build_policy_version": ".memory_evaluation",
    "build_projection_job": ".index_projection",
    "build_index_projection_payload": ".index_projection",
    "enqueue_projection_jobs": ".index_projection",
    "evaluate_candidate_for_auto_policy": ".memory_evaluation",
    "execute_projection_job": ".index_projection",
    "human_approve_memory_card_candidate": ".memory_promotion",
    "human_reject_memory_card_candidate": ".memory_promotion",
    "inventory_fixture_sources": ".backfill",
    "mark_candidate_needs_review": ".memory_promotion",
    "plan_context_query": ".query_planner",
    "pack_project_memory_document": ".memory_regeneration",
    "pack_session_memory_document": ".memory_regeneration",
    "pack_session_recap_document": ".memory_regeneration",
    "projection_idempotency_key": ".index_projection",
    "projection_lag_marker": ".index_projection",
    "render_projection_document": ".index_projection",
    "resolve_brain_ids": ".brain_query",
    "rollback_auto_policy_candidate": ".memory_evaluation",
    "run_brain_query": ".brain_query",
    "run_brain_query_v2": ".brain_query",
    "dry_run_backfill": ".backfill",
    "sha256_text": ".query_planner",
    "suggest_accept_from_evidence": ".memory_promotion",
    "suggest_superseded_classification": ".memory_promotion",
    "summarize_feedback_patterns": ".memory_evaluation",
    "TOOL_EVIDENCE_SYNC_SCHEMA_VERSION": ".tool_evidence_sync",
    "build_transcript_chunks": ".transcript_chunking",
    "knowledge_id_for_chunk": ".transcript_chunking",
    "validate_auto_policy_operation": ".memory_evaluation",
    "validate_native_memory_sync_approval": ".native_memory_sync_approval",
    "validate_memory_card_envelope": ".memory_card",
}

__all__ = sorted(_EXPORT_MODULES)


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value
