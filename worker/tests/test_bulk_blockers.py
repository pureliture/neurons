"""Integration tests for the four independent-review blockers. Synthetic/local only."""

from __future__ import annotations

import json
import os
import uuid

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core import bulk_semantic_cli as cli
from agent_knowledge.llm_brain_core.bulk_full_input import SourceInvalid
from agent_knowledge.llm_brain_core.bulk_full_input_pg_state import PgChunkState
from agent_knowledge.llm_brain_core.bulk_source_validation import plan_validated_source
from agent_knowledge.llm_brain_core.bulk_stream_runner import run_streamed_full_input
from agent_knowledge.llm_brain_core.bulk_semantic import (
    BulkSemanticEntity,
    BulkSemanticExtractionResult,
    BulkSemanticSessionResult,
    BulkSemanticWriteReport,
    OpenAICompatibleBulkSemanticExtractor,
)
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

pytestmark = pytest.mark.skipif(not os.environ.get("TRIAL_PG_DSN"), reason="no isolated PG")
P = "synthetic-provider"
PFX = uuid.uuid4().hex[:8] + "-"


class _Extractor:
    def __init__(self, on_extract=None):
        self.calls = 0
        self.on_extract = on_extract
        self.model = "free-model"
        self.provider = P
        self.endpoint_class = "approved_free"
        self.price_status = "free_confirmed"

    def extract(self, batch):
        self.calls += 1
        if self.on_extract:
            self.on_extract()
        return BulkSemanticExtractionResult(tuple(
            BulkSemanticSessionResult(
                session_key=item.session_key,
                entities=(BulkSemanticEntity(name=PFX + "E", type="Concept", summary="s"),),
            )
            for item in batch
        ))


class _ReceiptWriter:
    def write_batch(self, inputs, extraction, *, allow_empty_sessions=False):
        return BulkSemanticWriteReport(
            projected=len(inputs), entities_written=len(inputs), relations_written=0,
            chunk_receipts={item.session_key: "receipt:" + item.session_key for item in inputs},
        )


def _seed(store, project, bodies, evidence=False):
    sid = dm.build_session_id_hash(P, project + "-session")
    store.put(dm.build_transcript_session_document(
        session=TranscriptSession(session_id_hash=sid, provider=P, project=project,
                                  started_at="2026-01-01T00:00:00Z")))
    hashes = []
    for i, body in enumerate(bodies):
        chunk = TranscriptChunk.from_text(chunk_id=f"chunk_{i}", session_id_hash=sid,
                                          provider=P, project=project, turn_start_index=i,
                                          turn_end_index=i, text=body)
        doc = dm.build_conversation_chunk_document(chunk=chunk)
        hashes.append(str(doc["content_hash"]))
        store.put(doc)
    ec = 0
    if evidence:
        store.put(dm.build_tool_evidence_bundle_document(
            session_id_hash=sid, provider=P, project=project, part_index=0, part_count=1,
            evidence_index_start=0, evidence_index_end=1, record_content_hashes=[],
            body="synthetic evidence"))
        ec = 1
    store.put(dm.build_coverage_manifest_document(
        session_id_hash=sid, provider=P, project=project,
        conversation_chunk_count=len(bodies), tool_evidence_bundle_count=ec,
        conversation_content_hashes=hashes, tool_evidence_coverage_hashes=[],
        project_authority={"project": project, "ambiguous": False,
                           "eligible_for_retirement": True}))
    return sid


def _run_stream(store, source_hash, extractor, state, project, tmp_path, batch_size=1):
    return run_streamed_full_input(
        ledger_path=tmp_path / "ledger.db", source_store=store,
        extractor=extractor, writer=_ReceiptWriter(), full_input_state=state,
        policy_version=PFX + "v1", project=project, limit=1,
        batch_size=batch_size,
    )


def test_source_change_during_run_supersedes_and_promotes_nothing(tmp_path):
    store = InMemoryCouchDBSourceStore()
    project = PFX + "fence"
    sid = _seed(store, project, ["a", "b"])
    docs = store.find_by_session(session_id_hash=sid)
    chunk = next(d for d in docs if d.get("doc_type") == dm.SourceDocType.CONVERSATION_CHUNK)

    def mutate():
        changed = TranscriptChunk.from_text(chunk_id="chunk_0", session_id_hash=sid,
                                            provider=P, project=project, turn_start_index=0,
                                            turn_end_index=0, text="a CHANGED")
        store.put({**dm.build_conversation_chunk_document(chunk=changed),
                   "_rev": chunk.get("_rev")})

    extractor = _Extractor(on_extract=mutate)
    state = PgChunkState(os.environ["TRIAL_PG_DSN"])
    report = _run_stream(store, "ignored", extractor, state, project, tmp_path)
    assert report["superseded"] > 0
    assert report["stream_coverage_complete"] is False
    assert state.completed_chunks(project, PFX + "v1") == []


def test_out_of_order_and_missing_members_fail_before_model_call(tmp_path):
    store = InMemoryCouchDBSourceStore()
    project = PFX + "order"
    sid = _seed(store, project, ["a", "b", "c"])
    docs = store.find_by_session(session_id_hash=sid)
    manifest = next(d for d in docs if d.get("doc_type") == dm.SourceDocType.COVERAGE_MANIFEST)
    chunks = [d for d in docs if d.get("doc_type") == dm.SourceDocType.CONVERSATION_CHUNK]
    manifest["conversation_content_hashes"] = list(reversed(
        [str(d["content_hash"]) for d in chunks]
    ))
    store.put(manifest)
    extractor = _Extractor()
    state = PgChunkState(os.environ["TRIAL_PG_DSN"])
    report = _run_stream(store, "ignored", extractor, state, project, tmp_path)
    assert extractor.calls == 0
    assert report["status"] == "failed"
    assert report["failure_reasons"].get("source_invalid", 0) >= 1


def test_stream_mode_rejects_non_shadow_writer_before_any_db_write(tmp_path, monkeypatch):
    monkeypatch.setenv("NEURON_TRIAL_MODE", "1")
    monkeypatch.setenv("LLM_BRAIN_BULK_FREE_MODEL_ALLOWLIST", "free-model")
    store = InMemoryCouchDBSourceStore()
    project = PFX + "guard"
    _seed(store, project, ["a"])
    monkeypatch.setattr(cli, "_build_source_store", lambda **kw: store)
    monkeypatch.setattr(OpenAICompatibleBulkSemanticExtractor, "from_env",
                        classmethod(lambda cls, **kw: _Extractor()))
    args = ["--ledger", str(tmp_path / "ledger.db"), "--runtime-dir", str(tmp_path / "rt"),
            "--full-input-policy-version", PFX + "v1", "--stream-mode", "--limit", "1"]
    rc = cli.main(args)
    assert rc == 1


def test_shadow_writer_uses_shadow_group_and_leaves_legacy_node_untouched(tmp_path):
    from agent_knowledge.llm_brain_core.bulk_full_input_neo4j_writer import Neo4jShadowWriter
    if not (os.environ.get("TRIAL_NEO4J_URI") and os.environ.get("TRIAL_NEO4J_PASSWORD")):
        pytest.skip("no isolated Neo4j")
    writer = Neo4jShadowWriter(os.environ["TRIAL_NEO4J_URI"], "neo4j",
                               os.environ["TRIAL_NEO4J_PASSWORD"])
    store = InMemoryCouchDBSourceStore()
    project = PFX + "neo"
    _seed(store, project, ["a"])
    extractor = _Extractor()
    state = PgChunkState(os.environ["TRIAL_PG_DSN"])
    try:
        writer.set_legacy_marker(uid=PFX + "legacy", marker="unchanged")
        report = run_streamed_full_input(
            ledger_path=tmp_path / "ledger.db", source_store=store,
            extractor=extractor, writer=writer, full_input_state=state,
            policy_version=PFX + "v1", project=project, limit=1, batch_size=1,
        )
        assert report["status"] == "ok"
        assert writer.legacy_marker(uid=PFX + "legacy") == "unchanged"
    finally:
        writer.delete_legacy_marker(uid=PFX + "legacy")
        writer.close()
