from __future__ import annotations

import json
from typing import Any

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core.bulk_semantic import (
    BulkSemanticEntity,
    BulkSemanticExtractionResult,
    BulkSemanticRelation,
    BulkSemanticSessionResult,
    BulkSemanticWriteReport,
    DeterministicGraphitiSemanticWriter,
    make_bulk_session_input,
    parse_bulk_semantic_result,
)
from agent_knowledge.llm_brain_core.bulk_semantic_cli import run_couchdb_bulk_semantic_projection
from agent_knowledge.llm_brain_core.graph_projection_status_cli import build_graph_projection_status
from agent_knowledge.llm_brain_core.runtime import session_episode_from_couchdb_source
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

PROVIDER = "codex"
PROJECT = "neurons"


class _FakeBulkExtractor:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def extract(self, batch):  # type: ignore[no-untyped-def]
        self.calls.append([item.session_key for item in batch])
        sessions = []
        for item in batch:
            sessions.append(
                BulkSemanticSessionResult(
                    session_key=item.session_key,
                    entities=(
                        BulkSemanticEntity(name=f"Neo4j {item.session_key}", type="Tool", summary="Graph backend"),
                        BulkSemanticEntity(name=f"Graphiti {item.session_key}", type="Library", summary="Graph layer"),
                    ),
                    relations=(
                        BulkSemanticRelation(
                            source=f"Graphiti {item.session_key}",
                            target=f"Neo4j {item.session_key}",
                            type="stores_in",
                            fact="Graphiti stores semantic graph data in Neo4j.",
                        ),
                    ),
                )
            )
        return BulkSemanticExtractionResult(tuple(sessions))


class _ExplodingExtractor:
    def extract(self, batch):  # type: ignore[no-untyped-def]
        _ = batch
        raise AssertionError("extractor should not run")


class _FakeBulkWriter:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def write_batch(self, inputs, extraction, *, allow_empty_sessions=False):  # type: ignore[no-untyped-def]
        _ = allow_empty_sessions
        self.calls.append([item.session_key for item in inputs])
        sessions = extraction.by_session_key()
        return BulkSemanticWriteReport(
            projected=len(inputs),
            entities_written=sum(len(sessions[item.session_key].entities) for item in inputs),
            relations_written=sum(len(sessions[item.session_key].relations) for item in inputs),
        )


class _FakeEmbedder:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.texts = list(texts)
        return [[float(index), float(index) + 0.5] for index, _text in enumerate(texts)]


class _FakeAsyncDriver:
    graph_operations_interface = None

    def __init__(self) -> None:
        from graphiti_core.driver.driver import GraphProvider

        self.provider = GraphProvider.NEO4J
        self.calls: list[dict[str, Any]] = []

    async def execute_query(self, query, **params):  # type: ignore[no-untyped-def]
        self.calls.append({"query": str(query), "params": dict(params)})
        return ([], None, None)


def _seed_session(
    store: InMemoryCouchDBSourceStore,
    *,
    raw_id: str,
    body: str = "Graphiti writes semantic entities into Neo4j using MENTIONS and RELATES_TO edges.",
) -> str:
    sid = dm.build_session_id_hash(PROVIDER, raw_id)
    session = TranscriptSession(
        session_id_hash=sid,
        provider=PROVIDER,
        project=PROJECT,
        started_at="2026-06-21T00:00:00Z",
    )
    store.put(dm.build_transcript_session_document(session=session))
    chunk = TranscriptChunk.from_text(
        chunk_id=f"chunk_{raw_id}",
        session_id_hash=sid,
        provider=PROVIDER,
        project=PROJECT,
        turn_start_index=0,
        turn_end_index=0,
        text=body,
    )
    chunk_doc = dm.build_conversation_chunk_document(chunk=chunk)
    store.put(chunk_doc)
    store.put(
        dm.build_coverage_manifest_document(
            session_id_hash=sid,
            provider=PROVIDER,
            project=PROJECT,
            conversation_chunk_count=1,
            tool_evidence_bundle_count=0,
            conversation_content_hashes=[chunk_doc["content_hash"]],
            tool_evidence_coverage_hashes=[],
            project_authority={
                "project": PROJECT,
                "ambiguous": False,
                "eligible_for_retirement": True,
            },
        )
    )
    return sid


def test_bulk_semantic_projects_five_sessions_per_llm_call_and_marks_entity_state(tmp_path):
    store = InMemoryCouchDBSourceStore()
    for index in range(6):
        _seed_session(store, raw_id=f"bulk-{index}")
    extractor = _FakeBulkExtractor()
    writer = _FakeBulkWriter()

    report = run_couchdb_bulk_semantic_projection(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        limit=6,
        project=PROJECT,
        provider=PROVIDER,
        max_sessions_per_call=5,
        max_projects=5,
        extractor=extractor,
        writer=writer,
    )

    assert report["status"] == "ok"
    assert report["projection"]["attempted"] == 5
    assert report["projection"]["projected"] == 5
    assert report["projection"]["failed"] == 0
    assert report["semantic"]["llm_batches"] == 1
    assert extractor.calls == [["s1", "s2", "s3", "s4", "s5"]]
    assert writer.calls == [["s1", "s2", "s3", "s4", "s5"]]
    assert report["semantic"]["entities_written"] == 10
    assert report["semantic"]["relations_written"] == 5
    assert report["raw_paths_printed"] is False

    status = build_graph_projection_status(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        project=PROJECT,
        provider=PROVIDER,
    )
    assert status["projection_state"]["entity_session_projected"] == 5
    assert status["projection_state"]["entity_session_backlog"] == 1


def test_bulk_semantic_resume_skips_without_llm_call(tmp_path):
    store = InMemoryCouchDBSourceStore()
    for index in range(3):
        _seed_session(store, raw_id=f"resume-{index}")

    first = run_couchdb_bulk_semantic_projection(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        limit=3,
        project=PROJECT,
        provider=PROVIDER,
        max_sessions_per_call=3,
        extractor=_FakeBulkExtractor(),
        writer=_FakeBulkWriter(),
    )
    assert first["projection"]["projected"] == 3

    second = run_couchdb_bulk_semantic_projection(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        limit=3,
        project=PROJECT,
        provider=PROVIDER,
        max_sessions_per_call=3,
        extractor=_ExplodingExtractor(),
        writer=_FakeBulkWriter(),
    )

    assert second["projection"]["projected"] == 0
    assert second["projection"]["skipped_resumed"] == 3
    assert second["semantic"]["llm_batches"] == 0
    assert second["projection"]["failed"] == 0


def test_bulk_semantic_max_projects_caps_below_batch_size(tmp_path):
    store = InMemoryCouchDBSourceStore()
    for index in range(4):
        _seed_session(store, raw_id=f"cap-{index}")
    extractor = _FakeBulkExtractor()

    report = run_couchdb_bulk_semantic_projection(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        limit=4,
        project=PROJECT,
        provider=PROVIDER,
        max_sessions_per_call=5,
        max_projects=2,
        extractor=extractor,
        writer=_FakeBulkWriter(),
    )

    assert report["projection"]["attempted"] == 2
    assert report["projection"]["projected"] == 2
    assert report["projection"]["stopped_after_max_projects"] is True
    assert extractor.calls == [["s1", "s2"]]


def test_bulk_semantic_parse_accepts_aliases_and_rejects_private_output():
    parsed = parse_bulk_semantic_result(
        {
            "sessions": [
                {
                    "session_key": "s1",
                    "entities": [{"entity_name": "Neo4j", "entity_type": "Tool"}],
                    "relations": [],
                }
            ]
        }
    )
    assert parsed.sessions[0].entities[0].name == "Neo4j"
    assert parsed.sessions[0].entities[0].type == "Tool"

    with pytest.raises(ValueError):
        parse_bulk_semantic_result(
            {
                "sessions": [
                    {
                        "session_key": "s1",
                        "entities": [{"name": "/Users/example/private.txt", "type": "File"}],
                        "relations": [],
                    }
                ]
            }
        )


def test_deterministic_writer_uses_graphiti_compatible_nodes_and_edges(tmp_path):
    store = InMemoryCouchDBSourceStore()
    sid = _seed_session(store, raw_id="writer")
    episode = session_episode_from_couchdb_source(session_id_hash=sid, source_store=store)
    item = make_bulk_session_input(session_key="s1", episode=episode, max_chars=800)
    extraction = BulkSemanticExtractionResult(
        (
            BulkSemanticSessionResult(
                session_key="s1",
                entities=(
                    BulkSemanticEntity(name="Graphiti", type="Library", summary="Temporal graph library"),
                    BulkSemanticEntity(name="Neo4j", type="Database", summary="Graph database"),
                ),
                relations=(
                    BulkSemanticRelation(
                        source="Graphiti",
                        target="Neo4j",
                        type="stores_in",
                        fact="Graphiti stores extracted facts in Neo4j.",
                    ),
                ),
            ),
        )
    )
    driver = _FakeAsyncDriver()
    embedder = _FakeEmbedder()
    writer = DeterministicGraphitiSemanticWriter(driver, embedder=embedder)

    report = writer.write_batch([item], extraction)

    assert report == BulkSemanticWriteReport(projected=1, entities_written=2, relations_written=1)
    assert any(call["params"].get("uuid") == episode.episode_id for call in driver.calls)
    entity_calls = [call for call in driver.calls if "entity_data" in call["params"]]
    mention_calls = [call for call in driver.calls if "episode_uuid" in call["params"]]
    relation_calls = [call for call in driver.calls if "edge_data" in call["params"]]
    assert len(entity_calls) == 2
    assert len(mention_calls) == 2
    assert len(relation_calls) == 1
    assert embedder.texts == ["Graphiti", "Neo4j", "Graphiti stores extracted facts in Neo4j."]
    assert entity_calls[0]["params"]["entity_data"]["name_embedding"] == [0.0, 0.5]
    assert relation_calls[0]["params"]["edge_data"]["fact_embedding"] == [2.0, 2.5]
