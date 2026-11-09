import asyncio
import json

from agent_knowledge.session_memory import change_events as ce

DIGEST = "a" * 64
HASH = "sha256:" + DIGEST


def test_event_payload_is_hashed_identifiers_only_and_dedup_id_is_per_delivery():
    event = ce.SessionChangedEvent(session_id_hash=HASH, provider="claude", project="p", payload_hash="b" * 64)
    payload = json.loads(event.to_bytes())

    assert set(payload) == {"session_id_hash", "provider", "project", "payload_hash", "changed_at", "schema_version"}
    assert payload["schema_version"] == ce.SESSION_CHANGED_EVENT_SCHEMA
    assert event.msg_id() == f"sm:{DIGEST[:16]}:{'b' * 16}"
    other_delivery = ce.SessionChangedEvent(session_id_hash=HASH, payload_hash="c" * 64)
    assert other_delivery.msg_id() != event.msg_id()


def test_parse_rejects_malformed_payloads():
    assert ce.parse_session_changed_event(b"not json") is None
    assert ce.parse_session_changed_event(b"[]") is None
    assert ce.parse_session_changed_event(json.dumps({"session_id_hash": "raw-id"}).encode()) is None
    parsed = ce.parse_session_changed_event(json.dumps({"session_id_hash": HASH, "project": "p"}).encode())
    assert parsed is not None and parsed.session_id_hash == HASH and parsed.project == "p"


def test_collector_is_drain_once_and_ignores_non_hash_ids():
    collector = ce.SessionChangeCollector()
    collector(ce.SessionChangedEvent(session_id_hash="plain-session-id"))
    collector(ce.SessionChangedEvent(session_id_hash=HASH))
    assert len(collector) == 1
    assert [e.session_id_hash for e in collector.drain()] == [HASH]
    assert collector.drain() == []


def test_publish_enabled_is_opt_in():
    assert ce.publish_enabled({}) is False
    assert ce.publish_enabled({"SESSION_MEMORY_EVENTS_PUBLISH": "0"}) is False
    assert ce.publish_enabled({"SESSION_MEMORY_EVENTS_PUBLISH": "1"}) is True


class _FakeJetStream:
    def __init__(self, *, fail=False, stream_exists=True):
        self.published = []
        self.fail = fail
        self.stream_exists = stream_exists
        self.added = []

    async def publish(self, subject, payload, *, stream=None, headers=None):
        if self.fail:
            raise ConnectionError("broker down")
        self.published.append((subject, json.loads(payload), stream, headers))

    async def stream_info(self, stream):
        if not self.stream_exists:
            raise RuntimeError("not found")
        return {"name": stream}

    async def add_stream(self, config):
        self.added.append(config)
        self.stream_exists = True
        return config


def test_publish_collected_is_best_effort_and_never_raises():
    logs = []
    collector = ce.SessionChangeCollector()
    collector(ce.SessionChangedEvent(session_id_hash=HASH, payload_hash="d" * 64))
    js = _FakeJetStream(fail=True)

    published = asyncio.run(ce.publish_collected_best_effort(js, collector, log=logs.append))

    assert published == 0
    assert logs == ["session_change_publish_failed count=1 error=ConnectionError"]
    assert collector.drain() == []  # dropped by contract; the idle sweep compensates


def test_publish_collected_sends_subject_stream_and_msg_id():
    collector = ce.SessionChangeCollector()
    collector(ce.SessionChangedEvent(session_id_hash=HASH, payload_hash="d" * 64))
    js = _FakeJetStream()

    assert asyncio.run(ce.publish_collected_best_effort(js, collector, log=lambda _l: None)) == 1
    subject, payload, stream, headers = js.published[0]
    assert subject == ce.SESSION_MEMORY_CHANGED_SUBJECT
    assert stream == ce.SESSION_MEMORY_EVENTS_STREAM
    assert headers == {"Nats-Msg-Id": f"sm:{DIGEST[:16]}:{'d' * 16}"}
    assert payload["session_id_hash"] == HASH


def test_ensure_stream_creates_limits_file_stream_only_when_absent():
    js = _FakeJetStream(stream_exists=True)
    asyncio.run(ce.ensure_session_memory_stream(js))
    assert js.added == []

    js = _FakeJetStream(stream_exists=False)
    asyncio.run(ce.ensure_session_memory_stream(js))
    (config,) = js.added
    assert config.name == ce.SESSION_MEMORY_EVENTS_STREAM
    assert config.subjects == ["session_memory.>"]
    assert str(config.retention) == "limits" or config.retention.value == "limits"
    assert config.storage.value == "file"
    assert config.max_age == float(ce.DEFAULT_STREAM_MAX_AGE_SECONDS)


def test_dead_letter_payload_carries_attempts_and_error_class_only():
    event = ce.SessionChangedEvent(session_id_hash=HASH, provider="claude")
    body = json.loads(ce.build_dead_letter_payload(event, attempts=5, error_class="RuntimeError", raw_len=12))
    assert body["schema_version"] == ce.DEAD_LETTER_EVENT_SCHEMA
    assert body["attempts"] == 5 and body["error_class"] == "RuntimeError"
    assert body["event"]["session_id_hash"] == HASH
    body = json.loads(ce.build_dead_letter_payload(None, attempts=1, error_class="invalid_event", raw_len=3))
    assert body["event"] is None and body["raw_len"] == 3


def test_bare_64_hex_is_normalised_to_stored_sha256_form_and_other_shapes_rejected():
    upper = DIGEST.upper()
    assert ce.normalize_session_id_hash(upper) == HASH
    assert ce.normalize_session_id_hash(" " + HASH + " ") == HASH
    parsed = ce.parse_session_changed_event(json.dumps({"session_id_hash": upper}).encode())
    assert parsed is not None and parsed.session_id_hash == HASH

    collector = ce.SessionChangeCollector()
    collector(ce.SessionChangedEvent(session_id_hash=DIGEST, payload_hash="d" * 64))
    (event,) = collector.drain()
    assert event.session_id_hash == HASH and event.payload_hash == "d" * 64

    for bad in ("md5:" + DIGEST, DIGEST[:63], DIGEST + "0", "g" * 64, "sha256:" + DIGEST[:63], "SHA256:" + DIGEST, ""):
        assert ce.normalize_session_id_hash(bad) is None, bad
        assert ce.parse_session_changed_event(json.dumps({"session_id_hash": bad}).encode()) is None, bad
