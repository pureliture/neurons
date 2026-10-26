"""고정 identity 변경 시 새 의미 판단 호출을 차단한다."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest


folder = Path(__file__).resolve().parents[1] / "eval"
sys.path.insert(0, str(folder))
try:
    spec = importlib.util.spec_from_file_location("correction_relation_v4_eval", folder / "correction_relation_v4_eval.py")
    evaluation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluation)
finally:
    sys.path.remove(str(folder))


def test_changed_profile_identity_fails_before_auth_or_any_provider_call(tmp_path, monkeypatch):
    profile = {"schema_version": "correction_relation_profile.v4", "identity": {
        **evaluation.identity(), "connector_hash": "sha256:" + "0" * 64}}
    source = tmp_path / "profile.json"
    source.write_text(json.dumps(profile))
    def forbidden(**kwargs):
        pytest.fail("changed profile must fail before client construction")
    monkeypatch.setattr(evaluation.connector, "CorrectionRelationClient", forbidden)
    with pytest.raises(ValueError, match="profile changed"):
        evaluation.main(["--phase", "holdout", "--profile", str(source),
            "--key-file", str(tmp_path / "absent-secret"), "--output", str(tmp_path / "report.json")])
    assert not (tmp_path / "report.json").exists()


def test_previous_independent_data_is_development_and_new_cases_are_distinct():
    old = []
    for name in ("correction_judgment_cases.json", "correction_judgment_v3_holdout.json"):
        old.extend(json.loads((folder / name).read_text())["cases"])
    new = json.loads((folder / "correction_relation_v4_holdout.json").read_text())
    assert len(old) == 76 and len(new["cases"]) == 40
    assert sum(case["expected"] == "supported" for case in new["cases"]) == 16
    for key in ("id", "proposed_value", "evidence"):
        assert not ({case[key] for case in old} & {case[key] for case in new["cases"]})
    assert new["release_criterion"] == evaluation.fitting.CRITERION


def test_old_experimental_response_without_source_binding_cannot_be_frozen():
    with pytest.raises(ValueError, match="development identity mismatch"):
        evaluation.freeze({"schema_version": "correction_relation_pilot.v4", "phase": "development_only",
            "question_version": "correction_relation.v4-pilot"})
