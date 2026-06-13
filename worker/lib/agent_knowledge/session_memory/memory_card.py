from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from ..redaction import redact_text_v2
from .transcript_model import bound_text


CANDIDATE_TYPES = (
    "semantic_fact",
    "user_preference",
    "project_decision",
    "procedural_rule",
    "tool_skill",
    "unresolved_task",
    "risk_or_constraint",
)
MAX_MEMORY_STATEMENT_CHARS = 240
PROFILE_CHANGING_TYPES = {"user_preference"}

MEMORY_CARD_TYPES = (
    "decision",
    "task",
    "drift",
    "preference",
    "status",
    "evidence",
)
LIFECYCLE_STATES = (
    "candidate",
    "suggested_accept",
    "human_accepted",
    "human_rejected",
    "auto_accepted",
    "needs_review",
    "accepted",
    "rejected",
)
JUDGMENT_STATES = (
    "none",
    "suggested_status",
    "suggested_superseded",
    "auto_status",
    "needs_review",
)
APPROVAL_STATES = ("suggested", "approved", "rejected", "auto_accepted", "needs_review")
GOVERNANCE_TIERS = ("low", "medium", "high")
FRESHNESS_VALUES = ("current", "recent", "historical", "unknown")
CURRENTNESS_VALUES = ("current", "stale", "superseded", "conflicted", "unknown")

_COMMON_MEMORY_CARD_FIELDS = (
    "memory_id",
    "brain_id",
    "card_type",
    "scope",
    "project",
    "provider",
    "title",
    "summary",
    "render_text",
    "lifecycle_state",
    "judgment_state",
    "status",
    "approval_state",
    "governance_tier",
    "freshness",
    "currentness",
    "confidence",
    "confidence_basis",
    "source_refs",
    "evidence_refs",
    "evidence_hashes",
    "derived_from",
    "supersedes",
    "superseded_by",
    "conflicts",
    "active_until",
    "typed_payload",
)
_TYPED_PAYLOAD_REQUIRED_FIELDS = {
    "decision": ("decision", "rationale", "alternatives", "consequence", "authority_ref"),
    "task": ("task_state", "next_action", "blocker", "owner_hint", "status"),
    "drift": (
        "subject",
        "expected_state",
        "observed_state",
        "drift_kind",
        "severity",
        "authority_lane",
        "source_precedence_rank",
        "resolution_action",
        "suggested_action",
        "basis_refs",
    ),
    "preference": (
        "preference",
        "explicitness",
        "repeated_count",
        "confirmation_status",
        "applies_to",
    ),
    "status": ("status_value", "observed_at", "expires_at", "current_authority"),
    "evidence": ("evidence_kind", "result_status", "hash_refs", "count_refs"),
}
_DRIFT_SEVERITIES = ("low", "medium", "high")
_DRIFT_AUTHORITY_LANES = ("design", "implementation", "runtime", "governance")
_DRIFT_RESOLUTION_ACTIONS = (
    "keep_current",
    "mark_superseded",
    "update_projection",
    "needs_review",
)
_PREFERENCE_EXPLICITNESS = ("explicit", "inferred")
_PREFERENCE_CONFIRMATION = ("unconfirmed", "confirmed", "rejected")
_EVIDENCE_KINDS = ("commit", "merge", "runtime", "user_approval", "transcript", "document")
_EVIDENCE_RESULT_STATUSES = ("pass", "fail", "mixed", "unknown")
_REASON_CAPSULE_FIELDS = (
    "rule_hits",
    "deterministic_signals",
    "evidence_gap",
    "model_reason",
    "policy_version",
    "evaluator_version",
    "review_block_reason",
)
_JUDGMENTS_REQUIRING_REASON = {
    "suggested_status",
    "suggested_superseded",
    "auto_status",
    "needs_review",
}
_LIFECYCLES_REQUIRING_REASON = {"suggested_accept", "needs_review"}
_BUNDLE_FIELDS = (
    "judgment_id",
    "memory_id",
    "source_refs",
    "span_refs",
    "redacted_summary",
    "evidence_hashes",
    "deterministic_signals",
    "model_reason",
    "confidence",
    "policy_version",
    "evaluator_version",
)
_FEEDBACK_FIELDS = (
    "feedback_id",
    "decision_id",
    "memory_id",
    "repo_id",
    "artifact_id",
    "proposed_status",
    "final_status",
    "user_action",
    "user_reason",
    "corrected_status",
    "correction_reason",
    "model_reason",
    "confidence",
    "deterministic_signals",
    "evidence_snapshot",
    "source_refs",
    "conflict_state",
    "policy_version",
    "evaluator_version",
    "timestamp",
)

_FORBIDDEN_REF_KEYS = {
    "body",
    "content",
    "excerpt",
    "path",
    "raw",
    "raw_excerpt",
    "raw_text",
    "raw_transcript",
    "secret",
    "text",
    "token",
    "uri",
    "url",
}
_FORBIDDEN_TEXT_PATTERNS = (
    re.compile(r"/Users/", re.IGNORECASE),
    re.compile(r"(^|\s)~/"),
    re.compile(r"(^|\s)/private/", re.IGNORECASE),
    re.compile(r"\braw[_ -]?transcript[_ -]?(body|text|content)?\b", re.IGNORECASE),
    re.compile(r"\b(private transcript|transcript excerpt)\b", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{8,}", re.IGNORECASE),
    re.compile(r"\b[A-Z0-9_]*(TOKEN|SECRET|API_KEY|PASSWORD|PASSWD)\b\s*[:=]", re.IGNORECASE),
    re.compile(r"\b(password|passwd|secret|token|api[_-]?key)\b\s*[:=]", re.IGNORECASE),
)


def build_memory_candidate(
    *,
    candidate_type: str,
    statement: str,
    project: str,
    provider: str,
    evidence_refs: list[dict],
    sensitivity: str | None = None,
) -> dict:
    if candidate_type not in CANDIDATE_TYPES:
        raise ValueError(f"unsupported memory candidate type: {candidate_type}")
    bounded_statement = bound_text(" ".join(redact_text_v2(statement).split()), MAX_MEMORY_STATEMENT_CHARS)
    safe_evidence_refs = _safe_evidence_refs(evidence_refs)
    sensitivity = sensitivity or ("profile_changing" if candidate_type in PROFILE_CHANGING_TYPES else "normal")
    content_hash = _sha256("|".join([candidate_type, project, provider, bounded_statement]))
    evidence_hashes = ",".join(ref["content_hash"] for ref in safe_evidence_refs)
    candidate_id = "cand_" + _sha256("|".join([content_hash, evidence_hashes])).split(":", 1)[1][:16]
    return {
        "candidate_id": candidate_id,
        "candidate_type": candidate_type,
        "project": project,
        "provider": provider,
        "statement": bounded_statement,
        "content_hash": content_hash,
        "sensitivity": sensitivity,
        "requires_manual_approval": sensitivity != "normal" or candidate_type in PROFILE_CHANGING_TYPES,
        "approval_state": "pending",
        "evidence_refs": safe_evidence_refs,
    }


def build_memory_card(candidate: dict, *, approved_by: str, supersedes: str = "") -> dict:
    if candidate.get("approval_state") not in {"pending", "approved"}:
        raise ValueError("only pending or approved candidates can become memory cards")
    memory_id = "mem_" + candidate["content_hash"].split(":", 1)[1][:16]
    return {
        "memory_id": memory_id,
        "candidate_id": candidate["candidate_id"],
        "card_type": candidate["candidate_type"],
        "project": candidate["project"],
        "provider": candidate["provider"],
        "title": _title_for_type(candidate["candidate_type"]),
        "summary": candidate["statement"],
        "content_hash": candidate["content_hash"],
        "state": "active",
        "approved_by": approved_by,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "supersedes": supersedes,
    }


def validate_memory_card_envelope(card: Mapping[str, Any]) -> dict:
    """Validate the LLM-brain MemoryCard v1 envelope without mutating storage."""

    if not isinstance(card, Mapping):
        raise ValueError("MemoryCard must be an object")
    normalized = dict(card)
    _require_fields(normalized, _COMMON_MEMORY_CARD_FIELDS, "MemoryCard")
    _require_enum(normalized, "card_type", MEMORY_CARD_TYPES)
    _require_enum(normalized, "lifecycle_state", LIFECYCLE_STATES)
    _require_enum(normalized, "judgment_state", JUDGMENT_STATES)
    _require_enum(normalized, "approval_state", APPROVAL_STATES)
    _require_enum(normalized, "governance_tier", GOVERNANCE_TIERS)
    _require_enum(normalized, "freshness", FRESHNESS_VALUES)
    _require_enum(normalized, "currentness", CURRENTNESS_VALUES)
    _require_number(normalized.get("confidence"), "confidence")
    _ensure_no_forbidden_content(normalized.get("summary"), "summary")
    _ensure_no_forbidden_content(normalized.get("render_text"), "render_text")
    _validate_locator_list(normalized.get("source_refs"), "source_refs")
    _validate_locator_list(normalized.get("evidence_refs"), "evidence_refs")
    _validate_hash_list(normalized.get("evidence_hashes"), "evidence_hashes")
    _require_list(normalized.get("derived_from"), "derived_from")
    _require_list(normalized.get("supersedes"), "supersedes")
    _require_list(normalized.get("superseded_by"), "superseded_by")
    _require_list(normalized.get("conflicts"), "conflicts")
    validate_typed_payload(
        str(normalized["card_type"]), normalized.get("typed_payload"), field_name="typed_payload"
    )
    validate_memory_card_state_invariants(normalized)
    reason_required = (
        normalized["judgment_state"] in _JUDGMENTS_REQUIRING_REASON
        or normalized["lifecycle_state"] in _LIFECYCLES_REQUIRING_REASON
    )
    if reason_required:
        validate_reason_capsule(normalized.get("reason_capsule"))
    return normalized


def validate_typed_payload(card_type: str, payload: Mapping[str, Any], *, field_name: str = "payload") -> dict:
    if card_type not in MEMORY_CARD_TYPES:
        raise ValueError(f"unsupported MemoryCard card_type: {card_type}")
    if not isinstance(payload, Mapping):
        raise ValueError(f"{field_name} must be an object")
    normalized = dict(payload)
    _require_fields(normalized, _TYPED_PAYLOAD_REQUIRED_FIELDS[card_type], field_name)
    _ensure_no_forbidden_content(normalized, field_name)
    if card_type == "drift":
        _require_enum(normalized, "severity", _DRIFT_SEVERITIES, owner=field_name)
        _require_enum(normalized, "authority_lane", _DRIFT_AUTHORITY_LANES, owner=field_name)
        _require_enum(normalized, "resolution_action", _DRIFT_RESOLUTION_ACTIONS, owner=field_name)
        _require_number(normalized.get("source_precedence_rank"), f"{field_name}.source_precedence_rank")
        _validate_locator_list(normalized.get("basis_refs"), f"{field_name}.basis_refs")
    elif card_type == "preference":
        _require_enum(normalized, "explicitness", _PREFERENCE_EXPLICITNESS, owner=field_name)
        _require_enum(normalized, "confirmation_status", _PREFERENCE_CONFIRMATION, owner=field_name)
        _require_number(normalized.get("repeated_count"), f"{field_name}.repeated_count")
    elif card_type == "evidence":
        _require_enum(normalized, "evidence_kind", _EVIDENCE_KINDS, owner=field_name)
        _require_enum(normalized, "result_status", _EVIDENCE_RESULT_STATUSES, owner=field_name)
        _validate_hash_list(normalized.get("hash_refs"), f"{field_name}.hash_refs")
        _require_list(normalized.get("count_refs"), f"{field_name}.count_refs")
    return normalized


def validate_memory_card_state_invariants(card: Mapping[str, Any]) -> None:
    lifecycle_state = str(card.get("lifecycle_state") or "")
    judgment_state = str(card.get("judgment_state") or "")
    approval_state = str(card.get("approval_state") or "")
    currentness = str(card.get("currentness") or "")
    if lifecycle_state == "accepted" and approval_state == "suggested":
        raise ValueError("accepted MemoryCard cannot have suggested approval_state")
    if lifecycle_state == "accepted" and judgment_state == "needs_review":
        raise ValueError("accepted MemoryCard cannot have needs_review judgment_state")
    if approval_state == "auto_accepted" and card.get("auto_policy_ready") is not True:
        raise ValueError("auto_accepted requires auto_policy_ready=true")
    if currentness == "superseded" and not card.get("superseded_by") and judgment_state != "suggested_superseded":
        raise ValueError("superseded MemoryCard requires superseded_by or suggested_superseded")
    if judgment_state == "auto_status":
        reason_capsule = card.get("reason_capsule")
        if not isinstance(reason_capsule, Mapping) or not reason_capsule.get("policy_version"):
            raise ValueError("auto_status requires reason_capsule.policy_version")


def validate_reason_capsule(capsule: Mapping[str, Any] | None) -> dict:
    if not isinstance(capsule, Mapping):
        raise ValueError("reason_capsule must be an object")
    normalized = dict(capsule)
    _require_fields(normalized, _REASON_CAPSULE_FIELDS, "reason_capsule")
    _require_list(normalized.get("rule_hits"), "reason_capsule.rule_hits")
    _require_list(normalized.get("deterministic_signals"), "reason_capsule.deterministic_signals")
    _require_list(normalized.get("evidence_gap"), "reason_capsule.evidence_gap")
    _ensure_no_forbidden_content(normalized, "reason_capsule")
    if not str(normalized.get("policy_version") or ""):
        raise ValueError("reason_capsule.policy_version is required")
    if not str(normalized.get("evaluator_version") or ""):
        raise ValueError("reason_capsule.evaluator_version is required")
    return normalized


def validate_judgment_basis_bundle(bundle: Mapping[str, Any]) -> dict:
    if not isinstance(bundle, Mapping):
        raise ValueError("judgment basis bundle must be an object")
    normalized = dict(bundle)
    _require_fields(normalized, _BUNDLE_FIELDS, "judgment_basis_bundle")
    _validate_locator_list(normalized.get("source_refs"), "judgment_basis_bundle.source_refs")
    _validate_locator_list(normalized.get("span_refs"), "judgment_basis_bundle.span_refs")
    _validate_hash_list(normalized.get("evidence_hashes"), "judgment_basis_bundle.evidence_hashes")
    _require_list(normalized.get("deterministic_signals"), "judgment_basis_bundle.deterministic_signals")
    _require_number(normalized.get("confidence"), "judgment_basis_bundle.confidence")
    _ensure_no_forbidden_content(normalized, "judgment_basis_bundle")
    return normalized


def validate_feedback_record(record: Mapping[str, Any]) -> dict:
    if not isinstance(record, Mapping):
        raise ValueError("feedback record must be an object")
    normalized = dict(record)
    _require_fields(normalized, _FEEDBACK_FIELDS, "feedback_record")
    _require_enum(normalized, "user_action", ("approve", "reject", "correct"), owner="feedback_record")
    _require_enum(
        normalized,
        "conflict_state",
        ("none", "projection_stale", "conflict"),
        owner="feedback_record",
    )
    _require_number(normalized.get("confidence"), "feedback_record.confidence")
    _require_list(normalized.get("deterministic_signals"), "feedback_record.deterministic_signals")
    _validate_locator_list(normalized.get("source_refs"), "feedback_record.source_refs")
    _ensure_no_forbidden_content(normalized, "feedback_record")
    return normalized


def validate_source_locator(ref: Any, *, field_name: str = "source_ref") -> Any:
    if isinstance(ref, str):
        if not ref.strip():
            raise ValueError(f"{field_name} must not be empty")
        _ensure_no_forbidden_content(ref, field_name)
        return ref
    if not isinstance(ref, Mapping):
        raise ValueError(f"{field_name} must be an opaque string or object")
    for key, value in ref.items():
        key_text = str(key)
        if key_text in _FORBIDDEN_REF_KEYS:
            raise ValueError(f"{field_name}.{key_text} is not an opaque locator field")
        _ensure_no_forbidden_content(key_text, f"{field_name}.{key_text}")
        _ensure_no_forbidden_content(value, f"{field_name}.{key_text}")
    return dict(ref)


def _safe_evidence_refs(evidence_refs: list[dict]) -> list[dict]:
    safe_refs = []
    for ref in evidence_refs:
        knowledge_id = str(ref.get("knowledge_id") or "")
        content_hash = str(ref.get("content_hash") or "")
        if not knowledge_id or not content_hash.startswith("sha256:"):
            raise ValueError("memory evidence refs require knowledge_id and sha256 content_hash")
        safe_refs.append({"knowledge_id": knowledge_id, "content_hash": content_hash})
    if not safe_refs:
        raise ValueError("memory candidate requires at least one evidence ref")
    return safe_refs


def _title_for_type(candidate_type: str) -> str:
    return candidate_type.replace("_", " ").capitalize()


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_fields(value: Mapping[str, Any], fields: tuple[str, ...], owner: str) -> None:
    missing = [field for field in fields if field not in value]
    if missing:
        raise ValueError(f"{owner} missing required fields: {', '.join(missing)}")


def _require_enum(
    value: Mapping[str, Any],
    field: str,
    allowed: tuple[str, ...],
    *,
    owner: str = "MemoryCard",
) -> None:
    actual = value.get(field)
    if actual not in allowed:
        raise ValueError(f"{owner}.{field} must be one of: {', '.join(allowed)}")


def _require_number(value: Any, field_name: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number")
    if not 0 <= float(value) <= 1:
        raise ValueError(f"{field_name} must be between 0 and 1")


def _require_list(value: Any, field_name: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")


def _validate_locator_list(value: Any, field_name: str) -> list:
    _require_list(value, field_name)
    return [validate_source_locator(ref, field_name=f"{field_name}[]") for ref in value]


def _validate_hash_list(value: Any, field_name: str) -> None:
    _require_list(value, field_name)
    for item in value:
        if not isinstance(item, str) or not item.startswith("sha256:"):
            raise ValueError(f"{field_name} entries must be sha256 strings")


def _ensure_no_forbidden_content(value: Any, field_name: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            _ensure_no_forbidden_content(str(key), f"{field_name}.{key}")
            _ensure_no_forbidden_content(child, f"{field_name}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _ensure_no_forbidden_content(child, f"{field_name}[{index}]")
        return
    if value is None:
        return
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError(f"{field_name} must contain only scalar/list/object values")
    text = str(value)
    for pattern in _FORBIDDEN_TEXT_PATTERNS:
        if pattern.search(text):
            raise ValueError(f"{field_name} contains forbidden private/source content")
    if redact_text_v2(text) != text:
        raise ValueError(f"{field_name} contains redaction-required content")
