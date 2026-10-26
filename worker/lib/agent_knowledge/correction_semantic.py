"""실제 source 범위와 독립 평가에 결속한 의미 기반 값 선택.

모델은 서버가 등록한 값별 주장의 지지 여부만 판단한다. target, source 권한,
범위, 현재성, profile 활성화와 authority commit은 코드가 결정한다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path

from . import correction_capabilities as capabilities
from . import correction_judgment as transport
from . import correction_relation as relation
from . import correction_relation_policy as policies
from .public_safe_util import ensure_public_safe, hash_payload, require_sha256, sha256_text


GATE_VERSION = "capability_semantic_selection.v1"
VALIDATION_SCHEMA = "semantic_admission_validation.v1"
EVALUATION_POLICY = "semantic-hybrid-eval.v1"
SUPPORT_MIN = .8


def runtime_identity():
    return {"gate_version": GATE_VERSION, "question_version": relation.QUESTION_VERSION,
        "questions_hash": hash_payload(relation.questions()), "model": relation.DEFAULT_CORRECTION_MODEL,
        "source_gate_hash": sha256_text(Path(capabilities.__file__).read_text()),
        "client_hash": sha256_text(Path(relation.__file__).read_text()),
        "transport_hash": sha256_text(Path(transport.__file__).read_text()),
        "policy_hash": sha256_text(Path(policies.__file__).read_text()),
        "coordinator_hash": sha256_text(Path(__file__).with_name("fact_correction_coordinator.py").read_text()),
        "selection_hash": sha256_text(Path(__file__).read_text())}


def validate_claims(claims):
    if (not isinstance(claims, (list, tuple)) or not 2 <= len(claims) <= 4
            or any(not isinstance(pair, (list, tuple)) or len(pair) != 2 for pair in claims)):
        raise ValueError("two to four explicit server value claims required")
    if any(not isinstance(text, str) or not text.strip() or text != text.strip()
            or len(text) > (2048 if index else 128) for pair in claims for index, text in enumerate(pair)):
        raise ValueError("bounded server value claims required")
    if len({pair[0] for pair in claims}) != len(claims) or len({pair[1] for pair in claims}) != len(claims):
        raise ValueError("distinct server values and claims required")
    ensure_public_safe(claims, "SemanticFactClaims")


def _state(observed, scope_description, hypothesis):
    return {"scope": scope_description, "hypothesis": hypothesis, "premise": json.dumps({
        "observed_capability": observed.capability.value, "observed_value": observed.value}, ensure_ascii=False)}


def _fingerprint(observed, scope_description, hypothesis, policy_version):
    binding = hash_payload(asdict(observed))
    return hash_payload({"payload": {"model": relation.DEFAULT_CORRECTION_MODEL,
        "state": _state(observed, scope_description, hypothesis), "questions": relation.questions()},
        "question_version": relation.QUESTION_VERSION, "policy_version": policy_version,
        "source_binding_hash": binding})


def verify_selection(*, observed, requirement, scope_description, claims, policy, policy_version, selection):
    """저장된 응답도 매번 현재 source/질문/주장/정책으로 다시 확인한다."""
    validate_claims(claims)
    if not observed.satisfies(requirement):
        raise capabilities.CapabilityUnavailable("source_capability_or_scope_mismatch")
    binding = hash_payload(asdict(observed))
    if (selection.get("schema_version") != GATE_VERSION or selection.get("source_binding_hash") != binding
            or selection.get("requirement_hash") != hash_payload(asdict(requirement))
            or set(selection.get("judgments", {})) != {pair[0] for pair in claims}):
        raise capabilities.CapabilityUnavailable("semantic_selection_binding_mismatch")
    permitted = []
    for value, hypothesis in claims:
        result = selection["judgments"][value]
        expected = _fingerprint(observed, scope_description, hypothesis, policy_version)
        # permits=False에는 source mismatch도 포함되므로 먼저 identity 자체를 검사한다.
        if (not isinstance(result, dict) or result.get("input_hash") != expected
                or result.get("source_binding_hash") != binding or result.get("policy_version") != policy_version
                or result.get("resolved_model") != policy.resolved_model
                or result.get("schema_version") != "correction_relation.v1"
                or result.get("question_version") != relation.QUESTION_VERSION
                or result.get("requested_model") != relation.DEFAULT_CORRECTION_MODEL):
            raise capabilities.CapabilityUnavailable("judgment_source_binding_mismatch")
        try:
            relation.parse_response({"model": result["resolved_model"], "answers": {"relation": {
                "type": "choice", "choice": result["relation"], "probabilities": result["relation_probabilities"]}}})
        except (KeyError, ValueError, TypeError):
            raise capabilities.CapabilityUnavailable("judgment_response_invalid") from None
        if policy.permits(result, expected_input_hash=expected,
                source_binding_hash=binding, policy_version=policy_version):
            permitted.append(value)
    value = permitted[0] if len(permitted) == 1 else None
    reason = "semantic_value_verified" if value is not None else (
        "semantic_values_conflict" if permitted else "semantic_support_insufficient")
    if selection.get("value") != value or selection.get("reason_code") != reason:
        raise capabilities.CapabilityUnavailable("semantic_selection_result_mismatch")
    return value


def select_value(*, reader, requirement, scope_description, claims, policy, policy_version, client,
                 expected_source_binding=None):
    """평가와 실제 worker가 동일한 선택 경로를 사용한다. authority는 쓰지 않는다."""
    validate_claims(claims)
    observed = reader.read()
    if not observed.satisfies(requirement):
        raise capabilities.CapabilityUnavailable("source_capability_or_scope_mismatch")
    binding = hash_payload(asdict(observed))
    if expected_source_binding is not None and expected_source_binding != binding:
        raise capabilities.CapabilityUnavailable("source_read_changed")
    judgments, permitted = {}, []
    gate = capabilities.CapabilityCheckedRelation(reader=reader, requirement=requirement, client=client)
    for value, hypothesis in claims:
        checked = gate.evaluate(scope_description=scope_description, hypothesis=hypothesis,
            policy_version=policy_version)
        if checked["observation"] != observed:
            raise capabilities.CapabilityUnavailable("source_read_changed")
        judgments[value] = checked["judgment"]
        if policy.permits(checked["judgment"], expected_input_hash=checked["expected_input_hash"],
                source_binding_hash=binding, policy_version=policy_version):
            permitted.append(value)
    reader.assert_still_current(observed)
    chosen = permitted[0] if len(permitted) == 1 else None
    selection = {"schema_version": GATE_VERSION, "source_binding_hash": binding,
        "requirement_hash": hash_payload(asdict(requirement)), "judgments": judgments, "value": chosen,
        "reason_code": "semantic_value_verified" if chosen is not None else (
            "semantic_values_conflict" if permitted else "semantic_support_insufficient")}
    verify_selection(observed=observed, requirement=requirement, scope_description=scope_description,
        claims=claims, policy=policy, policy_version=policy_version, selection=selection)
    return selection


@dataclass(frozen=True)
class ValidatedSemanticProfile:
    artifact_hash: str
    policy: policies.RelationSupportPolicy
    summary: dict


def validate_report(report):
    """서버가 신뢰하는 실제 평가 artifact를 재계산한다. pass boolean은 받지 않는다."""
    if (report.get("schema_version") != VALIDATION_SCHEMA or report.get("identity") != runtime_identity()
            or report.get("execution") != "live_openrouter" or report.get("support_min") != SUPPORT_MIN
            or report.get("policy_version") != EVALUATION_POLICY):
        raise ValueError("semantic profile identity or execution mismatch")
    rows = report["results"]
    development = set(report["development_case_hashes"])
    if not development or any(not isinstance(value, str) for value in development):
        raise ValueError("explicit development exclusion required")
    for value in development:
        require_sha256(value, "development case")
    if not isinstance(rows, list) or not 40 <= len(rows) <= 500:
        raise ValueError("independent semantic validation sample required")
    case_hashes, positive, negative, correct, wrong, semantic_negative, cost = set(), 0, 0, 0, 0, 0, 0.
    policy = policies.RelationSupportPolicy(SUPPORT_MIN, "validation-only", report["resolved_model"],
        hash_payload(relation.questions()))
    for row in rows:
        case = row["case"]
        digest = hash_payload(case)
        if digest in case_hashes or digest in development or row.get("case_hash") != digest:
            raise ValueError("validation case overlap or binding mismatch")
        case_hashes.add(digest)
        observed = capabilities.ObservedFactRead(**{**row["observed"],
            "capability": capabilities.ObservedCapability(row["observed"]["capability"])})
        requirement = capabilities.EvidenceRequirement(**{**row["requirement"],
            "capability": capabilities.ObservedCapability(row["requirement"]["capability"])})
        expected = case["expected_value"]
        claims = case["claims"]
        validate_claims(claims)
        if expected is not None and expected not in {pair[0] for pair in claims}:
            raise ValueError("invalid validation expected value")
        if observed.value != case["content"]:
            raise ValueError("validation observation content mismatch")
        if requirement.capability.value != case["required_capability"]:
            raise ValueError("validation requirement mismatch")
        if observed.satisfies(requirement):
            if row.get("source_revalidated") is not True:
                raise ValueError("source postcheck required")
            chosen = verify_selection(observed=observed, requirement=requirement,
                scope_description=case["scope_description"], claims=claims, policy=policy,
                policy_version=EVALUATION_POLICY, selection=row["selection"])
            if expected is None:
                semantic_negative += 1
            cost += sum(value.get("usage", {}).get("cost", 0) for value in row["selection"]["judgments"].values())
        else:
            if row.get("reason_code") != "source_capability_or_scope_mismatch" or row.get("selection") is not None:
                raise ValueError("source gate rejection required")
            chosen = None
        if expected is not None:
            positive += 1
            correct += chosen == expected
        else:
            negative += 1
        wrong += chosen is not None and chosen != expected
    summary = {"cases": len(rows), "positive_cases": positive, "positive_permits": correct,
        "negative_cases": negative, "semantic_negative_cases": semantic_negative,
        "wrong_permits": wrong, "reported_cost_usd": cost}
    if positive < 20 or negative < 20 or semantic_negative < 10 or wrong or correct / positive < .8:
        raise ValueError("independent semantic validation criterion not met")
    return summary


def load_validated_profile(path, *, expected_hash):
    require_sha256(expected_hash, "semantic profile artifact")
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 4_000_000 or sha256_text(raw.decode()) != expected_hash:
            raise ValueError("semantic profile artifact changed")
        report = json.loads(raw, object_pairs_hook=transport._unique_object)
        summary = validate_report(report)
        return ValidatedSemanticProfile(expected_hash,
            policies.RelationSupportPolicy(SUPPORT_MIN, expected_hash, report["resolved_model"],
                hash_payload(relation.questions())), summary)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        raise capabilities.CapabilityUnavailable("semantic_profile_unvalidated") from None
