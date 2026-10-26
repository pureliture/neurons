"""배타적 choice의 지지 확률만 사용하는 후보 정책.

독립 확인을 통과한 profile의 server 등록은 별도 단계다. 이 모듈 자체는
profile을 승인하거나 authority를 변경하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from .correction_relation import DEFAULT_CORRECTION_MODEL, QUESTION_VERSION, parse_response, questions
from .public_safe_util import hash_payload, require_sha256


POLICY_SCHEMA = "relation_support_policy.v1"


@dataclass(frozen=True)
class RelationSupportPolicy:
    support_min: float
    profile_id: str
    resolved_model: str
    questions_hash: str

    def __post_init__(self):
        if (type(self.support_min) not in (int, float) or not math.isfinite(self.support_min)
                or not .5 < self.support_min <= 1 or not self.profile_id or len(self.profile_id) > 180
                or not isinstance(self.resolved_model, str)
                or not (self.resolved_model == DEFAULT_CORRECTION_MODEL
                    or self.resolved_model.startswith(DEFAULT_CORRECTION_MODEL + "-"))
                or self.questions_hash != hash_payload(questions())):
            raise ValueError("explicit validated relation support policy required")

    def permits(self, result, *, expected_input_hash, source_binding_hash, policy_version):
        """다른 source·정책·질문·모델의 cached 점수를 재사용하지 않는다."""
        try:
            require_sha256(expected_input_hash, "expected input")
            require_sha256(source_binding_hash, "source binding")
            if (result["schema_version"] != "correction_relation.v1" or result["question_version"] != QUESTION_VERSION
                    or result["requested_model"] != DEFAULT_CORRECTION_MODEL
                    or result["resolved_model"] != self.resolved_model
                    or result["input_hash"] != expected_input_hash or result["source_binding_hash"] != source_binding_hash
                    or not isinstance(policy_version, str) or not policy_version or result["policy_version"] != policy_version):
                return False
            checked = parse_response({"model": result["resolved_model"], "answers": {"relation": {
                "type": "choice", "choice": result["relation"], "probabilities": result["relation_probabilities"]}}})
            return checked["relation"] == "supported" and checked["relation_probabilities"]["supported"] >= self.support_min
        except (ValueError, KeyError, TypeError):
            return False


def support_evaluation(rows, threshold):
    """개발/독립 평가에 공통인 집계. 단위 검사 점수는 실제 모델 성능이 아니다."""
    positives = [row for row in rows if row["expected"] == "supported"]
    completed = sum("result" in row for row in rows)
    def allowed(row):
        if "result" not in row:
            return False
        result = row["result"]
        policy = RelationSupportPolicy(threshold, "evaluation-only", result["resolved_model"], hash_payload(questions()))
        return policy.permits(result, expected_input_hash=result["input_hash"],
            source_binding_hash=result["source_binding_hash"], policy_version=result["policy_version"])
    correct = sum(allowed(row) for row in positives)
    wrong = sum(allowed(row) for row in rows if row["expected"] != "supported")
    return {"cases": len(rows), "completed": completed, "positive_cases": len(positives),
        "positive_permits": correct, "missed_positives": len(positives) - correct,
        "negative_cases": len(rows) - len(positives), "wrong_permits": wrong,
        "gate_passed": bool(positives and len(positives) < len(rows) and completed == len(rows)
            and wrong == 0 and correct / len(positives) >= .8),
        "reported_cost_usd": sum(row.get("result", {}).get("usage", {}).get("cost", 0) for row in rows)}


def fit_support_threshold(development_rows):
    # 단일 지지 확률에서 기준을 만족하는 가장 높은 문턱을 고른다.
    eligible = [threshold for threshold in (.60, .65, .70, .75, .80, .85, .90, .95, .98)
        if support_evaluation(development_rows, threshold)["gate_passed"]]
    if not eligible:
        raise ValueError("development criterion not met")
    return max(eligible)
