"""joint choice 분포에 독립 반증 문턱을 다시 적용하지 않으며 cache 범위를 지킨다."""
from copy import deepcopy
import json

import pytest

from agent_knowledge.correction_relation import CorrectionRelationClient, DEFAULT_CORRECTION_MODEL, questions
from agent_knowledge.correction_relation_policy import RelationSupportPolicy, fit_support_threshold, support_evaluation
from agent_knowledge.public_safe_util import hash_payload


def observation(scores=None):
    scores = scores or {"supported": .83, "contradicted": .09, "insufficient": .06, "unrelated": .02}
    response = {"model": DEFAULT_CORRECTION_MODEL + "-20261016", "answers": {"relation": {
        "type": "choice", "choice": max(scores, key=scores.get), "probabilities": scores}}}
    client = CorrectionRelationClient(api_key="synthetic-auth", transport=lambda *args: (200, json.dumps(response).encode()))
    return client.evaluate({"scope": "이번 검사", "hypothesis": "지연된 관측이 값을 되돌리지 않았다",
        "premise": "과거 근거만 추가되고 현재 값은 그대로였다"}, policy_version="fixture-policy",
        source_binding_hash=hash_payload("fixture-source"))


def policy():
    return RelationSupportPolicy(.8, "synthetic-validated-profile", DEFAULT_CORRECTION_MODEL + "-20261016", hash_payload(questions()))


def permits(result):
    return policy().permits(result, expected_input_hash=observation()["input_hash"],
        source_binding_hash=hash_payload("fixture-source"), policy_version="fixture-policy")


def test_joint_supported_probability_is_used_without_duplicate_derived_gates():
    assert permits(observation())
    opposed = observation({"supported": .1, "contradicted": .8, "insufficient": .08, "unrelated": .02})
    assert not permits(opposed)


@pytest.mark.parametrize("field,value", [
    ("input_hash", hash_payload("different-task")), ("source_binding_hash", hash_payload("different-source")),
    ("policy_version", "old-policy"), ("question_version", "old-question"),
    ("resolved_model", DEFAULT_CORRECTION_MODEL + "-different-revision"), ("requested_model", "other-model"),
])
def test_other_snapshot_or_policy_scores_cannot_authorize(field, value):
    result = deepcopy(observation())
    result[field] = value
    assert not permits(result)


def test_evaluation_does_not_fit_an_all_hold_or_incomplete_profile():
    rows = [{"expected": "supported", "result": observation()},
        {"expected": "insufficient", "result": observation({"supported": .1, "contradicted": .1, "insufficient": .7, "unrelated": .1})}]
    assert fit_support_threshold(rows) == .8
    assert support_evaluation(rows, .8)["positive_permits"] == 1
    with pytest.raises(ValueError, match="criterion not met"):
        fit_support_threshold([{**rows[0], "result": rows[1]["result"]}, rows[1]])
    with pytest.raises(ValueError, match="criterion not met"):
        fit_support_threshold([rows[0], {"expected": "insufficient", "error_code": "timeout"}])
