"""저장된 기억의 대상 식별을 실제 선택 모델로 독립 평가한다."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from agent_knowledge import correction_target as target
from agent_knowledge.correction_target_identity import (
    CorrectionTargetClient,
    DEFAULT_CORRECTION_MODEL,
    questions,
)
from agent_knowledge.correction_target_identity import TargetIdentityPolicy
from agent_knowledge.public_safe_util import hash_payload, sha256_text


def cases(path):
    data = json.loads(Path(path).read_text())
    result = []
    for raw in data["cases"]:
        context = {
            "project": "target-eval",
            "repository": "neurons",
            "branch": "refs/heads/fixture",
            "environment": "test",
            **raw.get("context", {}),
        }
        candidates = []
        for index, item in enumerate(raw["candidates"]):
            registered_context = item.get("context", {})
            if item.get("registered"):
                registered_context = {
                    **context,
                    "subject": item["subject"],
                    "predicate": item["predicate"],
                    **registered_context,
                }
            card = {
                "memory_id": f"mem_{raw['id']}_{index}",
                "project": context["project"],
                "card_type": "status",
                "title": item["title"],
                "summary": item["summary"],
                "content_hash": hash_payload(item),
                "fact_key": None,
                "context": registered_context,
                "field": {
                    "path": "typed_payload.status_value",
                    "value": item.get("value", "missing"),
                    "description": item.get("field_description", ""),
                },
                "aliases": item.get("aliases", []),
            }
            candidates.append(card)
        snapshot = {
            "schema_version": "target_inventory.v1",
            "complete": True,
            "description": raw["description"],
            "property": {
                "subject": raw["subject"],
                "predicate": raw.get("predicate", "implementation_status"),
                "field_path": "typed_payload.status_value",
                "render_template": raw["field_description"] + "{value}",
            },
            "context": context,
            "candidates": candidates,
            "inventory": [
                {"memory_id": item["memory_id"], "content_hash": item["content_hash"]}
                for item in candidates
            ],
        }
        expected = raw["expected_index"]
        result.append(
            {
                "id": raw["id"],
                "snapshot": snapshot,
                "expected_memory_id": candidates[expected]["memory_id"]
                if expected is not None
                else None,
            }
        )
    return result


def freeze(fixture):
    development = cases(Path(__file__).with_name("correction_target_development.json"))
    development += cases(
        Path(__file__).with_name("correction_target_development_initial.json")
    )
    development += cases(Path(__file__).with_name("correction_target_holdout.json"))
    development += cases(Path(__file__).with_name("correction_target_v2_holdout.json"))
    development += cases(Path(__file__).with_name("correction_target_v3_holdout.json"))
    hashes = [hash_payload(case) for case in fixture]
    excluded = sorted({hash_payload(case) for case in development})
    excluded_ids = sorted({case["id"] for case in development})
    if (
        len(set(hashes)) != len(hashes)
        or set(hashes) & set(excluded)
        or any(case["id"] in excluded_ids for case in fixture)
    ):
        raise ValueError("independent target cases required")
    return {
        "schema_version": "target_evaluation_plan.v1",
        "identity": target.runtime_identity(),
        "evaluator_hash": sha256_text(Path(__file__).read_text()),
        "fixture_hash": hash_payload(fixture),
        "development_case_hashes": excluded,
        "development_case_ids": excluded_ids,
        "threshold": target.THRESHOLD,
        "resolved_model": DEFAULT_CORRECTION_MODEL + "-20261016",
    }


def save(path, value):
    path = Path(path)
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--phase", choices=("freeze", "run", "development"), required=True
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path(__file__).with_name("correction_target_v4_holdout.json"),
    )
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    fixture = cases(args.fixture)
    if args.phase == "freeze":
        plan = freeze(fixture)
        save(args.output, plan)
        print(json.dumps({"cases": len(fixture), "plan_hash": hash_payload(plan)}))
        return 0
    if args.phase == "run":
        plan = json.loads(args.plan.read_text())
        if plan != freeze(fixture):
            raise ValueError("target evaluation changed after freeze")
    else:
        plan = {
            "resolved_model": DEFAULT_CORRECTION_MODEL + "-20261016",
            "development_case_hashes": [hash_payload("development-only")],
            "development_case_ids": ["development-only"],
        }
    client = CorrectionTargetClient(api_key=os.environ.get("OPENROUTER_API_KEY", ""))
    policy = TargetIdentityPolicy(
        0.8, "target-evaluation-only", plan["resolved_model"], hash_payload(questions())
    )
    rows = []
    for case in fixture:
        row = {
            "case": case,
            "case_hash": hash_payload(case),
            "snapshot": case["snapshot"],
        }
        try:
            row["selection"] = target.select_target(
                case["snapshot"],
                client=client,
                policy=policy,
                policy_version="target-eval.v1",
            )
        except Exception as exc:
            row["reason_code"] = getattr(
                exc, "reason_code", "target_evaluation_unavailable"
            )
        rows.append(row)
    report = {
        "schema_version": target.VALIDATION_SCHEMA,
        "identity": target.runtime_identity(),
        "execution": "live_openrouter" if args.phase == "run" else "development_openrouter",
        "threshold": target.THRESHOLD,
        "resolved_model": plan["resolved_model"],
        "development_case_hashes": plan["development_case_hashes"],
        "development_case_ids": plan["development_case_ids"],
        "results": rows,
    }
    try:
        summary = target.validate_report(report)
        passed = True
    except (ValueError, KeyError, TypeError):
        summary = {
            "cases": len(rows),
            "exact_matches": sum(
                "selection" in row
                and row["selection"].get("memory_id")
                == row["case"]["expected_memory_id"]
                for row in rows
            ),
            "unavailable": sum("selection" not in row for row in rows),
        }
        passed = False
    save(args.output, report)
    print(
        json.dumps(
            {
                "validated": passed,
                "summary": summary,
                "artifact_hash": sha256_text(args.output.read_text()),
            }
        )
    )
    return 0 if passed or args.phase == "development" else 1


if __name__ == "__main__":
    raise SystemExit(main())
