from __future__ import annotations

from agent_knowledge.llm_brain_core import CentralBrainShadowRebuilder, FakeGraphMemoryAdapter
from agent_knowledge.llm_brain_core.models import BrainEventEnvelope


def test_central_shadow_rebuild_projects_only_replayed_current_payloads():
    graph = FakeGraphMemoryAdapter()
    rebuilder = CentralBrainShadowRebuilder(graph)
    tombstone = _event(
        "evt_004",
        "brain_event:task:delete",
        "2026-06-19T00:04:00Z",
        {"target_id": "task:sync", "reason": "superseded"},
        tombstone=True,
    )
    events = [
        _memory_card_event("evt_002", "brain_event:task:update", "2026-06-19T00:02:00Z", "Updated task"),
        _memory_card_event("evt_001_dup", "brain_event:task:create", "2026-06-19T00:03:00Z", "Initial task"),
        _memory_card_event("evt_001", "brain_event:task:create", "2026-06-19T00:01:00Z", "Initial task"),
        tombstone,
        _memory_card_event("evt_005", "brain_event:task:bad-resurrect", "2026-06-19T00:05:00Z", "Bad resurrect"),
        _memory_card_event(
            "evt_006",
            "brain_event:task:resolved-resurrect",
            "2026-06-19T00:06:00Z",
            "Resolved task",
            supersedes=["evt_004"],
        ),
    ]

    report = rebuilder.rebuild(events).to_dict()
    search = graph.search_context(
        brain_id="/project/neurons",
        query="Resolved task",
        entity_types=["Task"],
        limit=5,
    )

    assert report["status"] == "partial"
    assert report["replay"]["duplicates"] == ["evt_001_dup"]
    assert report["replay"]["tombstones"] == ["evt_004"]
    assert report["replay"]["quarantined"][0]["event_id"] == "evt_005"
    assert report["projection"]["projected"] == 1
    assert [episode.payload["summary"] for episode in search.episodes] == ["Resolved task"]


def test_central_shadow_rebuild_accepts_ontology_episode_payloads():
    graph = FakeGraphMemoryAdapter()
    rebuilder = CentralBrainShadowRebuilder(graph)
    episode = _episode_payload()

    report = rebuilder.rebuild(
        [
            _event(
                "evt_episode",
                "brain_event:episode:1",
                "2026-06-19T00:00:00Z",
                {"target_id": "episode:decision", "ontology_episode": episode},
            )
        ]
    ).to_dict()

    assert report["status"] == "succeeded"
    assert report["projection"]["projected"] == 1
    assert report["episode_ids"] == [episode["episode_id"]]


def _memory_card_event(event_id, key, occurred_at, summary, *, supersedes=None):
    payload = _memory_card(summary)
    if supersedes:
        payload["supersedes"] = list(supersedes)
    return _event(event_id, key, occurred_at, payload)


def _event(event_id, key, occurred_at, payload, tombstone=False):
    return BrainEventEnvelope.from_payload(
        event_id=event_id,
        idempotency_key=key,
        device_id_hash=_h("device-a"),
        event_type="memory_card_delta",
        occurred_at=occurred_at,
        observed_at=occurred_at,
        payload=payload,
        tombstone=tombstone,
    )


def _memory_card(summary):
    return {
        "target_id": "task:sync",
        "memory_id": "mem_sync_task",
        "brain_id": "/project/neurons",
        "card_type": "task",
        "scope": "project",
        "project": "neurons",
        "provider": "codex",
        "title": summary,
        "summary": summary,
        "render_text": summary,
        "lifecycle_state": "accepted",
        "approval_state": "approved",
        "currentness": "current",
        "confidence": 0.9,
        "source_refs": [{"source_ref_id": "src_sync_shadow", "content_hash": _h("source")}],
        "derived_from": ["evt_sync_shadow"],
        "typed_payload": {
            "task_state": summary,
            "next_action": "Project central shadow graph",
            "status": "open",
        },
    }


def _episode_payload():
    from agent_knowledge.llm_brain_core.models import OntologyEpisode

    return OntologyEpisode.from_payload(
        event_id="evt_episode_payload",
        entity_type="Decision",
        natural_id="decision:sync",
        payload={"decision": "Central sync rebuilds from events."},
        observed_at="2026-06-19T00:00:00+00:00",
        reference_time="2026-06-19T00:00:00+00:00",
    ).to_dict()


def _h(value):
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()
