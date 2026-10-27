"""대상 식별 모델의 전송/응답 계약. 실제 모델 정확도 검사는 별도다."""

import copy
import json

import pytest

from agent_knowledge.correction_target import candidate_state
from agent_knowledge.correction_target_identity import (
    CorrectionTargetClient,
    parse_response,
    DEFAULT_CORRECTION_MODEL,
)
from agent_knowledge.correction_judgment import JudgmentUnavailable
from agent_knowledge.public_safe_util import sha256_text
from test_correction_target import snapshot


def response():
    return {
        "model": DEFAULT_CORRECTION_MODEL + "-20261016",
        "answers": {
            "identity": {
                "type": "choice",
                "choice": "same_fact",
                "probabilities": {
                    "same_fact": 0.98,
                    "different_fact": 0.01,
                    "uncertain": 0.01,
                },
            }
        },
    }


@pytest.mark.parametrize("damage", ["bool", "nan", "sum", "choice", "missing", "model"])
def test_invalid_target_distribution_or_model_is_rejected(damage):
    value = response()
    answer = value["answers"]["identity"]
    if damage == "bool":
        answer["probabilities"]["same_fact"] = True
    if damage == "nan":
        answer["probabilities"]["same_fact"] = float("nan")
    if damage == "sum":
        answer["probabilities"]["same_fact"] = 0.5
    if damage == "choice":
        answer["choice"] = "different_fact"
    if damage == "missing":
        value["answers"] = {}
    if damage == "model":
        value["model"] = "other-model"
    with pytest.raises((ValueError, KeyError)):
        parse_response(value)


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "judgment_auth_failed"),
        (403, "judgment_auth_failed"),
        (402, "judgment_quota_exceeded"),
        (429, "judgment_rate_limited"),
        (404, "judgment_model_unavailable"),
        (500, "judgment_provider_error"),
    ],
)
def test_provider_error_never_echoes_body(status, code):
    data = snapshot("교정 상태")
    client = CorrectionTargetClient(
        api_key="unit",
        transport=lambda *args: (status, b"credential-containing provider body"),
    )
    with pytest.raises(JudgmentUnavailable, match=code) as error:
        client.evaluate(
            candidate_state(data, data["candidates"][0]),
            policy_version="unit-v1",
            source_binding_hash=sha256_text("binding"),
        )
    assert str(error.value) == code


def test_transport_exception_is_redacted():
    data = snapshot("교정 상태")

    def unavailable(*args):
        raise RuntimeError("private credential must not be included")

    client = CorrectionTargetClient(api_key="unit", transport=unavailable)
    with pytest.raises(JudgmentUnavailable, match="judgment_transport_error") as error:
        client.evaluate(
            candidate_state(data, data["candidates"][0]),
            policy_version="unit-v1",
            source_binding_hash=sha256_text("binding"),
        )
    assert str(error.value) == "judgment_transport_error"


def test_duplicate_response_keys_are_not_accepted():
    data = snapshot("교정 상태")
    body = (
        json.dumps(response())
        .replace('"answers":', '"answers": {}, "answers":', 1)
        .encode()
    )
    client = CorrectionTargetClient(api_key="unit", transport=lambda *args: (200, body))
    with pytest.raises(JudgmentUnavailable, match="judgment_response_invalid"):
        client.evaluate(
            candidate_state(data, data["candidates"][0]),
            policy_version="unit-v1",
            source_binding_hash=sha256_text("binding"),
        )


def test_request_data_does_not_change_question_and_original_metadata_remains_unchanged():
    data = snapshot("교정 상태")
    state = candidate_state(data, data["candidates"][0])
    state["candidate_field"] = json.dumps(
        {"path": "typed_payload.status_value", "value": "행1\n행2"}
    )
    before = copy.deepcopy(state)
    sent = []
    client = CorrectionTargetClient(
        api_key="unit",
        transport=lambda *args: (
            sent.append(json.loads(args[1])) or 200,
            json.dumps(response()).encode(),
        ),
    )
    result = client.evaluate(
        state, policy_version="unit-v1", source_binding_hash=sha256_text("binding")
    )
    assert state == before and result["identity"] == "same_fact"
    assert sent[0]["state"] == before
    assert "교정 상태" not in sent[0]["questions"]["identity"]["instructions"]
