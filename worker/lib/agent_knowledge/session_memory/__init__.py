"""Server-owned session-memory primitives vendored into neurons.

Only the modules already moved into this worker are exported here. Additional
brain/session-memory surfaces should be added as their ownership slices land.
"""

from importlib import import_module

_EXPORT_MODULES = {
    "BrainReadModel": ".brain_query",
    "CurationService": ".curation",
    "FakeMemoryMiner": ".memory_miner",
    "GC_BACKUP_SCHEMA_VERSION": ".gc_backup",
    "LegacyLedgerBrainReadModel": ".brain_read_model",
    "LLMBrainMemoryService": ".llm_brain_service",
    "LlmMemoryMiner": ".memory_miner",
    "ApprovedStatement": ".native_memory_writer",
    "NativeMemoryMirrorStore": ".native_memory_mirror",
    "NativeMemoryMirrorWriter": ".native_memory_writer",
    "NativeMemoryMirrorWriteRunner": ".native_memory_write_runner",
    "NATIVE_MEMORY_OVERFETCH_THRESHOLD": ".native_memory_recall",
    "NativeMemoryReconcileConfig": ".native_memory_reconcile",
    "NativeMemoryReconcileRunner": ".native_memory_reconcile",
    "NativeMemoryWriteConfig": ".native_memory_write_runner",
    "RagflowMemoryCardProjectionClient": ".ragflow_projection",
    "apply_auto_acceptance_plan": ".memory_evaluation",
    "adapt_card_to_statement": ".native_memory_write_runner",
    "TerminalSkippedQuarantineRunner": ".terminal_skipped_quarantine",
    "TranscriptChunk": ".transcript_model",
    "ZombieSnapshotRepairRunner": ".zombie_snapshot_repair",
    "brain_id_for_project": ".native_memory_mirror",
    "build_feedback_record": ".memory_promotion",
    "build_memory_candidate": ".memory_card",
    "build_memory_card": ".memory_card",
    "build_memory_card_candidate_from_source_span": ".memory_miner",
    "build_policy_version": ".memory_evaluation",
    "build_projection_job": ".ragflow_projection",
    "build_ragflow_projection_payload": ".ragflow_projection",
    "build_semantic_recall": ".brain_read_model",
    "enqueue_projection_jobs": ".ragflow_projection",
    "evaluate_candidate_for_auto_policy": ".memory_evaluation",
    "execute_projection_job": ".ragflow_projection",
    "filter_active_native_memory": ".native_memory_recall",
    "human_approve_memory_card_candidate": ".memory_promotion",
    "human_reject_memory_card_candidate": ".memory_promotion",
    "list_gc_backups": ".gc_backup",
    "mark_candidate_needs_review": ".memory_promotion",
    "plan_context_query": ".query_planner",
    "projection_idempotency_key": ".ragflow_projection",
    "projection_lag_marker": ".ragflow_projection",
    "recall_active_native_memory": ".native_memory_recall",
    "render_projection_document": ".ragflow_projection",
    "read_gc_backup": ".gc_backup",
    "resolve_brain_ids": ".brain_query",
    "rollback_auto_policy_candidate": ".memory_evaluation",
    "run_brain_query": ".brain_query",
    "run_brain_query_v2": ".brain_query",
    "run_native_memory_sync": ".native_memory_write_runner",
    "session_tag_for": ".native_memory_mirror",
    "sha256_text": ".query_planner",
    "suggest_accept_from_evidence": ".memory_promotion",
    "suggest_superseded_classification": ".memory_promotion",
    "summarize_feedback_patterns": ".memory_evaluation",
    "validate_auto_policy_operation": ".memory_evaluation",
    "validate_memory_card_envelope": ".memory_card",
    "write_gc_backup": ".gc_backup",
}

__all__ = sorted(_EXPORT_MODULES)


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value
