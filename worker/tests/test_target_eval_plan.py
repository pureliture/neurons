"""개발에 사용한 모델 표본을 독립 평가로 다시 사용하지 않는다."""

from pathlib import Path
import runpy

import pytest

from agent_knowledge.correction_target import validate_report
from agent_knowledge import correction_target as target
from agent_knowledge.public_safe_util import hash_payload
from test_correction_target import client, policy, MODEL


def test_new_target_plan_excludes_development_and_previous_failed_holdout():
    root = Path(__file__).resolve().parents[1] / "eval"
    evaluator = runpy.run_path(str(root / "correction_target_eval.py"))
    cases = evaluator["cases"](root / "correction_target_v3_holdout.json")
    plan = evaluator["freeze"](cases)
    assert len(cases) == 40
    assert sum(case["expected_memory_id"] is not None for case in cases) == 20
    assert "target-h1-01" in plan["development_case_ids"]
    assert "dev-target-1" in plan["development_case_ids"]
    previous = evaluator["cases"](root / "correction_target_holdout.json")
    with pytest.raises(ValueError, match="independent target cases required"):
        evaluator["freeze"](previous)


def test_development_execution_is_never_a_valid_profile():
    from agent_knowledge.correction_target import runtime_identity, VALIDATION_SCHEMA, THRESHOLD
    with pytest.raises(ValueError, match="target profile identity required"):
        validate_report({"schema_version": VALIDATION_SCHEMA, "identity": runtime_identity(),
            "execution": "development_openrouter", "threshold": THRESHOLD})


@pytest.mark.parametrize("normal_permits,wrong,accepted", [(16, False, True), (15, False, False),
    (20, True, False), (0, False, False)])
def test_target_profile_requires_normal_coverage_and_zero_wrong_permits(normal_permits, wrong, accepted):
    """확률 응답 대역으로 profile guard만 검사한다. 실제 모델 증거로 사용하지 않는다."""
    root = Path(__file__).resolve().parents[1] / "eval"
    evaluator = runpy.run_path(str(root / "correction_target_eval.py"))
    cases = evaluator["cases"](root / "correction_target_v3_holdout.json")
    plan = evaluator["freeze"](cases)
    rows = []
    positive = 0
    for case in cases:
        expected = case["expected_memory_id"]
        titles = {"title: " + row["title"] + "\n" for row in case["snapshot"]["candidates"]
            if row["memory_id"] == expected}
        permit = expected is not None and positive < normal_permits
        positive += expected is not None
        if wrong and case["id"] == "target-h3-21":
            label = lambda text: "supported"
        elif permit:
            label = lambda text: "supported" if any(text.startswith(title) for title in titles) else "unrelated"
        else:
            label = lambda text: "insufficient"
        selection = target.select_target(case["snapshot"], client=client(label).value,
            policy=policy(), policy_version="target-eval.v1")
        rows.append({"case": case, "case_hash": hash_payload(case), "snapshot": case["snapshot"],
            "selection": selection})
    report = {"schema_version": target.VALIDATION_SCHEMA, "identity": target.runtime_identity(),
        "execution": "live_openrouter", "threshold": target.THRESHOLD, "resolved_model": MODEL,
        "development_case_hashes": plan["development_case_hashes"],
        "development_case_ids": plan["development_case_ids"], "results": rows}
    if accepted:
        assert validate_report(report)["positive_permits"] == 16
    else:
        with pytest.raises(ValueError, match="target independent validation criterion not met"):
            validate_report(report)
