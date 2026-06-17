from __future__ import annotations

import json
from pathlib import Path


class ApprovalError(ValueError):
    pass


def validate_memory_enqueue_approval(
    path: Path | str | None,
    *,
    operation: str,
    command_argv: list[str],
) -> dict:
    if not path:
        raise ApprovalError("approval is required")
    approval_path = Path(path)
    _reject_secret_approval_path(approval_path)
    try:
        payload = json.loads(approval_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ApprovalError("approval file not found") from exc
    except OSError as exc:
        raise ApprovalError("approval file could not be read") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ApprovalError("approval file must be valid JSON") from exc
    if payload.get("schema_version") != "agent_knowledge_live_approval.v1":
        raise ApprovalError("unsupported approval schema")
    if not _approval_operation_matches(payload, operation):
        raise ApprovalError("approval operation mismatch")
    operator_approval = payload.get("operator_approval") or {}
    if operator_approval.get("approved") is not True:
        raise ApprovalError("live approval is not approved")
    if payload.get("redaction_required") is not True:
        raise ApprovalError("redaction is required")
    if int(payload.get("timeout_seconds") or 0) <= 0:
        raise ApprovalError("timeout_seconds is required")
    if not payload.get("rollback_or_abort_criteria"):
        raise ApprovalError("abort criteria are required")
    approved_argv = (payload.get("command") or {}).get("argv")
    if approved_argv != command_argv:
        raise ApprovalError("approval argv mismatch")
    return payload


def validate_native_memory_sync_approval(
    path: Path | str | None,
    *,
    operation: str,
    memory_id: str,
    command_argv: list[str],
) -> dict:
    payload = validate_memory_enqueue_approval(path, operation=operation, command_argv=command_argv)
    target = payload.get("target") or {}
    if target.get("memory_id") != memory_id:
        raise ApprovalError("approval memory_id mismatch")
    return payload


def validate_goal3_live_approval(
    path: Path | str | None,
    *,
    operation: str,
    dataset_id: str,
    ragflow_base_url: str,
    command_argv: list[str],
    project: str | None = None,
    max_wait_seconds: float | None = None,
) -> dict:
    """Live-mutation approval gate for GC executors (dataset/url-bound + argv match).

    Vendored from the source monolith's live_smoke validator so the neuron GC
    runners (session_memory_gc / transcript_volume_gc / transcript_session_gc) can
    execute behind an operator-approved runtime contract. Reuses this module's
    schema/operation/secret-path helpers; binds the approval to the exact dataset,
    base URL, and argv so an unapproved or mismatched invocation fails closed.
    """
    if not path:
        raise ApprovalError("approval is required")
    approval_path = Path(path)
    _reject_secret_approval_path(approval_path)
    try:
        payload = json.loads(approval_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ApprovalError("approval file not found") from exc
    except OSError as exc:
        raise ApprovalError("approval file could not be read") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ApprovalError("approval file must be valid JSON") from exc
    if payload.get("schema_version") != "agent_knowledge_live_approval.v1":
        raise ApprovalError("unsupported approval schema")
    if not _approval_operation_matches(payload, operation):
        raise ApprovalError("approval operation mismatch")
    operator_approval = payload.get("operator_approval") or {}
    if operator_approval.get("approved") is not True:
        raise ApprovalError("live approval is not approved")
    if payload.get("redaction_required") is not True:
        raise ApprovalError("redaction is required")
    if not payload.get("rollback_or_abort_criteria"):
        raise ApprovalError("abort criteria are required")
    timeout_seconds = float(payload.get("timeout_seconds") or 0)
    if timeout_seconds <= 0:
        raise ApprovalError("timeout_seconds is required")
    target = payload.get("target") or {}
    if target.get("dataset_id") != dataset_id:
        raise ApprovalError("approval dataset_id mismatch")
    if str(target.get("ragflow_base_url") or "").rstrip("/") != ragflow_base_url.rstrip("/"):
        raise ApprovalError("approval ragflow_base_url mismatch")
    if project is not None and target.get("project") and target.get("project") != project:
        raise ApprovalError("approval project mismatch")
    if max_wait_seconds is not None and float(max_wait_seconds) > timeout_seconds:
        raise ApprovalError("timeout_seconds is below command wait bound")
    approved_argv = (payload.get("command") or {}).get("argv")
    if approved_argv != command_argv:
        raise ApprovalError("approval argv mismatch")
    return payload


def _reject_secret_approval_path(path: Path) -> None:
    parts = set(path.parts)
    if ".openclaw" in parts and "private" in parts:
        raise ApprovalError("approval path boundary rejected private OpenClaw path")
    if path.name == "secrets.json":
        raise ApprovalError("approval path boundary rejected secret approval path")


def _approval_operation_matches(payload: dict, operation: str) -> bool:
    if payload.get("operation") == operation:
        return True
    operations = payload.get("operations")
    return isinstance(operations, list) and operation in operations
