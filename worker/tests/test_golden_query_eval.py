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
            "verification": {"freshness_checked": [{"evidence_id": "ev:test"}]},
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
    assert report["product_evidence_status"] == "PASS"
    assert "production_quality_not_green" in report["goal_completion_blockers"]
    assert "live_runtime_read_path_unverified" in report["goal_completion_blockers"]
    checks = {item["phase"]: item for item in report["product_evidence_checks"]}
    assert set(checks) == {"P6", "P7", "P8", "P9"}
    assert all(item["result"] == "PASS" for item in checks.values())
    evidence = {item["phase"]: item for item in report["product_evidence_summary"]}
    assert set(evidence) == {"P6", "P7", "P8", "P9"}
    assert evidence["P6"]["schema_version"] == "object_extraction_session_project_rollup_preview.v1"
    assert evidence["P6"]["object_count"] >= 5
    assert evidence["P6"]["edge_count"] >= 6
    assert evidence["P6"]["evidence_count"] >= 1
    assert evidence["P7"]["schema_version"] == "object_extraction_preference_style_preview.v1"
    assert evidence["P7"]["artifact_preference_pack_status"] == "pass"
    assert evidence["P8"]["schema_version"] == "object_extraction_runtime_truth_preview.v1"
    assert evidence["P8"]["runtime_unverified_count"] == 1
    assert evidence["P8"]["permission"] == "allowed"
    assert evidence["P8"]["permission_reason"] == "approved_scope_present"
    assert evidence["P8"]["authority_write_performed"] is False
    assert evidence["P9"]["schema_version"] == "agent_context_product_pack.v1"
    assert evidence["P9"]["section_counts"]["style_preference"] >= 1
    assert evidence["P9"]["section_counts"]["active_work"] >= 1
    assert evidence["P9"]["tool_hint_count"] >= 5
    assert evidence["P9"]["mutation_allowed"] is False
    assert all(item["production_mutation_performed"] is False for item in evidence.values())

    phase_progress = {item["phase"]: item for item in report["phase_progress"]}
    assert phase_progress["P4"]["quality_result"] == "PASS_WITH_GAPS"
    assert phase_progress["P5"]["state"] == "in_progress"
    assert phase_progress["P6"]["state"] == "local_validated"
    assert phase_progress["P9"]["state"] == "local_validated"


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
    assert "P6:product_evidence_failed" in result["hard_failures"]
    assert "P7:product_evidence_missing" in result["hard_failures"]
    assert "P8:product_evidence_failed" in result["hard_failures"]
    checks = {item["phase"]: item for item in result["checks"]}
    assert "p6_session_rollup_incomplete" in checks["P6"]["failures"]
    assert "p8_production_mutation_performed" in checks["P8"]["failures"]


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
                "mutation_allowed": False,
                "production_mutation_performed": False,
            },
        ]
    )

    checks = {item["phase"]: item for item in result["checks"]}
    assert result["status"] == "FAIL"
    assert "P9:product_evidence_failed" in result["hard_failures"]
    assert "p9_active_work_section_missing" in checks["P9"]["failures"]
