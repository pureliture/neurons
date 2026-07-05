from __future__ import annotations

from typing import Any, Mapping

from .._util import ensure_public_safe, hash_payload, public_safe_text
from .knowledge_objects import EvidenceRef, KnowledgeObjectEnvelope


ROUTE_SPECS: dict[str, dict[str, Any]] = {
    "documentation_cleanup": {
        "required_object_types": ["RepoDocument"],
        "optional_object_types": ["ReferenceDocument", "Spec"],
        "allowed_authority_lanes": [
            "accepted_current",
            "reference_only",
            "proposal_only",
            "archive_only",
            "derived_projection",
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
            "candidate": [],
            "reference_only": [],
            "proposal_only": [],
            "archive_only": [],
            "derived_projection": [],
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
        gap = {"reason": "runtime_evidence_unverified", "deployment": dict(deployment or {})}
        pack["verification"]["runtime_unverified"].append(gap)
        pack["gaps"].append("runtime_evidence_unverified")
        pack["recommended_actions"].append({"action": "verify_runtime", "object_id": ""})
    ensure_public_safe(pack, "RuntimeTruthPack")
    return pack


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


def build_agent_context_object_packs(
    *,
    documents: list[Mapping[str, Any]],
    preferences: list[Mapping[str, Any]],
    style_profile: Mapping[str, Any],
    current_work: list[str],
    required_verification: list[str],
    guardrails: list[str],
) -> dict[str, dict[str, Any]]:
    preference_titles = [str(item.get("rule") or item.get("title") or "") for item in preferences if item]
    style_titles = [str(item.get("claim") or "") for item in style_profile.get("claims") or []]
    packs = {
        "documentation_cleanup": build_documentation_cleanup_pack(documents=documents),
        "reference_corpus": _simple_pack("reference_corpus_research", [], "ReferenceDocument"),
        "preferences": _simple_pack("code_style_preference", preference_titles, "ArtifactPreference"),
        "style": _simple_pack("code_style_preference", style_titles, "StyleRule"),
        "current_work": _simple_pack("temporal_work_recall", list(current_work), "WorkUnit"),
        "required_verification": _simple_pack("required_verification", list(required_verification), "Test"),
        "do_not_touch_boundaries": _simple_pack("do_not_touch_boundaries", list(guardrails), "ToolHandoffContext"),
    }
    ensure_public_safe(packs, "AgentContextObjectPacks")
    return packs
