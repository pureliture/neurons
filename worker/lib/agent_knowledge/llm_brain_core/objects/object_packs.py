from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .._util import ensure_public_safe, hash_payload, public_safe_text
from .knowledge_objects import EvidenceRef, KnowledgeEdge, KnowledgeObjectEnvelope


NON_CURRENT_AUTHORITY = frozenset({"stale", "superseded", "archive_candidate"})
CANDIDATE_REVIEW_ACTIONS = (
    "promote",
    "reject",
    "hold",
    "merge",
    "split",
    "stale",
    "supersede",
    "retire",
    "request_more_evidence",
)
EDITABLE_OBJECT_FIELDS = frozenset(
    {
        "title",
        "summary",
        "recommended_action",
        "evidence_refs",
        "edge_refs",
        "confidence",
        "freshness",
        "payload",
    }
)
EDITABLE_EDGE_FIELDS = frozenset(
    {
        "edge_type",
        "from_object_id",
        "to_object_id",
        "evidence_refs",
        "confidence",
        "freshness",
        "payload",
    }
)
EDITABLE_EVIDENCE_FIELDS = frozenset({"summary"})
AUTHORITY_PROTECTED_FIELDS = frozenset(
    {
        "object_id",
        "object_type",
        "authority_lane",
        "lifecycle_status",
        "review_state",
        "verification_state",
        "content_hash",
        "schema_version",
    }
)


ROUTE_SPECS: dict[str, dict[str, Any]] = {
    "documentation_cleanup": {
        "required_object_types": ["RepoDocument"],
        "optional_object_types": ["ReferenceDocument", "Spec"],
        "allowed_authority_lanes": [
            "accepted_current",
            "accepted_non_current",
            "reference_only",
            "proposal_only",
            "archive_only",
            "derived_projection",
            "rejected",
        ],
        "verification_policy": "allow_unverified_with_gap",
        "evidence_policy": "require_evidence_or_gap",
        "redaction_mode": "object_safe",
        "recommended_action_vocabulary": [
            "keep",
            "update",
            "merge",
            "supersede",
            "archive",
            "retire",
            "request_evidence",
            "review",
            "review_archive",
            "preserve_as_companion",
        ],
        "eval_assertions": [
            "separates_current_from_archive",
            "includes_recommended_action",
            "includes_evidence_or_gap",
        ],
    },
    "deployment_runtime_truth": {
        "required_object_types": ["PullRequest", "RuntimeTruth"],
        "allowed_authority_lanes": ["accepted_current", "candidate", "proposal_only"],
        "verification_policy": "runtime_verified_required_for_deployed_claim",
        "evidence_policy": "require_runtime_evidence_or_gap",
        "redaction_mode": "object_safe",
        "recommended_action_vocabulary": ["verify_runtime", "request_evidence"],
        "eval_assertions": ["merge_does_not_imply_deploy"],
    },
    "candidate_graph_review": {
        "required_object_types": ["KnowledgeObjectEnvelope"],
        "allowed_authority_lanes": ["candidate", "proposal_only", "reference_only"],
        "verification_policy": "allow_unverified_with_gap",
        "evidence_policy": "require_evidence_or_gap",
        "redaction_mode": "object_safe",
        "recommended_action_vocabulary": list(CANDIDATE_REVIEW_ACTIONS),
        "editable_object_fields": sorted(EDITABLE_OBJECT_FIELDS),
        "eval_assertions": [
            "candidate_graph_is_not_authority",
            "reviewer_edit_does_not_mutate_authority",
            "approval_board_required_for_promotion",
        ],
    },
}


def route_spec_for(route: str) -> dict[str, Any]:
    spec = ROUTE_SPECS.get(route)
    if spec is None:
        raise ValueError("unknown route spec")
    return {key: list(value) if isinstance(value, list) else value for key, value in spec.items()}


def _empty_pack(route: str) -> dict[str, Any]:
    return {
        "schema_version": "object_pack.v1",
        "route": route,
        "route_spec": route_spec_for(route) if route in ROUTE_SPECS else {},
        "objects": [],
        "edges": [],
        "evidence": [],
        "lanes": {
            "accepted_current": [],
            "accepted_non_current": [],
            "candidate": [],
            "reference_only": [],
            "proposal_only": [],
            "archive_only": [],
            "derived_projection": [],
            "rejected": [],
        },
        "verification": {
            "runtime_verified": [],
            "runtime_unverified": [],
            "unverified": [],
        },
        "recommended_actions": [],
        "confidence": {"score": 0.0, "basis": ""},
        "gaps": [],
        "audit": {"request_hash": "", "consumer": "unspecified"},
    }


def _doc_lane(status: str) -> str:
    if status == "source_of_truth":
        return "accepted_current"
    if status in {"archive_candidate", "historical", "superseded", "stale"}:
        return "proposal_only"
    if status == "generated_companion":
        return "derived_projection"
    return "reference_only"


def _doc_action(status: str) -> str:
    if status == "source_of_truth":
        return "keep"
    if status in {"archive_candidate", "historical", "superseded", "stale"}:
        return "review_archive"
    if status == "generated_companion":
        return "preserve_as_companion"
    return "review"


def _doc_lifecycle_status(lane: str) -> str:
    if lane == "accepted_current":
        return "current"
    if lane == "proposal_only":
        return "proposed"
    return "observed"


def _doc_review_state(lane: str) -> str:
    if lane == "accepted_current":
        return "accepted"
    if lane == "proposal_only":
        return "needs_review"
    return "not_required"


def _evidence_ref_view(ref: str, *, authority_lane: str = "reference_only") -> dict[str, Any]:
    safe_ref = public_safe_text(ref, max_chars=180)
    return {
        "evidence_id": safe_ref,
        "evidence_type": "memory_card_or_inventory",
        "authority_lane": authority_lane,
        "verification_state": "unverified",
        "locator_view": {
            "locator_kind": "evidence_ref",
            "display_ref": safe_ref,
            "display_ref_redacted": False,
            "locator_digest": hash_payload({"kind": "evidence_ref", "value": safe_ref}),
        },
        "summary": "Referenced redacted evidence.",
        "raw_return_capability": "denied",
    }


def _runtime_evidence_is_verified(live_evidence: Mapping[str, Any] | None) -> bool:
    if not isinstance(live_evidence, Mapping):
        return False
    return (
        str(live_evidence.get("verification_state") or "") == "runtime_verified"
        and bool(str(live_evidence.get("evidence_id") or ""))
    )


def _runtime_deployment_gap_view(deployment: Mapping[str, Any] | None) -> dict[str, Any]:
    target_ref = public_safe_text(str((deployment or {}).get("target") or "deployment"), max_chars=120)
    private_ref = str((deployment or {}).get("private_authority_ref") or "")
    return {
        "target_ref": target_ref,
        "private_authority_ref_present": bool(private_ref),
        "private_authority_ref_digest": hash_payload({"private_authority_ref": private_ref}) if private_ref else "",
        "protected_values_returned": False,
    }


def build_documentation_cleanup_pack(
    *,
    documents: list[Mapping[str, Any]],
    route: str = "documentation_cleanup",
    consumer: str = "unspecified",
) -> dict[str, Any]:
    pack = _empty_pack(route)
    pack["audit"] = {"request_hash": hash_payload([route, documents]), "consumer": consumer}
    for doc in documents:
        status = str(doc.get("status") or "active")
        lane = _doc_lane(status)
        path = str(doc.get("path") or doc.get("document_path") or "")
        content_hash = hash_payload({"path": path, "status": status, "reason": doc.get("reason")})
        evidence_refs = [str(item) for item in doc.get("evidence_refs") or [] if item]
        evs = []
        if not evidence_refs:
            ev = EvidenceRef.from_parts(
                evidence_type="file_inventory",
                authority_lane=lane if lane != "proposal_only" else "reference_only",
                verification_state="unverified",
                locator={"kind": "relative_repo_path", "value": path},
                content_hash=content_hash,
                summary=f"Inventory evidence for {path}.",
            )
            pack["evidence"].append(ev.to_view())
            evs.append(ev.evidence_id)
        else:
            evs.extend(evidence_refs)
            for ref in evidence_refs:
                pack["evidence"].append(_evidence_ref_view(ref, authority_lane="reference_only"))
        obj = KnowledgeObjectEnvelope.from_parts(
            object_type="RepoDocument",
            natural_key=path or content_hash,
            scope={"project": "neurons"},
            title=path or "RepoDocument",
            summary=str(doc.get("reason") or status),
            lifecycle_status=_doc_lifecycle_status(lane),
            authority_lane=lane,
            verification_state="source_hash_verified" if lane == "accepted_current" else "unverified",
            review_state=_doc_review_state(lane),
            content_hash=content_hash,
            evidence_refs=evs,
            confidence={"score": float(doc.get("confidence") or 0), "basis": str(doc.get("reason") or "")},
            recommended_action=_doc_action(status),
            payload={"path_ref": path, "status": status, "reason": str(doc.get("reason") or "")},
        ).to_dict()
        pack["objects"].append(obj)
        pack["lanes"][lane].append(obj)
        pack["recommended_actions"].append({"object_id": obj["object_id"], "action": _doc_action(status)})
    if not pack["lanes"]["accepted_current"]:
        pack["gaps"].append("accepted_current documents empty")
        pack["gaps"].append("review_proposals_needed")
    pack["confidence"] = {"score": 0.8 if pack["objects"] else 0.0, "basis": "document_authority_pack"}
    ensure_public_safe(pack, "DocumentationCleanupPack")
    return pack


def build_runtime_truth_pack(
    *,
    pull_request: Mapping[str, Any] | None,
    deployment: Mapping[str, Any] | None,
    live_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    pack = _empty_pack("deployment_runtime_truth")
    if pull_request:
        pr = KnowledgeObjectEnvelope.from_parts(
            object_type="PullRequest",
            natural_key=str(pull_request.get("id") or "pr"),
            scope={"project": "neurons"},
            title=str(pull_request.get("id") or "PullRequest"),
            summary="Pull request merge evidence.",
            lifecycle_status="current" if pull_request.get("merged") else "observed",
            authority_lane="candidate",
            verification_state="source_hash_verified",
            review_state="needs_review",
            content_hash=hash_payload(pull_request),
            payload={"merged": bool(pull_request.get("merged"))},
        ).to_dict()
        pack["objects"].append(pr)
        pack["lanes"]["candidate"].append(pr)
    if deployment and _runtime_evidence_is_verified(live_evidence):
        pack["verification"]["runtime_verified"].append(dict(live_evidence))
    else:
        gap = {"reason": "runtime_evidence_unverified", "deployment": _runtime_deployment_gap_view(deployment)}
        pack["verification"]["runtime_unverified"].append(gap)
        pack["gaps"].append("runtime_evidence_unverified")
        pack["recommended_actions"].append({"action": "verify_runtime", "object_id": ""})
    ensure_public_safe(pack, "RuntimeTruthPack")
    return pack


def build_candidate_graph_review_pack(
    *,
    objects: list[Mapping[str, Any]],
    edges: list[Mapping[str, Any]],
    evidence: list[Mapping[str, Any]],
    extractor: str,
    reviewer_actions: list[str] | tuple[str, ...] = CANDIDATE_REVIEW_ACTIONS,
    consumer: str = "unspecified",
) -> dict[str, Any]:
    pack = _empty_pack("candidate_graph_review")
    safe_objects = [dict(obj) for obj in objects if isinstance(obj, Mapping)]
    safe_edges = [dict(edge) for edge in edges if isinstance(edge, Mapping)]
    safe_evidence = [dict(item) for item in evidence if isinstance(item, Mapping)]
    actions = _reviewer_actions(reviewer_actions)
    graph_hash = _candidate_graph_hash(safe_objects, safe_edges, safe_evidence)
    pack.update(
        {
            "candidate_graph_hash": graph_hash,
            "extractor": public_safe_text(extractor, max_chars=120),
            "consumer": public_safe_text(consumer, max_chars=80),
            "production_mutation_performed": False,
            "authority_write_performed": False,
            "authoritative_memory_changed": False,
            "raw_body_return_capability": "denied",
            "minimal_edit_surface": {
                "supported": True,
                "editable_object_fields": sorted(EDITABLE_OBJECT_FIELDS),
                "editable_edge_fields": sorted(EDITABLE_EDGE_FIELDS),
                "editable_evidence_fields": sorted(EDITABLE_EVIDENCE_FIELDS),
                "protected_authority_fields": sorted(AUTHORITY_PROTECTED_FIELDS),
                "allowed_actions": list(actions),
            },
            "approval_board": [],
            "no_authority_mutation_proof": {
                "ai_output_lane": "candidate_graph_only",
                "promotion_requires": "approval_board_decision",
                "authority_write_performed": False,
                "authoritative_memory_changed": False,
            },
            "audit": {
                "request_hash": hash_payload([graph_hash, extractor, consumer]),
                "consumer": public_safe_text(consumer, max_chars=80),
                "candidate_graph_hash": graph_hash,
            },
        }
    )
    pack["objects"] = safe_objects
    pack["edges"] = safe_edges
    pack["evidence"] = safe_evidence
    _rebuild_lanes(pack)
    _rebuild_verification(pack)
    for obj in safe_objects:
        board_item = _approval_board_item(obj, safe_edges=safe_edges, reviewer_actions=actions)
        pack["approval_board"].append(board_item)
        action = str(obj.get("recommended_action") or "review")
        pack["recommended_actions"].append({"object_id": obj.get("object_id", ""), "action": action})
    if not pack["approval_board"]:
        pack["gaps"].append("candidate_graph_empty")
    missing_evidence = [
        str(item.get("object_id") or "")
        for item in safe_objects
        if not _object_evidence_refs(item, safe_edges=safe_edges)
    ]
    if missing_evidence:
        pack["gaps"].append("candidate_evidence_refs_missing")
    pack["confidence"] = {
        "score": 0.75 if pack["approval_board"] else 0.0,
        "basis": "candidate_graph_review_pack",
    }
    ensure_public_safe(pack, "CandidateGraphReviewPack")
    return pack


def apply_candidate_review_edits(
    pack: Mapping[str, Any],
    *,
    edits: list[Mapping[str, Any]],
    reviewer: Mapping[str, Any],
) -> dict[str, Any]:
    updated_pack = deepcopy(dict(pack))
    original_hash = str(pack.get("candidate_graph_hash") or "")
    objects = updated_pack.get("objects") if isinstance(updated_pack.get("objects"), list) else []
    edges = updated_pack.get("edges") if isinstance(updated_pack.get("edges"), list) else []
    evidence_items = updated_pack.get("evidence") if isinstance(updated_pack.get("evidence"), list) else []
    object_by_id = {
        str(obj.get("object_id") or ""): obj
        for obj in objects
        if isinstance(obj, dict) and obj.get("object_id")
    }
    edge_by_id = {
        str(edge.get("edge_id") or ""): edge
        for edge in edges
        if isinstance(edge, dict) and edge.get("edge_id")
    }
    evidence_by_id = {
        str(item.get("evidence_id") or ""): item
        for item in evidence_items
        if isinstance(item, dict) and item.get("evidence_id")
    }
    accepted_edits: list[dict[str, Any]] = []
    rejected_edits: list[dict[str, Any]] = []
    for edit in edits:
        if not isinstance(edit, Mapping):
            continue
        action = public_safe_text(str(edit.get("action") or ""), max_chars=80)
        if action == "update_object":
            _apply_object_edit(
                edit=edit,
                object_by_id=object_by_id,
                accepted_edits=accepted_edits,
                rejected_edits=rejected_edits,
            )
            continue
        if action == "update_edge":
            _apply_edge_edit(
                edit=edit,
                edge_by_id=edge_by_id,
                accepted_edits=accepted_edits,
                rejected_edits=rejected_edits,
            )
            continue
        if action == "update_evidence":
            _apply_evidence_edit(
                edit=edit,
                evidence_by_id=evidence_by_id,
                accepted_edits=accepted_edits,
                rejected_edits=rejected_edits,
            )
            continue
        object_id = public_safe_text(str(edit.get("object_id") or ""), max_chars=180)
        rejected_edits.append(
            {
                "action": action,
                "object_id": object_id,
                "reason": "unsupported_candidate_edit_action",
            }
        )
    _rebuild_lanes(updated_pack)
    _rebuild_verification(updated_pack)
    _rebuild_candidate_review_surface(updated_pack)
    updated_hash = _candidate_graph_hash(
        [dict(obj) for obj in updated_pack.get("objects") or [] if isinstance(obj, Mapping)],
        [dict(edge) for edge in updated_pack.get("edges") or [] if isinstance(edge, Mapping)],
        [dict(item) for item in updated_pack.get("evidence") or [] if isinstance(item, Mapping)],
    )
    updated_pack["candidate_graph_hash"] = updated_hash
    updated_pack["authority_write_performed"] = False
    updated_pack["authoritative_memory_changed"] = False
    updated_pack["production_mutation_performed"] = False
    updated_pack["audit"] = {
        **dict(updated_pack.get("audit") or {}),
        "candidate_graph_hash": updated_hash,
        "review_edit_hash": hash_payload([original_hash, updated_hash, accepted_edits, rejected_edits]),
    }
    result = {
        "schema_version": "candidate_review_edit_result.v1",
        "candidate_state_changed": bool(accepted_edits),
        "authority_write_performed": False,
        "authoritative_memory_changed": False,
        "production_mutation_performed": False,
        "original_extraction_preserved": True,
        "original_candidate_graph_hash": original_hash,
        "updated_candidate_graph_hash": updated_hash,
        "reviewer_ref": public_safe_text(str(reviewer.get("id") or reviewer.get("role") or "unspecified"), max_chars=120),
        "accepted_edits": accepted_edits,
        "rejected_edits": rejected_edits,
        "updated_pack": updated_pack,
    }
    ensure_public_safe(result, "CandidateReviewEditResult")
    return result


def _apply_object_edit(
    *,
    edit: Mapping[str, Any],
    object_by_id: dict[str, dict[str, Any]],
    accepted_edits: list[dict[str, Any]],
    rejected_edits: list[dict[str, Any]],
) -> None:
    action = "update_object"
    object_id = public_safe_text(str(edit.get("object_id") or ""), max_chars=180)
    target = object_by_id.get(object_id)
    if target is None:
        rejected_edits.append(
            {
                "action": action,
                "object_id": object_id,
                "reason": "candidate_object_not_found",
            }
        )
        return
    fields = edit.get("fields") if isinstance(edit.get("fields"), Mapping) else {}
    changed_fields: list[str] = []
    for field, value in fields.items():
        field_name = public_safe_text(str(field or ""), max_chars=80)
        if field_name in AUTHORITY_PROTECTED_FIELDS:
            rejected_edits.append(
                {
                    "action": action,
                    "object_id": object_id,
                    "field": field_name,
                    "reason": "authority_field_requires_approval_board_decision",
                }
            )
            continue
        if field_name not in EDITABLE_OBJECT_FIELDS:
            rejected_edits.append(
                {
                    "action": action,
                    "object_id": object_id,
                    "field": field_name,
                    "reason": "unsupported_candidate_edit_field",
                }
            )
            continue
        target[field_name] = _safe_edit_value(field_name, value)
        changed_fields.append(field_name)
    if changed_fields:
        accepted_edits.append(
            {
                "action": action,
                "object_id": object_id,
                "changed_fields": sorted(changed_fields),
            }
        )


def _apply_edge_edit(
    *,
    edit: Mapping[str, Any],
    edge_by_id: dict[str, dict[str, Any]],
    accepted_edits: list[dict[str, Any]],
    rejected_edits: list[dict[str, Any]],
) -> None:
    action = "update_edge"
    edge_id = public_safe_text(str(edit.get("edge_id") or ""), max_chars=180)
    target = edge_by_id.get(edge_id)
    if target is None:
        rejected_edits.append({"action": action, "edge_id": edge_id, "reason": "candidate_edge_not_found"})
        return
    fields = edit.get("fields") if isinstance(edit.get("fields"), Mapping) else {}
    changed_fields: list[str] = []
    for field, value in fields.items():
        field_name = public_safe_text(str(field or ""), max_chars=80)
        if field_name in AUTHORITY_PROTECTED_FIELDS:
            rejected_edits.append(
                {
                    "action": action,
                    "edge_id": edge_id,
                    "field": field_name,
                    "reason": "authority_field_requires_approval_board_decision",
                }
            )
            continue
        if field_name not in EDITABLE_EDGE_FIELDS:
            rejected_edits.append(
                {
                    "action": action,
                    "edge_id": edge_id,
                    "field": field_name,
                    "reason": "unsupported_candidate_edit_field",
                }
            )
            continue
        target[field_name] = _safe_edit_value(field_name, value)
        changed_fields.append(field_name)
    if changed_fields:
        _refresh_edge_identity(target)
        accepted_edits.append(
            {
                "action": action,
                "edge_id": edge_id,
                "updated_edge_id": target.get("edge_id", ""),
                "changed_fields": sorted(changed_fields),
            }
        )


def _apply_evidence_edit(
    *,
    edit: Mapping[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
    accepted_edits: list[dict[str, Any]],
    rejected_edits: list[dict[str, Any]],
) -> None:
    action = "update_evidence"
    evidence_id = public_safe_text(str(edit.get("evidence_id") or ""), max_chars=180)
    target = evidence_by_id.get(evidence_id)
    if target is None:
        rejected_edits.append({"action": action, "evidence_id": evidence_id, "reason": "candidate_evidence_not_found"})
        return
    fields = edit.get("fields") if isinstance(edit.get("fields"), Mapping) else {}
    changed_fields: list[str] = []
    for field, value in fields.items():
        field_name = public_safe_text(str(field or ""), max_chars=80)
        if field_name in AUTHORITY_PROTECTED_FIELDS:
            rejected_edits.append(
                {
                    "action": action,
                    "evidence_id": evidence_id,
                    "field": field_name,
                    "reason": "authority_field_requires_approval_board_decision",
                }
            )
            continue
        if field_name not in EDITABLE_EVIDENCE_FIELDS:
            rejected_edits.append(
                {
                    "action": action,
                    "evidence_id": evidence_id,
                    "field": field_name,
                    "reason": "unsupported_candidate_edit_field",
                }
            )
            continue
        target[field_name] = _safe_edit_value(field_name, value)
        changed_fields.append(field_name)
    if changed_fields:
        accepted_edits.append(
            {
                "action": action,
                "evidence_id": evidence_id,
                "changed_fields": sorted(changed_fields),
            }
        )


def _refresh_edge_identity(edge: dict[str, Any]) -> None:
    refreshed = KnowledgeEdge.from_parts(
        edge_type=str(edge.get("edge_type") or "related_to"),
        from_object_id=str(edge.get("from_object_id") or ""),
        to_object_id=str(edge.get("to_object_id") or ""),
        evidence_refs=[str(ref) for ref in edge.get("evidence_refs") or [] if ref],
        lifecycle_status=str(edge.get("lifecycle_status") or "proposed"),
        authority_lane=str(edge.get("authority_lane") or "candidate"),
        verification_state=str(edge.get("verification_state") or "unverified"),
        confidence=dict(edge.get("confidence") or {}),
        payload=dict(edge.get("payload") or {}),
    ).to_dict()
    edge.clear()
    edge.update(refreshed)


def _simple_pack(route: str, titles: list[str], object_type: str) -> dict[str, Any]:
    pack = _empty_pack(route)
    for title in titles:
        obj = KnowledgeObjectEnvelope.from_parts(
            object_type=object_type,
            natural_key=title,
            scope={"project": "neurons"},
            title=title,
            summary=title,
            lifecycle_status="observed",
            authority_lane="reference_only",
            verification_state="unverified",
            review_state="not_required",
            content_hash=hash_payload([route, title]),
            payload={},
        ).to_dict()
        pack["objects"].append(obj)
        pack["lanes"]["reference_only"].append(obj)
    return pack


def _reference_document_pack_from_documentation(documentation_pack: Mapping[str, Any]) -> dict[str, Any]:
    pack = _empty_pack("reference_corpus_research")
    source_objects = documentation_pack.get("objects") if isinstance(documentation_pack.get("objects"), (list, tuple)) else []
    for obj in source_objects:
        if not isinstance(obj, Mapping) or obj.get("authority_lane") != "reference_only":
            continue
        ref_obj = dict(obj)
        pack["objects"].append(ref_obj)
        pack["lanes"]["reference_only"].append(ref_obj)
    pack["confidence"] = {
        "score": 0.6 if pack["objects"] else 0.0,
        "basis": "reference_only_document_authority_objects",
    }
    return pack


def build_agent_context_object_packs(
    *,
    documents: list[Mapping[str, Any]],
    preferences: list[Mapping[str, Any]],
    style_profile: Mapping[str, Any],
    current_work: list[str],
    required_verification: list[str],
    guardrails: list[str],
) -> dict[str, dict[str, Any]]:
    documentation_pack = build_documentation_cleanup_pack(documents=documents)
    preference_titles = [
        str(item.get("rule") or item.get("title") or "")
        for item in preferences
        if item and str(item.get("currentness") or "") not in NON_CURRENT_AUTHORITY
    ]
    style_titles = [str(item.get("claim") or "") for item in style_profile.get("claims") or []]
    packs = {
        "documentation_cleanup": documentation_pack,
        "reference_corpus": _reference_document_pack_from_documentation(documentation_pack),
        "preferences": _simple_pack("code_style_preference", preference_titles, "ArtifactPreference"),
        "style": _simple_pack("code_style_preference", style_titles, "StyleRule"),
        "current_work": _simple_pack("temporal_work_recall", list(current_work), "WorkUnit"),
        "required_verification": _simple_pack("required_verification", list(required_verification), "Test"),
        "do_not_touch_boundaries": _simple_pack("do_not_touch_boundaries", list(guardrails), "ToolHandoffContext"),
    }
    ensure_public_safe(packs, "AgentContextObjectPacks")
    return packs


def _reviewer_actions(actions: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    safe_actions = []
    for action in actions:
        safe = public_safe_text(str(action or ""), max_chars=80)
        if safe in CANDIDATE_REVIEW_ACTIONS and safe not in safe_actions:
            safe_actions.append(safe)
    return tuple(safe_actions or CANDIDATE_REVIEW_ACTIONS)


def _candidate_graph_hash(
    objects: list[Mapping[str, Any]],
    edges: list[Mapping[str, Any]],
    evidence: list[Mapping[str, Any]],
) -> str:
    return hash_payload(
        {
            "objects": objects,
            "edges": edges,
            "evidence": evidence,
        }
    )


def _object_evidence_refs(obj: Mapping[str, Any], *, safe_edges: list[Mapping[str, Any]]) -> list[str]:
    refs = [str(ref) for ref in obj.get("evidence_refs") or [] if ref]
    object_id = str(obj.get("object_id") or "")
    for edge in safe_edges:
        if object_id not in {str(edge.get("from_object_id") or ""), str(edge.get("to_object_id") or "")}:
            continue
        refs.extend(str(ref) for ref in edge.get("evidence_refs") or [] if ref)
    return sorted(dict.fromkeys(refs))


def _object_edge_refs(obj: Mapping[str, Any], *, safe_edges: list[Mapping[str, Any]]) -> list[str]:
    object_id = str(obj.get("object_id") or "")
    refs = [str(ref) for ref in obj.get("edge_refs") or [] if ref]
    refs.extend(
        str(edge.get("edge_id") or "")
        for edge in safe_edges
        if object_id in {str(edge.get("from_object_id") or ""), str(edge.get("to_object_id") or "")}
        and edge.get("edge_id")
    )
    return sorted(dict.fromkeys(refs))


def _approval_board_item(
    obj: Mapping[str, Any],
    *,
    safe_edges: list[Mapping[str, Any]],
    reviewer_actions: tuple[str, ...],
) -> dict[str, Any]:
    lane = str(obj.get("authority_lane") or "")
    evidence_refs = _object_evidence_refs(obj, safe_edges=safe_edges)
    gaps = [] if evidence_refs else ["evidence_refs_missing"]
    editable = lane in {"candidate", "proposal_only"}
    item = {
        "schema_version": "candidate_review_board_item.v1",
        "object_id": public_safe_text(str(obj.get("object_id") or ""), max_chars=180),
        "object_type": public_safe_text(str(obj.get("object_type") or ""), max_chars=120),
        "title": public_safe_text(str(obj.get("title") or ""), max_chars=240),
        "authority_lane": public_safe_text(lane, max_chars=80),
        "review_state": public_safe_text(str(obj.get("review_state") or ""), max_chars=80),
        "editable": editable,
        "editable_fields": sorted(EDITABLE_OBJECT_FIELDS) if editable else [],
        "allowed_actions": list(reviewer_actions) if editable else [],
        "recommended_action": public_safe_text(str(obj.get("recommended_action") or "review"), max_chars=120),
        "edge_refs": _object_edge_refs(obj, safe_edges=safe_edges),
        "evidence_refs": evidence_refs,
        "confidence": dict(obj.get("confidence") or {}),
        "gaps": gaps,
    }
    ensure_public_safe(item, "CandidateReviewBoardItem")
    return item


def _rebuild_lanes(pack: dict[str, Any]) -> None:
    pack["lanes"] = {lane: [] for lane in _empty_pack("candidate_graph_review")["lanes"]}
    for obj in pack.get("objects") or []:
        if not isinstance(obj, Mapping):
            continue
        lane = str(obj.get("authority_lane") or "reference_only")
        if lane not in pack["lanes"]:
            lane = "reference_only"
        pack["lanes"][lane].append(obj)


def _rebuild_verification(pack: dict[str, Any]) -> None:
    pack["verification"] = {"runtime_verified": [], "runtime_unverified": [], "unverified": []}
    for obj in pack.get("objects") or []:
        if not isinstance(obj, Mapping):
            continue
        state = str(obj.get("verification_state") or "unverified")
        if state == "runtime_verified":
            pack["verification"]["runtime_verified"].append(obj)
        elif state == "runtime_unverified":
            pack["verification"]["runtime_unverified"].append(obj)
        elif state == "unverified":
            pack["verification"]["unverified"].append(obj)


def _rebuild_candidate_review_surface(pack: dict[str, Any]) -> None:
    edit_surface = pack.get("minimal_edit_surface") if isinstance(pack.get("minimal_edit_surface"), Mapping) else {}
    actions = _reviewer_actions(tuple(edit_surface.get("allowed_actions") or CANDIDATE_REVIEW_ACTIONS))
    safe_edges = [dict(edge) for edge in pack.get("edges") or [] if isinstance(edge, Mapping)]
    board = []
    recommended_actions = []
    for obj in pack.get("objects") or []:
        if not isinstance(obj, Mapping):
            continue
        board.append(_approval_board_item(obj, safe_edges=safe_edges, reviewer_actions=actions))
        recommended_actions.append(
            {
                "object_id": public_safe_text(str(obj.get("object_id") or ""), max_chars=180),
                "action": public_safe_text(str(obj.get("recommended_action") or "review"), max_chars=120),
            }
        )
    pack["approval_board"] = board
    pack["recommended_actions"] = recommended_actions


def _safe_edit_value(field_name: str, value: Any) -> Any:
    if field_name in {"title", "summary", "recommended_action"}:
        return public_safe_text(str(value or ""), max_chars=1024 if field_name == "summary" else 240)
    if field_name in {"edge_type", "from_object_id", "to_object_id"}:
        return public_safe_text(str(value or ""), max_chars=180)
    if field_name in {"evidence_refs", "edge_refs"}:
        return [public_safe_text(str(item), max_chars=180) for item in value or [] if item]
    if field_name in {"confidence", "freshness", "payload"} and isinstance(value, Mapping):
        safe_value = dict(value)
        ensure_public_safe(safe_value, f"CandidateReviewEdit.{field_name}")
        return safe_value
    return value
