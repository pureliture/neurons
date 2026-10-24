"""명시적 source/field 정책으로 제한한 MemoryCard 사실 교정."""
from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from psycopg.errors import DeadlockDetected, LockNotAvailable, SerializationFailure
from psycopg import Error as PostgresError

from .llm_brain_core.ledger_adapter import LedgerSourceRefCatalog
from .llm_brain_core.objects.knowledge_objects import ReviewProposal, memory_card_to_knowledge_object
from .public_safe_util import (
    ensure_public_safe, hash_payload, require_opaque_id, require_sha256,
    sha256_text, short_hash, stable_json, utc_now_iso,
)
from .session_memory.memory_card import validate_memory_card_envelope
from .session_memory.memory_promotion import commit_supersession

POLICY_SCHEMA = "evidence_correction_policy.v1"
POLICY_VERSION = "evidence_correction.v1"
_FIELDS = {
    "status": {"summary", "typed_payload.status_value"},
    "evidence": {"summary", "typed_payload.result_status"},
    "task": {"typed_payload.task_state", "typed_payload.status"},
}


class CorrectionError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def validate_policies(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, Mapping) or set(value) != {"schema_version", "rules"}:
        raise ValueError("correction policy document fields are invalid")
    if value["schema_version"] != POLICY_SCHEMA or not isinstance(value["rules"], list):
        raise ValueError("correction policy schema is invalid")
    if not 1 <= len(value["rules"]) <= 100:
        raise ValueError("correction policy rule count is invalid")
    rules = []
    seen = set()
    for item in value["rules"]:
        fields = {"policy_id", "project", "repository", "memory_id", "field_path",
                  "summary_template", "sources", "max_age_seconds", "value_map"}
        if not isinstance(item, Mapping) or set(item) != fields:
            raise ValueError("correction rule fields are invalid")
        rule = copy.deepcopy(dict(item))
        ensure_public_safe(rule, "EvidenceCorrectionPolicy")
        for key in ("policy_id", "project", "repository", "memory_id"):
            _identity(rule[key], key, maximum=128 if key != "project" else 64)
        if rule["policy_id"] in seen:
            raise ValueError("duplicate correction policy id")
        seen.add(rule["policy_id"])
        if rule["field_path"] not in set().union(*_FIELDS.values()):
            raise ValueError("correction field is unsupported")
        template = rule["summary_template"]
        if (not isinstance(template, str) or len(template) > 1024
                or template.count("{value}") != 1
                or any(char in template.replace("{value}", "") for char in "{}")):
            raise ValueError("correction summary template must contain exactly one value placeholder")
        if type(rule["max_age_seconds"]) is not int or not 1 <= rule["max_age_seconds"] <= 86400:
            raise ValueError("correction source freshness window is invalid")
        if not isinstance(rule["value_map"], Mapping) or len(rule["value_map"]) > 100:
            raise ValueError("correction value map is invalid")
        for key, mapped in rule["value_map"].items():
            if not isinstance(key, str) or not isinstance(mapped, str) or not mapped or len(mapped) > 2048:
                raise ValueError("correction mapped value is invalid")
        if not isinstance(rule["sources"], list) or not 1 <= len(rule["sources"]) <= 5:
            raise ValueError("correction source allowlist is invalid")
        locators = set()
        for source in rule["sources"]:
            if not isinstance(source, Mapping) or set(source) != {
                "adapter", "root_id", "device_id_hash", "relative_path_hash", "json_pointer"
            }:
                raise ValueError("correction source binding fields are invalid")
            if source["adapter"] != "registered_json":
                raise ValueError("correction source adapter is unsupported")
            require_opaque_id(source["root_id"], "root_id")
            require_sha256(source["device_id_hash"], "device_id_hash")
            require_sha256(source["relative_path_hash"], "relative_path_hash")
            _pointer_tokens(source["json_pointer"])
            locator = _locator(source)
            if locator in locators:
                raise ValueError("duplicate correction source binding")
            locators.add(locator)
        rules.append(rule)
    return tuple(rules)


def load_policies(path: str) -> tuple[dict[str, Any], ...]:
    if not path:
        return ()
    if Path(path).stat().st_size > 131072:
        raise ValueError("correction policy is too large")
    raw = Path(path).read_text(encoding="utf-8")
    if len(raw.encode("utf-8")) > 131072:
        raise ValueError("correction policy is too large")
    return validate_policies(json.loads(raw, object_pairs_hook=_unique_object))


def resolve_value(*, rule: Mapping[str, Any], catalog: Any,
                  evidence_refs: list[dict[str, str]], now: datetime | None = None) -> tuple[str, list[dict]]:
    """요청한 citation과 별개로 정책의 모든 source head를 독립 조회한다."""
    instant = now or datetime.now(timezone.utc)
    supplied = {ref["source_ref_id"]: ref["source_content_hash"] for ref in evidence_refs}
    if len(supplied) != len(evidence_refs) or len(supplied) != len(rule["sources"]):
        raise CorrectionError("source_set_mismatch")
    values, proofs = [], []
    for binding in rule["sources"]:
        try:
            same_path = catalog.list_for_path(**{key: binding[key] for key in (
                "root_id", "device_id_hash", "relative_path_hash"
            )})
        except (ValueError, sqlite3.Error, PostgresError):
            raise CorrectionError("source_coverage_unverifiable") from None
        if not same_path:
            raise CorrectionError("source_unavailable")
        latest_time = max(_timestamp(record.last_seen_at) for record in same_path)
        latest = [record for record in same_path if _timestamp(record.last_seen_at) == latest_time]
        if len(latest) != 1:
            raise CorrectionError("source_head_ambiguous")
        record = latest[0]
        if supplied.get(record.source_ref_id) != record.content_hash:
            raise CorrectionError("source_revision_not_current")
        if record.sync_policy != "full_sync" or record.permission_scope != "project":
            raise CorrectionError("source_policy_denied")
        if record.deleted_at or record.revoked_at:
            raise CorrectionError("source_deleted_or_revoked")
        revision_id = "src_" + short_hash([
            record.device_id_hash, record.root_id, record.relative_path_hash, record.content_hash,
        ])
        if record.source_ref_id != revision_id:
            raise CorrectionError("source_revision_not_content_addressed")
        age = (instant - latest_time).total_seconds()
        if age < -60 or age > rule["max_age_seconds"]:
            raise CorrectionError("source_stale")
        # 원문과 redacted byte hash가 다르면 이 adapter로는 원문을 검증할 수 없다.
        if not record.redacted_content or sha256_text(record.redacted_content) != record.content_hash:
            raise CorrectionError("source_hash_unverifiable")
        try:
            body = json.loads(record.redacted_content, object_pairs_hook=_unique_object)
            value = body
            for token in _pointer_tokens(binding["json_pointer"]):
                if isinstance(value, list):
                    if not token.isascii() or not token.isdecimal() or (len(token) > 1 and token[0] == "0"):
                        raise ValueError("invalid list index")
                    value = value[int(token)]
                else:
                    value = value[token]
        except (ValueError, TypeError, KeyError, IndexError):
            raise CorrectionError("source_field_unverifiable") from None
        if type(value) not in (str, int, bool) or (isinstance(value, str) and not value):
            raise CorrectionError("source_value_unsupported")
        mapping = rule["value_map"]
        if mapping:
            if stable_json(value) not in mapping:
                raise CorrectionError("source_value_unmapped")
            value = mapping[stable_json(value)]
        if not isinstance(value, str) or not value or len(value) > 2048:
            raise CorrectionError("source_value_unsupported")
        ensure_public_safe(value, "EvidenceCorrectionValue")
        values.append(value)
        proofs.append({"source_ref_id": record.source_ref_id, "content_hash": record.content_hash,
                       "observed_at": record.last_seen_at, "binding_hash": hash_payload(binding)})
    if len(set(values)) != 1:
        raise CorrectionError("authoritative_sources_conflict")
    return values[0], proofs


class EvidenceCorrectionService:
    def __init__(self, *, ledger: Any, store: Any, enabled: bool,
                 policies: tuple[dict, ...], invalidate: Callable[[], None], read: Callable[..., dict]):
        self.ledger, self.store, self.enabled = ledger, store, enabled
        self.policies, self.invalidate, self.read = policies, invalidate, read

    def correct(self, *, project: str, repository: str, target_object_id: str = "", target_memory_id: str = "",
                expected_content_hash: str, policy_id: str, idempotency_key: str,
                evidence_refs: list[dict[str, str]]) -> dict:
        arguments = dict(project=project, repository=repository, target_object_id=target_object_id,
                         target_memory_id=target_memory_id,
                         expected_content_hash=expected_content_hash, policy_id=policy_id,
                         idempotency_key=idempotency_key, evidence_refs=evidence_refs)
        _validate_request(arguments)
        rule = next((item for item in self.policies if item["policy_id"] == policy_id), None)
        request_hash = hash_payload({**arguments, "policy_hash": hash_payload(rule)})
        decision_id = "correction:" + short_hash([project, idempotency_key], length=40)
        try:
            if not self.enabled or not self.policies:
                raise CorrectionError("correction_capability_disabled")
            if self.store is None or getattr(self.ledger, "read_only", True):
                raise CorrectionError("writable_canonical_adapter_unavailable")
            if rule is None or rule["project"] != project or rule["repository"] != repository:
                raise CorrectionError("policy_scope_mismatch")
            previous = self.store.get_steward_decision(decision_id)
            if previous:
                card = previous["result_card"]
                if (previous["action"] != "correct_fact" or not isinstance(card, Mapping)
                        or card.get("evidence_correction", {}).get("request_hash") != request_hash):
                    raise CorrectionError("idempotency_conflict")
                return self._result(card, previous["result_target"], decision_id, duplicate=True)
            if target_memory_id:
                candidate_target = self.store.get_steward_card(target_memory_id)
                matched = [candidate_target] if candidate_target is not None else []
            else:
                cards = self.store.list_steward_cards(project=project, accepted_only=True, current_only=True, limit=100)
                matched = [card for card in cards if memory_card_to_knowledge_object(card).object_id == target_object_id]
            if len(matched) != 1:
                raise CorrectionError("target_revision_conflict")
            old = matched[0]
            target_object_id = target_object_id or memory_card_to_knowledge_object(old).object_id
            self._check_target(old, rule, target_object_id, expected_content_hash)
            catalog = LedgerSourceRefCatalog(self.ledger, initialize_schema=False)
            value, proofs = resolve_value(rule=rule, catalog=catalog, evidence_refs=evidence_refs)
            field = rule["field_path"]
            prior_value = _field(old, field)
            if prior_value == (rule["summary_template"].replace("{value}", value) if field == "summary" else value):
                raise CorrectionError("no_factual_mismatch")
            candidate_id = "mem_correction_" + short_hash([decision_id, request_hash], length=40)
            candidate = self.store.get_steward_card(candidate_id)
            if candidate is None:
                candidate = _candidate(old, rule, value, proofs, candidate_id, request_hash, decision_id)
                self.store.put_steward_proposal(candidate)
            elif candidate.get("evidence_correction", {}).get("request_hash") != request_hash:
                raise CorrectionError("successor_identity_conflict")

            def transform(current, target):
                self._check_target(target, rule, target_object_id, expected_content_hash)
                if (current.get("lifecycle_state") not in {"candidate", "needs_review", "suggested_accept"}
                        or current.get("evidence_correction", {}).get("request_hash") != request_hash):
                    raise CorrectionError("successor_revision_conflict")
                checked_value, checked_proofs = resolve_value(rule=rule, catalog=catalog, evidence_refs=evidence_refs)
                if checked_value != value or checked_proofs != proofs:
                    raise CorrectionError("source_changed_during_validation")
                accepted = copy.deepcopy(current)
                accepted.update(lifecycle_state="auto_accepted", approval_state="auto_accepted",
                                judgment_state="auto_status", status="accepted", auto_policy_ready=True,
                                authorization_status="active", currentness="current", freshness="current")
                validate_memory_card_envelope(accepted)
                return accepted, commit_supersession(target, superseded_by=accepted["memory_id"])

            changed = self.store.steward_decision(
                decision_id=decision_id, memory_id=candidate_id, action="correct_fact",
                actor=POLICY_VERSION, content_hash=candidate["content_hash"],
                target_memory_id=old["memory_id"], transform=transform,
            )
            return self._result(changed["card"], changed["target"], decision_id, duplicate=changed.get("duplicate", False))
        except (CorrectionError, DeadlockDetected, LockNotAvailable, SerializationFailure) as exc:
            # 첫 조회 뒤 다른 동일 요청이 commit했으면 같은 결정의 불변 결과만 재사용한다.
            if self.store is not None and self.enabled:
                previous = self.store.get_steward_decision(decision_id)
                card = previous.get("result_card") if previous else None
                if (previous and previous["action"] == "correct_fact" and isinstance(card, Mapping)
                        and card.get("evidence_correction", {}).get("request_hash") == request_hash):
                    return self._result(card, previous["result_target"], decision_id, duplicate=True)
            reason = exc.reason if isinstance(exc, CorrectionError) else "authority_lock_conflict"
            return self._denied(arguments, request_hash, reason)

    @staticmethod
    def _check_target(card, rule, object_id, expected_hash):
        if (not isinstance(card, Mapping) or card.get("project") != rule["project"]
                or card.get("scope") != "project" or card.get("brain_id") != f"/project/{rule['project']}"
                or card.get("currentness") != "current"
                or card.get("lifecycle_state") not in {"accepted", "human_accepted", "auto_accepted"}
                or card.get("approval_state") not in {"approved", "auto_accepted"}
                or card.get("conflicts")
                or card.get("content_hash") != expected_hash
                or memory_card_to_knowledge_object(card).object_id != object_id):
            raise CorrectionError("target_revision_conflict")
        origin = card.get("evidence_correction", {}).get("origin_memory_id", card["memory_id"])
        if origin != rule["memory_id"] or rule["field_path"] not in _FIELDS.get(card.get("card_type"), set()):
            raise CorrectionError("target_field_policy_mismatch")
        if not isinstance(_field(card, rule["field_path"]), str):
            raise CorrectionError("target_field_unsupported")

    def _result(self, card, old, decision_id, *, duplicate):
        self.invalidate()
        verified, cursor = False, None
        # 같은 공개 brain.resolve(list) 경로로 bounded pagination/read-back한다.
        for _ in range(50):
            try:
                page = self.read(project=card["project"], mode="list", response_mode="slim", limit=20, cursor=cursor)
            except Exception:
                break
            item = next((item for item in page.get("items", []) if item.get("id") == card["memory_id"]), None)
            if item:
                verified = (item.get("content_hash") == card["content_hash"]
                            and item.get("summary") == card["summary"]
                            and _field(item, card["evidence_correction"]["field_path"])
                            == _field(card, card["evidence_correction"]["field_path"]))
                break
            cursor = page.get("next_cursor")
            if not cursor:
                break
        try:
            stored_old = self.store.get_steward_card(old["memory_id"])
            stored_new = self.store.get_steward_card(card["memory_id"])
            lineage_verified = (
                bool(stored_old and stored_new) and stored_old.get("currentness") == "superseded"
                and card["memory_id"] in stored_old.get("superseded_by", [])
                and old["memory_id"] in stored_new.get("supersedes", [])
                and stored_new.get("currentness") == "current"
            )
        except Exception:
            lineage_verified = False
        verified = verified and lineage_verified
        result = {
            "schema_version": "brain_object_correction.v1",
            "correction_status": "committed" if verified else "committed_readback_gap",
            "authority_write_performed": not duplicate, "authoritative_memory_changed": not duplicate,
            "authority_basis": "evidence_policy", "decision_type": "correct_fact", "decision_id": decision_id,
            "duplicate": duplicate, "cache_invalidated": True, "read_after_write_verified": verified,
            "supersession_readback_verified": lineage_verified,
            "read_route": "brain.resolve:list", "successor_object": memory_card_to_knowledge_object(card).to_dict(),
            "successor_memory_id": card["memory_id"], "previous_memory_id": old["memory_id"],
            "previous_revision": old["content_hash"], "successor_revision": card["content_hash"],
            "supersedes": old["memory_id"], "evidence_validation_id": card["evidence_correction"]["request_hash"],
        }
        ensure_public_safe(result, "EvidenceCorrectionResult")
        return result

    def _denied(self, arguments, request_hash, reason):
        audit_id = None
        if self.store is not None:
            audit_id = self.store.record_correction_denial(request_hash=request_hash, reason=reason)
        proposal = ReviewProposal.from_parts(
            proposal_type="correct_from_evidence", target_object_id=(arguments["target_object_id"]
                or "memory:" + arguments["target_memory_id"]),
            reason=reason, evidence_refs=[request_hash],
        ).to_dict()
        rule = next((item for item in self.policies if item["policy_id"] == arguments["policy_id"]), None)
        if (self.enabled and rule and rule["project"] == arguments["project"]
                and rule["repository"] == arguments["repository"] and not getattr(self.ledger, "read_only", True)):
            proposal.update(project=rule["project"], object_type="MemoryCard",
                            proposal_write_performed=True, proposal_write_target="review_queue")
            proposal = self.ledger.upsert_object_review_proposal(proposal)
        result = {
            "schema_version": "brain_object_correction.v1",
            "correction_status": "conflict" if "conflict" in reason else "review_required",
            "authority_write_performed": False, "authoritative_memory_changed": False,
            "reason_code": reason, "audit_event_id": audit_id, "review_proposal": proposal,
        }
        ensure_public_safe(result, "EvidenceCorrectionDenied")
        return result


def _candidate(old, rule, value, proofs, memory_id, request_hash, decision_id):
    card = copy.deepcopy(old)
    for key in ("approved_by", "approved_at", "operator_approval_ref", "rejected_by", "rejected_at"):
        card.pop(key, None)
    card.pop("card_hash", None)
    field = rule["field_path"]
    summary = rule["summary_template"].replace("{value}", value)
    if field != "summary":
        card["typed_payload"][field.split(".", 1)[1]] = value
    card.update(memory_id=memory_id, candidate_id=memory_id,
                summary=summary, render_text=summary, lifecycle_state="candidate",
                approval_state="suggested", judgment_state="none", status="candidate",
                currentness="current", authorization_status="disabled", freshness="current",
                supersedes=[old["memory_id"]], superseded_by=[], valid_to=None,
                steward_proposal_kind="evidence_correction", steward_target_memory_id=old["memory_id"],
                idempotency_key=request_hash, created_at=utc_now_iso(), updated_at=utc_now_iso())
    observed = min(_timestamp(proof["observed_at"]) for proof in proofs)
    card.update(observed_at=observed.isoformat(), freshness_state="verified_current",
                freshness_checked_at=utc_now_iso(), source_hash=hash_payload(proofs),
                freshness_next_check_after=(observed + timedelta(seconds=rule["max_age_seconds"])).isoformat())
    if card["card_type"] == "status":
        card["typed_payload"]["observed_at"] = observed.isoformat()
    card["evidence_correction"] = {
        "request_hash": request_hash, "policy_id_hash": hash_payload(rule["policy_id"]),
        "policy_hash": hash_payload(rule), "decision_id": decision_id, "authority_basis": "evidence_policy",
        "field_path": field, "origin_memory_id": rule["memory_id"], "evidence_refs": proofs,
    }
    card["source_refs"] = [*card.get("source_refs", []), *[
        {"source_ref_id": proof["source_ref_id"], "content_hash": proof["content_hash"]} for proof in proofs
    ]]
    card["evidence_hashes"] = sorted(set([*card.get("evidence_hashes", []), *[proof["content_hash"] for proof in proofs]]))
    card["reason_capsule"] = {
        "rule_hits": ["source_field_mapping_verified", "target_revision_verified"],
        "deterministic_signals": [{"kind": "evidence_validation", "hash": request_hash}],
        "evidence_gap": [], "model_reason": "서버 source 정책의 필드 대응 검증을 통과했다.",
        "policy_version": POLICY_VERSION, "evaluator_version": POLICY_VERSION, "review_block_reason": "",
    }
    card.pop("content_hash", None)
    card["content_hash"] = hash_payload({key: item for key, item in card.items()
                                       if key not in {"created_at", "updated_at", "freshness_checked_at"}})
    return validate_memory_card_envelope(card)


def _validate_request(arguments):
    ensure_public_safe(arguments, "EvidenceCorrectionRequest")
    if not all(isinstance(arguments[key], str) for key in ("target_object_id", "target_memory_id")):
        raise ValueError("correction target identity type is invalid")
    if bool(arguments["target_object_id"]) == bool(arguments["target_memory_id"]):
        raise ValueError("exactly one correction target identity is required")
    for key in ("project", "repository", "policy_id", "idempotency_key"):
        _identity(arguments[key], key, maximum=240 if key == "repository" else 180)
    for key in ("target_object_id", "target_memory_id"):
        if arguments[key]:
            _identity(arguments[key], key, maximum=180 if key == "target_object_id" else 64)
    require_sha256(arguments["expected_content_hash"], "expected_content_hash")
    refs = arguments["evidence_refs"]
    if not isinstance(refs, list) or not 1 <= len(refs) <= 5:
        raise ValueError("correction evidence refs are invalid")
    for ref in refs:
        if not isinstance(ref, Mapping) or set(ref) != {"source_ref_id", "source_content_hash"}:
            raise ValueError("correction evidence ref fields are invalid")
        require_opaque_id(ref["source_ref_id"], "source_ref_id")
        require_sha256(ref["source_content_hash"], "source_content_hash")


def _field(card, field):
    return card.get("summary") if field == "summary" else card.get("typed_payload", {}).get(field.split(".", 1)[1])


def _locator(value):
    if isinstance(value, Mapping):
        return tuple(value[key] for key in ("root_id", "device_id_hash", "relative_path_hash"))
    return value.root_id, value.device_id_hash, value.relative_path_hash


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed.astimezone(timezone.utc)
    except ValueError:
        raise CorrectionError("source_observation_time_invalid") from None


def _identity(value, name, *, maximum):
    if not isinstance(value, str) or not value or len(value) > maximum or value.strip() != value or any(char.isspace() for char in value):
        raise ValueError(f"invalid correction {name}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _pointer_tokens(pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/") or len(pointer) > 1024:
        raise ValueError("correction JSON pointer is invalid")
    tokens = pointer[1:].split("/")
    for token in tokens:
        if "~" in token.replace("~0", "").replace("~1", ""):
            raise ValueError("correction JSON pointer escape is invalid")
    return [token.replace("~1", "/").replace("~0", "~") for token in tokens]
