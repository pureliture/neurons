from agent_knowledge.llm_brain_core.golden_query_eval import (
    GOLDEN_QUERIES,
    REQUIRED_QUALITY_AXES,
    build_baseline_golden_query_report,
    build_product_activation_progress_report,
    build_phase_golden_query_coverage_report,
    build_source_to_authority_quality_gate_report,
    evaluate_object_pack_response,
    evaluate_product_evidence_summary,
)
from agent_knowledge.llm_brain_core.object_packs import build_code_change_impact_pack
from agent_knowledge.llm_brain_core.objects.runtime_readiness import (
    REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES,
)

_REQUIRED_ROUTE_NAMES = list(REQUIRED_BRAIN_OBJECTS_QUERY_ROUTES)


def test_golden_query_baseline_records_current_low_quality_failures():
    report = build_baseline_golden_query_report()

    assert report["schema_version"] == "knowledge_object_golden_query_eval.v1"
    assert len(report["queries"]) >= 10
    assert report["status"] == "baseline_red"
    assert all(item["passes"] is False for item in report["queries"])
    assert report["queries"][1]["query"] == "이 repo 문서 최신화하려면 뭘 봐야 해?"


def test_eval_requires_lane_evidence_gap_and_recommended_action():
    failing = evaluate_object_pack_response(
        GOLDEN_QUERIES[1],
        {
            "route": "documentation_cleanup",
            "lanes": {"accepted_current": []},
            "evidence": [],
            "gaps": [],
            "recommended_actions": [],
        },
    )
    passing = evaluate_object_pack_response(
        GOLDEN_QUERIES[1],
        {
            "route": "documentation_cleanup",
            "lanes": {"accepted_current": [{"object_id": "ko:RepoDocument:readme"}]},
            "evidence": [{"evidence_id": "ev:source_hash:readme"}],
            "gaps": [],
            "recommended_actions": [{"object_id": "ko:RepoDocument:readme", "action": "keep"}],
        },
    )

    assert failing["passes"] is False
    assert "missing_evidence_or_gap" in failing["failures"]
    assert "missing_recommended_action" in failing["failures"]
    assert passing["passes"] is True


def test_eval_strict_axes_require_edge_freshness_and_gap_fields():
    failing = evaluate_object_pack_response(
        GOLDEN_QUERIES[3],
        {
            "route": "code_change_impact",
            "lanes": {"candidate": [{"object_id": "ko:Commit:change"}]},
            "evidence": [{"evidence_id": "ev:test"}],
            "recommended_actions": [{"object_id": "ko:Commit:change", "action": "run_tests"}],
        },
        required_axes=REQUIRED_QUALITY_AXES,
    )
    passing = evaluate_object_pack_response(
        GOLDEN_QUERIES[3],
        {
            "route": "code_change_impact",
            "lanes": {"candidate": [{"object_id": "ko:Commit:change"}]},
            "edges": [{"edge_id": "ke:validated_by:test", "edge_type": "validated_by"}],
            "evidence": [{"evidence_id": "ev:test", "verification_state": "test_verified"}],
            "verification": {
                "freshness_checked": [{"evidence_id": "ev:test"}],
                "runtime_verified": [],
                "runtime_unverified": [{"reason": "live_runtime_impact_unverified"}],
            },
            "gaps": [],
            "recommended_actions": [{"object_id": "ko:Commit:change", "action": "run_tests"}],
        },
        required_axes=REQUIRED_QUALITY_AXES,
    )

    assert failing["passes"] is False
    assert "missing_edge" in failing["failures"]
    assert "missing_freshness" in failing["failures"]
    assert "missing_gap_field" in failing["failures"]
    assert passing["passes"] is True
    assert passing["checked_axes"] == REQUIRED_QUALITY_AXES


def test_eval_strict_axes_require_empty_authority_lane_disclosure():
    base_response = {
        "route": "documentation_cleanup",
        "lanes": {
            "accepted_current": [],
            "proposal_only": [{"object_id": "ko:RepoDocument:legacy"}],
        },
        "edges": [{"edge_id": "ke:requires_evidence:legacy", "edge_type": "requires_evidence"}],
        "evidence": [{"evidence_id": "ev:inventory:legacy", "verification_state": "source_hash_verified"}],
        "verification": {"freshness_checked": [{"evidence_id": "ev:inventory:legacy"}]},
        "recommended_actions": [{"object_id": "ko:RepoDocument:legacy", "action": "review_archive"}],
    }
    failing = evaluate_object_pack_response(
        GOLDEN_QUERIES[1],
        {**base_response, "gaps": []},
        required_axes=REQUIRED_QUALITY_AXES,
    )
    passing = evaluate_object_pack_response(
        GOLDEN_QUERIES[1],
        {**base_response, "gaps": ["accepted_current documents empty"]},
        required_axes=REQUIRED_QUALITY_AXES,
    )

    assert failing["passes"] is False
    assert "empty_authority_lane_not_stated:accepted_current" in failing["failures"]
    assert passing["passes"] is True


def test_eval_strict_axes_require_freshness_specific_verification():
    response = {
        "route": "documentation_cleanup",
        "lanes": {"candidate": [{"object_id": "ko:RepoDocument:legacy"}]},
        "edges": [{"edge_id": "ke:requires_evidence:legacy", "edge_type": "requires_evidence"}],
        "evidence": [{"evidence_id": "ev:inventory:legacy", "verification_state": "source_hash_verified"}],
        "verification": {"unverified": [{"evidence_id": "ev:inventory:legacy"}]},
        "gaps": [],
        "recommended_actions": [{"object_id": "ko:RepoDocument:legacy", "action": "review_archive"}],
    }

    result = evaluate_object_pack_response(
        GOLDEN_QUERIES[1],
        response,
        required_axes=REQUIRED_QUALITY_AXES,
    )

    assert result["passes"] is False
    assert "missing_freshness" in result["failures"]


def test_eval_strict_axes_require_runtime_evidence_for_runtime_claims():
    base_response = {
        "route": "deployment_runtime_truth",
        "lanes": {"candidate": [{"object_id": "ko:RuntimeTruth:deploy"}]},
        "edges": [{"edge_id": "ke:validated_by:deploy", "edge_type": "validated_by"}],
        "evidence": [{"evidence_id": "ev:pr-merge", "verification_state": "source_hash_verified"}],
        "gaps": [],
        "recommended_actions": [{"object_id": "ko:RuntimeTruth:deploy", "action": "verify_runtime"}],
    }
    failing = evaluate_object_pack_response(
        GOLDEN_QUERIES[4],
        {**base_response, "verification": {"runtime_verified": [], "runtime_unverified": []}},
        required_axes=REQUIRED_QUALITY_AXES,
    )
    passing = evaluate_object_pack_response(
        GOLDEN_QUERIES[4],
        {
            **base_response,
            "verification": {
                "freshness_checked": [{"evidence_id": "ev:runtime-freshness"}],
                "runtime_verified": [],
                "runtime_unverified": [{"reason": "runtime_evidence_unverified"}],
            },
        },
        required_axes=REQUIRED_QUALITY_AXES,
    )

    assert failing["passes"] is False
    assert "runtime_evidence_missing" in failing["failures"]
    assert passing["passes"] is True


def test_eval_strict_axes_detect_korean_runtime_claims():
    base_response = {
        "route": "code_change_impact",
        "lanes": {"candidate": [{"object_id": "ko:RuntimeSurface:lbrain"}]},
        "edges": [{"edge_id": "ke:requires_live_evidence:lbrain", "edge_type": "requires_live_evidence"}],
        "evidence": [{"evidence_id": "ev:test", "verification_state": "freshness_checked"}],
        "verification": {"freshness_checked": [{"evidence_id": "ev:test"}]},
        "gaps": [],
        "recommended_actions": [{"object_id": "ko:RuntimeSurface:lbrain", "action": "verify_runtime"}],
    }
    failing = evaluate_object_pack_response(
        GOLDEN_QUERIES[3],
        {**base_response, "verification": {"freshness_checked": [{"evidence_id": "ev:test"}]}},
        required_axes=REQUIRED_QUALITY_AXES,
    )
    passing = evaluate_object_pack_response(
        GOLDEN_QUERIES[3],
        {
            **base_response,
            "verification": {
                "freshness_checked": [{"evidence_id": "ev:test"}],
                "runtime_verified": [],
                "runtime_unverified": [{"reason": "live_runtime_impact_unverified"}],
            },
        },
        required_axes=REQUIRED_QUALITY_AXES,
    )

    assert failing["passes"] is False
    assert "runtime_evidence_missing" in failing["failures"]
    assert passing["passes"] is True


def test_code_change_impact_pack_passes_strict_axes_with_runtime_gap():
    pack = build_code_change_impact_pack(
        current_files=["worker/lib/agent_knowledge/llm_brain_core/objects/runtime_readiness.py"],
        consumer="codex",
    )

    result = evaluate_object_pack_response(
        GOLDEN_QUERIES[3],
        pack,
        required_axes=REQUIRED_QUALITY_AXES,
    )

    assert result["passes"] is True


def test_phase_golden_query_coverage_reports_pass_with_gaps_not_green():
    report = build_phase_golden_query_coverage_report()

    assert report["schema_version"] == "knowledge_object_phase_golden_query_coverage.v1"
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["release_quality_gate"] == "not_green"
    phases = {item["phase"]: item for item in report["phases"]}
    assert set(phases) >= {"P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10"}
    assert phases["P1"]["result"] == "PASS_WITH_GAPS"
    assert phases["P4"]["golden_query_family"] == "review queue and authority promotion"
    assert phases["P4"]["result"] == "PASS_WITH_GAPS"
    assert phases["P4"]["required_axes"] == [
        "object",
        "edge",
        "evidence",
        "freshness",
        "gap",
        "recommended_action",
    ]
    assert "production_authority_pilot_not_executed" in phases["P4"]["gaps"]
    assert phases["P6"]["result"] == "PASS_WITH_GAPS"
    assert "handoff_pack_not_implemented" not in phases["P6"]["gaps"]
    assert "live_multi_device_rollup_unproven" in phases["P6"]["gaps"]
    assert phases["P7"]["result"] == "PASS_WITH_GAPS"
    assert phases["P7"]["golden_query_family"] == "code style drift"
    assert "accepted_preference_context_pack_live_unproven" in phases["P7"]["gaps"]
    assert phases["P8"]["result"] == "PASS_WITH_GAPS"
    assert phases["P8"]["golden_query_family"] == "pr merge and deploy truth"
    assert "live_runtime_rollout_identity_unproven" in phases["P8"]["gaps"]
    assert phases["P9"]["result"] == "PASS_WITH_GAPS"
    assert phases["P9"]["golden_query_family"] == "agent context productization"
    assert "production_consumer_context_pack_live_unproven" in phases["P9"]["gaps"]


def test_source_to_authority_quality_gate_covers_review_approval_and_read_path_without_production_mutation():
    report = build_source_to_authority_quality_gate_report()

    assert report["schema_version"] == "source_to_authority_quality_gate_report.v1"
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["local_quality_gate"] == "green"
    assert report["release_quality_gate"] == "not_green"
    assert report["production_mutation_performed"] is False
    assert report["production_approval_gate"] == "preapproved"
    assert report["production_mutation_execution"] == "not_performed_by_local_gate"
    assert report["authority_write_scope"] == "local_test"
    checks = {item["id"]: item for item in report["path_checks"]}

    assert set(checks) >= {
        "source_to_candidate_graph",
        "candidate_review_edit",
        "approval_board_local_test",
        "authority_read_after_write",
        "production_decision_denial",
    }
    assert checks["source_to_candidate_graph"]["result"] == "PASS"
    assert checks["source_to_candidate_graph"]["quality_eval"]["passes"] is True
    assert checks["candidate_review_edit"]["result"] == "PASS"
    assert checks["candidate_review_edit"]["target_scope"] == "production"
    assert checks["candidate_review_edit"]["mutation_mode"] == "no_mutation"
    assert checks["candidate_review_edit"]["accepted_edit_actions"] == [
        "update_object",
        "add_evidence",
        "add_edge",
        "remove_edge",
        "remove_evidence",
    ]
    assert checks["candidate_review_edit"]["updated_edge_count"] == 1
    assert checks["candidate_review_edit"]["updated_evidence_count"] == 1
    assert checks["approval_board_local_test"]["result"] == "PASS"
    assert checks["authority_read_after_write"]["result"] == "PASS"
    assert checks["authority_read_after_write"]["quality_eval"]["passes"] is True
    assert checks["production_decision_denial"]["result"] == "PASS"
    assert checks["production_decision_denial"]["production_mutation_performed"] is False
    surface_checks = {item["id"]: item for item in report["product_surface_checks"]}
    assert set(surface_checks) >= {
        "mcp_brain_objects_query_tool",
        "mcp_source_to_candidate_graph_tool",
        "mcp_candidate_review_edit_tool",
        "mcp_approval_board_decide_tool",
        "mcp_source_to_candidate_runtime_readiness_tool",
    }
    assert surface_checks["mcp_brain_objects_query_tool"]["result"] == "PASS"
    assert surface_checks["mcp_brain_objects_query_tool"]["tool"] == "brain_objects_query"
    assert surface_checks["mcp_brain_objects_query_tool"]["production_mutation_performed"] is False
    assert surface_checks["mcp_source_to_candidate_graph_tool"]["result"] == "PASS"
    assert surface_checks["mcp_source_to_candidate_graph_tool"]["tool"] == "brain_source_to_candidate_graph"
    assert surface_checks["mcp_source_to_candidate_graph_tool"]["production_target_denied"] is True
    assert surface_checks["mcp_candidate_review_edit_tool"]["result"] == "PASS"
    assert surface_checks["mcp_candidate_review_edit_tool"]["authority_write_performed"] is False
    assert surface_checks["mcp_approval_board_decide_tool"]["result"] == "PASS"
    assert surface_checks["mcp_approval_board_decide_tool"]["production_target_denied"] is True
    assert surface_checks["mcp_source_to_candidate_runtime_readiness_tool"]["result"] == "PASS"
    assert surface_checks["mcp_source_to_candidate_runtime_readiness_tool"]["network_used"] is False
    assert surface_checks["mcp_source_to_candidate_runtime_readiness_tool"]["production_mutation_performed"] is False
    assert "production_authority_gate_preapproved_not_executed" in report["gaps"]


def test_product_activation_progress_keeps_p2_to_p9_scope_visible():
    report = build_product_activation_progress_report()

    assert report["schema_version"] == "lbrain_product_activation_progress.v1"
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["goal_complete"] is False
    assert report["production_ready"] is False
    assert report["local_quality_gate"] == "green"
    assert report["release_quality_gate"] == "not_green"
    assert report["production_mutation_performed"] is False
    assert report["production_approval_gate"] == "preapproved"
    assert report["production_mutation_execution"] == "not_performed_by_local_gate"
    assert report["scope_phases"] == ["P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9"]
    assert report["minimum_review_loop_checkpoint"]["phases"] == ["P2", "P3", "P4"]
    assert report["minimum_review_loop_checkpoint"]["status"] == "PASS_WITH_GAPS"
    assert report["next_phase"] == "P5"
    assert set(report["remaining_phases"]) >= {"P5", "P6", "P7", "P8", "P9"}
    assert report["hard_failures"] == []
    assert report["quality_gate_inputs"]["source_to_authority_local_quality_gate"] == "green"
    assert report["product_evidence_status"] == "PASS_WITH_GAPS"
    assert "production_quality_not_green" in report["goal_completion_blockers"]
    assert "live_runtime_read_path_unverified" in report["goal_completion_blockers"]
    assert "future_phase_golden_query_slices_planned" not in report["goal_completion_blockers"]
    assert "future_phase_slices_planned" not in report["goal_completion_blockers"]
    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    assert set(checks) == {"P2", "P6", "P7", "P8", "P9"}
    assert checks["P2"]["result"] == "PASS_WITH_GAPS"
    assert "p2_production_corpus_ingest_evidence_unverified" in checks["P2"]["gaps"]
    assert checks["P6"]["result"] == "PASS"
    assert checks["P7"]["result"] == "PASS"
    assert checks["P8"]["result"] == "PASS_WITH_GAPS"
    assert "p8_runtime_evidence_unverified" in checks["P8"]["gaps"]
    assert "p8_runtime_evidence_collection_plan_not_live_evidence" in checks["P8"]["gaps"]
    assert "p8_runtime_evidence_packet_template_not_live_evidence" in checks["P8"]["gaps"]
    assert "p8_runtime_evidence_collector_not_live_evidence" in checks["P8"]["gaps"]
    assert "p8_shadow_route_smoke_collection_pending" in checks["P8"]["gaps"]
    assert "p8_shadow_route_smoke_collection_pending:deployment_runtime_truth" in checks["P8"]["gaps"]
    assert "p8_shadow_collection_run_pending" in checks["P8"]["gaps"]
    assert "p8_shadow_collection_run_pending:deployment_runtime_truth" in checks["P8"]["gaps"]
    assert checks["P9"]["result"] == "PASS"
    evidence = {item["phase"]: item for item in report["product_evidence_summary"]}
    assert set(evidence) == {"P2", "P6", "P7", "P8", "P9"}
    assert evidence["P2"]["schema_version"] == "reference_corpus_production_ingest_readiness.v1"
    assert evidence["P2"]["production_mutation_performed"] is False
    assert evidence["P6"]["schema_version"] == "object_extraction_session_project_rollup_preview.v1"
    assert evidence["P6"]["object_count"] >= 5
    assert evidence["P6"]["edge_count"] >= 6
    assert evidence["P6"]["evidence_count"] >= 1
    assert evidence["P7"]["schema_version"] == "object_extraction_preference_style_preview.v1"
    assert evidence["P7"]["artifact_preference_pack_status"] == "pass"
    assert evidence["P8"]["schema_version"] == "object_extraction_runtime_truth_preview.v1"
    assert evidence["P8"]["runtime_unverified_count"] == 1
    assert evidence["P8"]["source_commit_matches_pr_head"] is True
    assert evidence["P8"]["permission"] == "allowed"
    assert evidence["P8"]["permission_reason"] == "approved_scope_present"
    assert evidence["P8"]["authority_write_performed"] is False
    assert (
        evidence["P8"]["runtime_evidence_collection_plan_schema"]
        == "source_to_candidate_runtime_evidence_collection_plan.v1"
    )
    assert evidence["P8"]["runtime_evidence_collection_plan_status"] == "ready"
    assert (
        evidence["P8"]["runtime_authority_bounded_execution_required_demote_step"]
        == "demote_prior_object_to_accepted_non_current_or_archive_only"
    )
    assert evidence["P8"]["runtime_authority_bounded_execution_demote_step_required"] is True
    assert evidence["P8"]["runtime_evidence_collection_plan_network_used"] is False
    assert evidence["P8"]["runtime_evidence_collection_plan_mutation_allowed"] is False
    assert evidence["P8"]["runtime_evidence_collection_plan_production_mutation_performed"] is False
    assert evidence["P8"]["runtime_evidence_collection_plan_readiness_claim"] == "plan_only_not_runtime_evidence"
    assert (
        evidence["P8"]["runtime_evidence_packet_template_schema"]
        == "source_to_candidate_runtime_evidence_packet_template.v1"
    )
    assert evidence["P8"]["runtime_evidence_packet_template_status"] == "template_ready"
    assert evidence["P8"]["runtime_evidence_packet_template_network_used"] is False
    assert evidence["P8"]["runtime_evidence_packet_template_mutation_allowed"] is False
    assert evidence["P8"]["runtime_evidence_packet_template_production_mutation_performed"] is False
    assert (
        evidence["P8"]["runtime_evidence_packet_template_readiness_claim"]
        == "template_only_not_runtime_evidence"
    )
    assert evidence["P8"]["runtime_evidence_packet_template_required_field_count"] >= 8
    assert evidence["P8"]["runtime_evidence_packet_template_route_count"] == len(_REQUIRED_ROUTE_NAMES)
    assert (
        evidence["P8"]["shadow_route_smoke_request_schema"]
        == "source_to_candidate_runtime_shadow_collection_request.v1"
    )
    assert evidence["P8"]["shadow_route_smoke_request_status"] == "requested"
    assert evidence["P8"]["shadow_route_smoke_route_count"] == len(_REQUIRED_ROUTE_NAMES)
    assert set(evidence["P8"]["shadow_route_smoke_pending_routes"]) == set(_REQUIRED_ROUTE_NAMES)
    assert evidence["P8"]["shadow_route_smoke_network_used"] is False
    assert evidence["P8"]["shadow_route_smoke_mutation_allowed"] is False
    assert evidence["P8"]["shadow_route_smoke_production_mutation_performed"] is False
    assert evidence["P8"]["shadow_route_smoke_readiness_claim"] == "request_only_not_live_evidence"
    assert (
        evidence["P8"]["shadow_collection_registration_schema"]
        == "source_to_candidate_runtime_shadow_collection_registration.v1"
    )
    assert evidence["P8"]["shadow_collection_registration_status"] == "registration_ready"
    assert evidence["P8"]["shadow_collection_registration_run_status"] == "not_run"
    assert evidence["P8"]["shadow_collection_registration_request_count"] == 1
    assert evidence["P8"]["shadow_collection_registration_route_count"] == len(_REQUIRED_ROUTE_NAMES)
    assert set(evidence["P8"]["shadow_collection_registration_routes"]) == set(_REQUIRED_ROUTE_NAMES)
    assert evidence["P8"]["shadow_collection_registration_network_used"] is False
    assert evidence["P8"]["shadow_collection_registration_mutation_allowed"] is False
    assert evidence["P8"]["shadow_collection_registration_production_mutation_performed"] is False
    assert (
        evidence["P8"]["shadow_collection_registration_readiness_claim"]
        == "registration_only_not_runtime_evidence"
    )
    assert (
        evidence["P8"]["runtime_evidence_collector_packet_schema"]
        == "source_to_candidate_runtime_evidence.v1"
    )
    assert evidence["P8"]["runtime_evidence_collector_route_count"] == len(_REQUIRED_ROUTE_NAMES)
    assert evidence["P8"]["runtime_evidence_collector_network_used"] is False
    assert evidence["P8"]["runtime_evidence_collector_production_mutation_performed"] is False
    assert (
        evidence["P8"]["runtime_evidence_collector_readiness_claim"]
        == "collector_packet_not_live_evidence"
    )
    assert (
        evidence["P8"]["runtime_evidence_post_deploy_capture_packet_schema"]
        == "source_to_candidate_runtime_evidence.v1"
    )
    assert (
        evidence["P8"]["runtime_evidence_post_deploy_capture_collection_mode"]
        == "post_deploy_read_only_smoke"
    )
    assert evidence["P8"]["runtime_evidence_post_deploy_capture_network_used"] is True
    assert evidence["P8"]["runtime_evidence_post_deploy_capture_production_mutation_performed"] is False
    assert evidence["P8"]["runtime_evidence_post_deploy_capture_report_status"] == "PASS_WITH_GAPS"
    assert evidence["P8"]["runtime_evidence_post_deploy_capture_production_ready"] is False
    assert (
        evidence["P8"]["runtime_evidence_collector_review_loop_schema"]
        == "source_to_candidate_review_loop_evidence.v1"
    )
    assert evidence["P8"]["runtime_evidence_collector_review_loop_candidate_count"] >= 1
    assert evidence["P8"]["runtime_evidence_collector_review_loop_edited_count"] >= 1
    assert evidence["P8"]["runtime_evidence_collector_review_loop_decision_count"] >= 1
    assert evidence["P8"]["runtime_evidence_collector_review_loop_authority_scope"] == "local_test"
    assert (
        evidence["P8"]["runtime_evidence_collector_session_rollup_schema"]
        == "session_project_rollup_runtime_evidence.v1"
    )
    assert evidence["P8"]["runtime_evidence_collector_session_rollup_device_count"] >= 2
    assert evidence["P8"]["runtime_evidence_collector_session_rollup_visible_session_count"] >= 1
    assert evidence["P8"]["runtime_evidence_collector_session_rollup_read_after_write_status"] == "validated"
    assert (
        evidence["P8"]["runtime_evidence_collector_preference_artifact_schema"]
        == "preference_artifact_memory_runtime_evidence.v1"
    )
    assert evidence["P8"]["runtime_evidence_collector_preference_accepted_count"] >= 1
    assert evidence["P8"]["runtime_evidence_collector_preference_proposal_count"] >= 1
    assert evidence["P8"]["runtime_evidence_collector_preference_html_route"] == "html_visualization_preference"
    assert evidence["P8"]["runtime_evidence_collector_preference_artifact_check_status"] == "pass"
    assert (
        evidence["P8"]["runtime_evidence_collector_permission_audit_schema"]
        == "permission_sensitive_runtime_audit_evidence.v1"
    )
    assert evidence["P8"]["runtime_evidence_collector_permission_audit_event_count"] == 2
    assert evidence["P8"]["runtime_evidence_collector_permission_audit_store_status"] == "recorded"
    assert (
        evidence["P8"]["runtime_evidence_collector_agent_context_startup_schema"]
        == "agent_context_startup_runtime_evidence.v1"
    )
    assert evidence["P8"]["runtime_evidence_collector_agent_context_startup_loaded"] is True
    assert evidence["P8"]["runtime_evidence_collector_agent_context_startup_read_path_tool"] == "brain_objects_query"
    assert evidence["P8"]["runtime_evidence_collector_agent_context_startup_route_count"] == len(
        _REQUIRED_ROUTE_NAMES
    )
    assert evidence["P9"]["schema_version"] == "agent_context_product_pack.v1"
    assert evidence["P9"]["section_counts"]["style_preference"] >= 1
    assert evidence["P9"]["section_counts"]["active_work"] >= 1
    assert evidence["P9"]["tool_hint_count"] >= 5
    assert evidence["P9"]["tool_hint_safe_target_count"] >= 5
    assert evidence["P9"]["unsafe_tool_hint_count"] == 0
    assert evidence["P9"]["mutation_allowed"] is False
    assert all(item["production_mutation_performed"] is False for item in evidence.values())

    phase_progress = {item["phase"]: item for item in report["phase_progress"]}
    assert phase_progress["P4"]["quality_result"] == "PASS_WITH_GAPS"
    assert phase_progress["P5"]["state"] == "in_progress"
    assert phase_progress["P6"]["state"] == "local_validated"
    assert phase_progress["P9"]["state"] == "local_validated"


def test_product_evidence_summary_fails_when_p8_source_commit_mismatches_pr_head():
    progress = build_product_activation_progress_report()
    evidence = [
        {**item, "source_commit_matches_pr_head": False}
        if item.get("phase") == "P8"
        else item
        for item in progress["product_evidence_summary"]
    ]

    result = evaluate_product_evidence_summary(evidence)

    checks = {item["phase"]: item for item in result["checks"]}
    assert result["status"] == "FAIL"
    assert "P8:product_evidence_failed" in result["hard_failures"]
    assert "p8_source_commit_mismatch_with_pr_head" in checks["P8"]["failures"]


def test_product_evidence_summary_marks_missing_p8_source_commit_identity_as_gap():
    progress = build_product_activation_progress_report()
    evidence = []
    for item in progress["product_evidence_summary"]:
        if item.get("phase") == "P8":
            item = dict(item)
            item.pop("source_commit_matches_pr_head")
        evidence.append(item)

    result = evaluate_product_evidence_summary(evidence)

    checks = {item["phase"]: item for item in result["checks"]}
    assert result["status"] == "PASS_WITH_GAPS"
    assert "P8:product_evidence_failed" not in result["hard_failures"]
    assert "p8_source_commit_matches_pr_head_unverified" in checks["P8"]["gaps"]


def test_product_evidence_summary_fails_closed_when_required_phase_evidence_is_missing():
    result = evaluate_product_evidence_summary(
        [
            {
                "phase": "P6",
                "schema_version": "object_extraction_session_project_rollup_preview.v1",
                "object_count": 1,
                "edge_count": 0,
                "evidence_count": 0,
                "production_mutation_performed": False,
            },
            {
                "phase": "P8",
                "schema_version": "object_extraction_runtime_truth_preview.v1",
                "runtime_unverified_count": 0,
                "permission": "allowed",
                "permission_reason": "approved_scope_present",
                "authority_write_performed": True,
                "production_mutation_performed": True,
            },
        ]
    )

    assert result["status"] == "FAIL"
    assert "P2:product_evidence_missing" in result["hard_failures"]
    assert "P6:product_evidence_failed" in result["hard_failures"]
    assert "P7:product_evidence_missing" in result["hard_failures"]
    assert "P8:product_evidence_failed" in result["hard_failures"]
    checks = {item["phase"]: item for item in result["checks"]}
    assert "p6_session_rollup_incomplete" in checks["P6"]["failures"]
    assert "p8_production_mutation_performed" in checks["P8"]["failures"]


def test_product_evidence_summary_fails_when_p2_claims_pass_without_live_evidence():
    result = evaluate_product_evidence_summary(
        [
            {
                "phase": "P2",
                "schema_version": "reference_corpus_production_ingest_readiness.v1",
                "status": "PASS",
                "live_evidence_provided": False,
                "production_mutation_performed": True,
                "network_used": False,
                "gaps": [],
            }
        ]
    )

    checks = {item["phase"]: item for item in result["checks"]}
    assert result["status"] == "FAIL"
    assert "P2:product_evidence_failed" in result["hard_failures"]
    assert "p2_live_evidence_missing_for_pass" in checks["P2"]["failures"]


def test_product_evidence_summary_marks_p8_runtime_unverified_as_gap_not_pass():
    result = evaluate_product_evidence_summary(
        [
            {
                "phase": "P2",
                "schema_version": "reference_corpus_production_ingest_readiness.v1",
                "status": "PASS_WITH_GAPS",
                "production_mutation_performed": False,
                "gaps": ["production_corpus_ingest_evidence_unverified"],
            },
            {
                "phase": "P6",
                "schema_version": "object_extraction_session_project_rollup_preview.v1",
                "object_count": 8,
                "edge_count": 8,
                "evidence_count": 1,
                "handoff_pack_schema": "session_project_handoff_pack.v1",
                "production_mutation_performed": False,
            },
            {
                "phase": "P7",
                "schema_version": "object_extraction_preference_style_preview.v1",
                "object_count": 2,
                "artifact_preference_pack_status": "pass",
                "accepted_preference_count": 1,
                "source_evidence_ref_count": 1,
                "production_mutation_performed": False,
            },
            {
                "phase": "P8",
                "schema_version": "object_extraction_runtime_truth_preview.v1",
                "runtime_verified_count": 0,
                "runtime_unverified_count": 1,
                "source_commit_matches_pr_head": True,
                "permission": "allowed",
                "permission_reason": "approved_scope_present",
                "authority_write_performed": False,
                "runtime_authority_bounded_execution_required_demote_step": (
                    "demote_prior_object_to_accepted_non_current_or_archive_only"
                ),
                "runtime_authority_bounded_execution_demote_step_required": True,
                "runtime_evidence_collection_plan_schema": "source_to_candidate_runtime_evidence_collection_plan.v1",
                "runtime_evidence_collection_plan_status": "ready",
                "runtime_evidence_collection_plan_network_used": False,
                "runtime_evidence_collection_plan_mutation_allowed": False,
                "runtime_evidence_collection_plan_production_mutation_performed": False,
                "runtime_evidence_collection_plan_readiness_claim": "plan_only_not_runtime_evidence",
                "runtime_evidence_packet_template_schema": "source_to_candidate_runtime_evidence_packet_template.v1",
                "runtime_evidence_packet_template_status": "template_ready",
                "runtime_evidence_packet_template_network_used": False,
                "runtime_evidence_packet_template_mutation_allowed": False,
                "runtime_evidence_packet_template_production_mutation_performed": False,
                "runtime_evidence_packet_template_readiness_claim": "template_only_not_runtime_evidence",
                "runtime_evidence_packet_template_required_field_count": 9,
                "runtime_evidence_packet_template_route_count": len(_REQUIRED_ROUTE_NAMES),
                "shadow_route_smoke_request_schema": "source_to_candidate_runtime_shadow_collection_request.v1",
                "shadow_route_smoke_request_status": "requested",
                "shadow_route_smoke_route_count": len(_REQUIRED_ROUTE_NAMES),
                "shadow_route_smoke_pending_routes": list(_REQUIRED_ROUTE_NAMES),
                "shadow_route_smoke_network_used": False,
                "shadow_route_smoke_mutation_allowed": False,
                "shadow_route_smoke_production_mutation_performed": False,
                "shadow_route_smoke_readiness_claim": "request_only_not_live_evidence",
                "shadow_collection_registration_schema": "source_to_candidate_runtime_shadow_collection_registration.v1",
                "shadow_collection_registration_status": "registration_ready",
                "shadow_collection_registration_run_status": "not_run",
                "shadow_collection_registration_request_count": 1,
                "shadow_collection_registration_route_count": len(_REQUIRED_ROUTE_NAMES),
                "shadow_collection_registration_routes": list(_REQUIRED_ROUTE_NAMES),
                "shadow_collection_registration_network_used": False,
                "shadow_collection_registration_mutation_allowed": False,
                "shadow_collection_registration_production_mutation_performed": False,
                "shadow_collection_registration_readiness_claim": "registration_only_not_runtime_evidence",
                "runtime_evidence_collector_packet_schema": "source_to_candidate_runtime_evidence.v1",
                "runtime_evidence_collector_route_count": len(_REQUIRED_ROUTE_NAMES),
                "runtime_evidence_collector_network_used": False,
                "runtime_evidence_collector_production_mutation_performed": False,
                "runtime_evidence_collector_readiness_claim": "collector_packet_not_live_evidence",
                "runtime_evidence_post_deploy_capture_packet_schema": "source_to_candidate_runtime_evidence.v1",
                "runtime_evidence_post_deploy_capture_collection_mode": "post_deploy_read_only_smoke",
                "runtime_evidence_post_deploy_capture_network_used": True,
                "runtime_evidence_post_deploy_capture_production_mutation_performed": False,
                "runtime_evidence_post_deploy_capture_report_status": "PASS_WITH_GAPS",
                "runtime_evidence_post_deploy_capture_production_ready": False,
                "runtime_evidence_collector_review_loop_schema": "source_to_candidate_review_loop_evidence.v1",
                "runtime_evidence_collector_review_loop_candidate_count": 2,
                "runtime_evidence_collector_review_loop_edited_count": 1,
                "runtime_evidence_collector_review_loop_decision_count": 1,
                "runtime_evidence_collector_review_loop_authority_scope": "local_test",
                "runtime_evidence_collector_session_rollup_schema": "session_project_rollup_runtime_evidence.v1",
                "runtime_evidence_collector_session_rollup_device_count": 2,
                "runtime_evidence_collector_session_rollup_visible_session_count": 2,
                "runtime_evidence_collector_session_rollup_read_after_write_status": "validated",
                "runtime_evidence_collector_preference_artifact_schema": "preference_artifact_memory_runtime_evidence.v1",
                "runtime_evidence_collector_preference_accepted_count": 1,
                "runtime_evidence_collector_preference_proposal_count": 1,
                "runtime_evidence_collector_preference_html_route": "html_visualization_preference",
                "runtime_evidence_collector_preference_artifact_check_status": "pass",
                "runtime_evidence_collector_permission_audit_schema": "permission_sensitive_runtime_audit_evidence.v1",
                "runtime_evidence_collector_permission_audit_event_count": 2,
                "runtime_evidence_collector_permission_audit_store_status": "recorded",
                "runtime_evidence_collector_agent_context_startup_schema": "agent_context_startup_runtime_evidence.v1",
                "runtime_evidence_collector_agent_context_startup_loaded": True,
                "runtime_evidence_collector_agent_context_startup_read_path_tool": "brain_objects_query",
                "runtime_evidence_collector_agent_context_startup_route_count": len(_REQUIRED_ROUTE_NAMES),
                "production_mutation_performed": False,
            },
            {
                "phase": "P9",
                "schema_version": "agent_context_product_pack.v1",
                "section_counts": {"style_preference": 1, "active_work": 1},
                "tool_hint_count": 5,
                "tool_hint_safe_target_count": 5,
                "unsafe_tool_hint_count": 0,
                "mutation_allowed": False,
                "production_mutation_performed": False,
            },
        ]
    )

    checks = {item["phase"]: item for item in result["checks"]}
    assert result["status"] == "PASS_WITH_GAPS"
    assert result["hard_failures"] == []
    assert checks["P8"]["result"] == "PASS_WITH_GAPS"
    assert checks["P8"]["failures"] == []
    assert checks["P8"]["gaps"] == [
        "p8_runtime_evidence_unverified",
        "p8_runtime_verified_evidence_missing",
        "p8_runtime_evidence_collection_plan_not_live_evidence",
        "p8_runtime_evidence_packet_template_not_live_evidence",
        "p8_runtime_evidence_collector_not_live_evidence",
        "p8_shadow_route_smoke_collection_pending",
        *[
            f"p8_shadow_route_smoke_collection_pending:{route}"
            for route in _REQUIRED_ROUTE_NAMES
        ],
        "p8_shadow_collection_run_pending",
        *[
            f"p8_shadow_collection_run_pending:{route}"
            for route in _REQUIRED_ROUTE_NAMES
        ],
    ]


def test_product_evidence_summary_fails_when_p8_collection_plan_is_missing_or_mutating():
    result = evaluate_product_evidence_summary(
        [
            {
                "phase": "P6",
                "schema_version": "object_extraction_session_project_rollup_preview.v1",
                "object_count": 8,
                "edge_count": 8,
                "evidence_count": 1,
                "handoff_pack_schema": "session_project_handoff_pack.v1",
                "production_mutation_performed": False,
            },
            {
                "phase": "P7",
                "schema_version": "object_extraction_preference_style_preview.v1",
                "object_count": 2,
                "artifact_preference_pack_status": "pass",
                "accepted_preference_count": 1,
                "source_evidence_ref_count": 1,
                "production_mutation_performed": False,
            },
            {
                "phase": "P8",
                "schema_version": "object_extraction_runtime_truth_preview.v1",
                "runtime_verified_count": 0,
                "runtime_unverified_count": 1,
                "permission": "allowed",
                "permission_reason": "approved_scope_present",
                "authority_write_performed": False,
                "runtime_evidence_collection_plan_schema": "source_to_candidate_runtime_evidence_collection_plan.v1",
                "runtime_evidence_collection_plan_status": "ready",
                "runtime_evidence_collection_plan_network_used": True,
                "runtime_evidence_collection_plan_mutation_allowed": True,
                "runtime_evidence_collection_plan_production_mutation_performed": True,
                "runtime_evidence_collection_plan_readiness_claim": "runtime_verified",
                "runtime_evidence_packet_template_schema": "source_to_candidate_runtime_evidence_packet_template.v1",
                "runtime_evidence_packet_template_status": "completed",
                "runtime_evidence_packet_template_network_used": True,
                "runtime_evidence_packet_template_mutation_allowed": True,
                "runtime_evidence_packet_template_production_mutation_performed": True,
                "runtime_evidence_packet_template_readiness_claim": "runtime_verified",
                "runtime_evidence_packet_template_required_field_count": 0,
                "runtime_evidence_packet_template_route_count": 0,
                "shadow_route_smoke_request_schema": "source_to_candidate_runtime_shadow_collection_request.v1",
                "shadow_route_smoke_request_status": "requested",
                "shadow_route_smoke_route_count": len(_REQUIRED_ROUTE_NAMES),
                "shadow_route_smoke_pending_routes": list(_REQUIRED_ROUTE_NAMES),
                "shadow_route_smoke_network_used": True,
                "shadow_route_smoke_mutation_allowed": True,
                "shadow_route_smoke_production_mutation_performed": True,
                "shadow_route_smoke_readiness_claim": "runtime_verified",
                "shadow_collection_registration_schema": "source_to_candidate_runtime_shadow_collection_registration.v1",
                "shadow_collection_registration_status": "completed",
                "shadow_collection_registration_run_status": "completed",
                "shadow_collection_registration_request_count": 0,
                "shadow_collection_registration_route_count": 0,
                "shadow_collection_registration_routes": [],
                "shadow_collection_registration_network_used": True,
                "shadow_collection_registration_mutation_allowed": True,
                "shadow_collection_registration_production_mutation_performed": True,
                "shadow_collection_registration_readiness_claim": "runtime_verified",
                "runtime_evidence_post_deploy_capture_packet_schema": "not_runtime_evidence",
                "runtime_evidence_post_deploy_capture_collection_mode": "local_test_replay",
                "runtime_evidence_post_deploy_capture_network_used": False,
                "runtime_evidence_post_deploy_capture_production_mutation_performed": True,
                "runtime_evidence_post_deploy_capture_report_status": "PASS",
                "runtime_evidence_post_deploy_capture_production_ready": True,
                "production_mutation_performed": False,
            },
            {
                "phase": "P9",
                "schema_version": "agent_context_product_pack.v1",
                "section_counts": {"style_preference": 1, "active_work": 1},
                "tool_hint_count": 5,
                "tool_hint_safe_target_count": 5,
                "unsafe_tool_hint_count": 0,
                "mutation_allowed": False,
                "production_mutation_performed": False,
            },
        ]
    )

    checks = {item["phase"]: item for item in result["checks"]}
    assert result["status"] == "FAIL"
    assert "P8:product_evidence_failed" in result["hard_failures"]
    assert "p8_runtime_evidence_collection_plan_used_network" in checks["P8"]["failures"]
    assert "p8_runtime_evidence_collection_plan_mutation_allowed" in checks["P8"]["failures"]
    assert "p8_runtime_evidence_collection_plan_mutated_production" in checks["P8"]["failures"]
    assert "p8_runtime_evidence_collection_plan_claims_live_evidence" in checks["P8"]["failures"]
    assert "p8_runtime_evidence_packet_template_not_ready" in checks["P8"]["failures"]
    assert "p8_runtime_evidence_packet_template_used_network" in checks["P8"]["failures"]
    assert "p8_runtime_evidence_packet_template_mutation_allowed" in checks["P8"]["failures"]
    assert "p8_runtime_evidence_packet_template_mutated_production" in checks["P8"]["failures"]
    assert "p8_post_deploy_capture_packet_missing" in checks["P8"]["failures"]
    assert "p8_post_deploy_capture_collection_mode_missing" in checks["P8"]["failures"]
    assert "p8_post_deploy_capture_network_not_used" in checks["P8"]["failures"]
    assert "p8_post_deploy_capture_mutated_production" in checks["P8"]["failures"]
    assert "p8_post_deploy_capture_unexpected_report_status" in checks["P8"]["failures"]
    assert "p8_post_deploy_capture_claims_production_ready" in checks["P8"]["failures"]
    assert "p8_runtime_evidence_packet_template_claims_live_evidence" in checks["P8"]["failures"]
    assert "p8_runtime_evidence_packet_template_fields_missing" in checks["P8"]["failures"]
    assert "p8_runtime_evidence_packet_template_routes_missing" in checks["P8"]["failures"]
    assert "p8_shadow_route_smoke_used_network" in checks["P8"]["failures"]
    assert "p8_shadow_route_smoke_mutation_allowed" in checks["P8"]["failures"]
    assert "p8_shadow_route_smoke_mutated_production" in checks["P8"]["failures"]
    assert "p8_shadow_route_smoke_claims_live_evidence" in checks["P8"]["failures"]
    assert "p8_shadow_collection_registration_not_ready" in checks["P8"]["failures"]
    assert "p8_shadow_collection_registration_claims_run" in checks["P8"]["failures"]
    assert "p8_shadow_collection_registration_requests_missing" in checks["P8"]["failures"]
    assert "p8_shadow_collection_registration_routes_missing" in checks["P8"]["failures"]
    assert "p8_shadow_collection_registration_used_network" in checks["P8"]["failures"]
    assert "p8_shadow_collection_registration_mutation_allowed" in checks["P8"]["failures"]
    assert "p8_shadow_collection_registration_mutated_production" in checks["P8"]["failures"]
    assert "p8_shadow_collection_registration_claims_live_evidence" in checks["P8"]["failures"]


def test_product_evidence_summary_fails_when_p9_active_work_is_missing():
    result = evaluate_product_evidence_summary(
        [
            {
                "phase": "P6",
                "schema_version": "object_extraction_session_project_rollup_preview.v1",
                "object_count": 8,
                "edge_count": 16,
                "evidence_count": 1,
                "handoff_pack_schema": "session_project_handoff_pack.v1",
                "production_mutation_performed": False,
            },
            {
                "phase": "P7",
                "schema_version": "object_extraction_preference_style_preview.v1",
                "object_count": 2,
                "artifact_preference_pack_status": "pass",
                "accepted_preference_count": 1,
                "source_evidence_ref_count": 1,
                "production_mutation_performed": False,
            },
            {
                "phase": "P8",
                "schema_version": "object_extraction_runtime_truth_preview.v1",
                "runtime_unverified_count": 1,
                "permission": "allowed",
                "permission_reason": "approved_scope_present",
                "authority_write_performed": False,
                "production_mutation_performed": False,
            },
            {
                "phase": "P9",
                "schema_version": "agent_context_product_pack.v1",
                "section_counts": {"style_preference": 1, "active_work": 0},
                "tool_hint_count": 5,
                "tool_hint_safe_target_count": 5,
                "unsafe_tool_hint_count": 0,
                "mutation_allowed": False,
                "production_mutation_performed": False,
            },
        ]
    )

    checks = {item["phase"]: item for item in result["checks"]}
    assert result["status"] == "FAIL"
    assert "P9:product_evidence_failed" in result["hard_failures"]
    assert "p9_active_work_section_missing" in checks["P9"]["failures"]


def test_product_evidence_summary_fails_when_p9_tool_hints_are_unsafe():
    result = evaluate_product_evidence_summary(
        [
            {
                "phase": "P9",
                "schema_version": "agent_context_product_pack.v1",
                "section_counts": {"style_preference": 1, "active_work": 1},
                "tool_hint_count": 5,
                "tool_hint_safe_target_count": 5,
                "unsafe_tool_hint_count": 1,
                "mutation_allowed": False,
                "production_mutation_performed": False,
            },
        ]
    )

    checks = {item["phase"]: item for item in result["checks"]}
    assert checks["P9"]["result"] == "FAIL"
    assert "P9:product_evidence_failed" in result["hard_failures"]
    assert "p9_tool_hint_safety_violations" in checks["P9"]["failures"]
