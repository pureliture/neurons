from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote

from .._util import ensure_public_safe, hash_payload, public_safe_text, require_sha256
from .authority_policy import knowledge_object_class_from_id


ARTIFACT_PREFERENCE_APPLICATION_RECEIPT_SCHEMA = (
    "artifact_preference_application_receipt.v1"
)
ARTIFACT_PREFERENCE_EVALUATOR_TOOL = "brain_artifact_preference_evaluate"
ARTIFACT_PREFERENCE_EVALUATOR_VERSION = "v1"
ARTIFACT_PREFERENCE_COLLECTOR_ATTESTATION_SCHEMA = (
    "artifact_preference_collector_attestation.v1"
)
ARTIFACT_PREFERENCE_CARD_SCAN_LIMIT = 100
ARTIFACT_PREFERENCE_METRICS = frozenset(
    {
        "object_count",
        "relationship_count",
        "evidence_count",
        "gate_status_count",
        "hidden_gap_count",
        "protected_content_count",
    }
)
ARTIFACT_PREFERENCE_METRIC_MAX = 1_000_000
ARTIFACT_TYPE_APPLIES_TO = {
    "html_review": "html_review_artifact",
    "html_review_artifact": "html_review_artifact",
}
APPLIES_TO_EVALUATOR_PROFILE = {
    "html_review_artifact": "html_review_evidence_density_v1",
}
ARTIFACT_PREFERENCE_CONSUMERS = frozenset(
    {
        "unspecified",
        "codex",
        "claude-code",
        "gemini",
        "hermes",
        "post_deploy_mcp_capture",
    }
)
ARTIFACT_DESCRIPTOR_FIELDS = frozenset(
    {
        "artifact_type",
        "summary",
        "artifact_fingerprint",
        "metrics",
        "evidence_refs",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "applied",
        "production_mutation_performed",
        "preference_binding",
        "artifact_binding",
        "application_result",
        "consumer_surface",
        "failures",
        "gaps",
        "receipt_hash",
    }
)
_PREFERENCE_BINDING_FIELDS = frozenset(
    {
        "target_object_id",
        "project",
        "memory_id",
        "card_content_hash",
        "source_content_hash",
        "proposal_id",
        "decision_id",
        "authority_lane",
    }
)
_ARTIFACT_BINDING_FIELDS = frozenset(
    {
        "repository_hash",
        "branch_hash",
        "artifact_type",
        "artifact_fingerprint",
        "summary_hash",
        "metrics_hash",
        "evidence_refs_hash",
    }
)
_APPLICATION_RESULT_FIELDS = frozenset(
    {"evaluator_profile", "outcome", "passed_rules", "failed_rules"}
)
_CONSUMER_SURFACE_FIELDS = frozenset({"tool", "version", "consumer"})

_EVIDENCE_REF_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]{1,63}:[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$"
)
_RAW_EXTERNAL_ID_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:ragflow_)?(?:dataset|document)(?:_?id)?\s*[:=]",
    re.IGNORECASE,
)
_RAW_EXTERNAL_REF_PREFIXES = (
    "dataset:",
    "dataset_id:",
    "document:",
    "document_id:",
    "ragflow_dataset:",
    "ragflow_document:",
)
_PROTECTED_RECEIPT_KEYS = frozenset(
    {
        "dataset",
        "dataset_id",
        "document",
        "document_id",
        "ragflow_dataset",
        "ragflow_dataset_id",
        "ragflow_document",
        "ragflow_document_id",
    }
)

_PROFILE_RULES = (
    ("object_count_at_least_one", "object_count", lambda value: value >= 1),
    ("relationship_count_at_least_one", "relationship_count", lambda value: value >= 1),
    ("evidence_count_at_least_one", "evidence_count", lambda value: value >= 1),
    ("gate_status_count_at_least_one", "gate_status_count", lambda value: value >= 1),
    ("hidden_gap_count_zero", "hidden_gap_count", lambda value: value == 0),
    ("protected_content_count_zero", "protected_content_count", lambda value: value == 0),
)


def artifact_descriptor_fingerprint(
    *,
    artifact_type: str,
    summary: str,
    metrics: Mapping[str, Any],
    evidence_refs: list[str] | tuple[str, ...],
) -> str:
    safe_type = _artifact_type(artifact_type)
    safe_summary = _safe_required_text(summary, field="summary", max_chars=1200)
    safe_metrics = _artifact_metrics(metrics)
    safe_refs = _artifact_evidence_refs(evidence_refs)
    return hash_payload(
        {
            "artifact_type": safe_type,
            "summary": safe_summary,
            "metrics": safe_metrics,
            "evidence_refs": safe_refs,
        }
    )


def validate_artifact_descriptor(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != ARTIFACT_DESCRIPTOR_FIELDS:
        raise ValueError("artifact descriptor requires the exact public-safe fields")
    safe_type = _artifact_type(value["artifact_type"])
    safe_summary = _safe_required_text(value["summary"], field="summary", max_chars=1200)
    safe_metrics = _artifact_metrics(value["metrics"])
    safe_refs = _artifact_evidence_refs(value["evidence_refs"])
    safe_fingerprint = require_sha256(
        str(value["artifact_fingerprint"] or ""),
        "artifact_fingerprint",
    )
    expected_fingerprint = artifact_descriptor_fingerprint(
        artifact_type=safe_type,
        summary=safe_summary,
        metrics=safe_metrics,
        evidence_refs=safe_refs,
    )
    if safe_fingerprint != expected_fingerprint:
        raise ValueError("artifact_fingerprint does not match the canonical descriptor")
    if safe_metrics["evidence_count"] != len(safe_refs):
        raise ValueError("evidence_count must match evidence_refs")
    return {
        "artifact_type": safe_type,
        "summary": safe_summary,
        "artifact_fingerprint": safe_fingerprint,
        "metrics": safe_metrics,
        "evidence_refs": safe_refs,
    }


def evaluate_artifact_preference(
    *,
    ledger: Any,
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
    safe_repository = _safe_required_text(repository, field="repository", max_chars=240)
    safe_branch = _safe_required_text(branch, field="branch", max_chars=180)
    safe_project = _safe_required_text(project, field="project", max_chars=120)
    descriptor = validate_artifact_descriptor(
        {
            "artifact_type": artifact_type,
            "summary": summary,
            "artifact_fingerprint": artifact_fingerprint,
            "metrics": metrics,
            "evidence_refs": evidence_refs,
        }
    )
    safe_type = descriptor["artifact_type"]
    safe_summary = descriptor["summary"]
    safe_metrics = descriptor["metrics"]
    safe_refs = descriptor["evidence_refs"]
    safe_fingerprint = descriptor["artifact_fingerprint"]
    safe_consumer = _consumer(consumer)

    artifact_binding = {
        "repository_hash": hash_payload(safe_repository),
        "branch_hash": hash_payload(safe_branch),
        "artifact_type": safe_type,
        "artifact_fingerprint": safe_fingerprint,
        "summary_hash": hash_payload(safe_summary),
        "metrics_hash": hash_payload(safe_metrics),
        "evidence_refs_hash": hash_payload(safe_refs),
    }
    consumer_surface = {
        "tool": ARTIFACT_PREFERENCE_EVALUATOR_TOOL,
        "version": ARTIFACT_PREFERENCE_EVALUATOR_VERSION,
        "consumer": safe_consumer,
    }
    applies_to = ARTIFACT_TYPE_APPLIES_TO[safe_type]
    evaluator_profile = APPLIES_TO_EVALUATOR_PROFILE[applies_to]
    scan = _artifact_preference_card_scan(
        ledger,
        project=safe_project,
        applies_to=applies_to,
    )
    if scan["saturated"]:
        return _receipt(
            status="FAIL",
            applied=False,
            preference_binding={},
            artifact_binding=artifact_binding,
            evaluator_profile=evaluator_profile,
            outcome="not_evaluated",
            passed_rules=[],
            failed_rules=[],
            consumer_surface=consumer_surface,
            failures=["accepted_current_artifact_preference_scan_saturated"],
        )
    artifact_cards = scan["artifact_cards"]
    matching_cards = scan["matching_cards"]
    current_cards = scan["current_cards"]

    if not current_cards:
        if matching_cards:
            return _receipt(
                status="FAIL",
                applied=False,
                preference_binding={},
                artifact_binding=artifact_binding,
                evaluator_profile=evaluator_profile,
                outcome="not_evaluated",
                passed_rules=[],
                failed_rules=[],
                consumer_surface=consumer_surface,
                failures=["artifact_preference_not_current"],
            )
        if artifact_cards:
            return _receipt(
                status="FAIL",
                applied=False,
                preference_binding={},
                artifact_binding=artifact_binding,
                evaluator_profile="",
                outcome="not_evaluated",
                passed_rules=[],
                failed_rules=[],
                consumer_surface=consumer_surface,
                failures=["unsupported_artifact_preference_profile"],
            )
        return _receipt(
            status="FAIL",
            applied=False,
            preference_binding={},
            artifact_binding=artifact_binding,
            evaluator_profile=evaluator_profile,
            outcome="not_evaluated",
            passed_rules=[],
            failed_rules=[],
            consumer_surface=consumer_surface,
            failures=["accepted_current_artifact_preference_missing"],
        )
    if len(current_cards) != 1:
        return _receipt(
            status="FAIL",
            applied=False,
            preference_binding={},
            artifact_binding=artifact_binding,
            evaluator_profile=evaluator_profile,
            outcome="not_evaluated",
            passed_rules=[],
            failed_rules=[],
            consumer_surface=consumer_surface,
            failures=["multiple_accepted_current_artifact_preferences"],
        )

    card = current_cards[0]
    authority = _authority_snapshot(ledger, card=card, project=safe_project)
    before_guard = _evaluation_guard(current_cards=current_cards, authority=authority)
    authority_failures = _authority_failures(
        authority,
        project=safe_project,
        applies_to=applies_to,
        evaluator_profile=evaluator_profile,
    )
    try:
        preference_binding = _preference_binding(authority)
    except ValueError:
        preference_binding = {}
        authority_failures.append("canonical_artifact_preference_binding_invalid")
    authority_failures = list(dict.fromkeys(authority_failures))
    if authority_failures:
        return _receipt(
            status="FAIL",
            applied=False,
            preference_binding=preference_binding,
            artifact_binding=artifact_binding,
            evaluator_profile=evaluator_profile,
            outcome="not_evaluated",
            passed_rules=[],
            failed_rules=[],
            consumer_surface=consumer_surface,
            failures=authority_failures,
        )

    passed_rules = [
        rule_id
        for rule_id, metric, predicate in _PROFILE_RULES
        if predicate(safe_metrics[metric])
    ]
    failed_rules = [
        rule_id
        for rule_id, metric, predicate in _PROFILE_RULES
        if not predicate(safe_metrics[metric])
    ]
    after_scan = _artifact_preference_card_scan(
        ledger,
        project=safe_project,
        applies_to=applies_to,
    )
    after_current_cards = after_scan["current_cards"]
    if after_scan["saturated"] or len(after_current_cards) != 1:
        return _receipt(
            status="FAIL",
            applied=False,
            preference_binding=preference_binding,
            artifact_binding=artifact_binding,
            evaluator_profile=evaluator_profile,
            outcome="not_evaluated",
            passed_rules=[],
            failed_rules=[],
            consumer_surface=consumer_surface,
            failures=["authority_decision_drift_during_evaluation"],
        )
    after_authority = _authority_snapshot(
        ledger,
        card=after_current_cards[0],
        project=safe_project,
    )
    after_guard = _evaluation_guard(
        current_cards=after_current_cards,
        authority=after_authority,
    )
    if hash_payload(before_guard) != hash_payload(after_guard):
        return _receipt(
            status="FAIL",
            applied=False,
            preference_binding=preference_binding,
            artifact_binding=artifact_binding,
            evaluator_profile=evaluator_profile,
            outcome="not_evaluated",
            passed_rules=[],
            failed_rules=[],
            consumer_surface=consumer_surface,
            failures=["authority_decision_drift_during_evaluation"],
        )

    return _receipt(
        status="PASS" if not failed_rules else "FAIL",
        applied=True,
        preference_binding=preference_binding,
        artifact_binding=artifact_binding,
        evaluator_profile=evaluator_profile,
        outcome="pass" if not failed_rules else "fail",
        passed_rules=passed_rules,
        failed_rules=failed_rules,
        consumer_surface=consumer_surface,
        failures=failed_rules,
    )


def artifact_preference_application_receipt_is_valid(value: Any) -> bool:
    if (
        not isinstance(value, Mapping)
        or _contains_protected_receipt_key(value)
        or _contains_raw_external_id_receipt_value(value)
        or set(value) != _RECEIPT_FIELDS
    ):
        return False
    preference_binding = (
        value.get("preference_binding")
        if isinstance(value.get("preference_binding"), Mapping)
        else {}
    )
    artifact_binding = (
        value.get("artifact_binding")
        if isinstance(value.get("artifact_binding"), Mapping)
        else {}
    )
    application_result = (
        value.get("application_result")
        if isinstance(value.get("application_result"), Mapping)
        else {}
    )
    consumer_surface = (
        value.get("consumer_surface")
        if isinstance(value.get("consumer_surface"), Mapping)
        else {}
    )
    if (
        set(preference_binding) != _PREFERENCE_BINDING_FIELDS
        or set(artifact_binding) != _ARTIFACT_BINDING_FIELDS
        or set(application_result) != _APPLICATION_RESULT_FIELDS
        or set(consumer_surface) != _CONSUMER_SURFACE_FIELDS
        or not all(
            isinstance(preference_binding.get(field), str)
            for field in _PREFERENCE_BINDING_FIELDS
        )
        or not all(
            isinstance(artifact_binding.get(field), str)
            for field in _ARTIFACT_BINDING_FIELDS
        )
        or not isinstance(application_result.get("evaluator_profile"), str)
        or not isinstance(application_result.get("outcome"), str)
        or not all(
            isinstance(consumer_surface.get(field), str)
            for field in _CONSUMER_SURFACE_FIELDS
        )
        or not isinstance(value.get("failures"), list)
        or not isinstance(value.get("gaps"), list)
        or not isinstance(application_result.get("passed_rules"), list)
        or not isinstance(application_result.get("failed_rules"), list)
        or not all(isinstance(item, str) for item in value.get("failures", []))
        or not all(isinstance(item, str) for item in value.get("gaps", []))
        or not all(
            isinstance(item, str)
            for item in application_result.get("passed_rules", [])
        )
        or not all(
            isinstance(item, str)
            for item in application_result.get("failed_rules", [])
        )
    ):
        return False
    expected_hash = hash_payload(
        {
            "preference_binding": dict(preference_binding),
            "artifact_binding": dict(artifact_binding),
            "application_result": dict(application_result),
            "consumer_surface": dict(consumer_surface),
        }
    )
    try:
        ensure_public_safe(dict(value), "ArtifactPreferenceApplicationReceipt")
        require_sha256(str(value.get("receipt_hash") or ""), "receipt_hash")
        require_sha256(
            str(preference_binding.get("source_content_hash") or ""),
            "preference_binding.source_content_hash",
        )
        require_sha256(
            str(preference_binding.get("card_content_hash") or ""),
            "preference_binding.card_content_hash",
        )
        require_sha256(
            str(artifact_binding.get("artifact_fingerprint") or ""),
            "artifact_binding.artifact_fingerprint",
        )
        for field in (
            "repository_hash",
            "branch_hash",
            "summary_hash",
            "metrics_hash",
            "evidence_refs_hash",
        ):
            require_sha256(str(artifact_binding.get(field) or ""), f"artifact_binding.{field}")
    except ValueError:
        return False
    return (
        value.get("schema_version") == ARTIFACT_PREFERENCE_APPLICATION_RECEIPT_SCHEMA
        and value.get("status") == "PASS"
        and value.get("applied") is True
        and value.get("production_mutation_performed") is False
        and not list(value.get("failures") or [])
        and not list(value.get("gaps") or [])
        and str(preference_binding.get("project") or "")
        and str(preference_binding.get("memory_id") or "")
        and knowledge_object_class_from_id(
            str(preference_binding.get("target_object_id") or "")
        )
        == "ArtifactPreference"
        and str(preference_binding.get("proposal_id") or "")
        and str(preference_binding.get("decision_id") or "")
        and preference_binding.get("authority_lane") == "accepted_current"
        and artifact_binding.get("artifact_type") in ARTIFACT_TYPE_APPLIES_TO
        and application_result.get("evaluator_profile")
        == "html_review_evidence_density_v1"
        and application_result.get("outcome") == "pass"
        and set(application_result.get("passed_rules") or [])
        == {rule_id for rule_id, _, _ in _PROFILE_RULES}
        and len(application_result.get("passed_rules") or []) == len(_PROFILE_RULES)
        and not list(application_result.get("failed_rules") or [])
        and consumer_surface
        == {
            "tool": ARTIFACT_PREFERENCE_EVALUATOR_TOOL,
            "version": ARTIFACT_PREFERENCE_EVALUATOR_VERSION,
            "consumer": "post_deploy_mcp_capture",
        }
        and value.get("receipt_hash") == expected_hash
    )


def _artifact_preference_card_scan(
    ledger: Any,
    *,
    project: str,
    applies_to: str,
) -> dict[str, Any]:
    cards = ledger.list_llm_brain_memory_cards(
        project=project,
        accepted_only=True,
        current_only=False,
        limit=ARTIFACT_PREFERENCE_CARD_SCAN_LIMIT + 1,
    )
    artifact_cards = [card for card in cards if _is_artifact_preference_card(card)]
    matching_cards = [
        card for card in artifact_cards if _card_applies_to(card) == applies_to
    ]
    current_cards = sorted(
        (
            card
            for card in matching_cards
            if str(card.get("currentness") or "") == "current"
        ),
        key=lambda card: str(card.get("memory_id") or ""),
    )
    return {
        "saturated": len(cards) >= ARTIFACT_PREFERENCE_CARD_SCAN_LIMIT + 1,
        "artifact_cards": artifact_cards,
        "matching_cards": matching_cards,
        "current_cards": current_cards,
    }


def _evaluation_guard(
    *,
    current_cards: list[Mapping[str, Any]],
    authority: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "matching_current_cards": [dict(card) for card in current_cards],
        "selected_authority": dict(authority),
    }


def _authority_snapshot(ledger: Any, *, card: Mapping[str, Any], project: str) -> dict[str, Any]:
    payload = card.get("typed_payload") if isinstance(card.get("typed_payload"), Mapping) else {}
    target_object_id = str(payload.get("target_object_id") or "")
    proposal_id = str(payload.get("authority_proposal_id") or "")
    decision_id = str(payload.get("authority_decision_id") or "")
    return {
        "card": dict(card),
        "state": ledger.get_object_authority_state(target_object_id),
        "proposal": ledger.get_object_review_proposal(proposal_id),
        "decision": next(
            (
                decision
                for decision in ledger.list_object_authority_decisions(
                    target_object_id=target_object_id,
                    limit=100,
                )
                if str(decision.get("decision_id") or "") == decision_id
            ),
            {},
        ),
        "project": project,
    }


def _preference_binding(authority: Mapping[str, Any]) -> dict[str, str]:
    card = authority.get("card") if isinstance(authority.get("card"), Mapping) else {}
    payload = card.get("typed_payload") if isinstance(card.get("typed_payload"), Mapping) else {}
    state = authority.get("state") if isinstance(authority.get("state"), Mapping) else {}
    return {
        "target_object_id": _safe_required_text(
            payload.get("target_object_id"),
            field="target_object_id",
            max_chars=180,
        ),
        "project": _safe_required_text(
            card.get("project"),
            field="project",
            max_chars=120,
        ),
        "memory_id": _safe_required_text(
            card.get("memory_id"),
            field="memory_id",
            max_chars=180,
        ),
        "card_content_hash": require_sha256(
            str(card.get("content_hash") or ""),
            "card_content_hash",
        ),
        "source_content_hash": require_sha256(
            str(payload.get("source_content_hash") or ""),
            "source_content_hash",
        ),
        "proposal_id": _safe_required_text(
            payload.get("authority_proposal_id"),
            field="proposal_id",
            max_chars=180,
        ),
        "decision_id": _safe_required_text(
            payload.get("authority_decision_id"),
            field="decision_id",
            max_chars=180,
        ),
        "authority_lane": _safe_required_text(
            state.get("authority_lane"),
            field="authority_lane",
            max_chars=80,
        ),
    }


def _authority_failures(
    authority: Mapping[str, Any],
    *,
    project: str,
    applies_to: str,
    evaluator_profile: str,
) -> list[str]:
    card = authority.get("card") if isinstance(authority.get("card"), Mapping) else {}
    payload = card.get("typed_payload") if isinstance(card.get("typed_payload"), Mapping) else {}
    state = authority.get("state") if isinstance(authority.get("state"), Mapping) else {}
    proposal = authority.get("proposal") if isinstance(authority.get("proposal"), Mapping) else {}
    proposed_object = (
        proposal.get("proposed_object")
        if isinstance(proposal.get("proposed_object"), Mapping)
        else {}
    )
    proposed_scope = (
        proposed_object.get("scope")
        if isinstance(proposed_object.get("scope"), Mapping)
        else {}
    )
    proposed_payload = (
        proposed_object.get("payload")
        if isinstance(proposed_object.get("payload"), Mapping)
        else {}
    )
    decision = authority.get("decision") if isinstance(authority.get("decision"), Mapping) else {}
    target_object_id = str(payload.get("target_object_id") or "")
    proposal_id = str(payload.get("authority_proposal_id") or "")
    decision_id = str(payload.get("authority_decision_id") or "")
    source_content_hash = str(payload.get("source_content_hash") or "")
    failures: list[str] = []

    if (
        str(card.get("project") or "") != project
        or str(card.get("card_type") or "") != "preference"
        or str(card.get("lifecycle_state") or "")
        not in {"accepted", "human_accepted", "auto_accepted"}
        or str(card.get("approval_state") or "") not in {"approved", "auto_accepted"}
        or str(card.get("currentness") or "") != "current"
        or str(card.get("freshness") or "") != "current"
        or list(card.get("superseded_by") or [])
        or str(payload.get("source_object_type") or "") != "ArtifactPreference"
        or knowledge_object_class_from_id(target_object_id) != "ArtifactPreference"
    ):
        failures.append("canonical_artifact_preference_card_mismatch")
    try:
        require_sha256(source_content_hash, "source_content_hash")
        card_content_hash = require_sha256(
            str(card.get("content_hash") or ""),
            "card.content_hash",
        )
        card_hash = require_sha256(str(card.get("card_hash") or ""), "card.card_hash")
        hash_source = dict(card)
        hash_source.pop("content_hash", None)
        hash_source.pop("card_hash", None)
        if card_content_hash != hash_payload(hash_source) or card_hash != card_content_hash:
            raise ValueError("card content hash does not match canonical card")
    except ValueError:
        failures.append("canonical_artifact_preference_content_hash_mismatch")
    stored_profile = str(payload.get("evaluator_profile") or "")
    if str(payload.get("applies_to") or "") != applies_to:
        failures.append("artifact_preference_applies_to_mismatch")
    if stored_profile and stored_profile != evaluator_profile:
        failures.append("unsupported_artifact_preference_profile")
    if (
        str(state.get("project") or "") != project
        or str(state.get("target_object_id") or "") != target_object_id
        or str(state.get("authority_lane") or "") != "accepted_current"
        or str(state.get("proposal_id") or "") != proposal_id
        or str(state.get("decision_id") or "") != decision_id
        or str(state.get("decision_type") or "") != "accept_current"
    ):
        failures.append("artifact_preference_authority_state_mismatch")
    if (
        str(proposal.get("project") or "") != project
        or str(proposal.get("proposal_id") or "") != proposal_id
        or str(proposal.get("proposal_type") or "") != "propose_current"
        or str(proposal.get("target_object_id") or "") != target_object_id
        or str(proposal.get("object_type") or "") != "ArtifactPreference"
        or str(proposal.get("status") or "") != "accepted"
        or str(proposal.get("decision_id") or "") != decision_id
        or str(proposed_object.get("object_id") or "") != target_object_id
        or str(proposed_object.get("object_type") or "") != "ArtifactPreference"
        or str(proposed_scope.get("project") or "") != project
        or str(proposed_object.get("content_hash") or "") != source_content_hash
        or str(proposed_payload.get("applies_to") or "") != applies_to
    ):
        failures.append("artifact_preference_proposal_lineage_mismatch")
    if (
        str(decision.get("project") or "") != project
        or str(decision.get("proposal_id") or "") != proposal_id
        or str(decision.get("decision_id") or "") != decision_id
        or str(decision.get("target_object_id") or "") != target_object_id
        or str(decision.get("decision_type") or "") != "accept_current"
        or str(decision.get("new_authority_lane") or "") != "accepted_current"
    ):
        failures.append("artifact_preference_decision_lineage_mismatch")
    return list(dict.fromkeys(failures))


def _receipt(
    *,
    status: str,
    applied: bool,
    preference_binding: Mapping[str, Any],
    artifact_binding: Mapping[str, Any],
    evaluator_profile: str,
    outcome: str,
    passed_rules: list[str],
    failed_rules: list[str],
    consumer_surface: Mapping[str, Any],
    failures: list[str] | None = None,
    gaps: list[str] | None = None,
) -> dict[str, Any]:
    application_result = {
        "evaluator_profile": evaluator_profile,
        "outcome": outcome,
        "passed_rules": list(passed_rules),
        "failed_rules": list(failed_rules),
    }
    receipt = {
        "schema_version": ARTIFACT_PREFERENCE_APPLICATION_RECEIPT_SCHEMA,
        "status": status,
        "applied": applied,
        "production_mutation_performed": False,
        "preference_binding": dict(preference_binding),
        "artifact_binding": dict(artifact_binding),
        "application_result": application_result,
        "consumer_surface": dict(consumer_surface),
        "failures": list(failures or []),
        "gaps": list(gaps or []),
    }
    receipt["receipt_hash"] = hash_payload(
        {
            "preference_binding": receipt["preference_binding"],
            "artifact_binding": receipt["artifact_binding"],
            "application_result": receipt["application_result"],
            "consumer_surface": receipt["consumer_surface"],
        }
    )
    ensure_public_safe(receipt, "ArtifactPreferenceApplicationReceipt")
    return receipt


def _is_artifact_preference_card(card: Mapping[str, Any]) -> bool:
    payload = card.get("typed_payload") if isinstance(card.get("typed_payload"), Mapping) else {}
    return (
        str(card.get("card_type") or "") == "preference"
        and str(payload.get("source_object_type") or "") == "ArtifactPreference"
        and knowledge_object_class_from_id(str(payload.get("target_object_id") or ""))
        == "ArtifactPreference"
    )


def _card_applies_to(card: Mapping[str, Any]) -> str:
    payload = card.get("typed_payload") if isinstance(card.get("typed_payload"), Mapping) else {}
    return str(payload.get("applies_to") or "")


def _artifact_type(value: str) -> str:
    safe = _safe_required_text(value, field="artifact_type", max_chars=80)
    if safe not in ARTIFACT_TYPE_APPLIES_TO:
        raise ValueError("unsupported artifact_type")
    return safe


def _consumer(value: str) -> str:
    safe = _safe_required_text(value or "unspecified", field="consumer", max_chars=80)
    if safe not in ARTIFACT_PREFERENCE_CONSUMERS:
        raise ValueError("unsupported consumer")
    return safe


def _artifact_metrics(value: Mapping[str, Any]) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError("metrics must be an object")
    keys = set(value)
    unknown = sorted(str(key) for key in keys - ARTIFACT_PREFERENCE_METRICS)
    missing = sorted(ARTIFACT_PREFERENCE_METRICS - keys)
    if unknown:
        raise ValueError("metrics contains unknown fields")
    if missing:
        raise ValueError("metrics is missing required fields")
    metrics: dict[str, int] = {}
    for key in sorted(ARTIFACT_PREFERENCE_METRICS):
        metric = value[key]
        if not isinstance(metric, int) or isinstance(metric, bool):
            raise ValueError("metrics values must be integers")
        if metric < 0 or metric > ARTIFACT_PREFERENCE_METRIC_MAX:
            raise ValueError("metrics values are outside the allowed range")
        metrics[key] = metric
    return metrics


def _artifact_evidence_refs(value: list[str] | tuple[str, ...]) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("evidence_refs must be an array")
    if len(value) > 64:
        raise ValueError("evidence_refs exceeds the allowed count")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str):
            raise ValueError("evidence_refs must contain opaque strings")
        safe = _safe_required_text(ref, field="evidence_refs", max_chars=180)
        if (
            _EVIDENCE_REF_RE.fullmatch(safe) is None
            or safe.casefold().startswith(_RAW_EXTERNAL_REF_PREFIXES)
            or _contains_raw_external_id(safe)
        ):
            raise ValueError("evidence_refs must contain opaque internal refs")
        refs.append(safe)
    if len(set(refs)) != len(refs):
        raise ValueError("evidence_refs must be unique")
    return refs


def _safe_required_text(value: Any, *, field: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    ensure_public_safe(value, field)
    if _contains_raw_external_id(value):
        raise ValueError(f"{field} contains a raw external ID")
    safe = public_safe_text(value, max_chars=max_chars)
    if not safe:
        raise ValueError(f"{field} is required")
    return safe


def _contains_raw_external_id(value: str) -> bool:
    decoded = value
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", decoded)
    normalized = re.sub(r"[.\-\s]+", "_", snake).casefold()
    normalized = re.sub(r"_*([:=])_*", r"\1", normalized)
    return decoded != value or _RAW_EXTERNAL_ID_RE.search(normalized) is not None


def _contains_protected_receipt_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
            normalized = re.sub(r"[.\-\s]+", "_", snake).casefold()
            if normalized in _PROTECTED_RECEIPT_KEYS:
                return True
            if _contains_protected_receipt_key(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_protected_receipt_key(item) for item in value)
    return False


def _contains_raw_external_id_receipt_value(value: Any) -> bool:
    if isinstance(value, str):
        return _contains_raw_external_id(value)
    if isinstance(value, Mapping):
        return any(_contains_raw_external_id_receipt_value(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_raw_external_id_receipt_value(item) for item in value)
    return False
