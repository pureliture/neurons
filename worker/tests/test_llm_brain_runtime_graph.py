from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_knowledge.llm_brain_core.graph import FakeGraphMemoryAdapter, NullGraphMemoryAdapter, UnavailableGraphMemoryAdapter
from agent_knowledge.llm_brain_core.models import OntologyEpisode
from agent_knowledge.llm_brain_core.runtime_graph import (
    build_graph_adapter_from_env,
    graph_env_enabled,
    metadata_first_hybrid_enabled,
)


def test_graph_env_enabled_reads_explicit_switch():
    assert graph_env_enabled({"LLM_BRAIN_GRAPH_ENABLED": "true"}) is True
    assert graph_env_enabled({"LLM_BRAIN_GRAPH_ENABLED": "1"}) is True
    assert graph_env_enabled({"LLM_BRAIN_GRAPH_ENABLED": "True"}) is True
    assert graph_env_enabled({"LLM_BRAIN_GRAPH_ENABLED": "0"}) is False
    assert graph_env_enabled({"LLM_BRAIN_GRAPH_ENABLED": "false"}) is False
    assert graph_env_enabled({"LLM_BRAIN_GRAPH_ENABLED": "False"}) is False
    assert graph_env_enabled({"LLM_BRAIN_GRAPH_ENABLED": ""}) is False
    assert graph_env_enabled({}) is False


def test_graph_env_enabled_respects_explicit_empty_environment(monkeypatch):
    monkeypatch.setenv("LLM_BRAIN_GRAPH_ENABLED", "true")

    assert graph_env_enabled({}) is False
    assert isinstance(build_graph_adapter_from_env({}, required=False), NullGraphMemoryAdapter)


def test_metadata_first_hybrid_env_enabled_reads_explicit_switch():
    assert metadata_first_hybrid_enabled({"LLM_BRAIN_GRAPH_METADATA_FIRST_HYBRID": "true"}) is True
    assert metadata_first_hybrid_enabled({"LLM_BRAIN_GRAPH_METADATA_FIRST_HYBRID": "1"}) is True
    assert metadata_first_hybrid_enabled({"LLM_BRAIN_GRAPH_METADATA_FIRST_HYBRID": "on"}) is True
    assert metadata_first_hybrid_enabled({"LLM_BRAIN_GRAPH_METADATA_FIRST_HYBRID": "false"}) is False
    assert metadata_first_hybrid_enabled({"LLM_BRAIN_GRAPH_METADATA_FIRST_HYBRID": "0"}) is False
    assert metadata_first_hybrid_enabled({}) is False


def test_graph_builder_wraps_metadata_first_hybrid_and_disables_entity_extraction(monkeypatch):
    graph = FakeGraphMemoryAdapter()
    captured_env: list[dict[str, str]] = []

    def _fake_from_env(env):
        captured_env.append(dict(env))
        return graph

    monkeypatch.setattr(
        "agent_knowledge.llm_brain_core.runtime_graph.GraphitiNeo4jGraphMemoryAdapter.from_env",
        staticmethod(_fake_from_env),
    )

    adapter = build_graph_adapter_from_env(
        {
            "LLM_BRAIN_GRAPH_METADATA_FIRST_HYBRID": "true",
            "LLM_BRAIN_GRAPH_EXTRACT_ENTITIES": "true",
            "LLM_BRAIN_GRAPH_FORCE_REEXTRACT_ENTITIES": "true",
        },
        enable_flag=True,
    )
    episode = OntologyEpisode.from_payload(
        event_id="evt_hybrid_runtime",
        entity_type="Task",
        natural_id="task:hybrid-runtime",
        payload={
            "brain_id": "/project/neurons",
            "title": "Run expensive semantic extraction",
            "typed_payload": {"next_action": "Use metadata-first graph"},
        },
        source_event_ids=["evt_hybrid_runtime"],
        source_ref_ids=["src_hybrid_runtime"],
        observed_at="2026-06-22T00:00:00+00:00",
    )

    assert adapter.upsert_episode(episode) == "inserted"
    stored = graph.get_episodes_by_ids([episode.episode_id], brain_id="/project/neurons")[0]
    assert stored.payload["metadata_first"] is True
    assert "typed_payload" not in stored.payload
    assert captured_env[0]["LLM_BRAIN_GRAPH_EXTRACT_ENTITIES"] == "false"
    assert "LLM_BRAIN_GRAPH_FORCE_REEXTRACT_ENTITIES" not in captured_env[0]


def test_graph_adapter_required_but_disabled_fails_before_backend_initialization():
    with pytest.raises(ValueError, match="graph is required but not enabled"):
        build_graph_adapter_from_env({}, enabled=False, required=True)


def test_graph_adapter_disabled_returns_null_adapter_when_not_required():
    adapter = build_graph_adapter_from_env({}, enabled=False, required=False)

    assert isinstance(adapter, NullGraphMemoryAdapter)


def test_graph_required_runs_connectivity_probe_and_propagates_failure(monkeypatch):
    # required=must-have: a one-shot probe runs and a failing probe fails fast.
    calls: list[object] = []
    built: list[object] = []

    def _failing_probe(adapter):
        calls.append(adapter)
        raise RuntimeError("neo4j unreachable")

    def _fake_from_env(env):
        adapter = object()
        built.append(adapter)
        return adapter

    monkeypatch.setattr(
        "agent_knowledge.llm_brain_core.runtime_graph.GraphitiNeo4jGraphMemoryAdapter.from_env",
        staticmethod(_fake_from_env),
    )

    with pytest.raises(RuntimeError, match="neo4j unreachable"):
        build_graph_adapter_from_env(
            {},
            enable_flag=True,
            required_flag=True,
            probe=_failing_probe,
        )

    assert built, "backend must be built before the probe runs"
    assert calls == built, "probe must run exactly once against the built adapter"


def test_graph_best_effort_does_not_run_connectivity_probe(monkeypatch):
    # enable=best-effort: no probe, and an init failure degrades instead of raising.
    probe_calls: list[object] = []

    def _spy_probe(adapter):
        probe_calls.append(adapter)

    def _raising_from_env(env):
        raise RuntimeError("backend init failed")

    monkeypatch.setattr(
        "agent_knowledge.llm_brain_core.runtime_graph.GraphitiNeo4jGraphMemoryAdapter.from_env",
        staticmethod(_raising_from_env),
    )

    adapter = build_graph_adapter_from_env(
        {},
        enable_flag=True,
        required_flag=False,
        probe=_spy_probe,
    )

    assert isinstance(adapter, UnavailableGraphMemoryAdapter)
    assert probe_calls == [], "best-effort must not run the connectivity probe"


def test_probe_graphiti_connectivity_raises_when_driver_cannot_verify():
    from agent_knowledge.llm_brain_core.graphiti_adapter import probe_graphiti_connectivity

    class _DeadDriver:
        def verify_connectivity(self):
            raise RuntimeError("connection refused")

    adapter = SimpleNamespace(_graphiti=SimpleNamespace(driver=_DeadDriver()))

    with pytest.raises(RuntimeError, match="connection refused"):
        probe_graphiti_connectivity(adapter)


def test_probe_graphiti_connectivity_ok_when_driver_verifies():
    from agent_knowledge.llm_brain_core.graphiti_adapter import probe_graphiti_connectivity

    verified: list[bool] = []

    class _LiveDriver:
        def verify_connectivity(self):
            verified.append(True)

    adapter = SimpleNamespace(_graphiti=SimpleNamespace(driver=_LiveDriver()))

    assert probe_graphiti_connectivity(adapter) is None
    assert verified == [True]
