from __future__ import annotations

import copy
import math
import os
import re
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .ledger import Ledger, artifact_preference_decision_semantically_equal
from .llm_brain_core.document_bridge import RetiredIndexBridgeDocumentBridge
from .llm_brain_core.graph import GraphMemoryAdapter
from .llm_brain_core.ledger_adapter import LedgerSessionMemoryArtifactStore, LedgerSourceRefCatalog
from .llm_brain_core.runtime import build_runtime_brain_service
from .llm_brain_core.couchdb_projection_cli import _build_source_store, _project_ref
from .llm_brain_core.graph_projection_status_cli import build_graph_projection_status
from .llm_brain_core.objects.extraction_pipeline import (
    run_graph_search_projection_join_preview,
    run_source_to_candidate_graph_activation_preview,
)
from .llm_brain_core.objects.authority_policy import (
    allowed_object_class_gap,
    allowed_object_classes_list,
    is_allowed_object_target,
    knowledge_object_class_from_id,
)
from .llm_brain_core.objects.artifact_preference_evaluator import (
    evaluate_artifact_preference,
)
from .llm_brain_core.objects.knowledge_objects import AuthorityDecision, ReviewProposal
from .llm_brain_core.objects.object_packs import apply_approval_board_decisions, apply_candidate_review_edits
from .llm_brain_core.objects.runtime_readiness import (
    build_source_to_candidate_runtime_collected_shadow_evidence_packet,
    build_source_to_candidate_runtime_evidence_collection_plan,
    build_source_to_candidate_runtime_evidence_packet_template,
    build_source_to_candidate_runtime_post_deploy_capture_packet,
    build_source_to_candidate_runtime_post_deploy_capture_readiness_report,
    build_preference_artifact_memory_runtime_evidence,
    build_source_to_candidate_runtime_readiness_report,
    build_source_to_candidate_runtime_shadow_evidence_packet,
    build_source_to_candidate_runtime_shadow_readiness_report,
)
from .memory_read_pipeline import AuthorizedMemoryReader, MemoryReadPipeline, MemorySearchQuery
from .index_client import RetiredIndexBridgeHttpClient
from .public_safe_util import ensure_public_safe, public_safe_text, require_sha256, sha256_text, short_hash
from .session_memory.memory_card import validate_memory_card_envelope
from .session_memory.memory_promotion import commit_stale, commit_supersession
from .session_memory.brain_query import (
    SEMANTIC_RESULT_MIN_SCORE,
    resolve_brain_ids,
    run_brain_query_v2,
)
from .session_memory.brain_read_model import LegacyLedgerBrainReadModel, build_semantic_recall


class DisabledRetiredIndexBridgeClient:
    def retrieve(self, *args, **kwargs) -> list[dict]:
        return []

    def search_messages(self, *args, **kwargs) -> dict:
        return {"status_code": 200, "json": {"code": 0, "data": []}}


def _parse_utc_runtime_timestamp(value: object, *, field: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is missing")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} is invalid")
    return parsed.astimezone(timezone.utc)


def _native_semantic_memory_scores(hits: list[dict]) -> dict[str, float]:
    """Map only explicit MemoryCard semantic hits into a fail-closed ranker."""

    scores: dict[str, float] = {}
    for hit in hits:
        if not isinstance(hit, Mapping):
            continue
        score = hit.get("score")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
            or float(score) < SEMANTIC_RESULT_MIN_SCORE
        ):
            continue
        memory_id = str(hit.get("memory_id") or "")
        if not memory_id:
            session_tag = str(hit.get("session_tag") or "")
            if session_tag.startswith("mem:"):
                memory_id = session_tag.removeprefix("mem:")
        if not memory_id:
            continue
        scores[memory_id] = max(scores.get(memory_id, 0.0), float(score))
    return scores


_APPROVAL_BOARD_PRODUCTION_REQUIRED_TRUE_FIELDS = (
    "configured_deployed_mcp_identity_matches_source",
    "read_after_write_smoke_plan",
    "rollback_or_supersession_plan",
    "no_raw_private_evidence",
)

_ARTIFACT_PREFERENCE_PAYLOAD_FIELDS = frozenset(
    {
        "preference",
        "scope",
        "applies_to",
        "reason",
        "exceptions",
        "explicitness",
        "repeated_count",
        "confirmation_status",
        "currentness",
        "artifact_memory_kind",
        "source_memory_id",
        "raw_return_capability",
    }
)
_FORBIDDEN_SNAPSHOT_KEYS = frozenset(
    {
        "body",
        "content",
        "raw",
        "raw_body",
        "raw_content",
        "raw_source",
        "raw_text",
        "private",
        "private_path",
        "secret",
        "token",
        "api_key",
        "password",
        "dataset_id",
        "document_id",
    }
)
_FORBIDDEN_SNAPSHOT_COMPACT_KEYS = frozenset(key.replace("_", "") for key in _FORBIDDEN_SNAPSHOT_KEYS)
_RAW_EXTERNAL_REF_PREFIXES = (
    "dataset:",
    "dataset_id:",
    "document:",
    "document_id:",
    "ragflow_dataset:",
    "ragflow_document:",
)
_RAW_EXTERNAL_ID_ASSIGNMENT_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:ragflow_)?(?:dataset|document)(?:_?id)?\s*[:=]",
    re.IGNORECASE,
)
_ARTIFACT_PREFERENCE_EVIDENCE_REF_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]{1,63}:[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$"
)
_ARTIFACT_PREFERENCE_TEXT_PAYLOAD_FIELDS = _ARTIFACT_PREFERENCE_PAYLOAD_FIELDS - {
    "exceptions",
    "repeated_count",
}


def _approval_board_production_decision_state(action: str) -> tuple[str, str, str, str, str]:
    if action == "promote":
        return "propose_current", "accept_current", "accepted_current", "current", "accepted"
    return "", "", "", "", ""


def _reject_forbidden_snapshot_keys(value: Any, *, field: str = "proposed_object") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = _normalized_snapshot_key(key)
            compact_key = key_text.replace("_", "")
            if (
                key_text in _FORBIDDEN_SNAPSHOT_KEYS
                or compact_key in _FORBIDDEN_SNAPSHOT_COMPACT_KEYS
                or (
                    key_text.endswith("s")
                    and (
                        key_text[:-1] in _FORBIDDEN_SNAPSHOT_KEYS
                        or compact_key[:-1] in _FORBIDDEN_SNAPSHOT_COMPACT_KEYS
                    )
                )
            ):
                raise ValueError(f"{field} contains a forbidden field")
            _reject_forbidden_snapshot_keys(child, field=field)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_forbidden_snapshot_keys(child, field=field)


def _normalized_snapshot_key(value: Any) -> str:
    decoded = _fully_unquote(str(value).strip())
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", decoded)
    return re.sub(r"[^A-Za-z0-9]+", "_", snake).strip("_").casefold()


def _fully_unquote(value: str) -> str:
    decoded = value
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def _contains_raw_external_id(value: str) -> bool:
    decoded = _fully_unquote(value)
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", decoded)
    normalized = re.sub(r"[.\-\s]+", "_", snake).casefold()
    return decoded != value or _RAW_EXTERNAL_ID_ASSIGNMENT_RE.search(normalized) is not None


def _artifact_preference_payload_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a public-safe string")
    ensure_public_safe(value, field)
    if _contains_raw_external_id(value):
        raise ValueError(f"{field} must not contain a raw external ID")
    return value.strip()


def _artifact_preference_evidence_refs(refs: Any) -> list[str]:
    if refs is None:
        return []
    if not isinstance(refs, list):
        raise ValueError("ArtifactPreference evidence_refs must be a list of opaque string refs")
    safe_refs: list[str] = []
    for ref in refs:
        if not isinstance(ref, str):
            _reject_forbidden_snapshot_keys(ref, field="proposed_object.evidence_refs")
            raise ValueError("ArtifactPreference evidence_refs must contain opaque string refs")
        if ref.casefold().startswith(_RAW_EXTERNAL_REF_PREFIXES) or _contains_raw_external_id(ref):
            raise ValueError("ArtifactPreference evidence_refs must not contain raw external IDs")
        if _ARTIFACT_PREFERENCE_EVIDENCE_REF_RE.fullmatch(ref) is None:
            raise ValueError("ArtifactPreference evidence_refs must use internal opaque locator syntax")
        safe_ref = public_safe_text(ref, max_chars=180)
        if not safe_ref:
            raise ValueError("ArtifactPreference evidence_refs must not contain raw external IDs")
        safe_refs.append(safe_ref)
    return list(dict.fromkeys(safe_refs))


def _source_ref_record_view(record: Any) -> Mapping[str, Any]:
    if isinstance(record, Mapping):
        return record
    metadata = getattr(record, "metadata", None)
    if callable(metadata):
        value = metadata()
        if isinstance(value, Mapping):
            return value
    return {}


def _artifact_preference_source_refs(
    refs: Any,
    *,
    project: str,
    source_ref_lookup: Callable[[str], Any],
) -> list[dict[str, str]]:
    safe_refs: list[dict[str, str]] = []
    for ref in refs or []:
        if not isinstance(ref, Mapping):
            raise ValueError("ArtifactPreference source_refs require source_ref_id locators")
        _reject_forbidden_snapshot_keys(ref, field="proposed_object.source_refs")
        if set(ref) - {"source_ref_id", "content_hash"}:
            raise ValueError("ArtifactPreference source_refs allow only source_ref_id and content_hash")
        source_ref_id = public_safe_text(str(ref.get("source_ref_id") or ""), max_chars=180)
        if not source_ref_id:
            raise ValueError("ArtifactPreference source_refs require source_ref_id")
        record = _source_ref_record_view(source_ref_lookup(source_ref_id))
        if not record:
            raise ValueError("ArtifactPreference source_ref_id is not registered")
        if str(record.get("revoked_at") or ""):
            raise ValueError("ArtifactPreference source_ref_id permission is revoked")
        if str(record.get("deleted_at") or ""):
            raise ValueError("ArtifactPreference source_ref_id is deleted")
        if str(record.get("permission_scope") or "") != f"project:{project}":
            raise ValueError("ArtifactPreference source_ref_id project permission scope mismatch")
        catalog_hash = require_sha256(str(record.get("content_hash") or ""), "source_ref.content_hash")
        supplied_hash = str(ref.get("content_hash") or "")
        if supplied_hash and require_sha256(supplied_hash, "source_refs.content_hash") != catalog_hash:
            raise ValueError("ArtifactPreference source_ref content_hash mismatch")
        safe_refs.append({"source_ref_id": source_ref_id, "content_hash": catalog_hash})
    return list({ref["source_ref_id"]: ref for ref in safe_refs}.values())


def _artifact_preference_repeated_count(value: Any) -> int:
    if value is None:
        return 1
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("ArtifactPreference payload.repeated_count must be a positive integer")
    return value


def _artifact_preference_exceptions(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("ArtifactPreference payload.exceptions must be a list of public-safe strings")
    exceptions: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("ArtifactPreference payload.exceptions must be a list of public-safe strings")
        ensure_public_safe(item, "ArtifactPreference payload.exceptions")
        if _contains_raw_external_id(item):
            raise ValueError("ArtifactPreference payload.exceptions must not contain raw external IDs")
        safe_item = public_safe_text(item, max_chars=240)
        if not safe_item:
            raise ValueError("ArtifactPreference payload.exceptions must not contain empty strings")
        exceptions.append(safe_item)
    return exceptions


def _artifact_preference_snapshot(
    proposed_object: Mapping[str, Any],
    *,
    target_object_id: str,
    project: str,
    source_ref_lookup: Callable[[str], Any],
) -> dict[str, Any]:
    ensure_public_safe(dict(proposed_object), "artifact_preference_proposed_object")
    _reject_forbidden_snapshot_keys(proposed_object)
    object_id = public_safe_text(str(proposed_object.get("object_id") or ""), max_chars=180)
    object_type = public_safe_text(str(proposed_object.get("object_type") or ""), max_chars=120)
    if object_id != target_object_id:
        raise ValueError("ArtifactPreference proposed_object must match target_object_id")
    if object_type != "ArtifactPreference" or knowledge_object_class_from_id(object_id) != object_type:
        raise ValueError("ArtifactPreference object ID and type must be continuous")
    raw_scope = proposed_object.get("scope")
    if not isinstance(raw_scope, Mapping):
        raise ValueError("ArtifactPreference proposed_object.scope must be an object")
    scope = raw_scope
    scope_project = public_safe_text(str(scope.get("project") or ""), max_chars=120)
    if not project or scope_project != project:
        raise ValueError("ArtifactPreference proposed_object project scope must match proposal project")
    if str(proposed_object.get("privacy_class") or "") != "public_safe":
        raise ValueError("ArtifactPreference proposed_object privacy_class must be public_safe")
    title = _artifact_preference_payload_text(
        proposed_object.get("title"),
        field="ArtifactPreference proposed_object.title",
    )
    summary = _artifact_preference_payload_text(
        proposed_object.get("summary"),
        field="ArtifactPreference proposed_object.summary",
    )
    if not title or not summary:
        raise ValueError("ArtifactPreference proposed_object requires title and summary")
    content_hash = require_sha256(str(proposed_object.get("content_hash") or ""), "proposed_object.content_hash")
    raw_payload = proposed_object.get("payload")
    if not isinstance(raw_payload, Mapping):
        raise ValueError("ArtifactPreference proposed_object.payload must be an object")
    payload = raw_payload
    safe_payload = {
        str(key): copy.deepcopy(value)
        for key, value in payload.items()
        if str(key) in _ARTIFACT_PREFERENCE_PAYLOAD_FIELDS
    }
    for key in _ARTIFACT_PREFERENCE_TEXT_PAYLOAD_FIELDS:
        if key in safe_payload:
            safe_payload[key] = _artifact_preference_payload_text(
                safe_payload[key],
                field=f"ArtifactPreference payload.{key}",
            )
    preference = safe_payload.get("preference") or title
    applies_to = safe_payload.get("applies_to") or safe_payload.get("scope") or "project"
    if not preference or not applies_to:
        raise ValueError("ArtifactPreference proposed_object requires preference and scope")
    safe_payload["preference"] = preference
    safe_payload["applies_to"] = applies_to
    safe_payload["repeated_count"] = _artifact_preference_repeated_count(safe_payload.get("repeated_count"))
    safe_payload["exceptions"] = _artifact_preference_exceptions(safe_payload.get("exceptions"))
    evidence_refs = _artifact_preference_evidence_refs(proposed_object.get("evidence_refs"))
    source_refs = _artifact_preference_source_refs(
        proposed_object.get("source_refs"),
        project=project,
        source_ref_lookup=source_ref_lookup,
    )
    raw_confidence = proposed_object.get("confidence")
    if not isinstance(raw_confidence, Mapping):
        raise ValueError("ArtifactPreference proposed_object.confidence must be an object")
    confidence = raw_confidence
    score = confidence.get("score", 0.9)
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= float(score) <= 1:
        raise ValueError("ArtifactPreference proposed_object confidence.score must be between 0 and 1")
    snapshot = {
        "schema_version": "artifact_preference_proposed_object_snapshot.v1",
        "object_id": object_id,
        "object_type": object_type,
        "scope": {"project": scope_project},
        "title": title,
        "summary": summary,
        "content_hash": content_hash,
        "evidence_refs": evidence_refs,
        "source_refs": source_refs,
        "confidence": {
            "score": float(score),
            "basis": public_safe_text(
                _artifact_preference_payload_text(
                    confidence.get("basis") or "",
                    field="ArtifactPreference proposed_object.confidence.basis",
                ),
                max_chars=240,
            ),
        },
        "privacy_class": "public_safe",
        "payload": safe_payload,
    }
    ensure_public_safe(snapshot, "artifact_preference_proposed_object_snapshot")
    return snapshot


def _artifact_preference_memory_id(
    *,
    target_object_id: str,
    source_content_hash: str,
    proposal_id: str,
    decision_id: str,
) -> str:
    return "mem_artifact_preference_" + short_hash(
        [target_object_id, source_content_hash, proposal_id, decision_id],
        length=24,
    )


def _artifact_preference_cards_for_target(
    tx: Any,
    *,
    project: str,
    target_object_id: str,
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for card in tx.list_llm_brain_memory_cards(project=project):
        payload = card.get("typed_payload") if isinstance(card.get("typed_payload"), Mapping) else {}
        if (
            str(payload.get("source_object_type") or "") == "ArtifactPreference"
            and str(payload.get("target_object_id") or "") == target_object_id
        ):
            cards.append(card)
    return cards


def _current_artifact_preference_card_for_target(
    tx: Any,
    *,
    project: str,
    target_object_id: str,
) -> dict[str, Any] | None:
    current = [
        card
        for card in _artifact_preference_cards_for_target(
            tx,
            project=project,
            target_object_id=target_object_id,
        )
        if str(card.get("currentness") or "") == "current"
    ]
    if len(current) > 1:
        raise ValueError("ArtifactPreference target has multiple current canonical versions")
    return current[0] if current else None


def _artifact_preference_memory_card(
    *,
    proposal: Mapping[str, Any],
    decision: Mapping[str, Any],
    source_ref_lookup: Callable[[str], Any],
) -> dict[str, Any]:
    snapshot = proposal.get("proposed_object")
    if not isinstance(snapshot, Mapping):
        raise ValueError("ArtifactPreference accept_current requires proposed_object snapshot")
    target_object_id = str(decision.get("target_object_id") or "")
    project = str(decision.get("project") or "")
    snapshot = _artifact_preference_snapshot(
        snapshot,
        target_object_id=target_object_id,
        project=project,
        source_ref_lookup=source_ref_lookup,
    )
    payload = snapshot["payload"]
    approved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    evidence_refs = list(
        dict.fromkeys(
            [
                *[str(ref) for ref in snapshot.get("evidence_refs") or [] if str(ref or "")],
                *_artifact_preference_evidence_refs(proposal.get("evidence_refs")),
            ]
        )
    )
    card = {
        "memory_id": _artifact_preference_memory_id(
            target_object_id=target_object_id,
            source_content_hash=snapshot["content_hash"],
            proposal_id=str(proposal.get("proposal_id") or ""),
            decision_id=str(decision.get("decision_id") or ""),
        ),
        "brain_id": f"/project/{project}",
        "card_type": "preference",
        "scope": "project",
        "project": project,
        "provider": public_safe_text(str(proposal.get("proposer") or "codex"), max_chars=80) or "codex",
        "title": snapshot["title"],
        "summary": snapshot["summary"],
        "render_text": snapshot["summary"],
        "lifecycle_state": "human_accepted",
        "judgment_state": "none",
        "status": "accepted",
        "approval_state": "approved",
        "governance_tier": "medium",
        "freshness": "current",
        "currentness": "current",
        "confidence": float(snapshot["confidence"]["score"]),
        "confidence_basis": public_safe_text(
            str(snapshot["confidence"].get("basis") or "Object authority accept_current decision."),
            max_chars=240,
        ),
        "source_refs": list(snapshot.get("source_refs") or []),
        "evidence_refs": evidence_refs,
        "evidence_hashes": [snapshot["content_hash"]],
        "derived_from": [target_object_id, str(proposal.get("proposal_id") or "")],
        "supersedes": [],
        "superseded_by": [],
        "conflicts": [],
        "active_until": "",
        "approved_by": "redacted",
        "approved_at": approved_at,
        "typed_payload": {
            "preference": payload["preference"],
            "explicitness": str(payload.get("explicitness") or "explicit"),
            "repeated_count": payload["repeated_count"],
            "confirmation_status": str(payload.get("confirmation_status") or "confirmed"),
            "applies_to": payload["applies_to"],
            "reason": str(payload.get("reason") or snapshot["summary"]),
            "exceptions": payload["exceptions"],
            "target_object_id": target_object_id,
            "source_object_type": "ArtifactPreference",
            "source_content_hash": snapshot["content_hash"],
            "authority_proposal_id": public_safe_text(
                str(proposal.get("proposal_id") or ""),
                max_chars=180,
            ),
            "authority_decision_id": public_safe_text(
                str(decision.get("decision_id") or ""),
                max_chars=180,
            ),
        },
    }
    return validate_memory_card_envelope(card)


def _artifact_preference_supersession_cards(
    tx: Any,
    *,
    prior_card: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    project = str(decision.get("project") or "")
    prior_target = str(decision.get("target_object_id") or "")
    related_decision_id = str(decision.get("supersedes_decision_id") or "")
    if not related_decision_id:
        raise ValueError("ArtifactPreference commit_supersession requires related successor decision")
    related = tx.get_object_authority_decision(related_decision_id)
    if not related:
        raise ValueError("ArtifactPreference commit_supersession related decision was not found")
    successor_target = str(related.get("target_object_id") or "")
    if not successor_target or successor_target == prior_target:
        raise ValueError("ArtifactPreference commit_supersession requires distinct prior and successor targets")
    if str(related.get("project") or "") != project:
        raise ValueError("ArtifactPreference commit_supersession related decision project mismatch")
    if knowledge_object_class_from_id(prior_target) != "ArtifactPreference" or knowledge_object_class_from_id(
        successor_target
    ) != "ArtifactPreference":
        raise ValueError("ArtifactPreference commit_supersession related decision type mismatch")
    if str(related.get("decision_type") or "") != "accept_current" or str(
        related.get("new_authority_lane") or ""
    ) != "accepted_current":
        raise ValueError("ArtifactPreference commit_supersession related decision is not current authority")

    successor_card = _current_artifact_preference_card_for_target(
        tx,
        project=project,
        target_object_id=successor_target,
    )
    if successor_card is None:
        raise ValueError("ArtifactPreference commit_supersession successor MemoryCard was not found")
    _validate_current_artifact_preference_lineage(
        tx,
        card=prior_card,
        target_object_id=prior_target,
        project=project,
        label="prior",
    )
    _validate_current_artifact_preference_lineage(
        tx,
        card=successor_card,
        target_object_id=successor_target,
        project=project,
        label="successor",
        expected_decision_id=related_decision_id,
    )
    prior_payload = prior_card.get("typed_payload") if isinstance(prior_card.get("typed_payload"), Mapping) else {}
    successor_payload = (
        successor_card.get("typed_payload")
        if isinstance(successor_card.get("typed_payload"), Mapping)
        else {}
    )
    if (
        str(prior_card.get("project") or "") != project
        or str(prior_payload.get("target_object_id") or "") != prior_target
        or str(prior_payload.get("source_object_type") or "") != "ArtifactPreference"
    ):
        raise ValueError("ArtifactPreference prior MemoryCard continuity mismatch")
    if (
        str(successor_card.get("project") or "") != project
        or str(successor_payload.get("target_object_id") or "") != successor_target
        or str(successor_payload.get("source_object_type") or "") != "ArtifactPreference"
        or str(successor_payload.get("authority_decision_id") or "") != related_decision_id
        or str(successor_card.get("currentness") or "") != "current"
    ):
        raise ValueError("ArtifactPreference successor MemoryCard continuity mismatch")

    demoted = commit_supersession(prior_card, superseded_by=str(successor_card["memory_id"]))
    successor = copy.deepcopy(successor_card)
    successor["supersedes"] = list(
        dict.fromkeys([*[str(item) for item in successor.get("supersedes") or []], str(prior_card["memory_id"])])
    )
    return validate_memory_card_envelope(demoted), validate_memory_card_envelope(successor)


def _validate_current_artifact_preference_lineage(
    tx: Any,
    *,
    card: Mapping[str, Any] | None,
    target_object_id: str,
    project: str,
    label: str,
    expected_decision_id: str = "",
) -> None:
    state = tx.get_object_authority_state(target_object_id)
    payload = card.get("typed_payload") if isinstance(card, Mapping) and isinstance(card.get("typed_payload"), Mapping) else {}
    card_decision_id = str(payload.get("authority_decision_id") or "")
    card_proposal_id = str(payload.get("authority_proposal_id") or "")
    if (
        not isinstance(card, Mapping)
        or str(card.get("project") or "") != project
        or str(card.get("currentness") or "") != "current"
        or str(payload.get("source_object_type") or "") != "ArtifactPreference"
        or str(payload.get("target_object_id") or "") != target_object_id
        or not card_decision_id
        or not card_proposal_id
        or (expected_decision_id and card_decision_id != expected_decision_id)
        or str(state.get("project") or "") != project
        or str(state.get("target_object_id") or "") != target_object_id
        or str(state.get("authority_lane") or "") != "accepted_current"
        or str(state.get("decision_id") or "") != card_decision_id
        or str(state.get("proposal_id") or "") != card_proposal_id
    ):
        raise ValueError(f"ArtifactPreference {label} current lineage mismatch")
    accepted = tx.get_object_authority_decision(card_decision_id)
    if (
        not accepted
        or str(accepted.get("project") or "") != project
        or str(accepted.get("target_object_id") or "") != target_object_id
        or str(accepted.get("proposal_id") or "") != card_proposal_id
        or str(accepted.get("decision_type") or "") != "accept_current"
        or str(accepted.get("new_authority_lane") or "") != "accepted_current"
    ):
        raise ValueError(f"ArtifactPreference {label} accepted decision lineage mismatch")


def _validate_artifact_preference_rollback_lineage(
    tx: Any,
    *,
    card: Mapping[str, Any] | None,
    decision: Mapping[str, Any],
) -> None:
    if (
        str(decision.get("previous_authority_lane") or "") != "accepted_current"
        or str(decision.get("new_authority_lane") or "") != "archive_only"
    ):
        raise ValueError("ArtifactPreference rollback requires accepted_current to archive_only transition")
    rollback_of_decision_id = str(decision.get("rollback_of_decision_id") or "")
    referenced = tx.get_object_authority_decision(rollback_of_decision_id)
    target_object_id = str(decision.get("target_object_id") or "")
    project = str(decision.get("project") or "")
    ledger_scope = str(decision.get("ledger_scope") or "")
    if not referenced:
        raise ValueError("ArtifactPreference rollback decision was not found")
    if (
        str(referenced.get("target_object_id") or "") != target_object_id
        or str(referenced.get("project") or "") != project
        or str(referenced.get("ledger_scope") or "") != ledger_scope
        or str(referenced.get("decision_type") or "") != "accept_current"
        or str(referenced.get("new_authority_lane") or "") != "accepted_current"
    ):
        raise ValueError("ArtifactPreference rollback decision lineage mismatch")
    _validate_current_artifact_preference_lineage(
        tx,
        card=card,
        target_object_id=target_object_id,
        project=project,
        label="rollback",
        expected_decision_id=rollback_of_decision_id,
    )


def _validate_completed_artifact_preference_decision_retry(
    tx: Any,
    *,
    proposal: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> dict[str, Any] | None:
    decision_id = str(decision.get("decision_id") or "")
    stored = tx.get_object_authority_decision(decision_id)
    if not stored:
        return None
    decision_type = str(stored.get("decision_type") or "")
    if decision_type not in {"rollback_decision", "commit_supersession"}:
        return None
    if not artifact_preference_decision_semantically_equal(stored, decision):
        raise ValueError("approved ArtifactPreference decision lineage is immutable")

    target_object_id = str(stored.get("target_object_id") or "")
    project = str(stored.get("project") or "")
    proposal_id = str(stored.get("proposal_id") or "")
    expected_lane = "archive_only" if decision_type == "rollback_decision" else "accepted_non_current"
    expected_status = "rolled_back" if decision_type == "rollback_decision" else "superseded"
    state = tx.get_object_authority_state(target_object_id)
    if (
        str(stored.get("previous_authority_lane") or "") != "accepted_current"
        or str(stored.get("new_authority_lane") or "") != expected_lane
        or str(proposal.get("proposal_id") or "") != proposal_id
        or str(proposal.get("target_object_id") or "") != target_object_id
        or str(proposal.get("project") or "") != project
        or str(proposal.get("status") or "") != expected_status
        or str(proposal.get("decision_id") or "") != decision_id
        or str(state.get("project") or "") != project
        or str(state.get("target_object_id") or "") != target_object_id
        or str(state.get("proposal_id") or "") != proposal_id
        or str(state.get("decision_id") or "") != decision_id
        or str(state.get("decision_type") or "") != decision_type
        or str(state.get("authority_lane") or "") != expected_lane
    ):
        raise ValueError("ArtifactPreference exact retry final authority lineage mismatch")

    cards = _artifact_preference_cards_for_target(
        tx,
        project=project,
        target_object_id=target_object_id,
    )
    if len(cards) != 1:
        raise ValueError("ArtifactPreference exact retry requires one canonical prior MemoryCard")
    prior_card = cards[0]
    prior_payload = (
        prior_card.get("typed_payload")
        if isinstance(prior_card.get("typed_payload"), Mapping)
        else {}
    )
    prior_decision_id = str(prior_payload.get("authority_decision_id") or "")
    prior_proposal_id = str(prior_payload.get("authority_proposal_id") or "")
    prior_decision = tx.get_object_authority_decision(prior_decision_id)
    if (
        str(prior_card.get("project") or "") != project
        or str(prior_payload.get("source_object_type") or "") != "ArtifactPreference"
        or str(prior_payload.get("target_object_id") or "") != target_object_id
        or not prior_decision
        or str(prior_decision.get("project") or "") != project
        or str(prior_decision.get("target_object_id") or "") != target_object_id
        or str(prior_decision.get("ledger_scope") or "") != str(stored.get("ledger_scope") or "")
        or str(prior_decision.get("decision_type") or "") != "accept_current"
        or str(prior_decision.get("new_authority_lane") or "") != "accepted_current"
        or str(prior_decision.get("proposal_id") or "") != prior_proposal_id
    ):
        raise ValueError("ArtifactPreference exact retry prior card lineage mismatch")

    if decision_type == "rollback_decision":
        if (
            str(stored.get("rollback_of_decision_id") or "") != prior_decision_id
            or str(prior_card.get("currentness") or "") != "stale"
            or any(str(card.get("currentness") or "") == "current" for card in cards)
        ):
            raise ValueError("ArtifactPreference rollback exact retry final lineage mismatch")
        return dict(stored)

    successor_decision_id = str(stored.get("supersedes_decision_id") or "")
    successor_decision = tx.get_object_authority_decision(successor_decision_id)
    successor_target = str(successor_decision.get("target_object_id") or "")
    successor_card = _current_artifact_preference_card_for_target(
        tx,
        project=project,
        target_object_id=successor_target,
    )
    _validate_current_artifact_preference_lineage(
        tx,
        card=successor_card,
        target_object_id=successor_target,
        project=project,
        label="successor retry",
        expected_decision_id=successor_decision_id,
    )
    if (
        str(prior_card.get("currentness") or "") != "superseded"
        or list(prior_card.get("superseded_by") or []) != [str(successor_card["memory_id"])]
        or str(prior_card["memory_id"]) not in list(successor_card.get("supersedes") or [])
    ):
        raise ValueError("ArtifactPreference supersession exact retry final lineage mismatch")
    return dict(stored)


def _resolve_artifact_preference_accept_card(
    tx: Any,
    *,
    proposal: Mapping[str, Any],
    decision: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any] | None:
    target_object_id = str(decision.get("target_object_id") or "")
    project = str(decision.get("project") or "")
    proposal_id = str(proposal.get("proposal_id") or "")
    decision_id = str(decision.get("decision_id") or "")
    cards = _artifact_preference_cards_for_target(
        tx,
        project=project,
        target_object_id=target_object_id,
    )
    state = tx.get_object_authority_state(target_object_id)
    exact = next((card for card in cards if card.get("memory_id") == candidate.get("memory_id")), None)
    stored_decision = tx.get_object_authority_decision(decision_id)
    if not cards and not state and not stored_decision:
        return dict(candidate)
    candidate_payload = (
        candidate.get("typed_payload") if isinstance(candidate.get("typed_payload"), Mapping) else {}
    )
    exact_payload = exact.get("typed_payload") if isinstance(exact, Mapping) and isinstance(exact.get("typed_payload"), Mapping) else {}
    if (
        exact is None
        or len(cards) != 1
        or str(exact.get("currentness") or "") != "current"
        or str(exact_payload.get("source_content_hash") or "")
        != str(candidate_payload.get("source_content_hash") or "")
        or str(exact_payload.get("authority_proposal_id") or "") != proposal_id
        or str(exact_payload.get("authority_decision_id") or "") != decision_id
        or str(state.get("project") or "") != project
        or str(state.get("authority_lane") or "") != "accepted_current"
        or str(state.get("proposal_id") or "") != proposal_id
        or str(state.get("decision_id") or "") != decision_id
    ):
        raise ValueError(
            "ArtifactPreference target already has an immutable canonical version; "
            "use a distinct target with explicit supersession"
        )
    if (
        not stored_decision
        or str(stored_decision.get("proposal_id") or "") != proposal_id
        or str(stored_decision.get("target_object_id") or "") != target_object_id
        or str(stored_decision.get("project") or "") != project
        or str(stored_decision.get("ledger_scope") or "") != str(decision.get("ledger_scope") or "")
        or str(stored_decision.get("decision_type") or "") != "accept_current"
        or str(stored_decision.get("new_authority_lane") or "") != "accepted_current"
    ):
        raise ValueError("ArtifactPreference exact retry decision lineage mismatch")
    return None


def build_index_client(
    *,
    index_url: str = "",
    token: str = "",
    policy_proxy_url: str = "",
) -> DisabledRetiredIndexBridgeClient:
    _ = (index_url, token, policy_proxy_url)
    return DisabledRetiredIndexBridgeClient()


class _SessionCardCache:
    """세션 안에서 승인된 MemoryCard를 (project, limit) 단위로 스냅샷한다.

    기존 brain tool 호출은 read model을 매번 다시 만들고 ledger에
    `list_accepted_cards`(승인 카드 전체 reload, limit=100)를 다시 질의했다.
    단일 stdio MCP 세션에서는 승인 카드 집합이 충분히 안정적이므로, 세션 생명주기
    동안 결과를 메모이즈해 반복 호출을 (project, limit)별 ledger read 1회로 줄인다.
    실제 read model을 감싸며 나머지 read path는 그대로 전달하므로 graph 상태,
    evidence policy, 다른 조회 경로는 건드리지 않는다.

    stale 범위: 현재 노출된 brain tool은 모두 read-only이고 세션 내부 write path가
    없으므로 세션 동안 스냅샷은 유효하다. 다른 프로세스(worker/ingestion)가 같은
    ledger에 쓰는 변경은 세션 재시작 전까지 반영되지 않는다. cross-process 또는 TTL
    invalidation은 없다. `invalidate()`는 향후 세션 내부 write path가 생기면 호출할
    명시적 refresh seam이다. production wrapper인 `invalidate_brain_card_cache`는
    아직 production caller가 없고, 현재는 테스트에서만 닿는다.
    """

    def __init__(self, read_model) -> None:
        self._read_model = read_model
        self._cards: dict[tuple[str, int], list[dict]] = {}

    def list_accepted_cards(self, *, project: str, limit: int) -> list[dict]:
        key = (str(project), int(limit))
        cached = self._cards.get(key)
        if cached is None:
            cached = self._read_model.list_accepted_cards(project=project, limit=limit)
            self._cards[key] = cached
        # downstream consumer가 list뿐 아니라 card 내부 dict/list까지 mutate해도
        # 공유 스냅샷이 오염되지 않도록 deep copy를 넘긴다. accepted-card window는
        # 작게 제한되어 있어(limit<=100) ledger read를 줄이는 이득에 비해 비용이 작다.
        return [copy.deepcopy(card) for card in cached]

    def invalidate(self) -> None:
        self._cards.clear()

    def __getattr__(self, name: str):
        # 캐시하지 않는 read-model 메서드는 감싼 모델로 그대로 위임한다.
        return getattr(self._read_model, name)


class KnowledgeSearchService:
    def __init__(
        self,
        *,
        ledger: Ledger,
        retired_index_bridge,
        dataset_ids: list[str],
        allow_private_results: bool = False,
        native_memory_id: str = "",
        graph_adapter: GraphMemoryAdapter | None = None,
        authorized_reader: AuthorizedMemoryReader | None = None,
        read_pipeline: AuthorizedMemoryReader | None = None,
        mirror_search=None,
        semantic_ranker=None,
        allow_restricted_steward: bool = False,
        allow_steward_auto_accept: bool = False,
        allow_local_test_object_authority_writes: bool = False,
        allow_production_object_authority_writes: bool = False,
    ):
        self.ledger = ledger
        self.retired_index_bridge = retired_index_bridge
        self.dataset_ids = dataset_ids
        self.allow_private_results = bool(allow_private_results)
        self.native_memory_id = native_memory_id
        self.graph_adapter = graph_adapter
        # Brain Steward restricted tools 는 기본적으로 막혀 있다. review_commit(approve/reject/
        # supersede_commit/stale_commit)과 가장 위험한 auto_accept 를 별도 flag 로 분리한다.
        # human/manual gate 또는 명시적 test-only path 에서만 연다.
        self.allow_restricted_steward = bool(allow_restricted_steward)
        self.allow_steward_auto_accept = bool(allow_steward_auto_accept)
        self.allow_local_test_object_authority_writes = bool(allow_local_test_object_authority_writes)
        self.allow_production_object_authority_writes = bool(allow_production_object_authority_writes)
        # M8 read cutover: a Qdrant-backed (query, brain_id) -> list[dict] callable
        # that fills brain.query's archive/evidence lanes from the Qdrant searchable
        # mirror. When set it REPLACES the RetiredIndexBridge archive search (which is off in the
        # live MCP anyway). None -> legacy behaviour (RetiredIndexBridge if dataset_ids, else empty).
        self._mirror_search = mirror_search
        self._semantic_ranker = semantic_ranker
        self.authorized_reader = authorized_reader or read_pipeline or MemoryReadPipeline(
            ledger=ledger,
            retired_index_bridge=retired_index_bridge,
            dataset_ids=dataset_ids,
            allow_private_results=allow_private_results,
        )
        self.read_pipeline = self.authorized_reader
        # Session-lifetime accepted-card snapshot shared across brain tool calls.
        self._brain_card_cache = _SessionCardCache(LegacyLedgerBrainReadModel(self.ledger))

    def invalidate_brain_card_cache(self) -> None:
        """세션 card snapshot을 비워 다음 brain tool 호출이 ledger를 다시 읽게 한다."""

        self._brain_card_cache.invalidate()

    def brain_steward(self):
        """proposal-only Brain Steward 서비스. restricted 위임은 flag 로만 열린다."""

        from .session_memory.brain_steward import BrainStewardService

        return BrainStewardService(
            self.ledger,
            allow_restricted=self.allow_restricted_steward,
            allow_auto_accept=self.allow_steward_auto_accept,
        )

    def append_object_review_proposal(self, proposal: dict) -> dict:
        stored = dict(proposal)
        if (
            str(stored.get("object_type") or "") == "ArtifactPreference"
            or knowledge_object_class_from_id(str(stored.get("target_object_id") or ""))
            == "ArtifactPreference"
        ):
            stored["evidence_refs"] = _artifact_preference_evidence_refs(
                stored.get("evidence_refs")
            )
        ensure_public_safe(stored, "object_review_proposal")
        return self.ledger.upsert_object_review_proposal(stored)

    def prepare_proposed_object_snapshot(
        self,
        proposed_object: Mapping[str, Any],
        *,
        target_object_id: str,
        project: str,
    ) -> dict[str, Any]:
        object_type = knowledge_object_class_from_id(target_object_id)
        if object_type == "ArtifactPreference":
            return _artifact_preference_snapshot(
                proposed_object,
                target_object_id=target_object_id,
                project=project,
                source_ref_lookup=LedgerSourceRefCatalog(self.ledger).get,
            )
        ensure_public_safe(dict(proposed_object), "proposed_object")
        _reject_forbidden_snapshot_keys(proposed_object)
        return {
            key: copy.deepcopy(proposed_object[key])
            for key in (
                "schema_version",
                "object_id",
                "object_type",
                "scope",
                "title",
                "summary",
                "content_hash",
                "evidence_refs",
                "source_refs",
                "privacy_class",
                "payload",
            )
            if key in proposed_object
        }

    def commit_object_authority_decision(
        self,
        decision: dict,
        *,
        proposal: dict | None = None,
    ) -> dict:
        stored = dict(decision)
        proposal_to_store = dict(proposal) if proposal is not None else None
        target_is_artifact_preference = (
            knowledge_object_class_from_id(str(stored.get("target_object_id") or ""))
            == "ArtifactPreference"
        )
        if target_is_artifact_preference:
            stored["evidence_refs"] = _artifact_preference_evidence_refs(
                stored.get("evidence_refs")
            )
            if proposal_to_store is not None:
                proposal_to_store["evidence_refs"] = _artifact_preference_evidence_refs(
                    proposal_to_store.get("evidence_refs")
                )
        ensure_public_safe(stored, "object_authority_decision")
        with self.ledger._transaction() as tx:
            if proposal_to_store is not None:
                tx.upsert_object_review_proposal(proposal_to_store)
            stored_proposal = tx.get_object_review_proposal(str(stored.get("proposal_id") or ""))
            if not stored_proposal:
                raise ValueError("object authority decision requires an existing review proposal")
            target_object_id = str(stored.get("target_object_id") or "")
            object_type = str(
                stored_proposal.get("object_type") or knowledge_object_class_from_id(target_object_id)
            )
            materialized_cards: list[dict[str, Any]] = []
            if object_type == "ArtifactPreference":
                completed_retry = _validate_completed_artifact_preference_decision_retry(
                    tx,
                    proposal=stored_proposal,
                    decision=stored,
                )
                if completed_retry is not None:
                    return completed_retry
                decision_type = str(stored.get("decision_type") or "")
                new_lane = str(stored.get("new_authority_lane") or "")
                if decision_type == "accept_current" and new_lane == "accepted_current":
                    candidate = _artifact_preference_memory_card(
                        proposal=stored_proposal,
                        decision=stored,
                        source_ref_lookup=tx.get_llm_brain_source_ref,
                    )
                    materialized_card = _resolve_artifact_preference_accept_card(
                        tx,
                        proposal=stored_proposal,
                        decision=stored,
                        candidate=candidate,
                    )
                    if materialized_card is not None:
                        materialized_cards.append(materialized_card)
                elif new_lane != "accepted_current":
                    existing = _current_artifact_preference_card_for_target(
                        tx,
                        project=str(stored.get("project") or ""),
                        target_object_id=target_object_id,
                    )
                    state = tx.get_object_authority_state(target_object_id)
                    if existing is None and str(state.get("authority_lane") or "") == "accepted_current":
                        raise ValueError("ArtifactPreference current authority has no matching current MemoryCard")
                    if decision_type == "rollback_decision":
                        _validate_artifact_preference_rollback_lineage(
                            tx,
                            card=existing,
                            decision=stored,
                        )
                        materialized_cards.append(commit_stale(existing))
                    elif decision_type == "commit_supersession":
                        if (
                            str(stored.get("previous_authority_lane") or "") != "accepted_current"
                            or new_lane != "accepted_non_current"
                        ):
                            raise ValueError(
                                "ArtifactPreference supersession requires accepted_current to accepted_non_current transition"
                            )
                        if existing is None or str(existing.get("currentness") or "") != "current":
                            raise ValueError(
                                "ArtifactPreference commit_supersession requires a current prior MemoryCard"
                            )
                        materialized_cards.extend(
                            _artifact_preference_supersession_cards(
                                tx,
                                prior_card=existing,
                                decision=stored,
                            )
                        )
                    elif existing is not None and str(existing.get("currentness") or "") == "current":
                        raise ValueError(
                            "ArtifactPreference current authority can only be removed by rollback or verified supersession"
                        )
            committed = tx.commit_object_authority_decision(stored)
            if object_type == "ArtifactPreference" and not materialized_cards:
                return committed
            for materialized_card in materialized_cards:
                tx.upsert_llm_brain_memory_card(materialized_card)
        self.invalidate_brain_card_cache()
        return committed

    def object_review_proposals(self, *, project: str = "", limit: int = 20) -> dict:
        bounded = max(1, min(int(limit or 20), 100))
        project_name = public_safe_text(project, max_chars=120)
        items = self.ledger.list_object_review_proposals(project=project_name, limit=bounded)
        response = {
            "schema_version": "brain_review_proposals.v1",
            "project": project_name,
            "count": len(items),
            "items": items,
            "gaps": [] if items else ["review_queue_empty"],
        }
        ensure_public_safe(response, "object_review_proposals")
        return response

    def brain_objects_query(
        self,
        *,
        repository: str,
        branch: str,
        query: str,
        current_files: list[str],
        project: str | None = None,
        object_types: list[str] | None = None,
        route: str = "",
        limit: int = 20,
        response_mode: str = "full",
        consumer: str = "unspecified",
        as_of: str = "",
        date_from: str = "",
        date_to: str = "",
    ) -> dict[str, Any]:
        result = self.core_brain(project=project or "").brain_objects_query(
            repository=repository,
            branch=branch,
            query=query,
            current_files=current_files,
            project=project or None,
            object_types=object_types or [],
            route=route,
            limit=limit,
            response_mode=response_mode,
            consumer=consumer,
            as_of=as_of,
            date_from=date_from,
            date_to=date_to,
        )
        return self._overlay_object_authority_states(result)

    def brain_artifact_preference_evaluate(
        self,
        *,
        repository: str,
        branch: str,
        project: str,
        artifact_type: str,
        summary: str,
        artifact_fingerprint: str,
        metrics: Mapping[str, Any],
        evidence_refs: list[str] | tuple[str, ...],
        consumer: str,
    ) -> dict[str, Any]:
        return evaluate_artifact_preference(
            ledger=self.ledger,
            repository=repository,
            branch=branch,
            project=project,
            artifact_type=artifact_type,
            summary=summary,
            artifact_fingerprint=artifact_fingerprint,
            metrics=metrics,
            evidence_refs=evidence_refs,
            consumer=consumer,
        )

    def brain_source_to_candidate_graph(
        self,
        *,
        project: str,
        corpus_id: str = "",
        target: str = "production",
        consumer: str = "unspecified",
        limit: int = 20,
    ) -> dict[str, Any]:
        safe_target = public_safe_text(str(target or "production"), max_chars=80)
        if safe_target != "local_test":
            result = {
                "schema_version": "object_substrate_cli_denied.v1",
                "status": "denied",
                "reason": "production_source_to_candidate_graph_requires_later_validation_goal",
                "mutation_performed": False,
                "production_mutation_performed": False,
                "ledger_mutation_performed": False,
                "network_used": False,
            }
            ensure_public_safe(result, "brain_source_to_candidate_graph_denied")
            return result
        status = self.ledger.reference_corpus_status(
            project=public_safe_text(project, max_chars=120),
            corpus_id=public_safe_text(corpus_id, max_chars=180),
            limit=limit,
        )
        return run_source_to_candidate_graph_activation_preview(
            corpus_status=status,
            project=project,
            consumer=consumer,
        )

    def brain_candidate_review_edit(
        self,
        *,
        pack: Mapping[str, Any],
        edits: list[Mapping[str, Any]],
        reviewer_id: str = "unspecified",
        target: str = "local_test",
        mutation_mode: str = "no_mutation",
    ) -> dict[str, Any]:
        return apply_candidate_review_edits(
            pack,
            edits=edits,
            reviewer={"id": reviewer_id},
            target_scope=target,
            mutation_mode=mutation_mode,
        )

    def brain_approval_board_decide(
        self,
        *,
        pack: Mapping[str, Any],
        decisions: list[Mapping[str, Any]],
        target: str = "production",
        reviewer_id: str = "unspecified",
        production_gate: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        safe_target = public_safe_text(str(target or "production"), max_chars=80)
        if safe_target != "local_test" and isinstance(production_gate, Mapping):
            return self._brain_approval_board_decide_production(
                pack=pack,
                decisions=decisions,
                reviewer_id=reviewer_id,
                production_gate=production_gate,
            )
        return apply_approval_board_decisions(
            pack,
            decisions=decisions,
            reviewer={"id": reviewer_id},
            ledger_scope=safe_target,
        )

    def _brain_approval_board_decide_production(
        self,
        *,
        pack: Mapping[str, Any],
        decisions: list[Mapping[str, Any]],
        reviewer_id: str,
        production_gate: Mapping[str, Any],
    ) -> dict[str, Any]:
        decision_arg = decisions[0] if len(decisions) == 1 and isinstance(decisions[0], Mapping) else {}
        target_object_id = public_safe_text(str(decision_arg.get("object_id") or ""), max_chars=180)
        target = self._candidate_object(pack, target_object_id)
        target_scope = target.get("scope") if isinstance(target.get("scope"), Mapping) else {}
        target_project = public_safe_text(
            str(target_scope.get("project") or target.get("project") or ""),
            max_chars=120,
        )
        target_object_type = public_safe_text(str(target.get("object_type") or ""), max_chars=120)
        prepared_snapshot = None
        if target_object_type == "ArtifactPreference":
            prepared_snapshot = self.prepare_proposed_object_snapshot(
                target,
                target_object_id=target_object_id,
                project=target_project,
            )
        preview = apply_approval_board_decisions(
            pack,
            decisions=decisions,
            reviewer={"id": reviewer_id},
            ledger_scope="local_test",
        )
        accepted_decisions = [dict(item) for item in preview.get("decisions") or [] if isinstance(item, Mapping)]
        rejected_decisions = [dict(item) for item in preview.get("rejected_decisions") or [] if isinstance(item, Mapping)]
        gate = self._approval_board_production_gate(
            production_gate,
            target_object_id=target_object_id,
            target_project=target_project,
            target_object_type=target_object_type,
            decision_count=len(decisions),
            action=public_safe_text(str(decision_arg.get("action") or ""), max_chars=80),
        )
        if not gate["allowed"] or len(accepted_decisions) != 1 or rejected_decisions:
            missing = list(gate["missing_gate_evidence"])
            if len(accepted_decisions) != 1:
                missing.append("approval_board_single_accepted_decision")
            if rejected_decisions:
                missing.append("approval_board_rejected_decisions_absent")
            result = {
                "schema_version": "approval_board_decision_result.v1",
                "permission": "denied",
                "reason": "production_approval_gate_invalid",
                "ledger_scope": "production",
                "production_mutation_performed": False,
                "proposal_write_performed": False,
                "authority_write_performed": False,
                "authority_write_scope": "",
                "authoritative_memory_changed": False,
                "decision_count": 0,
                "decisions": [],
                "rejected_decisions": rejected_decisions,
                "updated_pack": copy.deepcopy(dict(pack)),
                "promotion_plan": self._approval_board_production_promotion_plan(
                    gate=gate,
                    missing_gate_evidence=missing,
                    requested_action=str(decision_arg.get("action") or ""),
                ),
            }
            ensure_public_safe(result, "ApprovalBoardDecisionResult")
            return result

        authority_decision = accepted_decisions[0]
        reason = public_safe_text(str(decision_arg.get("reason") or "Production approval-board decision."), max_chars=512)
        evidence_refs = [str(ref) for ref in target.get("evidence_refs") or [] if ref]
        proposal = ReviewProposal.from_parts(
            proposal_type=gate["proposal_type"],
            target_object_id=target_object_id,
            reason=reason,
            evidence_refs=evidence_refs,
            proposer="codex",
        ).to_dict(proposal_write_performed=True, proposal_write_target="production_ledger")
        proposal["project"] = gate["project"]
        proposal["object_type"] = target_object_type
        proposal["ledger_scope"] = "production"
        proposal["production_mutation_performed"] = True
        proposal["production_gate_ref_hash"] = gate["approval_ref_hash"]
        proposal["proposed_object"] = prepared_snapshot or self.prepare_proposed_object_snapshot(
            target,
            target_object_id=target_object_id,
            project=gate["project"],
        )

        decision = AuthorityDecision.from_parts(
            decision_type=str(authority_decision["decision_type"]),
            target_object_id=target_object_id,
            previous_authority_lane=str(authority_decision["previous_authority_lane"]),
            new_authority_lane=str(authority_decision["new_authority_lane"]),
            approved_by="redacted",
            evidence_refs=evidence_refs,
        ).to_dict(authority_write_performed=True, cache_invalidated=True)
        decision["proposal_id"] = proposal["proposal_id"]
        decision["project"] = gate["project"]
        decision["decision_reason"] = reason
        reviewer_ref_hash = "sha256:" + short_hash(str(decision_arg.get("approved_by") or reviewer_id), length=24)
        decision["approved_by_hash"] = reviewer_ref_hash
        decision["ledger_scope"] = "production"
        decision["authority_write_scope"] = "production_ledger"
        decision["production_mutation_performed"] = True
        decision["production_gate_ref_hash"] = gate["approval_ref_hash"]
        committed = self.commit_object_authority_decision(decision, proposal=proposal)

        updated_pack = copy.deepcopy(dict(preview.get("updated_pack") or pack))
        updated_pack["authority_write_scope"] = "production_ledger"
        updated_pack["production_mutation_performed"] = True
        updated_pack["ledger_scope"] = "production"
        result = {
            "schema_version": "approval_board_decision_result.v1",
            "permission": "allowed",
            "reason": "production_approval_board_decision",
            "ledger_scope": "production",
            "production_mutation_performed": True,
            "proposal_write_performed": True,
            "proposal_id": proposal["proposal_id"],
            "proposal_write_target": "production_ledger",
            "authority_write_performed": True,
            "authority_write_scope": "production_ledger",
            "authoritative_memory_changed": True,
            "production_gate_ref_hash": gate["approval_ref_hash"],
            "original_candidate_graph_hash": preview.get("original_candidate_graph_hash", ""),
            "updated_candidate_graph_hash": preview.get("updated_candidate_graph_hash", ""),
            "reviewer_ref": "redacted",
            "reviewer_ref_hash": reviewer_ref_hash,
            "decision_count": 1,
            "decisions": [committed],
            "rejected_decisions": [],
            "updated_pack": updated_pack,
            "promotion_plan": self._approval_board_production_promotion_plan(
                gate=gate,
                missing_gate_evidence=[],
                requested_action=str(decision_arg.get("action") or ""),
            ),
        }
        ensure_public_safe(result, "ApprovalBoardDecisionResult")
        return result

    def _approval_board_production_gate(
        self,
        gate: Mapping[str, Any],
        *,
        target_object_id: str,
        target_project: str,
        target_object_type: str,
        decision_count: int,
        action: str,
    ) -> dict[str, Any]:
        approval_ref = public_safe_text(str(gate.get("approval_ref") or ""), max_chars=160)
        project = public_safe_text(str(gate.get("project") or ""), max_chars=120)
        candidate_project = public_safe_text(str(target_project or ""), max_chars=120)
        proposal_type, decision_type, new_lane, _lifecycle, _review = _approval_board_production_decision_state(action)
        missing: list[str] = []
        if not bool(self.allow_production_object_authority_writes):
            missing.append("service_production_object_authority_write_flag")
        if bool(getattr(self.ledger, "read_only", True)):
            missing.append("writable_ledger")
        if gate.get("approved") is not True:
            missing.append("approved")
        if not approval_ref:
            missing.append("approval_ref")
        if str(gate.get("scope") or "") != "single_project_single_object":
            missing.append("single_project_single_object_scope")
        if not project or project != candidate_project:
            missing.append("project_scope_match")
        try:
            max_objects = int(gate.get("max_objects") or 0)
        except (TypeError, ValueError):
            max_objects = 0
        if max_objects != 1:
            missing.append("max_objects_1")
        if decision_count != 1:
            missing.append("approval_board_single_decision")
        for field in _APPROVAL_BOARD_PRODUCTION_REQUIRED_TRUE_FIELDS:
            if gate.get(field) is not True:
                missing.append(field)
        if not target_object_type:
            missing.append("explicit_object_type")
        elif not is_allowed_object_target(target_object_id, object_type=target_object_type):
            missing.append(allowed_object_class_gap())
        if not proposal_type or not decision_type or new_lane != "accepted_current":
            missing.append("allowed_approval_board_action")
        return {
            "allowed": not missing,
            "missing_gate_evidence": list(dict.fromkeys(missing)),
            "approval_ref_hash": sha256_text(approval_ref) if approval_ref else "",
            "project": project,
            "target_project": candidate_project,
            "target_object_id": target_object_id,
            "proposal_type": proposal_type,
            "decision_type": decision_type,
        }

    def _candidate_object(self, pack: Mapping[str, Any], object_id: str) -> dict[str, Any]:
        objects = pack.get("objects") if isinstance(pack.get("objects"), list) else []
        for obj in objects:
            if isinstance(obj, Mapping) and str(obj.get("object_id") or "") == object_id:
                return dict(obj)
        return {}

    def _approval_board_production_promotion_plan(
        self,
        *,
        gate: Mapping[str, Any],
        missing_gate_evidence: list[str],
        requested_action: str,
    ) -> dict[str, Any]:
        mutation_allowed = not missing_gate_evidence and bool(gate.get("allowed"))
        return {
            "schema_version": "object_authority_promotion_plan.v1",
            "production_write_state": "open_with_preapproved_gate" if mutation_allowed else "closed_without_valid_production_gate",
            "mutation_allowed": mutation_allowed,
            "requested_approval_board_action": public_safe_text(requested_action, max_chars=80),
            "project": public_safe_text(str(gate.get("project") or ""), max_chars=120),
            "target_object_id": public_safe_text(str(gate.get("target_object_id") or ""), max_chars=180),
            "missing_gate_evidence": list(dict.fromkeys(missing_gate_evidence)),
            "allowed_object_classes": allowed_object_classes_list(),
            "allowed_approval_board_actions": ["promote"],
            "required_gate_evidence": [
                "configured_deployed_mcp_identity_matches_source",
                "single_object_scope",
                "read_after_write_smoke_plan",
                "rollback_or_supersession_plan",
                "no_raw_private_evidence",
            ],
            "no_mutation_report": {
                "proposal_write_performed": False,
                "authority_write_performed": False,
                "authoritative_memory_changed": False,
            },
        }

    def brain_source_to_candidate_runtime_readiness(
        self,
        *,
        live_evidence: Mapping[str, Any] | None = None,
        normalize_post_deploy_capture: Mapping[str, Any] | None = None,
        post_deploy_capture: Mapping[str, Any] | None = None,
        normalize_shadow_evidence: Mapping[str, Any] | None = None,
        shadow_evidence: Mapping[str, Any] | None = None,
        expected_commit: str = "",
        evidence_collection_plan: bool = False,
        evidence_packet_template: bool = False,
        collect_shadow_evidence: bool = False,
        evidence_collection_mode: str = "local_test_replay",
        evidence_collection_network_used: bool = False,
        repository: str = "",
        branch: str = "",
        project: str = "",
        consumer: str = "codex",
    ) -> dict[str, Any]:
        if evidence_collection_plan:
            return build_source_to_candidate_runtime_evidence_collection_plan(
                expected_commit=expected_commit,
                repository=repository,
                branch=branch,
                project=project,
                consumer=consumer,
            )
        if evidence_packet_template:
            return build_source_to_candidate_runtime_evidence_packet_template(
                expected_commit=expected_commit,
                repository=repository,
                branch=branch,
                project=project,
                consumer=consumer,
            )
        if collect_shadow_evidence:
            resolved_project = public_safe_text(
                str(project or ("neurons" if evidence_collection_mode == "post_deploy_read_only_smoke" else "")),
                max_chars=120,
            )

            def route_runner(route: str) -> Mapping[str, Any]:
                return self.brain_objects_query(
                    repository=repository,
                    branch=branch,
                    query=f"source-to-candidate runtime readiness route smoke: {route}",
                    current_files=[],
                    project=resolved_project or None,
                    route=route,
                    limit=5,
                    response_mode="full",
                    consumer=consumer,
                )

            collection_mode = public_safe_text(str(evidence_collection_mode or "local_test_replay"), max_chars=80)
            network_used = bool(evidence_collection_network_used)
            projection_join_runner = None
            preference_artifact_memory_runner = None
            temporal_correctness_runtime_runner = None
            if collection_mode == "post_deploy_read_only_smoke" and network_used:
                projection_join_runner = lambda: self._projection_join_runtime_read_path_evidence(
                    repository=repository,
                    branch=branch,
                    project=resolved_project,
                    consumer=consumer,
                )
                preference_artifact_memory_runner = lambda: self._preference_artifact_memory_runtime_read_path_evidence(
                    repository=repository,
                    branch=branch,
                    project=resolved_project,
                    consumer=consumer,
                )
                temporal_correctness_runtime_runner = (
                    lambda: self._temporal_correctness_runtime_read_path_evidence(
                        project=resolved_project,
                    )
                )
            return build_source_to_candidate_runtime_collected_shadow_evidence_packet(
                expected_commit=expected_commit,
                repository=repository,
                branch=branch,
                project=resolved_project,
                consumer=consumer,
                route_runner=route_runner,
                projection_join_runner=projection_join_runner,
                preference_artifact_memory_runner=preference_artifact_memory_runner,
                temporal_correctness_runtime_runner=(
                    temporal_correctness_runtime_runner
                ),
                collection_mode=collection_mode,
                network_used=network_used,
            )
        if isinstance(normalize_post_deploy_capture, Mapping):
            return build_source_to_candidate_runtime_post_deploy_capture_packet(
                captured_evidence=dict(normalize_post_deploy_capture),
            )
        if isinstance(post_deploy_capture, Mapping):
            return build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
                captured_evidence=dict(post_deploy_capture),
                expected_commit=expected_commit,
            )
        if isinstance(normalize_shadow_evidence, Mapping):
            return build_source_to_candidate_runtime_shadow_evidence_packet(
                captured_evidence=dict(normalize_shadow_evidence),
            )
        if isinstance(shadow_evidence, Mapping):
            return build_source_to_candidate_runtime_shadow_readiness_report(
                captured_evidence=dict(shadow_evidence),
                expected_commit=expected_commit,
            )
        return build_source_to_candidate_runtime_readiness_report(
            live_evidence=dict(live_evidence) if isinstance(live_evidence, Mapping) else None,
            expected_commit=expected_commit,
        )

    def _temporal_correctness_runtime_read_path_evidence(
        self,
        *,
        project: str,
    ) -> dict[str, Any]:
        """Read current projection/artifact aggregates from production authorities."""

        source_store = _build_source_store(
            couchdb_url=os.environ.get("COUCHDB_URL", ""),
            couchdb_db=os.environ.get("COUCHDB_DB", "transcript_source"),
            couchdb_user=os.environ.get("COUCHDB_USER", ""),
            couchdb_password_env="COUCHDB_PASSWORD",
        )
        runtime_dir_text = str(os.environ.get("LLM_BRAIN_GRAPH_RUNTIME_DIR") or "")
        if not runtime_dir_text:
            raise ValueError("LLM_BRAIN_GRAPH_RUNTIME_DIR is required")
        runtime_dir = Path(runtime_dir_text)
        status = build_graph_projection_status(
            ledger_path=self.ledger.path,
            source_store=source_store,
            project=project,
            progress_jsonl=[runtime_dir / "graph-trigger-progress.jsonl"],
            dead_letter_jsonl=[runtime_dir / "graph-trigger-dead-letter.jsonl"],
        )
        projection = (
            status.get("projection_state")
            if isinstance(status.get("projection_state"), Mapping)
            else {}
        )
        source = (
            status.get("source")
            if isinstance(status.get("source"), Mapping)
            else {}
        )
        artifact = (
            status.get("artifact_age")
            if isinstance(status.get("artifact_age"), Mapping)
            else {}
        )
        progress = (
            status.get("progress")
            if isinstance(status.get("progress"), Mapping)
            else {}
        )
        entity_run = (
            progress.get("latest_entity_run")
            if isinstance(progress.get("latest_entity_run"), Mapping)
            else {}
        )
        if not entity_run.get("event_counts"):
            raise ValueError("graph progress evidence is missing")
        if entity_run.get("completed") is not True:
            raise ValueError("latest graph projection run did not complete")
        if str(entity_run.get("status") or "") != "ok":
            raise ValueError("latest graph projection run is not ok")
        if (
            entity_run.get("scope_consistent") is not True
            or entity_run.get("project_set") is not True
            or str(entity_run.get("project_ref") or "")
            != _project_ref(project)
            or str(entity_run.get("provider") or "")
            or str(entity_run.get("target_extraction_level") or "")
            != "entity"
        ):
            raise ValueError("graph projection run scope does not match")
        graph_started_at = _parse_utc_runtime_timestamp(
            entity_run.get("started_at"),
            field="latest graph projection run started_at",
        )
        graph_completed_at = _parse_utc_runtime_timestamp(
            entity_run.get("completed_at"),
            field="latest graph projection run completed_at",
        )
        if graph_started_at > graph_completed_at:
            raise ValueError("latest graph projection run timestamps are invalid")
        try:
            graph_max_age_seconds = int(
                os.environ.get("LLM_BRAIN_GRAPH_RUN_MAX_AGE_SECONDS", "900")
            )
        except ValueError as exc:
            raise ValueError("LLM_BRAIN_GRAPH_RUN_MAX_AGE_SECONDS is invalid") from exc
        if graph_max_age_seconds < 1 or graph_max_age_seconds > 86400:
            raise ValueError("LLM_BRAIN_GRAPH_RUN_MAX_AGE_SECONDS is invalid")
        graph_age_seconds = int(
            (datetime.now(timezone.utc) - graph_completed_at).total_seconds()
        )
        if graph_age_seconds < -60:
            raise ValueError("latest graph projection run timestamps are invalid")
        graph_age_seconds = max(0, graph_age_seconds)
        if graph_age_seconds > graph_max_age_seconds:
            raise ValueError("latest graph projection run is stale")
        artifact_missing = int(artifact.get("artifact_missing_session_count") or 0)
        artifact_unknown = int(artifact.get("artifact_age_unknown_count") or 0)
        artifact_mismatch = int(
            artifact.get("artifact_source_hash_mismatch_count") or 0
        )
        session_memory_noncurrent = int(
            projection.get("session_memory_projection_noncurrent_count") or 0
        )
        graph_current = int(projection.get("episodic_session_projected") or 0)
        graph_noncurrent = int(projection.get("episodic_session_noncurrent") or 0)
        session_memory_mismatch = int(
            projection.get("session_memory_source_hash_mismatch_count") or 0
        )
        session_memory_stale = int(
            projection.get("session_memory_stale_projected_session_count") or 0
        )
        source_hash_mismatch = int(
            projection.get("source_hash_mismatch_count") or 0
        )
        stale_projected = int(
            projection.get("stale_projected_session_count") or 0
        )
        evidence = {
            "schema_version": "temporal_correctness_runtime_aggregate.v1",
            "projection_currentness": {
                "source_hash_match": all(
                    value == 0
                    for value in (
                        source_hash_mismatch,
                        stale_projected,
                        graph_noncurrent,
                        session_memory_noncurrent,
                        session_memory_mismatch,
                        session_memory_stale,
                    )
                ),
                "source_hash_mismatch_count": source_hash_mismatch,
                "stale_projected_session_count": stale_projected,
                "source_session_count": int(source.get("session_count") or 0),
                "graph_projection_current_count": graph_current,
                "graph_projection_noncurrent_count": graph_noncurrent,
                "session_memory_projection_current_count": int(
                    projection.get("session_memory_projection_current_count") or 0
                ),
                "session_memory_projection_noncurrent_count": session_memory_noncurrent,
                "session_memory_source_hash_mismatch_count": session_memory_mismatch,
                "session_memory_stale_projected_session_count": session_memory_stale,
                "artifact_current": (
                    artifact_missing == 0
                    and artifact_unknown == 0
                    and artifact_mismatch == 0
                ),
                "artifact_missing_session_count": artifact_missing,
                "artifact_age_unknown_count": artifact_unknown,
                "artifact_source_hash_mismatch_count": artifact_mismatch,
                "oldest_artifact_age_seconds": int(
                    artifact.get("oldest_artifact_age_seconds") or 0
                ),
                "graph_run_scope_match": True,
                "graph_run_fresh": True,
                "graph_run_completed_age_seconds": graph_age_seconds,
                "graph_run_max_age_seconds": graph_max_age_seconds,
            },
            "entity_projection": {
                "valid_source_count": int(
                    projection.get("entity_valid_source_sessions") or 0
                ),
                "coverage_count": int(
                    projection.get("entity_session_projected") or 0
                ),
                "backlog_count": int(
                    projection.get("entity_session_backlog") or 0
                ),
                "error_count": int(
                    max(
                        int(entity_run.get("failed") or 0),
                        int(projection.get("entity_source_invalid") or 0),
                        int(entity_run.get("dead_letter_count") or 0),
                    )
                ),
            },
            "production_mutation_performed": False,
        }
        ensure_public_safe(evidence, "TemporalCorrectnessRuntimeReadPathEvidence")
        return evidence

    def _preference_artifact_memory_runtime_read_path_evidence(
        self,
        *,
        repository: str,
        branch: str,
        project: str,
        consumer: str,
    ) -> dict[str, Any]:
        safe_repository = public_safe_text(repository or project or "neurons", max_chars=120)
        safe_branch = public_safe_text(branch or "main", max_chars=120)
        request = "Review an HTML artifact using the accepted style preference."
        preference_route = self.brain_objects_query(
            repository=safe_repository,
            branch=safe_branch,
            project=project,
            route="code_style_preference",
            query=request,
            current_files=[],
            consumer=consumer,
        )
        html_route = self.brain_objects_query(
            repository=safe_repository,
            branch=safe_branch,
            project=project,
            route="html_visualization_preference",
            query=request,
            current_files=[],
            consumer=consumer,
        )
        context_pack = self.core_brain(project=project).brain_context_resolve(
            repository=safe_repository,
            branch=safe_branch,
            project=project,
            current_request=request,
            current_files=[],
            consumer=consumer,
        ).to_dict()
        return build_preference_artifact_memory_runtime_evidence(
            preference_route=preference_route,
            html_route=html_route,
            context_pack=context_pack,
            artifact_summary={
                "artifact_type": "html_review",
            },
        )

    def _projection_join_runtime_read_path_evidence(
        self,
        *,
        repository: str,
        branch: str,
        project: str,
        consumer: str,
    ) -> dict[str, Any]:
        safe_repository = public_safe_text(repository or "pureliture/neurons", max_chars=120)
        safe_project = public_safe_text(project or "neurons", max_chars=120)
        query = public_safe_text(
            f"{safe_repository} {branch or 'main'} source candidate graph projection join {consumer or 'codex'}",
            max_chars=240,
        )
        target_object_id = "ko:RuntimeProjectionJoinProbe:" + short_hash(f"{safe_repository}:{safe_project}:{query}")
        target_object = {
            "object_id": target_object_id,
            "object_type": "RuntimeProjectionJoinProbe",
            "title": "Runtime projection join read-path probe",
            "summary": "Public-safe candidate target for graph/search projection join evidence.",
            "authority_lane": "candidate",
            "verification_state": "runtime_unverified",
            "review_state": "needs_review",
        }
        brain_id = f"/project/{safe_project}"
        projection_hits: list[dict[str, Any]] = []
        graph_status = "not_configured"
        graph_detail_count = 0
        if self.graph_adapter is not None:
            try:
                graph_result = self.graph_adapter.search_context(
                    brain_id=brain_id,
                    query=query,
                    limit=3,
                )
                graph_status = public_safe_text(str(getattr(graph_result, "status", "") or ""), max_chars=80)
                graph_detail_count = len(getattr(graph_result, "details", ()) or ())
                for index, episode in enumerate(getattr(graph_result, "episodes", ()) or [], start=1):
                    projection_hits.append(
                        {
                            "hit_id": f"graph:{short_hash(str(getattr(episode, 'episode_id', '') or index))}",
                            "source": "graph",
                            "object_ref": target_object_id,
                            "summary": public_safe_text(_episode_projection_summary(episode), max_chars=280),
                            "score": 0.72,
                        }
                    )
            except Exception as exc:  # pragma: no cover - defensive live read guard
                graph_status = public_safe_text(f"error:{type(exc).__name__}", max_chars=80)
        qdrant_status = "not_configured"
        if self._mirror_search is not None:
            try:
                qdrant_hits = self._mirror_search(query, brain_id)
                qdrant_status = "available"
                for index, hit in enumerate(qdrant_hits or [], start=1):
                    if not isinstance(hit, Mapping):
                        continue
                    projection_hits.append(
                        {
                            "hit_id": f"qdrant:{short_hash(str(hit.get('memory_id') or hit.get('content_hash') or index))}",
                            "source": "search",
                            "object_ref": target_object_id,
                            "summary": public_safe_text(str(hit.get("summary") or "Derived search mirror hit."), max_chars=280),
                            "score": _safe_float(hit.get("score"), default=0.64),
                        }
                    )
            except Exception as exc:  # pragma: no cover - defensive live read guard
                qdrant_status = public_safe_text(f"error:{type(exc).__name__}", max_chars=80)
        preview = run_graph_search_projection_join_preview(
            objects=[target_object],
            projection_hits=projection_hits,
            repository=safe_repository,
        )
        graph_hit_count = int(preview.get("pack_preview", {}).get("graph_hit_count") or 0)
        search_hit_count = int(preview.get("pack_preview", {}).get("search_hit_count") or 0)
        gaps = [str(gap) for gap in preview.get("gaps") or [] if gap]
        if graph_hit_count < 1:
            gaps.append("graph_projection_hit_missing")
        if search_hit_count < 1:
            gaps.append("qdrant_projection_hit_missing")
        if graph_status not in {"available", "degraded"}:
            gaps.append("graph_projection_read_unavailable")
        if qdrant_status != "available":
            gaps.append("qdrant_projection_read_unavailable")
        gaps = sorted(set(public_safe_text(gap, max_chars=120) for gap in gaps))
        status = "pass" if int(preview.get("edge_count") or 0) > 0 and not gaps else "pass_with_gaps"
        preview["status"] = status
        preview["gaps"] = gaps
        preview["evidence_class"] = "runtime_projection_join"
        preview["runtime_read_path"] = {
            "schema_version": "projection_join_runtime_read_path.v1",
            "graph_status": graph_status,
            "graph_detail_count": graph_detail_count,
            "graph_hit_count": graph_hit_count,
            "qdrant_status": qdrant_status,
            "qdrant_hit_count": search_hit_count,
            "production_mutation_performed": False,
        }
        preview["postcheck"] = {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        }
        evaluator = preview.get("evaluator_report")
        if isinstance(evaluator, dict):
            evaluator["passes"] = status == "pass"
            evaluator["failures"] = [] if status == "pass" else gaps or ["projection_join_missing"]
            evaluator["gaps"] = gaps
        for strategy in preview.get("strategy_comparison") or []:
            if isinstance(strategy, dict) and strategy.get("selected") is True:
                strategy["status"] = status
                strategy["gaps"] = gaps
        ensure_public_safe(preview, "ProjectionJoinRuntimeReadPathEvidence")
        return preview

    def _overlay_object_authority_states(self, result: Mapping[str, Any]) -> dict[str, Any]:
        response = copy.deepcopy(dict(result))
        object_pack = response.get("object_pack")
        if not isinstance(object_pack, dict):
            return response
        objects = [dict(obj) for obj in object_pack.get("objects", []) if isinstance(obj, Mapping)]
        object_ids = [str(obj.get("object_id") or "") for obj in objects if obj.get("object_id")]
        try:
            states = self.ledger.get_object_authority_states(object_ids) if object_ids else {}
            overlay_status = "available"
            overlay_error_type = ""
        except Exception as exc:  # noqa: BLE001 - read path must survive optional live overlay schema gaps.
            states = {}
            overlay_status = "unavailable"
            overlay_error_type = public_safe_text(type(exc).__name__, max_chars=80)
        overlay_count = 0
        for obj in objects:
            object_id = str(obj.get("object_id") or "")
            state = states.get(object_id)
            if not state:
                continue
            _apply_object_authority_state(obj, state)
            overlay_count += 1
        object_pack["objects"] = objects
        if overlay_count:
            object_pack["lanes"] = _rebuild_object_lanes(object_pack, objects)
            object_pack["recommended_actions"] = [
                {"object_id": obj["object_id"], "action": obj["recommended_action"]}
                for obj in objects
                if obj.get("object_id") and obj.get("recommended_action")
            ]
        audit = dict(object_pack.get("audit") or {})
        audit["authority_state_overlay_count"] = overlay_count
        audit["authority_state_overlay_status"] = overlay_status
        if overlay_error_type:
            audit["authority_state_overlay_error_type"] = overlay_error_type
            gaps = list(object_pack.get("gaps") or [])
            if "authority_state_overlay_unavailable" not in gaps:
                gaps.append("authority_state_overlay_unavailable")
            object_pack["gaps"] = gaps
        object_pack["audit"] = audit
        ensure_public_safe(response, "brain_objects_query_authority_overlay")
        return response

    def brain_object_explain(
        self,
        *,
        object_id: str,
        include_edges: bool = True,
        include_evidence: bool = True,
        response_mode: str = "full",
    ) -> dict[str, Any]:
        safe_object_id = public_safe_text(object_id, max_chars=180)
        result = self.core_brain().brain_object_explain(
            object_id=safe_object_id,
            include_edges=include_edges,
            include_evidence=include_evidence,
            response_mode=response_mode,
        )
        state = self.ledger.get_object_authority_state(safe_object_id)
        history = self.ledger.list_object_authority_decisions(target_object_id=safe_object_id, limit=20)
        result["decision_history"] = [dict(item) for item in history]
        if state:
            obj = dict(result.get("object") or {})
            obj.setdefault("object_id", safe_object_id)
            obj.setdefault("object_type", _object_type_from_object_id(safe_object_id))
            obj.setdefault("title", safe_object_id)
            obj.setdefault("summary", "Object authority state from ledger decision history.")
            obj.setdefault("lifecycle_status", "observed")
            obj.setdefault("authority_lane", str(state.get("previous_authority_lane") or "candidate"))
            obj.setdefault("verification_state", "unverified")
            obj.setdefault("review_state", "needs_review")
            obj.setdefault("recommended_action", "review")
            _apply_object_authority_state(obj, state)
            result["object"] = obj
            result["authority_state"] = _object_authority_state_view(state)
            gaps = [str(item) for item in result.get("gaps", []) if item]
            if "authority_state_from_ledger_only" not in gaps:
                gaps.append("authority_state_from_ledger_only")
            result["gaps"] = gaps
        ensure_public_safe(result, "brain_object_explain_authority_overlay")
        return result

    def core_brain(self, *, project: str = ""):
        return build_runtime_brain_service(
            project=project,
            artifact_store=LedgerSessionMemoryArtifactStore(self.ledger),
            read_model=self._brain_card_cache,
            source_catalog=LedgerSourceRefCatalog(self.ledger),
            graph_adapter=self.graph_adapter,
            document_bridge=RetiredIndexBridgeDocumentBridge(retired_index_bridge=self.retired_index_bridge, dataset_ids=self.dataset_ids),
            search_mirror_status=self._search_mirror_status(),
            reference_corpus_status_reader=self.ledger.reference_corpus_status,
        )

    def _search_mirror_status(self) -> dict:
        if self._mirror_search is None:
            return {
                "status": "unverified",
                "last_verified_at": "",
                "evidence_ref": "",
                "details": ["mirror_search_not_configured_for_context_authority"],
            }
        return {
            "status": "configured_unverified",
            "last_verified_at": "",
            "evidence_ref": "service:mirror_search_configured",
            "details": ["mirror_search_callable_configured_without_live_probe"],
        }

    def search(
        self,
        query: str,
        *,
        filters: dict | None = None,
        limit: int = 10,
        include_private: bool = False,
    ) -> dict:
        bounded_limit = _knowledge_search_public_limit(limit)
        search_query = MemorySearchQuery(
            query=query,
            filters=filters,
            limit=bounded_limit,
            include_private=include_private,
        )
        response = self.authorized_reader.read(search_query)
        results_dict = []
        for item in response.results:
            item_dict = {
                "knowledge_id": item.knowledge_id,
                "result_type": item.result_type,
                "title": item.title,
                "domain": item.domain,
                "project": item.project,
                "provider": item.provider,
                "summary": item.summary,
                "score": item.score,
                "currentness": item.currentness,
                "provenance": {
                    "authority": "ledger_authorized",
                    "citation_ref": item.knowledge_id,
                },
            }
            if item.conversation_chunk is not None:
                chunk = item.conversation_chunk
                item_dict.update({
                    "chunk_id": chunk.chunk_id,
                    "session_id_hash": chunk.session_id_hash,
                    "turn_range": {
                        "start": chunk.turn_range.start,
                        "end": chunk.turn_range.end,
                    },
                    "snippet": chunk.snippet,
                    "source_status": chunk.source_status,
                    "redaction_version": chunk.redaction_version,
                })
            results_dict.append(item_dict)
        return {"results": results_dict}

    def brain_query(self, *, brain_id: str, query: str, limit: int = 8) -> dict:
        read_model = LegacyLedgerBrainReadModel(self.ledger)
        index_search = self._mirror_search or (
            self._brain_query_index_search if self.dataset_ids else None
        )
        semantic_hits: list[dict] = []
        semantic_failure_type = ""
        effective_semantic_ranker = self._semantic_ranker
        if self.native_memory_id:
            semantic = build_semantic_recall(
                ledger=self.ledger,
                retired_index_bridge=self.retired_index_bridge,
                memory_id=self.native_memory_id,
            )
            try:
                semantic_hits = semantic(query, brain_id)
            except (OSError, RuntimeError, ValueError, KeyError, TypeError, sqlite3.DatabaseError) as exc:
                semantic_hits = []
                semantic_failure_type = type(exc).__name__
            native_scores = _native_semantic_memory_scores(semantic_hits)
            if native_scores:
                def _native_semantic_ranker(**kwargs):
                    ranked = []
                    for card in kwargs.get("cards") or []:
                        memory_id = str(card.get("memory_id") or "")
                        score = native_scores.get(memory_id)
                        if score is None:
                            continue
                        enriched = dict(card)
                        enriched["_semantic_score"] = score
                        ranked.append(enriched)
                    ranked.sort(key=lambda card: (-float(card.get("_semantic_score") or 0.0), str(card.get("memory_id") or "")))
                    return ranked[: max(0, int(kwargs.get("limit") or 0))]

                effective_semantic_ranker = _native_semantic_ranker
        result = run_brain_query_v2(
            read_model=read_model,
            index_search=index_search,
            brain_id=brain_id,
            query=query,
            query_intent="session_context",
            limit=limit,
            semantic_ranker=effective_semantic_ranker,
        )
        if self.native_memory_id:
            audit = dict(result.get("audit") or {})
            audit["native_memory_bound"] = True
            audit["native_memory_hits"] = len(semantic_hits)
            if semantic_failure_type:
                audit["native_memory_error_type"] = semantic_failure_type
            result["audit"] = audit
        return result

    def _brain_query_index_search(self, query: str, brain_id: str) -> list[dict]:
        from .session_memory.brain_query import project_from_brain_id

        project = project_from_brain_id(brain_id)
        filters = {"project": project} if project else None
        chunks = self.retired_index_bridge.retrieve(query, self.dataset_ids, filters=filters, limit=8)
        results: list[dict] = []
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
            results.append(
                {
                    "result_type": str(chunk.get("result_type") or metadata.get("result_type") or "index_mirror"),
                    "memory_id": str(
                        chunk.get("memory_id")
                        or metadata.get("memory_id")
                        or chunk.get("source_ref")
                        or ""
                    ),
                    "card_type": str(chunk.get("card_type") or metadata.get("card_type") or ""),
                    "summary": str(chunk.get("summary") or ""),
                    "currentness": str(chunk.get("currentness") or metadata.get("currentness") or "unknown"),
                    "score": chunk.get("score"),
                    "content_hash": str(chunk.get("content_hash") or metadata.get("content_hash") or ""),
                }
            )
        return results

    def brain_resolve(self, *, query: str = "") -> dict:
        return resolve_brain_ids(read_model=LegacyLedgerBrainReadModel(self.ledger), query=query)


def _knowledge_search_public_limit(limit: int) -> int:
    return max(1, min(10, int(limit)))


def _episode_projection_summary(episode: Any) -> str:
    payload = getattr(episode, "payload", {})
    payload = payload if isinstance(payload, Mapping) else {}
    summary = (
        payload.get("summary")
        or payload.get("title")
        or payload.get("fact")
        or getattr(episode, "entity_type", "")
        or "Derived graph projection hit."
    )
    return public_safe_text(str(summary), max_chars=280)


def _safe_float(value: Any, *, default: float = 0.0) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return default
    if score < 0:
        return 0.0
    if score > 1:
        return 1.0
    return score


def _apply_object_authority_state(obj: dict[str, Any], state: Mapping[str, Any]) -> None:
    lane = public_safe_text(str(state.get("authority_lane") or obj.get("authority_lane") or ""), max_chars=80)
    decision_type = public_safe_text(str(state.get("decision_type") or ""), max_chars=120)
    obj["authority_lane"] = lane
    obj["lifecycle_status"] = _lifecycle_status_for_authority_state(lane, decision_type, obj)
    obj["review_state"] = _review_state_for_authority_state(lane, obj)
    obj["recommended_action"] = _recommended_action_for_authority_state(lane, decision_type, obj)
    obj["authority_state"] = _object_authority_state_view(state)


def _object_authority_state_view(state: Mapping[str, Any]) -> dict[str, str]:
    return {
        "schema_version": str(state.get("schema_version") or "object_authority_state.v1"),
        "source": "ledger_object_authority_state",
        "decision_id": public_safe_text(str(state.get("decision_id") or ""), max_chars=180),
        "proposal_id": public_safe_text(str(state.get("proposal_id") or ""), max_chars=180),
        "decision_type": public_safe_text(str(state.get("decision_type") or ""), max_chars=120),
        "previous_authority_lane": public_safe_text(str(state.get("previous_authority_lane") or ""), max_chars=80),
        "authority_lane": public_safe_text(str(state.get("authority_lane") or ""), max_chars=80),
        "rollback_of_decision_id": public_safe_text(str(state.get("rollback_of_decision_id") or ""), max_chars=180),
        "supersedes_decision_id": public_safe_text(str(state.get("supersedes_decision_id") or ""), max_chars=180),
        "updated_at": public_safe_text(str(state.get("updated_at") or ""), max_chars=80),
    }


def _lifecycle_status_for_authority_state(lane: str, decision_type: str, obj: Mapping[str, Any]) -> str:
    if lane == "accepted_current":
        return "current"
    if lane == "accepted_non_current":
        if "supersed" in decision_type or "supersess" in decision_type:
            return "superseded"
        if "retir" in decision_type:
            return "retired"
        return "stale"
    if lane == "archive_only":
        return "archived"
    if lane == "rejected":
        return "rejected"
    if lane == "proposal_only":
        return "proposed"
    return public_safe_text(str(obj.get("lifecycle_status") or "observed"), max_chars=80)


def _review_state_for_authority_state(lane: str, obj: Mapping[str, Any]) -> str:
    if lane in {"accepted_current", "accepted_non_current", "archive_only"}:
        return "accepted"
    if lane == "rejected":
        return "rejected"
    if lane in {"candidate", "proposal_only"}:
        return "needs_review"
    return public_safe_text(str(obj.get("review_state") or "not_required"), max_chars=80)


def _recommended_action_for_authority_state(lane: str, decision_type: str, obj: Mapping[str, Any]) -> str:
    if lane == "accepted_current":
        return "keep"
    if lane == "accepted_non_current":
        if "supersed" in decision_type or "supersess" in decision_type:
            return "supersede"
        if "retir" in decision_type:
            return "retire"
        return "archive"
    if lane == "archive_only":
        return "archive"
    if lane == "rejected":
        return "retire"
    return public_safe_text(str(obj.get("recommended_action") or "review"), max_chars=80)


def _rebuild_object_lanes(object_pack: Mapping[str, Any], objects: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    existing_lanes = object_pack.get("lanes") if isinstance(object_pack.get("lanes"), Mapping) else {}
    lanes: dict[str, list[dict[str, Any]]] = {str(lane): [] for lane in existing_lanes}
    for lane in (
        "accepted_current",
        "accepted_non_current",
        "reference_only",
        "proposal_only",
        "archive_only",
        "derived_projection",
        "rejected",
    ):
        lanes.setdefault(lane, [])
    for obj in objects:
        lane = str(obj.get("authority_lane") or "reference_only")
        lanes.setdefault(lane, []).append(obj)
    return lanes


def _object_type_from_object_id(object_id: str) -> str:
    parts = str(object_id or "").split(":")
    if len(parts) >= 3 and parts[0] == "ko" and parts[1]:
        return public_safe_text(parts[1], max_chars=80)
    return "KnowledgeObject"
