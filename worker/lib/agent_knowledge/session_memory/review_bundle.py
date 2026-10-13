"""명시적 사람 검토 묶음. 읽기는 무기록, 서명은 프로세스 수명에만 유효하다."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

_VERSION = "brain_steward_review_bundle.v1"
# Snapshot은 권한 증거가 아니다. 재시작/다중 프로세스 이동 시 다시 검토해야 한다.
_SIGNING_KEY = secrets.token_bytes(32)
_CONTEXT_FIELDS = {
    "decision": ("decision", "rationale", "alternatives", "consequence"),
    "preference": ("preference", "explicitness", "confirmation_status", "applies_to"),
    "task": ("task_state", "next_action", "blocker", "owner_hint", "status"),
    "status": ("status_value", "observed_at", "expires_at", "current_authority"),
    "evidence": ("evidence_kind", "result_status", "count_refs"),
    "drift": ("subject", "expected_state", "observed_state", "drift_kind", "severity", "resolution_action"),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()


def _scope(project, brain_id, scope, member_ids) -> list[str]:
    from .memory_card import _ensure_no_forbidden_content
    for value, maximum in ((project, 64), (brain_id, 256), (scope, 64)):
        if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
            raise ValueError("review bundle requires exact nonempty scope")
    if (not isinstance(member_ids, list) or not 1 <= len(member_ids) <= 20
            or any(not isinstance(i, str) or not i or len(i) > 64 or i != i.strip() for i in member_ids)
            or len(set(member_ids)) != len(member_ids)):
        raise ValueError("review bundle requires one to twenty unique explicit members")
    try:
        _ensure_no_forbidden_content([project, brain_id, scope, *member_ids], "bundle_identity")
    except ValueError:
        raise ValueError("review bundle identity contains forbidden data") from None
    return sorted(member_ids)


def _subject(card: dict) -> tuple:
    title = re.sub(r"[^0-9a-z가-힣]+", " ", str(card.get("title") or "").lower()).strip()
    return (card.get("project"), card.get("brain_id"), card.get("scope"), card.get("card_type"), title)


def _state(service, *, project, brain_id, scope, member_ids, store=None) -> dict:
    state = (store or service._pg()).read_steward_bundle_state(
        project=project, brain_id=brain_id, scope=scope, member_ids=member_ids)
    if not isinstance(state, dict) or set(state) != {"members", "current"}:
        raise ValueError("review bundle context unavailable")
    for rows in state.values():
        if not isinstance(rows, list):
            raise ValueError("review bundle context unavailable")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("card"), dict) or not isinstance(row.get("sql"), dict):
                raise ValueError("review bundle context unavailable")
            card = row["card"]
            if tuple(card.get(k) for k in ("project", "brain_id", "scope")) != (project, brain_id, scope):
                raise ValueError("review bundle scope mismatch")
            from .memory_card import _ensure_no_forbidden_content
            try:
                _ensure_no_forbidden_content(row, "review_bundle_state")
            except ValueError:
                raise ValueError("review bundle contains forbidden data") from None
    state["members"].sort(key=lambda r: r["card"]["memory_id"])
    state["current"].sort(key=lambda r: r["card"]["memory_id"])
    if [r["card"]["memory_id"] for r in state["members"]] != member_ids:
        raise ValueError("review bundle member unavailable")
    return state


def _timestamp(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise ValueError("review bundle invalid time") from None


def _issues(row: dict, *, now: datetime, pending: bool) -> list[str]:
    from .memory_card import REVIEW_LIFECYCLE_STATES
    card, sql = row["card"], row["sql"]
    issues = []
    for key in ("memory_id", "project", "card_type", "title", "summary", "typed_payload", "content_hash",
                "lifecycle_state", "authorization_status", "currentness"):
        if key not in sql or sql[key] != card.get(key):
            issues.append("stored_state_mismatch")
            break
    for key in ("valid_from", "valid_to"):
        if card.get(key) and _timestamp(card[key]) != _timestamp(sql.get(key)):
            issues.append("stored_time_mismatch")
    if pending:
        if (card.get("lifecycle_state") not in REVIEW_LIFECYCLE_STATES
                or card.get("approval_state") not in {"suggested", "needs_review"}
                or card.get("authorization_status") != "disabled"):
            issues.append("member_not_pending")
        if (card.get("steward_proposal_kind", "candidate") not in {"candidate", "needs_review"}
                or card.get("steward_target_memory_id") or card.get("supersedes")):
            issues.append("dedicated_authority_change_required")
    else:
        if (card.get("approval_state") not in {"approved", "auto_accepted"}
                or card.get("authorization_status") != "active"):
            issues.append("current_context_inconsistent")
    expiry = _timestamp(sql.get("valid_to"))
    status_expiry = _timestamp((card.get("typed_payload") or {}).get("expires_at")) if card.get("card_type") == "status" else None
    active_until = _timestamp(card.get("active_until"))
    if (expiry and expiry <= now) or (status_expiry and status_expiry <= now) or (active_until and active_until <= now):
        issues.append("member_expired" if pending else "current_expired")
    return list(dict.fromkeys(issues))


def _live_current(records: list[dict], *, now: datetime) -> list[dict]:
    """Only rows that still carry live authority define subject conflicts.

    The full current set stays in the signed digest (any change forces a new
    review); disabled or out-of-window rows are excluded here so the check
    matches ``list_steward_current_context``'s live windows.
    """
    return [record for record in records if not _issues(record, now=now, pending=False)]


def _item(service, record: dict, *, now: datetime) -> dict:
    card = record["card"]
    from .brain_steward import assert_public_safe
    context = {k: card.get("typed_payload", {}).get(k) for k in _CONTEXT_FIELDS.get(card.get("card_type"), ())}
    item = {**service._review_item(card), "context": context,
            "observed_at": card.get("observed_at"), "valid_from": record["sql"].get("valid_from"),
            "valid_to": record["sql"].get("valid_to"),
            "blockers": _issues(record, now=now, pending=True)}
    try:
        return assert_public_safe(item, "review_bundle_item")
    except ValueError:
        raise ValueError("review bundle contains forbidden data") from None


def _token(claims: dict) -> str:
    body = base64.urlsafe_b64encode(_json(claims).encode()).decode().rstrip("=")
    signature = hmac.new(_SIGNING_KEY, body.encode(), hashlib.sha256).hexdigest()
    return body + "." + signature


def read_bundle(service, *, project, brain_id, scope, member_ids) -> dict:
    from .brain_steward import assert_public_safe
    ids = _scope(project, brain_id, scope, member_ids)
    state = _state(service, project=project, brain_id=brain_id, scope=scope, member_ids=ids)
    now = _now()
    items = [_item(service, r, now=now) for r in state["members"]]
    current_subjects = {_subject(r["card"]): r for r in _live_current(state["current"], now=now)}
    subjects = [_subject(r["card"]) for r in state["members"]]
    for item, subject in zip(items, subjects):
        if not subject[-1]:
            item["blockers"].append("subject_missing")
        if subjects.count(subject) > 1:
            item["blockers"].append("same_subject_in_review_bundle")
        conflict = current_subjects.get(subject)
        item["current_matches"] = []
        if conflict:
            item["blockers"].append("same_subject_already_current")
            old = conflict["card"]
            item["current_matches"].append({"memory_id": old["memory_id"], "title": old["title"], "summary": old["summary"]})
    groups = []
    for card_type in sorted({i["card_type"] for i in items}):
        members = [i for i in items if i["card_type"] == card_type]
        groups.append({"card_type": card_type, "count": len(members), "items": members})
    claims = {"version": _VERSION, "project": project, "brain_id": brain_id, "scope": scope,
              "member_ids": ids, "state_fingerprint": _digest(state),
              "issued_at": now.isoformat(), "expires_at": (now + timedelta(minutes=10)).isoformat()}
    result = {"schema_version": _VERSION, "project": project, "brain_id": brain_id, "scope": scope,
              "member_ids": ids, "count": len(ids), "groups": groups, "items": items,
              "context_complete": True, "current_context_count": len(state["current"]),
              "classification_is_advisory": True, "comparison": "exact_normalized_subject_only",
              "approval_is_automatic": False, "snapshot_token": _token(claims),
              "expires_at": claims["expires_at"], "write_performed": False}
    try:
        assert_public_safe(result, "review_bundle")
    except ValueError:
        raise ValueError("review bundle contains forbidden data") from None
    if len(_json(result).encode()) > 131072:
        raise ValueError("review bundle too large; select fewer explicit members")
    return result


def _decode_token(value: str) -> dict:
    try:
        body, signature = value.rsplit(".", 1)
        expected = hmac.new(_SIGNING_KEY, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        raw = base64.b64decode(body + "=" * (-len(body) % 4), altchars=b"-_", validate=True)
        claims = json.loads(raw)
        if not isinstance(claims, dict) or set(claims) != {"version", "project", "brain_id", "scope", "member_ids", "state_fingerprint", "issued_at", "expires_at"}:
            raise ValueError
        return claims
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("review bundle snapshot invalid") from None


def commit_bundle(service, *, project, brain_id, scope, member_ids, snapshot_token,
                  action, actor, decision_id, reason="") -> dict:
    from .brain_steward import assert_public_safe
    from .memory_card import _ensure_no_forbidden_content, validate_memory_card_envelope
    from .memory_evaluation import classify_candidate_block_reason
    from .memory_promotion import human_approve_memory_card_candidate, human_reject_memory_card_candidate

    ids = _scope(project, brain_id, scope, member_ids)
    if action not in ("approve", "reject"):
        raise ValueError("review bundle action must be approve or reject")
    for value, maximum in ((actor, 128), (decision_id, 256)):
        if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
            raise ValueError("review bundle decision identity required")
    if not isinstance(reason, str) or len(reason) > 2000 or (action == "reject" and not reason.strip()):
        raise ValueError("review bundle rejection requires a reason")
    if not isinstance(snapshot_token, str) or not 1 <= len(snapshot_token) <= 8192:
        raise ValueError("review bundle snapshot invalid")
    try:
        _ensure_no_forbidden_content([actor, decision_id, reason], "bundle_decision")
    except ValueError:
        raise ValueError("review bundle decision contains forbidden data") from None
    request_fingerprint = _digest({"version": _VERSION, "project": project, "brain_id": brain_id,
                                   "scope": scope, "member_ids": ids, "snapshot_token": snapshot_token,
                                   "action": action, "actor": actor, "reason": reason})

    def decide(store):
        # Audit retry is handled by the owner before invoking this callback:
        # an original committed result survives later state changes/expiry/restart.
        claims = _decode_token(snapshot_token)
        if (claims["version"] != _VERSION or (claims["project"], claims["brain_id"], claims["scope"], claims["member_ids"]) != (project, brain_id, scope, ids)):
            raise ValueError("review bundle snapshot scope or membership mismatch")
        now = _now()
        issued, expires = _timestamp(claims["issued_at"]), _timestamp(claims["expires_at"])
        if issued is None or expires is None or issued > now or expires <= now or expires - issued > timedelta(minutes=10):
            raise ValueError("review bundle snapshot expired or invalid")
        state = _state(service, project=project, brain_id=brain_id, scope=scope, member_ids=ids, store=store)
        if not hmac.compare_digest(_digest(state), str(claims["state_fingerprint"])):
            raise ValueError("review bundle changed; review again")
        current_subjects = {_subject(r["card"]) for r in _live_current(state["current"], now=now)}
        member_subjects = [_subject(r["card"]) for r in state["members"]]
        if action == "approve" and len(set(member_subjects)) != len(member_subjects):
            raise ValueError("review bundle contains competing members")
        current_issues = [i for r in state["current"] for i in _issues(r, now=now, pending=False) if i != "current_expired"]
        if current_issues:
            raise ValueError("review bundle current context inconsistent")
        planned = []
        for row in state["members"]:
            card = row["card"]
            if _issues(row, now=now, pending=True):
                raise ValueError("review bundle member is not eligible")
            if action == "approve" and (not _subject(card)[-1] or _subject(card) in current_subjects):
                raise ValueError("review bundle conflicts with current context")
            try:
                _ensure_no_forbidden_content(card, "bundle_member")
                validate_memory_card_envelope(card)
                child_id = "bundle-member:" + hashlib.sha256((decision_id + "|" + card["memory_id"]).encode()).hexdigest()
                if action == "approve":
                    if classify_candidate_block_reason(card):
                        raise ValueError("approval blocked")
                    changed = human_approve_memory_card_candidate(card, approved_by=actor, decision_id=child_id,
                                                                  user_reason=reason or None)["accepted_card"]
                else:
                    changed = human_reject_memory_card_candidate(card, rejected_by=actor, decision_id=child_id,
                                                                 reason=reason)["rejected_card"]
                    changed["rejection_reason"] = reason
                changed["bundle_decision_id"] = decision_id
                changed["authorization_status"] = "active" if action == "approve" else "disabled"
                _ensure_no_forbidden_content(changed, "bundle_transition")
                assert_public_safe(service._authority_item(changed), "bundle_decision_result")
            except ValueError:
                raise ValueError("review bundle member validation failed") from None
            planned.append((card, changed, child_id))
        # Every member/transformed result/privacy boundary is validated BEFORE
        # the first write; the PG owner makes audit/outboxes/results atomic.
        items = []
        for card, changed, child_id in planned:
            def transform(current, target, expected=card, result=changed):
                if _digest(current) != _digest(expected):
                    raise ValueError("review bundle member changed during commit")
                return result, None
            committed = store.steward_decision(decision_id=child_id, memory_id=card["memory_id"],
                                               content_hash=card["content_hash"], action=action, actor=actor, transform=transform)
            items.append(service._authority_item(committed["card"]))
        result = {"schema_version": "brain_steward_review_bundle_decision.v1", "project": project,
                  "brain_id": brain_id, "scope": scope, "decision_id": decision_id, "action": action,
                  "actor": actor, "reason": reason, "member_ids": ids, "count": len(ids), "items": items,
                  "canonical_write_performed": True, "authoritative_memory_changed": action == "approve"}
        return assert_public_safe(result, "review_bundle_decision")

    return service._pg().steward_bundle_decision(decision_id=decision_id, request_fingerprint=request_fingerprint,
                                                project=project, brain_id=brain_id, scope=scope, member_ids=ids, decide=decide)
