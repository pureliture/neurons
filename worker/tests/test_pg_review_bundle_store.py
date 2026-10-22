"""리뷰 묶음 PG 경계: offline SQL 계약과 opt-in 실제 PG 회귀.

Offline 연결은 SQL 바인딩/호출 순서만 검증한다. PostgreSQL의 스냅샷,
잠금, 제약 및 원자성은 ``test_live_pg_*``와 isolated_pg_store가 검증한다.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json

import pytest

from agent_knowledge.postgres_store.pgvector_store import PgVectorStore


PROJECT = "review-bundle-test"
BRAIN_ID = "/project/review-bundle-test"
SCOPE = "project"


class _SQLTraceConnection:
    """응답을 주입하는 offline 연결. 실제 DB 동작을 흉내내지 않는다."""

    def __init__(self, responses=(), *, fail_on=""):
        self.responses = list(responses)
        self.executed = []
        self.events = []
        self.fail_on = fail_on

    def cursor(self):
        return _SQLTraceCursor(self)

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")

    def close(self):
        self.events.append("close")


class _SQLTraceCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        normalized = " ".join(query.split())
        self.connection.executed.append((normalized, deepcopy(tuple(params))))
        self.rows = []
        if self.connection.fail_on and self.connection.fail_on in normalized:
            raise RuntimeError("injected audit persistence failure")
        if (normalized.startswith("LOCK TABLE") or "pg_advisory_xact_lock" in normalized
                or normalized.startswith("SELECT set_config('lock_timeout'")):
            return
        if normalized.startswith("SELECT") or "RETURNING" in normalized:
            self.rows = deepcopy(self.connection.responses.pop(0))

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


def _owned_store(monkeypatch, connection):
    store = PgVectorStore(dsn="offline-test-only")
    monkeypatch.setattr(store, "_open_connection", lambda: connection)
    return store


def test_current_context_has_no_cap_and_filters_live_authority():
    envelopes = [{"memory_id": f"context-{number:04d}"} for number in range(125)]
    connection = _SQLTraceConnection([[{"steward_envelope": card} for card in envelopes]])
    store = PgVectorStore(connection=connection)

    assert store.list_steward_current_context(project=PROJECT) == envelopes

    assert len(connection.executed) == 1
    query, params = connection.executed[0]
    assert "LIMIT" not in query
    for clause in (
        "steward_envelope IS NOT NULL", "project = %s",
        "lifecycle_state IN ('accepted', 'human_accepted', 'auto_accepted')",
        "approval_state", "authorization_status = 'active'", "currentness = 'current'",
        "valid_from <=", "valid_to IS NULL OR valid_to >", "expires_at",
        "steward_proposal_kind",
    ):
        assert clause in query
    assert PROJECT in params
    assert connection.events == []


def _state_row(memory_id, **changes):
    card = {
        "memory_id": memory_id, "project": PROJECT, "brain_id": BRAIN_ID,
        "scope": SCOPE, "card_type": "decision", "title": "Safe review fixture",
        "summary": "Synthetic authority state", "typed_payload": {},
    }
    row = {
        "steward_envelope": card, "memory_id": memory_id, "project": PROJECT,
        "card_type": "decision", "title": card["title"], "summary": card["summary"],
        "typed_payload": {}, "lifecycle_state": "human_accepted",
        "authorization_status": "active", "currentness": "current",
        "confidence": Decimal("0.90"),
        "valid_from": datetime(2026, 1, 1, 9, tzinfo=timezone(timedelta(hours=9))),
        "valid_to": None,
        "content_hash": "sha256:" + hashlib.sha256(memory_id.encode()).hexdigest(),
        "source_ref": [],
    }
    row.update(changes)
    return row


def test_bundle_state_is_complete_sorted_serializable_in_one_statement():
    rows = [_state_row(f"current-{number:04d}") for number in range(125)]
    # Disabled/expired accepted-current SQL rows must stay in the fingerprint
    # input, so later reactivation cannot silently evade snapshot validation.
    rows[0].update(authorization_status="disabled", valid_to=datetime(2026, 1, 2, tzinfo=timezone.utc))
    candidate = _state_row("pending-member", lifecycle_state="candidate", authorization_status="disabled")
    connection = _SQLTraceConnection([[candidate, *reversed(rows)]])
    store = PgVectorStore(connection=connection)

    state = store.read_steward_bundle_state(
        project=PROJECT, brain_id=BRAIN_ID, scope=SCOPE,
        member_ids=[candidate["memory_id"], rows[0]["memory_id"]],
    )

    assert set(state) == {"members", "current"}
    assert [record["sql"]["memory_id"] for record in state["members"]] == ["current-0000", "pending-member"]
    assert [record["sql"]["memory_id"] for record in state["current"]] == [row["memory_id"] for row in rows]
    record = state["current"][0]
    assert set(record) == {"card", "sql"}
    assert set(record["sql"]) == set(rows[0]) - {"steward_envelope"}
    assert record["sql"]["confidence"] == 0.9
    assert record["sql"]["valid_from"] == "2026-01-01T00:00:00+00:00"
    assert record["sql"]["valid_to"] == "2026-01-02T00:00:00+00:00"
    assert record["card"] == rows[0]["steward_envelope"]
    json.dumps(state, allow_nan=False)
    assert len(connection.executed) == 1
    query, params = connection.executed[0]
    assert "memory_id = ANY(%s)" in query
    assert " OR " in query
    assert "project = %s" in query
    assert "steward_envelope->>'brain_id' = %s" in query
    assert "steward_envelope->>'scope' = %s" in query
    assert "LIMIT" not in query
    assert "authorization_status = 'active'" not in query
    assert "valid_from <=" not in query
    assert PROJECT in params and BRAIN_ID in params and SCOPE in params


@pytest.mark.parametrize("mismatch", ["missing", "legacy", "project", "brain_id", "scope", "sql_project", "memory_id"])
def test_bundle_state_refuses_unknown_or_out_of_scope_members(mismatch):
    row = _state_row("member")
    if mismatch == "legacy":
        row["steward_envelope"] = None
    elif mismatch in {"project", "brain_id", "scope", "memory_id"}:
        row["steward_envelope"][mismatch] = "other-value"
    elif mismatch == "sql_project":
        row["project"] = "another-project"
    connection = _SQLTraceConnection([[] if mismatch == "missing" else [row]])
    store = PgVectorStore(connection=connection)

    with pytest.raises(ValueError, match="unknown|scope|identity"):
        store.read_steward_bundle_state(
            project=PROJECT, brain_id=BRAIN_ID, scope=SCOPE, member_ids=["member"],
        )
    assert len(connection.executed) == 1


@pytest.mark.parametrize("changes", [
    {"project": ""}, {"brain_id": ""}, {"scope": ""},
    {"member_ids": [""]}, {"member_ids": ["member", "member"]},
    {"member_ids": "member"}, {"member_ids": ["x" * 65]},
])
def test_bundle_state_validates_identity_without_repairing_input(changes):
    connection = _SQLTraceConnection([[]])
    store = PgVectorStore(connection=connection)
    request = dict(project=PROJECT, brain_id=BRAIN_ID, scope=SCOPE, member_ids=["member"])
    request.update(changes)

    with pytest.raises(ValueError):
        store.read_steward_bundle_state(**request)
    assert connection.executed == []


def _proposal(tag="member", *, project=PROJECT, brain_id=BRAIN_ID, scope=SCOPE):
    from agent_knowledge.session_memory.memory_miner import build_memory_card_candidate_from_source_span

    span = {
        "card_type": "decision", "project": project, "brain_id": brain_id,
        "scope": scope, "provider": "hermes", "title": f"Fixture {tag}",
        "redacted_summary": "Synthetic review bundle fixture",
        "typed_payload": {"decision": "Review synthetic authority", "rationale": "Human gate",
                          "alternatives": [], "consequence": "Atomic review", "authority_ref": "fixture"},
        "source_ref": {"source_id": "fixture-source-" + tag},
        "span_ref": {"span_id": "fixture-span-" + tag},
        "content_hash": "sha256:" + hashlib.sha256(tag.encode()).hexdigest(),
    }
    return build_memory_card_candidate_from_source_span(span, refresh_watermark="fixture-cycle")


def _assert_common_authority_locks(connection):
    locks = _authority_lock_queries(connection)
    assert locks[0] == ("LOCK TABLE memory_cards IN SHARE ROW EXCLUSIVE MODE", ())
    query, params = locks[1]
    assert "pg_advisory_xact_lock" in query
    assert params == ("lbrain-steward-authority-write-v1",)


def _authority_lock_queries(connection):
    return [(query, params) for query, params in connection.executed
            if query.startswith("LOCK TABLE") or "pg_advisory_xact_lock" in query]


def test_proposal_acquires_table_then_common_lock_before_card_lock():
    candidate = _proposal()
    connection = _SQLTraceConnection([[{
        "steward_envelope": candidate, "content_hash": candidate["content_hash"],
        "lifecycle_state": candidate["lifecycle_state"],
    }]])
    store = PgVectorStore(connection=connection)

    assert store.put_steward_proposal(candidate) == candidate
    _assert_common_authority_locks(connection)
    assert _authority_lock_queries(connection)[2][1] == (candidate["memory_id"],)


def test_single_decision_acquires_same_locks_even_for_immutable_retry():
    candidate = _proposal()
    stored = {
        "decision_id": "single-decision", "memory_id": candidate["memory_id"],
        "content_hash": candidate["content_hash"], "action": "approve", "actor": "reviewer",
        "target_memory_id": None, "result_card": candidate, "result_target": None,
    }
    connection = _SQLTraceConnection([[stored]])
    store = PgVectorStore(connection=connection)

    def never_transform(*_args):
        pytest.fail("immutable retry must not execute the transform")

    assert store.steward_decision(
        decision_id=stored["decision_id"], memory_id=candidate["memory_id"],
        content_hash=candidate["content_hash"], action="approve", actor="reviewer",
        transform=never_transform,
    ) == {"card": candidate, "target": None}
    _assert_common_authority_locks(connection)
    assert _authority_lock_queries(connection)[2][1] == ("single-decision",)


def _bundle_request(**changes):
    request = {
        "decision_id": "bundle-decision", "request_fingerprint": "sha256:" + hashlib.sha256(b"request").hexdigest(),
        "project": PROJECT, "brain_id": BRAIN_ID, "scope": SCOPE, "member_ids": ["member"],
    }
    request.update(changes)
    return request


def _bundle_result():
    return {
        "action": "approve", "actor": "reviewer", "reason": "Reviewed synthetic fixtures",
        "member_ids": ["member"], "items": [{"memory_id": "member", "state": "accepted"}],
        "canonical_write_performed": True,
    }


def test_bundle_decision_owns_transaction_and_persists_exact_safe_result(monkeypatch):
    result = _bundle_result()
    connection = _SQLTraceConnection([[], [{"result": result}]])
    store = _owned_store(monkeypatch, connection)
    callbacks = []

    def decide(scoped_store):
        assert isinstance(scoped_store, PgVectorStore)
        assert scoped_store.connection is connection
        assert scoped_store.dsn is None
        assert connection.events == []
        _assert_common_authority_locks(connection)
        assert "FROM steward_bundle_decisions" in connection.executed[-1][0]
        callbacks.append(scoped_store)
        return deepcopy(result)

    assert store.steward_bundle_decision(**_bundle_request(), decide=decide) == result
    assert len(callbacks) == 1
    insert, params = connection.executed[-1]
    assert insert.startswith("INSERT INTO steward_bundle_decisions")
    assert "RETURNING result" in insert
    assert params[:5] == (
        "bundle-decision", _bundle_request()["request_fingerprint"], PROJECT, BRAIN_ID, SCOPE,
    )
    assert json.loads(params[5]) == ["member"]
    assert json.loads(params[6]) == result
    assert connection.events == ["commit", "close"]


def test_bundle_retry_returns_original_result_before_reading_members(monkeypatch):
    result = _bundle_result()
    previous = {**_bundle_request(), "result": result}
    connection = _SQLTraceConnection([[previous]])
    store = _owned_store(monkeypatch, connection)

    def never_decide(_store):
        pytest.fail("idempotent replay must not revalidate changed or expired members")

    assert store.steward_bundle_decision(**_bundle_request(), decide=never_decide) == result
    _assert_common_authority_locks(connection)
    assert sum("FROM steward_bundle_decisions" in query for query, _params in connection.executed) == 1
    assert all("FROM memory_cards" not in query for query, _params in connection.executed)
    assert all("INSERT" not in query for query, _params in connection.executed)
    assert connection.events == ["commit", "close"]


@pytest.mark.parametrize("changes", [
    {"request_fingerprint": "sha256:" + hashlib.sha256(b"different-request").hexdigest()},
    {"project": "another-project"}, {"brain_id": "/project/another-brain"},
    {"scope": "workspace"}, {"member_ids": ["another-member"]},
])
def test_bundle_retry_refuses_mismatch_before_callback(monkeypatch, changes):
    connection = _SQLTraceConnection([[{**_bundle_request(), "result": _bundle_result()}]])
    store = _owned_store(monkeypatch, connection)

    def never_decide(_store):
        pytest.fail("conflicting request must not execute decisions")

    with pytest.raises(ValueError, match="conflicting steward bundle decision id"):
        store.steward_bundle_decision(**_bundle_request(**changes), decide=never_decide)
    assert sum("FROM steward_bundle_decisions" in query for query, _params in connection.executed) == 1
    assert all("FROM memory_cards" not in query for query, _params in connection.executed)
    assert connection.events == ["rollback", "close"]


def test_bundle_requires_owned_outer_transaction():
    connection = _SQLTraceConnection([[], [{"result": _bundle_result()}]])
    store = PgVectorStore(connection=connection)
    callbacks = []

    with pytest.raises(ValueError, match="owned.*transaction"):
        store.steward_bundle_decision(
            **_bundle_request(), decide=lambda _store: callbacks.append(True) or _bundle_result(),
        )
    assert callbacks == []
    assert connection.executed == []
    assert connection.events == []


@pytest.mark.parametrize("changes", [
    {"decision_id": ""}, {"decision_id": None},
    {"request_fingerprint": ""}, {"request_fingerprint": None}, {"member_ids": []},
])
def test_bundle_decision_refuses_missing_identity_before_writes(monkeypatch, changes):
    connection = _SQLTraceConnection([[], [{"result": _bundle_result()}]])
    store = _owned_store(monkeypatch, connection)

    with pytest.raises(ValueError):
        store.steward_bundle_decision(**_bundle_request(**changes), decide=lambda _store: _bundle_result())
    assert connection.executed == []
    assert connection.events == []


@pytest.mark.parametrize("result", [None, [], {"non_finite": float("nan")}])
def test_bundle_decision_refuses_invalid_result_and_rolls_back(monkeypatch, result):
    connection = _SQLTraceConnection([[], [{"result": result}]])
    store = _owned_store(monkeypatch, connection)

    with pytest.raises(ValueError):
        store.steward_bundle_decision(**_bundle_request(), decide=lambda _store: result)
    assert all(not query.startswith("INSERT") for query, _params in connection.executed)
    assert connection.events == ["rollback", "close"]


@pytest.mark.parametrize("result", [None, []])
def test_bundle_retry_refuses_unavailable_safe_response(monkeypatch, result):
    connection = _SQLTraceConnection([[{**_bundle_request(), "result": result}]])
    store = _owned_store(monkeypatch, connection)

    with pytest.raises(ValueError, match="result unavailable"):
        store.steward_bundle_decision(**_bundle_request(), decide=lambda _store: pytest.fail("unsafe replay"))
    assert connection.events == ["rollback", "close"]


def test_bundle_schema_is_additive_repeatable_and_keeps_full_result():
    schema = PgVectorStore.schema_sql()
    prefix = "CREATE TABLE IF NOT EXISTS steward_bundle_decisions ("
    assert prefix in schema
    definition = schema.split(prefix, 1)[1].split("\n);", 1)[0]
    for declaration in (
        "decision_id TEXT PRIMARY KEY", "request_fingerprint TEXT NOT NULL",
        "project TEXT NOT NULL", "brain_id TEXT NOT NULL", "scope TEXT NOT NULL",
        "member_ids JSONB NOT NULL", "result JSONB NOT NULL",
        "decided_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
        "jsonb_typeof(result) = 'object'", "jsonb_typeof(member_ids) = 'array'",
    ):
        assert declaration in definition
    assert "DROP TABLE" not in schema


def test_authority_writer_timeouts_are_bounded_without_lengthening_existing_limits():
    candidate = _proposal()
    connection = _SQLTraceConnection([[{
        "steward_envelope": candidate, "content_hash": candidate["content_hash"],
        "lifecycle_state": candidate["lifecycle_state"],
    }]])
    store = PgVectorStore(connection=connection)

    store.put_steward_proposal(candidate)

    query, params = connection.executed[0]
    assert query.startswith("SELECT set_config('lock_timeout'")
    assert "set_config('statement_timeout'" in query
    assert "WHERE name = 'lock_timeout'" in query
    assert "WHERE name = 'statement_timeout'" in query
    assert query.count("LEAST(setting::bigint, %s)") == 2
    assert query.count("setting::bigint = 0") == 2
    assert params == (1000, 1000, 5000, 5000)
    _assert_common_authority_locks(connection)


def test_bundle_owner_sets_read_committed_before_any_snapshot(monkeypatch):
    connection = _SQLTraceConnection([[], [{"result": _bundle_result()}]])
    store = _owned_store(monkeypatch, connection)

    store.steward_bundle_decision(**_bundle_request(), decide=lambda _store: _bundle_result())

    assert connection.executed[0] == ("SET TRANSACTION ISOLATION LEVEL READ COMMITTED", ())
    assert connection.executed[1][0].startswith("SELECT set_config('lock_timeout'")
    _assert_common_authority_locks(connection)


@pytest.mark.parametrize("failure_stage", ["callback", "header"])
def test_bundle_exception_rolls_back_owned_transaction(monkeypatch, failure_stage):
    connection = _SQLTraceConnection([[]], fail_on="INSERT INTO steward_bundle_decisions" if failure_stage == "header" else "")
    store = _owned_store(monkeypatch, connection)
    callbacks = []

    def decide(scoped_store):
        assert scoped_store.connection is connection
        callbacks.append(True)
        if failure_stage == "callback":
            raise ValueError("synthetic member validation failure")
        return _bundle_result()

    error = ValueError if failure_stage == "callback" else RuntimeError
    with pytest.raises(error, match="synthetic|injected"):
        store.steward_bundle_decision(**_bundle_request(), decide=decide)
    assert callbacks == [True]
    assert connection.events == ["rollback", "close"]


def _accepted_envelope(tag, **changes):
    card = _proposal(tag)
    card.update(lifecycle_state="human_accepted", approval_state="approved",
                authorization_status="active", currentness="current",
                valid_from="2000-01-01T00:00:00+00:00", valid_to=None)
    card.update(changes)
    return card


def _seed_pg_envelopes(store, envelopes):
    from agent_knowledge.session_memory.llm_brain_service import _build_pg_card

    with store.transaction() as db:
        for envelope in envelopes:
            card = _build_pg_card(envelope, active=envelope.get("authorization_status") == "active")
            store.insert_card(card, conn=db)
            with db.cursor() as cur:
                cur.execute("UPDATE memory_cards SET steward_envelope = %s::jsonb WHERE memory_id = %s",
                            (json.dumps(envelope), card.memory_id))


def _create_pg_members(store, count=2):
    return [store.put_steward_proposal(_proposal(f"member-{index}")) for index in range(count)]


def _child_decision_id(bundle_id, member_id):
    return "bundle-member:" + hashlib.sha256(f"{bundle_id}|{member_id}".encode()).hexdigest()


def _approve_pg_member(store, bundle_id, card, *, fail=False):
    def transform(current, target):
        if fail:
            raise ValueError("synthetic second member validation failure")
        assert current["lifecycle_state"] in {"candidate", "needs_review", "suggested_accept"}
        assert target is None
        return {**current, "lifecycle_state": "human_accepted", "approval_state": "approved",
                "currentness": "current"}, None

    return store.steward_decision(
        decision_id=_child_decision_id(bundle_id, card["memory_id"]),
        memory_id=card["memory_id"], content_hash=card["content_hash"],
        action="approve", actor="reviewer", transform=transform,
    )


def _pg_effect_counts(store):
    with store._scope() as db:
        with db.cursor() as cur:
            cur.execute("""SELECT
                (SELECT count(*) FROM steward_bundle_decisions) AS bundles,
                (SELECT count(*) FROM steward_card_decisions) AS children,
                (SELECT count(*) FROM embedding_outbox) AS embeddings,
                (SELECT count(*) FROM graph_projection_outbox) AS projections""")
            return dict(cur.fetchone())


def _pg_request(members, bundle_id="bundle-decision"):
    return _bundle_request(decision_id=bundle_id, member_ids=sorted(card["memory_id"] for card in members))


def _approve_pg_bundle(scoped_store, request):
    state = scoped_store.read_steward_bundle_state(
        project=request["project"], brain_id=request["brain_id"], scope=request["scope"],
        member_ids=request["member_ids"],
    )
    items = [_approve_pg_member(scoped_store, request["decision_id"], record["card"])["card"]
             for record in state["members"]]
    return {**_bundle_result(), "member_ids": request["member_ids"], "items": items}


def test_live_pg_bundle_state_exceeds_100_and_uses_exact_scope(isolated_pg_store):
    store = isolated_pg_store
    accepted = [_accepted_envelope(f"authority-{index}") for index in range(125)]
    _seed_pg_envelopes(store, accepted)
    assert {card["memory_id"] for card in store.list_steward_current_context(PROJECT)} == {
        card["memory_id"] for card in accepted}

    disabled = _accepted_envelope("disabled", authorization_status="disabled",
                                  valid_to="2000-01-02T00:00:00+00:00")
    candidate = _proposal("pending")
    others = [
        _accepted_envelope("other-brain", brain_id="/project/another-brain"),
        _accepted_envelope("other-scope", scope="workspace"),
        _accepted_envelope("other-project", project="another-project"),
    ]
    _seed_pg_envelopes(store, [disabled, candidate, *others])
    from agent_knowledge.postgres_store.pgvector_store import MemoryCard
    store.insert_card(MemoryCard(memory_id="legacy-fixture", project=PROJECT, card_type="decision",
        title="Legacy fixture", summary="No steward envelope", lifecycle_state="human_accepted",
        authorization_status="active", content_hash="sha256:" + hashlib.sha256(b"legacy").hexdigest()))

    state = store.read_steward_bundle_state(
        project=PROJECT, brain_id=BRAIN_ID, scope=SCOPE, member_ids=[candidate["memory_id"]],
    )
    assert [record["sql"]["memory_id"] for record in state["members"]] == [candidate["memory_id"]]
    expected_ids = sorted(card["memory_id"] for card in [*accepted, disabled])
    assert [record["sql"]["memory_id"] for record in state["current"]] == expected_ids
    assert len(state["current"]) == 126
    json.dumps(state, allow_nan=False)
    assert all(isinstance(record["sql"]["confidence"], float) for record in state["current"])
    assert all(record["sql"]["valid_from"].endswith("+00:00") for record in state["current"])


def test_live_pg_current_context_obeys_authority_windows_and_status_expiry(isolated_pg_store):
    live = _accepted_envelope("live")
    status_expired = _accepted_envelope("status-expired", card_type="status",
        typed_payload={"status_value": "completed", "observed_at": "2000-01-01T00:00:00Z",
                       "expires_at": "2000-01-02T00:00:00Z", "current_authority": True})
    envelopes = [live, status_expired,
        _accepted_envelope("disabled", authorization_status="disabled"),
        _accepted_envelope("expired", valid_to="2000-01-02T00:00:00Z"),
        _accepted_envelope("future", valid_from="2999-01-01T00:00:00Z"),
        _accepted_envelope("unapproved", approval_state="suggested"),
        _accepted_envelope("superseded", currentness="superseded"),
        _accepted_envelope("stale-claim", steward_proposal_kind="stale"),
        _proposal("candidate"),
    ]
    _seed_pg_envelopes(isolated_pg_store, envelopes)
    assert [card["memory_id"] for card in isolated_pg_store.list_steward_current_context(PROJECT)] == [live["memory_id"]]
    state = isolated_pg_store.read_steward_bundle_state(
        project=PROJECT, brain_id=BRAIN_ID, scope=SCOPE, member_ids=[status_expired["memory_id"]],
    )
    # Fingerprints retain expired/disabled/current-but-unapproved authority rows.
    assert status_expired["memory_id"] in {record["sql"]["memory_id"] for record in state["current"]}


@pytest.mark.parametrize("which", ["missing", "project", "brain_id", "scope", "legacy"])
def test_live_pg_bundle_state_fails_closed_for_wrong_members(isolated_pg_store, which):
    store = isolated_pg_store
    member = _proposal("outside")
    if which in {"project", "brain_id", "scope"}:
        member[which] = {"project": "another-project", "brain_id": "/project/another-brain", "scope": "workspace"}[which]
    if which == "legacy":
        from agent_knowledge.session_memory.llm_brain_service import _build_pg_card
        store.insert_card(_build_pg_card(member, active=False))
    elif which != "missing":
        store.put_steward_proposal(member)
    with pytest.raises(ValueError, match="unknown|scope|identity"):
        store.read_steward_bundle_state(
            project=PROJECT, brain_id=BRAIN_ID, scope=SCOPE, member_ids=[member["memory_id"]],
        )


def test_live_pg_bundle_second_member_failure_rolls_back_every_effect(isolated_pg_store):
    store = isolated_pg_store
    members = _create_pg_members(store)
    request = _pg_request(members, "bundle-second-failure")
    applied = []

    def decide(scoped):
        state = scoped.read_steward_bundle_state(
            project=PROJECT, brain_id=BRAIN_ID, scope=SCOPE, member_ids=request["member_ids"],
        )
        first, second = [record["card"] for record in state["members"]]
        _approve_pg_member(scoped, request["decision_id"], first)
        applied.append(first["memory_id"])
        assert scoped.get_steward_card(first["memory_id"])["lifecycle_state"] == "human_accepted"
        assert _pg_effect_counts(scoped) == dict(bundles=0, children=1, embeddings=1, projections=1)
        _approve_pg_member(scoped, request["decision_id"], second, fail=True)
        pytest.fail("second member must fail")

    with pytest.raises(ValueError, match="second member validation failure"):
        store.steward_bundle_decision(**request, decide=decide)
    assert len(applied) == 1
    assert [store.get_steward_card(card["memory_id"]) for card in members] == members
    assert _pg_effect_counts(store) == dict(bundles=0, children=0, embeddings=0, projections=0)


def test_live_pg_bundle_replay_is_immutable_and_mismatch_refused(isolated_pg_store):
    store = isolated_pg_store
    members = _create_pg_members(store)
    request = _pg_request(members)
    callbacks = []

    def decide(scoped):
        callbacks.append(True)
        return _approve_pg_bundle(scoped, request)

    result = store.steward_bundle_decision(**request, decide=decide)
    assert _pg_effect_counts(store) == dict(bundles=1, children=2, embeddings=2, projections=2)
    with store.transaction() as db:
        with db.cursor() as cur:
            cur.execute("UPDATE memory_cards SET currentness = 'stale', valid_to = clock_timestamp() WHERE memory_id = ANY(%s)",
                        (request["member_ids"],))
    assert store.steward_bundle_decision(**request, decide=decide) == result
    assert callbacks == [True]
    assert all(card["currentness"] == "current" for card in result["items"])
    with pytest.raises(ValueError, match="conflicting steward bundle decision id"):
        store.steward_bundle_decision(**{**request, "request_fingerprint": "different-request"}, decide=decide)
    assert callbacks == [True]
    with store._scope() as db:
        with db.cursor() as cur:
            cur.execute("SELECT result, member_ids FROM steward_bundle_decisions WHERE decision_id = %s", (request["decision_id"],))
            saved = cur.fetchone()
    assert saved["result"] == result
    assert saved["member_ids"] == request["member_ids"]
    assert saved["result"]["reason"] == "Reviewed synthetic fixtures"
    assert _pg_effect_counts(store) == dict(bundles=1, children=2, embeddings=2, projections=2)


def test_live_pg_late_bundle_audit_failure_rolls_back_children_and_outboxes(isolated_pg_store):
    import psycopg

    store = isolated_pg_store
    members = _create_pg_members(store)
    request = _pg_request(members, "bundle-audit-failure")
    with store.transaction() as db:
        db.execute("ALTER TABLE steward_bundle_decisions ADD CONSTRAINT fixture_header_failure CHECK (decision_id <> 'bundle-audit-failure')")
    prepared = []

    def decide(scoped):
        result = _approve_pg_bundle(scoped, request)
        assert _pg_effect_counts(scoped) == dict(bundles=0, children=2, embeddings=2, projections=2)
        prepared.append(True)
        return result

    with pytest.raises(psycopg.errors.CheckViolation):
        store.steward_bundle_decision(**request, decide=decide)
    assert prepared == [True]
    assert [store.get_steward_card(card["memory_id"]) for card in members] == members
    assert _pg_effect_counts(store) == dict(bundles=0, children=0, embeddings=0, projections=0)


def test_live_pg_caller_owned_bundle_rejection_remains_atomic_after_caught_error(isolated_pg_store):
    store = isolated_pg_store
    members = _create_pg_members(store)
    request = _pg_request(members)
    callbacks = []
    with store.transaction() as db:
        scoped = PgVectorStore(connection=db)

        def decide(inner):
            callbacks.append(True)
            _approve_pg_member(inner, request["decision_id"], members[0])
            raise ValueError("caller catches the later error")

        try:
            scoped.steward_bundle_decision(**request, decide=decide)
        except ValueError as exc:
            assert "owned outer transaction" in str(exc)
            db.commit()  # 잘못된 caller가 예외를 잡고 commit해도 변경이 없어야 한다.
        else:
            pytest.fail("caller-owned bundle entry must fail closed")
    assert callbacks == []
    assert [store.get_steward_card(card["memory_id"]) for card in members] == members
    assert _pg_effect_counts(store) == dict(bundles=0, children=0, embeddings=0, projections=0)


def test_live_pg_bundle_schema_repeatable_preserves_decision_time_response(isolated_pg_store):
    store = isolated_pg_store
    members = _create_pg_members(store, count=1)
    request = _pg_request(members)
    result = store.steward_bundle_decision(**request, decide=lambda scoped: _approve_pg_bundle(scoped, request))
    store.execute_ddl()
    store.execute_ddl()
    assert store.steward_bundle_decision(**request, decide=lambda _store: pytest.fail("repeat must not rerun")) == result
    assert _pg_effect_counts(store) == dict(bundles=1, children=1, embeddings=1, projections=1)


@pytest.mark.parametrize("lock_ms,statement_ms,expected_lock,expected_statement", [
    (0, 0, 1000, 5000), (200, 800, 200, 800), (10000, 100, 1000, 100),
])
def test_live_pg_bundle_uses_read_committed_and_preserves_shorter_timeouts(
    isolated_pg_store, monkeypatch, lock_ms, statement_ms, expected_lock, expected_statement,
):
    import psycopg

    store = isolated_pg_store
    members = _create_pg_members(store, count=1)
    request = _pg_request(members)
    real_open = store._open_connection

    def open_with_session_defaults():
        db = real_open()
        db.execute("SELECT set_config('lock_timeout', %s, false), set_config('statement_timeout', %s, false)",
                   (f"{lock_ms}ms", f"{statement_ms}ms"))
        db.commit()
        db.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        return db

    monkeypatch.setattr(store, "_open_connection", open_with_session_defaults)

    def decide(scoped):
        with scoped._scope() as db:
            with db.cursor() as cur:
                cur.execute("""SELECT current_setting('transaction_isolation') AS isolation,
                    (SELECT setting::bigint FROM pg_settings WHERE name = 'lock_timeout') AS lock_ms,
                    (SELECT setting::bigint FROM pg_settings WHERE name = 'statement_timeout') AS statement_ms""")
                settings = cur.fetchone()
        assert settings == dict(isolation="read committed", lock_ms=expected_lock, statement_ms=expected_statement)
        state = scoped.read_steward_bundle_state(
            project=PROJECT, brain_id=BRAIN_ID, scope=SCOPE, member_ids=request["member_ids"],
        )
        assert len(state["members"]) == 1
        return {"validated": True, "action": "inspect", "actor": "reviewer", "reason": "Synthetic transaction limits"}

    assert store.steward_bundle_decision(**request, decide=decide)["validated"] is True
