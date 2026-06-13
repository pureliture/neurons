"""Server-owned session-memory primitives vendored into neurons.

Only the modules already moved into this worker are exported here. Additional
brain/session-memory surfaces should be added as their ownership slices land.
"""

from importlib import import_module

_EXPORT_MODULES = {
    "TranscriptChunk": ".transcript_model",
    "build_memory_candidate": ".memory_card",
    "build_memory_card": ".memory_card",
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
