from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._util import ensure_public_safe
from .local_evidence import RAW_BODY_FIELDS


NEURONS_CENTRAL_FEDERATION_SCHEMA = "neurons_central_federation.v1"
FORBIDDEN_CENTRAL_SYNC_FIELDS = {
    *RAW_BODY_FIELDS,
    "graph_db_file",
    "graph_db_path",
    "graph_db_bytes",
    "neo4j_store",
    "graph_store",
}


def federate_neurons_local_artifacts(artifacts: list[Mapping[str, Any]]) -> dict[str, Any]:
    for artifact in artifacts:
        _reject_forbidden_residue(artifact)
        if artifact.get("central_safe") is not True or artifact.get("raw_body_included") is True:
            raise ValueError("raw file bodies and graph DB files must not be centrally synced")

    devices: list[str] = []
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    target_content_devices: dict[str, dict[str, list[str]]] = {}
    for artifact in artifacts:
        device_id = str(artifact.get("device_id_hash") or "")
        if device_id and device_id not in devices:
            devices.append(device_id)
        for edge in artifact.get("evidence_edges") or []:
            if not isinstance(edge, Mapping):
                continue
            _reject_forbidden_residue(edge)
            target_ref = str(edge.get("target_ref") or "")
            content_hash = str(edge.get("content_hash") or "")
            sync_policy = str(edge.get("sync_policy") or "metadata_only")
            key = (target_ref, content_hash, sync_policy)
            entry = merged.setdefault(
                key,
                {
                    "edge_type": edge.get("edge_type") or "",
                    "target_ref": target_ref,
                    "relative_path_hash": edge.get("relative_path_hash") or "",
                    "content_hash": content_hash,
                    "sync_policy": sync_policy,
                    "source_refs": [],
                    "device_id_hashes": [],
                },
            )
            _append_unique(entry["source_refs"], str(edge.get("source_ref") or ""))
            _append_unique(entry["device_id_hashes"], device_id)
            by_content = target_content_devices.setdefault(target_ref, {})
            by_content.setdefault(content_hash, [])
            _append_unique(by_content[content_hash], device_id)

    conflicts = _conflicts(target_content_devices)
    result = {
        "schema_version": NEURONS_CENTRAL_FEDERATION_SCHEMA,
        "central_safe": True,
        "device_count": len(devices),
        "edge_count": len(merged),
        "merged_edges": list(merged.values()),
        "conflicts": conflicts,
    }
    ensure_public_safe(result, "NeuronsCentralFederation")
    return result


def _conflicts(target_content_devices: dict[str, dict[str, list[str]]]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for target_ref, by_content in target_content_devices.items():
        content_hashes = list(by_content)
        if len(content_hashes) <= 1:
            continue
        devices: list[str] = []
        for content_hash in content_hashes:
            for device_id in by_content[content_hash]:
                _append_unique(devices, device_id)
        conflicts.append(
            {
                "code": "file_content_hash_conflict",
                "target_ref": target_ref,
                "content_hashes": content_hashes,
                "device_id_hashes": devices,
                "explainable": True,
            }
        )
    return conflicts


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _reject_forbidden_residue(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in FORBIDDEN_CENTRAL_SYNC_FIELDS and item not in (None, "", b"", False):
                raise ValueError("raw file bodies and graph DB files must not be centrally synced")
            _reject_forbidden_residue(item)
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden_residue(item)
