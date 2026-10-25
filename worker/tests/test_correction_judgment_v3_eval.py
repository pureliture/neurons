"""독립 확인 결과를 보기 전의 calibration과 전부 보류 거부를 검사한다."""
import importlib.util
import json
from pathlib import Path

import pytest


path = Path(__file__).resolve().parents[1] / "eval" / "correction_judgment_v3_eval.py"
spec = importlib.util.spec_from_file_location("correction_judgment_v3_eval", path)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def row(label, relevant, support, contradiction):
    return {"expected": label, "result": {"probabilities": {
        "relevant": relevant, "supports": support, "contradicts": contradiction}}}


def test_fit_uses_one_profile_without_case_exceptions_and_requires_normal_changes():
    rows = [row("supported", .94, .88, .03), row("supported", .92, .86, .05),
        row("insufficient", .99, .80, .01), row("contradicted", .99, .97, .98)]
    profile = evaluation.fit_thresholds(rows, profile_id="synthetic-dev")
    assert (profile.relevant_min, profile.support_min, profile.contradiction_max) == (.9, .85, .05)
    summary = evaluation.summarize(rows, profile)
    assert summary["gate_passed"] and summary["positive_permits"] == 2 and summary["wrong_permits"] == 0


@pytest.mark.parametrize("rows", [
    [row("supported", .4, .4, .4), row("insufficient", .99, .01, .01)],
    [row("supported", .9, .9, .01), row("insufficient", .95, .95, .01)],
    [row("supported", .9, .9, .01), {"expected": "insufficient", "error_code": "timeout"}],
])
def test_all_hold_unsafe_or_incomplete_development_does_not_generate_profile(rows):
    with pytest.raises(ValueError, match="criterion not met"):
        evaluation.fit_thresholds(rows, profile_id="synthetic-dev")


def test_new_holdout_is_larger_and_does_not_reuse_development_text_or_ids():
    folder = path.parent
    previous = json.loads((folder / "correction_judgment_cases.json").read_text())["cases"]
    document = json.loads((folder / "correction_judgment_v3_holdout.json").read_text())
    cases = document["cases"]
    assert len(cases) == 48 and sum(case["expected"] == "supported" for case in cases) == 16
    assert len({case["id"] for case in cases}) == len(cases)
    for key in ("id", "evidence", "proposed_value"):
        assert not ({case[key] for case in cases} & {case[key] for case in previous})
    assert document["release_criterion"] == evaluation.CRITERION


def test_holdout_rejects_changed_connector_before_loading_auth_or_calling_model(tmp_path, monkeypatch):
    frozen = {"schema_version": "correction_judgment_profile.v3", "question_version": "old-questions",
        "connector_hash": "sha256:" + "0" * 64}
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(frozen))
    def forbidden(**kwargs):
        pytest.fail("invalid profile must fail before any model client is constructed")
    monkeypatch.setattr(evaluation, "CorrectionJudgmentClient", forbidden)
    with pytest.raises(ValueError, match="identity mismatch"):
        evaluation.main(["--phase", "holdout", "--key-file", str(tmp_path / "absent-key"),
            "--output", str(tmp_path / "report.json"), "--profile", str(profile)])
    assert not (tmp_path / "report.json").exists()
