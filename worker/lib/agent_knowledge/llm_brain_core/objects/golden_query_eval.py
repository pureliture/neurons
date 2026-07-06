from __future__ import annotations

from typing import Any, Mapping

from .._util import ensure_public_safe, hash_payload
from .knowledge_objects import EvidenceRef, KnowledgeEdge, KnowledgeObjectEnvelope
from .object_packs import (
    apply_approval_board_decisions,
    apply_candidate_review_edits,
    build_candidate_graph_review_pack,
)

GOLDEN_QUERIES = [
    "어제 이 repo에서 뭐 했어?",
    "이 repo 문서 최신화하려면 뭘 봐야 해?",
    "오래된 문서/개념 후보 알려줘.",
    "이 파일 바꾸면 어떤 테스트/런타임 영향 있어?",
    "이 PR merge됐어? 배포도 됐어?",
    "지금 current SoT와 stale archive를 분리해서 말해줘.",
    "이 Palantir reference 문서는 공식/current/source URL 확인 가능 자료야?",
    "이 corpus에서 LBrain object model 설계에 필요한 개념만 뽑아줘.",
    "내 Java code style과 다른 diff를 찾아줘.",
    "내가 선호하는 HTML review artifact 기준으로 이 산출물을 평가해줘.",
]

REQUIRED_QUALITY_AXES = [
    "object",
    "edge",
    "evidence",
    "freshness",
    "gap",
    "recommended_action",
]

ACTIVATION_SCOPE_PHASES = ("P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9")
MINIMUM_REVIEW_LOOP_PHASES = ("P2", "P3", "P4")
PRODUCT_EVIDENCE_PHASES = ("P6", "P7", "P8", "P9")

_LOCAL_VALIDATED_PHASES = {"P2", "P3", "P4", "P6", "P7", "P8", "P9"}


def build_baseline_golden_query_report() -> dict[str, Any]:
    report = {
        "schema_version": "knowledge_object_golden_query_eval.v1",
        "status": "baseline_red",
        "queries": [
            {
                "index": index + 1,
                "query": query,
                "passes": False,
                "failures": [
                    "object_pack_missing",
                    "lane_separation_missing",
                    "evidence_or_gap_missing",
                    "recommended_action_missing",
                ],
            }
            for index, query in enumerate(GOLDEN_QUERIES)
        ],
    }
    ensure_public_safe(report, "GoldenQueryBaseline")
    return report


def build_phase_golden_query_coverage_report() -> dict[str, Any]:
    phases = [
        _phase_coverage(
            phase="P1",
            title="Production MCP Activation",
            golden_query_family="pr merge and deploy truth",
            query=GOLDEN_QUERIES[4],
            result="PASS_WITH_GAPS",
            evaluator="configured/live MCP smoke plus deployment identity check",
            gaps=[
                "current_session_mcp_namespace_stale",
                "current_main_image_identity_unproven",
            ],
        ),
        _phase_coverage(
            phase="P2",
            title="Living Reference Corpus Store",
            golden_query_family="reference corpus freshness/source authority",
            query=GOLDEN_QUERIES[6],
            result="PASS_WITH_GAPS",
            evaluator="reference corpus store local/test status and ingest policy checks",
            gaps=[
                "private_palantir_manifest_ingest_not_performed",
                "production_ingest_pilot_not_executed",
            ],
        ),
        _phase_coverage(
            phase="P3",
            title="Processing And Object Extraction Pipeline",
            golden_query_family="corpus-to-design concept extraction",
            query=GOLDEN_QUERIES[7],
            result="PASS_WITH_GAPS",
            evaluator="extraction evaluator suite preview",
            gaps=[
                "live_graph_qdrant_projection_join_unproven",
            ],
        ),
        _phase_coverage(
            phase="P4",
            title="Review Queue And Authority Promotion",
            golden_query_family="review queue and authority promotion",
            query=GOLDEN_QUERIES[2],
            result="PASS_WITH_GAPS",
            evaluator="local/test review queue, authority decision, object query, and object explain checks",
            gaps=[
                "production_authority_pilot_not_executed",
                "production_authority_write_evidence_missing",
            ],
        ),
        _phase_coverage(
            phase="P5",
            title="Continuous Golden Query Quality Gates",
            golden_query_family="continuous phase coverage",
            query="P1-P10 phase coverage is explicit and gaps are visible.",
            result="in_progress",
            evaluator="phase golden query coverage report",
            gaps=[
                "release_quality_gate_not_green",
                "future_phase_slices_planned",
            ],
        ),
        _phase_coverage(
            phase="P6",
            title="Session, Device, Project, And Work-Unit 360",
            golden_query_family="temporal repo recall",
            query=GOLDEN_QUERIES[0],
            result="PASS_WITH_GAPS",
            evaluator="session project rollup local/test preview and handoff pack",
            gaps=[
                "live_multi_device_rollup_unproven",
            ],
        ),
        _phase_coverage(
            phase="P7",
            title="Preference, Style, And Artifact Memory",
            golden_query_family="code style drift",
            query=GOLDEN_QUERIES[8],
            result="PASS_WITH_GAPS",
            evaluator="artifact preference pack local/test preview",
            gaps=[
                "accepted_preference_context_pack_live_unproven",
                "html_artifact_review_live_unproven",
            ],
        ),
        _phase_coverage(
            phase="P8",
            title="Runtime Truth, Security, And Deployment Authority",
            golden_query_family="pr merge and deploy truth",
            query=GOLDEN_QUERIES[4],
            result="PASS_WITH_GAPS",
            evaluator="runtime authority policy local/test preview",
            gaps=[
                "live_runtime_rollout_identity_unproven",
                "production_permission_audit_live_unproven",
            ],
        ),
        _phase_coverage(
            phase="P9",
            title="Agent Context Productization",
            golden_query_family="agent context productization",
            query=GOLDEN_QUERIES[3],
            result="PASS_WITH_GAPS",
            evaluator="consumer-specific compact context pack local/test preview",
            gaps=[
                "production_consumer_context_pack_live_unproven",
                "consumer_action_surface_runtime_policy_unproven",
            ],
        ),
        _planned_phase("P10", "Product Application Surface", "HTML/visualization review preference", GOLDEN_QUERIES[9]),
    ]
    report = {
        "schema_version": "knowledge_object_phase_golden_query_coverage.v1",
        "status": "PASS_WITH_GAPS",
        "release_quality_gate": "not_green",
        "required_axes": list(REQUIRED_QUALITY_AXES),
        "phases": phases,
        "gaps": [
            "production_quality_not_green",
            "future_phase_golden_query_slices_planned",
        ],
    }
    ensure_public_safe(report, "PhaseGoldenQueryCoverage")
    return report


def build_source_to_authority_quality_gate_report() -> dict[str, Any]:
    review_pack = _source_to_authority_fixture_pack()
    candidate_object = review_pack["objects"][0]
    original_edge = review_pack["edges"][0]
    original_evidence = review_pack["evidence"][0]
    replacement_evidence = EvidenceRef.from_parts(
        evidence_type="source_freshness",
        authority_lane="reference_only",
        verification_state="freshness_checked",
        locator={"kind": "relative_repo_path", "value": "docs/specs/lbrain-source-fixture-reviewed.md"},
        content_hash=hash_payload({"fixture": "source-to-authority-quality-gate-reviewed"}),
        summary="Reviewer-attached replacement freshness evidence.",
    )
    product_surface_checks = _object_native_product_surface_checks()
    review_eval = evaluate_object_pack_response(
        "새 자료를 candidate object, edge, evidence로 쪼개서 review surface에 올려줘.",
        review_pack,
        required_axes=REQUIRED_QUALITY_AXES,
    )
    edit_result = apply_candidate_review_edits(
        review_pack,
        edits=[
            {
                "action": "update_object",
                "object_id": candidate_object["object_id"],
                "fields": {
                    "summary": "Reviewer clarified the candidate claim before promotion.",
                    "recommended_action": "promote",
                    "freshness": {"source_checked": True, "state": "freshness_checked"},
                },
            },
            {
                "action": "add_evidence",
                "attach_to_object_id": candidate_object["object_id"],
                "fields": {
                    "evidence_type": "source_freshness",
                    "locator": {"kind": "relative_repo_path", "value": "docs/specs/lbrain-source-fixture-reviewed.md"},
                    "content_hash": hash_payload({"fixture": "source-to-authority-quality-gate-reviewed"}),
                    "summary": "Reviewer-attached replacement freshness evidence.",
                    "verification_state": "freshness_checked",
                },
            },
            {
                "action": "add_edge",
                "fields": {
                    "edge_type": "supports",
                    "from_object_id": candidate_object["object_id"],
                    "to_object_id": candidate_object["object_id"],
                    "evidence_refs": [replacement_evidence.evidence_id],
                },
            },
            {"action": "remove_edge", "edge_id": original_edge["edge_id"]},
            {"action": "remove_evidence", "evidence_id": original_evidence["evidence_id"]},
        ],
        reviewer={"id": "quality-gate-reviewer"},
        target_scope="production",
        mutation_mode="no_mutation",
    )
    edited_pack = edit_result["updated_pack"]
    edit_eval = evaluate_object_pack_response(
        "사용자가 candidate object/edge/evidence를 authority mutation 없이 고쳐줘.",
        edited_pack,
        required_axes=REQUIRED_QUALITY_AXES,
    )
    approval_result = apply_approval_board_decisions(
        edited_pack,
        decisions=[
            {
                "action": "promote",
                "object_id": candidate_object["object_id"],
                "reason": "Quality gate local/test approval preview.",
                "approved_by": "quality-gate-reviewer",
            }
        ],
        reviewer={"id": "quality-gate-reviewer"},
        ledger_scope="local_test",
    )
    authority_pack = approval_result["updated_pack"]
    authority_eval = evaluate_object_pack_response(
        "승격된 object를 accepted/current authority read path에서 읽어줘.",
        authority_pack,
        required_axes=REQUIRED_QUALITY_AXES,
    )
    accepted_current_object = _first_lane_item(authority_pack, "accepted_current")
    production_denial = apply_approval_board_decisions(
        edited_pack,
        decisions=[
            {
                "action": "promote",
                "object_id": candidate_object["object_id"],
                "reason": "Production promotion must stay gated.",
                "approved_by": "quality-gate-reviewer",
            }
        ],
        reviewer={"id": "quality-gate-reviewer"},
        ledger_scope="production",
    )
    path_checks = [
        {
            "id": "source_to_candidate_graph",
            "result": _pass_fail(
                review_eval["passes"]
                and bool(review_pack["lanes"]["candidate"])
                and bool(review_pack["edges"])
                and bool(review_pack["evidence"])
                and review_pack["authority_write_performed"] is False
                and review_pack["production_mutation_performed"] is False
            ),
            "quality_eval": review_eval,
            "production_mutation_performed": False,
        },
        {
            "id": "candidate_review_edit",
            "result": _pass_fail(
                edit_result["candidate_state_changed"] is True
                and edit_result["authority_write_performed"] is False
                and edit_result["production_mutation_performed"] is False
                and edit_result["mutation_mode"] == "no_mutation"
                and not edit_result["rejected_edits"]
                and edit_eval["passes"]
            ),
            "quality_eval": edit_eval,
            "target_scope": str(edit_result.get("target_scope") or ""),
            "mutation_mode": str(edit_result.get("mutation_mode") or ""),
            "accepted_edit_actions": [
                str(item.get("action") or "")
                for item in edit_result.get("accepted_edits", [])
                if isinstance(item, Mapping)
            ],
            "updated_edge_count": len(edited_pack.get("edges") or []),
            "updated_evidence_count": len(edited_pack.get("evidence") or []),
            "production_mutation_performed": False,
        },
        {
            "id": "approval_board_local_test",
            "result": _pass_fail(
                approval_result["authority_write_performed"] is True
                and approval_result["authority_write_scope"] == "local_test"
                and approval_result["production_mutation_performed"] is False
                and bool(authority_pack["lanes"]["accepted_current"])
            ),
            "authority_write_scope": approval_result["authority_write_scope"],
            "production_mutation_performed": False,
        },
        {
            "id": "authority_read_after_write",
            "result": _pass_fail(
                authority_eval["passes"]
                and accepted_current_object.get("review_state") == "accepted"
                and accepted_current_object.get("recommended_action") == "keep"
            ),
            "quality_eval": authority_eval,
            "production_mutation_performed": False,
        },
        {
            "id": "production_decision_denial",
            "result": _pass_fail(
                production_denial["permission"] == "denied"
                and production_denial["production_mutation_performed"] is False
                and production_denial["authority_write_performed"] is False
                and production_denial["promotion_plan"]["production_mutation_performed"] is False
            ),
            "permission": production_denial["permission"],
            "reason": production_denial["reason"],
            "production_mutation_performed": False,
        },
    ]
    hard_failures = [
        item["id"]
        for item in [*path_checks, *product_surface_checks]
        if item["result"] != "PASS"
    ]
    report = {
        "schema_version": "source_to_authority_quality_gate_report.v1",
        "status": "FAIL" if hard_failures else "PASS_WITH_GAPS",
        "release_quality_gate": "blocked" if hard_failures else "not_green",
        "required_axes": list(REQUIRED_QUALITY_AXES),
        "path_checks": path_checks,
        "product_surface_checks": product_surface_checks,
        "hard_failures": hard_failures,
        "gaps": [
            "production_authority_gate_preapproved_not_executed",
            "live_runtime_read_path_unverified",
            "production_quality_not_green",
        ],
        "production_mutation_performed": False,
        "production_authoritative_memory_changed": False,
        "production_approval_gate": "preapproved",
        "production_mutation_execution": "not_performed_by_local_gate",
        "local_test_authority_write_performed": approval_result["authority_write_performed"],
        "authority_write_scope": approval_result["authority_write_scope"],
    }
    ensure_public_safe(report, "SourceToAuthorityQualityGateReport")
    return report


def build_product_activation_progress_report() -> dict[str, Any]:
    phase_coverage = build_phase_golden_query_coverage_report()
    source_gate = build_source_to_authority_quality_gate_report()
    product_evidence_summary = _product_evidence_summary()
    product_evidence_result = evaluate_product_evidence_summary(product_evidence_summary)
    phases_by_id = {
        str(item.get("phase") or ""): item
        for item in phase_coverage.get("phases", [])
        if isinstance(item, Mapping)
    }
    phase_progress = [
        _activation_phase_progress(phase, phases_by_id.get(phase, {}))
        for phase in ACTIVATION_SCOPE_PHASES
    ]
    minimum_checkpoint = _minimum_review_loop_checkpoint(phase_progress, source_gate)
    hard_failures = _dedupe(
        [
            *[
                str(item)
                for item in source_gate.get("hard_failures", [])
                if str(item or "")
            ],
            *[
                f"{item['phase']}:quality_failed"
                for item in phase_progress
                if item.get("quality_result") == "FAIL"
            ],
            *[
                f"{phase}:coverage_missing"
                for phase in ACTIVATION_SCOPE_PHASES
                if phase not in phases_by_id
            ],
            *product_evidence_result["hard_failures"],
        ]
    )
    blockers = _activation_goal_blockers(
        phase_coverage=phase_coverage,
        source_gate=source_gate,
        phase_progress=phase_progress,
    )
    status = "FAIL" if hard_failures else ("PASS_WITH_GAPS" if blockers else "PASS")
    next_phase = _next_activation_phase(phase_progress)
    production_ready = (
        status == "PASS"
        and source_gate["release_quality_gate"] == "green"
        and all(item.get("state") in {"production_validated", "complete"} for item in phase_progress)
    )
    report = {
        "schema_version": "lbrain_product_activation_progress.v1",
        "status": status,
        "scope_phases": list(ACTIVATION_SCOPE_PHASES),
        "phase_progress": phase_progress,
        "product_evidence_summary": product_evidence_summary,
        "product_evidence_checks": product_evidence_result["checks"],
        "product_evidence_status": product_evidence_result["status"],
        "minimum_review_loop_checkpoint": minimum_checkpoint,
        "next_phase": next_phase,
        "remaining_phases": _remaining_activation_phases(next_phase),
        "quality_gate_inputs": {
            "phase_coverage_status": phase_coverage["status"],
            "source_to_authority_status": source_gate["status"],
            "source_to_authority_release_quality_gate": source_gate["release_quality_gate"],
        },
        "production_approval_gate": str(source_gate.get("production_approval_gate") or ""),
        "production_mutation_execution": str(source_gate.get("production_mutation_execution") or ""),
        "release_quality_gate": "blocked" if hard_failures else source_gate["release_quality_gate"],
        "goal_completion_blockers": blockers,
        "hard_failures": hard_failures,
        "goal_complete": production_ready and not blockers,
        "production_ready": production_ready,
        "production_mutation_performed": bool(
            source_gate.get("production_mutation_performed")
            or any(item.get("production_mutation_performed") for item in phase_progress)
            or any(item.get("production_mutation_performed") for item in product_evidence_summary)
        ),
        "production_authoritative_memory_changed": bool(
            source_gate.get("production_authoritative_memory_changed")
        ),
    }
    ensure_public_safe(report, "LBrainProductActivationProgress")
    return report


def evaluate_product_evidence_summary(evidence_summary: list[Mapping[str, Any]]) -> dict[str, Any]:
    by_phase = {
        str(item.get("phase") or ""): item
        for item in evidence_summary
        if isinstance(item, Mapping)
    }
    checks = [_product_evidence_check(phase, by_phase.get(phase, {})) for phase in PRODUCT_EVIDENCE_PHASES]
    hard_failures = [
        f"{check['phase']}:product_evidence_failed"
        for check in checks
        if check["result"] == "FAIL" and "product_evidence_missing" not in check["failures"]
    ]
    hard_failures.extend(
        f"{check['phase']}:product_evidence_missing"
        for check in checks
        if "product_evidence_missing" in check["failures"]
    )
    has_gaps = any(check.get("gaps") for check in checks)
    result = {
        "schema_version": "lbrain_product_evidence_summary_eval.v1",
        "status": "FAIL" if hard_failures else ("PASS_WITH_GAPS" if has_gaps else "PASS"),
        "checks": checks,
        "hard_failures": hard_failures,
        "production_mutation_performed": any(
            bool(item.get("production_mutation_performed"))
            for item in evidence_summary
            if isinstance(item, Mapping)
        ),
    }
    ensure_public_safe(result, "LBrainProductEvidenceSummaryEval")
    return result


def evaluate_object_pack_response(
    query: str,
    response: Mapping[str, Any],
    *,
    required_axes: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    failures: list[str] = []
    checked_axes = [str(axis or "") for axis in required_axes or []]
    lanes = response.get("lanes") if isinstance(response.get("lanes"), Mapping) else {}
    lane_items = []
    for value in lanes.values():
        if isinstance(value, list):
            lane_items.extend(value)
    edges = response.get("edges") if isinstance(response.get("edges"), list) else []
    evidence = response.get("evidence") if isinstance(response.get("evidence"), list) else []
    gaps = response.get("gaps") if isinstance(response.get("gaps"), list) else []
    actions = response.get("recommended_actions") if isinstance(response.get("recommended_actions"), list) else []
    if not response.get("route"):
        failures.append("missing_route")
    if not lane_items:
        failures.append("missing_object_lane")
    if not evidence and not gaps:
        failures.append("missing_evidence_or_gap")
    if not actions:
        failures.append("missing_recommended_action")
    if "edge" in checked_axes and not edges and not _gap_declares_not_applicable(gaps, "edge"):
        failures.append("missing_edge")
    if "freshness" in checked_axes and not _has_freshness_signal(response, evidence):
        failures.append("missing_freshness")
    if "gap" in checked_axes and not isinstance(response.get("gaps"), list):
        failures.append("missing_gap_field")
    if checked_axes:
        for lane, value in lanes.items():
            safe_lane = str(lane or "")
            if isinstance(value, list) and not value and not _empty_lane_is_stated(safe_lane, gaps):
                failures.append(f"empty_authority_lane_not_stated:{safe_lane}")
    if checked_axes and _is_runtime_claim(query, response) and not _has_runtime_evidence_or_gap(response, evidence, gaps):
        failures.append("runtime_evidence_missing")
    result = {
        "query": query,
        "passes": not failures,
        "failures": failures,
        "checked_axes": checked_axes,
    }
    ensure_public_safe(result, "GoldenQueryEvalResult")
    return result


def _activation_phase_progress(phase: str, coverage: Mapping[str, Any]) -> dict[str, Any]:
    result = str(coverage.get("result") or "missing")
    state = _activation_phase_state(phase, result)
    gaps = [str(gap) for gap in coverage.get("gaps", []) if str(gap or "")]
    return {
        "phase": phase,
        "state": state,
        "quality_result": result,
        "golden_query_family": str(coverage.get("golden_query_family") or ""),
        "evaluator": str(coverage.get("evaluator") or ""),
        "gaps": gaps,
        "production_mutation_performed": False,
        "next_action": _activation_phase_next_action(phase, state, gaps),
    }


def _product_evidence_summary() -> list[dict[str, Any]]:
    p6 = _p6_session_project_rollup_evidence()
    p7 = _p7_preference_artifact_evidence()
    p8 = _p8_runtime_authority_evidence()
    p9 = _p9_agent_context_evidence(preference_preview=p7)
    return [p6, p7, p8, p9]


def _product_evidence_check(phase: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    gaps: list[str] = []
    if not evidence:
        failures.append("product_evidence_missing")
    elif bool(evidence.get("production_mutation_performed")):
        failures.append(f"{phase.lower()}_production_mutation_performed")
    if phase == "P6" and evidence:
        failures.extend(_p6_evidence_failures(evidence))
    elif phase == "P7" and evidence:
        failures.extend(_p7_evidence_failures(evidence))
    elif phase == "P8" and evidence:
        failures.extend(_p8_evidence_failures(evidence))
        gaps.extend(_p8_evidence_gaps(evidence))
    elif phase == "P9" and evidence:
        failures.extend(_p9_evidence_failures(evidence))
    return {
        "phase": phase,
        "result": "FAIL" if failures else ("PASS_WITH_GAPS" if gaps else "PASS"),
        "schema_version": str(evidence.get("schema_version") or ""),
        "failures": failures,
        "gaps": _dedupe(gaps),
    }


def _p6_evidence_failures(evidence: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if evidence.get("schema_version") != "object_extraction_session_project_rollup_preview.v1":
        failures.append("p6_schema_mismatch")
    if int(evidence.get("object_count") or 0) < 5:
        failures.append("p6_session_rollup_incomplete")
    if int(evidence.get("edge_count") or 0) < 6:
        failures.append("p6_session_rollup_incomplete")
    if int(evidence.get("evidence_count") or 0) < 1:
        failures.append("p6_session_rollup_incomplete")
    if evidence.get("handoff_pack_schema") != "session_project_handoff_pack.v1":
        failures.append("p6_handoff_pack_missing")
    return _dedupe(failures)


def _p7_evidence_failures(evidence: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if evidence.get("schema_version") != "object_extraction_preference_style_preview.v1":
        failures.append("p7_schema_mismatch")
    if int(evidence.get("object_count") or 0) < 2:
        failures.append("p7_preference_style_objects_missing")
    if int(evidence.get("source_evidence_ref_count") or 0) < 1:
        failures.append("p7_source_evidence_missing")
    if evidence.get("artifact_preference_pack_status") != "pass":
        failures.append("p7_artifact_preference_pack_not_pass")
    if int(evidence.get("accepted_preference_count") or 0) < 1:
        failures.append("p7_accepted_preference_missing")
    return failures


def _p8_evidence_failures(evidence: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if evidence.get("schema_version") != "object_extraction_runtime_truth_preview.v1":
        failures.append("p8_schema_mismatch")
    if evidence.get("runtime_evidence_collection_plan_schema") != "source_to_candidate_runtime_evidence_collection_plan.v1":
        failures.append("p8_runtime_evidence_collection_plan_missing")
    if evidence.get("runtime_evidence_collection_plan_status") != "ready":
        failures.append("p8_runtime_evidence_collection_plan_not_ready")
    if bool(evidence.get("runtime_evidence_collection_plan_network_used")):
        failures.append("p8_runtime_evidence_collection_plan_used_network")
    if bool(evidence.get("runtime_evidence_collection_plan_mutation_allowed")):
        failures.append("p8_runtime_evidence_collection_plan_mutation_allowed")
    if bool(evidence.get("runtime_evidence_collection_plan_production_mutation_performed")):
        failures.append("p8_runtime_evidence_collection_plan_mutated_production")
    if evidence.get("runtime_evidence_collection_plan_readiness_claim") != "plan_only_not_runtime_evidence":
        failures.append("p8_runtime_evidence_collection_plan_claims_live_evidence")
    if evidence.get("shadow_route_smoke_request_schema") != "source_to_candidate_runtime_shadow_collection_request.v1":
        failures.append("p8_shadow_route_smoke_request_missing")
    if evidence.get("shadow_route_smoke_request_status") != "requested":
        failures.append("p8_shadow_route_smoke_not_requested")
    if int(evidence.get("shadow_route_smoke_route_count") or 0) < 1:
        failures.append("p8_shadow_route_smoke_routes_missing")
    if bool(evidence.get("shadow_route_smoke_network_used")):
        failures.append("p8_shadow_route_smoke_used_network")
    if bool(evidence.get("shadow_route_smoke_mutation_allowed")):
        failures.append("p8_shadow_route_smoke_mutation_allowed")
    if bool(evidence.get("shadow_route_smoke_production_mutation_performed")):
        failures.append("p8_shadow_route_smoke_mutated_production")
    if evidence.get("shadow_route_smoke_readiness_claim") != "request_only_not_live_evidence":
        failures.append("p8_shadow_route_smoke_claims_live_evidence")
    runtime_evidence_count = int(evidence.get("runtime_verified_count") or 0) + int(
        evidence.get("runtime_unverified_count") or 0
    )
    if runtime_evidence_count < 1:
        failures.append("p8_runtime_evidence_classification_missing")
    if evidence.get("permission") != "allowed" or evidence.get("permission_reason") != "approved_scope_present":
        failures.append("p8_preapproved_scope_missing")
    if bool(evidence.get("authority_write_performed")):
        failures.append("p8_authority_write_performed")
    if bool(evidence.get("production_mutation_performed")):
        failures.append("p8_production_mutation_performed")
    return failures


def _p8_evidence_gaps(evidence: Mapping[str, Any]) -> list[str]:
    gaps: list[str] = []
    if int(evidence.get("runtime_unverified_count") or 0) > 0:
        gaps.append("p8_runtime_evidence_unverified")
    if int(evidence.get("runtime_verified_count") or 0) < 1:
        gaps.append("p8_runtime_verified_evidence_missing")
    if evidence.get("runtime_evidence_collection_plan_readiness_claim") == "plan_only_not_runtime_evidence":
        gaps.append("p8_runtime_evidence_collection_plan_not_live_evidence")
    if evidence.get("shadow_route_smoke_readiness_claim") == "request_only_not_live_evidence":
        gaps.append("p8_shadow_route_smoke_collection_pending")
        gaps.extend(
            f"p8_shadow_route_smoke_collection_pending:{route}"
            for route in evidence.get("shadow_route_smoke_pending_routes", [])
            if isinstance(route, str) and route
        )
    return gaps


def _p9_evidence_failures(evidence: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if evidence.get("schema_version") != "agent_context_product_pack.v1":
        failures.append("p9_schema_mismatch")
    section_counts = evidence.get("section_counts") if isinstance(evidence.get("section_counts"), Mapping) else {}
    if int(section_counts.get("style_preference") or 0) < 1:
        failures.append("p9_style_preference_section_missing")
    if int(section_counts.get("active_work") or 0) < 1:
        failures.append("p9_active_work_section_missing")
    if int(evidence.get("tool_hint_count") or 0) < 4:
        failures.append("p9_object_native_tool_hints_missing")
    if bool(evidence.get("mutation_allowed")):
        failures.append("p9_mutation_allowed")
    return failures


def _p6_session_project_rollup_evidence() -> dict[str, Any]:
    from .extraction_pipeline import run_session_project_rollup_preview

    report = run_session_project_rollup_preview(
        sessions=[
            {
                "session_id_hash": "session:p6-a",
                "device_id_hash": "device:this",
                "provider": "codex",
                "summary": "P6 rollup fixture session.",
                "work_unit_id": "work:p6",
                "evidence_refs": ["ev:p6:session"],
            }
        ],
        repository="neurons",
        branch="codex/knowledge-object-review-flow-roadmap",
        project="neurons",
        specs=[{"spec_ref": "docs/specs/p6/design.md", "work_unit_id": "work:p6"}],
        pull_requests=[{"pr_id": "pr:95", "number": 95, "work_unit_id": "work:p6"}],
        commits=[{"commit_id": "commit:p6", "pull_request_id": "pr:95", "work_unit_id": "work:p6"}],
        requesting_device_id_hash="device:this",
        scope="all_devices",
    )
    return {
        "phase": "P6",
        "schema_version": str(report.get("schema_version") or ""),
        "status": str(report.get("status") or ""),
        "golden_query_slice": _golden_slice(report),
        "object_count": int(report.get("object_count") or 0),
        "edge_count": int(report.get("edge_count") or 0),
        "evidence_count": int(report.get("evidence_count") or 0),
        "handoff_pack_schema": str((report.get("handoff_pack") or {}).get("schema_version") or ""),
        "gaps": list(report.get("gaps") or []),
        "production_mutation_performed": bool(report.get("production_mutation_performed")),
    }


def _p7_preference_artifact_evidence() -> dict[str, Any]:
    from .extraction_pipeline import run_preference_style_extraction_preview

    report = run_preference_style_extraction_preview(
        memory_cards=[
            {
                "memory_id": "mem:p7-html",
                "card_type": "preference",
                "summary": "Accepted HTML artifact preference",
                "confidence": 0.94,
                "currentness": "current",
                "review_state": "accepted",
                "typed_payload": {
                    "preference": "HTML review artifacts should be information dense.",
                    "applies_to": "html review artifact",
                    "reason": "Accepted local fixture preference.",
                },
                "source_refs": [{"source_ref_id": "ev:p7:preference"}],
            },
            {
                "memory_id": "mem:p7-style",
                "card_type": "repo_style",
                "summary": "Accepted worker test command",
                "confidence": 0.91,
                "review_state": "accepted",
                "typed_payload": {
                    "claim": "Python worker tests use uv run pytest.",
                    "repo_scope": "neurons/worker",
                    "reason": "Repo instructions and verified runs.",
                    "files": ["worker/tests/test_golden_query_eval.py"],
                    "commits": ["commit:p7"],
                },
            },
        ],
        repository="neurons",
        current_request="review HTML artifact and worker test evidence",
        current_files=["worker/tests/test_golden_query_eval.py"],
        artifact_review={
            "artifact_type": "html_review",
            "summary": "Information dense HTML review artifact.",
            "text_metrics": {"word_count": 120},
        },
    )
    artifact_pack = report.get("artifact_preference_pack") if isinstance(report.get("artifact_preference_pack"), Mapping) else {}
    accepted_count = len((artifact_pack.get("lanes") or {}).get("accepted_current") or [])
    return {
        "phase": "P7",
        "schema_version": str(report.get("schema_version") or ""),
        "status": str(report.get("status") or ""),
        "golden_query_slice": _golden_slice(report),
        "object_count": len(report.get("objects") or []),
        "source_evidence_ref_count": len(report.get("source_evidence_refs") or []),
        "artifact_preference_pack_status": "pass" if accepted_count else "pass_with_gaps",
        "accepted_preference_count": accepted_count,
        "proposal_preference_count": len((artifact_pack.get("lanes") or {}).get("proposal_only") or []),
        "artifact_review_check_status": str((report.get("artifact_review_check") or {}).get("status") or ""),
        "gaps": list(report.get("gaps") or []),
        "production_mutation_performed": bool(report.get("production_mutation_performed")),
    }


def _p8_runtime_authority_evidence() -> dict[str, Any]:
    from .extraction_pipeline import run_runtime_truth_extraction_preview
    from .runtime_readiness import build_source_to_candidate_runtime_evidence_collection_plan

    expected_commit = "e3f6296"
    report = run_runtime_truth_extraction_preview(
        pull_request={"id": "pr:95", "merged": False, "head_sha": expected_commit},
        deployment={
            "target": "production",
            "artifact_digest": "sha256:" + "a" * 64,
            "deployed_source_commit": expected_commit,
            "private_authority_ref": "redacted-private-authority",
        },
        live_evidence=None,
        ci_statuses=[
            {"name": "worker pytest", "conclusion": "SUCCESS", "commit_sha": expected_commit},
            {"name": "gradle-test", "conclusion": "SUCCESS", "commit_sha": expected_commit},
        ],
        runtime_surface={
            "surface_ref": "lbrain-mcp-read-path",
            "surface_kind": "mcp_http",
            "object_native_tools": True,
        },
        requested_action={"action": "promote_runtime_authority", "target": "production"},
        actor={"agent": "codex", "role": "agent", "approved_scope": True},
        consumer="codex",
    )
    preview = report.get("pack_preview") if isinstance(report.get("pack_preview"), Mapping) else {}
    identity = report.get("deployed_artifact_identity") if isinstance(report.get("deployed_artifact_identity"), Mapping) else {}
    permission = report.get("permission_check") if isinstance(report.get("permission_check"), Mapping) else {}
    collection_plan = build_source_to_candidate_runtime_evidence_collection_plan(
        expected_commit=expected_commit,
        repository="pureliture/neurons",
        branch="codex/knowledge-object-review-flow-roadmap",
        consumer="codex",
    )
    shadow_requests = [
        item
        for item in collection_plan.get("shadow_collection_requests", [])
        if isinstance(item, Mapping) and item.get("request_id") == "shadow_brain_objects_query_route_smoke"
    ]
    shadow_request = shadow_requests[0] if shadow_requests else {}
    return {
        "phase": "P8",
        "schema_version": str(report.get("schema_version") or ""),
        "status": str(report.get("status") or ""),
        "golden_query_slice": _golden_slice(report),
        "object_count": len(report.get("objects") or []),
        "edge_count": len(report.get("edges") or []),
        "runtime_verified_count": int(preview.get("runtime_verified_count") or 0),
        "runtime_unverified_count": int(preview.get("runtime_unverified_count") or 0),
        "source_commit_matches_pr_head": bool(identity.get("source_commit_matches_pr_head")),
        "permission": str(permission.get("permission") or ""),
        "permission_reason": str(permission.get("reason") or ""),
        "authority_write_performed": bool(permission.get("authority_write_performed")),
        "runtime_evidence_collection_plan_schema": str(collection_plan.get("schema_version") or ""),
        "runtime_evidence_collection_plan_status": str(collection_plan.get("status") or ""),
        "runtime_evidence_collection_plan_required_step_count": len(collection_plan.get("required_steps") or []),
        "runtime_evidence_collection_plan_network_used": bool(collection_plan.get("network_used")),
        "runtime_evidence_collection_plan_mutation_allowed": bool(collection_plan.get("mutation_allowed")),
        "runtime_evidence_collection_plan_production_mutation_performed": bool(
            collection_plan.get("production_mutation_performed")
        ),
        "runtime_evidence_collection_plan_readiness_claim": str(collection_plan.get("readiness_claim") or ""),
        "shadow_route_smoke_request_schema": str(shadow_request.get("schema_version") or ""),
        "shadow_route_smoke_request_status": str(shadow_request.get("status") or ""),
        "shadow_route_smoke_route_count": len(shadow_request.get("routes") or []),
        "shadow_route_smoke_pending_routes": [
            str(route) for route in shadow_request.get("routes", []) if isinstance(route, str) and route
        ],
        "shadow_route_smoke_network_used": bool(shadow_request.get("network_used")),
        "shadow_route_smoke_mutation_allowed": bool(shadow_request.get("mutation_allowed")),
        "shadow_route_smoke_production_mutation_performed": bool(
            shadow_request.get("production_mutation_performed")
        ),
        "shadow_route_smoke_readiness_claim": str(shadow_request.get("readiness_claim") or ""),
        "gaps": list(preview.get("gaps") or []),
        "production_mutation_performed": bool(report.get("production_mutation_performed")),
    }


def _p9_agent_context_evidence(*, preference_preview: Mapping[str, Any]) -> dict[str, Any]:
    from ..context_builder import build_agent_context_product_pack

    preference_object = {
        "object_id": "ko:ArtifactPreference:p9-html",
        "object_type": "ArtifactPreference",
        "title": "HTML review artifacts should be information dense.",
        "authority_lane": "accepted_current",
        "recommended_action": "apply_preference",
    }
    preference_pack = {
        "objects": [preference_object],
        "lanes": {"accepted_current": [preference_object], "proposal_only": []},
        "gaps": [],
    }
    active_work_object = {
        "object_id": "ko:WorkUnit:p9-active-work",
        "object_type": "WorkUnit",
        "title": "Continue source-to-candidate graph product activation",
        "authority_lane": "reference_only",
        "recommended_action": "resume",
    }
    active_work_pack = {
        "objects": [active_work_object],
        "lanes": {"reference_only": [active_work_object]},
        "gaps": [],
    }
    product = build_agent_context_product_pack(
        consumer="codex",
        block={
            "object_packs": {
                "preferences": preference_pack,
                "current_work": active_work_pack,
                "required_verification": {
                    "objects": [
                        {
                            "object_id": "ko:Verification:worker",
                            "object_type": "VerificationCommand",
                            "title": "cd worker && uv run pytest -q",
                            "authority_lane": "candidate",
                            "recommended_action": "run",
                        }
                    ],
                    "lanes": {"candidate": [{"object_id": "ko:Verification:worker"}]},
                    "gaps": [],
                },
            },
            "documents": [],
            "preferences": [{"summary": "HTML review artifacts should be information dense."}],
            "workflow_contracts": [],
        },
        gaps=["runtime_evidence_unverified"],
        cards=[{"currentness": "current"}],
    )
    section_counts = {
        name: int(section.get("object_count") or 0)
        for name, section in product.get("sections", {}).items()
        if isinstance(section, Mapping)
    }
    return {
        "phase": "P9",
        "schema_version": str(product.get("schema_version") or ""),
        "status": "pass_with_gaps" if product.get("degraded_mode", {}).get("active") else "pass",
        "consumer": str(product.get("consumer") or ""),
        "section_counts": section_counts,
        "tool_hint_count": len(product.get("tool_hints") or []),
        "action_hint_count": len(product.get("action_hints") or []),
        "mutation_allowed": bool(product.get("surface_policy", {}).get("mutation_allowed")),
        "degraded_mode_active": bool(product.get("degraded_mode", {}).get("active")),
        "gaps": list(product.get("degraded_mode", {}).get("gaps") or []),
        "production_mutation_performed": False,
    }


def _golden_slice(report: Mapping[str, Any]) -> str:
    evaluator = report.get("evaluator_report") if isinstance(report.get("evaluator_report"), Mapping) else {}
    return str(evaluator.get("golden_query_slice") or "")


def _activation_phase_state(phase: str, quality_result: str) -> str:
    if quality_result == "FAIL":
        return "blocked"
    if phase == "P5":
        return "in_progress"
    if phase in _LOCAL_VALIDATED_PHASES and quality_result in {"PASS", "PASS_WITH_GAPS"}:
        return "local_validated"
    if quality_result == "PASS":
        return "local_validated"
    if quality_result == "missing":
        return "missing"
    return "in_progress"


def _activation_phase_next_action(phase: str, state: str, gaps: list[str]) -> str:
    if state == "blocked":
        return "fix_quality_failure"
    if phase == "P5":
        return "keep_continuous_quality_gate_active_until_release_gate_green"
    if any("production" in gap or "live" in gap for gap in gaps):
        return "collect_bounded_runtime_or_production_evidence"
    if state == "local_validated":
        return "advance_next_phase_with_gap_visible"
    return "complete_local_phase_slice"


def _minimum_review_loop_checkpoint(
    phase_progress: list[Mapping[str, Any]],
    source_gate: Mapping[str, Any],
) -> dict[str, Any]:
    by_phase = {str(item.get("phase") or ""): item for item in phase_progress}
    checkpoint_items = [by_phase.get(phase, {}) for phase in MINIMUM_REVIEW_LOOP_PHASES]
    checkpoint_failures = [
        str(item.get("phase") or "unknown")
        for item in checkpoint_items
        if item.get("quality_result") == "FAIL" or item.get("state") in {"missing", "blocked"}
    ]
    gaps = _dedupe(
        [
            *[
                str(gap)
                for item in checkpoint_items
                for gap in item.get("gaps", [])
                if str(gap or "")
            ],
            *[
                str(gap)
                for gap in source_gate.get("gaps", [])
                if str(gap or "")
            ],
        ]
    )
    return {
        "phases": list(MINIMUM_REVIEW_LOOP_PHASES),
        "status": "FAIL" if checkpoint_failures else ("PASS_WITH_GAPS" if gaps else "PASS"),
        "local_product_loop_ready": not checkpoint_failures,
        "does_not_complete_goal": True,
        "failures": checkpoint_failures,
        "gaps": gaps,
    }


def _activation_goal_blockers(
    *,
    phase_coverage: Mapping[str, Any],
    source_gate: Mapping[str, Any],
    phase_progress: list[Mapping[str, Any]],
) -> list[str]:
    blockers = [
        str(gap)
        for gap in phase_coverage.get("gaps", [])
        if str(gap or "")
    ]
    blockers.extend(str(gap) for gap in source_gate.get("gaps", []) if str(gap or ""))
    for item in phase_progress:
        blockers.extend(str(gap) for gap in item.get("gaps", []) if str(gap or ""))
    if source_gate.get("release_quality_gate") != "green":
        blockers.append("production_quality_not_green")
    return _dedupe(blockers)


def _next_activation_phase(phase_progress: list[Mapping[str, Any]]) -> str:
    for item in phase_progress:
        if item.get("state") == "in_progress":
            return str(item.get("phase") or "")
    for item in phase_progress:
        if item.get("state") not in {"local_validated", "production_validated", "complete"}:
            return str(item.get("phase") or "")
    return ""


def _remaining_activation_phases(next_phase: str) -> list[str]:
    if not next_phase or next_phase not in ACTIVATION_SCOPE_PHASES:
        return []
    start = ACTIVATION_SCOPE_PHASES.index(next_phase)
    return list(ACTIVATION_SCOPE_PHASES[start:])


def _dedupe(values: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _phase_coverage(
    *,
    phase: str,
    title: str,
    golden_query_family: str,
    query: str,
    result: str,
    evaluator: str,
    gaps: list[str],
) -> dict[str, Any]:
    return {
        "phase": phase,
        "title": title,
        "golden_query_family": golden_query_family,
        "query": query,
        "result": result,
        "evaluator": evaluator,
        "required_axes": list(REQUIRED_QUALITY_AXES),
        "gaps": list(gaps),
    }


def _planned_phase(phase: str, title: str, golden_query_family: str, query: str) -> dict[str, Any]:
    return _phase_coverage(
        phase=phase,
        title=title,
        golden_query_family=golden_query_family,
        query=query,
        result="planned",
        evaluator="not_implemented",
        gaps=["phase_slice_not_implemented"],
    )


def _gap_declares_not_applicable(gaps: list[Any], axis: str) -> bool:
    wanted = {f"{axis}_not_applicable", f"{axis}s_not_applicable"}
    return any(str(item or "") in wanted for item in gaps)


def _has_freshness_signal(response: Mapping[str, Any], evidence: list[Any]) -> bool:
    if response.get("freshness") or response.get("freshness_gaps"):
        return True
    verification = response.get("verification")
    if isinstance(verification, Mapping):
        for key in ("freshness", "freshness_checked", "freshness_gaps", "freshness_verified"):
            value = verification.get(key)
            if isinstance(value, list) and value:
                return True
            if isinstance(value, Mapping) and value:
                return True
            if isinstance(value, bool) and value:
                return True
            if isinstance(value, str) and value:
                return True
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        verification_state = str(item.get("verification_state") or "")
        evidence_type = str(item.get("evidence_type") or "").lower()
        if verification_state in {"freshness_checked", "freshness_verified"} or "freshness" in evidence_type:
            return True
    return False


def _empty_lane_is_stated(lane: str, gaps: list[Any]) -> bool:
    for item in gaps:
        text = str(item or "").lower()
        if lane.lower() in text and any(marker in text for marker in ("empty", "missing", "none")):
            return True
    return False


def _is_runtime_claim(query: str, response: Mapping[str, Any]) -> bool:
    route = str(response.get("route") or "").lower()
    text = f"{route} {query}".lower()
    return any(marker in text for marker in ("runtime", "런타임", "deploy", "deployment", "배포", "live"))


def _source_to_authority_fixture_pack() -> dict[str, Any]:
    evidence = EvidenceRef.from_parts(
        evidence_type="source_freshness",
        authority_lane="reference_only",
        verification_state="freshness_checked",
        locator={"kind": "relative_repo_path", "value": "docs/specs/lbrain-source-fixture.md"},
        content_hash=hash_payload({"fixture": "source-to-authority-quality-gate"}),
        summary="Public-safe source fixture with freshness evidence.",
    )
    obj = KnowledgeObjectEnvelope.from_parts(
        object_type="RepoDocument",
        natural_key="docs/specs/lbrain-source-fixture.md",
        scope={"project": "neurons"},
        title="Source-to-authority fixture",
        summary="AI extracted candidate claim.",
        lifecycle_status="proposed",
        authority_lane="candidate",
        verification_state="freshness_checked",
        review_state="needs_review",
        content_hash=hash_payload({"fixture": "candidate-object"}),
        evidence_refs=[evidence.evidence_id],
        confidence={"score": 0.82, "basis": "deterministic_quality_gate_fixture"},
        recommended_action="review",
        freshness={"source_checked": True, "state": "freshness_checked"},
        payload={"path_ref": "docs/specs/lbrain-source-fixture.md"},
    ).to_dict()
    edge = KnowledgeEdge.from_parts(
        edge_type="requires_evidence",
        from_object_id=obj["object_id"],
        to_object_id=obj["object_id"],
        evidence_refs=[evidence.evidence_id],
        lifecycle_status="proposed",
        authority_lane="candidate",
        verification_state="freshness_checked",
        confidence={"score": 0.77, "basis": "deterministic_quality_gate_fixture"},
        payload={"source": "quality_gate_fixture"},
    ).to_dict()
    return build_candidate_graph_review_pack(
        objects=[obj],
        edges=[edge],
        evidence=[evidence.to_view()],
        extractor="quality_gate_fixture_extractor",
        reviewer_actions=["promote", "reject", "hold", "request_more_evidence"],
        consumer="codex",
    )


def _object_native_product_surface_checks() -> list[dict[str, Any]]:
    from agent_knowledge.mcp_tools import (
        BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME,
        BRAIN_CANDIDATE_REVIEW_EDIT_TOOL_NAME,
        BRAIN_OBJECTS_QUERY_TOOL_NAME,
        BRAIN_SOURCE_TO_CANDIDATE_RUNTIME_READINESS_TOOL_NAME,
        BRAIN_SOURCE_TO_CANDIDATE_GRAPH_TOOL_NAME,
        tool_registry,
    )

    registry = tool_registry()
    return [
        {
            "id": "mcp_brain_objects_query_tool",
            "surface": "mcp",
            "tool": BRAIN_OBJECTS_QUERY_TOOL_NAME,
            "result": _pass_fail(
                _tool_requires_fields(
                    registry,
                    BRAIN_OBJECTS_QUERY_TOOL_NAME,
                    ("repository", "branch", "query"),
                )
            ),
            "read_path": True,
            "production_mutation_performed": False,
        },
        {
            "id": "mcp_source_to_candidate_graph_tool",
            "surface": "mcp",
            "tool": BRAIN_SOURCE_TO_CANDIDATE_GRAPH_TOOL_NAME,
            "result": _pass_fail(
                _tool_has_target_enum(
                    registry,
                    BRAIN_SOURCE_TO_CANDIDATE_GRAPH_TOOL_NAME,
                    ("local_test", "production"),
                )
            ),
            "local_test_preview_allowed": True,
            "production_target_denied": True,
            "production_mutation_performed": False,
        },
        {
            "id": "mcp_candidate_review_edit_tool",
            "surface": "mcp",
            "tool": BRAIN_CANDIDATE_REVIEW_EDIT_TOOL_NAME,
            "result": _pass_fail(
                _tool_requires_fields(
                    registry,
                    BRAIN_CANDIDATE_REVIEW_EDIT_TOOL_NAME,
                    ("pack", "edits"),
                )
            ),
            "authority_write_performed": False,
            "production_mutation_performed": False,
        },
        {
            "id": "mcp_approval_board_decide_tool",
            "surface": "mcp",
            "tool": BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME,
            "result": _pass_fail(
                _tool_has_target_enum(
                    registry,
                    BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME,
                    ("local_test", "production"),
                )
                and _tool_requires_fields(registry, BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME, ("pack", "decisions"))
            ),
            "local_test_preview_allowed": True,
            "production_target_denied": True,
            "production_mutation_performed": False,
        },
        {
            "id": "mcp_source_to_candidate_runtime_readiness_tool",
            "surface": "mcp",
            "tool": BRAIN_SOURCE_TO_CANDIDATE_RUNTIME_READINESS_TOOL_NAME,
            "result": _pass_fail(
                _tool_has_properties(
                    registry,
                    BRAIN_SOURCE_TO_CANDIDATE_RUNTIME_READINESS_TOOL_NAME,
                    ("live_evidence", "expected_commit"),
                )
            ),
            "network_used": False,
            "production_mutation_performed": False,
        },
    ]


def _tool_has_target_enum(registry: Mapping[str, Any], tool_name: str, expected: tuple[str, ...]) -> bool:
    tool = registry.get(tool_name)
    if not isinstance(tool, Mapping):
        return False
    schema = tool.get("inputSchema")
    properties = schema.get("properties") if isinstance(schema, Mapping) else {}
    target = properties.get("target") if isinstance(properties, Mapping) else {}
    return isinstance(target, Mapping) and tuple(target.get("enum") or ()) == expected


def _tool_requires_fields(registry: Mapping[str, Any], tool_name: str, expected: tuple[str, ...]) -> bool:
    tool = registry.get(tool_name)
    if not isinstance(tool, Mapping):
        return False
    schema = tool.get("inputSchema")
    required = schema.get("required") if isinstance(schema, Mapping) else []
    return all(field in required for field in expected)


def _tool_has_properties(registry: Mapping[str, Any], tool_name: str, expected: tuple[str, ...]) -> bool:
    tool = registry.get(tool_name)
    if not isinstance(tool, Mapping):
        return False
    schema = tool.get("inputSchema")
    properties = schema.get("properties") if isinstance(schema, Mapping) else {}
    return isinstance(properties, Mapping) and all(field in properties for field in expected)


def _pass_fail(condition: bool) -> str:
    return "PASS" if condition else "FAIL"


def _first_lane_item(pack: Mapping[str, Any], lane: str) -> dict[str, Any]:
    lanes = pack.get("lanes") if isinstance(pack.get("lanes"), Mapping) else {}
    items = lanes.get(lane)
    if isinstance(items, list) and items and isinstance(items[0], Mapping):
        return dict(items[0])
    return {}


def _has_runtime_evidence_or_gap(response: Mapping[str, Any], evidence: list[Any], gaps: list[Any]) -> bool:
    verification = response.get("verification")
    if isinstance(verification, Mapping):
        for key in ("runtime_verified", "runtime_unverified"):
            value = verification.get(key)
            if isinstance(value, list) and value:
                return True
    for item in evidence:
        if isinstance(item, Mapping) and str(item.get("verification_state") or "") in {
            "runtime_verified",
            "runtime_unverified",
        }:
            return True
    for item in gaps:
        if "runtime_evidence" in str(item or ""):
            return True
    return False
