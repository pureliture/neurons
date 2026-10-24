"""Offline regressions exercising Steward and the real PG adapter's SQL binds.

Only the injected DB connection is a test double. These tests do not exercise
PostgreSQL transactions, constraints, locks, or a live authority database.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re

import pytest

from agent_knowledge.postgres_store.pgvector_store import PgVectorStore
from agent_knowledge.session_memory.brain_steward import BrainStewardService, assert_public_safe
from agent_knowledge.session_memory.memory_miner import build_memory_card_candidate_from_source_span

PROJECT = "steward-provenance-test"
OBSERVED_AT = "2026-10-10T12:00:00Z"


def _span(tag="original"):
    return {
        "source_ref": {"source_id": "fixture-source-" + tag},
        "span_ref": {"span_id": "fixture-span-" + tag},
        "content_hash": "sha256:" + hashlib.sha256(tag.encode()).hexdigest(),
        "card_type": "decision",
        "project": PROJECT,
        "brain_id": "/workspace/steward-provenance-test",
        "scope": "workspace",
        "provider": "hermes",
        "title": "Preserve the storage boundary",
        "redacted_summary": "PostgreSQL is the authority store; candidates require review.",
        "typed_payload": {
            "decision": "Keep PostgreSQL as the authority store",
            "rationale": "One authority store",
            "alternatives": [],
            "consequence": "Candidates require human review",
            "authority_ref": "fixture-design",
        },
    }


def _candidate(tag="original"):
    candidate = build_memory_card_candidate_from_source_span(
        _span(tag), refresh_watermark="original-miner-cycle"
    )
    # The prebuilt envelope carries source observation time, not persistence time.
    candidate["observed_at"] = OBSERVED_AT
    candidate["created_at"] = "2026-10-11T13:00:00Z"
    return candidate


class _RecordingConnection:
    """Capture only the SQL touched here; reject unexpected DB operations."""

    def __init__(self):
        self.executed = []
        self.rows = {}
        self.decisions = {}
        self.embedding_jobs = {}
        self.graph_jobs = {}
        self.committed_at = datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc)

    def cursor(self):
        return _RecordingCursor(self)


class _RecordingCursor:
    def __init__(self, connection):
        self.connection = connection
        self.response = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        query = " ".join(query.split())
        self.connection.executed.append((query, deepcopy(tuple(params))))
        self.response = []
        self.rowcount = 0
        if query.startswith("SELECT pg_advisory_xact_lock"):
            return
        if query.startswith("SELECT decision_id"):
            decision = self.connection.decisions.get(params[0])
            if decision is not None:
                self.response = [deepcopy(decision)]
            return
        if query.startswith("SELECT memory_id, steward_envelope, content_hash"):
            self.response = [deepcopy(self.connection.rows[key]) for key in params[0]
                             if key in self.connection.rows]
            return
        if query == "SELECT clock_timestamp()":
            self.response = [{"clock_timestamp": self.connection.committed_at}]
            return
        if query.startswith("INSERT INTO memory_cards"):
            columns = query.split("(", 1)[1].split(")", 1)[0].split(", ")
            row = dict(zip(columns, params, strict=True))
            for field in ("typed_payload", "source_ref"):
                row[field] = json.loads(row[field])
            assert row["memory_id"] not in self.connection.rows
            self.connection.rows[row["memory_id"]] = row
            self.rowcount = 1
            return
        if query.startswith("UPDATE memory_cards SET steward_envelope") and len(params) == 2:
            envelope, memory_id = params
            self.connection.rows[memory_id]["steward_envelope"] = json.loads(envelope)
            self.rowcount = 1
            return
        if query.startswith("UPDATE memory_cards SET steward_envelope") and len(params) == 7:
            envelope, lifecycle, authorization, currentness, valid_from, valid_to, memory_id = params
            row = self.connection.rows[memory_id]
            row.update(steward_envelope=json.loads(envelope), lifecycle_state=lifecycle,
                       authorization_status=authorization, currentness=currentness, valid_to=valid_to)
            if valid_from is not None:
                row["valid_from"] = datetime.fromisoformat(valid_from)
            self.rowcount = 1
            return
        if query.startswith("INSERT INTO steward_card_decisions"):
            names = ("decision_id", "memory_id", "content_hash", "action", "actor",
                     "target_memory_id", "result_card", "result_target")
            decision = dict(zip(names, params, strict=True))
            for key in ("result_card", "result_target"):
                if decision[key] is not None:
                    decision[key] = json.loads(decision[key])
            assert decision["decision_id"] not in self.connection.decisions
            self.connection.decisions[decision["decision_id"]] = decision
            self.rowcount = 1
            return
        for table, jobs, id_field in (
            ("embedding_outbox", self.connection.embedding_jobs, "outbox_id"),
            ("graph_projection_outbox", self.connection.graph_jobs, "projection_id"),
        ):
            if query.startswith("SELECT " + id_field):
                if tuple(params) in jobs:
                    self.response = [{id_field: jobs[tuple(params)]}]
                return
            if query.startswith("INSERT INTO " + table):
                jobs[tuple(params[:3])] = len(jobs) + 1
                self.response = [{id_field: jobs[tuple(params[:3])]}]
                self.rowcount = 1
                return
        if query.startswith(("SAVEPOINT ", "RELEASE SAVEPOINT ")):
            return
        # Steward authority lock helper: bounded local timeouts and the table
        # fence run before narrow locks; this double journals no new state.
        if query.startswith("SELECT set_config('lock_timeout'"):
            return
        if query == "LOCK TABLE memory_cards IN SHARE ROW EXCLUSIVE MODE":
            return
        if query.startswith("SELECT steward_envelope"):
            row = self.connection.rows.get(params[0])
            if row is not None:
                self.response = [deepcopy(row)]
            return
        raise AssertionError("unexpected offline DB operation: " + query)

    def fetchone(self):
        return self.response.pop(0) if self.response else None

    def fetchall(self):
        rows, self.response = self.response, []
        return rows


def _steward(**kwargs):
    connection = _RecordingConnection()
    store = PgVectorStore(connection=connection)
    return BrainStewardService(None, pgvector_store=store, **kwargs), store, connection


def test_prebuilt_candidate_provenance_survives_steward_pg_storage():
    steward, store, connection = _steward()
    candidate = _candidate()
    preimage = deepcopy(candidate)

    response = steward.candidate_create(
        source_span=candidate, refresh_watermark="later-steward-cycle"
    )
    saved = store.get_steward_card(response["memory_id"])
    row = connection.rows[response["memory_id"]]

    assert row["source_ref"] == candidate["source_refs"]
    for field in (
        "source_refs", "span_refs", "evidence_refs", "evidence_hashes", "derived_from",
        "observed_at", "content_hash", "project", "brain_id", "scope", "candidate_id",
        "idempotency_key", "refresh_watermark", "judgment_basis_bundle",
    ):
        assert saved[field] == preimage[field], field
    expected_id = "mem_steward_" + hashlib.sha256(
        (candidate["idempotency_key"] + "|candidate|").encode()
    ).hexdigest()[:16]
    assert response["memory_id"] == expected_id
    assert row["lifecycle_state"] == saved["lifecycle_state"] == "candidate"
    assert row["authorization_status"] == saved["authorization_status"] == "disabled"
    assert candidate == preimage
    assert not any("outbox" in query for query, _params in connection.executed)
    assert_public_safe(response)
    assert not set(("source_refs", "span_refs", "typed_payload", "render_text")) & set(response["proposal"])


@pytest.mark.parametrize("prebuilt", [False, True])
def test_review_gate_preserves_needs_review_in_envelope_sql_and_response(prebuilt):
    steward, store, connection = _steward()
    source = _candidate() if prebuilt else _span()

    response = steward.candidate_create(
        source_span=source, mark_needs_review=True, review_reason="intake requires review"
    )
    saved = store.get_steward_card(response["memory_id"])
    row = connection.rows[response["memory_id"]]

    assert row["lifecycle_state"] == saved["lifecycle_state"] == "needs_review"
    assert response["lifecycle_state"] == response["proposal"]["lifecycle_state"] == "needs_review"
    assert saved["approval_state"] == saved["judgment_state"] == saved["status"] == "needs_review"
    assert row["authorization_status"] == saved["authorization_status"] == "disabled"
    assert response["accepted"] is False and response["authoritative_memory_changed"] is False
    assert not any("outbox" in query for query, _params in connection.executed)


def test_approval_persists_matching_envelope_sql_and_audit_authorization():
    steward, store, connection = _steward(allow_restricted=True)
    candidate = _candidate()
    memory_id = steward.candidate_create(source_span=candidate, mark_needs_review=True)["memory_id"]
    before = store.get_steward_card(memory_id)

    result = steward.candidate_approve(
        candidate_memory_id=memory_id, approved_by="fixture-operator", decision_id="fixture-approve"
    )
    saved = store.get_steward_card(memory_id)
    row = connection.rows[memory_id]

    assert row["authorization_status"] == saved["authorization_status"] == "active"
    assert row["lifecycle_state"] == saved["lifecycle_state"] == "human_accepted"
    assert saved["approval_state"] == "approved" and saved["currentness"] == "current"
    assert saved["valid_from"] == connection.committed_at.isoformat()
    assert saved["observed_at"] == before["observed_at"] == OBSERVED_AT
    for key in ("candidate_id", "idempotency_key", "content_hash", "source_refs", "span_refs"):
        assert saved[key] == before[key]
    audit = store.get_steward_decision("fixture-approve")
    assert audit["result_card"] == saved
    assert len(connection.embedding_jobs) == len(connection.graph_jobs) == 1
    writes_before = [query for query, _params in connection.executed if query.startswith(("UPDATE", "INSERT"))]
    assert steward.candidate_approve(
        candidate_memory_id=memory_id, approved_by="fixture-operator", decision_id="fixture-approve"
    ) == result
    assert writes_before == [query for query, _params in connection.executed if query.startswith(("UPDATE", "INSERT"))]
    assert_public_safe(result)


@pytest.mark.parametrize("field", ["span_refs", "judgment_basis_bundle"])
def test_prebuilt_envelope_rejects_non_locator_provenance_before_any_sql(field):
    steward, _store, connection = _steward()
    candidate = _candidate()
    if field == "span_refs":
        candidate[field] = [{"span_id": "fixture-span", "content": "synthetic private source"}]
    else:
        candidate[field]["span_refs"] = [{"span_id": "fixture-span", "content": "synthetic private source"}]

    with pytest.raises(ValueError):
        steward.candidate_create(source_span=candidate)
    assert connection.executed == []


@pytest.mark.parametrize("field", ["candidate_id", "idempotency_key", "span_refs"])
def test_partial_candidate_envelope_cannot_fall_back_to_another_identity(field):
    steward, _store, connection = _steward()
    candidate = _candidate()
    del candidate[field]

    with pytest.raises(ValueError):
        steward.candidate_create(source_span=candidate)
    assert connection.executed == []


@pytest.mark.parametrize("state", [
    {"authorization_status": "active"},
    {"approval_state": "approved"},
    {"approval_state": "rejected"},
    {"lifecycle_state": "human_accepted", "approval_state": "approved"},
])
def test_prebuilt_envelope_cannot_reenter_proposals_with_decided_or_active_state(state):
    steward, _store, connection = _steward()
    candidate = _candidate()
    candidate.update(state)

    with pytest.raises(ValueError, match="pending and disabled"):
        steward.candidate_create(source_span=candidate, mark_needs_review=True)
    assert connection.executed == []


@pytest.mark.parametrize("nested", [False, True])
def test_prebuilt_envelope_does_not_bypass_raw_source_key_guard(nested):
    steward, _store, connection = _steward()
    candidate = _candidate()
    payload = {"raw_text": "fixture-only source content"}
    if nested:
        candidate["metadata"] = payload
    else:
        candidate.update(payload)

    with pytest.raises(ValueError, match="raw transcript content"):
        steward.candidate_create(source_span=candidate)
    assert connection.executed == []


def test_multiple_prebuilt_locators_are_preserved_without_mutating_the_input():
    steward, store, connection = _steward()
    candidate = _candidate()
    candidate["source_refs"].append({"source_id": "fixture-additional-source"})
    candidate["span_refs"].append({"span_id": "fixture-additional-span"})
    candidate["judgment_basis_bundle"]["source_refs"] = deepcopy(candidate["source_refs"])
    candidate["judgment_basis_bundle"]["span_refs"] = deepcopy(candidate["span_refs"])
    preimage = deepcopy(candidate)

    first = steward.candidate_create(source_span=candidate)
    second = steward.candidate_create(source_span=candidate)
    saved = store.get_steward_card(first["memory_id"])

    assert first == second and len(connection.rows) == 1
    assert candidate == preimage
    assert saved["source_refs"] == connection.rows[first["memory_id"]]["source_ref"] == preimage["source_refs"]
    assert saved["span_refs"] == preimage["span_refs"]
    assert saved["candidate_id"] == preimage["candidate_id"]
    assert saved["idempotency_key"] == preimage["idempotency_key"]


def test_missing_observation_time_is_not_replaced_with_creation_time():
    steward, store, _connection = _steward()
    candidate = _candidate()
    del candidate["observed_at"]

    response = steward.candidate_create(source_span=candidate)
    saved = store.get_steward_card(response["memory_id"])

    assert "observed_at" not in saved
    assert saved["created_at"] == candidate["created_at"]


def test_rejection_disables_both_representations_and_remains_non_authoritative():
    steward, store, connection = _steward(allow_restricted=True)
    memory_id = steward.candidate_create(source_span=_candidate(), mark_needs_review=True)["memory_id"]
    # Replay a mismatched historical envelope: the SQL column is still disabled.
    connection.rows[memory_id]["steward_envelope"]["authorization_status"] = "active"

    result = steward.candidate_reject(
        candidate_memory_id=memory_id, rejected_by="fixture-operator",
        decision_id="fixture-reject", reason="Unsupported decision"
    )
    saved = store.get_steward_card(memory_id)

    assert connection.rows[memory_id]["authorization_status"] == saved["authorization_status"] == "disabled"
    assert saved["lifecycle_state"] == "human_rejected" and saved["approval_state"] == "rejected"
    assert store.get_steward_decision("fixture-reject")["result_card"] == saved
    assert not connection.embedding_jobs and not connection.graph_jobs
    assert steward.candidate_reject(
        candidate_memory_id=memory_id, rejected_by="fixture-operator",
        decision_id="fixture-reject", reason="Unsupported decision"
    ) == result
    with pytest.raises(ValueError, match="pending"):
        steward.candidate_approve(candidate_memory_id=memory_id,
                                  approved_by="fixture-operator", decision_id="reject-cannot-promote")


def test_demotions_keep_authorization_consistent_and_retries_return_immutable_snapshots():
    steward, store, connection = _steward(allow_restricted=True)
    old_id = steward.candidate_create(source_span=_candidate())["memory_id"]
    old_approval = steward.candidate_approve(
        candidate_memory_id=old_id, approved_by="fixture-operator", decision_id="fixture-old-approve"
    )
    old_snapshot = store.get_steward_decision("fixture-old-approve")
    old_before = store.get_steward_card(old_id)

    replacement = _candidate("replacement")
    new_id = steward.supersede_propose(old_memory_id=old_id, source_span=replacement)["memory_id"]
    new_pending = store.get_steward_card(new_id)
    assert store.get_steward_card(old_id) == old_before
    assert new_pending["authorization_status"] == "disabled"
    for field in ("source_refs", "span_refs", "observed_at", "candidate_id", "idempotency_key"):
        assert new_pending[field] == replacement[field]
    assert len(connection.embedding_jobs) == len(connection.graph_jobs) == 1

    supersession = steward.supersede_commit(
        proposal_memory_id=new_id, approved_by="fixture-operator", decision_id="fixture-replace"
    )
    replacement_snapshot = store.get_steward_decision("fixture-replace")
    new = store.get_steward_card(new_id)
    old = store.get_steward_card(old_id)
    for memory_id, saved in ((old_id, old), (new_id, new)):
        assert connection.rows[memory_id]["authorization_status"] == saved["authorization_status"] == "active"
    assert old["currentness"] == "superseded" and old["valid_to"] == connection.committed_at.isoformat()
    assert new["currentness"] == "current" and new["lifecycle_state"] == "human_accepted"
    assert replacement_snapshot["result_card"] == new and replacement_snapshot["result_target"] == old
    # Demotions now propagate to the graph projection (Neo4j must stop serving
    # the demoted card as current), so graph jobs can outnumber embedding jobs.
    assert len(connection.embedding_jobs) == 2
    assert len(connection.graph_jobs) == 3

    stale_id = steward.stale_mark(memory_id=new_id, reason="Outdated fixture decision")["memory_id"]
    assert store.get_steward_card(new_id) == new
    stale_result = steward.stale_commit(
        proposal_memory_id=stale_id, approved_by="fixture-operator", decision_id="fixture-stale"
    )
    for memory_id in (stale_id, new_id):
        saved = store.get_steward_card(memory_id)
        # Existing SQL semantics retain approval, but stale is never current authority.
        assert connection.rows[memory_id]["authorization_status"] == saved["authorization_status"] == "active"
        assert saved["currentness"] == "stale"
    stale_snapshot = store.get_steward_decision("fixture-stale")
    assert stale_snapshot["result_card"] == store.get_steward_card(stale_id)
    assert stale_snapshot["result_target"] == store.get_steward_card(new_id)
    assert store.get_steward_card(new_id)["valid_to"] == connection.committed_at.isoformat()
    assert len(connection.embedding_jobs) == 2
    assert len(connection.graph_jobs) == 4

    writes_before = [query for query, _params in connection.executed if query.startswith(("UPDATE", "INSERT"))]
    assert steward.candidate_approve(candidate_memory_id=old_id, approved_by="fixture-operator",
                                      decision_id="fixture-old-approve") == old_approval
    assert steward.supersede_commit(proposal_memory_id=new_id, approved_by="fixture-operator",
                                     decision_id="fixture-replace") == supersession
    assert steward.stale_commit(proposal_memory_id=stale_id, approved_by="fixture-operator",
                                decision_id="fixture-stale") == stale_result
    assert store.get_steward_decision("fixture-old-approve") == old_snapshot
    assert store.get_steward_decision("fixture-replace") == replacement_snapshot
    assert writes_before == [query for query, _params in connection.executed if query.startswith(("UPDATE", "INSERT"))]
    for payload in (old_approval, supersession, stale_result):
        assert_public_safe(payload)


@pytest.mark.parametrize("action", ["approve", "reject", "auto_accept"])
def test_default_permissions_still_deny_decisions_without_any_db_access(action):
    steward, _store, connection = _steward()
    with pytest.raises(PermissionError):
        if action == "approve":
            steward.candidate_approve(candidate_memory_id="fixture", approved_by="operator", decision_id="denied")
        elif action == "reject":
            steward.candidate_reject(candidate_memory_id="fixture", rejected_by="operator", decision_id="denied", reason="no")
        else:
            steward.candidate_auto_accept(candidate_memory_id="fixture", evaluation={}, operator_approval_ref="denied")
    assert connection.executed == []


def test_read_only_transport_still_blocks_valid_envelope_proposals():
    from types import SimpleNamespace

    _steward_service, store, connection = _steward()
    steward = BrainStewardService(SimpleNamespace(read_only=True), pgvector_store=store, allow_restricted=True)
    with pytest.raises(ValueError, match="writable transport"):
        steward.candidate_create(source_span=_candidate())
    assert connection.executed == []


def test_intake_to_steward_pg_binds_keeps_original_source_provenance():
    from agent_knowledge.session_memory import candidate_intake_cli as intake

    steward, store, connection = _steward()
    span = _span()
    chunk = {**span, "content": "Synthetic decision: preserve the authority boundary.",
             "knowledge_id": "fixture-source-original", "observed_at": OBSERVED_AT}
    calls = []

    def completion(messages):
        calls.append(1)
        source_keys = re.findall(r"SOURCE_KEY: (sha256:[0-9a-f]{64})", messages[-1]["content"])
        assert len(source_keys) == 1
        return json.dumps([{
            "source_key": source_keys[0],
            "candidate": {"card_type": span["card_type"], "title": span["title"],
                          "statement": span["redacted_summary"], "typed_payload": span["typed_payload"]},
        }])

    report, _state = intake.run_candidate_intake(
        settings=intake.CandidateIntakeSettings(project=PROJECT, enable_proposals=True),
        chunks=[chunk], completion_fn=completion, steward=steward,
    )

    assert report.status == "completed" and report.proposed_count == 1 and len(calls) == 1
    assert len(connection.rows) == 1
    memory_id = next(iter(connection.rows))
    saved = store.get_steward_card(memory_id)
    assert saved["source_refs"][0]["source_id"] == span["source_ref"]["source_id"]
    assert saved["span_refs"][0]["span_id"] == span["span_ref"]["span_id"]
    for field in ("content_hash", "project", "brain_id", "scope"):
        assert saved[field] == span[field]
    assert saved["observed_at"] == OBSERVED_AT
    expected = build_memory_card_candidate_from_source_span(span, refresh_watermark="fixture-identity")
    assert saved["candidate_id"] == expected["candidate_id"]
    assert saved["idempotency_key"] == expected["idempotency_key"]
    assert saved["lifecycle_state"] == saved["approval_state"] == saved["judgment_state"] == "needs_review"
    assert saved["authorization_status"] == connection.rows[memory_id]["authorization_status"] == "disabled"
    assert not connection.embedding_jobs and not connection.graph_jobs


def test_auto_acceptance_keeps_its_separate_gate_and_matching_authorization():
    from agent_knowledge.session_memory.memory_evaluation import build_policy_version, evaluate_candidate_for_auto_policy
    from agent_knowledge.session_memory.memory_promotion import build_feedback_record, suggest_accept_from_evidence

    steward, store, connection = _steward(allow_auto_accept=True)
    candidate = _candidate("auto-accept")
    candidate["confidence"] = 0.95
    suggested = suggest_accept_from_evidence(
        candidate,
        evidence={"evidence_kind": "commit", "decision_id": "fixture-evidence",
                  "content_hash": "sha256:" + hashlib.sha256(b"fixture-evidence").hexdigest(),
                  "source_ref": {"evidence_id": "fixture-approved-commit"}},
        decision_id="fixture-evidence",
    )["suggested_card"]
    memory_id = steward.candidate_create(source_span=suggested)["memory_id"]
    saved = store.get_steward_card(memory_id)
    records = [build_feedback_record(
        candidate=saved, decision_id=f"fixture-feedback-{i}", proposed_status="suggested_accept",
        final_status="accepted", user_action="approve", model_reason="Fixture feedback",
        confidence=0.95, conflict_state="none", timestamp=OBSERVED_AT,
    ) for i in range(6)]
    evaluation = evaluate_candidate_for_auto_policy(
        saved, feedback_records=records,
        policy=build_policy_version(min_feedback_count=5, min_approval_rate=0.8),
    )
    assert evaluation["decision"] == "auto_accept_eligible"
    assert steward.allow_review_commit is False
    with pytest.raises(PermissionError):
        steward.candidate_approve(candidate_memory_id=memory_id, approved_by="operator", decision_id="review-denied")

    result = steward.candidate_auto_accept(
        candidate_memory_id=memory_id, evaluation=evaluation, operator_approval_ref="fixture-auto-gate"
    )
    accepted = store.get_steward_card(memory_id)
    assert result["canonical_write_performed"] is True
    assert accepted["lifecycle_state"] == accepted["approval_state"] == "auto_accepted"
    assert connection.rows[memory_id]["authorization_status"] == accepted["authorization_status"] == "active"
    assert store.get_steward_decision("fixture-auto-gate")["result_card"] == accepted
    assert len(connection.embedding_jobs) == len(connection.graph_jobs) == 1
    assert_public_safe(result)
