from __future__ import annotations

import os
from collections.abc import Mapping

from .graph import GraphMemoryAdapter, NullGraphMemoryAdapter, UnavailableGraphMemoryAdapter
from .graphiti_adapter import GraphitiNeo4jGraphMemoryAdapter

_TRUTHY = {"1", "true", "yes", "on"}


def graph_env_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get("LLM_BRAIN_GRAPH_ENABLED", "")).lower() in _TRUTHY


def build_graph_adapter_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    enabled: bool | None = None,
    required: bool = False,
) -> GraphMemoryAdapter:
    env = dict(os.environ if environ is None else environ)
    should_enable = graph_env_enabled(env) if enabled is None else bool(enabled)
    if not should_enable:
        if required:
            raise ValueError("graph is required but not enabled")
        return NullGraphMemoryAdapter()
    try:
        return GraphitiNeo4jGraphMemoryAdapter.from_env(env)
    except Exception as exc:
        if required:
            raise
        return UnavailableGraphMemoryAdapter(type(exc).__name__)
