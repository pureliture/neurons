"""PG 현재 후보의 완전성을 확인한 뒤 설명으로 기존 대상을 식별한다.

이 판단은 대상 식별에만 사용한다. 기록된 문장을 현재 세계의 근거로
승격하지 않으며, 적용 값은 별도 source reader/resolution이 검증한다.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import json
from pathlib import Path

from psycopg.rows import dict_row

from . import correction_target_identity as identity
from . import correction_judgment as transport
from .correction_capabilities import CapabilityUnavailable
from .correction_target_identity import TargetIdentityPolicy
from .public_safe_util import (
    ensure_public_safe,
    hash_payload,
    require_sha256,
    sha256_text,
)

SCHEMA = "fact_target_selection.v1"
VALIDATION_SCHEMA = "fact_target_validation.v1"
THRESHOLD = 0.8


@dataclass(frozen=True)
class TargetSearchPolicy:
    validation_profile_path: str
    validation_profile_hash: str
    max_candidates: int = 32

    def __post_init__(self):
        require_sha256(self.validation_profile_hash, "target validation profile")
        if (
            not isinstance(self.validation_profile_path, str)
            or not self.validation_profile_path
        ):
            raise ValueError("target validation profile required")
        if type(self.max_candidates) is not int or not 1 <= self.max_candidates <= 64:
            raise ValueError("bounded target candidate inventory required")

    @property
    def version(self):
        return hash_payload(
            {
                "schema": SCHEMA,
                "profile_hash": self.validation_profile_hash,
                "max_candidates": self.max_candidates,
            }
        )


def runtime_identity():
    return {
        "schema": SCHEMA,
        "threshold": THRESHOLD,
        "model": identity.DEFAULT_CORRECTION_MODEL,
        "questions_hash": hash_payload(identity.questions()),
        "target_hash": sha256_text(Path(__file__).read_text()),
        "identity_hash": sha256_text(Path(identity.__file__).read_text()),
        "transport_hash": sha256_text(Path(transport.__file__).read_text()),
        "coordinator_hash": sha256_text(
            Path(__file__).with_name("fact_correction_coordinator.py").read_text()
        ),
    }


def candidate_state(snapshot, candidate):
    # 내용은 실제 DB의 저장된 설명이다. source의 현재 값 증명과 구분한다.
    return {
        "requested_fact": snapshot["description"],
        "candidate_fact": "title: "
        + candidate["title"]
        + "\nsummary: "
        + candidate["summary"]
        + "\n"
        + "\n".join(
            "alias: " + alias["title"] + " " + alias["summary"]
            for alias in candidate.get("aliases", [])
        ),
        "candidate_field": json.dumps(
            candidate.get("field", {}), ensure_ascii=False, sort_keys=True
        ),
        "requested_scope": json.dumps(
            snapshot["context"], ensure_ascii=False, sort_keys=True
        ),
        "candidate_scope": json.dumps(
            candidate.get("context", {}), ensure_ascii=False, sort_keys=True
        ),
    }


def _fingerprint(snapshot, candidate, policy_version):
    binding = hash_payload({"snapshot": snapshot, "candidate": candidate})
    payload = {
        "model": identity.DEFAULT_CORRECTION_MODEL,
        "state": candidate_state(snapshot, candidate),
        "questions": identity.questions(),
    }
    return binding, hash_payload(
        {
            "payload": payload,
            "question_version": identity.QUESTION_VERSION,
            "policy_version": policy_version,
            "source_binding_hash": binding,
        }
    )


def _scope_exclusion(snapshot, candidate, policy_version):
    # 이 context는 caller 문장이 아니라 서버가 등록 policy/fact scope에서 읽은
    # 식별자다. 알려진 다른 사실은 모델에 동일성 판단을 맡기지 않는다.
    expected = {**snapshot["context"], **{name: snapshot["property"][name]
        for name in ("subject", "predicate")}}
    known = candidate.get("context", {})
    mismatches = sorted(name for name, value in expected.items()
        if name in known and known[name] != value)
    if not mismatches:
        return None
    binding, fingerprint = _fingerprint(snapshot, candidate, policy_version)
    return {"schema_version": "target_registered_scope_gate.v1",
        "reason_code": "target_registered_scope_mismatch", "mismatched_fields": mismatches,
        "source_binding_hash": binding, "input_hash": fingerprint, "policy_version": policy_version}


def _validate_snapshot(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("complete") is not True:
        raise CapabilityUnavailable("target_search_incomplete")
    candidates = snapshot.get("candidates")
    context = snapshot.get("context")
    property = snapshot.get("property")
    if (
        snapshot.get("schema_version") != "target_inventory.v1"
        or not isinstance(candidates, list)
        or len(candidates) > 64
        or not isinstance(context, dict)
        or not isinstance(property, dict)
        or not all(isinstance(property.get(key), str) and property[key].strip()
            for key in ("subject", "predicate", "field_path", "render_template"))
        or not all(
            isinstance(context.get(key), str) and context[key].strip()
            for key in ("project", "repository", "branch", "environment")
        )
        or not isinstance(snapshot.get("description"), str)
        or not 1 <= len(snapshot["description"]) <= 512
        or any(
            not isinstance(row, dict)
            or not all(
                isinstance(row.get(key), str) and row[key]
                for key in (
                    "memory_id",
                    "project",
                    "card_type",
                    "title",
                    "summary",
                    "content_hash",
                )
            )
            for row in candidates
        )
        or any(row["project"] != context["project"] for row in candidates)
        or any(not isinstance(row.get("context", {}), dict) or any(
            name in row.get("context", {}) and (not isinstance(row["context"][name], str)
                or not row["context"][name].strip())
            for name in ("project", "repository", "branch", "environment", "subject", "predicate"))
            for row in candidates)
        or len({row["memory_id"] for row in candidates}) != len(candidates)
    ):
        raise CapabilityUnavailable("target_selection_binding_mismatch")
    ensure_public_safe(snapshot, "TargetInventory")
    if len(json.dumps(snapshot, ensure_ascii=False).encode()) > 131072:
        raise CapabilityUnavailable("target_search_incomplete")
    if any(
        len(json.dumps(candidate_state(snapshot, row), ensure_ascii=False).encode())
        > 60000
        for row in candidates
    ):
        raise CapabilityUnavailable("target_search_incomplete")


def verify_selection(snapshot, selection, *, policy, policy_version):
    _validate_snapshot(snapshot)
    if not isinstance(selection, dict):
        raise CapabilityUnavailable("target_selection_binding_mismatch")
    ensure_public_safe(snapshot, "TargetInventory")
    candidates = snapshot["candidates"]
    ids = [row["memory_id"] for row in candidates]
    if (
        not snapshot.get("complete")
        or len(ids) != len(set(ids))
        or len(ids) > 64
        or any(row["project"] != snapshot["context"]["project"] for row in candidates)
        or selection.get("schema_version") != SCHEMA
        or selection.get("inventory_hash") != hash_payload(snapshot)
        or set(selection.get("judgments", {})) != set(ids)
    ):
        raise CapabilityUnavailable("target_selection_binding_mismatch")
    matches = []
    uncertain = False
    for candidate in candidates:
        result = selection["judgments"][candidate["memory_id"]]
        excluded = _scope_exclusion(snapshot, candidate, policy_version)
        if excluded is not None:
            if result != excluded:
                raise CapabilityUnavailable("target_registered_scope_binding_mismatch")
            continue
        binding, fingerprint = _fingerprint(snapshot, candidate, policy_version)
        if not isinstance(result, dict) or (
            result.get("input_hash") != fingerprint
            or result.get("source_binding_hash") != binding
            or result.get("policy_version") != policy_version
            or result.get("resolved_model") != policy.resolved_model
            or result.get("requested_model") != identity.DEFAULT_CORRECTION_MODEL
            or result.get("schema_version") != identity.SCHEMA
            or result.get("question_version") != identity.QUESTION_VERSION
        ):
            raise CapabilityUnavailable("target_judgment_binding_mismatch")
        try:
            checked = identity.parse_response(
                {
                    "model": result["resolved_model"],
                    "answers": {
                        "identity": {
                            "type": "choice",
                            "choice": result["identity"],
                            "probabilities": result["identity_probabilities"],
                        }
                    },
                }
            )
        except (ValueError, KeyError, TypeError):
            raise CapabilityUnavailable("target_judgment_invalid") from None
        if policy.permits(
            result,
            expected_input_hash=fingerprint,
            source_binding_hash=binding,
            policy_version=policy_version,
        ):
            matches.append(candidate["memory_id"])
        elif (
            checked["identity"] != "different_fact"
            or checked["identity_probabilities"][checked["identity"]] < THRESHOLD
        ):
            uncertain = True
    selected = matches[0] if len(matches) == 1 and not uncertain else None
    reason = (
        "target_verified"
        if selected
        else "target_ambiguous"
        if len(matches) > 1
        else "target_identity_uncertain"
        if uncertain
        else "target_not_found"
    )
    if selection.get("memory_id") != selected or selection.get("reason_code") != reason:
        raise CapabilityUnavailable("target_selection_result_mismatch")
    return selected


def select_target(snapshot, *, client, policy, policy_version):
    _validate_snapshot(snapshot)

    def judge(candidate):
        excluded = _scope_exclusion(snapshot, candidate, policy_version)
        if excluded is not None:
            return candidate["memory_id"], excluded
        binding, _ = _fingerprint(snapshot, candidate, policy_version)
        return candidate["memory_id"], client.evaluate(
            candidate_state(snapshot, candidate),
            source_binding_hash=binding,
            policy_version=policy_version,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        judgments = dict(pool.map(judge, snapshot["candidates"]))
    # 같은 조건의 재시도에서 모델을 다시 호출하지 않도록 보류도 저장한다.
    matches = []
    uncertain = False
    for memory_id, result in judgments.items():
        if result.get("schema_version") == "target_registered_scope_gate.v1":
            continue
        label = result["identity"]
        score = result["identity_probabilities"][label]
        if label == "same_fact" and score >= THRESHOLD:
            matches.append(memory_id)
        elif label != "different_fact" or score < THRESHOLD:
            uncertain = True
    selected = matches[0] if len(matches) == 1 and not uncertain else None
    reason = (
        "target_verified"
        if selected
        else "target_ambiguous"
        if len(matches) > 1
        else "target_identity_uncertain"
        if uncertain
        else "target_not_found"
    )
    selection = {
        "schema_version": SCHEMA,
        "inventory_hash": hash_payload(snapshot),
        "judgments": judgments,
        "memory_id": selected,
        "reason_code": reason,
    }
    verify_selection(snapshot, selection, policy=policy, policy_version=policy_version)
    return selection


def validate_report(report):
    if (
        report.get("schema_version") != VALIDATION_SCHEMA
        or report.get("identity") != runtime_identity()
        or report.get("execution") != "live_openrouter"
        or report.get("threshold") != THRESHOLD
    ):
        raise ValueError("target profile identity required")
    rows = report["results"]
    excluded = set(report["development_case_hashes"])
    excluded_ids = set(report["development_case_ids"])
    for digest in excluded:
        require_sha256(digest, "target development case")
    if not 40 <= len(rows) <= 500 or not excluded:
        raise ValueError("independent target evaluation required")
    policy = TargetIdentityPolicy(
        THRESHOLD,
        "validation-only",
        report["resolved_model"],
        hash_payload(identity.questions()),
    )
    seen = set()
    seen_ids = set()
    cost = 0.0
    typed = 0
    scope_exclusions = 0
    positive = negative = correct = wrong = semantic_negative = 0
    for row in rows:
        case = row["case"]
        digest = hash_payload(case)
        if (
            digest in seen
            or digest in excluded
            or row.get("case_hash") != digest
            or case["id"] in excluded_ids
            or case["id"] in seen_ids
        ):
            raise ValueError("independent target case identity required")
        seen.add(digest)
        seen_ids.add(case["id"])
        if row["snapshot"] != case["snapshot"]:
            raise ValueError("target snapshot binding required")
        selected = verify_selection(
            row["snapshot"],
            row["selection"],
            policy=policy,
            policy_version="target-eval.v1",
        )
        judgments = row["selection"]["judgments"]
        scope_exclusions += sum(result.get("schema_version") == "target_registered_scope_gate.v1"
            for result in judgments.values())
        typed += sum(result.get("schema_version") == identity.SCHEMA for result in judgments.values())
        cost += sum(
            result.get("usage", {}).get("cost", 0) for result in judgments.values()
        )
        expected = case["expected_memory_id"]
        if expected is not None and expected not in {
            candidate["memory_id"] for candidate in row["snapshot"]["candidates"]
        }:
            raise ValueError("target expected identity required")
        if expected is not None:
            positive += 1
            correct += selected == expected
        else:
            negative += 1
            semantic_negative += any(result.get("schema_version") == identity.SCHEMA
                for result in judgments.values())
        wrong += selected is not None and selected != expected
    if positive < 20 or negative < 20 or semantic_negative < 12 or wrong or correct / positive < 0.8:
        raise ValueError("target independent validation criterion not met")
    return {
        "cases": len(rows),
        "positive_cases": positive,
        "positive_permits": correct,
        "negative_cases": negative,
        "semantic_negative_cases": semantic_negative,
        "wrong_permits": wrong,
        "typed_responses": typed,
        "registered_scope_exclusions": scope_exclusions,
        "reported_cost_usd": cost,
    }


def load_profile(config):
    try:
        raw = Path(config.validation_profile_path).read_bytes()
        if (
            len(raw) > 4_000_000
            or sha256_text(raw.decode()) != config.validation_profile_hash
        ):
            raise ValueError("target profile changed")
        report = json.loads(raw, object_pairs_hook=identity._unique_object)
        summary = validate_report(report)
        return TargetIdentityPolicy(
            THRESHOLD,
            config.validation_profile_hash,
            report["resolved_model"],
            hash_payload(identity.questions()),
        ), summary
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        raise CapabilityUnavailable("target_profile_unvalidated") from None


class PgTargetInventory:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    def read(self, rule, description, *, connection=None):
        config = self.coordinator.target_search
        if config is None:
            raise CapabilityUnavailable("target_search_unconfigured")
        with (
            self.coordinator.store._scope(conn=connection) as db,
            db.cursor(row_factory=dict_row) as cur,
        ):
            cur.execute(
                """SELECT steward_envelope FROM memory_cards WHERE project=%s
                AND authorization_status='active' AND currentness='current'
                ORDER BY memory_id LIMIT %s""",
                (rule.scope.project, config.max_candidates + 1),
            )
            rows = cur.fetchall()
            if len(rows) > config.max_candidates:
                raise CapabilityUnavailable("target_search_incomplete")
            if any(row["steward_envelope"] is None for row in rows):
                # 지원하지 않는 기존 기록을 빼고 대상 부재/유일함을 확정하지 않는다.
                raise CapabilityUnavailable("target_search_unsupported_records")
            cards = self.coordinator.resolver.project_cards(
                [row["steward_envelope"] for row in rows], connection=db
            )
            candidates = []
            inventory = []
            groups = {}
            for card in cards:
                inventory.append(
                    {
                        "memory_id": card["memory_id"],
                        "content_hash": card["content_hash"],
                    }
                )
                views = [
                    v
                    for v in card.get("fact_views", [])
                    if v["field_path"] == rule.field_path
                ]
                view = views[0] if len(views) == 1 else None
                memory_id = view["head_memory_id"] if view else card["memory_id"]
                key = view["fact_key"] if view else None
                context = {}
                admitted = [
                    policy
                    for policy in self.coordinator.policies.values()
                    if policy.scope.project == card["project"]
                    and policy.field_path == rule.field_path
                    and (
                        policy.scope.key == key
                        if key
                        else policy.target_title == card["title"]
                    )
                ]
                descriptions = sorted({policy.render_template for policy in admitted})
                if key:
                    cur.execute(
                        "SELECT scope FROM lbrain_fact_scopes WHERE project=%s AND fact_key=%s",
                        (rule.scope.project, key),
                    )
                    scoped = cur.fetchone()
                    context = scoped["scope"] if scoped else {}
                elif len({policy.scope.key for policy in admitted}) == 1:
                    context = asdict(admitted[0].scope)
                if context:
                    if any(
                        context.get(name) != getattr(rule.scope, name)
                        for name in ("repository", "branch", "environment")
                    ):
                        continue
                item = {
                    "memory_id": memory_id,
                    "project": card["project"],
                    "card_type": card["card_type"],
                    "title": card["title"],
                    "summary": card["summary"],
                    "content_hash": card["content_hash"],
                    "fact_key": key,
                    "context": context,
                    "field": {
                        "path": rule.field_path,
                        "value": card.get("typed_payload", {}).get(
                            rule.field_path.split(".", 1)[1]
                        ),
                        "description": descriptions[0]
                        if len(descriptions) == 1
                        else "",
                    },
                }
                if memory_id not in groups:
                    item["aliases"] = [
                        {"title": card["title"], "summary": card["summary"]}
                    ]
                    groups[memory_id] = item
                    candidates.append(item)
                else:
                    groups[memory_id]["aliases"].append(
                        {"title": card["title"], "summary": card["summary"]}
                    )
                    if card["memory_id"] == memory_id:
                        groups[memory_id].update(item)
            snapshot = {
                "schema_version": "target_inventory.v1",
                "complete": True,
                "description": description,
                "property": {
                    "subject": rule.scope.subject,
                    "predicate": rule.scope.predicate,
                    "field_path": rule.field_path,
                    "render_template": rule.render_template,
                },
                "context": {
                    name: getattr(rule.scope, name)
                    for name in ("project", "repository", "branch", "environment")
                },
                "inventory": inventory,
                "candidates": sorted(candidates, key=lambda row: row["memory_id"]),
                "profile_hash": config.validation_profile_hash,
                "policy_version": config.version,
            }
            ensure_public_safe(snapshot, "TargetInventory")
            if len(json.dumps(snapshot, ensure_ascii=False).encode()) > 131072:
                raise CapabilityUnavailable("target_search_incomplete")
            return snapshot
