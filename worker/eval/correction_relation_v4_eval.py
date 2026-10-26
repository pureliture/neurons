"""choice 질문의 profile을 development로 고정하고 새 자료만 독립 호출한다."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

from agent_knowledge import correction_relation as connector
from agent_knowledge import correction_judgment as transport
from agent_knowledge.correction_judgment import JudgmentThresholds, JudgmentUnavailable
from agent_knowledge.public_safe_util import hash_payload, sha256_text
import correction_judgment_v3_eval as fitting


def _state(case):
    return {"scope": case["fact"], "hypothesis": case["proposed_value"], "premise": case["evidence"]}


def identity():
    return {"question_version": connector.QUESTION_VERSION, "questions_hash": hash_payload(connector.questions()),
        "connector_hash": sha256_text(Path(connector.__file__).read_text()),
        "transport_hash": sha256_text(Path(transport.__file__).read_text()),
        "fitting_hash": sha256_text(Path(fitting.__file__).read_text()),
        "evaluator_hash": sha256_text(Path(__file__).read_text()),
        "holdout_hash": sha256_text(Path(__file__).with_name("correction_relation_v4_holdout.json").read_text()),
        "model": connector.DEFAULT_CORRECTION_MODEL}


def freeze(development):
    if (development.get("schema_version") != "correction_relation_pilot.v4"
            or development.get("phase") != "development_only"
            or development.get("question_version") != connector.QUESTION_VERSION
            or development.get("questions_hash") != hash_payload(connector.questions())):
        raise ValueError("development identity mismatch")
    cases, fixture_hashes = [], []
    for name in ("correction_judgment_cases.json", "correction_judgment_v3_holdout.json"):
        fixture = json.loads(Path(__file__).with_name(name).read_text())
        fixture_hashes.append(hash_payload(fixture))
        cases.extend(fixture["cases"])
    rows = development["results"]
    if development["fixture_hashes"] != fixture_hashes or len(rows) != len(cases):
        raise ValueError("development fixture mismatch")
    for row, case in zip(rows, cases):
        state = _state(case)
        if row["case_id"] != case["id"] or row["expected"] != case["expected"] or row.get("state") != state:
            raise ValueError("development case mismatch")
        result = row["result"]
        fingerprint = hash_payload({"payload": {"model": connector.DEFAULT_CORRECTION_MODEL, "state": state,
            "questions": connector.questions()}, "question_version": connector.QUESTION_VERSION,
            "policy_version": "relation-eval.v4", "source_binding_hash": hash_payload(state)})
        if result["input_hash"] != fingerprint:
            raise ValueError("development source binding mismatch")
        checked = connector.parse_response({"model": result["resolved_model"], "answers": {"relation": {
            "type": "choice", "choice": result["relation"], "probabilities": result["relation_probabilities"]}}})
        if checked["probabilities"] != result["probabilities"]:
            raise ValueError("development probability mismatch")
    thresholds = fitting.fit_thresholds(rows, profile_id=development["profile"]["profile_id"])
    if asdict(thresholds) != development["profile"]:
        raise ValueError("development threshold mismatch")
    summary = fitting.summarize(rows, thresholds)
    if not summary["gate_passed"]:
        raise ValueError("development criterion not met")
    return {"schema_version": "correction_relation_profile.v4", "identity": identity(),
        "frozen_at": datetime.now(timezone.utc).isoformat(), "criterion": fitting.CRITERION,
        "thresholds": asdict(thresholds), "development_report_hash": hash_payload(development),
        "development_summary": summary,
        "resolved_models": sorted({row["result"]["resolved_model"] for row in rows}),
        "production_activated": False}


def validate_frozen(profile):
    if (profile.get("schema_version") != "correction_relation_profile.v4" or profile.get("identity") != identity()
            or profile.get("criterion") != fitting.CRITERION or not profile["development_summary"]["gate_passed"]):
        raise ValueError("frozen relation profile changed")
    return JudgmentThresholds(**profile["thresholds"])


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("freeze", "holdout"), required=True)
    parser.add_argument("--development-report", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    fitting._new_private_path(args.output)
    if args.phase == "freeze":
        if not args.development_report or args.profile or args.key_file:
            raise ValueError("freeze requires development report only")
        profile = freeze(json.loads(args.development_report.read_text()))
        fitting._save(args.output, profile)
        print(json.dumps({"profile_frozen": True, "summary": profile["development_summary"]}, ensure_ascii=False))
        return 0
    if args.development_report or not args.profile or not args.key_file:
        raise ValueError("holdout requires frozen profile and auth")
    profile = json.loads(args.profile.read_text())
    thresholds = validate_frozen(profile)  # auth/model보다 먼저 모든 identity를 검사한다.
    fixture = json.loads(Path(__file__).with_name("correction_relation_v4_holdout.json").read_text())
    if fixture["release_criterion"] != fitting.CRITERION:
        raise ValueError("holdout criterion changed")
    client = connector.CorrectionRelationClient(api_key=args.key_file.read_text())

    def evaluate(case):
        state = _state(case)
        try:
            result = client.evaluate(state, policy_version="relation-eval.v4",
                source_binding_hash=hash_payload([profile["identity"]["holdout_hash"], case["id"], state]))
            return {"case_id": case["id"], "expected": case["expected"], "state": state, "result": result,
                "candidate_permitted": thresholds.permits(result)}
        except JudgmentUnavailable as error:
            return {"case_id": case["id"], "expected": case["expected"], "error_code": error.reason_code,
                "candidate_permitted": False}

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(evaluate, fixture["cases"]))
    summary = fitting.summarize(rows, thresholds)
    if sorted({row["result"]["resolved_model"] for row in rows if "result" in row}) != profile["resolved_models"]:
        summary.update(gate_passed=False, model_identity_changed=True)
    report = {"schema_version": "correction_relation_eval.v4", "phase": "holdout",
        "at": datetime.now(timezone.utc).isoformat(), "identity": identity(), "profile_hash": hash_payload(profile),
        "thresholds": asdict(thresholds), "criterion": fitting.CRITERION, "results": rows, "summary": summary,
        "canonical_writes": 0, "production_activated": False}
    fitting._save(args.output, report)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["gate_passed"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, TypeError, OSError):
        print(json.dumps({"status": "unavailable", "reason_code": "relation_evaluation_unavailable"}))
        raise SystemExit(2)
