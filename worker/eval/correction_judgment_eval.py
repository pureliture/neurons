"""합성 조정/독립 확인 세트를 분리해 실제 Decisions 호출을 기록한다."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from agent_knowledge.correction_judgment import CorrectionJudgmentClient, JudgmentThresholds, JudgmentUnavailable, QUESTION_VERSION
from agent_knowledge.public_safe_util import hash_payload, sha256_text


def diagnostic_label(probabilities):
    if probabilities["relevant"] < .5:
        return "unrelated"
    support, contradiction = probabilities["supports"] >= .5, probabilities["contradicts"] >= .5
    if support and contradiction:
        return "inconsistent"
    if support:
        return "supported"
    return "contradicted" if contradiction else "insufficient"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["calibration", "holdout"], required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()
    if args.output.exists() or not args.output.parent.is_dir() or args.output.parent.stat().st_mode & 0o077:
        raise ValueError("new output in an existing private directory required")
    raw = Path(__file__).with_name("correction_judgment_cases.json").read_text()
    cases = [c for c in json.loads(raw)["cases"] if c["split"] == args.split]
    thresholds = JudgmentThresholds(**json.loads(args.profile.read_text())) if args.profile else None
    client = CorrectionJudgmentClient(api_key=args.key_file.read_text())

    def evaluate(case):
        state = {"fact": case["fact"], "proposed_value": case["proposed_value"],
            "evidence": [{"evidence_id": "synthetic-" + case["id"], "text": case["evidence"]}]}
        try:
            result = client.evaluate(state, policy_version="correction-eval.v2")
            label = diagnostic_label(result["probabilities"])
            return {"case_id": case["id"], "expected": case["expected"], "diagnostic_label": label,
                "label_matches": label == case["expected"], "candidate_permitted": thresholds.permits(result) if thresholds else None,
                "state": state, "result": result}
        except JudgmentUnavailable as error:
            return {"case_id": case["id"], "expected": case["expected"], "label_matches": False,
                "candidate_permitted": False, "error_code": error.reason_code}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(evaluate, cases))
    summary = {"cases": len(cases), "completed": sum("result" in r for r in results),
        "label_matches": sum(r["label_matches"] for r in results),
        "wrong_permits": sum(r["candidate_permitted"] is True and r["expected"] != "supported" for r in results),
        "positive_cases": sum(r["expected"] == "supported" for r in results),
        "positive_permits": sum(r["candidate_permitted"] is True and r["expected"] == "supported" for r in results),
        "reported_cost_usd": sum(r.get("result", {}).get("usage", {}).get("cost", 0) for r in results)}
    import agent_knowledge.correction_judgment as module
    report = {"schema_version": "correction_judgment_eval.v2", "at": datetime.now(timezone.utc).isoformat(),
        "split": args.split, "fixture_hash": sha256_text(raw), "question_version": QUESTION_VERSION,
        "connector_hash": sha256_text(Path(module.__file__).read_text()),
        "profile": asdict(thresholds) if thresholds else None, "results": results, "summary": summary,
        "canonical_writes": 0, "production_activated": False}
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
