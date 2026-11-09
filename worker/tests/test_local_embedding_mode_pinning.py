from __future__ import annotations

import asyncio
from types import SimpleNamespace

import agent_knowledge.llm_brain_core.graphiti_adapter as graphiti_adapter
from agent_knowledge.llm_brain_core.graphiti_adapter import (
    LOCAL_EMBEDDING_PROFILE_ID,
    _safe_delete_graph_element,
    _stamp_local_embedding_profile,
)
from graphiti_core.local_profile import LOCAL_GRAPH_VECTOR_DIM, is_local_embedding_mode


LOCAL_ENV = {"NEURON_EMBEDDING_PROFILE": "lbrain-memory-model2vec-256-v1"}


def test_unpinned_objects_still_fall_back_to_env():
    driver = SimpleNamespace()
    assert is_local_embedding_mode(driver, environ=LOCAL_ENV) is True
    assert is_local_embedding_mode(driver, environ={}) is False


def test_pinned_false_wins_over_local_process_env():
    # Two adapters in one process must not share a verdict through os.environ.
    driver = SimpleNamespace(local_embedding_mode=False)
    assert is_local_embedding_mode(driver, environ=LOCAL_ENV) is False


def test_pinned_true_wins_over_empty_env_and_nested_pin_is_honored():
    assert is_local_embedding_mode(SimpleNamespace(local_embedding_mode=True), environ={}) is True
    graphiti = SimpleNamespace(driver=SimpleNamespace(local_embedding_mode=True))
    assert is_local_embedding_mode(graphiti, environ={}) is True


def test_pinned_false_ignores_incidental_local_looking_config():
    embedder = SimpleNamespace(
        local_embedding_mode=False,
        config=SimpleNamespace(embedding_model="other", embedding_dim=LOCAL_GRAPH_VECTOR_DIM),
    )
    assert is_local_embedding_mode(embedder, environ=LOCAL_ENV) is False


def test_stamp_pins_verdict_on_graphiti_driver_and_embedder_for_both_modes():
    for is_local in (True, False):
        graphiti = SimpleNamespace(driver=SimpleNamespace(), embedder=SimpleNamespace())
        _stamp_local_embedding_profile(graphiti, is_local=is_local)
        assert graphiti.local_embedding_mode is is_local
        assert graphiti.driver.local_embedding_mode is is_local
        assert graphiti.embedder.local_embedding_mode is is_local
        assert is_local_embedding_mode(graphiti.driver, graphiti.embedder, environ=LOCAL_ENV) is is_local
        assert is_local_embedding_mode(graphiti.driver, graphiti.embedder, environ={}) is is_local


def test_stamp_false_removes_stale_local_profile_from_graphiti_driver_and_embedder():
    graphiti = SimpleNamespace(driver=SimpleNamespace(), embedder=SimpleNamespace())
    targets = (graphiti, graphiti.driver, graphiti.embedder)
    for target in targets:
        target.local_embedding_profile = LOCAL_EMBEDDING_PROFILE_ID

    _stamp_local_embedding_profile(graphiti, is_local=False)

    for target in targets:
        assert target.local_embedding_mode is False
        assert not hasattr(target, "local_embedding_profile")


def test_stamp_false_preserves_non_local_profile_values():
    graphiti = SimpleNamespace(driver=SimpleNamespace(), embedder=SimpleNamespace())
    targets = (graphiti, graphiti.driver, graphiti.embedder)
    for target in targets:
        target.local_embedding_profile = "other-profile"

    _stamp_local_embedding_profile(graphiti, is_local=False)

    assert [target.local_embedding_profile for target in targets] == ["other-profile"] * 3


def test_safe_delete_records_tombstones_only_for_local_embedding_mode(monkeypatch):
    recorded = []
    deleted = []

    async def record_tombstone(driver, **kwargs):
        recorded.append((driver, kwargs))

    class Element:
        uuid = "element-uuid"
        group_id = "element-group"

        async def delete(self, driver):
            deleted.append(driver)

    monkeypatch.setattr(
        graphiti_adapter,
        "record_embedding_deletion_tombstone",
        record_tombstone,
    )
    drivers = [
        SimpleNamespace(
            local_embedding_mode=False,
            local_embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
        ),
        SimpleNamespace(local_embedding_mode=True),
        SimpleNamespace(local_embedding_profile=LOCAL_EMBEDDING_PROFILE_ID),
    ]

    for driver in drivers:
        asyncio.run(_safe_delete_graph_element(Element(), driver))

    assert [driver for driver, _ in recorded] == drivers[1:]
    assert [details["target_uuid"] for _, details in recorded] == ["element-uuid"] * 2
    assert [details["group_id"] for _, details in recorded] == ["element-group"] * 2
    assert deleted == drivers
