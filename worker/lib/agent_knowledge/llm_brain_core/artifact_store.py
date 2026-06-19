from __future__ import annotations

from collections.abc import Iterable
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

    def list_recent(self, *, project: str, limit: int = 10) -> list[SessionMemoryArtifact]: ...


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

    def list_recent(self, *, project: str, limit: int = 10) -> list[SessionMemoryArtifact]:
        bounded = max(1, min(int(limit), 100))
        return sorted(
            [artifact for artifact in self._artifacts.values() if artifact.project == project],
            key=lambda artifact: (artifact.created_at, artifact.artifact_id),
            reverse=True,
        )[:bounded]


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
