from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .memory_card import validate_memory_card_envelope
from .transcript_model import MAX_TRANSCRIPT_SNIPPET_CHARS, redact_and_bound_evidence_text


PROJECTION_JOB_SCHEMA_VERSION = "llm_brain_index_projection_job.v1"
PROJECTION_PAYLOAD_SCHEMA_VERSION = "llm_brain_index_projection_payload.v1"
PROJECTABLE_LIFECYCLE_STATES = {"accepted", "human_accepted", "auto_accepted"}
PROJECTABLE_APPROVAL_STATES = {"approved", "auto_accepted"}


class RetiredIndexBridgeMemoryCardProjectionClient:
    """Adapter from MemoryCard projection payloads to RetiredIndexBridge document APIs."""

    def __init__(self, *, retired_index_bridge: Any, dataset_id: str):
        if not dataset_id:
            raise ValueError("dataset_id is required")
        self.retired_index_bridge = retired_index_bridge
        self.dataset_id = dataset_id

    def upsert_memory_card(self, payload: Mapping[str, Any], *, idempotency_key: str) -> dict:
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        filename = _projection_filename(idempotency_key)
        existing = _find_existing_projection_document(self.retired_index_bridge, self.dataset_id, filename)
        if existing:
            return {
                "status": "already_projected",
                "dataset_id": self.dataset_id,
                "document_id": existing,
                "idempotency_key": idempotency_key,
            }
        document = self.retired_index_bridge.upload_document(
            self.dataset_id,
            render_projection_document(payload),
            filename=filename,
        )
        document_id = str(document.get("document_id") or "")
        if not document_id:
            raise ValueError("RetiredIndexBridge upload did not return document_id")
        self.retired_index_bridge.update_metadata(
            self.dataset_id,
            document_id,
            _projection_metadata(payload, idempotency_key=idempotency_key),
        )
        self.retired_index_bridge.request_parse(self.dataset_id, [document_id])
        return {
            "status": "projected",
            "dataset_id": self.dataset_id,
            "document_id": document_id,
            "run": str(document.get("run") or ""),
            "idempotency_key": idempotency_key,
        }


def projection_idempotency_key(card: Mapping[str, Any]) -> str:
    projectable = _require_projectable_card(card)
    seed = _stable_json(
        {
            "schema_version": PROJECTION_PAYLOAD_SCHEMA_VERSION,
            "memory_id": projectable["memory_id"],
            "brain_id": projectable["brain_id"],
            "content_hash": projectable.get("content_hash") or "",
            "evidence_hashes": projectable["evidence_hashes"],
            "currentness": projectable["currentness"],
        }
    )
    return "llm_brain_projection:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def build_index_projection_payload(card: Mapping[str, Any]) -> dict:
    projectable = _require_projectable_card(card)
    summary = redact_and_bound_evidence_text(
        str(projectable.get("summary") or ""), MAX_TRANSCRIPT_SNIPPET_CHARS
    )
    render_text = redact_and_bound_evidence_text(
        str(projectable.get("render_text") or summary), MAX_TRANSCRIPT_SNIPPET_CHARS
    )
    return {
        "schema_version": PROJECTION_PAYLOAD_SCHEMA_VERSION,
        "memory_id": projectable["memory_id"],
        "brain_id": projectable["brain_id"],
        "title": str(projectable.get("title") or ""),
        "summary": summary,
        "render_text": render_text,
        "metadata": {
            "card_type": projectable["card_type"],
            "project": projectable["project"],
            "provider": projectable["provider"],
            "approval_state": projectable["approval_state"],
            "lifecycle_state": projectable["lifecycle_state"],
            "currentness": projectable["currentness"],
            "freshness": projectable["freshness"],
            "evidence_hashes": list(projectable.get("evidence_hashes") or []),
            "source_ref_count": len(projectable.get("source_refs") or []),
        },
        "content_hash": projectable.get("content_hash") or _payload_hash(projectable),
    }


def render_projection_document(payload: Mapping[str, Any]) -> str:
    """Render a redacted, searchable document body for the RetiredIndexBridge mirror."""

    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    lines = [
        f"# {str(payload.get('title') or payload.get('memory_id') or 'MemoryCard')}",
        "",
        str(payload.get("render_text") or payload.get("summary") or ""),
        "",
        "## Metadata",
        f"- schema_version: {str(payload.get('schema_version') or '')}",
        f"- memory_id: {str(payload.get('memory_id') or '')}",
        f"- brain_id: {str(payload.get('brain_id') or '')}",
        f"- card_type: {str(metadata.get('card_type') or '')}",
        f"- project: {str(metadata.get('project') or '')}",
        f"- provider: {str(metadata.get('provider') or '')}",
        f"- lifecycle_state: {str(metadata.get('lifecycle_state') or '')}",
        f"- approval_state: {str(metadata.get('approval_state') or '')}",
        f"- currentness: {str(metadata.get('currentness') or '')}",
        f"- content_hash: {str(payload.get('content_hash') or '')}",
    ]
    return "\n".join(lines).strip() + "\n"


def build_projection_job(card: Mapping[str, Any]) -> dict:
    payload = build_index_projection_payload(card)
    key = projection_idempotency_key(card)
    job_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return {
        "schema_version": PROJECTION_JOB_SCHEMA_VERSION,
        "job_id": "proj_" + job_hash[:16],
        "operation": "index_upsert",
        "idempotency_key": key,
        "status": "queued",
        "attempt_count": 0,
        "payload": payload,
        "canonical_state_changed": False,
    }


def enqueue_projection_jobs(cards: list[Mapping[str, Any]]) -> dict:
    jobs = []
    skipped = []
    seen: set[str] = set()
    for index, card in enumerate(cards):
        try:
            job = build_projection_job(card)
        except ValueError as exc:
            skipped.append({"index": index, "reason": str(exc)})
            continue
        if job["idempotency_key"] in seen:
            skipped.append({"index": index, "reason": "duplicate_idempotency_key"})
            continue
        seen.add(job["idempotency_key"])
        jobs.append(job)
    return {
        "schema_version": "llm_brain_projection_queue_plan.v1",
        "write_performed": False,
        "job_count": len(jobs),
        "skipped_count": len(skipped),
        "jobs": jobs,
        "skipped": skipped,
    }


def execute_projection_job(job: Mapping[str, Any], *, client: Any, allow_write: bool = False) -> dict:
    if not allow_write:
        return {
            "schema_version": "llm_brain_projection_execution_result.v1",
            "job_id": str(job.get("job_id") or ""),
            "status": "dry_run",
            "write_performed": False,
            "canonical_state_changed": False,
            "projection_state": {"status": "projection_stale", "reason": "write_not_executed"},
        }
    gate_error = _projection_write_gate_error(job, client=client)
    if gate_error:
        return projection_write_failure_marker(job, reason=gate_error, status="blocked_approval_required")
    try:
        response = client.upsert_memory_card(
            job["payload"], idempotency_key=str(job.get("idempotency_key") or "")
        )
    except Exception as exc:
        return projection_write_failure_marker(job, reason=exc.__class__.__name__)
    return {
        "schema_version": "llm_brain_projection_execution_result.v1",
        "job_id": str(job.get("job_id") or ""),
        "status": "projected",
        "write_performed": True,
        "canonical_state_changed": False,
        "projection_state": {"status": "fresh", "reason": "index_upsert_ok"},
        "index_response": response,
    }


def projection_lag_marker(card: Mapping[str, Any], *, reason: str = "projection_lag") -> dict:
    memory_id = str(card.get("memory_id") or "")
    return {
        "memory_id": memory_id,
        "conflict_type": "projection_stale",
        "winner": "local_ledger",
        "reason": reason,
        "canonical_state_changed": False,
    }


def projection_write_failure_marker(
    job: Mapping[str, Any], *, reason: str, status: str = "write_failed"
) -> dict:
    return {
        "schema_version": "llm_brain_projection_execution_result.v1",
        "job_id": str(job.get("job_id") or ""),
        "status": status,
        "write_performed": False,
        "canonical_state_changed": False,
        "projection_state": {"status": "projection_stale", "reason": reason},
    }


def _require_projectable_card(card: Mapping[str, Any]) -> dict:
    projectable = validate_memory_card_envelope(card)
    if projectable["lifecycle_state"] not in PROJECTABLE_LIFECYCLE_STATES:
        raise ValueError("only accepted MemoryCards can be projected")
    if projectable["approval_state"] not in PROJECTABLE_APPROVAL_STATES:
        raise ValueError("only approved MemoryCards can be projected")
    return projectable


def _payload_hash(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _projection_filename(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return f"llm-brain-memory-card-{digest}.md"


def _find_existing_projection_document(retired_index_bridge: Any, dataset_id: str, filename: str) -> str:
    documents = retired_index_bridge.list_documents(dataset_id, keywords=filename, page=1, page_size=20)
    for document in documents:
        if not isinstance(document, Mapping):
            continue
        name = str(document.get("name") or document.get("filename") or document.get("file_name") or "")
        if name != filename:
            continue
        return str(document.get("id") or document.get("document_id") or "")
    return ""


def _projection_metadata(payload: Mapping[str, Any], *, idempotency_key: str) -> dict:
    source_metadata = payload.get("metadata")
    metadata = dict(source_metadata) if isinstance(source_metadata, Mapping) else {}
    metadata.update(
        {
            "schema_version": str(payload.get("schema_version") or ""),
            "memory_id": str(payload.get("memory_id") or ""),
            "brain_id": str(payload.get("brain_id") or ""),
            "content_hash": str(payload.get("content_hash") or ""),
            "idempotency_key": idempotency_key,
        }
    )
    return metadata


def _projection_write_gate_error(job: Mapping[str, Any], *, client: Any) -> str:
    approval = job.get("approval_record")
    if not isinstance(approval, Mapping):
        return "missing_projection_approval_record"
    if approval.get("approved") is not True:
        return "projection_approval_not_approved"
    if str(approval.get("operation") or "") != "index_projection_write":
        return "projection_approval_operation_mismatch"
    if str(approval.get("idempotency_key") or "") != str(job.get("idempotency_key") or ""):
        return "projection_approval_idempotency_key_mismatch"
    if str(approval.get("dry_run_status") or "") != "dry_run":
        return "projection_approval_requires_dry_run_status"
    dataset_id = str(getattr(client, "dataset_id", "") or "")
    if dataset_id:
        approved_dataset = str(approval.get("dataset_id") or "")
        allowed = [str(value) for value in approval.get("allowed_dataset_ids") or []]
        if approved_dataset and approved_dataset != dataset_id:
            return "projection_approval_dataset_mismatch"
        if allowed and dataset_id not in allowed:
            return "projection_approval_dataset_not_allowed"
    return ""


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
