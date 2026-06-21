"""C-full: two-body extraction split.

The graph entity pass must extract from REAL redacted prose (conversation chunks
/ typed-payload meaning), not the JSON metadata blob that only ever yields generic
entities. The stored EpisodicNode.content stays canonical JSON so recall
(_episode_node_to_ontology) is unchanged.

All unit/stub: no live Neo4j, no live LLM, no live CouchDB. A Fake graphiti
captures the add_episode(episode_body=...) so the extraction body can be asserted,
and an in-memory CouchDB source store supplies real chunk prose.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core.graphiti_adapter import (
    GraphitiNeo4jGraphMemoryAdapter,
    _episode_node_to_ontology,
)
from agent_knowledge.llm_brain_core.models import OntologyEpisode
from agent_knowledge.llm_brain_core.ontology import (
    episode_from_memory_card,
    episode_from_session_artifact,
)
from agent_knowledge.llm_brain_core.runtime import (
    extraction_text_from_couchdb_chunks,
    session_episode_from_couchdb_source,
)
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession


PROJECT = "neurons"
PROVIDER = "codex"


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class _CapturingDriver:
    provider = None
    graph_operations_interface = None

    def __init__(self) -> None:
        self.saved_uuids: list[str] = []

    async def execute_query(self, query, **params):
        if "routing_" not in params:
            uuid = params.get("uuid") or params.get("episode_uuid")
            if uuid:
                self.saved_uuids.append(str(uuid))
        return ([], None, None)

    def has_node(self, uuid: str) -> bool:
        return str(uuid) in self.saved_uuids


class _CapturingGraphiti:
    """Captures add_episode(episode_body=...) so the extraction body is assertable."""

    def __init__(self) -> None:
        self.added: list[dict] = []
        self.driver = _CapturingDriver()

    @property
    def saved_uuids(self) -> list[str]:
        return self.driver.saved_uuids

    async def add_episode(self, **kwargs):
        self.added.append(dict(kwargs))
        return SimpleNamespace(nodes=[], edges=[])


def _never_extracted(driver, episode_id):
    async def _probe():
        _ = (driver, episode_id)
        return False

    return _probe()


def _adapter(graphiti):
    return GraphitiNeo4jGraphMemoryAdapter(
        graphiti,
        default_group_id=f"/project/{PROJECT}",
        extract_entities=True,
        entity_extracted=_never_extracted,
    )


def _sid() -> str:
    return dm.build_session_id_hash(PROVIDER, "raw-session-c-full")


def _seed_session_with_chunk(store: InMemoryCouchDBSourceStore, *, text: str) -> str:
    session = TranscriptSession(
        session_id_hash=_sid(),
        provider=PROVIDER,
        project=PROJECT,
        started_at="2026-06-20T00:00:00Z",
    )
    store.put(dm.build_transcript_session_document(session=session))
    chunk = TranscriptChunk.from_text(
        chunk_id="chunk_c_full_001",
        session_id_hash=_sid(),
        provider=PROVIDER,
        project=PROJECT,
        turn_start_index=1,
        turn_end_index=1,
        text=text,
    )
    chunk_doc = dm.build_conversation_chunk_document(chunk=chunk)
    store.put(chunk_doc)
    coverage = dm.build_coverage_manifest_document(
        session_id_hash=_sid(),
        provider=PROVIDER,
        project=PROJECT,
        conversation_chunk_count=1,
        tool_evidence_bundle_count=0,
        conversation_content_hashes=[chunk_doc["content_hash"]],
        tool_evidence_coverage_hashes=[],
    )
    store.put(coverage)
    return _sid()


def _card(memory_id: str, typed_payload: dict, *, summary: str = "Card summary") -> dict:
    return {
        "memory_id": memory_id,
        "brain_id": f"/project/{PROJECT}",
        "card_type": "decision",
        "scope": "project",
        "project": PROJECT,
        "provider": PROVIDER,
        "title": summary,
        "summary": summary,
        "lifecycle_state": "accepted",
        "approval_state": "approved",
        "currentness": "current",
        "confidence": 0.9,
        "source_refs": [],
        "derived_from": [],
        "typed_payload": typed_payload,
    }


# --------------------------------------------------------------------------- #
# TDD 1: card extraction_body is meaning prose, not JSON metadata
# --------------------------------------------------------------------------- #


def test_memory_card_episode_extraction_body_is_meaning_prose_not_json():
    card = _card(
        "mem_decision_c",
        {
            "decision": "Adopt CouchDB as the transcript source of truth.",
            "rationale": "Idempotent deterministic upserts simplify replay.",
            "consequence": "RAGFlow keeps only the derived recall surface.",
        },
        summary="Choose CouchDB transcript SoT",
    )
    episode = episode_from_memory_card(card, project=PROJECT)
    graphiti = _CapturingGraphiti()

    result = _adapter(graphiti).upsert_episode(episode)

    assert result == "inserted"
    extraction_body = graphiti.added[0]["episode_body"]
    # Real meaning text from typed_payload is present.
    assert "Adopt CouchDB as the transcript source of truth." in extraction_body
    assert "Idempotent deterministic upserts simplify replay." in extraction_body
    assert "Choose CouchDB transcript SoT" in extraction_body
    # The extraction body is prose, NOT the JSON metadata blob.
    assert not extraction_body.lstrip().startswith("{")
    assert '"episode_id"' not in extraction_body


# --------------------------------------------------------------------------- #
# TDD 2: stored content stays canonical JSON (recall unchanged)
# --------------------------------------------------------------------------- #


def test_stored_episode_content_is_json_and_recall_rehydrates():
    card = _card("mem_json_store", {"decision": "Keep stored content as JSON."})
    episode = episode_from_memory_card(card, project=PROJECT)
    graphiti = _CapturingGraphiti()

    _adapter(graphiti).upsert_episode(episode)

    # The ensure-saved EpisodicNode content is the canonical JSON (the adapter
    # builds it from episode.to_dict()); rehydrate it the way recall does.
    body = json.dumps(episode.to_dict(), ensure_ascii=True, sort_keys=True)
    rehydrated = _episode_node_to_ontology(SimpleNamespace(content=body))
    assert rehydrated is not None
    assert rehydrated.episode_id == episode.episode_id
    assert rehydrated.entity_type == "Decision"
    # to_dict() never carries extraction_text (transient, recall-irrelevant).
    assert "extraction_text" not in episode.to_dict()


# --------------------------------------------------------------------------- #
# TDD 3: Session extraction_body is real, redacted, bounded chunk prose
# --------------------------------------------------------------------------- #


def test_session_episode_extraction_body_is_couchdb_chunk_prose():
    store = InMemoryCouchDBSourceStore()
    chunk_text = "User decided to migrate the recall substrate from CouchDB to session-memory."
    session_id_hash = _seed_session_with_chunk(store, text=chunk_text)

    episode = session_episode_from_couchdb_source(
        session_id_hash=session_id_hash, source_store=store
    )
    graphiti = _CapturingGraphiti()
    _adapter(graphiti).upsert_episode(episode)

    extraction_body = graphiti.added[0]["episode_body"]
    assert "migrate the recall substrate" in extraction_body
    # NOT the statistics summary that the artifact carries.
    assert "conversation_chunks=" not in extraction_body
    assert '"episode_id"' not in extraction_body


def test_session_extraction_body_is_redacted_and_bounded():
    store = InMemoryCouchDBSourceStore()
    # The chunk body is already public-safe redacted by the CouchDB builder;
    # extraction_text_from_couchdb_chunks must surface that redacted body, and
    # OntologyEpisode.__post_init__ bounds + re-redacts it.
    session_id_hash = _seed_session_with_chunk(
        store, text="A normal redaction-clean conversation turn about the graph."
    )
    prose = extraction_text_from_couchdb_chunks(
        session_id_hash=session_id_hash, source_store=store
    )

    assert "graph" in prose
    # No private path leaks survive in the sourced prose.
    assert "/Users/" not in prose
    assert len(prose) <= 8000


def test_session_extraction_text_passes_through_public_safe_filter():
    # An extraction_text carrying a private path is redacted by the model gate, so
    # what reaches the adapter is safe (defense in depth even though CouchDB
    # bodies are pre-redacted).
    artifact = _fake_artifact()
    episode = episode_from_session_artifact(
        artifact, extraction_text="see notes at /Users/secret/private.md for details"
    )
    assert "/Users/secret/private.md" not in episode.extraction_text


# --------------------------------------------------------------------------- #
# TDD 4: episode_id is stable -- extraction_text never changes the hash
# --------------------------------------------------------------------------- #


def test_extraction_text_does_not_change_episode_id_or_content_hash():
    card = _card("mem_stable", {"decision": "Stable id under different prose."})
    base = episode_from_memory_card(card, project=PROJECT)

    # Same canonical content, different extraction_text -> identical id/hash.
    variant = OntologyEpisode.from_payload(
        event_id=base.event_id,
        entity_type=base.entity_type,
        natural_id=base.natural_id,
        payload=dict(base.payload),
        lifecycle_state=base.lifecycle_state,
        currentness=base.currentness,
        extraction_text="completely different extraction prose here",
    )
    no_text = OntologyEpisode.from_payload(
        event_id=base.event_id,
        entity_type=base.entity_type,
        natural_id=base.natural_id,
        payload=dict(base.payload),
        lifecycle_state=base.lifecycle_state,
        currentness=base.currentness,
        extraction_text="",
    )

    assert variant.content_hash == no_text.content_hash == base.content_hash
    assert variant.episode_id == no_text.episode_id == base.episode_id


# --------------------------------------------------------------------------- #
# TDD 5: empty extraction_text -> safe fallback to JSON body
# --------------------------------------------------------------------------- #


def test_empty_extraction_text_falls_back_to_json_body():
    artifact = _fake_artifact()
    episode = episode_from_session_artifact(artifact)  # no extraction_text
    assert episode.extraction_text == ""
    graphiti = _CapturingGraphiti()

    result = _adapter(graphiti).upsert_episode(episode)

    assert result == "inserted"
    extraction_body = graphiti.added[0]["episode_body"]
    # Fallback: the JSON body is used so the entity pass still has input.
    parsed = json.loads(extraction_body)
    assert parsed["episode_id"] == episode.episode_id


def test_empty_extraction_text_logs_generic_only_regression(caplog):
    artifact = _fake_artifact()
    episode = episode_from_session_artifact(artifact)
    graphiti = _CapturingGraphiti()

    with caplog.at_level("WARNING"):
        _adapter(graphiti).upsert_episode(episode)

    assert any("generic-only extraction regression" in rec.message for rec in caplog.records)


# --------------------------------------------------------------------------- #
# TDD 6: behavior-preserving -- episodic-only (extract_entities=False) unchanged
# --------------------------------------------------------------------------- #


def test_episodic_only_path_stores_json_and_never_calls_add_episode():
    card = _card("mem_episodic", {"decision": "Episodic path unchanged."})
    episode = episode_from_memory_card(card, project=PROJECT)
    graphiti = _CapturingGraphiti()
    adapter = GraphitiNeo4jGraphMemoryAdapter(
        graphiti, default_group_id=f"/project/{PROJECT}"
    )  # extract_entities defaults to False

    result = adapter.upsert_episode(episode)

    assert result == "inserted"
    # No entity pass: add_episode never called, only the JSON node was saved.
    assert graphiti.added == []
    assert graphiti.saved_uuids == [episode.episode_id]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _fake_artifact():
    from agent_knowledge.llm_brain_core.models import SessionMemoryArtifact

    return SessionMemoryArtifact.from_summary(
        session_id_hash=_sid(),
        project=PROJECT,
        provider=PROVIDER,
        summary="Session artifact for codex/neurons. conversation_chunks=3.",
        source_event_ids=["evt:c-full"],
        created_at="2026-06-20T00:00:00Z",
    )
