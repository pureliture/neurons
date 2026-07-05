from __future__ import annotations

from agent_knowledge.session_memory.eval_readiness import (
    EVAL_READINESS_BLOCKER_CODES,
    VERIFICATION_LEVEL_CODES,
    build_eval_readiness_report,
)


def test_eval_readiness_report_classifies_worker_eval_as_dev_only_harness():
    report = build_eval_readiness_report()

    assert report["schema_version"] == "llm_brain_eval_readiness.v1"
    assert report["lane"] == "worker_eval"
    assert report["classification"] == "dev_only_harness"
    assert report["product_readiness_gate"] is False
    assert report["runtime_verified"] is False
    assert report["support_claim"] == "authored"
    assert report["approval_required_before_live_mutation"] is True
    assert report["open_blocker_codes"] == list(EVAL_READINESS_BLOCKER_CODES)


def test_eval_readiness_report_names_required_r1_blockers():
    assert EVAL_READINESS_BLOCKER_CODES == (
        "live_mining_provider",
        "scheduler_install",
        "machine_origin",
        "candidates_persistence",
        "runtime_tripwire",
        "golden_content_completion",
    )


def test_eval_readiness_report_separates_verification_levels():
    report = build_eval_readiness_report()

    assert VERIFICATION_LEVEL_CODES == (
        "api_shape_only",
        "api_queue_smoke",
        "local_unit_contract",
        "read_only_runtime",
        "full_e2e_business",
    )
    assert [level["code"] for level in report["verification_levels"]] == list(
        VERIFICATION_LEVEL_CODES
    )
    assert all(level["runtime_verified"] is False for level in report["verification_levels"])
    full_e2e = report["verification_levels"][-1]
    assert full_e2e["code"] == "full_e2e_business"
    assert full_e2e["requires"] == [
        "persistence",
        "mirror_or_index",
        "graph_or_projection",
        "recall_read_path",
    ]
