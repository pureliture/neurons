"""Server-owned session-memory primitives vendored into neurons.

Only the modules already moved into this worker are exported here. Additional
brain/session-memory surfaces should be added as their ownership slices land.
"""

from importlib import import_module

_EXPORT_MODULES = {
    "BrainReadModel": ".brain_query",
    "CurationService": ".curation",
    "FakeMemoryMiner": ".memory_miner",
    "LlmMemoryMiner": ".memory_miner",
    "TerminalSkippedQuarantineRunner": ".terminal_skipped_quarantine",
    "TranscriptChunk": ".transcript_model",
    "ZombieSnapshotRepairRunner": ".zombie_snapshot_repair",
    "build_memory_candidate": ".memory_card",
    "build_memory_card": ".memory_card",
    "build_memory_card_candidate_from_source_span": ".memory_miner",
    "plan_context_query": ".query_planner",
    "resolve_brain_ids": ".brain_query",
    "run_brain_query": ".brain_query",
    "run_brain_query_v2": ".brain_query",
    "sha256_text": ".query_planner",
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
