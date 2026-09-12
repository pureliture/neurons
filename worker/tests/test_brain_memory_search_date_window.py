"""Fix 2: brain_memory_search date_from/date_to 창 필터 검증 (spec Fix 2 완료 조건)."""

from agent_knowledge.llm_brain_core.context import BrainReadService
from agent_knowledge.llm_brain_core.graph import FakeGraphMemoryAdapter
from agent_knowledge.llm_brain_core.ontology import OntologyEpisode

_NEW = "2026-09-11T10:00:00Z"
_OLD = "2026-06-24T10:00:00Z"


def _episode(event_id: str, observed_at: str):
    return OntologyEpisode.from_payload(
        event_id=event_id,
        entity_type="GraphFact",
        natural_id=event_id,
        payload={"brain_id": "/project/neurons", "fact": f"fact {event_id}", "project": "neurons"},
        observed_at=observed_at,
    )


def _service(*episodes):
    return BrainReadService(graph_adapter=FakeGraphMemoryAdapter(list(episodes)))


def test_date_window_excludes_out_of_range_episodes():
    new_ep, old_ep = _episode("fact-new-001", _NEW), _episode("fact-old-001", _OLD)
    service = _service(old_ep, new_ep)

    result = service.brain_memory_search(
        project="neurons",
        query="fact",
        limit=10,
        date_from="2026-09-01T00:00:00Z",
    )
    ids = {item["event_id"] for item in result["graph_results"]}
    assert ids == {"fact-new-001"}


def test_no_window_returns_all_as_before():
    new_ep, old_ep = _episode("fact-new-001", _NEW), _episode("fact-old-001", _OLD)
    service = _service(old_ep, new_ep)

    result = service.brain_memory_search(project="neurons", query="fact", limit=10)
    ids = {item["event_id"] for item in result["graph_results"]}
    assert ids == {"fact-new-001", "fact-old-001"}


def test_date_to_only_window_includes_up_to_bound():
    ep = _episode("fact-a-001", _NEW)
    service = _service(ep)

    result = service.brain_memory_search(
        project="neurons",
        query="fact",
        limit=10,
        date_to="2026-09-01T00:00:00Z",
    )
    assert result["graph_results"] == []
