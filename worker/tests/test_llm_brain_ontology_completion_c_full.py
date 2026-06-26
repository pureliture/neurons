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
    _normalize_structured_keys,
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
        self.saved_contents: list[str] = []

    async def execute_query(self, query, **params):
        if "routing_" not in params:
            uuid = params.get("uuid") or params.get("episode_uuid")
            if uuid:
                self.saved_uuids.append(str(uuid))
            if "content" in params:
                self.saved_contents.append(str(params.get("content") or ""))
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

    @property
    def saved_contents(self) -> list[str]:
        return self.driver.saved_contents

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


def test_entity_path_temporarily_stores_prose_then_restores_json_content():
    store = InMemoryCouchDBSourceStore()
    chunk_text = "Graphiti should extract Neo4j and vertex-wrapper entities from prose."
    session_id_hash = _seed_session_with_chunk(store, text=chunk_text)
    episode = session_episode_from_couchdb_source(
        session_id_hash=session_id_hash, source_store=store
    )
    graphiti = _CapturingGraphiti()

    _adapter(graphiti).upsert_episode(episode)

    assert chunk_text in graphiti.saved_contents[0]
    restored = json.loads(graphiti.saved_contents[-1])
    assert restored["episode_id"] == episode.episode_id
    assert "Graphiti should extract" not in graphiti.saved_contents[-1]


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


def test_session_extraction_text_orders_multipart_chunks_by_turn_part():
    store = InMemoryCouchDBSourceStore()
    session_id_hash = _sid()
    session = TranscriptSession(
        session_id_hash=session_id_hash,
        provider=PROVIDER,
        project=PROJECT,
        started_at="2026-06-20T00:00:00Z",
    )
    store.put(dm.build_transcript_session_document(session=session))
    for part_index, text in (
        (3, "assistant: third part mentions Graphiti relation extraction."),
        (1, "user: first part introduces vertex-wrapper structured output."),
        (2, "assistant: second part explains entity_name normalization."),
    ):
        chunk = TranscriptChunk(
            chunk_id=f"chunk_c_full_part_{part_index}",
            session_id_hash=session_id_hash,
            provider=PROVIDER,
            project=PROJECT,
            turn_start_index=1,
            turn_end_index=1,
            redacted_text=text,
            content_hash=dm.sha256_hash(text),
            part_index=part_index,
            part_count=3,
            char_start=(part_index - 1) * 100,
            char_end=part_index * 100,
        )
        doc = dm.build_conversation_chunk_document(chunk=chunk)
        doc["body"] = "\n".join(
            [
                f"session_id_hash: {session_id_hash}",
                "turn_start_index: 1",
                f"turn_part_index: {part_index}",
                "turn_part_count: 3",
                f"char_start: {(part_index - 1) * 100}",
                f"char_end: {part_index * 100}",
                "",
                text,
            ]
        )
        store.put(doc)

    prose = extraction_text_from_couchdb_chunks(
        session_id_hash=session_id_hash, source_store=store
    )

    assert prose.index("first part") < prose.index("second part") < prose.index("third part")


def test_session_extraction_text_strips_couchdb_chunk_metadata_headers():
    store = InMemoryCouchDBSourceStore()
    session_id_hash = _sid()
    store.put(
        dm.build_transcript_session_document(
            session=TranscriptSession(
                session_id_hash=session_id_hash,
                provider=PROVIDER,
                project=PROJECT,
                started_at="2026-06-20T00:00:00Z",
            )
        )
    )
    chunk = TranscriptChunk.from_text(
        chunk_id="chunk_c_full_header",
        session_id_hash=session_id_hash,
        provider=PROVIDER,
        project=PROJECT,
        turn_start_index=1,
        turn_end_index=1,
        text="user: discuss Graphiti, Neo4j, and vertex-wrapper behavior.",
    )
    doc = dm.build_conversation_chunk_document(chunk=chunk)
    doc["body"] = "\n".join(
        [
            f"session_id_hash: {session_id_hash}",
            "turn_start_index: 1",
            "turn_part_index: 1",
            "turn_part_count: 1",
            "char_start: 0",
            "char_end: 64",
            "",
            chunk.redacted_text,
        ]
    )
    store.put(doc)

    prose = extraction_text_from_couchdb_chunks(
        session_id_hash=session_id_hash, source_store=store
    )

    assert prose.startswith("user: discuss Graphiti")
    assert "session_id_hash:" not in prose
    assert "turn_part_index:" not in prose
    assert "char_start:" not in prose


def test_session_extraction_text_streams_cut_at_max_chars_across_chunks():
    # Two oversized chunks: the join must be bounded at max_chars without first
    # concatenating both full bodies (streaming cut), and the cap holds exactly.
    store = InMemoryCouchDBSourceStore()
    session_id_hash = _sid()
    store.put(
        dm.build_transcript_session_document(
            session=TranscriptSession(
                session_id_hash=session_id_hash,
                provider=PROVIDER,
                project=PROJECT,
                started_at="2026-06-20T00:00:00Z",
            )
        )
    )
    for part_index, marker in ((1, "A"), (2, "B")):
        chunk = TranscriptChunk(
            chunk_id=f"chunk_stream_{part_index}",
            session_id_hash=session_id_hash,
            provider=PROVIDER,
            project=PROJECT,
            turn_start_index=1,
            turn_end_index=1,
            redacted_text=marker * 6000,
            content_hash=dm.sha256_hash(marker * 6000),
            part_index=part_index,
            part_count=2,
            char_start=(part_index - 1) * 6000,
            char_end=part_index * 6000,
        )
        store.put(dm.build_conversation_chunk_document(chunk=chunk))

    prose = extraction_text_from_couchdb_chunks(
        session_id_hash=session_id_hash, source_store=store, max_chars=100
    )

    assert len(prose) == 100
    # The cut happens inside the first chunk; the second chunk never contributes.
    assert set(prose) == {"A"}


def test_strip_metadata_header_keeps_prose_first_line_without_blank_separator():
    # A first sentence that merely looks like ``key: value`` (and even reuses a
    # metadata key) must survive when there is no blank-line header boundary.
    from agent_knowledge.llm_brain_core.runtime import _strip_chunk_metadata_header

    text = "char_start: where the debugging story begins\nthen we discussed Neo4j."
    assert _strip_chunk_metadata_header(text) == text

    # A genuine header block (closed by a blank line) is still stripped.
    headered = "\n".join(["char_start: 0", "char_end: 64", "", "user: real prose here."])
    assert _strip_chunk_metadata_header(headered) == "user: real prose here."


def test_session_extraction_text_is_trusted_from_source_not_re_redacted():
    # Contract: extraction_text is the entity-pass LLM INPUT and is trusted as-is.
    # Its production sources (CouchDB chunk bodies via redact_public_ingress_text;
    # card typed-payload via public_safe) are ALREADY redacted at ingestion/mapping,
    # so the model does NOT re-redact it here -- re-redaction stripped legitimate
    # technical prose and regressed extraction to generic. Bound length only; the
    # strict public-safe gate stays on extraction OUTPUT, not this input.
    artifact = _fake_artifact()
    long_text = "x" * 9000
    episode = episode_from_session_artifact(artifact, extraction_text=long_text)
    # passed through (not re-redacted) but length-bounded.
    assert len(episode.extraction_text) == 8000


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


# ---------------------------------------------------------------------------
# gemini structured-output key normalization (entity_name -> name)
#
# Root cause of the live 0-entity stall: under graphiti's non-strict json_schema
# response_format, gemini-3.5-flash-thinking emits each extracted-entity item as
# {"entity_name": X, "entity_type_id": 0} while graphiti's ExtractedEntities
# requires the field `name`, so model_validate() drops every entity. The adapter
# normalizes the parsed dict before validation. These lock that contract.
# ---------------------------------------------------------------------------


def test_normalize_renames_entity_name_to_name_in_list():
    raw = {
        "extracted_entities": [
            {"entity_name": "graphify", "entity_type_id": 0},
            {"entity_name": "Neo4j", "entity_type_id": 0},
        ]
    }
    out = _normalize_structured_keys(raw)
    assert [e["name"] for e in out["extracted_entities"]] == ["graphify", "Neo4j"]
    assert all("entity_name" not in e for e in out["extracted_entities"])
    # entity_type_id is graphiti's expected key already and must survive untouched.
    assert all(e["entity_type_id"] == 0 for e in out["extracted_entities"])


def test_normalize_preserves_extra_keys_like_entity_type_name():
    raw = {"entity_name": "graphify", "entity_type_id": 0, "entity_type_name": "Tool"}
    out = _normalize_structured_keys(raw)
    assert out["name"] == "graphify"
    # Extra keys gemini adds are left intact; pydantic ignores them.
    assert out["entity_type_name"] == "Tool"


def test_normalize_does_not_touch_edge_source_target_entity_name():
    # Edge model uses distinct keys; the exact-key alias must not corrupt them.
    raw = {
        "source_entity_name": "graphify",
        "target_entity_name": "Neo4j",
        "fact": "graphify writes to Neo4j",
    }
    out = _normalize_structured_keys(raw)
    assert out == raw


def test_normalize_never_clobbers_existing_name():
    raw = {"name": "canonical", "entity_name": "deviant"}
    out = _normalize_structured_keys(raw)
    # A correct `name` already present wins; the alias is not applied over it.
    assert out["name"] == "canonical"


def test_normalize_passes_scalars_and_is_recursive():
    assert _normalize_structured_keys("x") == "x"
    assert _normalize_structured_keys(7) == 7
    nested = {"outer": [{"entity_name": "a"}]}
    assert _normalize_structured_keys(nested) == {"outer": [{"name": "a"}]}
