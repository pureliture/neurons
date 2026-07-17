from __future__ import annotations

import re

from .public_safe_util import hash_payload, require_sha256


PERMISSION_AUDIT_POLICY = "single_bounded_denial.v1"
_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def build_permission_audit_operation_hash(
    *,
    build_association_hash: str,
    ops_revision: str,
    expected_commit: str,
) -> str:
    """Bind the one allowed audit action to one exact Jenkins build execution."""

    association = require_sha256(
        build_association_hash,
        "build_association_hash",
    )
    if not isinstance(ops_revision, str) or not _COMMIT_SHA_PATTERN.fullmatch(
        ops_revision
    ):
        raise ValueError("ops_revision must be a full commit sha")
    if not isinstance(expected_commit, str) or not _COMMIT_SHA_PATTERN.fullmatch(
        expected_commit
    ):
        raise ValueError("expected_commit must be a full commit sha")
    return hash_payload(
        {
            "action": PERMISSION_AUDIT_POLICY,
            "build_association_hash": association,
            "expected_commit": expected_commit,
            "ops_revision": ops_revision,
        }
    )


__all__ = [
    "PERMISSION_AUDIT_POLICY",
    "build_permission_audit_operation_hash",
]
