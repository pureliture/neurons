"""RED-first tests for shadow group + fence token contracts on both paths."""

from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core import bulk_semantic_cli as cli
from agent_knowledge.llm_brain_core.bulk_full_input_neo4j_writer import Neo4jShadowWriter
from agent_knowledge.llm_brain_core.bulk_full_input_pg_state import PgChunkState
from agent_knowledge.llm_brain_core.bulk_semantic import (
    BulkSemanticEntity,
    BulkSemanticExtractionResult,
    BulkSemanticSessionResult,
    BulkSemanticWriteReport,
    DeterministicGraphitiSemanticWriter,
    OpenAICompatibleBulkSemanticExtractor,
)
from agent_knowledge.llm_brain_core.bulk_stream_runner import run_streamed_full_input
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

pytestmark = pytest.mark.skipif(not os.environ.get("TRIAL_PG_DSN"), reason="no isolated PG")
P = "synthetic-provider"
PFX = uuid.uuid4().hex[:8] + "-"


def _seed(store, project, bodies=("a", "b")):
    sid = dm.build_session_id_hash(P, project + "-session")
    store.put(dm.build_transcript_session_document(
        session=TranscriptSession(session_id_hash=sid, provider=P, project=project,
                                  started_at="2026-01-01T00:00:00Z")))
    hashes = []
    for index, body in enumerate(bodies):
        chunk = TranscriptChunk.from_text(chunk_id=f"{index}", session_id_hash=sid,
                                          provider=P, project=project, turn_start_index=index,
                                          turn_end_index=index, text=body)
        doc = dm.build_conversation_chunk_document(chunk=chunk)
        hashes.append(str(doc["content_hash"]))
        store.put(doc)
    store.put(dm.build_coverage_manifest_document(
        session_id_hash=sid, provider=P, project=project,
        conversation_chunk_count=len(bodies), tool_evidence_bundle_count=0,
        conversation_content_hashes=hashes, tool_evidence_coverage_hashes=[],
        project_authority={"project": project, "ambiguous": False,
                           "eligible_for_retirement": True}))
    return sid


class _Extractor:
    model = "free-model"
    provider = P
    endpoint_class = "approved_free"
    price_status = "free_confirmed"

    def extract(self, batch):
        return BulkSemanticExtractionResult(tuple(
            BulkSemanticSessionResult(
                session_key=item.session_key,
                entities=(BulkSemanticEntity(name=PFX + "E", type="Concept", summary="s"),),
            )
            for item in batch
        ))


class _CapturingWriter:
    def __init__(self):
        self.payloads = []

    def write_batch(self, inputs, extraction, *, allow_empty_sessions=False):
        self.payloads.extend(dict(item.episode.payload) for item in inputs)
        return BulkSemanticWriteReport(
            projected=len(inputs), entities_written=len(inputs), relations_written=0,
            chunk_receipts={item.session_key: "receipt:" + item.session_key for item in inputs},
        )


def test_cli_full_input_payload_has_token_and_shadow_group(tmp_path, monkeypatch):
    monkeypatch.setenv("NEURON_TRIAL_MODE", "1")
    monkeypatch.setenv("LLM_BRAIN_BULK_FREE_MODEL_ALLOWLIST", "free-model")
    store = InMemoryCouchDBSourceStore()
    project = PFX + "cli"
    _seed(store, project)
    monkeypatch.setattr(cli, "_build_source_store", lambda **kw: store)
    monkeypatch.setattr(OpenAICompatibleBulkSemanticExtractor, "from_env",
                        classmethod(lambda cls, **kw: _Extractor()))
    writer = _CapturingWriter()
    monkeypatch.setattr(DeterministicGraphitiSemanticWriter, "from_env",
                        classmethod(lambda cls, **kw: writer))
    rc = cli.main(["--ledger", str(tmp_path / "ledger.db"),
                   "--runtime-dir", str(tmp_path / "rt"),
                   "--full-input-policy-version", PFX + "v1", "--limit", "1"])
    assert rc == 0
    assert writer.payloads
    assert all(int(p.get("fence_token") or 0) > 0 for p in writer.payloads)
    assert all(str(p.get("graph_group_id") or "").startswith("shadow_")
               for p in writer.payloads)


@pytest.mark.skipif(not os.environ.get("TRIAL_NEO4J_URI"), reason="no isolated Neo4j")
def test_writer_rejects_missing_token_or_group_before_db_access():
    writer = Neo4jShadowWriter(os.environ["TRIAL_NEO4J_URI"], "neo4j",
                               os.environ["TRIAL_NEO4J_PASSWORD"])
    item = SimpleNamespace(
        session_key=PFX + "bad",
        episode=SimpleNamespace(natural_id=PFX + "nat", payload={
            "canonical_chunk_hash": "sha256:" + "a" * 64,
        }),
        text="s",
    )
    extraction = BulkSemanticExtractionResult((BulkSemanticSessionResult(
        session_key=item.session_key,
        entities=(BulkSemanticEntity(name="E", type="Concept", summary="s"),),
    ),))
    with pytest.raises(RuntimeError, match="contract missing"):
        writer.write_batch([item], extraction)
    writer.close()


@pytest.mark.skipif(not os.environ.get("TRIAL_NEO4J_URI"), reason="no isolated Neo4j")
def test_stream_runner_writes_with_shadow_group_and_token(tmp_path):
    store = InMemoryCouchDBSourceStore()
    project = PFX + "runner"
    _seed(store, project)
    writer = Neo4jShadowWriter(os.environ["TRIAL_NEO4J_URI"], "neo4j",
                               os.environ["TRIAL_NEO4J_PASSWORD"])
    report = run_streamed_full_input(
        ledger_path=tmp_path / "ledger.db", source_store=store,
        extractor=_Extractor(), writer=writer,
        full_input_state=PgChunkState(os.environ["TRIAL_PG_DSN"]),
        policy_version=PFX + "v1", project=project, limit=1, batch_size=1,
    )
    assert report["status"] == "ok", json.dumps(report, sort_keys=True)
    writer.close()
