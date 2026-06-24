"""Backwards-compatible re-export of the public-safe helpers.

The implementations moved to the delivery-safe top-level module
``agent_knowledge.public_safe_util`` so the ingress/delivery worker (which does not
vendor the heavy ``llm_brain_core`` package) can use them via the searchable-mirror
adapter. Existing ``from ._util import ...`` call sites keep working unchanged.
"""

from __future__ import annotations

from agent_knowledge.public_safe_util import (
    OPAQUE_ID_RE,
    PRIVATE_OUTPUT_RE,
    SECRET_ASSIGNMENT_RE,
    SHA256_RE,
    ensure_public_safe,
    hash_payload,
    list_or_empty,
    public_dict,
    public_safe_text,
    require_non_empty,
    require_opaque_id,
    require_sha256,
    sha256_text,
    short_hash,
    stable_json,
    utc_now_iso,
)

__all__ = [
    "OPAQUE_ID_RE",
    "PRIVATE_OUTPUT_RE",
    "SECRET_ASSIGNMENT_RE",
    "SHA256_RE",
    "ensure_public_safe",
    "hash_payload",
    "list_or_empty",
    "public_dict",
    "public_safe_text",
    "require_non_empty",
    "require_opaque_id",
    "require_sha256",
    "sha256_text",
    "short_hash",
    "stable_json",
    "utc_now_iso",
]
