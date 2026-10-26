"""choice 확률의 완전성과 모델 치환 거부를 검사한다."""
import importlib.util
from pathlib import Path
import sys
import json

import pytest
from agent_knowledge.correction_relation import CorrectionRelationClient
from agent_knowledge.correction_judgment import JudgmentUnavailable
from agent_knowledge.public_safe_util import hash_payload


folder = Path(__file__).resolve().parents[1] / "eval"
sys.path.insert(0, str(folder))
try:
    spec = importlib.util.spec_from_file_location("correction_relation_pilot", folder / "correction_relation_pilot.py")
    pilot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pilot)
finally:
    sys.path.remove(str(folder))


def response(**overrides):
    return {"model": pilot.DEFAULT_CORRECTION_MODEL + "-20261016", "answers": {"relation": {
        "type": "choice", "choice": "supported", "probabilities": {
            "supported": .9, "contradicted": .01, "insufficient": .08, "unrelated": .01}}}, **overrides}


def test_relation_result_preserves_normalized_alternatives_and_selected_identity():
    result = pilot.parse_response(response())
    assert result["probabilities"] == {"relevant": .99, "supports": .9, "contradicts": .01}
    assert result["relation"] == "supported" and len(result["relation_probabilities"]) == 4


@pytest.mark.parametrize("change", ["model", "missing", "nan", "bool", "sum", "choice", "type"])
def test_invalid_or_substituted_choice_cannot_pass(change):
    value = response()
    answer = value["answers"]["relation"]
    if change == "model": value["model"] = "unrequested-model"
    if change == "missing": answer["probabilities"].pop("unrelated")
    if change == "nan": answer["probabilities"]["supported"] = float("nan")
    if change == "bool": answer["probabilities"]["supported"] = True
    if change == "sum": answer["probabilities"]["supported"] = .5
    if change == "choice": answer["choice"] = "contradicted"
    if change == "type": answer["type"] = "noul"
    with pytest.raises((ValueError, KeyError, TypeError)):
        pilot.parse_response(value)


def test_typed_relation_client_binds_source_policy_and_question_without_copying_text():
    calls = []
    def transport(url, payload, key, timeout):
        calls.append(json.loads(payload))
        return 200, json.dumps(response()).encode()
    client = CorrectionRelationClient(api_key="synthetic-auth", transport=transport)
    state = {"scope": "이번 검사", "hypothesis": "현재 기능이 없다", "premise": "현재 source에서 기능 부재를 확인했다"}
    first = client.evaluate(state, policy_version="p1", source_binding_hash=hash_payload("source-1"))
    second = client.evaluate(state, policy_version="p1", source_binding_hash=hash_payload("source-2"))
    assert first["input_hash"] != second["input_hash"]
    assert calls[0]["questions"] == pilot.questions() and calls[0]["state"] == state
    assert "synthetic-auth" not in json.dumps(first) and state["premise"] not in str(first)


@pytest.mark.parametrize("raw", [b'{"model":"a","model":"b"}', b'not-json', b'X' * 131073])
def test_typed_relation_client_rejects_bad_json_without_provider_body(raw):
    client = CorrectionRelationClient(api_key="synthetic-auth", transport=lambda *args: (200, raw))
    with pytest.raises(JudgmentUnavailable, match="^judgment_response_invalid$"):
        client.evaluate({"scope": "검사", "hypothesis": "주장", "premise": "관측"},
            policy_version="p1", source_binding_hash=hash_payload("source"))
