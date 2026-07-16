from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any, Protocol

from ._util import ensure_public_safe
from .models import SessionMemoryArtifact


_EXTERNAL_INDEX_OBJECTS = ("dataset", "document")
FORBIDDEN_EXTERNAL_INDEX_KEYS = {
    f"{name}_id" for name in _EXTERNAL_INDEX_OBJECTS
} | {
    f"{name}_id" + "s" for name in _EXTERNAL_INDEX_OBJECTS
}


class SessionMemoryArtifactStore(Protocol):
    def upsert(self, artifact: SessionMemoryArtifact) -> str: ...

    def get(self, artifact_id: str) -> SessionMemoryArtifact | None: ...

    def get_latest_for_session(
        self, *, project: str, session_id_hash: str
    ) -> SessionMemoryArtifact | None: ...

    def list_recent(self, *, project: str, limit: int = 10) -> list[SessionMemoryArtifact]: ...

    def list_observed_interval(
        self,
        *,
        project: str,
        observed_at_start: str,
        observed_at_end: str,
        limit: int = 100,
    ) -> list[SessionMemoryArtifact]: ...

    def list_observed_interval_revisions(
        self,
        *,
        project: str,
        observed_at_start: str,
        observed_at_end: str,
        limit: int = 1000,
    ) -> list[SessionMemoryArtifact]: ...


class InMemorySessionMemoryArtifactStore:
    """Deterministic artifact store used by the first local milestones."""

    def __init__(self, artifacts: Iterable[SessionMemoryArtifact] | None = None) -> None:
        self._artifacts: dict[str, SessionMemoryArtifact] = {}
        if artifacts:
            for artifact in artifacts:
                self.upsert(artifact)

    def upsert(self, artifact: SessionMemoryArtifact) -> str:
        _reject_external_index_fields(artifact.to_dict())
        existing = self._artifacts.get(artifact.artifact_id)
        if existing is not None:
            if existing.content_hash != artifact.content_hash:
                raise ValueError("artifact id collision with different content_hash")
            return "duplicate"
        self._artifacts[artifact.artifact_id] = artifact
        return "inserted"

    def get(self, artifact_id: str) -> SessionMemoryArtifact | None:
        return self._artifacts.get(artifact_id)

    def get_latest_for_session(
        self, *, project: str, session_id_hash: str
    ) -> SessionMemoryArtifact | None:
        matching = [
            artifact
            for artifact in self._artifacts.values()
            if artifact.project == project
            and artifact.session_id_hash == session_id_hash
        ]
        return max(matching, key=_artifact_currentness_key, default=None)

    def list_recent(self, *, project: str, limit: int = 10) -> list[SessionMemoryArtifact]:
        bounded = max(1, min(int(limit), 100))
        return sorted(
            _latest_artifacts_by_session(
                artifact
                for artifact in self._artifacts.values()
                if artifact.project == project
            ),
            key=_artifact_currentness_key,
            reverse=True,
        )[:bounded]

    def list_observed_interval(
        self,
        *,
        project: str,
        observed_at_start: str,
        observed_at_end: str,
        limit: int = 100,
    ) -> list[SessionMemoryArtifact]:
        bounded = max(1, min(int(limit), 1000))
        _validate_observed_interval(observed_at_start, observed_at_end)
        matching = [
            artifact
            for artifact in self._artifacts.values()
            if artifact.project == project
            and _artifact_overlaps_observed_interval(
                artifact,
                observed_at_start=observed_at_start,
                observed_at_end=observed_at_end,
            )
        ]
        latest = _latest_artifacts_by_session(
            artifact
            for artifact in matching
        )
        return sorted(latest, key=_artifact_currentness_key, reverse=True)[:bounded]

    def list_observed_interval_revisions(
        self,
        *,
        project: str,
        observed_at_start: str,
        observed_at_end: str,
        limit: int = 1000,
    ) -> list[SessionMemoryArtifact]:
        """Return every bounded revision matching an event-time interval.

        Temporal relevance must be evaluated before collapsing revisions of one
        session.  ``list_observed_interval`` keeps its compatibility contract for
        callers that explicitly want only the latest revision per session.
        """

        bounded = max(1, min(int(limit), 10000))
        _validate_observed_interval(observed_at_start, observed_at_end)
        matching = [
            artifact
            for artifact in self._artifacts.values()
            if artifact.project == project
            and _artifact_overlaps_observed_interval(
                artifact,
                observed_at_start=observed_at_start,
                observed_at_end=observed_at_end,
            )
        ]
        return sorted(matching, key=_artifact_currentness_key, reverse=True)[:bounded]


def _artifact_currentness_key(artifact: SessionMemoryArtifact) -> tuple[int, str, str, str, str]:
    return (
        int(artifact.materialization_revision),
        str(artifact.materialized_at or ""),
        str(artifact.source_revision or ""),
        str(artifact.created_at or ""),
        str(artifact.artifact_id or ""),
    )


def _latest_artifacts_by_session(
    artifacts: Iterable[SessionMemoryArtifact],
) -> list[SessionMemoryArtifact]:
    latest: dict[str, SessionMemoryArtifact] = {}
    for artifact in artifacts:
        existing = latest.get(artifact.session_id_hash)
        if existing is None or _artifact_currentness_key(artifact) > _artifact_currentness_key(
            existing
        ):
            latest[artifact.session_id_hash] = artifact
    return list(latest.values())


def _artifact_overlaps_observed_interval(
    artifact: SessionMemoryArtifact,
    *,
    observed_at_start: str,
    observed_at_end: str,
) -> bool:
    query_start, query_end = _validate_observed_interval(
        observed_at_start, observed_at_end
    )
    # Temporal recall is revision-time authoritative. Legacy artifacts only carry
    # cumulative session bounds, which can make every historical selector match
    # the same snapshot. They remain readable through non-temporal lanes, but are
    # ineligible here until a bounded metadata rebuild has supplied revision
    # intervals. This is deliberately fail-closed rather than a latest fallback.
    if artifact.revision_temporal_evidence != "bounded":
        return False
    if artifact.revision_observed_intervals:
        for raw_start, raw_end in artifact.revision_observed_intervals:
            interval_start = _parse_observed_time_or_none(raw_start)
            interval_end = _parse_observed_time_or_none(raw_end)
            if (
                interval_start is not None
                and interval_end is not None
                and interval_start <= interval_end
                and interval_start <= query_end
                and interval_end >= query_start
            ):
                return True
        return False
    artifact_start = _parse_observed_time_or_none(
        artifact.revision_observed_at_start
    )
    artifact_end = _parse_observed_time_or_none(
        artifact.revision_observed_at_end
    )
    if artifact_start is None or artifact_end is None or artifact_start > artifact_end:
        return False
    return artifact_start <= query_end and artifact_end >= query_start


def _validate_observed_interval(
    observed_at_start: str, observed_at_end: str
) -> tuple[datetime, datetime]:
    query_start = _parse_observed_time(observed_at_start, field="observed_at_start")
    query_end = _parse_observed_time(observed_at_end, field="observed_at_end")
    if query_start > query_end:
        raise ValueError("observed interval start must not be after end")
    return query_start, query_end


def _parse_observed_time(value: str, *, field: str) -> datetime:
    parsed = _parse_observed_time_or_none(value)
    if parsed is None:
        raise ValueError(f"{field} must be an ISO-8601 timestamp")
    return parsed


def _parse_observed_time_or_none(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _reject_external_index_fields(value: Any, path: str = "artifact") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if key_text.lower() in FORBIDDEN_EXTERNAL_INDEX_KEYS:
                raise ValueError(f"{path}.{key_text} is not allowed in core artifacts")
            _reject_external_index_fields(child, f"{path}.{key_text}")
        ensure_public_safe(value, path)
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_external_index_fields(child, f"{path}[{index}]")
