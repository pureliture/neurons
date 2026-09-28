"""PostgreSQL-only steward authority integration against an isolated local schema."""
import hashlib
import pytest

from agent_knowledge.session_memory.brain_steward import BrainStewardService
from agent_knowledge.postgres_store.pgvector_store import PgVectorStore


class NoCardLedger:
    read_only = False

    def __getattr__(self, name):
        if name.startswith(("get_llm_brain", "list_llm_brain", "upsert_llm_brain", "_transaction")):
            raise AssertionError("SQLite card lane was accessed")
        raise AttributeError(name)


SPAN = {"card_type": "preference", "project": "steward-test", "provider": "hermes", "scope": "project",
        "title": "PostgreSQL preference", "redacted_summary": "Use PostgreSQL for cards",
        "typed_payload": {"preference": "Use PostgreSQL for cards", "explicitness": "explicit", "repeated_count": 1,
                          "confirmation_status": "confirmed", "applies_to": "card_storage"},
        "source_ref": {"source_id": "src_pg_steward"}, "span_ref": {"span_id": "span_pg_steward"},
        "content_hash": "sha256:" + hashlib.sha256(b"pg-steward").hexdigest(),
        "confidence": 0.9, "confidence_basis": "human-approved preference"}


def test_missing_pg_fails_closed():
    steward = BrainStewardService(NoCardLedger(), allow_restricted=True)
    with pytest.raises(ValueError, match="PostgreSQL"):
        steward.candidate_create(source_span=SPAN)
    with pytest.raises(ValueError, match="PostgreSQL"):
        steward.review_queue_list(project="steward-test")


def test_pg_proposal_approve_retry_and_reject(isolated_pg_store):
    store = isolated_pg_store
    steward = BrainStewardService(NoCardLedger(), pgvector_store=store, allow_restricted=True)
    proposal = steward.candidate_create(source_span=SPAN)
    memory_id = proposal["memory_id"]
    candidate_time = store.get_card(memory_id).valid_from
    assert steward.review_queue_list(project="steward-test")["items"][0]["memory_id"] == memory_id
    assert [job for job in store.list_graph_projection_jobs() if job.source_id == memory_id] == []
    result = steward.candidate_approve(candidate_memory_id=memory_id, approved_by="operator", decision_id="decision-1")
    assert result["accepted_card"]["memory_id"] == memory_id
    assert store.get_card(memory_id).valid_from > candidate_time
    assert store.list_authorized_cards(project="steward-test", as_of=candidate_time) == []
    assert steward.candidate_approve(candidate_memory_id=memory_id, approved_by="operator", decision_id="decision-1") == result
    assert steward.review_queue_list(project="steward-test")["items"] == []
    assert steward.authority_pack_read(project="steward-test")["items"][0]["memory_id"] == memory_id
    with pytest.raises(ValueError):
        steward.candidate_approve(candidate_memory_id=memory_id, approved_by="operator", decision_id="decision-2")
    with pytest.raises(ValueError):
        steward.candidate_reject(candidate_memory_id=memory_id, rejected_by="operator", decision_id="decision-3", reason="no")
    with store._scope() as db:
        with db.cursor() as cur:
            cur.execute("SELECT count(*) FROM steward_card_decisions WHERE memory_id = %s", (memory_id,))
            assert cur.fetchone()["count"] == 1
            cur.execute("SELECT count(*) FROM graph_projection_outbox WHERE source_id = %s", (memory_id,))
            assert cur.fetchone()["count"] >= 1


def test_pg_supersede_and_stale(isolated_pg_store):
    store = isolated_pg_store
    steward = BrainStewardService(NoCardLedger(), pgvector_store=store, allow_restricted=True)
    old_id = steward.candidate_create(source_span=SPAN)["memory_id"]
    steward.candidate_approve(candidate_memory_id=old_id, approved_by="operator", decision_id="old")
    newer = dict(SPAN, title="New PostgreSQL preference", redacted_summary="Use new PostgreSQL",
                 content_hash="sha256:" + hashlib.sha256(b"pg-steward-new").hexdigest(),
                 typed_payload={**SPAN["typed_payload"], "preference": "Use new PostgreSQL for cards"})
    proposal_id = steward.supersede_propose(old_memory_id=old_id, source_span=newer)["memory_id"]
    result = steward.supersede_commit(proposal_memory_id=proposal_id, approved_by="operator", decision_id="replace")
    assert result["new_card"]["memory_id"] == proposal_id
    assert steward.supersede_commit(proposal_memory_id=proposal_id, approved_by="operator",
                                    decision_id="replace") == result
    assert store.get_card(old_id).currentness == "superseded"
    # Retrying the old approval after a later replacement must reproduce its
    # original decision-time result, not the now-superseded card.
    assert steward.candidate_approve(candidate_memory_id=old_id, approved_by="operator", decision_id="old")["accepted_card"]["currentness"] == "current"
    from agent_knowledge.session_memory.brain_read_model import PgStewardBrainReadModel
    assert PgStewardBrainReadModel(store).get_card_meta(old_id) is None
    assert {x["memory_id"] for x in steward.authority_pack_read(project="steward-test")["items"]} == {proposal_id}
    stale_id = steward.stale_mark(memory_id=proposal_id, reason="Outdated")["memory_id"]
    steward.stale_commit(proposal_memory_id=stale_id, approved_by="operator", decision_id="stale")
    assert store.get_card(proposal_id).currentness == "stale"
    assert steward.authority_pack_read(project="steward-test")["items"] == []
    assert store.get_card(stale_id).lifecycle_state == "human_accepted"
    stale_result = steward.stale_commit(proposal_memory_id=stale_id, approved_by="operator", decision_id="stale")
    assert stale_result["committed_proposal"]["memory_id"] == stale_id
    with pytest.raises(ValueError):
        steward.stale_commit(proposal_memory_id=stale_id, approved_by="other", decision_id="stale")
    with pytest.raises(ValueError):
        steward.supersede_commit(proposal_memory_id=proposal_id, approved_by="other", decision_id="replace")


def test_pg_proposal_collision_and_permission_boundary(isolated_pg_store):
    store = isolated_pg_store
    steward = BrainStewardService(NoCardLedger(), pgvector_store=store)
    memory_id = steward.candidate_create(source_span=SPAN)["memory_id"]
    assert steward.candidate_create(source_span=SPAN)["memory_id"] == memory_id
    with pytest.raises(PermissionError):
        steward.candidate_approve(candidate_memory_id=memory_id, approved_by="operator", decision_id="denied")
    assert store.get_steward_card(memory_id)["lifecycle_state"] == "candidate"
    with pytest.raises(ValueError, match="collision"):
        store.put_steward_proposal({**store.get_steward_card(memory_id),
            "content_hash": "sha256:" + hashlib.sha256(b"collision").hexdigest()})


def test_pg_read_does_not_mix_legacy_memory_cards(isolated_pg_store):
    from agent_knowledge.postgres_store.pgvector_store import MemoryCard
    from agent_knowledge.session_memory.brain_read_model import PgStewardBrainReadModel
    store = isolated_pg_store
    store.upsert_card(MemoryCard(memory_id="legacy-card", project="steward-test", card_type="preference",
        title="Legacy", summary="Legacy", lifecycle_state="human_accepted",
        authorization_status="active", currentness="current",
        content_hash="sha256:" + hashlib.sha256(b"legacy").hexdigest()))
    adapter = PgStewardBrainReadModel(store)
    assert adapter.get_card_meta("legacy-card") is None
    assert adapter.list_accepted_cards(project="steward-test", limit=10) == []
    assert adapter.list_project_card_counts() == []


def test_pg_schema_repeatable_and_read_adapter(isolated_pg_store):
    from agent_knowledge.session_memory.brain_read_model import PgStewardBrainReadModel
    store = isolated_pg_store
    store.execute_ddl()
    steward = BrainStewardService(NoCardLedger(), pgvector_store=store, allow_restricted=True)
    memory_id = steward.candidate_create(source_span=SPAN)["memory_id"]
    adapter = PgStewardBrainReadModel(store)
    assert adapter.get_card_meta(memory_id) is None
    assert adapter.list_accepted_cards(project="steward-test", limit=10) == []
    steward.candidate_approve(candidate_memory_id=memory_id, approved_by="operator", decision_id="read")
    approved_meta = adapter.get_card_meta(memory_id)
    assert approved_meta is not None and approved_meta["memory_id"] == memory_id
    assert adapter.list_recent_cards(project="steward-test", limit=10)[0]["memory_id"] == memory_id
    assert adapter.list_project_card_counts() == [("steward-test", 1)]


def test_pg_reject_retry_and_conflict(isolated_pg_store):
    steward = BrainStewardService(NoCardLedger(), pgvector_store=isolated_pg_store, allow_restricted=True)
    memory_id = steward.candidate_create(source_span=SPAN)["memory_id"]
    rejected = steward.candidate_reject(candidate_memory_id=memory_id, rejected_by="operator",
                                         decision_id="reject-one", reason="not valid")
    assert rejected["rejected_card"]["lifecycle_state"] == "human_rejected"
    assert steward.candidate_reject(candidate_memory_id=memory_id, rejected_by="operator",
                                    decision_id="reject-one", reason="not valid") == rejected
    with pytest.raises(ValueError):
        steward.candidate_reject(candidate_memory_id=memory_id, rejected_by="other",
                                 decision_id="reject-one", reason="not valid")


def test_pg_supersede_missing_target_rolls_back(isolated_pg_store):
    store = isolated_pg_store
    steward = BrainStewardService(NoCardLedger(), pgvector_store=store, allow_restricted=True)
    old_id = steward.candidate_create(source_span=SPAN)["memory_id"]
    steward.candidate_approve(candidate_memory_id=old_id, approved_by="operator", decision_id="first")
    newer = dict(SPAN, content_hash="sha256:" + hashlib.sha256(b"missing-target").hexdigest(),
                 typed_payload={**SPAN["typed_payload"], "preference": "Changed preference"})
    proposal_id = steward.supersede_propose(old_memory_id=old_id, source_span=newer)["memory_id"]
    # Simulate a missing target within the PG transaction without deleting an accepted row.
    with pytest.raises(ValueError, match="unknown steward card or target"):
        store.steward_decision(decision_id="missing", memory_id=proposal_id,
            content_hash=store.get_card(proposal_id).content_hash, action="supersede", actor="operator",
            target_memory_id="absent-target", transform=lambda card, target: (card, target))
    assert store.get_card(proposal_id).lifecycle_state == "candidate"
    assert store.get_steward_decision("missing") is None


def test_pg_transaction_rolls_back_on_outbox_failure(isolated_pg_store, monkeypatch):
    store = isolated_pg_store
    steward = BrainStewardService(NoCardLedger(), pgvector_store=store, allow_restricted=True)
    memory_id = steward.candidate_create(source_span=SPAN)["memory_id"]
    original = store._enqueue_graph_outbox_on
    def fail(*args, **kwargs):
        raise RuntimeError("injected graph enqueue failure")
    monkeypatch.setattr(store, "_enqueue_graph_outbox_on", fail)
    with pytest.raises(RuntimeError, match="injected"):
        steward.candidate_approve(candidate_memory_id=memory_id, approved_by="operator", decision_id="rollback")
    monkeypatch.setattr(store, "_enqueue_graph_outbox_on", original)
    assert store.get_card(memory_id).lifecycle_state == "candidate"
    with store._scope() as db:
        with db.cursor() as cur:
            cur.execute("SELECT count(*) FROM steward_card_decisions WHERE decision_id = 'rollback'")
            assert cur.fetchone()["count"] == 0
