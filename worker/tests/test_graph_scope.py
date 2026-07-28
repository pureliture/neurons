from __future__ import annotations

from agent_knowledge.llm_brain_core.graph import FakeGraphMemoryAdapter
from agent_knowledge.llm_brain_core.graph_scope import (
    episode_matches_graph_group,
    graph_group_id,
    graph_group_id_for_episode,
    graph_scope_for_episode,
)
from agent_knowledge.llm_brain_core.models import OntologyEpisode


def _episode(*, brain_id: str | None) -> OntologyEpisode:
    payload = {"task": "shared graph scope"}
    if brain_id is not None:
        payload["brain_id"] = brain_id
    return OntologyEpisode.from_payload(
        event_id="evt:graph-scope",
        entity_type="Task",
        natural_id="task:graph-scope",
        payload=payload,
    )


def test_graph_scope_hashes_unsafe_project_paths_without_retaining_the_scope():
    group_id = graph_group_id("/project/neurons")

    assert group_id.startswith("brain_")
    assert "/" not in group_id
    assert group_id != "/project/neurons"


def test_graph_scope_hashes_overlength_scope_deterministically():
    scope = "project-" + ("scope" * 100)

    first = graph_group_id(scope)
    second = graph_group_id(scope)

    assert first == second
    assert first.startswith("brain_")
    assert len(first) < len(scope)


def test_episode_brain_id_takes_precedence_and_default_scope_is_only_a_fallback():
    scoped = _episode(brain_id="/project/authoritative")
    legacy = _episode(brain_id=None)

    assert graph_scope_for_episode(scoped, "/project/default") == "/project/authoritative"
    assert graph_group_id_for_episode(scoped, "/project/default") == graph_group_id(
        "/project/authoritative"
    )
    assert graph_group_id_for_episode(legacy, "/project/default") == graph_group_id(
        "/project/default"
    )


def test_fake_adapter_uses_the_shared_codec_and_rejects_mismatched_provenance():
    episode = _episode(brain_id="/project/authoritative")
    expected_group_id = graph_group_id_for_episode(episode, "/project/default")
    adapter = FakeGraphMemoryAdapter(default_group_id="/project/default")

    assert adapter.upsert_episode(episode) == "inserted"
    assert episode_matches_graph_group(
        episode,
        expected_group_id=expected_group_id,
        default_scope="/project/default",
    )
    assert not episode_matches_graph_group(
        episode,
        expected_group_id=graph_group_id("/project/other"),
        default_scope="/project/default",
    )
    assert adapter.search_context(brain_id="/project/authoritative", query="shared").episodes == (
        episode,
    )
    assert adapter.search_context(brain_id="/project/other", query="shared").episodes == ()
