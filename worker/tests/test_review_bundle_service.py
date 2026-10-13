"""사람이 확인한 정확한 맥락 묶음의 읽기·서명 경계 (DB seam만 대역)."""
from copy import deepcopy
import hashlib

from agent_knowledge.session_memory.brain_steward import BrainStewardService
from test_pg_steward_authority import NoCardLedger


def _record(mid, title, *, project="neurons", payload=None):
    card = dict(memory_id=mid, project=project, brain_id="/project/neurons", scope="project",
                card_type="decision", title=title, summary=title + " 관련 결정",
                typed_payload=payload or {"decision": title, "rationale": "명시적 사용자 결정", "alternatives": [],
                                          "consequence": "다음 작업에 적용", "authority_ref": "opaque-design"},
                lifecycle_state="candidate", approval_state="suggested", authorization_status="disabled",
                currentness="unknown", steward_proposal_kind="candidate", content_hash="sha256:"+hashlib.sha256(mid.encode()).hexdigest())
    sql = {k: deepcopy(card[k]) for k in ("memory_id", "project", "card_type", "title", "summary", "typed_payload",
                                       "lifecycle_state", "authorization_status", "currentness", "content_hash")}
    sql.update(valid_from="2026-01-01T00:00:00+00:00", valid_to=None)
    return {"card": card, "sql": sql}


class StateStore:
    def __init__(self, records, current=()):
        self.records = deepcopy(records)
        self.current = deepcopy(list(current))
        self.writes = 0

    def read_steward_bundle_state(self, *, project, brain_id, scope, member_ids):
        return {"members": [deepcopy(r) for r in self.records if r["card"]["memory_id"] in member_ids],
                "current": deepcopy(self.current)}


def _service(store, *, allowed=False, read_only=False):
    ledger = NoCardLedger()
    ledger.read_only = read_only
    return BrainStewardService(ledger, pgvector_store=store, allow_restricted=allowed)


def test_review_bundle_read_contains_complete_grouped_context_without_writes():
    store = StateStore([_record("m-a", "PostgreSQL 권위"), _record("m-b", "승인과 배포 분리")])
    service = _service(store)
    assert callable(getattr(service, "review_bundle_read", None)), "context bundle read API is missing"
    result = service.review_bundle_read(project="neurons", brain_id="/project/neurons", scope="project",
                                        member_ids=["m-b", "m-a"])
    assert result["member_ids"] == ["m-a", "m-b"]
    assert result["count"] == 2
    assert result["groups"][0]["count"] == 2
    assert "PostgreSQL 권위" in result["groups"][0]["items"][0]["context"]["decision"]
    assert result["snapshot_token"]
    assert result["context_complete"] is True
    assert result["approval_is_automatic"] is False
    assert store.writes == 0


def test_bundle_commit_remains_denied_by_default():
    import pytest
    store = StateStore([_record("m-a", "PostgreSQL 권위")])
    service = _service(store)
    assert callable(getattr(service, "review_bundle_commit", None)), "restricted bundle commit API is missing"
    with pytest.raises(PermissionError):
        service.review_bundle_commit(project="neurons", brain_id="/project/neurons", scope="project",
                                     member_ids=["m-a"], snapshot_token="unused", action="approve", actor="user", decision_id="d-1")
    assert store.writes == 0


class TransitionStore(StateStore):
    def steward_bundle_decision(self, *, decide, **kwargs):
        result = decide(self)
        self.writes += 1
        return result

    def steward_decision(self, *, memory_id, transform, **kwargs):
        row = next(r for r in self.records if r["card"]["memory_id"] == memory_id)
        card, target = transform(deepcopy(row["card"]), None)
        return {"card": card, "target": target}


def _full_record(mid, title):
    from agent_knowledge.session_memory.memory_miner import build_memory_card_candidate_from_source_span
    record = _record(mid, title)
    card = record["card"]
    envelope = build_memory_card_candidate_from_source_span({
        "card_type": "decision", "project": "neurons", "provider": "hermes", "scope": "project",
        "title": title, "redacted_summary": card["summary"], "typed_payload": card["typed_payload"],
        "content_hash": card["content_hash"], "source_ref": {"source_id": "opaque-source"},
        "span_ref": {"span_id": "opaque-span"}, "confidence": 0.9, "confidence_basis": "명시적 결정",
    }, refresh_watermark="2026-10-13T00:00:00+00:00")
    envelope.update(memory_id=mid, authorization_status="disabled", steward_proposal_kind="candidate")
    record["card"] = envelope
    record["sql"].update({k: deepcopy(envelope[k]) for k in ("lifecycle_state", "currentness")})
    return record


def test_explicit_signed_bundle_can_be_human_approved_together():
    from agent_knowledge.session_memory import review_bundle
    assert callable(getattr(review_bundle, "commit_bundle", None)), "bundle decision implementation is missing"
    store = TransitionStore([_full_record("m-a", "PostgreSQL 권위"), _full_record("m-b", "승인과 배포 분리")])
    service = _service(store, allowed=True)
    bundle = service.review_bundle_read(project="neurons", brain_id="/project/neurons", scope="project", member_ids=["m-a", "m-b"])
    result = service.review_bundle_commit(project="neurons", brain_id="/project/neurons", scope="project",
                                         member_ids=bundle["member_ids"], snapshot_token=bundle["snapshot_token"],
                                         action="approve", actor="user", decision_id="bundle-1")
    assert result["canonical_write_performed"] is True
    assert result["count"] == 2
    assert {i["memory_id"] for i in result["items"]} == {"m-a", "m-b"}
    assert all(i["lifecycle_state"] == "human_accepted" for i in result["items"])
    assert store.writes == 1


def _bundle_request(service, ids=("m-a",)):
    bundle = service.review_bundle_read(project="neurons", brain_id="/project/neurons", scope="project", member_ids=list(ids))
    return dict(project="neurons", brain_id="/project/neurons", scope="project", member_ids=bundle["member_ids"],
                snapshot_token=bundle["snapshot_token"], action="approve", actor="user", decision_id="bundle-x")


def test_active_until_expired_candidate_cannot_be_approved():
    import pytest
    row = _full_record("m-a", "지난 관측")
    row["card"]["active_until"] = "2025-01-01T00:00:00+00:00"
    store = TransitionStore([row])
    service = _service(store, allowed=True)
    request = _bundle_request(service)
    with pytest.raises(ValueError, match="eligible"):
        service.review_bundle_commit(**request)
    assert store.writes == 0


def test_private_nested_non_display_field_cannot_receive_a_review_token():
    import pytest
    row = _full_record("m-a", "합성 검토")
    row["card"]["typed_payload"]["detail"] = {"nested": {"private_hint": "/Users/private/source-secret.txt"}}
    store = StateStore([row])
    with pytest.raises(ValueError, match="forbidden"):
        _bundle_request(_service(store))
    assert store.writes == 0


import pytest


@pytest.mark.parametrize("field,value", [
    ("title", "확인 뒤 바뀐 제목"), ("summary", "확인 뒤 바뀐 내용"),
    ("typed_payload", {"decision": "다른 판단"}), ("source_refs", [{"source_id": "changed-locator"}]),
    ("approval_state", "approved"), ("currentness", "stale"),
    ("observed_at", "2026-01-01T00:00:00+00:00"),
])
def test_same_content_hash_changed_context_refuses_commit(field, value):
    store = TransitionStore([_full_record("m-a", "초기 판단")])
    service = _service(store, allowed=True)
    request = _bundle_request(service)
    store.records[0]["card"][field] = value
    with pytest.raises(ValueError, match="changed"):
        service.review_bundle_commit(**request)
    assert store.writes == 0


def test_sql_authority_change_refuses_commit_even_if_envelope_is_same():
    store = TransitionStore([_full_record("m-a", "초기 판단")])
    service = _service(store, allowed=True)
    request = _bundle_request(service)
    store.records[0]["sql"]["authorization_status"] = "active"
    with pytest.raises(ValueError, match="changed"):
        service.review_bundle_commit(**request)
    assert store.writes == 0


def test_modified_signature_and_membership_are_rejected():
    store = TransitionStore([_full_record("m-a", "하나"), _full_record("m-b", "둘")])
    service = _service(store, allowed=True)
    request = _bundle_request(service, ("m-a", "m-b"))
    corrupted = request["snapshot_token"][:-1] + ("0" if request["snapshot_token"][-1] != "0" else "1")
    with pytest.raises(ValueError, match="snapshot invalid"):
        service.review_bundle_commit(**{**request, "snapshot_token": corrupted})
    with pytest.raises(ValueError, match="membership mismatch"):
        service.review_bundle_commit(**{**request, "member_ids": ["m-a"]})
    assert store.writes == 0


def test_expired_token_and_rotated_process_key_need_new_review(monkeypatch):
    from datetime import timedelta
    from agent_knowledge.session_memory import review_bundle
    store = TransitionStore([_full_record("m-a", "유효시간 확인")])
    service = _service(store, allowed=True)
    request = _bundle_request(service)
    original_now = review_bundle._now()
    monkeypatch.setattr(review_bundle, "_now", lambda: original_now + timedelta(minutes=11))
    with pytest.raises(ValueError, match="expired"):
        service.review_bundle_commit(**request)
    monkeypatch.setattr(review_bundle, "_now", lambda: original_now)
    monkeypatch.setattr(review_bundle, "_SIGNING_KEY", b"different-process-key")
    with pytest.raises(ValueError, match="snapshot invalid"):
        service.review_bundle_commit(**request)
    assert store.writes == 0


def test_explicit_bulk_rejection_preserves_reason():
    store = TransitionStore([_full_record("m-a", "하나"), _full_record("m-b", "둘")])
    service = _service(store, allowed=True)
    request = _bundle_request(service, ("m-a", "m-b"))
    result = service.review_bundle_commit(**{**request, "action": "reject", "reason": "이미 끝난 작업"})
    assert result["reason"] == "이미 끝난 작업"
    assert all(i["lifecycle_state"] == "human_rejected" for i in result["items"])
    assert result["authoritative_memory_changed"] is False


def test_readonly_transport_stays_denied_with_review_capability():
    store = TransitionStore([_full_record("m-a", "변경 금지")])
    service = _service(store, allowed=True, read_only=True)
    with pytest.raises(ValueError, match="writable transport"):
        service.review_bundle_commit(**_bundle_request(service))
    assert store.writes == 0


def test_current_and_intra_bundle_subject_conflict_block_approval_not_rejection():
    rows = [_full_record("m-a", "같은 주제"), _full_record("m-b", "같은 주제")]
    store = TransitionStore(rows)
    service = _service(store, allowed=True)
    request = _bundle_request(service, ("m-a", "m-b"))
    with pytest.raises(ValueError, match="competing"):
        service.review_bundle_commit(**request)
    service.review_bundle_commit(**{**request, "action": "reject", "reason": "중복"})


def test_new_current_memory_invalidates_previously_reviewed_context():
    store = TransitionStore([_full_record("m-a", "정식 판단")])
    service = _service(store, allowed=True)
    request = _bundle_request(service)
    old = _full_record("m-old", "새로운 다른 맥락")
    old["card"].update(lifecycle_state="human_accepted", approval_state="approved", authorization_status="active")
    old["sql"].update(lifecycle_state="human_accepted", authorization_status="active")
    store.current.append(old)
    with pytest.raises(ValueError, match="changed"):
        service.review_bundle_commit(**request)
    assert store.writes == 0
