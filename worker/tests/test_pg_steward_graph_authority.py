"""Public graph-first PostgreSQL reads require steward authority and temporal boundaries."""
from datetime import datetime, timezone
import hashlib
from types import SimpleNamespace

from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
from agent_knowledge.llm_brain_core.models import GraphMemoryResult, OntologyEpisode
from agent_knowledge.postgres_store.pgvector_store import MemoryCard, make_dummy_vector
from agent_knowledge.session_memory.brain_steward import BrainStewardService


class NoCardLedger:
    read_only = False

    def __getattr__(self, name):
        if name.startswith(("get_llm_brain", "list_llm_brain", "upsert_llm_brain", "_transaction")):
            raise AssertionError("SQLite card lane was accessed")
        raise AttributeError(name)


def _span(suffix):
    return {"card_type": "preference", "project": "public-graph-authority", "provider": "hermes",
            "scope": "project", "title": "Steward preference", "redacted_summary": "Use steward authority",
            "typed_payload": {"preference": "Use steward authority", "explicitness": "explicit",
                              "repeated_count": 1, "confirmation_status": "confirmed", "applies_to": "card_storage"},
            "source_ref": {"source_id": "source-" + suffix}, "span_ref": {"span_id": "span-" + suffix},
            "content_hash": "sha256:" + hashlib.sha256(suffix.encode()).hexdigest(),
            "confidence": 0.9, "confidence_basis": "human-approved preference"}


def test_graph_episode_and_pg_fallback_exclude_legacy_accepted_row(isolated_pg_store):
    store = isolated_pg_store
    vector = make_dummy_vector(42)
    legacy = MemoryCard(memory_id="legacy-public", project="public-graph-authority", card_type="preference",
                        title="Legacy", summary="Legacy accepted", lifecycle_state="human_accepted",
                        authorization_status="active", currentness="current", embedding=vector,
                        embedding_state="ready", content_hash="sha256:" + hashlib.sha256(b"legacy-public").hexdigest())
    store.insert_card(legacy)
    # Generic consumers keep their previous broad contract.
    assert [card["memory_id"] for card in store.list_authorized_cards(project=legacy.project)] == [legacy.memory_id]
    assert legacy.memory_id in [card["memory_id"] for card in store.hybrid_search(project=legacy.project, query_vector=vector)]
    episode = OntologyEpisode.from_payload(event_id="event:legacy-public", entity_type="Preference",
        natural_id=legacy.memory_id, payload={"authority_memory_id": legacy.memory_id,
        "content_hash": legacy.content_hash, "brain_id": "/project/" + legacy.project})
    graph = SimpleNamespace(search_context=lambda **_: GraphMemoryResult(status="available", episodes=(episode,)))
    assert GraphFirstResolver(store, graph, lambda _: vector).resolve(project=legacy.project, query="preference")["items"] == []
    assert GraphFirstResolver(store, None, lambda _: vector).resolve(project=legacy.project, query="preference")["items"] == []
    assert GraphFirstResolver(store).resolve(project=legacy.project, mode="list")["items"] == []


def test_steward_demotion_closes_pg_and_historical_graph_and_fallback(isolated_pg_store):
    store = isolated_pg_store
    steward = BrainStewardService(NoCardLedger(), pgvector_store=store, allow_restricted=True)
    old_id = steward.candidate_create(source_span=_span("old"))["memory_id"]
    steward.candidate_approve(candidate_memory_id=old_id, approved_by="operator", decision_id="approve-old")
    vector = make_dummy_vector(42)
    with store.transaction() as db:
        db.execute("UPDATE memory_cards SET embedding = %s::halfvec, embedding_state = 'ready' WHERE memory_id = %s",
                   ("[" + ",".join(str(v) for v in vector) + "]", old_id))
    before = datetime.now(timezone.utc)
    newer = _span("new")
    newer["title"] = "New steward preference"
    newer["redacted_summary"] = "Use new steward authority"
    newer["typed_payload"] = {**newer["typed_payload"], "preference": "Use new steward authority"}
    new_id = steward.supersede_propose(old_memory_id=old_id, source_span=newer)["memory_id"]
    steward.supersede_commit(proposal_memory_id=new_id, approved_by="operator", decision_id="supersede-old")
    old = store.get_card(old_id)
    assert old.currentness == "superseded"
    assert old.valid_to is not None and old.valid_to >= before
    assert old_id not in [c["memory_id"] for c in store.list_authorized_cards(project=old.project, steward_only=True)]
    historic = (old.valid_from + (old.valid_to - old.valid_from) / 2).isoformat()
    assert old_id in [c["memory_id"] for c in store.list_authorized_cards(project=old.project, as_of=historic, steward_only=True)]
    assert old_id in [c["memory_id"] for c in store.hybrid_search(project=old.project, query_vector=vector, as_of=historic, steward_only=True)]
    episode = OntologyEpisode.from_payload(event_id="event:old", entity_type="Preference", natural_id=old_id,
        payload={"authority_memory_id": old_id, "content_hash": old.content_hash, "brain_id": "/project/" + old.project})
    graph = SimpleNamespace(search_context=lambda **_: GraphMemoryResult(status="available", episodes=(episode,)))
    assert GraphFirstResolver(store, graph, lambda _: vector).resolve(project=old.project, query="preference", as_of=historic)["items"]
    assert GraphFirstResolver(store, None, lambda _: vector).resolve(project=old.project, query="preference", as_of=historic)["items"]
    assert GraphFirstResolver(store, graph, lambda _: vector).resolve(project=old.project, query="preference")["items"] == []
    assert GraphFirstResolver(store, None, lambda _: vector).resolve(project=old.project, query="preference")["items"] == []
    with store.transaction() as db:
        db.execute("UPDATE memory_cards SET embedding = %s::halfvec, embedding_state = 'ready' WHERE memory_id = %s",
                   ("[" + ",".join(str(v) for v in vector) + "]", new_id))
    stale_id = steward.stale_mark(memory_id=new_id, reason="Outdated")["memory_id"]
    stale_before = datetime.now(timezone.utc)
    steward.stale_commit(proposal_memory_id=stale_id, approved_by="operator", decision_id="stale-new")
    stale = store.get_card(new_id)
    assert stale.currentness == "stale" and stale.valid_to is not None and stale.valid_to >= stale_before
    prior = (stale.valid_from + (stale.valid_to - stale.valid_from) / 2).isoformat()
    assert new_id in [c["memory_id"] for c in store.list_authorized_cards(project=stale.project, as_of=prior, steward_only=True)]
    assert new_id not in [c["memory_id"] for c in store.list_authorized_cards(project=stale.project, steward_only=True)]
    assert new_id in [c["memory_id"] for c in store.hybrid_search(project=stale.project, query_vector=vector,
                                                                    as_of=prior, steward_only=True)]
    stale_episode = OntologyEpisode.from_payload(event_id="event:stale-target", entity_type="Preference",
        natural_id=new_id, payload={"authority_memory_id": new_id, "content_hash": stale.content_hash,
                                   "brain_id": "/project/" + stale.project})
    stale_graph = SimpleNamespace(search_context=lambda **_: GraphMemoryResult(status="available", episodes=(stale_episode,)))
    assert GraphFirstResolver(store, stale_graph, lambda _: vector).resolve(project=stale.project, query="preference", as_of=prior)["items"]
    assert GraphFirstResolver(store, None, lambda _: vector).resolve(project=stale.project, query="preference", as_of=prior)["items"]
    assert GraphFirstResolver(store, stale_graph, lambda _: vector).resolve(project=stale.project, query="preference")["items"] == []
    assert GraphFirstResolver(store, None, lambda _: vector).resolve(project=stale.project, query="preference")["items"] == []


def test_stale_without_valid_to_never_historically_authorized(isolated_pg_store):
    store = isolated_pg_store
    card = MemoryCard(memory_id="legacy-stale", project="public-graph-authority", card_type="preference",
                      title="Stale", summary="Stale", lifecycle_state="human_accepted", authorization_status="active",
                      currentness="stale", content_hash="sha256:" + hashlib.sha256(b"legacy-stale").hexdigest())
    store.insert_card(card)
    with store.transaction() as db:
        db.execute("UPDATE memory_cards SET steward_envelope = %s::jsonb WHERE memory_id = %s",
                   ('{"approval_state":"approved"}', card.memory_id))
    assert store.list_authorized_cards(project=card.project, as_of=datetime.now(timezone.utc).isoformat(),
                                       steward_only=True) == []
