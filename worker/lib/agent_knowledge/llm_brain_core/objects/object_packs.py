from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .._util import ensure_public_safe, hash_payload, public_safe_text, short_hash
from .knowledge_objects import AuthorityDecision, EvidenceRef, KnowledgeEdge, KnowledgeObjectEnvelope


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
CANDIDATE_REVIEW_EDIT_ACTIONS = (
    "update_object",
    "update_edge",
    "update_evidence",
    "add_edge",
    "remove_edge",
    "add_evidence",
    "remove_evidence",
)
EDITABLE_CANDIDATE_LANES = frozenset({"candidate", "proposal_only"})
EDITABLE_EVIDENCE_LANES = frozenset({"candidate", "proposal_only", "reference_only"})
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
    "code_change_impact": {
        "required_object_types": ["RepoFile", "VerificationCommand"],
        "optional_object_types": ["RuntimeSurface", "McpTool", "Test"],
        "allowed_authority_lanes": ["reference_only", "candidate", "proposal_only"],
        "verification_policy": "local_tests_and_live_runtime_separated",
        "evidence_policy": "require_current_file_or_gap",
        "redaction_mode": "object_safe",
        "recommended_action_vocabulary": [
            "inspect_change",
            "run_tests",
            "verify_runtime",
            "request_evidence",
        ],
        "eval_assertions": [
            "source_file_maps_to_verification_commands",
            "source_file_maps_to_runtime_surface_when_applicable",
            "local_test_success_does_not_imply_runtime_ready",
        ],
    },
    "candidate_graph_review": {
        "required_object_types": ["KnowledgeObjectEnvelope"],
        "allowed_authority_lanes": ["candidate", "proposal_only", "reference_only"],
        "verification_policy": "allow_unverified_with_gap",
        "evidence_policy": "require_evidence_or_gap",
        "redaction_mode": "object_safe",
        "recommended_action_vocabulary": list(CANDIDATE_REVIEW_ACTIONS),
        "editable_object_fields": sorted(EDITABLE_OBJECT_FIELDS),
        "editable_edge_fields": sorted(EDITABLE_EDGE_FIELDS),
        "editable_evidence_fields": sorted(EDITABLE_EVIDENCE_FIELDS),
        "supported_edit_actions": list(CANDIDATE_REVIEW_EDIT_ACTIONS),
        "eval_assertions": [
            "candidate_graph_is_not_authority",
            "reviewer_edit_does_not_mutate_authority",
            "approval_board_required_for_promotion",
            "approval_board_decision_promotes_authority",
            "production_decision_denied_without_gate",
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


def build_code_change_impact_pack(
    *,
    current_files: list[str],
    route: str = "code_change_impact",
    consumer: str = "unspecified",
) -> dict[str, Any]:
    pack = _empty_pack(route)
    safe_files = [_safe_repo_path_ref(path) for path in current_files if str(path or "").strip()]
    safe_files = [path for path in safe_files if path]
    pack["audit"] = {
        "request_hash": hash_payload([route, safe_files]),
        "consumer": public_safe_text(consumer, max_chars=80),
        "object_pack_route_source": "code_change_impact_pack",
    }
    if not safe_files:
        pack["gaps"].append("current_files_missing")

    repo_objects = [_repo_file_object(path, pack=pack) for path in safe_files]
    verification_objects = [_verification_command_object(command, pack=pack) for command in _impact_verification_commands(safe_files)]
    runtime_surface = _runtime_surface_object(pack=pack)
    mcp_tool = _mcp_tool_object(pack=pack)

    pack["objects"].extend(repo_objects)
    pack["objects"].extend(verification_objects)
    pack["objects"].append(runtime_surface)
    pack["objects"].append(mcp_tool)

    for repo_obj in repo_objects:
        for command_obj in verification_objects:
            pack["edges"].append(
                KnowledgeEdge.from_parts(
                    edge_type="validated_by",
                    from_object_id=repo_obj["object_id"],
                    to_object_id=command_obj["object_id"],
                    evidence_refs=sorted(set(repo_obj.get("evidence_refs", []) + command_obj.get("evidence_refs", []))),
                    lifecycle_status="observed",
                    authority_lane="reference_only",
                    verification_state="unverified",
                    confidence={"score": 0.74, "basis": "route_spec_verification_mapping"},
                    payload={"mutation_performed": False},
                ).to_dict()
            )
        pack["edges"].append(
            KnowledgeEdge.from_parts(
                edge_type="requires_live_evidence",
                from_object_id=repo_obj["object_id"],
                to_object_id=runtime_surface["object_id"],
                evidence_refs=list(repo_obj.get("evidence_refs", [])),
                lifecycle_status="observed",
                authority_lane="candidate",
                verification_state="runtime_unverified",
                confidence={"score": 0.68, "basis": "runtime_read_path_may_be_impacted"},
                payload={"deployed_runtime_claim": False, "mutation_performed": False},
            ).to_dict()
        )
    pack["edges"].append(
        KnowledgeEdge.from_parts(
            edge_type="exposes_tool",
            from_object_id=runtime_surface["object_id"],
            to_object_id=mcp_tool["object_id"],
            evidence_refs=list(runtime_surface.get("evidence_refs", [])),
            lifecycle_status="observed",
            authority_lane="candidate",
            verification_state="runtime_unverified",
            confidence={"score": 0.7, "basis": "object_native_read_path_contract"},
            payload={"deployed_runtime_claim": False, "mutation_performed": False},
        ).to_dict()
    )

    _rebuild_lanes(pack)
    _rebuild_verification(pack)
    _refresh_empty_lane_gaps(pack)
    pack["freshness_gaps"] = ["source_freshness_unverified"]
    pack["verification"]["freshness_gaps"] = [{"reason": "source_freshness_unverified"}]
    pack["verification"]["runtime_unverified"].append(
        {
            "reason": "live_runtime_impact_unverified",
            "surface_ref": runtime_surface["object_id"],
            "protected_values_returned": False,
        }
    )
    for obj in repo_objects:
        pack["recommended_actions"].append({"object_id": obj["object_id"], "action": "inspect_change"})
    for obj in verification_objects:
        pack["recommended_actions"].append({"object_id": obj["object_id"], "action": "run_tests"})
    pack["recommended_actions"].append({"object_id": runtime_surface["object_id"], "action": "verify_runtime"})
    pack["gaps"].append("live_runtime_impact_unverified")
    pack["gaps"].append("production_mutation_forbidden")
    pack["gaps"].append("source_freshness_unverified")
    pack["confidence"] = {
        "score": 0.72 if repo_objects else 0.42,
        "basis": "deterministic_code_change_impact_route",
    }
    ensure_public_safe(pack, "CodeChangeImpactPack")
    return pack


def _safe_repo_path_ref(path: str) -> str:
    raw = " ".join(str(path or "").strip().split())
    if not raw:
        return ""
    if raw.startswith(("/", "~")) or raw.startswith("\\\\") or ":/" in raw or ":\\" in raw:
        return f"repo_path:{short_hash(raw)}"
    return public_safe_text(raw, max_chars=240)


def _impact_verification_commands(current_files: list[str]) -> list[str]:
    commands = ["cd worker && uv run pytest -q"]
    if any(path.startswith("worker/lib/agent_knowledge/llm_brain_core/") for path in current_files):
        commands.insert(
            0,
            "cd worker && uv run pytest -q tests/test_neuron_cli.py tests/test_neuron_mcp_stdio.py tests/test_object_packs.py",
        )
    return commands


def _repo_file_object(path: str, *, pack: dict[str, Any]) -> dict[str, Any]:
    evidence = EvidenceRef.from_parts(
        evidence_type="current_file_locator",
        authority_lane="reference_only",
        verification_state="unverified",
        locator={"kind": "relative_repo_path", "value": path},
        content_hash=hash_payload({"path": path, "kind": "current_file_locator"}),
        summary=f"Current file locator for {path}.",
        producer={"tool": "brain_objects_query", "route": "code_change_impact"},
    )
    pack["evidence"].append(evidence.to_view())
    return KnowledgeObjectEnvelope.from_parts(
        object_type="RepoFile",
        natural_key=path,
        scope={"project": "neurons"},
        title=path,
        summary="Current file selected for code-change impact analysis.",
        lifecycle_status="observed",
        authority_lane="reference_only",
        verification_state="unverified",
        review_state="not_required",
        content_hash=hash_payload({"object_type": "RepoFile", "path": path}),
        evidence_refs=[evidence.evidence_id],
        confidence={"score": 0.76, "basis": "current_files_argument"},
        recommended_action="inspect_change",
        payload={"path_ref": path, "mutation_performed": False},
    ).to_dict()


def _verification_command_object(command: str, *, pack: dict[str, Any]) -> dict[str, Any]:
    safe_command = public_safe_text(command, max_chars=240)
    evidence = EvidenceRef.from_parts(
        evidence_type="verification_command_contract",
        authority_lane="reference_only",
        verification_state="unverified",
        locator={"kind": "command_ref", "value": safe_command},
        content_hash=hash_payload({"command": safe_command, "route": "code_change_impact"}),
        summary=f"Verification command candidate: {safe_command}.",
        producer={"tool": "brain_objects_query", "route": "code_change_impact"},
    )
    pack["evidence"].append(evidence.to_view())
    return KnowledgeObjectEnvelope.from_parts(
        object_type="VerificationCommand",
        natural_key=safe_command,
        scope={"project": "neurons"},
        title=safe_command,
        summary="Local verification command candidate; success is not production readiness.",
        lifecycle_status="observed",
        authority_lane="reference_only",
        verification_state="unverified",
        review_state="not_required",
        content_hash=hash_payload({"object_type": "VerificationCommand", "command": safe_command}),
        evidence_refs=[evidence.evidence_id],
        confidence={"score": 0.74, "basis": "repo_test_policy"},
        recommended_action="run_tests",
        payload={"command_ref": safe_command, "production_readiness_claim": False},
    ).to_dict()


def _runtime_surface_object(*, pack: dict[str, Any]) -> dict[str, Any]:
    evidence = EvidenceRef.from_parts(
        evidence_type="runtime_surface_contract",
        authority_lane="reference_only",
        verification_state="runtime_unverified",
        locator={"kind": "runtime_surface_ref", "value": "lbrain_mcp_read_path"},
        content_hash=hash_payload({"surface": "lbrain_mcp_read_path", "route": "code_change_impact"}),
        summary="Runtime read-path impact requires live deployed evidence.",
        producer={"tool": "brain_objects_query", "route": "code_change_impact"},
        gaps=["live_runtime_impact_unverified"],
    )
    pack["evidence"].append(evidence.to_view())
    return KnowledgeObjectEnvelope.from_parts(
        object_type="RuntimeSurface",
        natural_key="lbrain_mcp_read_path",
        scope={"project": "neurons"},
        title="LBrain MCP read path",
        summary="Deployed read path that must be checked separately from local tests.",
        lifecycle_status="observed",
        authority_lane="candidate",
        verification_state="runtime_unverified",
        review_state="needs_review",
        content_hash=hash_payload({"object_type": "RuntimeSurface", "surface": "lbrain_mcp_read_path"}),
        evidence_refs=[evidence.evidence_id],
        confidence={"score": 0.68, "basis": "runtime_surface_contract"},
        recommended_action="verify_runtime",
        payload={"deployed_runtime_claim": False, "mutation_performed": False},
    ).to_dict()


def _mcp_tool_object(*, pack: dict[str, Any]) -> dict[str, Any]:
    evidence = EvidenceRef.from_parts(
        evidence_type="mcp_tool_contract",
        authority_lane="reference_only",
        verification_state="runtime_unverified",
        locator={"kind": "mcp_tool_ref", "value": "brain_objects_query"},
        content_hash=hash_payload({"tool": "brain_objects_query", "route": "code_change_impact"}),
        summary="Object-native query tool route requires live MCP smoke before runtime readiness claim.",
        producer={"tool": "brain_objects_query", "route": "code_change_impact"},
        gaps=["live_runtime_impact_unverified"],
    )
    pack["evidence"].append(evidence.to_view())
    return KnowledgeObjectEnvelope.from_parts(
        object_type="McpTool",
        natural_key="brain_objects_query",
        scope={"project": "neurons"},
        title="brain_objects_query",
        summary="Object-native read tool that should expose the code-change-impact route after deployment.",
        lifecycle_status="observed",
        authority_lane="candidate",
        verification_state="runtime_unverified",
        review_state="needs_review",
        content_hash=hash_payload({"object_type": "McpTool", "tool": "brain_objects_query"}),
        evidence_refs=[evidence.evidence_id],
        confidence={"score": 0.7, "basis": "object_native_read_path_contract"},
        recommended_action="verify_runtime",
        payload={"deployed_runtime_claim": False, "mutation_performed": False},
    ).to_dict()


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
                "supported_edit_actions": list(CANDIDATE_REVIEW_EDIT_ACTIONS),
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
    _refresh_empty_lane_gaps(pack)
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
    target_scope: str = "local_test",
    mutation_mode: str = "no_mutation",
) -> dict[str, Any]:
    updated_pack = deepcopy(dict(pack))
    scope = public_safe_text(str(target_scope or "local_test"), max_chars=80)
    mode = public_safe_text(str(mutation_mode or "no_mutation"), max_chars=80)
    original_hash = str(pack.get("candidate_graph_hash") or "")
    if mode != "no_mutation":
        updated_pack["authority_write_performed"] = False
        updated_pack["authoritative_memory_changed"] = False
        updated_pack["production_mutation_performed"] = False
        updated_pack["review_edit_target_scope"] = scope
        updated_pack["mutation_mode"] = mode
        result = {
            "schema_version": "candidate_review_edit_result.v1",
            "permission": "denied",
            "reason": "candidate_review_edit_mutation_mode_not_supported",
            "target_scope": scope,
            "mutation_mode": mode,
            "candidate_state_changed": False,
            "authority_write_performed": False,
            "authoritative_memory_changed": False,
            "production_mutation_performed": False,
            "original_extraction_preserved": True,
            "original_candidate_graph_hash": original_hash,
            "updated_candidate_graph_hash": original_hash,
            "reviewer_ref": public_safe_text(
                str(reviewer.get("id") or reviewer.get("role") or "unspecified"),
                max_chars=120,
            ),
            "accepted_edits": [],
            "rejected_edits": [
                {
                    "action": public_safe_text(str(edit.get("action") or ""), max_chars=80),
                    "reason": "candidate_review_edit_mutation_mode_not_supported",
                }
                for edit in edits
                if isinstance(edit, Mapping)
            ],
            "updated_pack": updated_pack,
        }
        ensure_public_safe(result, "CandidateReviewEditResult")
        return result
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
                object_by_id=object_by_id,
                objects=objects,
                accepted_edits=accepted_edits,
                rejected_edits=rejected_edits,
            )
            continue
        if action == "update_evidence":
            _apply_evidence_edit(
                edit=edit,
                evidence_by_id=evidence_by_id,
                objects=objects,
                edges=edges,
                accepted_edits=accepted_edits,
                rejected_edits=rejected_edits,
            )
            continue
        if action == "add_edge":
            _apply_add_edge_edit(
                edit=edit,
                object_by_id=object_by_id,
                edge_by_id=edge_by_id,
                evidence_by_id=evidence_by_id,
                objects=objects,
                edges=edges,
                accepted_edits=accepted_edits,
                rejected_edits=rejected_edits,
            )
            continue
        if action == "remove_edge":
            _apply_remove_edge_edit(
                edit=edit,
                edge_by_id=edge_by_id,
                edges=edges,
                objects=objects,
                accepted_edits=accepted_edits,
                rejected_edits=rejected_edits,
            )
            continue
        if action == "add_evidence":
            _apply_add_evidence_edit(
                edit=edit,
                object_by_id=object_by_id,
                edge_by_id=edge_by_id,
                evidence_by_id=evidence_by_id,
                evidence_items=evidence_items,
                accepted_edits=accepted_edits,
                rejected_edits=rejected_edits,
            )
            continue
        if action == "remove_evidence":
            _apply_remove_evidence_edit(
                edit=edit,
                evidence_by_id=evidence_by_id,
                evidence_items=evidence_items,
                objects=objects,
                edges=edges,
                edge_by_id=edge_by_id,
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
    _refresh_empty_lane_gaps(updated_pack)
    updated_hash = _candidate_graph_hash(
        [dict(obj) for obj in updated_pack.get("objects") or [] if isinstance(obj, Mapping)],
        [dict(edge) for edge in updated_pack.get("edges") or [] if isinstance(edge, Mapping)],
        [dict(item) for item in updated_pack.get("evidence") or [] if isinstance(item, Mapping)],
    )
    updated_pack["candidate_graph_hash"] = updated_hash
    updated_pack["authority_write_performed"] = False
    updated_pack["authoritative_memory_changed"] = False
    updated_pack["production_mutation_performed"] = False
    updated_pack["review_edit_target_scope"] = scope
    updated_pack["mutation_mode"] = "no_mutation"
    updated_pack["audit"] = {
        **dict(updated_pack.get("audit") or {}),
        "candidate_graph_hash": updated_hash,
        "review_edit_hash": hash_payload([original_hash, updated_hash, accepted_edits, rejected_edits]),
    }
    result = {
        "schema_version": "candidate_review_edit_result.v1",
        "permission": "allowed" if mode == "no_mutation" else "denied",
        "reason": "candidate_review_edit_no_mutation_preview",
        "target_scope": scope,
        "mutation_mode": "no_mutation",
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


def apply_approval_board_decisions(
    pack: Mapping[str, Any],
    *,
    decisions: list[Mapping[str, Any]],
    reviewer: Mapping[str, Any],
    ledger_scope: str = "local_test",
) -> dict[str, Any]:
    scope = public_safe_text(str(ledger_scope or ""), max_chars=80)
    if scope != "local_test":
        result = {
            "schema_version": "approval_board_decision_result.v1",
            "permission": "denied",
            "reason": "production_approval_gate_required",
            "ledger_scope": scope or "production",
            "production_mutation_performed": False,
            "authority_write_performed": False,
            "authority_write_scope": "",
            "authoritative_memory_changed": False,
            "decision_count": 0,
            "decisions": [],
            "rejected_decisions": [
                _denied_production_decision(decision)
                for decision in decisions
                if isinstance(decision, Mapping)
            ],
            "updated_pack": deepcopy(dict(pack)),
            "promotion_plan": _production_promotion_plan(),
        }
        ensure_public_safe(result, "ApprovalBoardDecisionResult")
        return result

    updated_pack = deepcopy(dict(pack))
    original_hash = str(pack.get("candidate_graph_hash") or "")
    objects = updated_pack.get("objects") if isinstance(updated_pack.get("objects"), list) else []
    object_by_id = {
        str(obj.get("object_id") or ""): obj
        for obj in objects
        if isinstance(obj, dict) and obj.get("object_id")
    }
    accepted_decisions: list[dict[str, Any]] = []
    rejected_decisions: list[dict[str, Any]] = []
    for decision in decisions:
        if not isinstance(decision, Mapping):
            continue
        object_id = public_safe_text(str(decision.get("object_id") or ""), max_chars=180)
        action = public_safe_text(str(decision.get("action") or ""), max_chars=80)
        target = object_by_id.get(object_id)
        if target is None:
            rejected_decisions.append(
                {
                    "action": action,
                    "object_id": object_id,
                    "reason": "candidate_object_not_found",
                }
            )
            continue
        decision_type, lane, lifecycle, review_state, recommended_action, writes_authority = _decision_state(action)
        if not decision_type:
            rejected_decisions.append(
                {
                    "action": action,
                    "object_id": object_id,
                    "reason": "unsupported_approval_board_decision",
                }
            )
            continue
        previous_lane = str(target.get("authority_lane") or "candidate")
        target["authority_lane"] = lane
        target["lifecycle_status"] = lifecycle
        target["review_state"] = review_state
        target["recommended_action"] = recommended_action
        target["authority_decision_ref"] = f"decision:{hash_payload([decision_type, object_id, previous_lane, lane])[-16:]}"
        authority_decision = AuthorityDecision.from_parts(
            decision_type=decision_type,
            target_object_id=object_id,
            previous_authority_lane=previous_lane,
            new_authority_lane=lane,
            approved_by=str(decision.get("approved_by") or reviewer.get("id") or reviewer.get("role") or "unspecified"),
            evidence_refs=[str(ref) for ref in target.get("evidence_refs") or [] if ref],
        ).to_dict(authority_write_performed=writes_authority, cache_invalidated=writes_authority)
        authority_decision["ledger_scope"] = "local_test"
        authority_decision["decision_reason"] = public_safe_text(str(decision.get("reason") or ""), max_chars=512)
        accepted_decisions.append(authority_decision)
    _rebuild_lanes(updated_pack)
    _rebuild_verification(updated_pack)
    _rebuild_candidate_review_surface(updated_pack)
    _refresh_empty_lane_gaps(updated_pack)
    updated_hash = _candidate_graph_hash(
        [dict(obj) for obj in updated_pack.get("objects") or [] if isinstance(obj, Mapping)],
        [dict(edge) for edge in updated_pack.get("edges") or [] if isinstance(edge, Mapping)],
        [dict(item) for item in updated_pack.get("evidence") or [] if isinstance(item, Mapping)],
    )
    changed_authority = any(item.get("authority_write_performed") for item in accepted_decisions)
    updated_pack["candidate_graph_hash"] = updated_hash
    updated_pack["authority_write_performed"] = changed_authority
    updated_pack["authority_write_scope"] = "local_test" if changed_authority else ""
    updated_pack["authoritative_memory_changed"] = changed_authority
    updated_pack["production_mutation_performed"] = False
    updated_pack["audit"] = {
        **dict(updated_pack.get("audit") or {}),
        "candidate_graph_hash": updated_hash,
        "approval_board_decision_hash": hash_payload([original_hash, updated_hash, accepted_decisions, rejected_decisions]),
    }
    result = {
        "schema_version": "approval_board_decision_result.v1",
        "permission": "allowed",
        "reason": "local_test_approval_board_decision",
        "ledger_scope": "local_test",
        "production_mutation_performed": False,
        "authority_write_performed": changed_authority,
        "authority_write_scope": "local_test" if changed_authority else "",
        "authoritative_memory_changed": changed_authority,
        "original_candidate_graph_hash": original_hash,
        "updated_candidate_graph_hash": updated_hash,
        "reviewer_ref": public_safe_text(str(reviewer.get("id") or reviewer.get("role") or "unspecified"), max_chars=120),
        "decision_count": len(accepted_decisions),
        "decisions": accepted_decisions,
        "rejected_decisions": rejected_decisions,
        "updated_pack": updated_pack,
    }
    ensure_public_safe(result, "ApprovalBoardDecisionResult")
    return result


def _denied_production_decision(decision: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "action": public_safe_text(str(decision.get("action") or ""), max_chars=80),
        "object_id": public_safe_text(str(decision.get("object_id") or ""), max_chars=180),
        "reason": "production_approval_gate_required",
    }


def _production_promotion_plan() -> dict[str, Any]:
    return {
        "schema_version": "object_authority_promotion_plan.v1",
        "required_gate_evidence": [
            "human_approval",
            "audit_trail",
            "rollback_or_supersession_path",
            "scoped_object_classes",
        ],
        "production_mutation_performed": False,
        "authority_write_performed": False,
        "authoritative_memory_changed": False,
    }


def _decision_state(action: str) -> tuple[str, str, str, str, str, bool]:
    if action == "promote":
        return "accept_current", "accepted_current", "current", "accepted", "keep", True
    if action == "reject":
        return "reject_candidate", "rejected", "rejected", "rejected", "rejected", True
    if action == "stale":
        return "commit_stale", "accepted_non_current", "stale", "accepted", "review", True
    if action == "supersede":
        return "commit_supersession", "accepted_non_current", "superseded", "accepted", "review", True
    if action == "retire":
        return "retire", "accepted_non_current", "retired", "accepted", "archive", True
    if action in {"hold", "request_more_evidence", "merge", "split"}:
        return action, "candidate", "proposed", "needs_review", "request_more_evidence", False
    return "", "", "", "", "", False


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
    if not _candidate_lane_editable(target):
        rejected_edits.append(
            {
                "action": action,
                "object_id": object_id,
                "reason": "candidate_review_edit_requires_candidate_lane",
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
    object_by_id: dict[str, dict[str, Any]],
    objects: list[Any],
    accepted_edits: list[dict[str, Any]],
    rejected_edits: list[dict[str, Any]],
) -> None:
    action = "update_edge"
    edge_id = public_safe_text(str(edit.get("edge_id") or ""), max_chars=180)
    target = edge_by_id.get(edge_id)
    if target is None:
        rejected_edits.append({"action": action, "edge_id": edge_id, "reason": "candidate_edge_not_found"})
        return
    if not _candidate_lane_editable(target):
        rejected_edits.append(
            {
                "action": action,
                "edge_id": edge_id,
                "reason": "candidate_review_edit_requires_candidate_lane",
            }
        )
        return
    fields = edit.get("fields") if isinstance(edit.get("fields"), Mapping) else {}
    for endpoint_field in ("from_object_id", "to_object_id"):
        if endpoint_field not in fields:
            continue
        endpoint_id = _safe_edit_value(endpoint_field, fields.get(endpoint_field))
        endpoint = object_by_id.get(str(endpoint_id or ""))
        if endpoint is None:
            rejected_edits.append(
                {
                    "action": action,
                    "edge_id": edge_id,
                    endpoint_field: endpoint_id,
                    "reason": "candidate_edge_endpoint_not_found",
                }
            )
            return
        if not _candidate_lane_editable(endpoint):
            rejected_edits.append(
                {
                    "action": action,
                    "edge_id": edge_id,
                    endpoint_field: endpoint_id,
                    "reason": "candidate_review_edit_requires_candidate_lane",
                }
            )
            return
    old_from_object_id = str(target.get("from_object_id") or "")
    old_to_object_id = str(target.get("to_object_id") or "")
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
        old_edge_id = edge_id
        _refresh_edge_identity(target)
        new_edge_id = str(target.get("edge_id") or "")
        if new_edge_id != old_edge_id:
            edge_by_id.pop(old_edge_id, None)
            edge_by_id[new_edge_id] = target
        _sync_object_edge_endpoint_refs(
            (obj for obj in objects if isinstance(obj, dict)),
            old_edge_id=old_edge_id,
            new_edge_id=new_edge_id,
            old_endpoint_ids={old_from_object_id, old_to_object_id},
            new_endpoint_ids={
                str(target.get("from_object_id") or ""),
                str(target.get("to_object_id") or ""),
            },
        )
        accepted_edits.append(
            {
                "action": action,
                "edge_id": edge_id,
                "updated_edge_id": new_edge_id,
                "changed_fields": sorted(changed_fields),
            }
        )


def _apply_evidence_edit(
    *,
    edit: Mapping[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
    objects: list[Any],
    edges: list[Any],
    accepted_edits: list[dict[str, Any]],
    rejected_edits: list[dict[str, Any]],
) -> None:
    action = "update_evidence"
    evidence_id = public_safe_text(str(edit.get("evidence_id") or ""), max_chars=180)
    target = evidence_by_id.get(evidence_id)
    if target is None:
        rejected_edits.append({"action": action, "evidence_id": evidence_id, "reason": "candidate_evidence_not_found"})
        return
    if not _evidence_lane_editable(target):
        rejected_edits.append(
            {
                "action": action,
                "evidence_id": evidence_id,
                "reason": "candidate_review_edit_requires_reference_or_candidate_evidence_lane",
            }
        )
        return
    if _evidence_used_by_non_candidate_authority(evidence_id, objects=objects, edges=edges):
        rejected_edits.append(
            {
                "action": action,
                "evidence_id": evidence_id,
                "reason": "candidate_evidence_used_by_non_candidate_authority",
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


def _apply_add_edge_edit(
    *,
    edit: Mapping[str, Any],
    object_by_id: dict[str, dict[str, Any]],
    edge_by_id: dict[str, dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
    objects: list[Any],
    edges: list[Any],
    accepted_edits: list[dict[str, Any]],
    rejected_edits: list[dict[str, Any]],
) -> None:
    action = "add_edge"
    fields = edit.get("fields") if isinstance(edit.get("fields"), Mapping) else {}
    edge_type = public_safe_text(str(fields.get("edge_type") or ""), max_chars=180)
    from_object_id = public_safe_text(str(fields.get("from_object_id") or ""), max_chars=180)
    to_object_id = public_safe_text(str(fields.get("to_object_id") or ""), max_chars=180)
    if not edge_type or not from_object_id or not to_object_id:
        rejected_edits.append({"action": action, "reason": "candidate_edge_required_fields_missing"})
        return
    from_object = object_by_id.get(from_object_id)
    to_object = object_by_id.get(to_object_id)
    if from_object is None or to_object is None:
        rejected_edits.append(
            {
                "action": action,
                "from_object_id": from_object_id,
                "to_object_id": to_object_id,
                "reason": "candidate_edge_endpoint_not_found",
            }
        )
        return
    if not _candidate_lane_editable(from_object) or not _candidate_lane_editable(to_object):
        rejected_edits.append(
            {
                "action": action,
                "from_object_id": from_object_id,
                "to_object_id": to_object_id,
                "reason": "candidate_review_edit_requires_candidate_lane",
            }
        )
        return
    evidence_refs = _safe_edit_value("evidence_refs", fields.get("evidence_refs") or [])
    missing_evidence = [ref for ref in evidence_refs if ref not in evidence_by_id]
    if missing_evidence:
        rejected_edits.append(
            {
                "action": action,
                "evidence_refs": missing_evidence,
                "reason": "candidate_evidence_not_found",
            }
        )
        return
    non_candidate_evidence_refs = [
        ref
        for ref in evidence_refs
        if _evidence_used_by_non_candidate_authority(ref, objects=objects, edges=edge_by_id.values())
    ]
    if non_candidate_evidence_refs:
        rejected_edits.append(
            {
                "action": action,
                "evidence_refs": non_candidate_evidence_refs,
                "reason": "candidate_evidence_used_by_non_candidate_authority",
            }
        )
        return
    try:
        edge = KnowledgeEdge.from_parts(
            edge_type=edge_type,
            from_object_id=from_object_id,
            to_object_id=to_object_id,
            evidence_refs=evidence_refs,
            lifecycle_status="proposed",
            authority_lane="candidate",
            verification_state="unverified",
            confidence=dict(fields.get("confidence") or {}) if isinstance(fields.get("confidence"), Mapping) else {},
            payload=dict(fields.get("payload") or {}) if isinstance(fields.get("payload"), Mapping) else {},
        ).to_dict()
    except ValueError as exc:
        rejected_edits.append({"action": action, "reason": public_safe_text(str(exc), max_chars=240)})
        return
    edge_id = str(edge.get("edge_id") or "")
    if edge_id in edge_by_id:
        rejected_edits.append({"action": action, "edge_id": edge_id, "reason": "candidate_edge_already_exists"})
        return
    edges.append(edge)
    edge_by_id[edge_id] = edge
    _append_unique_ref(from_object, "edge_refs", edge_id)
    _append_unique_ref(to_object, "edge_refs", edge_id)
    accepted_edits.append({"action": action, "edge_id": edge_id})


def _apply_remove_edge_edit(
    *,
    edit: Mapping[str, Any],
    edge_by_id: dict[str, dict[str, Any]],
    edges: list[Any],
    objects: list[Any],
    accepted_edits: list[dict[str, Any]],
    rejected_edits: list[dict[str, Any]],
) -> None:
    action = "remove_edge"
    edge_id = public_safe_text(str(edit.get("edge_id") or ""), max_chars=180)
    if edge_id not in edge_by_id:
        rejected_edits.append({"action": action, "edge_id": edge_id, "reason": "candidate_edge_not_found"})
        return
    if not _candidate_lane_editable(edge_by_id[edge_id]):
        rejected_edits.append(
            {
                "action": action,
                "edge_id": edge_id,
                "reason": "candidate_review_edit_requires_candidate_lane",
            }
        )
        return
    edges[:] = [
        edge
        for edge in edges
        if not (isinstance(edge, Mapping) and str(edge.get("edge_id") or "") == edge_id)
    ]
    edge_by_id.pop(edge_id, None)
    for obj in objects:
        if isinstance(obj, dict):
            obj["edge_refs"] = [ref for ref in obj.get("edge_refs") or [] if str(ref) != edge_id]
    accepted_edits.append({"action": action, "edge_id": edge_id})


def _apply_add_evidence_edit(
    *,
    edit: Mapping[str, Any],
    object_by_id: dict[str, dict[str, Any]],
    edge_by_id: dict[str, dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
    evidence_items: list[Any],
    accepted_edits: list[dict[str, Any]],
    rejected_edits: list[dict[str, Any]],
) -> None:
    action = "add_evidence"
    fields = edit.get("fields") if isinstance(edit.get("fields"), Mapping) else {}
    locator = fields.get("locator") if isinstance(fields.get("locator"), Mapping) else {}
    evidence_type = public_safe_text(str(fields.get("evidence_type") or ""), max_chars=120)
    content_hash = public_safe_text(str(fields.get("content_hash") or ""), max_chars=100)
    summary = public_safe_text(str(fields.get("summary") or ""), max_chars=512)
    if not evidence_type or not locator or not content_hash or not summary:
        rejected_edits.append({"action": action, "reason": "candidate_evidence_required_fields_missing"})
        return
    attach_to_object_id = public_safe_text(str(edit.get("attach_to_object_id") or ""), max_chars=180)
    attach_to_edge_id = public_safe_text(str(edit.get("attach_to_edge_id") or ""), max_chars=180)
    target_object = object_by_id.get(attach_to_object_id) if attach_to_object_id else None
    target_edge = edge_by_id.get(attach_to_edge_id) if attach_to_edge_id else None
    if attach_to_object_id and target_object is None:
        rejected_edits.append(
            {"action": action, "object_id": attach_to_object_id, "reason": "candidate_object_not_found"}
        )
        return
    if attach_to_edge_id and target_edge is None:
        rejected_edits.append({"action": action, "edge_id": attach_to_edge_id, "reason": "candidate_edge_not_found"})
        return
    if target_object is not None and not _candidate_lane_editable(target_object):
        rejected_edits.append(
            {
                "action": action,
                "object_id": attach_to_object_id,
                "reason": "candidate_review_edit_requires_candidate_lane",
            }
        )
        return
    if target_edge is not None and not _candidate_lane_editable(target_edge):
        rejected_edits.append(
            {
                "action": action,
                "edge_id": attach_to_edge_id,
                "reason": "candidate_review_edit_requires_candidate_lane",
            }
        )
        return
    try:
        evidence = EvidenceRef.from_parts(
            evidence_type=evidence_type,
            authority_lane="reference_only",
            verification_state=public_safe_text(
                str(fields.get("verification_state") or "source_hash_verified"),
                max_chars=80,
            ),
            locator=dict(locator),
            content_hash=content_hash,
            summary=summary,
            producer=dict(fields.get("producer") or {}) if isinstance(fields.get("producer"), Mapping) else {},
            privacy_class=public_safe_text(str(fields.get("privacy_class") or "public_safe"), max_chars=80),
            gaps=[public_safe_text(str(item), max_chars=180) for item in fields.get("gaps") or [] if item],
        )
        evidence_view = evidence.to_view()
    except ValueError as exc:
        rejected_edits.append({"action": action, "reason": public_safe_text(str(exc), max_chars=240)})
        return
    evidence_id = evidence.evidence_id
    if evidence_id in evidence_by_id:
        rejected_edits.append(
            {"action": action, "evidence_id": evidence_id, "reason": "candidate_evidence_already_exists"}
        )
        return
    evidence_items.append(evidence_view)
    evidence_by_id[evidence_id] = evidence_view
    attached_to: list[dict[str, str]] = []
    if target_object is not None:
        _append_unique_ref(target_object, "evidence_refs", evidence_id)
        attached_to.append({"object_id": attach_to_object_id})
    if target_edge is not None:
        old_edge_id = str(target_edge.get("edge_id") or "")
        _append_unique_ref(target_edge, "evidence_refs", evidence_id)
        _refresh_edge_identity(target_edge)
        _replace_object_edge_ref(object_by_id.values(), old_edge_id, str(target_edge.get("edge_id") or ""))
        _rebuild_edge_map(edge_by_id, list(edge_by_id.values()))
        attached_to.append({"edge_id": str(target_edge.get("edge_id") or "")})
    accepted_edits.append({"action": action, "evidence_id": evidence_id, "attached_to": attached_to})


def _apply_remove_evidence_edit(
    *,
    edit: Mapping[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
    evidence_items: list[Any],
    objects: list[Any],
    edges: list[Any],
    edge_by_id: dict[str, dict[str, Any]],
    accepted_edits: list[dict[str, Any]],
    rejected_edits: list[dict[str, Any]],
) -> None:
    action = "remove_evidence"
    evidence_id = public_safe_text(str(edit.get("evidence_id") or ""), max_chars=180)
    if evidence_id not in evidence_by_id:
        rejected_edits.append({"action": action, "evidence_id": evidence_id, "reason": "candidate_evidence_not_found"})
        return
    if not _evidence_lane_editable(evidence_by_id[evidence_id]):
        rejected_edits.append(
            {
                "action": action,
                "evidence_id": evidence_id,
                "reason": "candidate_review_edit_requires_reference_or_candidate_evidence_lane",
            }
        )
        return
    if _evidence_used_by_non_candidate_authority(evidence_id, objects=objects, edges=edges):
        rejected_edits.append(
            {
                "action": action,
                "evidence_id": evidence_id,
                "reason": "candidate_evidence_used_by_non_candidate_authority",
            }
        )
        return
    evidence_items[:] = [
        item
        for item in evidence_items
        if not (isinstance(item, Mapping) and str(item.get("evidence_id") or "") == evidence_id)
    ]
    evidence_by_id.pop(evidence_id, None)
    for obj in objects:
        if isinstance(obj, dict):
            obj["evidence_refs"] = [ref for ref in obj.get("evidence_refs") or [] if str(ref) != evidence_id]
    changed_edge_ids: list[dict[str, str]] = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        evidence_refs = [ref for ref in edge.get("evidence_refs") or [] if str(ref) != evidence_id]
        if evidence_refs == edge.get("evidence_refs"):
            continue
        old_edge_id = str(edge.get("edge_id") or "")
        edge["evidence_refs"] = evidence_refs
        _refresh_edge_identity(edge)
        new_edge_id = str(edge.get("edge_id") or "")
        _replace_object_edge_ref((obj for obj in objects if isinstance(obj, dict)), old_edge_id, new_edge_id)
        changed_edge_ids.append({"previous_edge_id": old_edge_id, "edge_id": new_edge_id})
    _rebuild_edge_map(edge_by_id, edges)
    accepted_edits.append({"action": action, "evidence_id": evidence_id, "updated_edges": changed_edge_ids})


def _append_unique_ref(target: dict[str, Any], field_name: str, ref: str) -> None:
    refs = [str(item) for item in target.get(field_name) or [] if item]
    if ref not in refs:
        refs.append(ref)
    target[field_name] = refs


def _candidate_lane_editable(target: Mapping[str, Any]) -> bool:
    return str(target.get("authority_lane") or "") in EDITABLE_CANDIDATE_LANES


def _evidence_lane_editable(target: Mapping[str, Any]) -> bool:
    return str(target.get("authority_lane") or "") in EDITABLE_EVIDENCE_LANES


def _evidence_used_by_non_candidate_authority(
    evidence_id: str,
    *,
    objects: Any,
    edges: Any,
) -> bool:
    for obj in objects:
        if not isinstance(obj, Mapping) or evidence_id not in {str(ref) for ref in obj.get("evidence_refs") or []}:
            continue
        if not _candidate_lane_editable(obj):
            return True
    for edge in edges:
        if not isinstance(edge, Mapping) or evidence_id not in {str(ref) for ref in edge.get("evidence_refs") or []}:
            continue
        if not _candidate_lane_editable(edge):
            return True
    return False


def _replace_object_edge_ref(objects: Any, old_edge_id: str, new_edge_id: str) -> None:
    if not old_edge_id or not new_edge_id or old_edge_id == new_edge_id:
        return
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        refs = [str(item) for item in obj.get("edge_refs") or [] if item]
        obj["edge_refs"] = [new_edge_id if ref == old_edge_id else ref for ref in refs]


def _sync_object_edge_endpoint_refs(
    objects: Any,
    *,
    old_edge_id: str,
    new_edge_id: str,
    old_endpoint_ids: set[str],
    new_endpoint_ids: set[str],
) -> None:
    if not old_edge_id or not new_edge_id:
        return
    old_ids = {str(item) for item in old_endpoint_ids if str(item or "")}
    new_ids = {str(item) for item in new_endpoint_ids if str(item or "")}
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        object_id = str(obj.get("object_id") or "")
        refs = [str(item) for item in obj.get("edge_refs") or [] if item]
        refs = [new_edge_id if ref == old_edge_id else ref for ref in refs]
        if object_id not in new_ids:
            refs = [ref for ref in refs if ref != new_edge_id]
        else:
            refs = [ref for ref in refs if ref != old_edge_id]
            if new_edge_id not in refs:
                refs.append(new_edge_id)
        if object_id in old_ids - new_ids:
            refs = [ref for ref in refs if ref not in {old_edge_id, new_edge_id}]
        obj["edge_refs"] = list(dict.fromkeys(refs))


def _rebuild_edge_map(edge_by_id: dict[str, dict[str, Any]], edges: list[Any]) -> None:
    edge_by_id.clear()
    for edge in edges:
        if isinstance(edge, dict) and edge.get("edge_id"):
            edge_by_id[str(edge.get("edge_id") or "")] = edge


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
            recommended_action="review",
            payload={},
        ).to_dict()
        pack["objects"].append(obj)
        pack["lanes"]["reference_only"].append(obj)
        pack["recommended_actions"].append({"object_id": obj["object_id"], "action": "review"})
    return pack


def _preference_pack(preferences: list[Mapping[str, Any]]) -> dict[str, Any]:
    pack = _empty_pack("code_style_preference")
    for item in preferences:
        if not isinstance(item, Mapping):
            continue
        currentness = str(item.get("currentness") or "unknown")
        if currentness in NON_CURRENT_AUTHORITY:
            continue
        title = str(item.get("rule") or item.get("title") or "")
        if not title:
            continue
        lane = "accepted_current" if currentness == "current" else "proposal_only"
        action = "apply_preference" if lane == "accepted_current" else "review_inferred_preference"
        obj = KnowledgeObjectEnvelope.from_parts(
            object_type="ArtifactPreference",
            natural_key=title,
            scope={"project": "neurons"},
            title=title,
            summary=str(item.get("reason") or title),
            lifecycle_status="current" if lane == "accepted_current" else "proposed",
            authority_lane=lane,
            verification_state="source_hash_verified" if lane == "accepted_current" else "unverified",
            review_state="accepted" if lane == "accepted_current" else "needs_review",
            content_hash=hash_payload(["code_style_preference", title, currentness]),
            recommended_action=action,
            payload={
                "scope": public_safe_text(str(item.get("scope") or ""), max_chars=180),
                "currentness": public_safe_text(currentness, max_chars=80),
            },
        ).to_dict()
        pack["objects"].append(obj)
        pack["lanes"][lane].append(obj)
        pack["recommended_actions"].append({"object_id": obj["object_id"], "action": action})
    if not pack["lanes"]["accepted_current"]:
        pack["gaps"].append("accepted_current preferences empty")
        pack["gaps"].append("review_preference_proposals_needed")
    pack["confidence"] = {
        "score": 0.8 if pack["lanes"]["accepted_current"] else 0.4 if pack["objects"] else 0.0,
        "basis": "preference_authority_pack",
    }
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
    style_titles = [str(item.get("claim") or "") for item in style_profile.get("claims") or []]
    packs = {
        "documentation_cleanup": documentation_pack,
        "reference_corpus": _reference_document_pack_from_documentation(documentation_pack),
        "preferences": _preference_pack(preferences),
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


def _refresh_empty_lane_gaps(pack: dict[str, Any]) -> None:
    lanes = pack.get("lanes") if isinstance(pack.get("lanes"), Mapping) else {}
    lane_names = set(_empty_pack("candidate_graph_review")["lanes"])
    existing_gaps = [
        str(item)
        for item in pack.get("gaps") or []
        if not _is_empty_lane_gap(str(item), lane_names=lane_names)
    ]
    for lane, value in lanes.items():
        lane_name = str(lane or "")
        if lane_name in lane_names and isinstance(value, list) and not value:
            existing_gaps.append(f"{lane_name} objects empty")
    pack["gaps"] = existing_gaps


def _is_empty_lane_gap(gap: str, *, lane_names: set[str]) -> bool:
    suffix = " objects empty"
    if not gap.endswith(suffix):
        return False
    return gap[: -len(suffix)] in lane_names


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
