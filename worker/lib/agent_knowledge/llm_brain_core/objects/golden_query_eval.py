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
PRODUCT_EVIDENCE_PHASES = ("P2", "P3", "P4", "P6", "P7", "P8", "P9")
DEFERRED_SCOPE_GAPS = {
    "future_phase_golden_query_slices_planned",
    "future_phase_slices_planned",
}

_LOCAL_VALIDATED_PHASES = {"P2", "P3", "P4", "P6", "P7", "P8", "P9"}
P9_ALLOWED_TOOL_SAFE_TARGETS = {
    "brain_objects_query": frozenset({"read_only_object_pack"}),
    "brain_source_to_candidate_graph": frozenset({"local_test"}),
    "brain_candidate_review_edit": frozenset({"local_test_pack"}),
    "brain_approval_board_decide": frozenset({"local_test"}),
    "brain_source_to_candidate_runtime_readiness": frozenset({"sanitized_evidence_packet"}),
}


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
            result="PASS",
            evaluator="phase golden query coverage report",
            gaps=[],
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
        "release_quality_gate": "green",
        "required_axes": list(REQUIRED_QUALITY_AXES),
        "phases": phases,
        "gaps": [
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
    local_quality_gate = "blocked" if hard_failures else "green"
    report = {
        "schema_version": "source_to_authority_quality_gate_report.v1",
        "status": "FAIL" if hard_failures else "PASS_WITH_GAPS",
        "local_quality_gate": local_quality_gate,
        "release_quality_gate": "blocked" if hard_failures else "green",
        "required_axes": list(REQUIRED_QUALITY_AXES),
        "path_checks": path_checks,
        "product_surface_checks": product_surface_checks,
        "hard_failures": hard_failures,
        "gaps": [
            "production_authority_gate_preapproved_not_executed",
            "live_runtime_read_path_unverified",
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


def build_product_activation_progress_report(
    *, live_evidence: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    phase_coverage = build_phase_golden_query_coverage_report()
    source_gate = build_source_to_authority_quality_gate_report()
    product_evidence_summary = _product_evidence_summary(live_evidence=live_evidence)
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
    phase_progress = _apply_product_evidence_to_phase_progress(
        phase_progress,
        product_evidence_result=product_evidence_result,
    )
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
    if any(
        item.get("phase") == "P4" and item.get("result") == "PASS"
        for item in product_evidence_result.get("checks", [])
        if isinstance(item, Mapping)
    ):
        blockers = [
            blocker
            for blocker in blockers
            if blocker != "production_authority_gate_preapproved_not_executed"
        ]
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
            "source_to_authority_local_quality_gate": source_gate["local_quality_gate"],
            "source_to_authority_release_quality_gate": source_gate["release_quality_gate"],
        },
        "production_approval_gate": str(source_gate.get("production_approval_gate") or ""),
        "production_mutation_execution": str(source_gate.get("production_mutation_execution") or ""),
        "local_quality_gate": str(source_gate.get("local_quality_gate") or ""),
        "release_quality_gate": "blocked" if hard_failures else source_gate["release_quality_gate"],
        "goal_completion_blockers": blockers,
        "hard_failures": hard_failures,
        "goal_complete": production_ready and not blockers,
        "production_ready": production_ready,
        "production_mutation_performed": bool(
            source_gate.get("production_mutation_performed")
            or (
                isinstance(live_evidence, Mapping)
                and (
                    live_evidence.get("production_mutation_performed") is True
                    or live_evidence.get("mutation_performed") is True
                )
            )
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


def _product_evidence_summary(
    *, live_evidence: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    p2 = _p2_reference_corpus_evidence()
    p3 = _p3_projection_join_evidence(live_evidence=live_evidence)
    p4 = _p4_replacement_current_evidence(live_evidence=live_evidence)
    p6 = _p6_session_project_rollup_evidence(live_evidence=live_evidence)
    p7 = _p7_preference_artifact_evidence(live_evidence=live_evidence)
    p8 = _p8_runtime_authority_evidence(live_evidence=live_evidence)
    p9 = _p9_agent_context_evidence(preference_preview=p7, live_evidence=live_evidence)
    return [p2, p3, p4, p6, p7, p8, p9]


def _product_evidence_check(phase: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    gaps: list[str] = []
    if not evidence:
        failures.append("product_evidence_missing")
    elif bool(evidence.get("production_mutation_performed")):
        if phase not in {"P2", "P4"}:
            failures.append(f"{phase.lower()}_production_mutation_performed")
    if phase == "P2" and evidence:
        failures.extend(_p2_evidence_failures(evidence))
        gaps.extend(_p2_evidence_gaps(evidence))
    elif phase == "P3" and evidence:
        failures.extend(_p3_evidence_failures(evidence))
        gaps.extend(_p3_evidence_gaps(evidence))
    elif phase == "P4" and evidence:
        failures.extend(_p4_evidence_failures(evidence))
        gaps.extend(_p4_evidence_gaps(evidence))
    elif phase == "P6" and evidence:
        failures.extend(_p6_evidence_failures(evidence))
        gaps.extend(_p6_evidence_gaps(evidence))
    elif phase == "P7" and evidence:
        failures.extend(_p7_evidence_failures(evidence))
        gaps.extend(_p7_evidence_gaps(evidence))
    elif phase == "P8" and evidence:
        failures.extend(_p8_evidence_failures(evidence))
        gaps.extend(_p8_evidence_gaps(evidence))
    elif phase == "P9" and evidence:
        failures.extend(_p9_evidence_failures(evidence))
        gaps.extend(_p9_evidence_gaps(evidence))
    return {
        "phase": phase,
        "result": "FAIL" if failures else ("PASS_WITH_GAPS" if gaps else "PASS"),
        "schema_version": str(evidence.get("schema_version") or ""),
        "failures": failures,
        "gaps": _dedupe(gaps),
    }


def _p2_evidence_failures(evidence: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    status = str(evidence.get("status") or "")
    if evidence.get("schema_version") != "reference_corpus_production_ingest_readiness.v1":
        failures.append("p2_schema_mismatch")
    if evidence.get("status") == "FAIL":
        failures.append("p2_production_corpus_ingest_failed")
    if bool(evidence.get("network_used")):
        failures.append("p2_corpus_ingest_evaluator_used_network")
    if status == "PASS" and evidence.get("live_evidence_provided") is not True:
        failures.append("p2_live_evidence_missing_for_pass")
    if status == "PASS" and evidence.get("production_mutation_performed") is not True:
        failures.append("p2_production_corpus_ingest_mutation_missing_for_pass")
    if (
        evidence.get("live_evidence_provided") is not True
        and bool(evidence.get("evidence_collection_network_used"))
    ):
        failures.append("p2_evidence_collection_claimed_without_live_evidence")
    if (
        bool(evidence.get("production_mutation_performed"))
        and evidence.get("status") != "PASS"
    ):
        failures.append("p2_unvalidated_production_corpus_mutation")
    return failures


def _p2_evidence_gaps(evidence: Mapping[str, Any]) -> list[str]:
    return [
        f"p2_{gap}"
        for gap in evidence.get("gaps", [])
        if isinstance(gap, str) and gap
    ]


def _p3_projection_join_evidence(
    *, live_evidence: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    from .runtime_readiness import build_source_to_candidate_runtime_readiness_report

    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=live_evidence,
        expected_commit=_p3_expected_commit(live_evidence),
    )
    claims = {
        str(item.get("claim_id") or ""): item
        for item in report.get("claims", [])
        if isinstance(item, Mapping)
    }
    projection = claims.get("live.source_to_candidate.projection_join", {})
    projection_gaps = [
        gap
        for gap in projection.get("gaps", [])
        if isinstance(gap, str) and gap
    ]
    status = _p3_projection_join_product_status(
        projection_status=str(projection.get("status") or ""),
        gaps=projection_gaps,
    )
    return {
        "phase": "P3",
        "schema_version": "source_to_candidate_projection_join_product_evidence.v1",
        "status": status,
        "golden_query_slice": "corpus-to-design concept extraction",
        "runtime_readiness_schema": str(report.get("schema_version") or ""),
        "runtime_readiness_status": str(report.get("status") or ""),
        "projection_join_claim_id": str(projection.get("claim_id") or ""),
        "projection_join_claim_status": str(projection.get("status") or ""),
        "projection_join_edge_count": int(projection.get("edge_count") or 0),
        "live_evidence_provided": bool(report.get("live_evidence_provided")),
        "evidence_is_live": bool(report.get("evidence_is_live")),
        "production_ready": bool(report.get("production_ready")),
        "network_used": bool(
            report.get("network_used") or report.get("evidence_collection_network_used")
        ),
        "evidence_collection_network_used": bool(
            report.get("evidence_collection_network_used")
        ),
        "gaps": projection_gaps,
        "production_mutation_performed": bool(projection.get("production_mutation_performed")),
    }


def _p3_expected_commit(live_evidence: Mapping[str, Any] | None) -> str:
    evidence = live_evidence if isinstance(live_evidence, Mapping) else {}
    identity = evidence.get("deployed_identity")
    identity = identity if isinstance(identity, Mapping) else {}
    for key in ("source_merge_commit", "source_commit", "merge_commit"):
        value = identity.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _p3_projection_join_product_status(*, projection_status: str, gaps: list[str]) -> str:
    if projection_status == "failed":
        return "FAIL"
    if projection_status == "validated" and not gaps:
        return "PASS"
    return "PASS_WITH_GAPS"


def _p3_evidence_failures(evidence: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if evidence.get("schema_version") != "source_to_candidate_projection_join_product_evidence.v1":
        failures.append("p3_schema_mismatch")
    status = str(evidence.get("status") or "")
    projection_status = str(evidence.get("projection_join_claim_status") or "")
    if status == "FAIL" or projection_status == "failed":
        failures.append("p3_projection_join_failed")
    if status == "PASS" and projection_status != "validated":
        failures.append("p3_projection_join_missing_for_pass")
    if status == "PASS" and evidence.get("evidence_is_live") is not True:
        failures.append("p3_live_evidence_missing_for_pass")
    if projection_status == "validated" and int(evidence.get("projection_join_edge_count") or 0) < 1:
        failures.append("p3_projection_join_edge_count_missing")
    if status == "FAIL" and int(evidence.get("projection_join_edge_count") or 0) < 1:
        failures.append("p3_projection_join_edge_count_missing")
    for field, failure in (
        ("raw_private_evidence_returned", "p3_projection_join_raw_private_evidence_returned"),
        ("secret_returned", "p3_projection_join_secret_returned"),
        ("host_topology_returned", "p3_projection_join_host_topology_returned"),
        ("raw_external_ids_returned", "p3_projection_join_raw_external_ids_returned"),
    ):
        if bool(evidence.get(field)):
            failures.append(failure)
    return _dedupe(failures)


def _p3_evidence_gaps(evidence: Mapping[str, Any]) -> list[str]:
    gaps = [
        f"p3_{gap}"
        for gap in evidence.get("gaps", [])
        if isinstance(gap, str) and gap
    ]
    if (
        evidence.get("projection_join_claim_status") == "validated"
        and evidence.get("evidence_is_live") is not True
    ):
        gaps.append("p3_projection_join_evidence_not_live")
    return gaps


def _p4_replacement_current_evidence(
    *, live_evidence: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    from .runtime_readiness import build_source_to_candidate_runtime_readiness_report

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=live_evidence)
    claims = {
        str(claim.get("claim_id") or ""): claim
        for claim in report.get("claims", [])
        if isinstance(claim, Mapping)
    }
    replacement = claims.get("live.production.object_authority_replacement_current", {})
    replacement = replacement if isinstance(replacement, Mapping) else {}
    provenance = claims.get("live.evidence.provenance", {})
    provenance = provenance if isinstance(provenance, Mapping) else {}
    gaps = [
        str(gap)
        for gap in replacement.get("gaps", [])
        if isinstance(gap, str) and gap
    ]
    if replacement.get("status") != "validated":
        gaps.extend(
            str(gap)
            for gap in report.get("gaps", [])
            if isinstance(gap, str)
            and (gap.startswith("replacement_") or gap == "replacement_current_execution_unverified")
        )
    return {
        "phase": "P4",
        "schema_version": "object_authority_replacement_current_product_evidence.v1",
        "status": str(report.get("status") or ""),
        "replacement_claim_status": str(replacement.get("status") or "not_validated"),
        "prior_authority_lane": str(replacement.get("prior_authority_lane") or ""),
        "successor_authority_lane": str(replacement.get("successor_authority_lane") or ""),
        "read_after_write_status": str(replacement.get("read_after_write_status") or ""),
        "postcheck_status": str(replacement.get("postcheck_status") or ""),
        "object_count": int(replacement.get("object_count") or 0),
        "live_evidence_provided": bool(live_evidence),
        "evidence_is_live": provenance.get("is_live") is True,
        "network_used": report.get("network_used") is True,
        "evidence_collection_network_used": report.get("evidence_collection_network_used") is True,
        "production_mutation_performed": replacement.get("production_mutation_performed") is True,
        "production_ready": report.get("production_ready") is True,
        "runtime_readiness_failed": str(report.get("status") or "") == "FAIL",
        "evidence_provenance_status": str(provenance.get("status") or ""),
        "gaps": _dedupe(gaps),
    }


def _p4_evidence_failures(evidence: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if evidence.get("schema_version") != "object_authority_replacement_current_product_evidence.v1":
        failures.append("p4_schema_mismatch")
    if evidence.get("runtime_readiness_failed") is True:
        failures.append("p4_runtime_readiness_failed")
    if evidence.get("replacement_claim_status") == "failed":
        failures.append("p4_replacement_current_failed")
    if evidence.get("evidence_provenance_status") == "failed":
        failures.append("p4_evidence_provenance_failed")
    if evidence.get("replacement_claim_status") == "validated":
        if evidence.get("evidence_is_live") is not True:
            failures.append("p4_replacement_current_not_live")
        if evidence.get("production_mutation_performed") is not True:
            failures.append("p4_replacement_current_mutation_missing")
        if evidence.get("prior_authority_lane") not in {"accepted_non_current", "archive_only"}:
            failures.append("p4_prior_current_not_demoted")
        if evidence.get("successor_authority_lane") != "accepted_current":
            failures.append("p4_successor_not_current")
        if evidence.get("read_after_write_status") != "validated":
            failures.append("p4_read_after_write_missing")
        if evidence.get("postcheck_status") != "validated":
            failures.append("p4_postcheck_missing")
        if int(evidence.get("object_count") or 0) != 2:
            failures.append("p4_replacement_object_count_not_two")
    return _dedupe(failures)


def _p4_evidence_gaps(evidence: Mapping[str, Any]) -> list[str]:
    gaps = [
        f"p4_{gap}"
        for gap in (evidence.get("gaps") or [])
        if isinstance(gap, str) and gap
    ]
    if evidence.get("replacement_claim_status") == "not_validated":
        gaps.append("p4_replacement_current_execution_unverified")
    return _dedupe(gaps)


def _p6_evidence_failures(evidence: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if evidence.get("schema_version") != "object_extraction_session_project_rollup_preview.v1":
        failures.append("p6_schema_mismatch")
    status = str(evidence.get("status") or "")
    rollup_status = str(evidence.get("rollup_claim_status") or "")
    rollup_evidence_absent = (
        evidence.get("rollup_evidence_present") is False
        and rollup_status == "not_validated"
        and int(evidence.get("evidence_count") or 0) < 1
    )
    if status == "FAIL" or rollup_status == "failed":
        failures.append("p6_session_rollup_runtime_failed")
    if evidence.get("runtime_readiness_status") == "FAIL":
        failures.append("p6_runtime_readiness_failed")
    if evidence.get("evidence_provenance_status") == "failed":
        failures.append("p6_evidence_provenance_failed")
    if status == "PASS" and rollup_status and rollup_status != "validated":
        failures.append("p6_session_rollup_missing_for_pass")
    if status == "PASS" and evidence.get("evidence_is_live") is not True:
        failures.append("p6_live_evidence_missing_for_pass")
    if status == "PASS" and evidence.get("evidence_provenance_status") != "validated":
        failures.append("p6_evidence_provenance_not_validated")
    if not rollup_evidence_absent:
        if int(evidence.get("object_count") or 0) < 5:
            failures.append("p6_session_rollup_incomplete")
        if int(evidence.get("edge_count") or 0) < 6:
            failures.append("p6_session_rollup_incomplete")
        if int(evidence.get("evidence_count") or 0) < 1:
            failures.append("p6_session_rollup_incomplete")
        if evidence.get("handoff_pack_schema") != "session_project_handoff_pack.v1":
            failures.append("p6_handoff_pack_missing")
    return _dedupe(failures)


def _p6_evidence_gaps(evidence: Mapping[str, Any]) -> list[str]:
    gaps = [
        f"p6_{gap}"
        for gap in (evidence.get("gaps") or [])
        if isinstance(gap, str) and gap
    ]
    if (
        evidence.get("rollup_claim_status") == "validated"
        and evidence.get("evidence_is_live") is not True
    ):
        gaps.append("p6_session_project_rollup_evidence_not_live")
    return _dedupe(gaps)


def _p7_evidence_failures(evidence: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if evidence.get("schema_version") != "object_extraction_preference_style_preview.v1":
        failures.append("p7_schema_mismatch")
    if (
        int(evidence.get("object_count") or 0) < 2
        and not _p7_single_live_current_preference_valid(evidence)
    ):
        failures.append("p7_preference_style_objects_missing")
    if int(evidence.get("source_evidence_ref_count") or 0) < 1:
        failures.append("p7_source_evidence_missing")
    if (
        evidence.get("artifact_preference_pack_status") != "pass"
        and not _p7_collector_capability_only_gap(evidence)
    ):
        failures.append("p7_artifact_preference_pack_not_pass")
    if int(evidence.get("accepted_preference_count") or 0) < 1:
        failures.append("p7_accepted_preference_missing")
    if evidence.get("status") == "FAIL":
        failures.append("p7_preference_artifact_runtime_failed")
    if evidence.get("runtime_readiness_status") == "FAIL":
        failures.append("p7_runtime_readiness_failed")
    if evidence.get("evidence_provenance_status") == "failed":
        failures.append("p7_evidence_provenance_failed")
    return failures


def _p7_single_live_current_preference_valid(evidence: Mapping[str, Any]) -> bool:
    return (
        evidence.get("preference_claim_status") == "validated"
        and evidence.get("evidence_is_live") is True
        and int(evidence.get("object_count") or 0) == 1
        and int(evidence.get("accepted_preference_count") or 0) == 1
        and int(evidence.get("proposal_preference_count") or 0) == 0
        and evidence.get("html_route_status") == "validated"
        and evidence.get("artifact_review_check_status") == "pass"
        and evidence.get("runtime_readiness_status") != "FAIL"
    )


def _p7_collector_capability_only_gap(evidence: Mapping[str, Any]) -> bool:
    gaps = {
        gap
        for gap in evidence.get("gaps", [])
        if isinstance(gap, str) and gap
    }
    return (
        evidence.get("status") == "PASS_WITH_GAPS"
        and evidence.get("preference_claim_status") == "not_validated"
        and evidence.get("artifact_preference_pack_status") == "pass_with_gaps"
        and gaps == {"preference_artifact_collector_capability_missing"}
    )


def _p7_evidence_gaps(evidence: Mapping[str, Any]) -> list[str]:
    gaps = [
        f"p7_{gap}"
        for gap in evidence.get("gaps", [])
        if isinstance(gap, str) and gap
    ]
    if (
        evidence.get("preference_claim_status") == "validated"
        and evidence.get("evidence_is_live") is not True
    ):
        gaps.append("p7_preference_artifact_memory_evidence_not_live")
    return _dedupe(gaps)


def _p8_evidence_failures(evidence: Mapping[str, Any]) -> list[str]:
    if evidence.get("evidence_source") == "live_runtime_authority_packet":
        return _p8_live_runtime_authority_failures(evidence)
    failures: list[str] = []
    if evidence.get("schema_version") != "object_extraction_runtime_truth_preview.v1":
        failures.append("p8_schema_mismatch")
    if evidence.get("runtime_evidence_collection_plan_schema") != "source_to_candidate_runtime_evidence_collection_plan.v1":
        failures.append("p8_runtime_evidence_collection_plan_missing")
    if evidence.get("runtime_evidence_collection_plan_status") != "ready":
        failures.append("p8_runtime_evidence_collection_plan_not_ready")
    if evidence.get("runtime_authority_bounded_execution_demote_step_required") is not True:
        failures.append("p8_runtime_authority_demote_step_gate_missing")
    if evidence.get("runtime_authority_bounded_execution_required_demote_step") != (
        "demote_prior_object_to_accepted_non_current_or_archive_only"
    ):
        failures.append("p8_runtime_authority_demote_step_name_missing")
    if bool(evidence.get("runtime_evidence_collection_plan_network_used")):
        failures.append("p8_runtime_evidence_collection_plan_used_network")
    if bool(evidence.get("runtime_evidence_collection_plan_mutation_allowed")):
        failures.append("p8_runtime_evidence_collection_plan_mutation_allowed")
    if bool(evidence.get("runtime_evidence_collection_plan_production_mutation_performed")):
        failures.append("p8_runtime_evidence_collection_plan_mutated_production")
    if evidence.get("runtime_evidence_collection_plan_readiness_claim") != "plan_only_not_runtime_evidence":
        failures.append("p8_runtime_evidence_collection_plan_claims_live_evidence")
    if (
        evidence.get("runtime_evidence_packet_template_schema")
        != "source_to_candidate_runtime_evidence_packet_template.v1"
    ):
        failures.append("p8_runtime_evidence_packet_template_missing")
    if evidence.get("runtime_evidence_packet_template_status") != "template_ready":
        failures.append("p8_runtime_evidence_packet_template_not_ready")
    if bool(evidence.get("runtime_evidence_packet_template_network_used")):
        failures.append("p8_runtime_evidence_packet_template_used_network")
    if bool(evidence.get("runtime_evidence_packet_template_mutation_allowed")):
        failures.append("p8_runtime_evidence_packet_template_mutation_allowed")
    if bool(evidence.get("runtime_evidence_packet_template_production_mutation_performed")):
        failures.append("p8_runtime_evidence_packet_template_mutated_production")
    if evidence.get("runtime_evidence_packet_template_readiness_claim") != "template_only_not_runtime_evidence":
        failures.append("p8_runtime_evidence_packet_template_claims_live_evidence")
    if int(evidence.get("runtime_evidence_packet_template_required_field_count") or 0) < 1:
        failures.append("p8_runtime_evidence_packet_template_fields_missing")
    if int(evidence.get("runtime_evidence_packet_template_route_count") or 0) < 1:
        failures.append("p8_runtime_evidence_packet_template_routes_missing")
    if evidence.get("runtime_evidence_collector_packet_schema") != "source_to_candidate_runtime_evidence.v1":
        failures.append("p8_runtime_evidence_collector_missing")
    if int(evidence.get("runtime_evidence_collector_route_count") or 0) < 1:
        failures.append("p8_runtime_evidence_collector_routes_missing")
    if bool(evidence.get("runtime_evidence_collector_network_used")):
        failures.append("p8_runtime_evidence_collector_used_network")
    if bool(evidence.get("runtime_evidence_collector_production_mutation_performed")):
        failures.append("p8_runtime_evidence_collector_mutated_production")
    if evidence.get("runtime_evidence_collector_readiness_claim") != "collector_packet_not_live_evidence":
        failures.append("p8_runtime_evidence_collector_claims_live_evidence")
    if evidence.get("runtime_evidence_post_deploy_capture_packet_schema") != "source_to_candidate_runtime_evidence.v1":
        failures.append("p8_post_deploy_capture_packet_missing")
    if evidence.get("runtime_evidence_post_deploy_capture_collection_mode") != "post_deploy_read_only_smoke":
        failures.append("p8_post_deploy_capture_collection_mode_missing")
    if evidence.get("runtime_evidence_post_deploy_capture_network_used") is not True:
        failures.append("p8_post_deploy_capture_network_not_used")
    if bool(evidence.get("runtime_evidence_post_deploy_capture_production_mutation_performed")):
        failures.append("p8_post_deploy_capture_mutated_production")
    if evidence.get("runtime_evidence_post_deploy_capture_report_status") != "PASS_WITH_GAPS":
        failures.append("p8_post_deploy_capture_unexpected_report_status")
    if bool(evidence.get("runtime_evidence_post_deploy_capture_production_ready")):
        failures.append("p8_post_deploy_capture_claims_production_ready")
    if evidence.get("runtime_evidence_collector_permission_audit_schema") != (
        "permission_sensitive_runtime_audit_evidence.v1"
    ):
        failures.append("p8_permission_sensitive_audit_collector_missing")
    if int(evidence.get("runtime_evidence_collector_permission_audit_event_count") or 0) < 2:
        failures.append("p8_permission_sensitive_audit_events_missing")
    if evidence.get("runtime_evidence_collector_permission_audit_store_status") != "recorded":
        failures.append("p8_permission_sensitive_audit_store_not_recorded")
    if evidence.get("runtime_evidence_collector_agent_context_startup_schema") != (
        "agent_context_startup_runtime_evidence.v1"
    ):
        failures.append("p8_agent_context_startup_collector_missing")
    if evidence.get("runtime_evidence_collector_agent_context_startup_loaded") is not True:
        failures.append("p8_agent_context_startup_not_loaded")
    if evidence.get("runtime_evidence_collector_agent_context_startup_read_path_tool") != "brain_objects_query":
        failures.append("p8_agent_context_startup_read_path_missing")
    if int(evidence.get("runtime_evidence_collector_agent_context_startup_route_count") or 0) < 4:
        failures.append("p8_agent_context_startup_routes_missing")
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
    if evidence.get("shadow_collection_registration_schema") != (
        "source_to_candidate_runtime_shadow_collection_registration.v1"
    ):
        failures.append("p8_shadow_collection_registration_missing")
    if evidence.get("shadow_collection_registration_status") != "registration_ready":
        failures.append("p8_shadow_collection_registration_not_ready")
    if evidence.get("shadow_collection_registration_run_status") != "not_run":
        failures.append("p8_shadow_collection_registration_claims_run")
    if int(evidence.get("shadow_collection_registration_request_count") or 0) < 1:
        failures.append("p8_shadow_collection_registration_requests_missing")
    if int(evidence.get("shadow_collection_registration_route_count") or 0) < 1:
        failures.append("p8_shadow_collection_registration_routes_missing")
    if bool(evidence.get("shadow_collection_registration_network_used")):
        failures.append("p8_shadow_collection_registration_used_network")
    if bool(evidence.get("shadow_collection_registration_mutation_allowed")):
        failures.append("p8_shadow_collection_registration_mutation_allowed")
    if bool(evidence.get("shadow_collection_registration_production_mutation_performed")):
        failures.append("p8_shadow_collection_registration_mutated_production")
    if evidence.get("shadow_collection_registration_readiness_claim") != "registration_only_not_runtime_evidence":
        failures.append("p8_shadow_collection_registration_claims_live_evidence")
    runtime_evidence_count = int(evidence.get("runtime_verified_count") or 0) + int(
        evidence.get("runtime_unverified_count") or 0
    )
    if runtime_evidence_count < 1:
        failures.append("p8_runtime_evidence_classification_missing")
    if evidence.get("source_commit_matches_pr_head") is False:
        failures.append("p8_source_commit_mismatch_with_pr_head")
    if evidence.get("permission") != "allowed" or evidence.get("permission_reason") != "approved_scope_present":
        failures.append("p8_preapproved_scope_missing")
    if bool(evidence.get("authority_write_performed")):
        failures.append("p8_authority_write_performed")
    if bool(evidence.get("production_mutation_performed")):
        failures.append("p8_production_mutation_performed")
    return failures


def _p8_evidence_gaps(evidence: Mapping[str, Any]) -> list[str]:
    if evidence.get("evidence_source") == "live_runtime_authority_packet":
        return _p8_live_runtime_authority_gaps(evidence)
    gaps: list[str] = []
    source_commit_matches_pr_head = evidence.get("source_commit_matches_pr_head")
    if source_commit_matches_pr_head is not True and source_commit_matches_pr_head is not False:
        gaps.append("p8_source_commit_matches_pr_head_unverified")
    if int(evidence.get("runtime_unverified_count") or 0) > 0:
        gaps.append("p8_runtime_evidence_unverified")
    if int(evidence.get("runtime_verified_count") or 0) < 1:
        gaps.append("p8_runtime_verified_evidence_missing")
    if evidence.get("runtime_evidence_collection_plan_readiness_claim") == "plan_only_not_runtime_evidence":
        gaps.append("p8_runtime_evidence_collection_plan_not_live_evidence")
    if evidence.get("runtime_evidence_packet_template_readiness_claim") == "template_only_not_runtime_evidence":
        gaps.append("p8_runtime_evidence_packet_template_not_live_evidence")
    if evidence.get("runtime_evidence_collector_readiness_claim") == "collector_packet_not_live_evidence":
        gaps.append("p8_runtime_evidence_collector_not_live_evidence")
    if evidence.get("shadow_route_smoke_readiness_claim") == "request_only_not_live_evidence":
        gaps.append("p8_shadow_route_smoke_collection_pending")
        gaps.extend(
            f"p8_shadow_route_smoke_collection_pending:{route}"
            for route in evidence.get("shadow_route_smoke_pending_routes", [])
            if isinstance(route, str) and route
        )
    if evidence.get("shadow_collection_registration_readiness_claim") == "registration_only_not_runtime_evidence":
        gaps.append("p8_shadow_collection_run_pending")
        gaps.extend(
            f"p8_shadow_collection_run_pending:{route}"
            for route in evidence.get("shadow_collection_registration_routes", [])
            if isinstance(route, str) and route
        )
    return gaps


def _p8_live_runtime_authority_failures(evidence: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if evidence.get("schema_version") != "object_extraction_runtime_truth_preview.v1":
        failures.append("p8_schema_mismatch")
    if evidence.get("status") == "FAIL":
        failures.append("p8_runtime_authority_live_failed")
    if evidence.get("permission_audit_claim_status") == "failed":
        failures.append("p8_permission_sensitive_audit_runtime_failed")
    if evidence.get("evidence_provenance_status") == "failed":
        failures.append("p8_evidence_provenance_failed")
    if evidence.get("source_commit_matches_pr_head") is False:
        failures.append("p8_source_commit_mismatch_with_pr_head")
    if bool(evidence.get("authority_write_performed")):
        failures.append("p8_authority_write_performed")
    if bool(evidence.get("production_mutation_performed")):
        failures.append("p8_production_mutation_performed")
    return _dedupe(failures)


def _p8_live_runtime_authority_gaps(evidence: Mapping[str, Any]) -> list[str]:
    gaps = [
        f"p8_{gap}"
        for gap in evidence.get("gaps", [])
        if isinstance(gap, str) and gap
    ]
    if evidence.get("source_commit_matches_pr_head") is None:
        gaps.append("p8_source_commit_matches_pr_head_unverified")
    if (
        evidence.get("permission_audit_claim_status") == "validated"
        and evidence.get("evidence_is_live") is not True
    ):
        gaps.append("p8_runtime_authority_evidence_not_live")
    return _dedupe(gaps)


def _p9_evidence_failures(evidence: Mapping[str, Any]) -> list[str]:
    if evidence.get("evidence_source") == "live_agent_context_packet":
        return _p9_live_agent_context_failures(evidence)
    failures: list[str] = []
    if evidence.get("schema_version") != "agent_context_product_pack.v1":
        failures.append("p9_schema_mismatch")
    section_counts = evidence.get("section_counts") if isinstance(evidence.get("section_counts"), Mapping) else {}
    tool_hint_count = int(evidence.get("tool_hint_count") or 0)
    if int(section_counts.get("style_preference") or 0) < 1:
        failures.append("p9_style_preference_section_missing")
    if int(section_counts.get("active_work") or 0) < 1:
        failures.append("p9_active_work_section_missing")
    if tool_hint_count < 4:
        failures.append("p9_object_native_tool_hints_missing")
    if int(evidence.get("tool_hint_safe_target_count") or 0) < tool_hint_count:
        failures.append("p9_tool_hint_safe_targets_incomplete")
    if int(evidence.get("unsafe_tool_hint_count") or 0) > 0:
        failures.append("p9_tool_hint_safety_violations")
    if bool(evidence.get("mutation_allowed")):
        failures.append("p9_mutation_allowed")
    return failures


def _p9_evidence_gaps(evidence: Mapping[str, Any]) -> list[str]:
    if evidence.get("evidence_source") == "live_agent_context_packet":
        return _p9_live_agent_context_prefixed_gaps(evidence)
    return [
        f"p9_{gap}"
        for gap in evidence.get("gaps", [])
        if isinstance(gap, str) and gap
    ]


_P9_LIVE_GAP_ONLY_PREFIXES = (
    "live_agent_context_section_missing:",
    "agent_context_startup_section_missing:",
    "agent_context_consumer_startup_unvalidated:",
)
_P9_LIVE_GAP_ONLY_VALUES = frozenset(
    {
        "live_agent_context_product_sections_unverified",
        "live_agent_context_current_authority_missing",
        "live_agent_context_current_authority_accepted_current_missing",
        "live_agent_context_style_preference_accepted_current_missing",
        "live_agent_context_startup_unverified",
        "production_startup_read_path_unproven",
        "production_consumer_context_pack_live_unproven",
        "consumer_action_surface_runtime_policy_unproven",
        "agent_context_action_surface_runtime_interception_unvalidated",
        "agent_context_codex_host_startup_hook_unvalidated",
        "agent_context_startup_collector_capability_missing",
        "agent_context_evidence_not_live",
    }
)


def _p9_live_agent_context_failures(evidence: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if evidence.get("schema_version") != "agent_context_product_pack.v1":
        failures.append("p9_schema_mismatch")
    if evidence.get("tool_hints_claim_status") == "failed":
        failures.append("p9_agent_context_tool_hints_failed")
    if (
        evidence.get("product_sections_claim_status") == "failed"
        and _p9_live_has_blocking_gap(evidence)
    ):
        failures.append("p9_agent_context_product_sections_failed")
    if evidence.get("startup_read_path_claim_status") == "failed":
        failures.append("p9_agent_context_startup_read_path_failed")
    if evidence.get("evidence_provenance_status") == "failed":
        failures.append("p9_evidence_provenance_failed")
    if bool(evidence.get("production_mutation_performed")):
        failures.append("p9_production_mutation_performed")
    return _dedupe(failures)


def _p9_live_agent_context_prefixed_gaps(evidence: Mapping[str, Any]) -> list[str]:
    return [
        f"p9_{gap}"
        for gap in evidence.get("gaps", [])
        if isinstance(gap, str) and gap
    ]


def _p9_live_has_blocking_gap(evidence: Mapping[str, Any]) -> bool:
    return any(
        _p9_live_gap_is_blocking(gap)
        for gap in evidence.get("gaps", [])
        if isinstance(gap, str) and gap
    )


def _p9_live_gap_is_blocking(gap: str) -> bool:
    if gap in _P9_LIVE_GAP_ONLY_VALUES:
        return False
    if any(gap.startswith(prefix) for prefix in _P9_LIVE_GAP_ONLY_PREFIXES):
        return False
    return True


def _p9_tool_hint_safety_summary(tool_hints: Any) -> dict[str, Any]:
    hints = tool_hints if isinstance(tool_hints, list) else []
    safe_target_count = 0
    unsafe_count = 0
    safety_failures: list[str] = []
    for hint in hints:
        if not isinstance(hint, Mapping):
            unsafe_count += 1
            safety_failures.append("p9_tool_hint_not_mapping")
            continue
        failures = _p9_tool_hint_safety_failures(hint)
        if failures:
            unsafe_count += 1
            safety_failures.extend(failures)
        if not _p9_tool_hint_safe_target_failures(hint):
            safe_target_count += 1
    return {
        "tool_hint_safe_target_count": safe_target_count,
        "unsafe_tool_hint_count": unsafe_count,
        "tool_hint_safety_failures": _dedupe(safety_failures),
    }


def _p9_tool_hint_safety_failures(hint: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    tool_name = str(hint.get("tool") or "")
    safe_targets = _safe_string_list(hint.get("safe_targets"))
    blocked_targets = _safe_string_list(hint.get("blocked_targets"))
    blocked_by = _safe_string_list(hint.get("blocked_by"))
    if tool_name not in P9_ALLOWED_TOOL_SAFE_TARGETS:
        failures.append("p9_tool_hint_unknown_tool")
    if hint.get("suggest_allowed") is not True:
        failures.append("p9_tool_hint_suggest_not_allowed")
    if hint.get("execute_allowed") is not False:
        failures.append("p9_tool_hint_execute_allowed")
    if hint.get("production_mutation_allowed") is not False:
        failures.append("p9_tool_hint_production_mutation_allowed")
    failures.extend(_p9_tool_hint_safe_target_failures(hint))
    if tool_name == "brain_approval_board_decide" and "approved_scope_required" not in blocked_by:
        failures.append("p9_tool_hint_approved_scope_blocker_missing")
    if tool_name == "brain_source_to_candidate_runtime_readiness":
        if "sanitized_evidence_packet" not in safe_targets:
            failures.append("p9_tool_hint_sanitized_evidence_target_missing")
        if "raw_private_runtime_evidence" not in blocked_targets:
            failures.append("p9_tool_hint_raw_private_blocker_missing")
    return _dedupe(failures)


def _p9_tool_hint_safe_target_failures(hint: Mapping[str, Any]) -> list[str]:
    tool_name = str(hint.get("tool") or "")
    safe_targets = _safe_string_list(hint.get("safe_targets"))
    allowed_targets = P9_ALLOWED_TOOL_SAFE_TARGETS.get(tool_name, frozenset())
    failures: list[str] = []
    if not safe_targets:
        failures.append("p9_tool_hint_safe_targets_missing")
    if allowed_targets and any(target not in allowed_targets for target in safe_targets):
        failures.append("p9_tool_hint_safe_targets_not_allowed")
    return failures


def _safe_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "")]


def _p2_reference_corpus_evidence() -> dict[str, Any]:
    from .reference_corpus import build_reference_corpus_production_ingest_readiness_report

    report = build_reference_corpus_production_ingest_readiness_report(
        expected_source_count=65,
    )
    return {
        "phase": "P2",
        "schema_version": str(report.get("schema_version") or ""),
        "status": str(report.get("status") or ""),
        "golden_query_slice": "reference corpus freshness/source authority",
        "expected_source_count": report.get("expected_source_count"),
        "live_evidence_provided": bool(report.get("live_evidence_provided")),
        "evidence_collection_network_used": bool(report.get("evidence_collection_network_used")),
        "gaps": list(report.get("gaps") or []),
        "production_mutation_performed": bool(report.get("production_mutation_performed")),
        "network_used": bool(report.get("network_used")),
    }


def _p6_session_project_rollup_evidence(
    *, live_evidence: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    if live_evidence:
        live_summary = _p6_live_session_project_rollup_evidence(live_evidence)
        if live_summary:
            return live_summary

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
        "gaps": _dedupe(
            [
                *[gap for gap in report.get("gaps", []) if isinstance(gap, str) and gap],
                "live_multi_device_rollup_unproven",
            ]
        ),
        "production_mutation_performed": bool(report.get("production_mutation_performed")),
    }


def _p6_live_session_project_rollup_evidence(
    live_evidence: Mapping[str, Any]
) -> dict[str, Any]:
    from .runtime_readiness import build_source_to_candidate_runtime_readiness_report

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=live_evidence)
    claims = {
        str(item.get("claim_id") or ""): item
        for item in (report.get("claims") or [])
        if isinstance(item, Mapping)
    }
    claim = claims.get("live.session_project.rollup", {})
    provenance_claim = claims.get("live.evidence.provenance", {})
    rollup_present = _runtime_evidence_field_present(
        live_evidence,
        "session_project_rollup_runtime",
        "session_project_rollup_runtime_present",
    )
    rollup = live_evidence.get("session_project_rollup_runtime")
    rollup = rollup if isinstance(rollup, Mapping) else {}
    preview = rollup.get("rollup_preview") if isinstance(rollup.get("rollup_preview"), Mapping) else {}
    handoff = rollup.get("handoff_pack") if isinstance(rollup.get("handoff_pack"), Mapping) else {}
    read_after_write = (
        rollup.get("read_after_write") if isinstance(rollup.get("read_after_write"), Mapping) else {}
    )
    object_type_counts = (
        preview.get("object_type_counts") if isinstance(preview.get("object_type_counts"), Mapping) else {}
    )
    claim_gaps = [
        gap
        for gap in (claim.get("gaps") or [])
        if isinstance(gap, str) and gap
    ]
    evidence_is_live = bool(report.get("evidence_is_live"))
    claim_status = str(claim.get("status") or "not_validated")
    provenance_status = str(provenance_claim.get("status") or "not_validated")
    runtime_readiness_status = str(report.get("status") or "")
    status = _p6_session_project_rollup_product_status(
        claim_status=claim_status,
        evidence_is_live=evidence_is_live,
        provenance_status=provenance_status,
        runtime_readiness_status=runtime_readiness_status,
        gaps=claim_gaps,
    )
    evidence_count = 1 if rollup else 0
    return {
        "phase": "P6",
        "schema_version": str(
            preview.get("schema_version") or "object_extraction_session_project_rollup_preview.v1"
        ),
        "status": status,
        "golden_query_slice": "temporal repo recall",
        "runtime_readiness_schema": str(report.get("schema_version") or ""),
        "runtime_readiness_status": runtime_readiness_status,
        "rollup_claim_id": str(claim.get("claim_id") or ""),
        "rollup_claim_status": claim_status,
        "evidence_provenance_status": provenance_status,
        "live_evidence_provided": bool(report.get("live_evidence_provided")),
        "evidence_is_live": evidence_is_live,
        "production_ready": bool(report.get("production_ready")),
        "rollup_evidence_present": rollup_present,
        "object_count": _positive_int(
            preview.get("object_count"),
            default=sum(_positive_int(count) for count in object_type_counts.values()),
        ),
        "edge_count": _positive_int(preview.get("edge_count")),
        "evidence_count": evidence_count,
        "handoff_pack_schema": str(handoff.get("schema_version") or ""),
        "device_count": _positive_int(claim.get("device_count")),
        "visible_session_count": _positive_int(claim.get("visible_session_count")),
        "all_device_session_count": _positive_int(claim.get("all_device_session_count")),
        "read_after_write_status": str(claim.get("read_after_write_status") or read_after_write.get("status") or ""),
        "raw_return_capability": str(claim.get("raw_return_capability") or handoff.get("raw_return_capability") or ""),
        "gaps": claim_gaps,
        "production_mutation_performed": bool(
            claim.get("production_mutation_performed")
            or (
                rollup_present
                and (
                    live_evidence.get("production_mutation_performed") is True
                    or live_evidence.get("mutation_performed") is True
                )
            )
        ),
    }


def _p6_session_project_rollup_product_status(
    *,
    claim_status: str,
    evidence_is_live: bool,
    provenance_status: str,
    runtime_readiness_status: str,
    gaps: list[str],
) -> str:
    if claim_status == "failed" or provenance_status == "failed" or runtime_readiness_status == "FAIL":
        return "FAIL"
    if (
        claim_status == "validated"
        and evidence_is_live
        and provenance_status == "validated"
        and not gaps
    ):
        return "PASS"
    return "PASS_WITH_GAPS"


def _runtime_evidence_field_present(
    evidence: Mapping[str, Any],
    field_name: str,
    marker_name: str,
) -> bool:
    marker = evidence.get(marker_name)
    if isinstance(marker, bool):
        return marker
    return field_name in evidence


def _p7_preference_artifact_evidence(
    *, live_evidence: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    if live_evidence:
        live_summary = _p7_live_preference_artifact_evidence(live_evidence)
        if live_summary:
            return live_summary

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
        "gaps": _dedupe(
            [
                *[gap for gap in report.get("gaps", []) if isinstance(gap, str) and gap],
                "accepted_preference_context_pack_live_unproven",
                "html_artifact_review_live_unproven",
            ]
        ),
        "production_mutation_performed": bool(report.get("production_mutation_performed")),
    }


def _p7_live_preference_artifact_evidence(live_evidence: Mapping[str, Any]) -> dict[str, Any]:
    from .runtime_readiness import build_source_to_candidate_runtime_readiness_report

    report = build_source_to_candidate_runtime_readiness_report(live_evidence=live_evidence)
    claims = {
        str(item.get("claim_id") or ""): item
        for item in (report.get("claims") or [])
        if isinstance(item, Mapping)
    }
    claim = claims.get("live.preference_artifact.memory", {})
    provenance_claim = claims.get("live.evidence.provenance", {})
    preference = live_evidence.get("preference_artifact_memory")
    preference = preference if isinstance(preference, Mapping) else {}
    if not preference:
        return {}
    if str(preference.get("evidence_class") or "") != "runtime_preference_artifact_memory":
        return {}
    preference_present = True
    pack = (
        preference.get("preference_object_pack")
        if isinstance(preference.get("preference_object_pack"), Mapping)
        else {}
    )
    artifact_check = (
        preference.get("artifact_review_check")
        if isinstance(preference.get("artifact_review_check"), Mapping)
        else {}
    )
    claim_gaps = [
        gap
        for gap in (claim.get("gaps") or [])
        if isinstance(gap, str) and gap
    ]
    evidence_is_live = bool(report.get("evidence_is_live"))
    claim_status = str(claim.get("status") or "not_validated")
    provenance_status = str(provenance_claim.get("status") or "not_validated")
    runtime_readiness_status = str(report.get("status") or "")
    status = _p7_preference_artifact_product_status(
        claim_status=claim_status,
        evidence_is_live=evidence_is_live,
        provenance_status=provenance_status,
        runtime_readiness_status=runtime_readiness_status,
        gaps=claim_gaps,
    )
    return {
        "phase": "P7",
        "schema_version": "object_extraction_preference_style_preview.v1",
        "status": status,
        "golden_query_slice": "code style drift",
        "runtime_readiness_schema": str(report.get("schema_version") or ""),
        "runtime_readiness_status": runtime_readiness_status,
        "preference_claim_id": str(claim.get("claim_id") or ""),
        "preference_claim_status": claim_status,
        "evidence_provenance_status": provenance_status,
        "live_evidence_provided": bool(report.get("live_evidence_provided")),
        "evidence_is_live": evidence_is_live,
        "production_ready": bool(report.get("production_ready")),
        "preference_evidence_present": preference_present,
        "object_count": _positive_int(
            pack.get("accepted_preference_count"),
            default=0,
        )
        + _positive_int(pack.get("proposal_preference_count"), default=0),
        "source_evidence_ref_count": 1 if preference else 0,
        "artifact_preference_pack_status": (
            "pass" if claim_status == "validated" and not claim_gaps else "pass_with_gaps"
        ),
        "accepted_preference_count": _positive_int(
            claim.get("accepted_preference_count"),
            default=_positive_int(pack.get("accepted_preference_count")),
        ),
        "proposal_preference_count": _positive_int(
            claim.get("proposal_preference_count"),
            default=_positive_int(pack.get("proposal_preference_count")),
        ),
        "html_route_status": str(claim.get("html_route_status") or ""),
        "agent_context_object_count": _positive_int(claim.get("agent_context_object_count")),
        "artifact_review_check_status": str(
            claim.get("artifact_review_check_status") or artifact_check.get("status") or ""
        ),
        "gaps": claim_gaps,
        "production_mutation_performed": bool(
            claim.get("production_mutation_performed")
            or (
                preference_present
                and (
                    live_evidence.get("production_mutation_performed") is True
                    or live_evidence.get("mutation_performed") is True
                )
            )
        ),
    }


def _p7_preference_artifact_product_status(
    *,
    claim_status: str,
    evidence_is_live: bool,
    provenance_status: str,
    runtime_readiness_status: str,
    gaps: list[str],
) -> str:
    if claim_status == "failed" or provenance_status == "failed" or runtime_readiness_status == "FAIL":
        return "FAIL"
    if (
        claim_status == "validated"
        and evidence_is_live
        and provenance_status == "validated"
        and not gaps
    ):
        return "PASS"
    return "PASS_WITH_GAPS"


def _p8_runtime_authority_evidence(
    *, live_evidence: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    if live_evidence:
        live_summary = _p8_live_runtime_authority_evidence(live_evidence)
        if live_summary:
            return live_summary

    from .extraction_pipeline import run_runtime_truth_extraction_preview
    from .runtime_readiness import (
        AGENT_CONTEXT_STARTUP_RUNTIME_SCHEMA,
        PERMISSION_SENSITIVE_AUDIT_RUNTIME_SCHEMA,
        REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES,
        build_source_to_candidate_runtime_collected_shadow_evidence_packet,
        build_source_to_candidate_runtime_evidence_collection_plan,
        build_source_to_candidate_runtime_evidence_packet_template,
        build_source_to_candidate_runtime_post_deploy_capture_packet,
        build_source_to_candidate_runtime_post_deploy_capture_readiness_report,
    )

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
    packet_template = build_source_to_candidate_runtime_evidence_packet_template(
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
    shadow_registration = collection_plan.get("shadow_collection_registration")
    shadow_registration = shadow_registration if isinstance(shadow_registration, Mapping) else {}
    collector_packet = build_source_to_candidate_runtime_collected_shadow_evidence_packet(
        expected_commit=expected_commit,
        repository="pureliture/neurons",
        branch="codex/knowledge-object-review-flow-roadmap",
        consumer="codex",
        route_runner=_p8_branch_local_runtime_route_smoke,
    )
    collector = collector_packet.get("collector") if isinstance(collector_packet.get("collector"), Mapping) else {}
    post_deploy_capture = {
        "tool_names": [
            "brain_objects_query",
            "brain_source_to_candidate_graph",
            "brain_candidate_review_edit",
            "brain_approval_board_decide",
            "brain_source_to_candidate_runtime_readiness",
        ],
        "brain_objects_query_smokes": [
            _p8_branch_local_runtime_route_smoke(route)
            for route in REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES
        ],
        "deployed_identity": {
            "contains_expected_commit": False,
            "identity_source": "redacted_post_deploy_capture_without_image_identity",
        },
        "collection": {
            "collection_mode": "post_deploy_read_only_smoke",
            "network_used": True,
            "mutation_scope": "none",
        },
        "production_mutation_performed": False,
    }
    post_deploy_packet = build_source_to_candidate_runtime_post_deploy_capture_packet(
        captured_evidence=post_deploy_capture,
    )
    post_deploy_report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=post_deploy_capture,
        expected_commit=expected_commit,
    )
    post_deploy_provenance = (
        post_deploy_packet.get("evidence_provenance")
        if isinstance(post_deploy_packet.get("evidence_provenance"), Mapping)
        else {}
    )
    collector_review_loop = (
        collector_packet.get("source_to_candidate_review_loop")
        if isinstance(collector_packet.get("source_to_candidate_review_loop"), Mapping)
        else {}
    )
    collector_review_graph = (
        collector_review_loop.get("source_to_candidate_graph")
        if isinstance(collector_review_loop.get("source_to_candidate_graph"), Mapping)
        else {}
    )
    collector_review_edit = (
        collector_review_loop.get("candidate_review_edit")
        if isinstance(collector_review_loop.get("candidate_review_edit"), Mapping)
        else {}
    )
    collector_review_decision = (
        collector_review_loop.get("approval_board_decision")
        if isinstance(collector_review_loop.get("approval_board_decision"), Mapping)
        else {}
    )
    collector_session_rollup = (
        collector_packet.get("session_project_rollup_runtime")
        if isinstance(collector_packet.get("session_project_rollup_runtime"), Mapping)
        else {}
    )
    collector_session_preview = (
        collector_session_rollup.get("rollup_preview")
        if isinstance(collector_session_rollup.get("rollup_preview"), Mapping)
        else {}
    )
    collector_session_read_after_write = (
        collector_session_rollup.get("read_after_write")
        if isinstance(collector_session_rollup.get("read_after_write"), Mapping)
        else {}
    )
    collector_preference_memory = (
        collector_packet.get("preference_artifact_memory")
        if isinstance(collector_packet.get("preference_artifact_memory"), Mapping)
        else {}
    )
    collector_preference_pack = (
        collector_preference_memory.get("preference_object_pack")
        if isinstance(collector_preference_memory.get("preference_object_pack"), Mapping)
        else {}
    )
    collector_preference_html_smoke = (
        collector_preference_memory.get("html_visualization_route_smoke")
        if isinstance(collector_preference_memory.get("html_visualization_route_smoke"), Mapping)
        else {}
    )
    collector_preference_artifact_check = (
        collector_preference_memory.get("artifact_review_check")
        if isinstance(collector_preference_memory.get("artifact_review_check"), Mapping)
        else {}
    )
    collector_permission_audit = (
        collector_packet.get("permission_sensitive_audit")
        if isinstance(collector_packet.get("permission_sensitive_audit"), Mapping)
        else {}
    )
    collector_permission_audit_store = (
        collector_permission_audit.get("audit_store")
        if isinstance(collector_permission_audit.get("audit_store"), Mapping)
        else {}
    )
    collector_agent_context_startup = (
        collector_packet.get("agent_context_startup_runtime")
        if isinstance(collector_packet.get("agent_context_startup_runtime"), Mapping)
        else {}
    )
    collector_startup_context = (
        collector_agent_context_startup.get("startup_context")
        if isinstance(collector_agent_context_startup.get("startup_context"), Mapping)
        else {}
    )
    collector_startup_read_path = (
        collector_agent_context_startup.get("read_path_smoke")
        if isinstance(collector_agent_context_startup.get("read_path_smoke"), Mapping)
        else {}
    )
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
        "runtime_authority_bounded_execution_required_demote_step": (
            "demote_prior_object_to_accepted_non_current_or_archive_only"
        ),
        "runtime_authority_bounded_execution_demote_step_required": True,
        "runtime_evidence_collection_plan_schema": str(collection_plan.get("schema_version") or ""),
        "runtime_evidence_collection_plan_status": str(collection_plan.get("status") or ""),
        "runtime_evidence_collection_plan_required_step_count": len(collection_plan.get("required_steps") or []),
        "runtime_evidence_collection_plan_network_used": bool(collection_plan.get("network_used")),
        "runtime_evidence_collection_plan_mutation_allowed": bool(collection_plan.get("mutation_allowed")),
        "runtime_evidence_collection_plan_production_mutation_performed": bool(
            collection_plan.get("production_mutation_performed")
        ),
        "runtime_evidence_collection_plan_readiness_claim": str(collection_plan.get("readiness_claim") or ""),
        "runtime_evidence_packet_template_schema": str(packet_template.get("schema_version") or ""),
        "runtime_evidence_packet_template_status": str(packet_template.get("status") or ""),
        "runtime_evidence_packet_template_required_field_count": len(
            packet_template.get("required_packet_fields") or []
        ),
        "runtime_evidence_packet_template_route_count": len(packet_template.get("required_routes") or []),
        "runtime_evidence_packet_template_network_used": bool(packet_template.get("network_used")),
        "runtime_evidence_packet_template_mutation_allowed": bool(packet_template.get("mutation_allowed")),
        "runtime_evidence_packet_template_production_mutation_performed": bool(
            packet_template.get("production_mutation_performed")
        ),
        "runtime_evidence_packet_template_readiness_claim": str(packet_template.get("readiness_claim") or ""),
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
        "shadow_collection_registration_schema": str(shadow_registration.get("schema_version") or ""),
        "shadow_collection_registration_status": str(shadow_registration.get("status") or ""),
        "shadow_collection_registration_run_status": str(shadow_registration.get("run_status") or ""),
        "shadow_collection_registration_request_count": len(shadow_registration.get("request_ids") or []),
        "shadow_collection_registration_route_count": len(shadow_registration.get("routes") or []),
        "shadow_collection_registration_routes": [
            str(route) for route in shadow_registration.get("routes", []) if isinstance(route, str) and route
        ],
        "shadow_collection_registration_network_used": bool(shadow_registration.get("network_used")),
        "shadow_collection_registration_mutation_allowed": bool(
            shadow_registration.get("mutation_allowed")
        ),
        "shadow_collection_registration_production_mutation_performed": bool(
            shadow_registration.get("production_mutation_performed")
        ),
        "shadow_collection_registration_readiness_claim": str(
            shadow_registration.get("readiness_claim") or ""
        ),
        "runtime_evidence_collector_packet_schema": str(collector_packet.get("schema_version") or ""),
        "runtime_evidence_collector_route_count": len(collector_packet.get("brain_objects_query_smokes") or []),
        "runtime_evidence_collector_network_used": bool(
            (collector_packet.get("evidence_provenance") or {}).get("network_used")
            if isinstance(collector_packet.get("evidence_provenance"), Mapping)
            else False
        ),
        "runtime_evidence_collector_production_mutation_performed": bool(
            collector_packet.get("production_mutation_performed")
        ),
        "runtime_evidence_collector_readiness_claim": str(collector.get("readiness_claim") or ""),
        "runtime_evidence_post_deploy_capture_packet_schema": str(post_deploy_packet.get("schema_version") or ""),
        "runtime_evidence_post_deploy_capture_collection_mode": str(
            post_deploy_provenance.get("collection_mode") or ""
        ),
        "runtime_evidence_post_deploy_capture_network_used": bool(post_deploy_provenance.get("network_used")),
        "runtime_evidence_post_deploy_capture_production_mutation_performed": bool(
            post_deploy_packet.get("production_mutation_performed")
        ),
        "runtime_evidence_post_deploy_capture_report_status": str(post_deploy_report.get("status") or ""),
        "runtime_evidence_post_deploy_capture_production_ready": bool(
            post_deploy_report.get("production_ready")
        ),
        "runtime_evidence_collector_review_loop_schema": str(collector_review_loop.get("schema_version") or ""),
        "runtime_evidence_collector_review_loop_candidate_count": int(
            collector_review_graph.get("candidate_count") or 0
        ),
        "runtime_evidence_collector_review_loop_edited_count": int(
            collector_review_edit.get("edited_candidate_count") or 0
        ),
        "runtime_evidence_collector_review_loop_decision_count": int(
            collector_review_decision.get("decision_count") or 0
        ),
        "runtime_evidence_collector_review_loop_authority_scope": str(
            collector_review_decision.get("authority_write_scope") or ""
        ),
        "runtime_evidence_collector_session_rollup_schema": str(collector_session_rollup.get("schema_version") or ""),
        "runtime_evidence_collector_session_rollup_device_count": int(
            collector_session_preview.get("device_count") or 0
        ),
        "runtime_evidence_collector_session_rollup_visible_session_count": int(
            collector_session_preview.get("visible_session_count") or 0
        ),
        "runtime_evidence_collector_session_rollup_read_after_write_status": str(
            collector_session_read_after_write.get("status") or ""
        ),
        "runtime_evidence_collector_preference_artifact_schema": str(
            collector_preference_memory.get("schema_version") or ""
        ),
        "runtime_evidence_collector_preference_accepted_count": int(
            collector_preference_pack.get("accepted_preference_count") or 0
        ),
        "runtime_evidence_collector_preference_proposal_count": int(
            collector_preference_pack.get("proposal_preference_count") or 0
        ),
        "runtime_evidence_collector_preference_html_route": str(
            collector_preference_html_smoke.get("route") or ""
        ),
        "runtime_evidence_collector_preference_artifact_check_status": str(
            collector_preference_artifact_check.get("status") or ""
        ),
        "runtime_evidence_collector_permission_audit_schema": str(
            collector_permission_audit.get("schema_version") or ""
        ),
        "runtime_evidence_collector_permission_audit_expected_schema": PERMISSION_SENSITIVE_AUDIT_RUNTIME_SCHEMA,
        "runtime_evidence_collector_permission_audit_event_count": len(
            collector_permission_audit.get("audit_events") or []
        ),
        "runtime_evidence_collector_permission_audit_store_status": str(
            collector_permission_audit_store.get("status") or ""
        ),
        "runtime_evidence_collector_agent_context_startup_schema": str(
            collector_agent_context_startup.get("schema_version") or ""
        ),
        "runtime_evidence_collector_agent_context_startup_expected_schema": AGENT_CONTEXT_STARTUP_RUNTIME_SCHEMA,
        "runtime_evidence_collector_agent_context_startup_loaded": (
            collector_startup_context.get("loaded_on_startup") is True
        ),
        "runtime_evidence_collector_agent_context_startup_read_path_tool": str(
            collector_startup_read_path.get("tool") or ""
        ),
        "runtime_evidence_collector_agent_context_startup_route_count": len(
            collector_startup_read_path.get("routes_checked") or []
        ),
        "gaps": list(preview.get("gaps") or []),
        "production_mutation_performed": bool(report.get("production_mutation_performed")),
    }


def _p8_live_runtime_authority_evidence(live_evidence: Mapping[str, Any]) -> dict[str, Any]:
    from .runtime_readiness import build_source_to_candidate_runtime_readiness_report

    identity = live_evidence.get("deployed_identity")
    identity = identity if isinstance(identity, Mapping) else {}
    audit = live_evidence.get("permission_sensitive_audit")
    audit = audit if isinstance(audit, Mapping) else {}
    desired_state = live_evidence.get("gitops_desired_state")
    desired_state = desired_state if isinstance(desired_state, Mapping) else {}
    if not identity and not audit and not desired_state:
        return {}

    expected_commit = str(live_evidence.get("expected_commit") or "")
    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=live_evidence,
        expected_commit=expected_commit,
    )
    claims = {
        str(item.get("claim_id") or ""): item
        for item in (report.get("claims") or [])
        if isinstance(item, Mapping)
    }
    permission_claim = claims.get("live.production.permission_sensitive_audit", {})
    desired_state_claim = claims.get("ops.gitops_desired_state.includes_expected_commit", {})
    identity_claim = claims.get("live.deployed_identity.includes_expected_commit", {})
    provenance_claim = claims.get("live.evidence.provenance", {})
    p8_claims = [permission_claim, desired_state_claim, identity_claim]
    claim_gaps = _dedupe(
        [
            *(
                gap
                for claim in p8_claims
                for gap in (claim.get("gaps") or [])
                if isinstance(gap, str) and gap
            ),
            *(
                gap
                for gap in (provenance_claim.get("gaps") or [])
                if isinstance(gap, str)
                and gap
                and gap not in _P8_PROVENANCE_AGGREGATE_MUTATION_GAPS
            ),
        ]
    )
    evidence_is_live = bool(report.get("evidence_is_live"))
    permission_status = str(permission_claim.get("status") or "not_validated")
    desired_state_status = str(desired_state_claim.get("status") or "not_validated")
    identity_status = str(identity_claim.get("status") or "not_validated")
    provenance_status = _p8_scoped_provenance_status(provenance_claim)
    authority_write_performed = permission_claim.get("production_mutation_performed") is True
    desired_state_mutation_performed = desired_state_claim.get("production_mutation_performed") is True
    production_mutation_performed = bool(
        authority_write_performed
        or permission_claim.get("production_mutation_performed") is True
        or desired_state_mutation_performed
    )
    status = _p8_runtime_authority_product_status(
        permission_status=permission_status,
        desired_state_status=desired_state_status,
        identity_status=identity_status,
        provenance_status=provenance_status,
        evidence_is_live=evidence_is_live,
        production_mutation_performed=production_mutation_performed,
        gaps=claim_gaps,
    )
    source_commit_matches = _p8_source_commit_match(identity=identity, identity_status=identity_status)
    return {
        "phase": "P8",
        "schema_version": "object_extraction_runtime_truth_preview.v1",
        "evidence_source": "live_runtime_authority_packet",
        "status": status,
        "golden_query_slice": "pr merge and deploy truth",
        "runtime_readiness_schema": str(report.get("schema_version") or ""),
        "runtime_readiness_status": str(report.get("status") or ""),
        "live_evidence_provided": bool(report.get("live_evidence_provided")),
        "evidence_is_live": evidence_is_live,
        "production_ready": False,
        "permission_audit_claim_status": permission_status,
        "permission_audit_event_count": int(permission_claim.get("event_count") or 0),
        "permission_audit_store_status": str(permission_claim.get("audit_store_status") or ""),
        "gitops_desired_state_claim_status": desired_state_status,
        "gitops_desired_state_matches_expected_commit": (
            desired_state_claim.get("images_include_expected_commit") is True
        ),
        "gitops_desired_state_source": str(desired_state_claim.get("desired_state_source") or ""),
        "gitops_desired_state_target_revision": str(desired_state_claim.get("target_revision") or ""),
        "gitops_desired_state_mutation_performed": desired_state_mutation_performed,
        "deployed_identity_claim_status": identity_status,
        "source_commit_matches_pr_head": source_commit_matches,
        "deployed_identity_source": str(identity_claim.get("identity_source") or ""),
        "evidence_provenance_status": provenance_status,
        "evidence_collection_mode": str(provenance_claim.get("collection_mode") or ""),
        "evidence_collection_network_used": provenance_claim.get("network_used_for_evidence") is True,
        "evidence_mutation_scope": str(provenance_claim.get("mutation_scope") or ""),
        "evidence_redaction_check": str(provenance_claim.get("redaction_check") or ""),
        "authority_write_performed": authority_write_performed,
        "production_mutation_performed": production_mutation_performed,
        "gaps": claim_gaps,
    }


def _p8_source_commit_match(*, identity: Mapping[str, Any], identity_status: str) -> bool | None:
    if identity_status == "validated":
        return True
    if identity.get("source_commit_matches_pr_head") is False:
        return False
    if identity.get("expected_commit_mismatch") is True or identity.get("source_commit_mismatch") is True:
        return False
    return None


_P8_PROVENANCE_AGGREGATE_MUTATION_GAPS = frozenset(
    {
        "live_evidence_provenance_mutation_scope_mismatch",
        "live_evidence_provenance_unexpected_mutation_scope",
    }
)


def _p8_scoped_provenance_status(provenance_claim: Mapping[str, Any]) -> str:
    status = str(provenance_claim.get("status") or "not_validated")
    if status != "failed":
        return status
    blocking_gaps = [
        gap
        for gap in provenance_claim.get("gaps", [])
        if isinstance(gap, str) and gap not in _P8_PROVENANCE_AGGREGATE_MUTATION_GAPS
    ]
    if blocking_gaps:
        return "failed"
    return "validated" if provenance_claim.get("is_live") is True else "not_validated"


def _p8_runtime_authority_product_status(
    *,
    permission_status: str,
    desired_state_status: str,
    identity_status: str,
    provenance_status: str,
    evidence_is_live: bool,
    production_mutation_performed: bool,
    gaps: list[str],
) -> str:
    if (
        permission_status == "failed"
        or desired_state_status == "failed"
        or identity_status == "failed"
        or provenance_status == "failed"
        or production_mutation_performed
    ):
        return "FAIL"
    if (
        permission_status == "validated"
        and desired_state_status == "validated"
        and identity_status == "validated"
        and provenance_status == "validated"
        and evidence_is_live
        and not gaps
    ):
        return "PASS"
    return "PASS_WITH_GAPS"


def _p8_branch_local_runtime_route_smoke(route: str) -> dict[str, Any]:
    gaps = ["runtime_evidence_unverified"] if route == "deployment_runtime_truth" else []
    return {
        "schema_version": "brain_objects_query.v1",
        "route": route,
        "production_mutation_performed": False,
        "object_pack": {
            "schema_version": "object_pack.v1",
            "route": route,
            "objects": [{"object_id": f"ko:RuntimeRoute:{route}", "object_type": "RuntimeTruth"}],
            "edges": [],
            "evidence": [],
            "lanes": {"candidate": [{"object_id": f"ko:RuntimeRoute:{route}", "object_type": "RuntimeTruth"}]},
            "recommended_actions": [{"object_id": f"ko:RuntimeRoute:{route}", "action": "request_evidence"}],
            "gaps": gaps,
        },
    }


def _p9_agent_context_evidence(
    *,
    preference_preview: Mapping[str, Any],
    live_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if live_evidence and (
        isinstance(live_evidence.get("agent_context_product"), Mapping)
        or isinstance(live_evidence.get("agent_context_startup_runtime"), Mapping)
    ):
        return _p9_live_agent_context_evidence(live_evidence)

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
    tool_hint_safety = _p9_tool_hint_safety_summary(product.get("tool_hints"))
    return {
        "phase": "P9",
        "schema_version": str(product.get("schema_version") or ""),
        "status": "pass_with_gaps" if product.get("degraded_mode", {}).get("active") else "pass",
        "consumer": str(product.get("consumer") or ""),
        "section_counts": section_counts,
        "tool_hint_count": len(product.get("tool_hints") or []),
        "tool_hint_safe_target_count": tool_hint_safety["tool_hint_safe_target_count"],
        "unsafe_tool_hint_count": tool_hint_safety["unsafe_tool_hint_count"],
        "tool_hint_safety_failures": tool_hint_safety["tool_hint_safety_failures"],
        "action_hint_count": len(product.get("action_hints") or []),
        "mutation_allowed": bool(product.get("surface_policy", {}).get("mutation_allowed")),
        "degraded_mode_active": bool(product.get("degraded_mode", {}).get("active")),
        "gaps": _dedupe(
            [
                *[
                    gap
                    for gap in product.get("degraded_mode", {}).get("gaps", [])
                    if isinstance(gap, str) and gap
                ],
                "production_consumer_context_pack_live_unproven",
                "consumer_action_surface_runtime_policy_unproven",
            ]
        ),
        "production_mutation_performed": False,
    }


def _p9_live_agent_context_evidence(live_evidence: Mapping[str, Any]) -> dict[str, Any]:
    from .runtime_readiness import build_source_to_candidate_runtime_readiness_report

    product = live_evidence.get("agent_context_product")
    product = product if isinstance(product, Mapping) else {}
    report = build_source_to_candidate_runtime_readiness_report(
        live_evidence=live_evidence,
        expected_commit=str(live_evidence.get("expected_commit") or ""),
    )
    claims = {
        str(item.get("claim_id") or ""): item
        for item in (report.get("claims") or [])
        if isinstance(item, Mapping)
    }
    tool_hints = claims.get("live.agent_context.tool_hints", {})
    product_sections = claims.get("live.agent_context.product_sections", {})
    startup = claims.get("live.agent_context.startup_read_path", {})
    provenance = claims.get("live.evidence.provenance", {})
    section_counts = _p9_live_section_counts(live_evidence)
    tool_hint_safety = _p9_tool_hint_safety_summary(product.get("tool_hints"))
    claim_gaps = _dedupe(
        gap
        for claim in (tool_hints, product_sections, startup, provenance)
        for gap in claim.get("gaps", [])
        if isinstance(gap, str) and gap
    )
    evidence_is_live = bool(report.get("evidence_is_live"))
    status = _p9_live_agent_context_product_status(
        tool_hints_status=str(tool_hints.get("status") or "not_validated"),
        product_sections_status=str(product_sections.get("status") or "not_validated"),
        startup_status=str(startup.get("status") or "not_validated"),
        provenance_status=str(provenance.get("status") or "not_validated"),
        evidence_is_live=evidence_is_live,
        production_mutation_performed=bool(startup.get("production_mutation_performed")),
        gaps=claim_gaps,
    )
    return {
        "phase": "P9",
        "schema_version": "agent_context_product_pack.v1",
        "evidence_source": "live_agent_context_packet",
        "status": status,
        "consumer": str(product_sections.get("consumer") or ""),
        "section_counts": section_counts,
        "tool_hint_count": len(product.get("tool_hints") or []),
        "tool_hint_safe_target_count": tool_hint_safety["tool_hint_safe_target_count"],
        "unsafe_tool_hint_count": tool_hint_safety["unsafe_tool_hint_count"],
        "tool_hint_safety_failures": [
            f"p9_{gap}" for gap in (tool_hints.get("unsafe_tool_hints") or []) if isinstance(gap, str) and gap
        ],
        "action_hint_count": len(product.get("action_hints") or []),
        "mutation_allowed": product_sections.get("mutation_allowed") is True,
        "runtime_readiness_schema": str(report.get("schema_version") or ""),
        "runtime_readiness_status": str(report.get("status") or ""),
        "live_evidence_provided": bool(report.get("live_evidence_provided")),
        "evidence_is_live": evidence_is_live,
        "production_ready": False,
        "tool_hints_claim_status": str(tool_hints.get("status") or "not_validated"),
        "product_sections_claim_status": str(product_sections.get("status") or "not_validated"),
        "startup_read_path_claim_status": str(startup.get("status") or "not_validated"),
        "bounded_adapter_status": str(
            startup.get("bounded_adapter_status") or "not_validated"
        ),
        "host_startup_hook_status": str(
            startup.get("host_startup_hook_status") or "not_validated"
        ),
        "evidence_provenance_status": str(provenance.get("status") or "not_validated"),
        "startup_loaded": startup.get("startup_loaded") is True,
        "read_path_tool": str(startup.get("read_path_tool") or ""),
        "routes_checked": list(startup.get("routes_checked") or []),
        "degraded_mode_active": bool(claim_gaps or not evidence_is_live),
        "gaps": _p9_live_agent_context_gaps(
            claim_gaps=claim_gaps,
            evidence_is_live=evidence_is_live,
            product_sections_status=str(product_sections.get("status") or "not_validated"),
            startup_status=str(startup.get("status") or "not_validated"),
        ),
        "production_mutation_performed": bool(startup.get("production_mutation_performed")),
    }


def _p9_live_section_counts(live_evidence: Mapping[str, Any]) -> dict[str, int]:
    product = live_evidence.get("agent_context_product")
    product = product if isinstance(product, Mapping) else {}
    sections = product.get("sections") if isinstance(product.get("sections"), Mapping) else {}
    counts: dict[str, int] = {}
    for section in ("current_authority", "style_preference", "active_work", "required_verification"):
        value = sections.get(section)
        if isinstance(value, Mapping):
            counts[section] = int(value.get("object_count") or 0)
    startup = live_evidence.get("agent_context_startup_runtime")
    startup = startup if isinstance(startup, Mapping) else {}
    context = startup.get("startup_context") if isinstance(startup.get("startup_context"), Mapping) else {}
    startup_counts = context.get("section_counts") if isinstance(context.get("section_counts"), Mapping) else {}
    for section in ("current_authority", "style_preference", "active_work", "required_verification"):
        if section not in counts:
            counts[section] = int(startup_counts.get(section) or 0)
    return counts


def _p9_live_agent_context_gaps(
    *,
    claim_gaps: list[str],
    evidence_is_live: bool,
    product_sections_status: str,
    startup_status: str,
) -> list[str]:
    gaps = list(claim_gaps)
    if product_sections_status != "validated":
        gaps.append("production_consumer_context_pack_live_unproven")
    if startup_status != "validated":
        gaps.append("consumer_action_surface_runtime_policy_unproven")
    if not evidence_is_live:
        gaps.append("agent_context_evidence_not_live")
    return _dedupe(gaps)


def _p9_live_agent_context_product_status(
    *,
    tool_hints_status: str,
    product_sections_status: str,
    startup_status: str,
    provenance_status: str,
    evidence_is_live: bool,
    production_mutation_performed: bool,
    gaps: list[str],
) -> str:
    if (
        tool_hints_status == "failed"
        or provenance_status == "failed"
        or production_mutation_performed
        or any(_p9_live_gap_is_blocking(gap) for gap in gaps)
    ):
        return "FAIL"
    if (
        tool_hints_status == "validated"
        and product_sections_status == "validated"
        and startup_status == "validated"
        and provenance_status == "validated"
        and evidence_is_live
        and not gaps
    ):
        return "PASS"
    return "PASS_WITH_GAPS"


def _golden_slice(report: Mapping[str, Any]) -> str:
    evaluator = report.get("evaluator_report") if isinstance(report.get("evaluator_report"), Mapping) else {}
    return str(evaluator.get("golden_query_slice") or "")


def _activation_phase_state(phase: str, quality_result: str) -> str:
    if quality_result == "FAIL":
        return "blocked"
    if phase == "P5":
        return "local_validated" if quality_result == "PASS" else "in_progress"
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
        return (
            "advance_next_phase_with_gap_visible"
            if state == "local_validated"
            else "keep_continuous_quality_gate_active_until_release_gate_green"
        )
    if any("production" in gap or "live" in gap for gap in gaps):
        return "collect_bounded_runtime_or_production_evidence"
    if state == "local_validated":
        return "advance_next_phase_with_gap_visible"
    return "complete_local_phase_slice"


def _apply_product_evidence_to_phase_progress(
    phase_progress: list[dict[str, Any]],
    *,
    product_evidence_result: Mapping[str, Any],
) -> list[dict[str, Any]]:
    checks = {
        str(item.get("phase") or ""): item
        for item in product_evidence_result.get("checks", [])
        if isinstance(item, Mapping)
    }
    removable_gaps_by_phase = {
        "P3": {"live_graph_qdrant_projection_join_unproven"},
        "P4": {
            "production_authority_pilot_not_executed",
            "production_authority_write_evidence_missing",
        },
        "P6": {"live_multi_device_rollup_unproven"},
        "P7": {
            "accepted_preference_context_pack_live_unproven",
            "html_artifact_review_live_unproven",
        },
        "P8": {
            "live_runtime_rollout_identity_unproven",
            "production_permission_audit_live_unproven",
        },
        "P9": {
            "production_consumer_context_pack_live_unproven",
            "consumer_action_surface_runtime_policy_unproven",
        },
    }
    adjusted: list[dict[str, Any]] = []
    for item in phase_progress:
        phase = str(item.get("phase") or "")
        check = checks.get(phase, {})
        removable = removable_gaps_by_phase.get(phase, set())
        if check.get("result") != "PASS" or not removable:
            adjusted.append(item)
            continue
        gaps = [gap for gap in item.get("gaps") or [] if gap not in removable]
        updated = {
            **item,
            "gaps": gaps,
            "quality_result": "PASS" if not gaps else item.get("quality_result"),
            "production_mutation_performed": (
                bool(item.get("production_mutation_performed"))
                or check.get("phase") == "P4"
            ),
        }
        updated["next_action"] = _activation_phase_next_action(
            phase,
            str(updated.get("state") or ""),
            gaps,
        )
        adjusted.append(updated)
    return adjusted


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
        if str(gap or "") and str(gap) not in DEFERRED_SCOPE_GAPS
    ]
    blockers.extend(str(gap) for gap in source_gate.get("gaps", []) if str(gap or ""))
    for item in phase_progress:
        blockers.extend(
            str(gap)
            for gap in item.get("gaps", [])
            if str(gap or "") and str(gap) not in DEFERRED_SCOPE_GAPS
        )
    if source_gate.get("release_quality_gate") != "green":
        blockers.append("production_quality_not_green")
    return _dedupe(blockers)


def _next_activation_phase(phase_progress: list[Mapping[str, Any]]) -> str:
    for item in phase_progress:
        if item.get("state") == "in_progress":
            return str(item.get("phase") or "")
    for item in phase_progress:
        phase = str(item.get("phase") or "")
        if phase not in ACTIVATION_SCOPE_PHASES:
            continue
        if phase in {"P2", "P3", "P4", "P5"}:
            continue
        if any(str(gap or "") not in DEFERRED_SCOPE_GAPS for gap in item.get("gaps") or []):
            return phase
    for item in phase_progress:
        if item.get("state") not in {"local_validated", "production_validated", "complete"}:
            return str(item.get("phase") or "")
    return ""


def _remaining_activation_phases(next_phase: str) -> list[str]:
    if not next_phase or next_phase not in ACTIVATION_SCOPE_PHASES:
        return []
    start = ACTIVATION_SCOPE_PHASES.index(next_phase)
    return list(ACTIVATION_SCOPE_PHASES[start:])


def _positive_int(value: Any, *, default: int = 0) -> int:
    candidate = default if value is None or value == "" else value
    try:
        number = int(candidate)
    except (TypeError, ValueError):
        number = default
    return max(0, number)


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
