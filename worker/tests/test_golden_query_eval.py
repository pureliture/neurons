from agent_knowledge.llm_brain_core.golden_query_eval import (
    GOLDEN_QUERIES,
    build_baseline_golden_query_report,
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
