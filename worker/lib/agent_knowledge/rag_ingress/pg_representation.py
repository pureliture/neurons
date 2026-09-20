"""Versioned source/representation mapping; not historical vector provenance.

Only the named deterministic transform is supported. The digest binds the
receipt, not a signature or evidence that a historical vector was safe to embed.
No importer, CLI, or runtime factory enables this local compatibility seam.
"""
from __future__ import annotations

import json
import re

from ..couchdb_source.document_model import sha256_hash
from ..model_connectors import DEFAULT_EMBEDDING_PROFILE_ID

REPRESENTATION_KIND = "qdrant_public_safe_mask.v1"
_MAPPING_FIELDS = (
    "receipt_version", "representation_kind", "embedding_profile",
    "session_id_hash", "provider", "project", "active_content_hash",
    "projected_source_hash", "representation_content_hash", "session_memory_knowledge_id",
)


def mapping_digest(receipt: dict) -> str:
    return sha256_hash(json.dumps(
        {key: receipt[key] for key in _MAPPING_FIELDS},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ))


_BASE_FIELDS = {
    "projection_status", "projected_source_hash", "active_content_hash",
    "session_memory_knowledge_id", "provider", "project", "materialized_at", "failure_reason",
}


def valid_pg_receipt_metadata(receipt: dict) -> bool:
    if not isinstance(receipt, dict):
        return False
    version = receipt.get("receipt_version", 1)
    if type(version) is not int or version not in (1, 2):
        return False
    if any(not isinstance(receipt[key], str) for key in _BASE_FIELDS if key in receipt):
        return False
    allowed = _BASE_FIELDS | {"receipt_version"}
    if version == 2:
        allowed |= set(_MAPPING_FIELDS) | {"provenance_digest"}
    return not (set(receipt) - allowed) and (version == 1 or valid_representation_receipt(receipt))


def valid_representation_receipt(receipt: dict) -> bool:
    from .pg_backfill import _derive_pg_chunk_id_impl

    if not isinstance(receipt, dict):
        return False
    if type(receipt.get("receipt_version")) is not int or receipt["receipt_version"] != 2:
        return False
    if receipt.get("representation_kind") != REPRESENTATION_KIND or receipt.get("embedding_profile") != DEFAULT_EMBEDDING_PROFILE_ID:
        return False
    if any(not isinstance(receipt.get(key), str) or not receipt[key] for key in _MAPPING_FIELDS if key != "receipt_version"):
        return False
    for key in ("session_id_hash", "active_content_hash", "projected_source_hash", "representation_content_hash", "provenance_digest"):
        if not isinstance(receipt.get(key), str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", receipt[key]):
            return False
    expected_id = _derive_pg_chunk_id_impl(
        project=receipt["project"], provider=receipt["provider"],
        session_id_hash=receipt["session_id_hash"], source_hash=receipt["projected_source_hash"],
        content_hash=receipt["representation_content_hash"], embedding_profile=receipt["embedding_profile"],
    )
    return receipt["session_memory_knowledge_id"] == expected_id and receipt["provenance_digest"] == mapping_digest(receipt)
