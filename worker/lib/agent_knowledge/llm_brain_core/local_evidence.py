from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

from ._util import ensure_public_safe, public_safe_text, require_non_empty, require_sha256, short_hash
from .models import OntologyEpisode


LocalEvidenceEdgeType = Literal["SessionFile", "CommitFile"]
LocalEvidenceSyncPolicy = Literal["local_only", "metadata_only", "derived_only"]

RAW_BODY_FIELDS = {"raw_body", "body", "content", "file_body", "raw_file_body"}
SYNC_POLICIES = {"local_only", "metadata_only", "derived_only"}


@dataclass(frozen=True)
class LocalEvidenceEdge:
    edge_type: LocalEvidenceEdgeType
    source_ref: str
    target_ref: str
    device_id_hash: str
    relative_path_hash: str
    content_hash: str
    sync_policy: LocalEvidenceSyncPolicy
    raw_body_included: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_ref", require_non_empty(self.source_ref, "source_ref"))
        object.__setattr__(self, "target_ref", require_non_empty(self.target_ref, "target_ref"))
        object.__setattr__(self, "device_id_hash", require_sha256(self.device_id_hash, "device_id_hash"))
        object.__setattr__(self, "relative_path_hash", require_sha256(self.relative_path_hash, "relative_path_hash"))
        object.__setattr__(self, "content_hash", require_sha256(self.content_hash, "content_hash"))
        if self.sync_policy not in SYNC_POLICIES:
            raise ValueError("sync_policy must be local_only, metadata_only, or derived_only")
        if self.raw_body_included:
            raise ValueError("raw file bodies must remain local")
        ensure_public_safe(self.to_dict(), "LocalEvidenceEdge")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def local_evidence_edges_from_capture(records: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for record in records:
        _reject_raw_body_fields(record)
        evidence_type = str(record.get("evidence_type") or "")
        if evidence_type == "session_file":
            edges.append(_session_file_edge(record).to_dict())
        elif evidence_type == "commit_file":
            edges.append(_commit_file_edge(record).to_dict())
        else:
            raise ValueError("evidence_type must be session_file or commit_file")
    return edges


def local_evidence_episodes_from_capture(
    records: list[Mapping[str, Any]],
    *,
    brain_id: str,
) -> tuple[OntologyEpisode, ...]:
    edges = local_evidence_edges_from_capture(records)
    event_id = f"local-evidence:{short_hash([brain_id, edges])}"
    episodes = tuple(
        OntologyEpisode.from_payload(
            event_id=event_id,
            entity_type="LocalEvidenceEdge",
            natural_id=f"local-evidence:{short_hash(edge)}",
            payload={"brain_id": brain_id, **edge},
            source_event_ids=(str(edge.get("source_ref") or ""),),
        )
        for edge in edges
    )
    ensure_public_safe([episode.to_dict() for episode in episodes], "local_evidence_projection")
    return episodes


def _session_file_edge(record: Mapping[str, Any]) -> LocalEvidenceEdge:
    session_id_hash = require_sha256(str(record.get("session_id_hash") or ""), "session_id_hash")
    relative_path_hash = require_sha256(str(record.get("relative_path_hash") or ""), "relative_path_hash")
    return LocalEvidenceEdge(
        edge_type="SessionFile",
        source_ref=f"session:{session_id_hash}",
        target_ref=f"file:{relative_path_hash}",
        device_id_hash=str(record.get("device_id_hash") or ""),
        relative_path_hash=relative_path_hash,
        content_hash=str(record.get("content_hash") or ""),
        sync_policy=_sync_policy(record),
        raw_body_included=False,
    )


def _commit_file_edge(record: Mapping[str, Any]) -> LocalEvidenceEdge:
    commit_id = public_safe_text(str(record.get("commit_id") or ""), max_chars=160)
    relative_path_hash = require_sha256(str(record.get("relative_path_hash") or ""), "relative_path_hash")
    return LocalEvidenceEdge(
        edge_type="CommitFile",
        source_ref=require_non_empty(commit_id, "commit_id"),
        target_ref=f"file:{relative_path_hash}",
        device_id_hash=str(record.get("device_id_hash") or ""),
        relative_path_hash=relative_path_hash,
        content_hash=str(record.get("content_hash") or ""),
        sync_policy=_sync_policy(record),
        raw_body_included=False,
    )


def _sync_policy(record: Mapping[str, Any]) -> LocalEvidenceSyncPolicy:
    policy = str(record.get("sync_policy") or "metadata_only")
    if policy not in SYNC_POLICIES:
        raise ValueError("sync_policy must be local_only, metadata_only, or derived_only")
    return policy  # type: ignore[return-value]


def _reject_raw_body_fields(record: Mapping[str, Any]) -> None:
    for key in RAW_BODY_FIELDS:
        value = record.get(key)
        if value not in (None, "", b""):
            raise ValueError("raw file bodies must remain local")
