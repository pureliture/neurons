from __future__ import annotations

import pytest

from agent_knowledge.llm_brain_core.graph import NullGraphMemoryAdapter
from agent_knowledge.llm_brain_core.runtime_graph import build_graph_adapter_from_env, graph_env_enabled


def test_graph_env_enabled_reads_explicit_switch():
    assert graph_env_enabled({"LLM_BRAIN_GRAPH_ENABLED": "true"}) is True
    assert graph_env_enabled({"LLM_BRAIN_GRAPH_ENABLED": "0"}) is False


def test_graph_adapter_required_but_disabled_fails_before_backend_initialization():
    with pytest.raises(ValueError, match="graph is required but not enabled"):
        build_graph_adapter_from_env({}, enabled=False, required=True)


def test_graph_adapter_disabled_returns_null_adapter_when_not_required():
    adapter = build_graph_adapter_from_env({}, enabled=False, required=False)

    assert isinstance(adapter, NullGraphMemoryAdapter)
