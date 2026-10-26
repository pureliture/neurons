"""모델을 호출하기 전에 전체 독립 평가 자료의 입력 계약을 확인한다."""

import json
from pathlib import Path
import runpy

import pytest


def test_freeze_rejects_redacted_source_before_execution():
    evaluator = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "eval/correction_semantic_eval.py")
    )
    fixture = {
        "cases": [
            {
                "id": "invalid-source",
                "content": "authorization: Bearer synthetic_value_abcdefghijklmnopqrstuvwxyz",
                "claims": [["a", "이 파일의 값은 a다."], ["b", "이 파일의 값은 b다."]],
            }
        ]
    }
    with pytest.raises(ValueError, match="evaluation source changed by redaction"):
        evaluator["freeze"](fixture)


def test_fresh_plan_excludes_all_earlier_samples():
    root = Path(__file__).resolve().parents[1] / "eval"
    evaluator = runpy.run_path(str(root / "correction_semantic_eval.py"))
    fixture = json.loads((root / "correction_semantic_v5_holdout.json").read_text())
    plan = evaluator["freeze"](fixture)
    from agent_knowledge.public_safe_util import hash_payload

    earlier = json.loads((root / "correction_semantic_v4_holdout.json").read_text())
    assert all(
        hash_payload(case) in plan["development_case_hashes"]
        for case in earlier["cases"]
    )
    assert not {hash_payload(case) for case in fixture["cases"]} & set(
        plan["development_case_hashes"]
    )
    assert len(fixture["cases"]) == 40


def test_source_read_failure_is_retained_as_failed_row_without_model_call(monkeypatch):
    root = Path(__file__).resolve().parents[1] / "eval"
    evaluator = runpy.run_path(str(root / "correction_semantic_eval.py"))
    fixture = json.loads((root / "correction_semantic_v5_holdout.json").read_text())
    from agent_knowledge.correction_capabilities import CapabilityUnavailable

    def unavailable(self):
        raise CapabilityUnavailable("source_read_changed")

    monkeypatch.setattr(evaluator["GitRepositoryReader"], "read", unavailable)
    row = evaluator["evaluate_case"](
        fixture["cases"][0], client=object(), policy=object()
    )
    assert row["selection"] is None and row["reason_code"] == "source_read_changed"
    assert row["case"] == fixture["cases"][0]
