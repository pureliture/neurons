from agent_knowledge.llm_brain_core import BrainEventReplayStore
from agent_knowledge.llm_brain_core.models import BrainEventEnvelope


def test_event_replay_orders_idempotently_and_quarantines_unresolved_current_after_tombstone():
    store = BrainEventReplayStore()
    tombstone = _event(
        "evt_004",
        "brain_event:task:delete",
        "2026-06-18T00:03:00Z",
        {"target_id": "task:ctx", "reason": "superseded"},
        tombstone=True,
    )
    events = [
        _event(
            "evt_002",
            "brain_event:task:update",
            "2026-06-18T00:02:00Z",
            {"target_id": "task:ctx", "task": "newer context task"},
        ),
        _event(
            "evt_001_dup",
            "brain_event:task:create",
            "2026-06-18T00:04:00Z",
            {"target_id": "task:ctx", "task": "initial context task"},
        ),
        _event(
            "evt_001",
            "brain_event:task:create",
            "2026-06-18T00:01:00Z",
            {"target_id": "task:ctx", "task": "initial context task"},
        ),
        tombstone,
        _event(
            "evt_005",
            "brain_event:task:bad-resurrect",
            "2026-06-18T00:05:00Z",
            {"target_id": "task:ctx", "task": "unresolved resurrection"},
        ),
        _event(
            "evt_006",
            "brain_event:task:resolved-resurrect",
            "2026-06-18T00:06:00Z",
            {"target_id": "task:ctx", "task": "resolved resurrection", "supersedes": ["evt_004"]},
        ),
    ]

    result = store.apply(events).to_dict()

    assert result["applied"] == ["evt_001", "evt_002", "evt_004", "evt_006"]
    assert result["duplicates"] == ["evt_001_dup"]
    assert result["tombstones"] == ["evt_004"]
    assert result["quarantined"] == [
        {
            "event_id": "evt_005",
            "idempotency_key": "brain_event:task:bad-resurrect",
            "target_id": "task:ctx",
            "reason_code": "current_delta_after_tombstone",
            "payload_hash": _h_payload({"target_id": "task:ctx", "task": "unresolved resurrection"}),
        }
    ]
    assert result["current_payloads"] == [
        {"target_id": "task:ctx", "task": "resolved resurrection", "supersedes": ["evt_004"]}
    ]


def test_event_replay_quarantines_same_idempotency_key_with_different_payload():
    store = BrainEventReplayStore()

    result = store.apply(
        [
            _event("evt_a", "brain_event:decision:1", "2026-06-18T00:00:00Z", {"target_id": "decision:1", "value": "A"}),
            _event("evt_b", "brain_event:decision:1", "2026-06-18T00:01:00Z", {"target_id": "decision:1", "value": "B"}),
        ]
    ).to_dict()

    assert result["applied"] == ["evt_a"]
    assert result["quarantined"][0]["reason_code"] == "idempotency_conflict"


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


def _h(value):
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _h_payload(payload):
    import hashlib
    import json

    return "sha256:" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
