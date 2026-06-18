from __future__ import annotations

from ._util import public_safe_text, short_hash
from .models import EvidenceRequest, EvidenceResponse, SourceRefRecord


class SourceRefCatalog:
    def __init__(self, records: list[SourceRefRecord] | None = None) -> None:
        self._records: dict[str, SourceRefRecord] = {}
        for record in records or []:
            self.register(record)

    def register(self, record: SourceRefRecord) -> None:
        self._records[record.source_ref_id] = record

    def get(self, source_ref_id: str) -> SourceRefRecord | None:
        return self._records.get(source_ref_id)

    def list_all(self) -> list[SourceRefRecord]:
        return list(self._records.values())

    def resolver(self) -> "SourceRefResolver":
        return SourceRefResolver(self.list_all())


class SourceRefResolver:
    """Policy evaluator for opaque source evidence lookup."""

    def __init__(self, records: list[SourceRefRecord] | None = None) -> None:
        self._records: dict[str, SourceRefRecord] = {}
        for record in records or []:
            self.register(record)

    def register(self, record: SourceRefRecord) -> None:
        self._records[record.source_ref_id] = record

    def resolve(self, request: EvidenceRequest) -> EvidenceResponse:
        record = self._records.get(request.source_ref_id)
        if record is None:
            return _response(
                request=request,
                resolution_state="unresolved",
                reason_code="source_ref_unknown",
                policy="unknown",
                same_device_proof="unknown",
            )
        if record.revoked_at:
            return _response(
                request=request,
                record=record,
                resolution_state="permission_revoked",
                reason_code="permission_revoked",
                same_device_proof="not_required",
            )
        if record.deleted_at:
            return _response(
                request=request,
                record=record,
                resolution_state="deleted_source",
                reason_code="source_deleted",
                same_device_proof="not_required",
            )
        if request.expected_content_hash and request.expected_content_hash != record.content_hash:
            return _response(
                request=request,
                record=record,
                resolution_state="stale_hash",
                reason_code="content_hash_mismatch",
                same_device_proof="not_required",
            )
        if record.sync_policy == "metadata_only":
            return _response(
                request=request,
                record=record,
                resolution_state="metadata_only",
                reason_code="policy_metadata_only",
                same_device_proof="not_required",
            )
        if record.sync_policy == "derived_only":
            return _response(
                request=request,
                record=record,
                resolution_state="derived_only",
                reason_code="policy_derived_only",
                same_device_proof="not_required",
                content=record.derived_summary,
            )
        if record.sync_policy == "local_only":
            if request.requesting_device_id_hash != record.device_id_hash:
                return _response(
                    request=request,
                    record=record,
                    resolution_state="same_device_required",
                    reason_code="same_device_required",
                    same_device_proof="failed",
                )
            if not request.approval_ref:
                return _response(
                    request=request,
                    record=record,
                    resolution_state="approval_required",
                    reason_code="approval_required",
                    same_device_proof="passed",
                )
            content = record.redacted_content or record.derived_summary
            if not content:
                return _response(
                    request=request,
                    record=record,
                    resolution_state="same_device_required",
                    reason_code="delegated_resolution_required",
                    same_device_proof="passed",
                )
            return _response(
                request=request,
                record=record,
                resolution_state="resolved",
                reason_code="delegated_redacted_content",
                same_device_proof="passed",
                content=content,
            )
        content = record.redacted_content or record.derived_summary
        if not content:
            return _response(
                request=request,
                record=record,
                resolution_state="metadata_only",
                reason_code="no_redacted_content_available",
                same_device_proof="not_required",
            )
        return _response(
            request=request,
            record=record,
            resolution_state="resolved",
            reason_code="policy_full_sync_redacted",
            same_device_proof="not_required",
            content=content,
        )


def _response(
    *,
    request: EvidenceRequest,
    resolution_state: str,
    reason_code: str,
    policy: str | None = None,
    same_device_proof: str,
    record: SourceRefRecord | None = None,
    content: str = "",
) -> EvidenceResponse:
    bounded = public_safe_text(content, max_chars=request.max_bytes)
    return EvidenceResponse(
        resolution_state=resolution_state,
        reason_code=reason_code,
        policy=policy or (record.sync_policy if record else "unknown"),
        same_device_proof=same_device_proof,
        approval_ref=request.approval_ref,
        audit_event_id=f"audit:{short_hash([request.source_ref_id, reason_code, request.approval_ref])}",
        content=bounded,
        metadata=record.metadata() if record else {},
    )
