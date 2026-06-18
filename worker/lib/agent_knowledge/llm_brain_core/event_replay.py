from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .models import BrainEventEnvelope


@dataclass(frozen=True)
class ReplayResult:
    applied: tuple[str, ...] = ()
    duplicates: tuple[str, ...] = ()
    tombstones: tuple[str, ...] = ()
    quarantined: tuple[dict[str, Any], ...] = ()
    current_payloads: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("applied", "duplicates", "tombstones", "quarantined", "current_payloads"):
            data[key] = list(data[key])
        return data


@dataclass
class _ReplayState:
    seen: dict[str, BrainEventEnvelope] = field(default_factory=dict)
    current: dict[str, BrainEventEnvelope] = field(default_factory=dict)
    tombstoned: dict[str, BrainEventEnvelope] = field(default_factory=dict)
    quarantined: list[dict[str, Any]] = field(default_factory=list)


class BrainEventReplayStore:
    """Idempotent local replay state for per-PC and central shadow rebuilds."""

    def __init__(self) -> None:
        self._state = _ReplayState()

    def apply(self, events: list[BrainEventEnvelope]) -> ReplayResult:
        applied: list[str] = []
        duplicates: list[str] = []
        tombstones: list[str] = []
        quarantined: list[dict[str, Any]] = []
        for event in sorted(events, key=lambda item: (item.occurred_at, item.event_id)):
            prior = self._state.seen.get(event.idempotency_key)
            if prior is not None:
                if prior.payload_hash == event.payload_hash and prior.tombstone == event.tombstone:
                    duplicates.append(event.event_id)
                    continue
                quarantine = _quarantine(event, "idempotency_conflict")
                self._state.quarantined.append(quarantine)
                quarantined.append(quarantine)
                continue

            target_id = event.target_id()
            previous_tombstone = self._state.tombstoned.get(target_id)
            if previous_tombstone is not None and not event.tombstone and not _resolves_tombstone(event, previous_tombstone):
                quarantine = _quarantine(event, "current_delta_after_tombstone")
                self._state.quarantined.append(quarantine)
                quarantined.append(quarantine)
                self._state.seen[event.idempotency_key] = event
                continue

            self._state.seen[event.idempotency_key] = event
            if event.tombstone:
                self._state.current.pop(target_id, None)
                self._state.tombstoned[target_id] = event
                tombstones.append(event.event_id)
            else:
                self._state.current[target_id] = event
            applied.append(event.event_id)

        return ReplayResult(
            applied=tuple(applied),
            duplicates=tuple(duplicates),
            tombstones=tuple(tombstones),
            quarantined=tuple(quarantined),
            current_payloads=tuple(event.payload for event in self._state.current.values()),
        )

    def current_payloads(self) -> list[dict[str, Any]]:
        return [dict(event.payload) for event in self._state.current.values()]

    def quarantined(self) -> list[dict[str, Any]]:
        return list(self._state.quarantined)


def _resolves_tombstone(event: BrainEventEnvelope, tombstone: BrainEventEnvelope) -> bool:
    supersedes = event.payload.get("supersedes")
    if isinstance(supersedes, str):
        supersedes = [supersedes]
    if not isinstance(supersedes, list):
        return False
    return tombstone.event_id in supersedes or tombstone.idempotency_key in supersedes


def _quarantine(event: BrainEventEnvelope, reason_code: str) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "idempotency_key": event.idempotency_key,
        "target_id": event.target_id(),
        "reason_code": reason_code,
        "payload_hash": event.payload_hash,
    }
