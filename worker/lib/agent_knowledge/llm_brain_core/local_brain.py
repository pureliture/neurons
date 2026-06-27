from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._util import ensure_public_safe, public_safe_text, require_sha256
from .context import BrainReadService
from .local_evidence import RAW_BODY_FIELDS
from .local_evidence import local_evidence_edges_from_capture


NEURONS_LOCAL_SYNC_ARTIFACT_SCHEMA = "neurons_local_sync_artifact.v1"


def build_neurons_local_sync_artifact(
    *,
    project: str,
    device_id_hash: str,
    evidence_edges: list[Mapping[str, Any]],
) -> dict[str, Any]:
    normalized_edges = [_central_safe_edge(edge) for edge in evidence_edges]
    artifact = {
        "schema_version": NEURONS_LOCAL_SYNC_ARTIFACT_SCHEMA,
        "project": public_safe_text(project, max_chars=120),
        "device_id_hash": require_sha256(device_id_hash, "device_id_hash"),
        "central_safe": True,
        "raw_body_included": False,
        "evidence_edges": normalized_edges,
        "edge_count": len(normalized_edges),
    }
    ensure_public_safe(artifact, "NeuronsLocalSyncArtifact")
    return artifact


def resolve_neurons_local_context(
    *,
    project: str,
    repository: str,
    branch: str,
    current_request: str,
    current_files: list[str],
    device_id_hash: str,
    memory_cards: list[Mapping[str, Any]],
    evidence_records: list[Mapping[str, Any]],
    response_mode: str = "full",
) -> dict[str, Any]:
    if response_mode not in {"full", "compact", "degraded"}:
        raise ValueError("response_mode must be full, compact, or degraded")
    evidence_edges = local_evidence_edges_from_capture(evidence_records)
    service = BrainReadService(memory_cards=[dict(card) for card in memory_cards])
    pack = service.brain_context_resolve(
        repository=repository,
        branch=branch,
        current_files=current_files,
        current_request=current_request,
        project=project,
    ).to_dict(mode=response_mode)  # type: ignore[arg-type]
    pack["local_mode"] = True
    pack["sync_artifact"] = build_neurons_local_sync_artifact(
        project=project,
        device_id_hash=device_id_hash,
        evidence_edges=evidence_edges,
    )
    ensure_public_safe(pack, "NeuronsLocalContextPack")
    return pack


def _central_safe_edge(edge: Mapping[str, Any]) -> dict[str, Any]:
    for key in RAW_BODY_FIELDS:
        if edge.get(key) not in (None, "", b""):
            raise ValueError("raw file bodies must remain local")
    if bool(edge.get("raw_body_included")):
        raise ValueError("raw file bodies must remain local")
    normalized = dict(edge)
    normalized["raw_body_included"] = False
    ensure_public_safe(normalized, "NeuronsLocalSyncArtifact.edge")
    return normalized
