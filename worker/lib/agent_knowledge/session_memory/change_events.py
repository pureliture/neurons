"""session-memory change events on NATS JetStream.

The ingress lane publishes one ``session_memory.changed`` event after each
authoritative CouchDB write (best-effort, never on the ingress ack path); the
session-memory ``RUN_MODE=watch`` consumer turns those events into build cycles.
Delivery retry and dead-lettering stay with JetStream (``max_deliver`` + nak
backoff + the ``session_memory.dead_letter`` subject), mirroring the ingress
``rag.ingress.>`` lane instead of a bespoke retry log.

Everything here is redaction-safe: events carry hashed identifiers only.
"""

from __future__ import annotations

import collections
import datetime
import json
import re
import threading
from dataclasses import asdict, dataclass, replace

SESSION_MEMORY_EVENTS_STREAM = "SESSION_MEMORY_EVENTS"
SESSION_MEMORY_EVENTS_SUBJECTS = ("session_memory.>",)
SESSION_MEMORY_CHANGED_SUBJECT = "session_memory.changed"
SESSION_MEMORY_DEAD_LETTER_SUBJECT = "session_memory.dead_letter"
SESSION_MEMORY_PROJECTOR_DURABLE = "session_memory_projector"
SESSION_CHANGED_EVENT_SCHEMA = "session_memory_changed.v1"
DEAD_LETTER_EVENT_SCHEMA = "session_memory_dead_letter.v1"
DEFAULT_STREAM_MAX_AGE_SECONDS = 7 * 24 * 3600
DEFAULT_DUPLICATE_WINDOW_SECONDS = 120

PUBLISH_ENABLED_ENV = "SESSION_MEMORY_EVENTS_PUBLISH"
NATS_URL_ENV = "RAG_INGRESS_NATS_URL"
DEFAULT_NATS_URL = "nats://127.0.0.1:4222"

# Canonical session_id_hash is ``sha256:<64 lowercase hex>``. A bare 64-hex digest
# (any case) is normalised to that form; every other shape is rejected.
_HASH_RE = re.compile(r"^(sha256:)?([0-9a-fA-F]{64})$")


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def normalize_session_id_hash(value: object) -> str | None:
    """Return the canonical ``sha256:<lowercase hex>`` form, or None when malformed."""
    match = _HASH_RE.fullmatch(str(value or "").strip())
    if match is None:
        return None
    return f"sha256:{match.group(2).lower()}"


def is_session_id_hash(value: object) -> bool:
    return normalize_session_id_hash(value) is not None


@dataclass(frozen=True)
class SessionChangedEvent:
    session_id_hash: str
    provider: str = ""
    project: str = ""
    payload_hash: str = ""
    changed_at: str = ""
    schema_version: str = SESSION_CHANGED_EVENT_SCHEMA

    def to_bytes(self) -> bytes:
        payload = asdict(self)
        if not payload["changed_at"]:
            payload["changed_at"] = _utc_now_iso()
        return json.dumps(payload, sort_keys=True).encode("utf-8")

    def msg_id(self) -> str:
        """JetStream dedup id: one id per delivery, not per session.

        Deduplicating per session inside the stream window would drop a change
        that arrives after the consumer already processed the earlier one, so
        only exact re-publishes of the same authoritative write are collapsed.
        """
        digest = self.session_id_hash.split(":", 1)[-1]
        return f"sm:{digest[:16]}:{(self.payload_hash or '-')[:16]}"


def parse_session_changed_event(data: bytes) -> SessionChangedEvent | None:
    """Return the event or None when the payload is not a well-formed event."""
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    session_id_hash = normalize_session_id_hash(payload.get("session_id_hash"))
    if session_id_hash is None:
        return None
    return SessionChangedEvent(
        session_id_hash=session_id_hash,
        provider=str(payload.get("provider") or ""),
        project=str(payload.get("project") or ""),
        payload_hash=str(payload.get("payload_hash") or ""),
        changed_at=str(payload.get("changed_at") or ""),
        schema_version=str(payload.get("schema_version") or SESSION_CHANGED_EVENT_SCHEMA),
    )


def build_dead_letter_payload(
    event: SessionChangedEvent | None,
    *,
    attempts: int,
    error_class: str,
    raw_len: int = 0,
) -> bytes:
    body = {
        "schema_version": DEAD_LETTER_EVENT_SCHEMA,
        "dead_lettered_at": _utc_now_iso(),
        "attempts": int(attempts),
        "error_class": str(error_class or ""),
        "event": None if event is None else asdict(event),
        "raw_len": int(raw_len),
    }
    return json.dumps(body, sort_keys=True).encode("utf-8")


class SessionChangeCollector:
    """Thread-safe hand-off from ``CouchDBDeliveryBackend.submit`` (runs in a
    worker thread) to the asyncio loop that owns the NATS connection."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: collections.deque[SessionChangedEvent] = collections.deque()

    def __call__(self, event: SessionChangedEvent) -> None:
        normalized = normalize_session_id_hash(event.session_id_hash)
        if normalized is None:
            return
        if normalized != event.session_id_hash:
            event = replace(event, session_id_hash=normalized)
        with self._lock:
            self._events.append(event)

    def drain(self) -> list[SessionChangedEvent]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


def publish_enabled(environ) -> bool:
    return str(environ.get(PUBLISH_ENABLED_ENV) or "").strip() == "1"


def nats_url_from_env(environ) -> str:
    return str(environ.get(NATS_URL_ENV) or DEFAULT_NATS_URL)


async def ensure_session_memory_stream(js, *, stream: str = SESSION_MEMORY_EVENTS_STREAM):
    """Idempotent: create the LIMITS/file stream when absent, leave it alone otherwise.

    Python owns this lane end to end (publisher and consumer), so provisioning
    lives here rather than in the Java ``NatsJetStreamProvisioner`` that owns
    only the ``rag.ingress.>`` WorkQueue.
    """
    try:
        return await js.stream_info(stream)
    except Exception:
        pass
    from nats.js import api

    config = api.StreamConfig(
        name=stream,
        subjects=list(SESSION_MEMORY_EVENTS_SUBJECTS),
        retention=api.RetentionPolicy.LIMITS,
        storage=api.StorageType.FILE,
        max_age=float(DEFAULT_STREAM_MAX_AGE_SECONDS),
        duplicate_window=float(DEFAULT_DUPLICATE_WINDOW_SECONDS),
    )
    return await js.add_stream(config)


async def publish_session_changes(
    js,
    events,
    *,
    subject: str = SESSION_MEMORY_CHANGED_SUBJECT,
    stream: str = SESSION_MEMORY_EVENTS_STREAM,
) -> int:
    """Publish events; raises on the first transport failure so the caller can log."""
    published = 0
    for event in events:
        await js.publish(
            subject,
            event.to_bytes(),
            stream=stream,
            headers={"Nats-Msg-Id": event.msg_id()},
        )
        published += 1
    return published


async def publish_collected_best_effort(js, collector: SessionChangeCollector, *, log) -> int:
    """Drain the collector and publish. Never raises: ingress success is already
    authoritative, so a publish failure is logged and left to the idle sweep."""
    events = collector.drain()
    if not events:
        return 0
    try:
        return await publish_session_changes(js, events)
    except Exception as exc:  # noqa: BLE001 - best-effort by contract
        log(f"session_change_publish_failed count={len(events)} error={type(exc).__name__}")
        return 0


async def publish_events_standalone(events, *, nats_url: str, log) -> int:
    """One-shot connect/publish/drain for synchronous callers (drain-deliveries)."""
    if not events:
        return 0
    import nats

    nc = await nats.connect(nats_url)
    try:
        js = nc.jetstream()
        await ensure_session_memory_stream(js)
        return await publish_session_changes(js, events)
    except Exception as exc:  # noqa: BLE001
        log(f"session_change_publish_failed count={len(events)} error={type(exc).__name__}")
        return 0
    finally:
        try:
            await nc.drain()
        except Exception:
            pass


__all__ = [
    "DEAD_LETTER_EVENT_SCHEMA",
    "DEFAULT_NATS_URL",
    "NATS_URL_ENV",
    "PUBLISH_ENABLED_ENV",
    "SESSION_CHANGED_EVENT_SCHEMA",
    "SESSION_MEMORY_CHANGED_SUBJECT",
    "SESSION_MEMORY_DEAD_LETTER_SUBJECT",
    "SESSION_MEMORY_EVENTS_STREAM",
    "SESSION_MEMORY_EVENTS_SUBJECTS",
    "SESSION_MEMORY_PROJECTOR_DURABLE",
    "SessionChangeCollector",
    "SessionChangedEvent",
    "build_dead_letter_payload",
    "ensure_session_memory_stream",
    "is_session_id_hash",
    "nats_url_from_env",
    "parse_session_changed_event",
    "publish_collected_best_effort",
    "publish_enabled",
    "publish_events_standalone",
    "publish_session_changes",
]
