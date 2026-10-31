"""모델 응답을 authority로 오인하지 않는 typed 연결 계약 검사."""
import json

import pytest

from agent_knowledge.correction_judgment import (
    CorrectionJudgmentClient, DEFAULT_CORRECTION_MODEL, DECISIONS_ENDPOINT,
    JudgmentThresholds, JudgmentUnavailable, correction_questions,
)


def state():
    return {"fact": "기능의 구현 상태", "proposed_value": "구현됨",
        "evidence": [{"text": "현재 source에서 구현을 확인했다.", "revision": "revision-a"}]}


def response(**overrides):
    return {"model": DEFAULT_CORRECTION_MODEL + "-20261016", "answers": {
        "relevant": {"type": "noul", "noul": .99},
        "supports": {"type": "noul", "noul": .99},
        "contradicts": {"type": "noul", "noul": .01},
    }, "usage": {"input_tokens": 10}, "provider": "synthetic", **overrides}


def client_with(value):
    return CorrectionJudgmentClient(api_key="synthetic-credential", transport=lambda *args: (200, json.dumps(value).encode()))


def test_real_protocol_shape_and_independent_propositions_are_bound_to_fingerprint():
    requests = []
    def send(url, body, key, timeout):
        requests.append(json.loads(body))
        assert url == DECISIONS_ENDPOINT and timeout == 20
        return 200, json.dumps(response()).encode()
    client = CorrectionJudgmentClient(api_key="synthetic-credential", transport=send)
    first = client.evaluate(state(), policy_version="policy-a")
    assert requests[0]["questions"] == correction_questions()
    assert first["probabilities"]["supports"] == .99
    assert "synthetic-credential" not in json.dumps(first)
    same = client.evaluate(state(), policy_version="policy-a")
    assert same["input_hash"] == first["input_hash"]
    changed = client.evaluate(state(), policy_version="policy-b")
    assert changed["input_hash"] != first["input_hash"]
    revised = state(); revised["evidence"][0]["revision"] = "revision-b"
    assert client.evaluate(revised, policy_version="policy-a")["input_hash"] != first["input_hash"]


@pytest.mark.parametrize("change", [
    {"model": "unexpected-fallback-model"}, {"answers": {}},
    {"answers": {name: {"type": "choice", "choice": "true"} for name in correction_questions()}},
    {"answers": {name: {"type": "noul", "noul": True} for name in correction_questions()}},
    {"answers": {name: {"type": "noul", "noul": float('nan')} for name in correction_questions()}},
    {"answers": {name: {"type": "noul", "noul": 1.01} for name in correction_questions()}},
])
def test_invalid_or_substituted_provider_results_cannot_authorize_a_change(change):
    with pytest.raises(JudgmentUnavailable, match="judgment_response_invalid"):
        client_with(response(**change)).evaluate(state(), policy_version="policy-a")


@pytest.mark.parametrize("raw", [b'not-json', b'{"answers":{},"answers":{}}', b'X' * 131073])
def test_malformed_duplicate_or_oversized_response_is_not_used(raw):
    client = CorrectionJudgmentClient(api_key="synthetic-credential", transport=lambda *args: (200, raw))
    with pytest.raises(JudgmentUnavailable, match="judgment_response_invalid"):
        client.evaluate(state(), policy_version="policy-a")


@pytest.mark.parametrize("status,reason,retryable", [
    (401, "judgment_auth_failed", False), (403, "judgment_auth_failed", False),
    (402, "judgment_quota_exceeded", False), (429, "judgment_rate_limited", True),
    (404, "judgment_model_unavailable", False), (503, "judgment_provider_error", True),
])
def test_provider_failure_stays_typed_without_secret_or_implicit_fallback(status, reason, retryable):
    client = CorrectionJudgmentClient(api_key="synthetic-credential", transport=lambda *args: (status, b'private provider error'))
    with pytest.raises(JudgmentUnavailable) as caught:
        client.evaluate(state(), policy_version="policy-a")
    assert str(caught.value) == reason and caught.value.retryable is retryable


def test_transport_exceptions_do_not_surface_provider_credentials():
    def fail(*args):
        raise RuntimeError("Bearer synthetic-credential private provider body")
    client = CorrectionJudgmentClient(api_key="synthetic-credential", transport=fail)
    with pytest.raises(JudgmentUnavailable) as caught:
        client.evaluate(state(), policy_version="policy-a")
    assert str(caught.value) == "judgment_transport_error"


def test_explicit_policy_requires_all_independent_conditions():
    policy = JudgmentThresholds(.95, .95, .05, "synthetic-policy-test")
    approved = client_with(response()).evaluate(state(), policy_version="policy-a")
    assert policy.permits(approved)
    for name, value in [("relevant", .6), ("supports", .9), ("contradicts", .98)]:
        inconsistent = {**approved, "probabilities": {**approved["probabilities"], name: value}}
        assert not policy.permits(inconsistent)
    assert not policy.permits({})
    with pytest.raises(ValueError):
        JudgmentThresholds(.4, .4, .6, "invalid")
