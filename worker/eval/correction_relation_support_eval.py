"""동일 choice 질문의 단일 지지 정책을 새 자료에서 확인한다."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path

from agent_knowledge import correction_relation as connector
from agent_knowledge import correction_relation_policy as policy_module
from agent_knowledge.public_safe_util import hash_payload, sha256_text
from agent_knowledge.correction_judgment import JudgmentUnavailable
import correction_relation_v4_eval as previous
from correction_judgment_v3_eval import CRITERION, _new_private_path, _save


def identity():
    return {"questions_hash": hash_payload(connector.questions()), "question_version": connector.QUESTION_VERSION,
        "connector_hash": sha256_text(Path(connector.__file__).read_text()),
        "policy_hash": sha256_text(Path(policy_module.__file__).read_text()),
        "transport_hash": sha256_text(Path(previous.transport.__file__).read_text()),
        "previous_validator_hash": sha256_text(Path(previous.__file__).read_text()),
        "fitter_hash": sha256_text(Path(previous.fitting.__file__).read_text()),
        "evaluator_hash": sha256_text(Path(__file__).read_text()),
        "holdout_hash": sha256_text(Path(__file__).with_name("correction_relation_support_holdout.json").read_text()),
        "model": connector.DEFAULT_CORRECTION_MODEL}


def freeze(development, previous_profile, previous_holdout):
    previous.freeze(development)  # 76건의 source/질문/입력/typed 응답을 재확인한다.
    previous.validate_frozen(previous_profile)
    if (previous_holdout["schema_version"] != "correction_relation_eval.v4" or previous_holdout["phase"] != "holdout"
            or previous_holdout["profile_hash"] != hash_payload(previous_profile)
            or previous_holdout["identity"] != previous_profile["identity"]):
        raise ValueError("previous evidence identity mismatch")
    old_cases = json.loads(Path(__file__).with_name("correction_relation_v4_holdout.json").read_text())["cases"]
    old_rows = previous_holdout["results"]
    if len(old_rows) != len(old_cases):
        raise ValueError("previous case count mismatch")
    for row, case in zip(old_rows, old_cases):
        state = previous._state(case)
        binding = hash_payload([previous_profile["identity"]["holdout_hash"], case["id"], state])
        result = row["result"]
        fingerprint = hash_payload({"payload": {"model": connector.DEFAULT_CORRECTION_MODEL, "state": state,
            "questions": connector.questions()}, "question_version": connector.QUESTION_VERSION,
            "policy_version": "relation-eval.v4", "source_binding_hash": binding})
        if (row["case_id"] != case["id"] or row["expected"] != case["expected"] or row["state"] != state
                or result["input_hash"] != fingerprint or result["source_binding_hash"] != binding):
            raise ValueError("previous case binding mismatch")
    # 이전 독립 40건도 이 시점부터는 조정 자료로만 사용한다.
    rows = development["results"] + old_rows
    models = sorted({row["result"]["resolved_model"] for row in rows})
    if len(models) != 1:
        raise ValueError("one model revision required")
    threshold = policy_module.fit_support_threshold(rows)
    return {"schema_version": "relation_support_profile.v1", "frozen_at": datetime.now(timezone.utc).isoformat(),
        "identity": identity(), "criterion": CRITERION, "support_min": threshold, "resolved_model": models[0],
        "development_evidence_hashes": [hash_payload(development), hash_payload(previous_profile), hash_payload(previous_holdout)],
        "development_summary": policy_module.support_evaluation(rows, threshold), "production_activated": False}


def validate_frozen(profile):
    if (profile["schema_version"] != "relation_support_profile.v1" or profile["identity"] != identity()
            or profile["criterion"] != CRITERION or not profile["development_summary"]["gate_passed"]):
        raise ValueError("relation support profile changed")
    return policy_module.RelationSupportPolicy(profile["support_min"], "frozen-" + hash_payload(profile)[7:23],
        profile["resolved_model"], profile["identity"]["questions_hash"])


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("freeze", "holdout"), required=True)
    parser.add_argument("--development-report", type=Path)
    parser.add_argument("--previous-profile", type=Path)
    parser.add_argument("--previous-holdout", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    _new_private_path(args.output)
    if args.phase == "freeze":
        if not all((args.development_report, args.previous_profile, args.previous_holdout)) or args.profile or args.key_file:
            raise ValueError("freeze requires development evidence only")
        profile = freeze(json.loads(args.development_report.read_text()), json.loads(args.previous_profile.read_text()),
            json.loads(args.previous_holdout.read_text()))
        _save(args.output, profile)
        print(json.dumps({"profile_frozen": True, "summary": profile["development_summary"]}, ensure_ascii=False))
        return 0
    if any((args.development_report, args.previous_profile, args.previous_holdout)) or not args.profile or not args.key_file:
        raise ValueError("holdout requires frozen profile and auth")
    profile = json.loads(args.profile.read_text())
    policy = validate_frozen(profile)
    fixture = json.loads(Path(__file__).with_name("correction_relation_support_holdout.json").read_text())
    if fixture["criterion"] != CRITERION:
        raise ValueError("holdout criterion changed")
    client = connector.CorrectionRelationClient(api_key=args.key_file.read_text())
    def evaluate(case):
        state = previous._state(case)
        binding = hash_payload([profile["identity"]["holdout_hash"], case["id"], state])
        try:
            result = client.evaluate(state, policy_version="relation-support-eval.v1", source_binding_hash=binding)
            return {"case_id": case["id"], "expected": case["expected"], "state": state, "result": result,
                "candidate_permitted": policy.permits(result, expected_input_hash=result["input_hash"],
                    source_binding_hash=binding, policy_version="relation-support-eval.v1")}
        except JudgmentUnavailable as error:
            return {"case_id": case["id"], "expected": case["expected"], "error_code": error.reason_code,
                "candidate_permitted": False}
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(evaluate, fixture["cases"]))
    summary = policy_module.support_evaluation(rows, profile["support_min"])
    if any(row.get("result", {}).get("resolved_model", profile["resolved_model"]) != profile["resolved_model"] for row in rows):
        summary.update(gate_passed=False, model_identity_changed=True)
    report = {"schema_version": "relation_support_eval.v1", "phase": "holdout",
        "at": datetime.now(timezone.utc).isoformat(), "identity": identity(), "profile_hash": hash_payload(profile),
        "criterion": CRITERION, "support_min": profile["support_min"], "results": rows, "summary": summary,
        "canonical_writes": 0, "production_activated": False}
    _save(args.output, report)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["gate_passed"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, TypeError, OSError):
        print(json.dumps({"status": "unavailable", "reason_code": "relation_support_evaluation_unavailable"}))
        raise SystemExit(2)
