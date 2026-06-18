from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ._util import (
    ensure_public_safe,
    hash_payload,
    public_safe_text,
    require_non_empty,
    require_opaque_id,
    require_sha256,
    short_hash,
    stable_json,
    utc_now_iso,
)


SyncPolicy = Literal["local_only", "metadata_only", "derived_only", "full_sync"]
ResolutionState = Literal[
    "metadata_only",
    "derived_only",
    "resolved",
    "same_device_required",
    "approval_required",
    "permission_revoked",
    "stale_hash",
    "deleted_source",
    "unresolved",
]


@dataclass(frozen=True)
class StatusBlock:
    status: str
    details: list[str] = field(default_factory=list)
    freshness: str = "unknown"
    authority: str = "diagnostic"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SessionMemoryArtifact:
    artifact_id: str
    session_id_hash: str
    project: str
    provider: str
    source_event_ids: tuple[str, ...]
    chunk_refs: tuple[str, ...]
    tool_evidence_refs: tuple[str, ...]
    summary: str
    content_hash: str
    ontology_version: str = "1.0.0"
    extractor_version: str = "0.1.0"
    created_at: str = ""

    def __post_init__(self) -> None:
        require_opaque_id(self.artifact_id, "artifact_id")
        require_sha256(self.session_id_hash, "session_id_hash")
        require_sha256(self.content_hash, "content_hash")
        object.__setattr__(self, "project", require_non_empty(self.project, "project"))
        object.__setattr__(self, "provider", require_non_empty(self.provider, "provider"))
        object.__setattr__(self, "summary", public_safe_text(self.summary, max_chars=2048))
        object.__setattr__(self, "source_event_ids", tuple(self.source_event_ids))
        object.__setattr__(self, "chunk_refs", tuple(self.chunk_refs))
        object.__setattr__(self, "tool_evidence_refs", tuple(self.tool_evidence_refs))
        object.__setattr__(self, "created_at", self.created_at or utc_now_iso())
        ensure_public_safe(self.to_dict(), "SessionMemoryArtifact")

    @classmethod
    def from_summary(
        cls,
        *,
        session_id_hash: str,
        project: str,
        provider: str,
        summary: str,
        source_event_ids: list[str] | tuple[str, ...],
        chunk_refs: list[str] | tuple[str, ...] = (),
        tool_evidence_refs: list[str] | tuple[str, ...] = (),
        ontology_version: str = "1.0.0",
        extractor_version: str = "0.1.0",
        created_at: str = "",
    ) -> "SessionMemoryArtifact":
        safe_summary = public_safe_text(summary, max_chars=2048)
        content_hash = hash_payload(
            {
                "session_id_hash": session_id_hash,
                "project": project,
                "provider": provider,
                "source_event_ids": list(source_event_ids),
                "summary": safe_summary,
                "ontology_version": ontology_version,
                "extractor_version": extractor_version,
            }
        )
        artifact_id = f"session-memory:{short_hash([session_id_hash, content_hash])}"
        return cls(
            artifact_id=artifact_id,
            session_id_hash=session_id_hash,
            project=project,
            provider=provider,
            source_event_ids=tuple(source_event_ids),
            chunk_refs=tuple(chunk_refs),
            tool_evidence_refs=tuple(tool_evidence_refs),
            summary=safe_summary,
            content_hash=content_hash,
            ontology_version=ontology_version,
            extractor_version=extractor_version,
            created_at=created_at,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source_event_ids"] = list(self.source_event_ids)
        data["chunk_refs"] = list(self.chunk_refs)
        data["tool_evidence_refs"] = list(self.tool_evidence_refs)
        return data


@dataclass(frozen=True)
class SourceRefRecord:
    source_ref_id: str
    device_id_hash: str
    root_id: str
    relative_path_hash: str
    content_hash: str
    mtime: str
    size: int
    sync_policy: SyncPolicy
    permission_scope: str = "project"
    last_seen_at: str = ""
    deleted_at: str = ""
    revoked_at: str = ""
    derived_summary: str = ""
    redacted_content: str = ""

    def __post_init__(self) -> None:
        require_opaque_id(self.source_ref_id, "source_ref_id")
        require_sha256(self.device_id_hash, "device_id_hash")
        require_sha256(self.relative_path_hash, "relative_path_hash")
        require_sha256(self.content_hash, "content_hash")
        if self.sync_policy not in ("local_only", "metadata_only", "derived_only", "full_sync"):
            raise ValueError("sync_policy is unsupported")
        if not isinstance(self.size, int) or self.size < 0:
            raise ValueError("size must be a non-negative integer")
        object.__setattr__(self, "root_id", require_opaque_id(self.root_id, "root_id"))
        object.__setattr__(self, "derived_summary", public_safe_text(self.derived_summary, max_chars=2048))
        object.__setattr__(self, "redacted_content", public_safe_text(self.redacted_content, max_chars=8192))
        ensure_public_safe(self.metadata(), "SourceRefRecord.metadata")

    def metadata(self) -> dict[str, Any]:
        return {
            "source_ref_id": self.source_ref_id,
            "device_id_hash": self.device_id_hash,
            "root_id": self.root_id,
            "relative_path_hash": self.relative_path_hash,
            "content_hash": self.content_hash,
            "mtime": self.mtime,
            "size": self.size,
            "sync_policy": self.sync_policy,
            "permission_scope": self.permission_scope,
            "last_seen_at": self.last_seen_at,
            "deleted_at": self.deleted_at,
            "revoked_at": self.revoked_at,
        }

    def to_dict(self) -> dict[str, Any]:
        data = self.metadata()
        data["derived_summary"] = self.derived_summary
        data["has_redacted_content"] = bool(self.redacted_content)
        return data


@dataclass(frozen=True)
class EvidenceRequest:
    source_ref_id: str
    requesting_device_id_hash: str
    span_ref_id: str = ""
    approval_ref: str = ""
    expected_content_hash: str = ""
    max_bytes: int = 4096
    redaction_profile: str = "public_safe"

    def __post_init__(self) -> None:
        require_opaque_id(self.source_ref_id, "source_ref_id")
        require_sha256(self.requesting_device_id_hash, "requesting_device_id_hash")
        if self.expected_content_hash:
            require_sha256(self.expected_content_hash, "expected_content_hash")
        if self.span_ref_id:
            require_opaque_id(self.span_ref_id, "span_ref_id")
        if not isinstance(self.max_bytes, int) or self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")


@dataclass(frozen=True)
class EvidenceResponse:
    resolution_state: ResolutionState
    reason_code: str
    policy: str
    same_device_proof: str
    approval_ref: str
    audit_event_id: str
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", public_safe_text(self.content, max_chars=8192))
        ensure_public_safe(self.to_dict(), "EvidenceResponse")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BrainEventEnvelope:
    event_id: str
    idempotency_key: str
    device_id_hash: str
    event_type: str
    occurred_at: str
    observed_at: str
    ontology_version: str
    payload_hash: str
    payload: dict[str, Any] = field(default_factory=dict)
    tombstone: bool = False

    def __post_init__(self) -> None:
        require_opaque_id(self.event_id, "event_id")
        require_opaque_id(self.idempotency_key, "idempotency_key")
        require_sha256(self.device_id_hash, "device_id_hash")
        require_sha256(self.payload_hash, "payload_hash")
        require_non_empty(self.event_type, "event_type")
        ensure_public_safe(self.payload, "BrainEventEnvelope.payload")

    @classmethod
    def from_payload(
        cls,
        *,
        event_id: str,
        idempotency_key: str,
        device_id_hash: str,
        event_type: str,
        occurred_at: str,
        payload: dict[str, Any],
        observed_at: str = "",
        ontology_version: str = "1.0.0",
        tombstone: bool = False,
    ) -> "BrainEventEnvelope":
        return cls(
            event_id=event_id,
            idempotency_key=idempotency_key,
            device_id_hash=device_id_hash,
            event_type=event_type,
            occurred_at=occurred_at,
            observed_at=observed_at or utc_now_iso(),
            ontology_version=ontology_version,
            payload_hash=hash_payload(payload),
            payload=dict(payload),
            tombstone=tombstone,
        )

    def target_id(self) -> str:
        for field_name in ("target_id", "memory_id", "source_ref_id", "artifact_id", "episode_id"):
            value = self.payload.get(field_name)
            if value:
                return str(value)
        return self.idempotency_key

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OntologyEpisode:
    episode_id: str
    event_id: str
    idempotency_key: str
    entity_type: str
    natural_id: str
    lifecycle_state: str
    currentness: str
    source_event_ids: tuple[str, ...]
    source_ref_ids: tuple[str, ...]
    valid_from: str
    valid_to: str
    observed_at: str
    reference_time: str
    content_hash: str
    ontology_version: str
    extractor_version: str
    payload: dict[str, Any] = field(default_factory=dict)
    relations: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        require_opaque_id(self.episode_id, "episode_id")
        require_opaque_id(self.event_id, "event_id")
        require_opaque_id(self.idempotency_key, "idempotency_key")
        require_opaque_id(self.natural_id, "natural_id")
        require_sha256(self.content_hash, "content_hash")
        object.__setattr__(self, "entity_type", require_non_empty(self.entity_type, "entity_type"))
        object.__setattr__(self, "source_event_ids", tuple(self.source_event_ids))
        object.__setattr__(self, "source_ref_ids", tuple(self.source_ref_ids))
        object.__setattr__(self, "relations", tuple(self.relations))
        ensure_public_safe(self.payload, "OntologyEpisode.payload")
        ensure_public_safe(list(self.source_ref_ids), "OntologyEpisode.source_ref_ids")

    @classmethod
    def from_payload(
        cls,
        *,
        event_id: str,
        entity_type: str,
        natural_id: str,
        payload: dict[str, Any],
        lifecycle_state: str = "accepted",
        currentness: str = "current",
        source_event_ids: list[str] | tuple[str, ...] = (),
        source_ref_ids: list[str] | tuple[str, ...] = (),
        valid_from: str = "",
        valid_to: str = "",
        observed_at: str = "",
        reference_time: str = "",
        ontology_version: str = "1.0.0",
        extractor_version: str = "0.1.0",
        relations: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    ) -> "OntologyEpisode":
        content_hash = hash_payload(
            {
                "entity_type": entity_type,
                "natural_id": natural_id,
                "payload": payload,
                "currentness": currentness,
                "relations": list(relations),
            }
        )
        episode_id = f"episode:{short_hash([event_id, natural_id, content_hash])}"
        idempotency_key = f"ontology-episode:{short_hash([natural_id, content_hash])}"
        timestamp = observed_at or utc_now_iso()
        return cls(
            episode_id=episode_id,
            event_id=event_id,
            idempotency_key=idempotency_key,
            entity_type=entity_type,
            natural_id=natural_id,
            lifecycle_state=lifecycle_state,
            currentness=currentness,
            source_event_ids=tuple(source_event_ids),
            source_ref_ids=tuple(source_ref_ids),
            valid_from=valid_from or timestamp,
            valid_to=valid_to,
            observed_at=timestamp,
            reference_time=reference_time or timestamp,
            content_hash=content_hash,
            ontology_version=ontology_version,
            extractor_version=extractor_version,
            payload=dict(payload),
            relations=tuple(dict(relation) for relation in relations),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source_event_ids"] = list(self.source_event_ids)
        data["source_ref_ids"] = list(self.source_ref_ids)
        data["relations"] = [dict(relation) for relation in self.relations]
        return data

    def search_text(self) -> str:
        return " ".join([self.entity_type, self.natural_id, stable_json(self.payload)]).lower()


@dataclass(frozen=True)
class GraphMemoryResult:
    status: str
    episodes: tuple[OntologyEpisode, ...] = ()
    details: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "episodes": [episode.to_dict() for episode in self.episodes],
            "details": list(self.details),
        }


@dataclass(frozen=True)
class ContextPack:
    brain_id: str
    current_task: str
    last_stopped_at: str
    unfinished_items: tuple[str, ...]
    relevant_decisions: tuple[dict[str, Any], ...]
    similar_incidents: tuple[dict[str, Any], ...]
    persona_constraints: tuple[dict[str, Any], ...]
    source_refs: tuple[dict[str, Any], ...]
    memory_status: dict[str, Any]
    graph_status: dict[str, Any]
    bridge_status: dict[str, Any]
    bridge_evidence: tuple[dict[str, Any], ...] = ()
    gaps: tuple[str, ...] = ()
    audit: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ensure_public_safe(self.to_dict(), "ContextPack")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in (
            "unfinished_items",
            "relevant_decisions",
            "similar_incidents",
            "persona_constraints",
            "source_refs",
            "bridge_evidence",
            "gaps",
        ):
            data[key] = list(data[key])
        return data
