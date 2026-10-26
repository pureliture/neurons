"""실제 Git source와 선택 모델을 사용한 값 선택의 독립 평가. authority는 쓰지 않는다."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import subprocess
import tempfile

from agent_knowledge.correction_capabilities import CapabilityUnavailable, EvidenceRequirement, GitRepositoryReader, ObservedCapability
from agent_knowledge.correction_relation import CorrectionRelationClient, DEFAULT_CORRECTION_MODEL, questions
from agent_knowledge.correction_relation_policy import RelationSupportPolicy
from agent_knowledge.correction_sources import GitSourcePolicy, GitSourceReader
from agent_knowledge.fact_resolution import FactScope
from agent_knowledge.public_safe_util import hash_payload, sha256_text
from agent_knowledge.redaction import redact_public_ingress_text
from agent_knowledge import correction_semantic as semantic


def save_new(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(data, stream, ensure_ascii=False, indent=2)


def freeze(fixture):
    # 병렬 모델 요청 전에 전체 source의 적격성을 확인한다. 일부 응답을 받은
    # 뒤 원문 redaction 문제로 결과 전체를 잃는 실행을 방지한다.
    for case in fixture['cases']:
        semantic.validate_claims(case['claims'])
        if redact_public_ingress_text(case['content'])!=case['content']:
            raise ValueError('evaluation source changed by redaction')
    development = []
    for name in ("correction_judgment_cases.json", "correction_judgment_v3_holdout.json",
                 "correction_relation_v4_holdout.json", "correction_relation_support_holdout.json",
                 "correction_semantic_holdout.json", "correction_semantic_v2_holdout.json",
                 "correction_semantic_v3_holdout.json", "correction_semantic_v4_holdout.json"):
        data = json.loads(Path(__file__).with_name(name).read_text())
        development.extend(hash_payload(case) for case in data["cases"])
    cases = fixture["cases"]
    hashes = [hash_payload(case) for case in cases]
    if len(set(hashes)) != len(cases) or set(hashes) & set(development):
        raise ValueError("independent case identity required")
    return {"schema_version": "semantic_evaluation_plan.v1", "identity": semantic.runtime_identity(),
        "evaluator_hash": sha256_text(Path(__file__).read_text()), "fixture_hash": hash_payload(fixture),
        "development_case_hashes": sorted(set(development)), "support_min": semantic.SUPPORT_MIN,
        "resolved_model": DEFAULT_CORRECTION_MODEL + "-20261016"}


def evaluate_case(case, *, client, policy):
    with tempfile.TemporaryDirectory(prefix="semantic-case-") as directory:
        root = Path(directory)
        def git(*arguments):
            return subprocess.run(["git", "-C", str(root), *arguments], capture_output=True,
                timeout=10, check=True).stdout.decode().strip()
        git("init", "-b", "fixture")
        (root / "source.txt").write_text(case["content"])
        git("add", "source.txt")
        git("-c", "user.name=Semantic Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "합성 관측")
        scope = FactScope("semantic-eval", "fixture-repo", "refs/heads/fixture", "test", case["id"], "source_fact")
        source = GitSourceReader({scope.repository: GitSourcePolicy(scope.repository, root, branch_ref=scope.branch)})
        reader = GitRepositoryReader(source, scope=scope, relative_path="source.txt")
        try:
            observed = reader.read()
        except CapabilityUnavailable as exc:
            # 일부 case가 읽기 전에 실패해도 다른 case의 실제 응답을 버리지 않는다.
            # 이 row가 포함된 report는 profile 검증을 통과할 수 없다.
            return {"case":case,"case_hash":hash_payload(case),"selection":None,
                "source_revalidated":False,"reason_code":exc.reason_code}
        requirement = EvidenceRequirement(ObservedCapability(case["required_capability"]), scope.context_hash, reader.resource_hash)
        if case.get("mismatch") == "context":
            requirement = replace(requirement, context_hash=hash_payload([scope.context_hash, "other-environment"]))
        elif case.get("mismatch") == "resource":
            requirement = replace(requirement, resource_hash=hash_payload([reader.resource_hash, "other-resource"]))
        row = {"case": case, "case_hash": hash_payload(case), "observed": asdict(observed),
            "requirement": asdict(requirement), "selection": None, "source_revalidated": False}
        try:
            row["selection"] = semantic.select_value(reader=reader, requirement=requirement,
                scope_description=case["scope_description"], claims=case["claims"], policy=policy,
                policy_version=semantic.EVALUATION_POLICY, client=client)
            reader.assert_still_current(observed)
            row["source_revalidated"] = True
        except CapabilityUnavailable as exc:
            row["reason_code"] = exc.reason_code
        except Exception as exc:
            # source/인증/provider 예외 본문은 보존하지 않는다.
            row["reason_code"] = getattr(exc, "reason_code", "evaluation_unavailable")
        return row


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("freeze", "run"), required=True)
    parser.add_argument("--fixture", type=Path, default=Path(__file__).with_name("correction_semantic_v5_holdout.json"))
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    fixture = json.loads(args.fixture.read_text())
    if args.phase == "freeze":
        if args.plan or args.key_file:
            raise ValueError("freeze does not use credentials or results")
        plan = freeze(fixture)
        save_new(args.output, plan)
        print(json.dumps({"plan_hash": hash_payload(plan), "cases": len(fixture["cases"])}))
        return 0
    plan = json.loads(args.plan.read_text())
    if plan != freeze(fixture):
        raise ValueError("evaluation plan or implementation changed")
    client = CorrectionRelationClient(api_key=(args.key_file.read_text() if args.key_file else os.environ.get("OPENROUTER_API_KEY", "")))
    policy = RelationSupportPolicy(semantic.SUPPORT_MIN, "evaluation-only", plan["resolved_model"], hash_payload(questions()))
    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(lambda case: evaluate_case(case, client=client, policy=policy), fixture["cases"]))
    report = {"schema_version": semantic.VALIDATION_SCHEMA, "identity": semantic.runtime_identity(),
        "execution": "live_openrouter", "support_min": semantic.SUPPORT_MIN, "policy_version": semantic.EVALUATION_POLICY,
        "plan_hash": hash_payload(plan), "development_case_hashes": plan["development_case_hashes"],
        "resolved_model": plan["resolved_model"], "results": rows}
    try:
        summary = semantic.validate_report(report)
        passed = True
    except (KeyError, ValueError, TypeError):
        # 실패 표본을 고쳐 같은 자료를 독립 평가로 다시 합격시키지 않는다.
        summary = {"cases": len(rows), "unavailable": sum(row["selection"] is None and row.get("reason_code") !=
            "source_capability_or_scope_mismatch" for row in rows)}
        passed = False
    save_new(args.output, report)
    print(json.dumps({"validated": passed, "summary": summary, "artifact_hash": sha256_text(args.output.read_text())}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
