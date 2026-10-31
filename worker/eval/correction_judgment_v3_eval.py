"""기존 v2 자료는 development로만 사용하고 새 확인 자료 전에 profile을 고정한다."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import itertools
import json
import os
from pathlib import Path

from agent_knowledge.correction_judgment import (
    CorrectionJudgmentClient, DEFAULT_CORRECTION_MODEL, JudgmentThresholds, JudgmentUnavailable, QUESTION_VERSION,
)
from agent_knowledge.public_safe_util import hash_payload, sha256_text


CRITERION = {"wrong_permits_max": 0, "positive_permit_rate_min": .8, "response_rate_min": 1.0}


def summarize(rows, thresholds):
    positive = [row for row in rows if row["expected"] == "supported"]
    permitted = lambda row: "result" in row and thresholds is not None and thresholds.permits(row["result"])
    completed = sum("result" in row for row in rows)
    correct = sum(permitted(row) for row in positive)
    wrong = sum(permitted(row) for row in rows if row["expected"] != "supported")
    return {"cases": len(rows), "completed": completed, "positive_cases": len(positive),
        "positive_permits": correct, "missed_positives": len(positive) - correct,
        "wrong_permits": wrong, "negative_cases": len(rows) - len(positive),
        "gate_passed": bool(rows and positive and wrong == 0 and completed == len(rows)
            and correct / len(positive) >= CRITERION["positive_permit_rate_min"]),
        "reported_cost_usd": sum(row.get("result", {}).get("usage", {}).get("cost", 0) for row in rows)}


def fit_thresholds(development_rows, *, profile_id):
    """누락 응답이나 전부 보류로 통과하지 않으며 holdout 결과를 받지 않는다."""
    candidates = []
    for relevant, support, contradiction in itertools.product(
            (.60, .65, .70, .75, .80, .85, .90, .95, .98),
            (.60, .65, .70, .75, .80, .85, .90, .95, .98),
            (.01, .025, .05, .075, .10, .15, .20, .25, .30, .35, .40)):
        profile = JudgmentThresholds(relevant, support, contradiction, profile_id)
        summary = summarize(development_rows, profile)
        if summary["gate_passed"]:
            # 정상 허용 기준을 넘는 정책 중 support, relevance 순서로 더
            # 엄격하고 contradiction은 더 낮은 문턱을 택한다. 정상 전부를
            # 허용하려고 더 느슨하게 만들거나 개별 사례별 예외를 두지 않는다.
            candidates.append(((support, relevant, -contradiction, summary["positive_permits"]), profile))
    if not candidates:
        raise ValueError("development release criterion not met")
    return max(candidates, key=lambda item: item[0])[1]


def _new_private_path(path):
    if path.exists() or not path.parent.is_dir() or path.parent.stat().st_mode & 0o077:
        raise ValueError("new output in existing private directory required")


def _save(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("development", "holdout"), required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile-output", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--development-replay", type=Path)
    args = parser.parse_args(argv)
    _new_private_path(args.output)
    if args.phase == "development":
        if args.profile is not None or args.profile_output is None:
            raise ValueError("development requires a new profile output only")
        _new_private_path(args.profile_output)
    elif args.profile is None or args.profile_output is not None:
        raise ValueError("holdout requires an already frozen profile")
    if args.phase != "development" and args.development_replay:
        raise ValueError("only development evidence may be replayed for fitting")

    import agent_knowledge.correction_judgment as connector
    connector_hash = sha256_text(Path(connector.__file__).read_text())
    evaluator_hash = sha256_text(Path(__file__).read_text())
    holdout_hash = sha256_text(Path(__file__).with_name("correction_judgment_v3_holdout.json").read_text())
    fixture_path = Path(__file__).with_name("correction_judgment_cases.json" if args.phase == "development"
        else "correction_judgment_v3_holdout.json")
    raw = fixture_path.read_text()
    fixture = json.loads(raw)
    cases = fixture["cases"]  # 옛 calibration/holdout 모두 이제 development다.
    frozen = json.loads(args.profile.read_text()) if args.profile else None
    thresholds = None
    if frozen is not None:
        if (frozen["schema_version"] != "correction_judgment_profile.v3"
                or frozen["question_version"] != QUESTION_VERSION or frozen["connector_hash"] != connector_hash
                or frozen["evaluator_hash"] != evaluator_hash or frozen["holdout_fixture_hash"] != holdout_hash
                or frozen["requested_model"] != DEFAULT_CORRECTION_MODEL or frozen["criterion"] != CRITERION
                or not frozen["development_summary"]["gate_passed"]):
            raise ValueError("frozen profile identity mismatch")
        thresholds = JudgmentThresholds(**frozen["thresholds"])
    if args.phase == "holdout" and fixture["release_criterion"] != CRITERION:
        raise ValueError("holdout criterion cannot change after profile freeze")
    replay = json.loads(args.development_replay.read_text()) if args.development_replay else None
    if replay is not None:
        if (replay.get("phase") != "development" or replay.get("question_version") != QUESTION_VERSION
                or replay.get("connector_hash") != connector_hash or replay.get("fixture_hash") != sha256_text(raw)
                or [row["case_id"] for row in replay["results"]] != [case["id"] for case in cases]
                or [row["expected"] for row in replay["results"]] != [case["expected"] for case in cases]):
            raise ValueError("development replay identity mismatch")
    client = CorrectionJudgmentClient(api_key=args.key_file.read_text()) if replay is None else None

    def evaluate(case):
        state = {"fact": case["fact"], "proposed_value": case["proposed_value"],
            "evidence": [{"evidence_id": "synthetic-" + case["id"], "text": case["evidence"]}]}
        try:
            result = client.evaluate(state, policy_version="correction-eval.v3")
            return {"case_id": case["id"], "expected": case["expected"], "state": state, "result": result}
        except JudgmentUnavailable as error:
            return {"case_id": case["id"], "expected": case["expected"], "error_code": error.reason_code}

    if replay is not None:
        rows = replay["results"]
        for row, case in zip(rows, cases):
            expected_state = {"fact": case["fact"], "proposed_value": case["proposed_value"],
                "evidence": [{"evidence_id": "synthetic-" + case["id"], "text": case["evidence"]}]}
            if "result" in row and row.get("state") != expected_state:
                raise ValueError("development replay state mismatch")
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(evaluate, cases))
    if args.phase == "development":
        try:
            thresholds = fit_thresholds(rows, profile_id="development-v3-" + hash_payload(rows)[7:23])
        except ValueError:
            thresholds = None  # 실패한 실호출 결과도 보존하고 profile은 생성하지 않는다.
    for row in rows:
        row["candidate_permitted"] = "result" in row and thresholds is not None and thresholds.permits(row["result"])
    summary = summarize(rows, thresholds)
    resolved = sorted({row["result"]["resolved_model"] for row in rows if "result" in row})
    if frozen is not None and resolved != frozen["resolved_models"]:
        summary["gate_passed"] = False
        summary["model_identity_changed"] = True
    report = {"schema_version": "correction_judgment_eval.v3", "at": datetime.now(timezone.utc).isoformat(),
        "phase": args.phase, "fixture_hash": sha256_text(raw), "question_version": QUESTION_VERSION,
        "connector_hash": connector_hash, "criterion": CRITERION, "thresholds": asdict(thresholds) if thresholds else None,
        "evaluator_hash": evaluator_hash, "holdout_fixture_hash": holdout_hash,
        "development_replay_hash": hash_payload(replay) if replay is not None else None,
        "new_model_calls": 0 if replay is not None else len(rows),
        "frozen_profile_hash": hash_payload(frozen) if frozen else None, "results": rows, "summary": summary,
        "canonical_writes": 0, "production_activated": False}
    _save(args.output, report)
    if args.phase == "development" and thresholds is not None:
        profile = {"schema_version": "correction_judgment_profile.v3", "frozen_at": report["at"],
            "question_version": QUESTION_VERSION, "connector_hash": connector_hash,
            "evaluator_hash": evaluator_hash, "holdout_fixture_hash": holdout_hash,
            "requested_model": DEFAULT_CORRECTION_MODEL, "resolved_models": resolved,
            "thresholds": asdict(thresholds), "criterion": CRITERION,
            "development_report_hash": hash_payload(report), "development_fixture_hash": report["fixture_hash"],
            "development_summary": summary, "production_activated": False}
        _save(args.profile_output, profile)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["gate_passed"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError):
        # key/path/provider 본문을 traceback에 노출하지 않는다.
        print(json.dumps({"status": "unavailable", "reason_code": "judgment_eval_unavailable"}))
        raise SystemExit(2)
