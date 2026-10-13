"""서명된 사람 검토 묶음의 실제 PG 수용 검사.

실행은 부모의 보호된 runner에 맡긴다. 모든 데이터·실패 주입은
``isolated_pg_store``가 만든 함수 소유 schema 안에만 존재한다.
후보와 정식 승인은 실제 BrainStewardService로 만들며 SQLite 카드,
모델, 그래프 서버, 운영 DB는 사용하지 않는다.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets
import threading

import psycopg
from psycopg import sql
import pytest

from agent_knowledge.postgres_store.pgvector_store import PgVectorStore
from agent_knowledge.session_memory import review_bundle
from agent_knowledge.session_memory.brain_steward import BrainStewardService
from test_pg_steward_authority import NoCardLedger, SPAN


PROJECT = "steward-test"
BRAIN_ID = "/project/steward-test"
SCOPE = "project"
# 전달되는 actor는 인증 주체가 아니라 advisory label이다. 실제 사람 승인 아님.
TEST_ACTOR = "synthetic-test-human"
_TABLE_KEYS = {
    "memory_cards": "memory_id",
    "steward_card_decisions": "decision_id",
    "steward_bundle_decisions": "decision_id",
    "embedding_outbox": "outbox_id",
    "graph_projection_outbox": "projection_id",
}


def _service(store, *, allowed=True, read_only=False):
    ledger = NoCardLedger()
    ledger.read_only = read_only
    return BrainStewardService(ledger, pgvector_store=store, allow_restricted=allowed)


def _span(marker, *, title=None, project=PROJECT, brain_id=None, scope=SCOPE):
    span = deepcopy(SPAN)
    span.update(
        project=project,
        brain_id=brain_id or f"/project/{project}",
        scope=scope,
        title=title if title is not None else f"Bundle fixture {marker}",
        redacted_summary=f"Synthetic preference for {marker}",
        source_ref={"source_id": f"bundle-source-{marker}"},
        span_ref={"span_id": f"bundle-span-{marker}"},
        content_hash="sha256:" + hashlib.sha256(f"review-bundle|{project}|{marker}".encode()).hexdigest(),
    )
    span["typed_payload"].update(preference=span["redacted_summary"], applies_to=f"fixture-{marker}")
    return span


def _candidate(service, marker, **span_kwargs):
    return service.candidate_create(source_span=_span(marker, **span_kwargs))["memory_id"]


def _read_arguments(member_ids):
    return dict(project=PROJECT, brain_id=BRAIN_ID, scope=SCOPE, member_ids=list(member_ids))


def _request(service, member_ids, *, decision_id="bundle-decision", action="approve", reason=""):
    bundle = service.review_bundle_read(**_read_arguments(member_ids))
    return {
        **_read_arguments(bundle["member_ids"]),
        "snapshot_token": bundle["snapshot_token"],
        "action": action,
        "actor": TEST_ACTOR,
        "decision_id": decision_id,
        "reason": reason,
    }


def _owned_schema(db):
    row = db.execute("SELECT current_schema() AS schema_name").fetchone()
    namespace = row["schema_name"]
    assert re.fullmatch(r"c1_test_[0-9a-f]{32}", namespace or ""), "function-owned test schema required"
    # search_path에 public이 있어도 테스트 table을 public에서 읽거나 바꾸지 않는다.
    for table in _TABLE_KEYS:
        row = db.execute(
            """SELECT n.nspname AS schema_name FROM pg_class c
               JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE c.oid = to_regclass(%s)""",
            (table,),
        ).fetchone()
        assert row is not None and row["schema_name"] == namespace
    return namespace


def _db_snapshot(store):
    """새 연결로 정확한 소유 table의 행을 읽는다. 토큰·DSN은 포함하지 않는다."""
    with store._scope() as db:
        namespace = _owned_schema(db)
        return {
            table: db.execute(
                sql.SQL("SELECT * FROM {} ORDER BY {}").format(
                    sql.Identifier(namespace, table), sql.Identifier(key)
                )
            ).fetchall()
            for table, key in _TABLE_KEYS.items()
        }


def _assert_pending(store, member_ids):
    for memory_id in member_ids:
        card = store.get_card(memory_id)
        envelope = store.get_steward_card(memory_id)
        assert card is not None and envelope is not None
        assert card.lifecycle_state == envelope["lifecycle_state"] == "candidate"
        assert card.authorization_status == envelope["authorization_status"] == "disabled"
        assert envelope["approval_state"] == "suggested"


def _assert_decided_rows(store, member_ids, receipt, *, action):
    state = _db_snapshot(store)
    ids = set(member_ids)
    assert len(state["memory_cards"]) == len(ids)
    assert len(state["steward_card_decisions"]) == len(ids)
    assert {row["memory_id"] for row in state["steward_card_decisions"]} == ids
    assert all(row["action"] == action and row["actor"] == TEST_ACTOR for row in state["steward_card_decisions"])
    assert len(state["steward_bundle_decisions"]) == 1
    header = state["steward_bundle_decisions"][0]
    assert header["decision_id"] == receipt["decision_id"]
    assert (header["project"], header["brain_id"], header["scope"]) == (PROJECT, BRAIN_ID, SCOPE)
    assert header["member_ids"] == sorted(ids)
    assert header["result"] == receipt
    approved = action == "approve"
    expected_state = "human_accepted" if approved else "human_rejected"
    for row in state["memory_cards"]:
        card = row["steward_envelope"]
        assert row["lifecycle_state"] == card["lifecycle_state"] == expected_state
        assert row["authorization_status"] == card["authorization_status"] == ("active" if approved else "disabled")
        assert card["bundle_decision_id"] == receipt["decision_id"]
        assert card["approval_state"] == ("approved" if approved else "rejected")
        if approved:
            assert row["currentness"] == card["currentness"] == "current"
        else:
            assert card["rejection_reason"] == receipt["reason"]
    expected_jobs = len(ids) if approved else 0
    assert len(state["embedding_outbox"]) == len(state["graph_projection_outbox"]) == expected_jobs
    if approved:
        assert {row["target_id"] for row in state["embedding_outbox"]} == ids
        assert {row["source_id"] for row in state["graph_projection_outbox"]} == ids
        assert all(row["status"] == "queued" for row in state["embedding_outbox"] + state["graph_projection_outbox"])
    return state


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_pg_bulk_two_members_read_is_pure_and_retry_is_exact(isolated_pg_store, action):
    store = isolated_pg_store
    service = _service(store)
    ids = [_candidate(service, "first"), _candidate(service, "second")]
    before = _db_snapshot(store)

    bundle = service.review_bundle_read(**_read_arguments(list(reversed(ids))))
    assert bundle["schema_version"] == "brain_steward_review_bundle.v1"
    assert bundle["count"] == 2
    assert bundle["member_ids"] == sorted(ids)
    assert bundle["context_complete"] is True and bundle["current_context_count"] == 0
    assert bundle["write_performed"] is False and bundle["approval_is_automatic"] is False
    assert bundle["classification_is_advisory"] is True
    grouped = [item for group in bundle["groups"] for item in group["items"]]
    assert sum(group["count"] for group in bundle["groups"]) == len(grouped) == 2
    assert {item["memory_id"] for item in grouped} == set(ids)
    assert all(item["context"]["explicitness"] == "explicit" and not item["blockers"] for item in bundle["items"])
    assert _db_snapshot(store) == before
    _assert_pending(store, ids)

    request = {
        **_read_arguments(bundle["member_ids"]), "snapshot_token": bundle["snapshot_token"],
        "action": action, "actor": TEST_ACTOR, "decision_id": f"bulk-{action}",
        "reason": "Synthetic human reviewed both preferences",
    }
    result = service.review_bundle_commit(**request)
    assert result["schema_version"] == "brain_steward_review_bundle_decision.v1"
    assert result["canonical_write_performed"] is True
    assert result["authoritative_memory_changed"] is (action == "approve")
    assert result["count"] == 2 and result["member_ids"] == sorted(ids)
    assert {item["memory_id"] for item in result["items"]} == set(ids)
    fresh = PgVectorStore(dsn=store.dsn)
    committed = _assert_decided_rows(fresh, ids, result, action=action)
    assert _service(fresh).review_bundle_commit(**request) == result
    assert _db_snapshot(fresh) == committed
    # 人工 검토 capability는 별도의 자동 수락 capability를 열지 않는다.
    with pytest.raises(PermissionError):
        service.candidate_auto_accept(candidate_memory_id=ids[0], evaluation={}, operator_approval_ref="synthetic-auto-denied")
    assert _db_snapshot(fresh) == committed


@pytest.mark.parametrize("later_change", ["stale", "supersede"])
def test_pg_committed_retry_survives_later_authority_change_expiry_and_restart(isolated_pg_store, monkeypatch, later_change):
    store = isolated_pg_store
    service = _service(store)
    ids = [_candidate(service, "retry-first"), _candidate(service, "retry-second")]
    issued = review_bundle._now()
    monkeypatch.setattr(review_bundle, "_now", lambda: issued)
    request = _request(service, ids, decision_id="immutable-bulk", reason="Original synthetic review")
    original = service.review_bundle_commit(**request)

    if later_change == "stale":
        proposal = service.stale_mark(memory_id=ids[0], reason="Synthetic observation is obsolete")["memory_id"]
        service.stale_commit(proposal_memory_id=proposal, approved_by=TEST_ACTOR, decision_id="later-stale")
    else:
        proposal = service.supersede_propose(
            old_memory_id=ids[0], source_span=_span("replacement", title=store.get_card(ids[0]).title)
        )["memory_id"]
        service.supersede_commit(proposal_memory_id=proposal, approved_by=TEST_ACTOR, decision_id="later-supersede")
    assert store.get_card(ids[0]).currentness == ("stale" if later_change == "stale" else "superseded")
    assert all(item["currentness"] == "current" for item in original["items"])

    monkeypatch.setattr(review_bundle, "_now", lambda: issued + timedelta(minutes=11))
    monkeypatch.setattr(review_bundle, "_SIGNING_KEY", secrets.token_bytes(32))
    fresh = PgVectorStore(dsn=store.dsn)
    before_retry = _db_snapshot(fresh)
    assert _service(fresh).review_bundle_commit(**request) == original
    assert _db_snapshot(fresh) == before_retry
    for change in (
        {"actor": "another-synthetic-human"},
        {"action": "reject", "reason": "A different synthetic decision"},
        {"reason": "Changed synthetic reason"},
    ):
        with pytest.raises(ValueError, match="conflicting.*bundle decision"):
            _service(fresh).review_bundle_commit(**{**request, **change})
        assert _db_snapshot(fresh) == before_retry


def test_pg_second_graph_enqueue_failure_rolls_back_every_member_audit_and_outbox(isolated_pg_store, monkeypatch):
    store = isolated_pg_store
    service = _service(store)
    ids = [_candidate(service, "rollback-first"), _candidate(service, "rollback-second")]
    request = _request(service, ids, decision_id="second-outbox-failure")
    before = _db_snapshot(store)
    original = PgVectorStore._enqueue_graph_outbox_on
    calls = []

    def fail_second(self, db, **kwargs):
        calls.append(kwargs["source_id"])
        if len(calls) == 2:
            raise RuntimeError("injected second graph enqueue failure")
        return original(self, db, **kwargs)

    # Bundle의 child store는 새 인스턴스다. 클래스 seam에만 실패를 주입한다.
    with monkeypatch.context() as patch:
        patch.setattr(PgVectorStore, "_enqueue_graph_outbox_on", fail_second)
        with pytest.raises(RuntimeError, match="injected second graph enqueue failure"):
            service.review_bundle_commit(**request)
    assert calls == sorted(ids)
    assert _db_snapshot(PgVectorStore(dsn=store.dsn)) == before
    _assert_pending(store, ids)
    # 실패한 결정 ID는 기록되지 않았다. 원래 토큰으로 정상 재시도가 가능하다.
    result = service.review_bundle_commit(**request)
    _assert_decided_rows(store, ids, result, action="approve")


def test_pg_late_header_insert_failure_rolls_back_already_written_children(isolated_pg_store):
    store = isolated_pg_store
    service = _service(store)
    ids = [_candidate(service, "header-first"), _candidate(service, "header-second")]
    request = _request(service, ids, decision_id="late-header-failure")
    before = _db_snapshot(store)
    with store.transaction() as db:
        namespace = _owned_schema(db)
        function = sql.Identifier(namespace, "review_bundle_fail_header")
        table = sql.Identifier(namespace, "steward_bundle_decisions")
        db.execute(sql.SQL("""CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF (SELECT count(*) FROM {}.steward_card_decisions) <> 2
                   OR (SELECT count(*) FROM {}.graph_projection_outbox) <> 2 THEN
                    RAISE EXCEPTION 'header injection did not reach all child writes';
                END IF;
                RAISE EXCEPTION 'injected late bundle header insert failure';
            END; $$""").format(function, sql.Identifier(namespace), sql.Identifier(namespace)))
        db.execute(sql.SQL("CREATE TRIGGER review_bundle_fail_header BEFORE INSERT ON {} "
                           "FOR EACH ROW EXECUTE FUNCTION {}()").format(table, function))
    try:
        with pytest.raises(psycopg.errors.RaiseException, match="injected late bundle header insert failure"):
            service.review_bundle_commit(**request)
        assert _db_snapshot(PgVectorStore(dsn=store.dsn)) == before
        _assert_pending(store, ids)
    finally:
        with store.transaction() as db:
            assert _owned_schema(db) == namespace
            db.execute(sql.SQL("DROP TRIGGER review_bundle_fail_header ON {}").format(table))
            db.execute(sql.SQL("DROP FUNCTION {}()").format(function))
    result = service.review_bundle_commit(**request)
    _assert_decided_rows(store, ids, result, action="approve")


@pytest.mark.parametrize("mutation", ["title", "payload", "envelope-state", "sql-state", "source-ref", "sql-window"])
def test_pg_same_hash_changed_stored_context_invalidates_snapshot(isolated_pg_store, mutation):
    store = isolated_pg_store
    service = _service(store)
    ids = [_candidate(service, "changed-first"), _candidate(service, "changed-second")]
    request = _request(service, ids, decision_id=f"changed-{mutation}")
    memory_id = ids[1]
    card = deepcopy(store.get_steward_card(memory_id))
    original_hash = card["content_hash"]
    with store.transaction() as db:
        namespace = _owned_schema(db)
        table = sql.Identifier(namespace, "memory_cards")
        if mutation == "title":
            card["title"] = "Changed reviewed title"
            db.execute(sql.SQL("UPDATE {} SET title = %s, steward_envelope = %s::jsonb WHERE memory_id = %s").format(table),
                       (card["title"], json.dumps(card), memory_id))
        elif mutation == "payload":
            card["typed_payload"]["preference"] = "Changed reviewed preference"
            db.execute(sql.SQL("UPDATE {} SET typed_payload = %s::jsonb, steward_envelope = %s::jsonb WHERE memory_id = %s").format(table),
                       (json.dumps(card["typed_payload"]), json.dumps(card), memory_id))
        elif mutation in {"envelope-state", "source-ref"}:
            if mutation == "envelope-state":
                card["approval_state"] = "approved"
            else:
                card["source_refs"] = [{"source_id": "changed-opaque-source"}]
            db.execute(sql.SQL("UPDATE {} SET steward_envelope = %s::jsonb WHERE memory_id = %s").format(table),
                       (json.dumps(card), memory_id))
        elif mutation == "sql-state":
            db.execute(sql.SQL("UPDATE {} SET authorization_status = 'active' WHERE memory_id = %s").format(table), (memory_id,))
        else:
            db.execute(sql.SQL("UPDATE {} SET valid_to = valid_from + interval '1 hour' WHERE memory_id = %s").format(table), (memory_id,))
    assert store.get_card(memory_id).content_hash == store.get_steward_card(memory_id)["content_hash"] == original_hash
    after_external_change = _db_snapshot(store)
    with pytest.raises(ValueError, match="changed; review again"):
        service.review_bundle_commit(**request)
    assert _db_snapshot(PgVectorStore(dsn=store.dsn)) == after_external_change
    assert all(not after_external_change[table] for table in _TABLE_KEYS if table != "memory_cards")


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_pg_new_current_memory_invalidates_whole_scoped_context(isolated_pg_store, action):
    store = isolated_pg_store
    service = _service(store)
    ids = [_candidate(service, "context-first"), _candidate(service, "context-second")]
    request = _request(service, ids, action=action, reason="Synthetic whole-context review")
    extra = _candidate(service, "new-current")
    service.candidate_approve(candidate_memory_id=extra, approved_by=TEST_ACTOR, decision_id="separate-human-approval")
    changed = _db_snapshot(store)
    with pytest.raises(ValueError, match="changed; review again"):
        service.review_bundle_commit(**request)
    assert _db_snapshot(store) == changed
    _assert_pending(store, ids)
    assert len(changed["steward_card_decisions"]) == 1 and not changed["steward_bundle_decisions"]


def test_pg_other_exact_scope_current_does_not_change_selected_context(isolated_pg_store):
    store = isolated_pg_store
    service = _service(store)
    ids = [_candidate(service, "scope-first"), _candidate(service, "scope-second")]
    request = _request(service, ids)
    # 같은 project라도 brain_id 또는 scope가 다르면 선택 맥락에 합치지 않는다.
    other = _candidate(service, "other-scope", brain_id="/project/steward-test-other", scope="workspace")
    service.candidate_approve(candidate_memory_id=other, approved_by=TEST_ACTOR, decision_id="other-exact-scope")
    result = service.review_bundle_commit(**request)
    assert result["count"] == 2 and set(result["member_ids"]) == set(ids)
    assert store.get_card(other).lifecycle_state == "human_accepted"
    assert len(_db_snapshot(store)["steward_card_decisions"]) == 3


def test_pg_complete_current_set_finds_conflict_beyond_first_hundred(isolated_pg_store):
    store = isolated_pg_store
    service = _service(store)
    oldest = _candidate(service, "oldest-current", title="Existing subject beyond page")
    service.candidate_approve(candidate_memory_id=oldest, approved_by=TEST_ACTOR, decision_id="current-oldest")
    for index in range(100):
        memory_id = _candidate(service, f"current-{index:03d}")
        service.candidate_approve(candidate_memory_id=memory_id, approved_by=TEST_ACTOR, decision_id=f"current-{index:03d}")
    capped = store.list_steward_cards(project=PROJECT, accepted_only=True, current_only=True, limit=100)
    assert len(capped) == 100 and oldest not in {card["memory_id"] for card in capped}
    selected = _candidate(service, "outside-capped-page", title="EXISTING--subject beyond page")
    bundle = service.review_bundle_read(**_read_arguments([selected]))
    assert bundle["current_context_count"] == 101 and bundle["context_complete"] is True
    assert "same_subject_already_current" in bundle["items"][0]["blockers"]
    assert [item["memory_id"] for item in bundle["items"][0]["current_matches"]] == [oldest]
    request = {**_read_arguments([selected]), "snapshot_token": bundle["snapshot_token"],
               "action": "approve", "actor": TEST_ACTOR, "decision_id": "beyond-hundred", "reason": ""}
    before = _db_snapshot(store)
    with pytest.raises(ValueError, match="conflicts with current context"):
        service.review_bundle_commit(**request)
    assert _db_snapshot(store) == before
    rejected = service.review_bundle_commit(**{**request, "action": "reject", "reason": "Duplicate synthetic subject"})
    assert rejected["count"] == 1 and store.get_card(selected).lifecycle_state == "human_rejected"
    assert store.get_card(oldest).currentness == "current"


def test_pg_competing_normalized_subjects_block_approval_but_allow_rejection(isolated_pg_store):
    store = isolated_pg_store
    service = _service(store)
    ids = [_candidate(service, "competing-one", title="Synthetic subject"),
           _candidate(service, "competing-two", title="SYNTHETIC--subject")]
    bundle = service.review_bundle_read(**_read_arguments(ids))
    assert all("same_subject_in_review_bundle" in item["blockers"] for item in bundle["items"])
    request = {**_read_arguments(ids), "snapshot_token": bundle["snapshot_token"],
               "action": "approve", "actor": TEST_ACTOR, "decision_id": "competing", "reason": ""}
    before = _db_snapshot(store)
    with pytest.raises(ValueError, match="competing members"):
        service.review_bundle_commit(**request)
    assert _db_snapshot(store) == before
    result = service.review_bundle_commit(**{**request, "action": "reject", "reason": "Conflicting synthetic alternatives"})
    _assert_decided_rows(store, ids, result, action="reject")


def test_pg_new_unselected_candidate_is_never_added_automatically(isolated_pg_store):
    store = isolated_pg_store
    service = _service(store)
    ids = [_candidate(service, "explicit-first"), _candidate(service, "explicit-second")]
    request = _request(service, list(reversed(ids)), decision_id="explicit-members-only")
    new_id = _candidate(service, "unselected-after-review")
    before = _db_snapshot(store)
    with pytest.raises(ValueError, match="membership mismatch"):
        service.review_bundle_commit(**{**request, "member_ids": ids + [new_id]})
    assert _db_snapshot(store) == before
    result = service.review_bundle_commit(**request)
    assert result["member_ids"] == sorted(ids) and result["count"] == 2
    _assert_pending(store, [new_id])
    state = _db_snapshot(PgVectorStore(dsn=store.dsn))
    assert len(state["memory_cards"]) == 3
    assert {row["memory_id"] for row in state["steward_card_decisions"]} == set(ids)
    assert {row["source_id"] for row in state["graph_projection_outbox"]} == set(ids)
    assert state["steward_bundle_decisions"][0]["member_ids"] == sorted(ids)


@pytest.mark.parametrize("proposal_kind", ["stale", "supersede"])
def test_pg_plain_bulk_cannot_decide_dedicated_authority_change(isolated_pg_store, proposal_kind):
    store = isolated_pg_store
    service = _service(store)
    old = _candidate(service, "dedicated-old")
    service.candidate_approve(candidate_memory_id=old, approved_by=TEST_ACTOR, decision_id="dedicated-existing")
    if proposal_kind == "stale":
        proposal = service.stale_mark(memory_id=old, reason="Synthetic stale observation")["memory_id"]
    else:
        proposal = service.supersede_propose(old_memory_id=old, source_span=_span("dedicated-replacement"))["memory_id"]
    plain = _candidate(service, "dedicated-plain")
    request = _request(service, [proposal, plain], decision_id="not-a-dedicated-commit")
    bundle = service.review_bundle_read(**_read_arguments([proposal, plain]))
    item = next(item for item in bundle["items"] if item["memory_id"] == proposal)
    assert "dedicated_authority_change_required" in item["blockers"]
    before = _db_snapshot(store)
    for action in ("approve", "reject"):
        with pytest.raises(ValueError, match="not eligible"):
            service.review_bundle_commit(**{**request, "action": action, "reason": "Synthetic plain decision"})
        assert _db_snapshot(store) == before
    assert store.get_card(old).currentness == "current"
    _assert_pending(store, [plain])


@pytest.mark.parametrize("boundary", ["default-denied", "read-only"])
def test_pg_bulk_commit_permission_boundary_leaves_no_changes(isolated_pg_store, boundary):
    store = isolated_pg_store
    writer = _service(store)
    ids = [_candidate(writer, "denied-first"), _candidate(writer, "denied-second")]
    denied = _service(store, allowed=boundary != "default-denied", read_only=boundary == "read-only")
    request = _request(denied, ids, decision_id="boundary-denied")
    before = _db_snapshot(store)
    if boundary == "default-denied":
        with pytest.raises(PermissionError):
            denied.review_bundle_commit(**request)
    else:
        with pytest.raises(ValueError, match="writable transport"):
            denied.review_bundle_commit(**request)
    assert _db_snapshot(store) == before
    _assert_pending(store, ids)


@pytest.mark.parametrize("field,value", [("project", "other-project"), ("brain_id", "/project/other-brain"), ("scope", "workspace")])
def test_pg_token_cannot_move_to_another_exact_scope(isolated_pg_store, field, value):
    store = isolated_pg_store
    service = _service(store)
    ids = [_candidate(service, "wrong-scope-one"), _candidate(service, "wrong-scope-two")]
    request = _request(service, ids)
    before = _db_snapshot(store)
    with pytest.raises(ValueError, match="scope"):
        service.review_bundle_read(**{**_read_arguments(ids), field: value})
    with pytest.raises(ValueError, match="snapshot scope"):
        service.review_bundle_commit(**{**request, field: value})
    assert _db_snapshot(store) == before


@pytest.mark.parametrize("case", ["expired", "future-issuer", "rotated-key", "naive-issuer", "overlong-ttl", "missing-issuer", "missing-expiry", "expiry-before-issue"])
def test_pg_signed_snapshot_time_and_process_key_fail_closed(isolated_pg_store, monkeypatch, case):
    store = isolated_pg_store
    service = _service(store)
    ids = [_candidate(service, "time-first"), _candidate(service, "time-second")]
    issued = datetime.now(timezone.utc)
    monkeypatch.setattr(review_bundle, "_now", lambda: issued)
    request = _request(service, ids, decision_id=f"invalid-time-{case}")
    if case == "expired":
        monkeypatch.setattr(review_bundle, "_now", lambda: issued + timedelta(minutes=10))
    elif case == "future-issuer":
        monkeypatch.setattr(review_bundle, "_now", lambda: issued - timedelta(seconds=1))
    elif case == "rotated-key":
        monkeypatch.setattr(review_bundle, "_SIGNING_KEY", secrets.token_bytes(32))
    else:
        # 실제 PG snapshot claims를 유지한 채 issuer의 잘못된 시각만 서명한다.
        # 키를 읽거나 내보내지 않으며 authority 상태는 만들어내지 않는다.
        claims = review_bundle._decode_token(request["snapshot_token"])
        if case == "naive-issuer":
            claims["issued_at"] = issued.replace(tzinfo=None).isoformat()
        elif case == "overlong-ttl":
            claims["expires_at"] = (issued + timedelta(minutes=11)).isoformat()
        elif case == "missing-issuer":
            claims["issued_at"] = None
        elif case == "missing-expiry":
            claims["expires_at"] = None
        else:
            claims["expires_at"] = (issued - timedelta(seconds=1)).isoformat()
        request["snapshot_token"] = review_bundle._token(claims)
    before = _db_snapshot(store)
    with pytest.raises(ValueError, match="snapshot|invalid time"):
        service.review_bundle_commit(**request)
    assert _db_snapshot(store) == before
    _assert_pending(store, ids)
    # 다시 읽은 현재 프로세스의 snapshot은 정상 승인된다.
    monkeypatch.setattr(review_bundle, "_now", lambda: issued + timedelta(minutes=11))
    fresh_request = _request(service, ids, decision_id="new-review-after-invalid-token")
    result = service.review_bundle_commit(**fresh_request)
    _assert_decided_rows(store, ids, result, action="approve")


def test_pg_status_expires_inside_still_valid_snapshot_without_authority_write(isolated_pg_store, monkeypatch):
    store = isolated_pg_store
    service = _service(store)
    issued = datetime.now(timezone.utc)
    monkeypatch.setattr(review_bundle, "_now", lambda: issued)
    span = _span("expiring-status")
    span.update(card_type="status", observed_at=issued.isoformat(), typed_payload={
        "status_value": "Synthetic test observation", "observed_at": issued.isoformat(),
        "expires_at": (issued + timedelta(minutes=1)).isoformat(), "current_authority": "synthetic-test-observation",
    })
    status_id = service.candidate_create(source_span=span)["memory_id"]
    other = _candidate(service, "status-other-member")
    request = _request(service, [status_id, other], decision_id="expired-status")
    before = _db_snapshot(store)
    monkeypatch.setattr(review_bundle, "_now", lambda: issued + timedelta(minutes=2))
    with pytest.raises(ValueError, match="not eligible"):
        service.review_bundle_commit(**request)
    assert _db_snapshot(store) == before
    _assert_pending(store, [status_id, other])
    bundle = service.review_bundle_read(**_read_arguments([status_id, other]))
    assert "member_expired" in next(item for item in bundle["items"] if item["memory_id"] == status_id)["blockers"]


def test_pg_expired_current_same_subject_is_not_a_live_conflict(isolated_pg_store, monkeypatch):
    """만료된 current 행은 권위가 아니므로 같은 주제 갱신 승인을 막지 않는다."""
    store = isolated_pg_store
    service = _service(store)
    issued = datetime.now(timezone.utc)
    monkeypatch.setattr(review_bundle, "_now", lambda: issued)

    def _status_span(marker, expires_at, value):
        span = _span(marker)
        span.update(card_type="status", title="Bundle repeated status observation",
                    observed_at=issued.isoformat(),
                    typed_payload={"status_value": value, "observed_at": issued.isoformat(),
                                   "expires_at": expires_at.isoformat(),
                                   "current_authority": "synthetic-test-observation"})
        return span

    old_id = service.candidate_create(
        source_span=_status_span("expiry-old", issued + timedelta(minutes=1), "이전 관측"))["memory_id"]
    service.candidate_approve(candidate_memory_id=old_id, approved_by=TEST_ACTOR,
                              decision_id="expiry-old-approve")
    new_id = service.candidate_create(
        source_span=_status_span("expiry-new", issued + timedelta(minutes=30), "갱신 관측"))["memory_id"]
    # 이전 status가 만료된 시점에 갱신 후보를 검토한다.
    monkeypatch.setattr(review_bundle, "_now", lambda: issued + timedelta(minutes=2))
    bundle = service.review_bundle_read(**_read_arguments([new_id]))
    assert "same_subject_already_current" not in bundle["items"][0]["blockers"]
    assert bundle["items"][0]["current_matches"] == []
    result = service.review_bundle_commit(**_request(service, [new_id], decision_id="expiry-renewal"))
    assert result["action"] == "approve" and result["count"] == 1
    fresh = PgVectorStore(dsn=store.dsn)
    assert fresh.get_card(new_id).lifecycle_state == "human_accepted"
    # 만료 행은 자동 정리되지 않는다: 승인과 무관하게 그대로 남는다.
    old = fresh.get_card(old_id)
    assert (old.lifecycle_state, old.authorization_status, old.currentness) == \
        ("human_accepted", "active", "current")
    state = _db_snapshot(store)
    assert len(state["steward_bundle_decisions"]) == 1
    assert state["steward_bundle_decisions"][0]["member_ids"] == [new_id]


def _race_public_calls(first_call, second_call):
    """두 public 호출 진입 직전에만 barrier를 둔다. PG lock 안에서는 기다리지 않는다."""
    barrier = threading.Barrier(3)

    def invoke(call):
        barrier.wait(timeout=5)
        try:
            return "committed", call()
        except ValueError as exc:
            # Deadlock/lock timeout/DB 오류는 잡지 않는다. 실제 오류면 검사 실패다.
            return "stale", str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invoke, first_call), pool.submit(invoke, second_call)]
        barrier.wait(timeout=5)
        return [future.result(timeout=25) for future in futures]


def test_pg_concurrent_single_and_bulk_never_commit_partial_bundle(isolated_pg_store):
    store = isolated_pg_store
    first = _service(store)
    second = _service(PgVectorStore(dsn=store.dsn))
    ids = [_candidate(first, "race-single"), _candidate(first, "race-bulk-other")]
    request = _request(second, ids, decision_id="race-bulk")
    outcomes = _race_public_calls(
        lambda: first.candidate_approve(candidate_memory_id=ids[0], approved_by=TEST_ACTOR, decision_id="race-single"),
        lambda: second.review_bundle_commit(**request),
    )
    assert sorted(status for status, _ in outcomes) == ["committed", "stale"]
    stale = next(value for status, value in outcomes if status == "stale")
    assert re.search(r"changed|pending|eligible", stale)
    state = _db_snapshot(PgVectorStore(dsn=store.dsn))
    if outcomes[1][0] == "committed":
        _assert_decided_rows(store, ids, outcomes[1][1], action="approve")
    else:
        assert store.get_card(ids[0]).lifecycle_state == "human_accepted"
        _assert_pending(store, [ids[1]])
        assert len(state["steward_card_decisions"]) == 1 and not state["steward_bundle_decisions"]
        assert len(state["embedding_outbox"]) == len(state["graph_projection_outbox"]) == 1
        assert state["steward_card_decisions"][0]["decision_id"] == "race-single"


@pytest.mark.parametrize("competition", ["overlapping", "disjoint", "same-decision"])
def test_pg_concurrent_bundles_are_atomic_or_exact_same_decision_retry(isolated_pg_store, competition):
    store = isolated_pg_store
    first = _service(store)
    second = _service(PgVectorStore(dsn=store.dsn))
    ids = [_candidate(first, f"bulk-race-{index}") for index in range(4)]
    first_request = _request(first, ids[:2], decision_id="race-first-bulk")
    if competition == "same-decision":
        second_request = {**first_request, "member_ids": list(reversed(first_request["member_ids"]))}
    else:
        members = ids[1:3] if competition == "overlapping" else ids[2:]
        second_request = _request(second, list(reversed(members)), decision_id="race-second-bulk")
    outcomes = _race_public_calls(
        lambda: first.review_bundle_commit(**first_request),
        lambda: second.review_bundle_commit(**second_request),
    )
    if competition == "same-decision":
        assert all(status == "committed" for status, _ in outcomes)
        assert outcomes[0][1] == outcomes[1][1]
        winning_ids = set(first_request["member_ids"])
    else:
        assert sorted(status for status, _ in outcomes) == ["committed", "stale"]
        assert "changed; review again" in next(value for status, value in outcomes if status == "stale")
        winning_ids = set(next(value for status, value in outcomes if status == "committed")["member_ids"])
    assert len(winning_ids) == 2
    state = _db_snapshot(PgVectorStore(dsn=store.dsn))
    assert len(state["memory_cards"]) == 4
    assert {row["memory_id"] for row in state["memory_cards"] if row["authorization_status"] == "active"} == winning_ids
    _assert_pending(store, set(ids) - winning_ids)
    assert len(state["steward_card_decisions"]) == 2
    assert {row["memory_id"] for row in state["steward_card_decisions"]} == winning_ids
    assert len(state["steward_bundle_decisions"]) == 1
    assert set(state["steward_bundle_decisions"][0]["member_ids"]) == winning_ids
    assert len(state["embedding_outbox"]) == len(state["graph_projection_outbox"]) == 2
    assert {row["target_id"] for row in state["embedding_outbox"]} == winning_ids
    assert {row["source_id"] for row in state["graph_projection_outbox"]} == winning_ids
