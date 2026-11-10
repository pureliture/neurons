"""Versioned source/representation mapping; not historical vector provenance.

Only the named deterministic transform is supported. The digest binds the
receipt, not a signature or evidence that a historical vector was safe to embed.
No importer, CLI, or runtime factory enables this local compatibility seam.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..couchdb_source.document_model import sha256_hash
from ..local_embedding.canonical import canonical_input_hash_v1, canonical_session_input_v1
from ..model_connectors import DEFAULT_EMBEDDING_PROFILE_ID, LOCAL_EMBEDDING_PROFILE_ID

REPRESENTATION_KIND = "qdrant_public_safe_mask.v1"
LOCAL_SESSION_REPRESENTATION_KIND = "session_memory_v1"
LOCAL_REPRESENTATION_KINDS = frozenset({REPRESENTATION_KIND, LOCAL_SESSION_REPRESENTATION_KIND})
LOCAL_REPRESENTATION_RECEIPT_VERSION = 3
LOCAL_IDENTITY_SCHEMES = frozenset({"legacy_exact", "legacy_reuse", "local_v1"})
LOCAL_RECEIPT_DIGEST_FIELDS = (
    "receipt_version",
    "representation_kind",
    "project",
    "provider",
    "session_id_hash",
    "active_content_hash",
    "projected_source_hash",
    "representation_content_hash",
    "session_memory_knowledge_id",
    "identity_scheme",
    "embedding_profile",
    "input_hash",
)
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


def derive_local_pg_chunk_id(
    *,
    project: str,
    provider: str,
    session_id_hash: str,
    source_hash: str,
    content_hash: str,
    embedding_profile: str = LOCAL_EMBEDDING_PROFILE_ID,
) -> str:
    """Versioned deterministic local_v1 chunk_id (64 hex chars)."""
    if not source_hash or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(source_hash)):
        raise ValueError("local_v1 chunk_id requires a valid sha256 source_hash")
    if not content_hash or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(content_hash)):
        raise ValueError("local_v1 chunk_id requires a valid sha256 content_hash")
    identity = json.dumps(
        [
            "local_v1",
            str(project or ""),
            str(provider or ""),
            str(session_id_hash or ""),
            str(source_hash),
            str(content_hash),
            str(embedding_profile or LOCAL_EMBEDDING_PROFILE_ID),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def local_mapping_digest_v3(receipt: dict) -> str:
    """Compute provenance_digest for Receipt v3 excluding provenance_digest itself."""
    if not isinstance(receipt, dict):
        raise ValueError("receipt must be a dict")
    for key in LOCAL_RECEIPT_DIGEST_FIELDS:
        if key not in receipt:
            raise ValueError(f"missing required receipt v3 field: {key}")
    digest_payload = {key: receipt[key] for key in LOCAL_RECEIPT_DIGEST_FIELDS}
    if "provenance_digest" in digest_payload:
        raise ValueError("provenance_digest must never be included in digest input")
    return sha256_hash(
        json.dumps(
            digest_payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def build_local_representation_receipt_v3(
    *,
    representation_kind: str = LOCAL_SESSION_REPRESENTATION_KIND,
    project: str,
    provider: str,
    session_id_hash: str,
    active_content_hash: str,
    projected_source_hash: str,
    representation_content_hash: str,
    session_memory_knowledge_id: str,
    identity_scheme: str,
    embedding_profile: str = LOCAL_EMBEDDING_PROFILE_ID,
    input_hash: str,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "receipt_version": LOCAL_REPRESENTATION_RECEIPT_VERSION,
        "representation_kind": representation_kind,
        "project": project,
        "provider": provider,
        "session_id_hash": session_id_hash,
        "active_content_hash": active_content_hash,
        "projected_source_hash": projected_source_hash,
        "representation_content_hash": representation_content_hash,
        "session_memory_knowledge_id": session_memory_knowledge_id,
        "identity_scheme": identity_scheme,
        "embedding_profile": embedding_profile,
        "input_hash": input_hash,
    }
    receipt["provenance_digest"] = local_mapping_digest_v3(receipt)
    if not valid_local_representation_receipt_v3(receipt):
        raise ValueError("constructed local representation receipt v3 failed validation")
    return receipt


def valid_local_pg_receipt_metadata(
    receipt: dict,
    *,
    store: Any | None = None,
    current_materialized: Any | None = None,
    require_ready_vector: bool = False,
) -> bool:
    if not isinstance(receipt, dict):
        return False
    version = receipt.get("receipt_version")
    if type(version) is not int:
        return False
    if version == 4:
        return valid_local_segment_receipt_v4(
            receipt,
            store=store,
            current_materialized=current_materialized,
            require_ready_vector=require_ready_vector,
        )
    if version != LOCAL_REPRESENTATION_RECEIPT_VERSION:
        return False
    if any(not isinstance(receipt[key], str) for key in _BASE_FIELDS if key in receipt):
        return False
    allowed = _BASE_FIELDS | set(LOCAL_RECEIPT_DIGEST_FIELDS) | {"provenance_digest"}
    if set(receipt) - allowed:
        return False
    return valid_local_representation_receipt_v3(
        receipt,
        store=store,
        current_materialized=current_materialized,
        require_ready_vector=require_ready_vector,
    )


def _segment_receipt_digest(receipt: dict) -> str:
    fields = (
        "receipt_version",
        "representation",
        "project",
        "provider",
        "session_id_hash",
        "active_content_hash",
        "projected_source_hash",
        "representation_content_hash",
        "session_memory_knowledge_id",
        "embedding_profile",
        "segment_ids",
        "segment_set_digest",
    )
    return sha256_hash(
        json.dumps(
            {key: receipt[key] for key in fields},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def build_local_segment_receipt_v4(
    *,
    project: str,
    provider: str,
    session_id_hash: str,
    active_content_hash: str,
    projected_source_hash: str,
    representation_content_hash: str,
    session_memory_knowledge_id: str,
    embedding_profile: str,
    segment_ids: list[str],
    segment_set_digest: str,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "receipt_version": 4,
        "representation": "segment_v1",
        "project": project,
        "provider": provider,
        "session_id_hash": session_id_hash,
        "active_content_hash": active_content_hash,
        "projected_source_hash": projected_source_hash,
        "representation_content_hash": representation_content_hash,
        "session_memory_knowledge_id": session_memory_knowledge_id,
        "embedding_profile": embedding_profile,
        "segment_ids": list(segment_ids),
        "segment_set_digest": segment_set_digest,
    }
    receipt["provenance_digest"] = _segment_receipt_digest(receipt)
    if not valid_local_segment_receipt_v4(receipt):
        raise ValueError("constructed local segment receipt v4 failed validation")
    return receipt


def valid_local_segment_receipt_v4(
    receipt: dict,
    *,
    store: Any | None = None,
    current_materialized: Any | None = None,
    require_ready_vector: bool = False,
) -> bool:
    if not isinstance(receipt, dict):
        return False
    required = {
        "receipt_version",
        "representation",
        "project",
        "provider",
        "session_id_hash",
        "active_content_hash",
        "projected_source_hash",
        "representation_content_hash",
        "session_memory_knowledge_id",
        "embedding_profile",
        "segment_ids",
        "segment_set_digest",
        "provenance_digest",
    }
    if set(receipt) - (_BASE_FIELDS | required) or not required.issubset(receipt):
        return False
    if (
        type(receipt.get("receipt_version")) is not int
        or receipt["receipt_version"] != 4
        or receipt.get("representation") != "segment_v1"
        or receipt.get("embedding_profile") != LOCAL_EMBEDDING_PROFILE_ID
    ):
        return False
    for key in (
        "project",
        "provider",
        "session_id_hash",
        "active_content_hash",
        "projected_source_hash",
        "representation_content_hash",
        "session_memory_knowledge_id",
    ):
        if not isinstance(receipt.get(key), str) or not receipt[key]:
            return False
    segment_ids = receipt.get("segment_ids")
    if (
        not isinstance(segment_ids, list)
        or not segment_ids
        or any(not isinstance(value, str) or not value or len(value) > 64 for value in segment_ids)
        or len(set(segment_ids)) != len(segment_ids)
        or receipt["session_memory_knowledge_id"] != segment_ids[0]
    ):
        return False
    if not all(
        isinstance(receipt.get(key), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", receipt[key])
        for key in (
            "session_id_hash",
            "active_content_hash",
            "projected_source_hash",
            "representation_content_hash",
            "segment_set_digest",
            "provenance_digest",
        )
    ):
        return False
    expected_set_digest = sha256_hash(
        json.dumps(segment_ids, ensure_ascii=False, separators=(",", ":"))
    )
    if (
        receipt["segment_set_digest"] != expected_set_digest
        or receipt["provenance_digest"] != _segment_receipt_digest(receipt)
    ):
        return False
    if current_materialized is not None and (
        not current_materialized.fully_materialized
        or current_materialized.session_id_hash != receipt["session_id_hash"]
        or current_materialized.project != receipt["project"]
        or current_materialized.provider != receipt["provider"]
        or current_materialized.content_hash != receipt["active_content_hash"]
        or current_materialized.source_hash != receipt["projected_source_hash"]
        or current_materialized.content_hash != receipt["representation_content_hash"]
    ):
        return False
    if store is not None:
        from ..local_embedding.canonical import compute_input_hash
        from ..model_connectors import LOCAL_EMBEDDING_DIM

        for segment_id in segment_ids:
            row = store.get_chunk(segment_id)
            if (
                row is None
                or row.session_id_hash != receipt["session_id_hash"]
                or row.project != receipt["project"]
                or row.provider != receipt["provider"]
                or not row.content_markdown
                or sha256_hash(row.content_markdown) != row.content_hash
            ):
                return False
            local = store.get_local_embedding_row(
                target_type="session_chunk",
                target_id=segment_id,
                embedding_profile=receipt["embedding_profile"],
            )
            if local is None:
                if require_ready_vector:
                    return False
                continue
            if (
                local.get("source_content_hash") != row.content_hash
                or local.get("input_hash") != compute_input_hash(row.content_markdown)
                or (require_ready_vector and (
                    local.get("state") != "ready"
                    or local.get("vector") is None
                    or len(local["vector"]) != LOCAL_EMBEDDING_DIM
                ))
            ):
                return False
    return True


def valid_local_representation_receipt_v3(
    receipt: dict,
    *,
    store: Any | None = None,
    current_materialized: Any | None = None,
    require_ready_vector: bool = False,
) -> bool:
    from .pg_backfill import _derive_pg_chunk_id_impl
    from .pg_embedding_chunks import CHUNK_BYTES, CHUNKED_PROFILE

    if not isinstance(receipt, dict):
        return False
    if (
        type(receipt.get("receipt_version")) is not int
        or receipt["receipt_version"] != LOCAL_REPRESENTATION_RECEIPT_VERSION
    ):
        return False
    allowed = _BASE_FIELDS | set(LOCAL_RECEIPT_DIGEST_FIELDS) | {"provenance_digest"}
    if set(receipt) - allowed:
        return False
    if any(key not in receipt for key in (*LOCAL_RECEIPT_DIGEST_FIELDS, "provenance_digest")):
        return False
    if any(
        not isinstance(receipt.get(key), str) or not receipt[key]
        for key in LOCAL_RECEIPT_DIGEST_FIELDS
        if key != "receipt_version"
    ):
        return False
    if receipt.get("representation_kind") not in LOCAL_REPRESENTATION_KINDS:
        return False
    if receipt.get("embedding_profile") != LOCAL_EMBEDDING_PROFILE_ID:
        return False
    identity_scheme = receipt.get("identity_scheme")
    if identity_scheme not in LOCAL_IDENTITY_SCHEMES:
        return False
    for key in (
        "session_id_hash",
        "active_content_hash",
        "projected_source_hash",
        "representation_content_hash",
        "input_hash",
        "provenance_digest",
    ):
        if not isinstance(receipt.get(key), str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", receipt[key]
        ):
            return False
    try:
        expected_digest = local_mapping_digest_v3(receipt)
    except ValueError:
        return False
    if receipt["provenance_digest"] != expected_digest:
        return False

    chunk_id = receipt["session_memory_knowledge_id"]
    if identity_scheme == "local_v1":
        try:
            expected_local_id = derive_local_pg_chunk_id(
                project=receipt["project"],
                provider=receipt["provider"],
                session_id_hash=receipt["session_id_hash"],
                source_hash=receipt["projected_source_hash"],
                content_hash=receipt["representation_content_hash"],
                embedding_profile=receipt["embedding_profile"],
            )
        except ValueError:
            return False
        if chunk_id != expected_local_id:
            return False
    elif identity_scheme == "legacy_exact":
        legacy_v1_id = _derive_pg_chunk_id_impl(
            project=receipt["project"],
            provider=receipt["provider"],
            session_id_hash=receipt["session_id_hash"],
            source_hash=receipt["projected_source_hash"],
            content_hash=receipt["representation_content_hash"],
            embedding_profile=DEFAULT_EMBEDDING_PROFILE_ID,
        )
        legacy_v2_id = _derive_pg_chunk_id_impl(
            project=receipt["project"],
            provider=receipt["provider"],
            session_id_hash=receipt["session_id_hash"],
            source_hash=receipt["projected_source_hash"],
            content_hash=receipt["representation_content_hash"],
            embedding_profile=CHUNKED_PROFILE,
        )
        if chunk_id not in (legacy_v1_id, legacy_v2_id):
            return False
    elif identity_scheme == "legacy_reuse":
        if not isinstance(chunk_id, str) or not chunk_id or len(chunk_id) > 64:
            return False
        # Verify it is not claiming legacy_reuse for an unverified >8KiB row when store is present
        legacy_v1_id = _derive_pg_chunk_id_impl(
            project=receipt["project"],
            provider=receipt["provider"],
            session_id_hash=receipt["session_id_hash"],
            source_hash=receipt["projected_source_hash"],
            content_hash=receipt["representation_content_hash"],
            embedding_profile=DEFAULT_EMBEDDING_PROFILE_ID,
        )
        legacy_v2_id = _derive_pg_chunk_id_impl(
            project=receipt["project"],
            provider=receipt["provider"],
            session_id_hash=receipt["session_id_hash"],
            source_hash=receipt["projected_source_hash"],
            content_hash=receipt["representation_content_hash"],
            embedding_profile=CHUNKED_PROFILE,
        )
        if store is not None:
            row = store.get_chunk(chunk_id)
            if row is None:
                return False
            if len(row.content_markdown.encode("utf-8")) > CHUNK_BYTES and chunk_id not in (
                legacy_v1_id,
                legacy_v2_id,
            ):
                return False

    if current_materialized is not None:
        if (
            not getattr(current_materialized, "fully_materialized", False)
            or getattr(current_materialized, "session_id_hash", "") != receipt["session_id_hash"]
            or getattr(current_materialized, "project", "") != receipt["project"]
            or getattr(current_materialized, "provider", "") != receipt["provider"]
            or getattr(current_materialized, "content_hash", "") != receipt["active_content_hash"]
            or getattr(current_materialized, "source_hash", "") != receipt["projected_source_hash"]
        ):
            return False

    if store is not None:
        row = store.get_chunk(chunk_id)
        if row is None:
            return False
        if (
            row.session_id_hash != receipt["session_id_hash"]
            or row.project != receipt["project"]
            or row.provider != receipt["provider"]
            or row.content_hash != receipt["representation_content_hash"]
            or sha256_hash(row.content_markdown) != receipt["representation_content_hash"]
        ):
            return False
        try:
            computed_input_hash = canonical_input_hash_v1(
                canonical_session_input_v1(row.content_markdown)
            )
        except ValueError:
            return False
        if computed_input_hash != receipt["input_hash"]:
            return False
        if hasattr(store, "get_local_embedding_row"):
            local_emb = store.get_local_embedding_row(
                target_type="session_chunk",
                target_id=chunk_id,
                embedding_profile=receipt["embedding_profile"],
            )
            if require_ready_vector and local_emb is None:
                return False
            if local_emb is not None:
                if (
                    local_emb.get("embedding_profile") != receipt["embedding_profile"]
                    or local_emb.get("content_hash") != receipt["representation_content_hash"]
                    or local_emb.get("input_hash") != receipt["input_hash"]
                ):
                    return False
                if require_ready_vector:
                    vec = local_emb.get("embedding")
                    if (
                        local_emb.get("embedding_state") != "ready"
                        or not isinstance(vec, list)
                        or len(vec) != 256
                    ):
                        return False

    return True
