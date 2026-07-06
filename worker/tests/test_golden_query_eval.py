from agent_knowledge.llm_brain_core.golden_query_eval import (
    GOLDEN_QUERIES,
    REQUIRED_QUALITY_AXES,
    build_baseline_golden_query_report,
    build_phase_golden_query_coverage_report,
    evaluate_object_pack_response,
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
        "verification": {"unverified": [{"evidence_id": "ev:inventory:legacy"}]},
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
    assert "approved_production_pilot_missing" in phases["P4"]["gaps"]
    assert phases["P6"]["result"] == "in_progress"
    assert "live_multi_device_rollup_unproven" in phases["P6"]["gaps"]
    assert phases["P7"]["result"] == "in_progress"
    assert phases["P7"]["golden_query_family"] == "code style drift"
    assert "accepted_preference_context_pack_live_unproven" in phases["P7"]["gaps"]
