from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

PRODUCTION_OBJECT_AUTHORITY_WRITE_CAPABILITY = "production_object_authority_write"
SINGLE_BOUNDED_DENIAL_ACTION = "single_bounded_denial.v1"
SINGLE_OBJECT_PRODUCTION_AUTHORITY_CLASSES = ("RepoDocument", "ArtifactPreference")
ALLOWED_PRODUCTION_PROPOSAL_TYPES = (
    "propose_current",
    "propose_stale",
    "propose_supersede",
    "propose_retire",
    "request_evidence",
)
ALLOWED_PRODUCTION_DECISION_TYPES = (
    "accept_current",
    "reject_candidate",
    "commit_supersession",
    "commit_stale",
    "retire",
    "archive_only",
    "rollback_decision",
)
_ALLOWED_PERMISSION_ACTIONS = frozenset(
    {
        "brain_object_proposal_create",
        "brain_object_decision_commit",
        SINGLE_BOUNDED_DENIAL_ACTION,
    }
)
_REQUIRED_TRUE_FIELDS = (
    "configured_deployed_mcp_identity_matches_source",
    "read_after_write_smoke_plan",
    "rollback_or_supersession_plan",
    "no_raw_private_evidence",
)
_UNSAFE_PUBLIC_TEXT_RE = re.compile(
    r"(/Users/|~/|/private/|/Volumes/|[A-Za-z]:\\|\\\\[A-Za-z0-9_.-]+|"
    r"\bBearer\s+|\braw[_ -]?transcript\b|"
    r"\b[A-Z0-9_]*(?:TOKEN|SECRET|API_KEY|PASSWORD|PASSWD)\b\s*[:=])",
    re.IGNORECASE,
)
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_OBJECT_ID_RE = re.compile(
    r"^ko:(?:RepoDocument|ArtifactPreference):[A-Za-z0-9][A-Za-z0-9_.:-]*$"
)
_APPROVAL_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/#-]*$")
_TYPE_OR_ACTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def evaluate_production_object_authority_permission(
    arguments: Mapping[str, Any],
    *,
    capability: str,
    action: str,
    service_write_enabled: bool,
    ledger_read_only: bool,
) -> dict[str, Any]:
    """Pure canonical permission decision shared by product dispatch and audit."""

    if capability != PRODUCTION_OBJECT_AUTHORITY_WRITE_CAPABILITY:
        raise ValueError("unsupported production authority capability")
    if action not in _ALLOWED_PERMISSION_ACTIONS:
        raise ValueError("unsupported production authority action")
    if not isinstance(arguments, Mapping):
        raise ValueError("production authority arguments must be a mapping")

    gate_value = arguments.get("production_gate")
    gate_provided = isinstance(gate_value, Mapping)
    gate = gate_value if isinstance(gate_value, Mapping) else {}
    project, project_exact = _project_from_arguments(arguments)
    raw_target_object_id = str(arguments.get("target_object_id") or "")
    target_object_id, target_exact = _exact_identity(
        raw_target_object_id,
        max_chars=180,
        pattern=_OBJECT_ID_RE,
    )
    approval_ref, approval_ref_exact = _exact_identity(
        str(gate.get("approval_ref") or ""),
        max_chars=160,
        pattern=_APPROVAL_REF_RE,
    )
    missing: list[str] = []
    if not service_write_enabled:
        missing.append("service_production_object_authority_write_flag")
    if ledger_read_only:
        missing.append("writable_ledger")
    if gate.get("approved") is not True:
        missing.append("approved")
    if not approval_ref_exact:
        missing.append("approval_ref")
    if str(gate.get("scope") or "") != "single_project_single_object":
        missing.append("single_project_single_object_scope")
    gate_project, gate_project_exact = _exact_identity(
        str(gate.get("project") or ""),
        max_chars=120,
        pattern=_PROJECT_ID_RE,
    )
    if (
        not project_exact
        or not gate_project_exact
        or gate_project != project
    ):
        missing.append("project_scope_match")
    try:
        max_objects = int(gate.get("max_objects") or 0)
    except (TypeError, ValueError):
        max_objects = 0
    if max_objects != 1:
        missing.append("max_objects_1")
    for field in _REQUIRED_TRUE_FIELDS:
        if gate.get(field) is not True:
            missing.append(field)
    if not target_exact:
        missing.append("target_object_id_exact")
    if not is_allowed_object_target(target_object_id):
        missing.append(allowed_object_class_gap())

    proposed_object = arguments.get("proposed_object")
    if proposed_object is not None:
        if not isinstance(proposed_object, Mapping):
            missing.append("proposed_object_shape")
        else:
            raw_proposed_object_id = str(proposed_object.get("object_id") or "")
            proposed_object_id, proposed_object_id_exact = _exact_identity(
                raw_proposed_object_id,
                max_chars=180,
                pattern=_OBJECT_ID_RE,
            )
            proposed_object_type, proposed_object_type_exact = _exact_identity(
                str(proposed_object.get("object_type") or ""),
                max_chars=120,
                pattern=_TYPE_OR_ACTION_RE,
            )
            if not proposed_object_id_exact:
                missing.append("proposed_object_id_exact")
            if not proposed_object_type_exact:
                missing.append("proposed_object_type_exact")
            if (
                proposed_object_id != target_object_id
                or raw_proposed_object_id != raw_target_object_id
            ):
                missing.append("proposed_object_target_match")
            if not proposed_object_type or not is_allowed_object_target(
                target_object_id,
                object_type=proposed_object_type,
            ):
                missing.append(allowed_object_class_gap())
    if "proposal_type" in arguments:
        proposal_type, proposal_type_exact = _exact_identity(
            str(arguments.get("proposal_type") or ""),
            max_chars=120,
            pattern=_TYPE_OR_ACTION_RE,
        )
        if not proposal_type_exact or proposal_type not in ALLOWED_PRODUCTION_PROPOSAL_TYPES:
            missing.append("allowed_proposal_type")
    if "decision_type" in arguments:
        decision_type, decision_type_exact = _exact_identity(
            str(arguments.get("decision_type") or ""),
            max_chars=120,
            pattern=_TYPE_OR_ACTION_RE,
        )
        if not decision_type_exact or decision_type not in ALLOWED_PRODUCTION_DECISION_TYPES:
            missing.append("allowed_decision_type")

    normalized_missing = list(dict.fromkeys(missing))
    return {
        "allowed": not normalized_missing,
        "gate_provided": gate_provided,
        "missing_gate_evidence": normalized_missing,
        "approval_ref_hash": _sha256_text(approval_ref) if approval_ref else "",
        "project": project,
        "target_object_id": target_object_id,
    }


def _project_from_arguments(arguments: Mapping[str, Any]) -> tuple[str, bool]:
    raw_explicit = str(arguments.get("project") or "")
    explicit = raw_explicit.strip()
    if explicit:
        if explicit != raw_explicit:
            return "", False
        return _exact_identity(
            explicit,
            max_chars=120,
            pattern=_PROJECT_ID_RE,
        )
    repository = str(arguments.get("repository") or "").strip().rstrip("/\\")
    if not repository:
        return "", False
    project = _project_from_repository(repository.replace("\\", "/"))
    if project == "unknown":
        return "", False
    return _exact_identity(
        project,
        max_chars=120,
        pattern=_PROJECT_ID_RE,
    )


def _project_from_repository(repository: str) -> str:
    value = str(repository or "").rstrip("/")
    if not value:
        return "unknown"
    name = value.split("/")[-1]
    return name.removesuffix(".git") or "unknown"


def _exact_identity(
    value: str,
    *,
    max_chars: int,
    pattern: re.Pattern[str],
) -> tuple[str, bool]:
    raw = str(value or "")
    if (
        not raw
        or len(raw) > max_chars
        or " ".join(raw.split()) != raw
        or _UNSAFE_PUBLIC_TEXT_RE.search(raw)
        or pattern.fullmatch(raw) is None
    ):
        return "", False
    return raw, True


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def knowledge_object_class_from_id(object_id: str) -> str:
    parts = str(object_id or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "ko":
        return ""
    return parts[1]


def allowed_object_classes_list(
    classes: tuple[str, ...] = SINGLE_OBJECT_PRODUCTION_AUTHORITY_CLASSES,
) -> list[str]:
    return list(classes)


def allowed_object_class_gap(
    classes: tuple[str, ...] = SINGLE_OBJECT_PRODUCTION_AUTHORITY_CLASSES,
) -> str:
    return "allowed_object_class_" + "_or_".join(classes)


def is_allowed_object_target(
    object_id: str,
    *,
    object_type: str = "",
    classes: tuple[str, ...] = SINGLE_OBJECT_PRODUCTION_AUTHORITY_CLASSES,
) -> bool:
    object_class = knowledge_object_class_from_id(object_id)
    if object_class not in classes:
        return False
    return not object_type or object_type == object_class


__all__ = [
    "ALLOWED_PRODUCTION_DECISION_TYPES",
    "ALLOWED_PRODUCTION_PROPOSAL_TYPES",
    "PRODUCTION_OBJECT_AUTHORITY_WRITE_CAPABILITY",
    "SINGLE_OBJECT_PRODUCTION_AUTHORITY_CLASSES",
    "SINGLE_BOUNDED_DENIAL_ACTION",
    "allowed_object_class_gap",
    "allowed_object_classes_list",
    "evaluate_production_object_authority_permission",
    "is_allowed_object_target",
    "knowledge_object_class_from_id",
]
