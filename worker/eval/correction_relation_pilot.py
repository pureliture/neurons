"""선택 모델의 배타적 의미 관계 질문을 development 자료에서만 비교한다.

이 탐색은 제품 authority와 연결하지 않으며 독립 확인 통과로 보고하지 않는다.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

from agent_knowledge.correction_judgment import JudgmentUnavailable
from agent_knowledge.correction_relation import CorrectionRelationClient, DEFAULT_CORRECTION_MODEL, questions, parse_response
from agent_knowledge.public_safe_util import hash_payload
from correction_judgment_v3_eval import _new_private_path, _save, fit_thresholds, summarize


QUESTION_VERSION = "correction_relation.v4"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    _new_private_path(args.output)
    client = CorrectionRelationClient(api_key=args.key_file.read_text())
    cases = []
    fixture_hashes = []
    for name in ("correction_judgment_cases.json", "correction_judgment_v3_holdout.json"):
        data = json.loads(Path(__file__).with_name(name).read_text())
        cases.extend(data["cases"])
        fixture_hashes.append(hash_payload(data))

    def evaluate(case):
        state = {"scope": case["fact"], "hypothesis": case["proposed_value"], "premise": case["evidence"]}
        payload = {"model": DEFAULT_CORRECTION_MODEL, "state": state, "questions": questions()}
        try:
            result = client.evaluate(state, policy_version="relation-eval.v4", source_binding_hash=hash_payload(state))
            return {"case_id": case["id"], "expected": case["expected"], "state": state,
                "input_hash": hash_payload(payload), "result": result}
        except (JudgmentUnavailable, ValueError, KeyError, TypeError):
            return {"case_id": case["id"], "expected": case["expected"], "error_code": "relation_pilot_unavailable"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(evaluate, cases))
    try:
        profile = fit_thresholds(rows, profile_id="relation-pilot-" + hash_payload(rows)[7:23])
    except ValueError:
        profile = None
    summary = summarize(rows, profile)
    summary["exact_labels"] = sum(row.get("result", {}).get("relation") == row["expected"] for row in rows)
    report = {"schema_version": "correction_relation_pilot.v4", "phase": "development_only",
        "at": datetime.now(timezone.utc).isoformat(), "question_version": QUESTION_VERSION,
        "questions_hash": hash_payload(questions()), "fixture_hashes": fixture_hashes,
        "profile": asdict(profile) if profile else None, "results": rows, "summary": summary,
        "canonical_writes": 0, "production_activated": False}
    _save(args.output, report)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["gate_passed"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "unavailable", "reason_code": "relation_pilot_unavailable"}))
        raise SystemExit(2)
