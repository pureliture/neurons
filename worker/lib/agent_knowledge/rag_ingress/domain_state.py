"""Domain-state helper shapes for M3 shadow projections."""

from __future__ import annotations

from .state_db import DomainRecordSpec


def build_delivery_projection_record(
    *,
    domain_record_id: str,
    resource_id_hash: str,
    lifecycle_status: str,
    payload_hash: str,
    target_profile: str,
    document_kind: str,
    session_id_hash: str = "",
    payload_ref: str = "",
) -> DomainRecordSpec:
    return DomainRecordSpec(
        domain_record_id=domain_record_id,
        domain_kind="delivery_projection",
        lifecycle_status=lifecycle_status,
        resource_id_hash=resource_id_hash,
        session_id_hash=session_id_hash,
        payload_hash=payload_hash,
        payload_ref=payload_ref,
        projection={
            "target_profile": target_profile,
            "document_kind": document_kind,
        },
    )
