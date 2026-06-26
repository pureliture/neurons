from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

from .graph import GraphMemoryAdapter, NullGraphMemoryAdapter, UnavailableGraphMemoryAdapter
from .graphiti_adapter import GraphitiNeo4jGraphMemoryAdapter, probe_graphiti_connectivity
from .hybrid_graph import MetadataFirstHybridGraphAdapter

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off", ""}

# Connectivity probe seam. Tests inject a failing probe to exercise the
# required=True fail-fast path without a live Neo4j. The probe receives the
# built adapter and must raise on failure.
GraphConnectivityProbe = Callable[[Any], None]


def graph_env_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get("LLM_BRAIN_GRAPH_ENABLED", "")).lower() in _TRUTHY


def metadata_first_hybrid_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    value = str(env.get("LLM_BRAIN_GRAPH_METADATA_FIRST_HYBRID", "")).lower()
    if value in _FALSY:
        return False
    return value in _TRUTHY


def build_graph_adapter_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    enable_flag: bool | None = None,
    required_flag: bool = False,
    enabled: bool | None = None,
    required: bool = False,
    probe: GraphConnectivityProbe | None = None,
) -> GraphMemoryAdapter:
    """Build the graph adapter under one shared enable/required policy.

    Policy (identical across projection_cli / cli / mcp_server entrypoints):

    - `--enable-graph` is best-effort: turn the backend on, but degrade to an
      `UnavailableGraphMemoryAdapter` if it cannot be initialized or reached.
    - `--graph-required` is must-have: the backend must initialize AND pass a
      one-shot connectivity probe, otherwise raise (fail-fast). `required`
      implies `enabled`.

    `enable_flag`/`required_flag` are the canonical parameter names. The legacy
    `enabled`/`required` aliases are kept so existing callers keep working; when
    both are given the canonical names win.
    """

    enable = enable_flag if enable_flag is not None else enabled
    require = bool(required_flag or required)

    env = dict(os.environ if environ is None else environ)
    connectivity_probe = probe if probe is not None else probe_graphiti_connectivity

    # An explicit `enable=False` with `require=True` is a contradiction: fail
    # fast before touching the backend instead of silently overriding.
    if require and enable is False:
        raise ValueError("graph is required but not enabled")

    # required is must-have, so otherwise it implies the backend should be on.
    should_enable = require or (graph_env_enabled(env) if enable is None else bool(enable))

    if not should_enable:
        if require:
            raise ValueError("graph is required but not enabled")
        return NullGraphMemoryAdapter()

    metadata_first = metadata_first_hybrid_enabled(env)
    adapter_env = _metadata_first_graphiti_env(env) if metadata_first else env

    try:
        adapter = GraphitiNeo4jGraphMemoryAdapter.from_env(adapter_env)
    except Exception as exc:
        if require:
            raise
        return UnavailableGraphMemoryAdapter(type(exc).__name__)

    if require:
        # must-have: a one-shot connectivity probe so a dead backend fails fast
        # instead of surfacing as a false 'available' read later.
        connectivity_probe(adapter)

    if metadata_first:
        return MetadataFirstHybridGraphAdapter(adapter)
    return adapter


def _metadata_first_graphiti_env(env: Mapping[str, str]) -> dict[str, str]:
    graphiti_env = dict(env)
    graphiti_env["LLM_BRAIN_GRAPH_EXTRACT_ENTITIES"] = "false"
    graphiti_env.pop("LLM_BRAIN_GRAPH_FORCE_REEXTRACT_ENTITIES", None)
    return graphiti_env
