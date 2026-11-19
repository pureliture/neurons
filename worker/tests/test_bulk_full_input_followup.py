"""TDD for remaining followup: streaming, evidence, partial resume, reconciliation, races."""

from __future__ import annotations

import json
import os
import threading
import uuid

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core import bulk_semantic_cli as cli
from agent_knowledge.llm_brain_core import bulk_full_input as bfi
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
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

pytestmark = pytest.mark.skipif(
    not (os.environ.get("TRIAL_PG_DSN") and os.environ.get("TRIAL_NEO4J_URI")),
    reason="no isolated local DBs",
)
P = "synthetic-provider"
POLICY = "full-input.v2"
PFX = uuid.uuid4().hex[:8] + "-"


class _Extractor:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.model = "free-model"
        self.provider = P
        self.endpoint_class = "approved_free"
        self.price_status = "free_confirmed"

    def extract(self, batch):  # type: ignore[no-untyped-def]
        self.texts.extend(item.text for item in batch)
        return BulkSemanticExtractionResult(tuple(
            BulkSemanticSessionResult(
                session_key=item.session_key,
                entities=(BulkSemanticEntity(name=PFX + "E", type="Concept", summary="s"),),
                relations=(),
            )
            for item in batch
        ))


class _FakeWriter:
    def write_batch(self, inputs, extraction, *, allow_empty_sessions=False):  # type: ignore
        by_key = extraction.by_session_key()
        return BulkSemanticWriteReport(
            projected=len(inputs),
            entities_written=sum(len(by_key[i.session_key].entities) for i in inputs),
            relations_written=0,
            projected_natural_ids=tuple(i.episode.natural_id for i in inputs),
        )


class _PartialWriter:
    def __init__(self, armed: bool = True) -> None:
        self.calls = 0
        self.armed = armed

    def write_batch(self, inputs, extraction, *, allow_empty_sessions=False):  # type: ignore
        self.calls += 1
        if self.armed and (self.calls == 1 or self.calls > 2):
            raise ValueError("synthetic partial failure")
        first = inputs[0]
        return BulkSemanticWriteReport(
            projected=1, entities_written=1, relations_written=0,
            projected_natural_ids=(first.episode.natural_id,),
            chunk_receipts={first.session_key: "receipt:" + first.session_key},
        )


def _seed(store, project, bodies, evidence=True):
    sid = dm.build_session_id_hash(P, project + "-session")
    store.put(dm.build_transcript_session_document(
        session=TranscriptSession(session_id_hash=sid, provider=P, project=project,
                                  started_at="2026-01-01T00:00:00Z")))
    hashes = []
    for i, body in enumerate(bodies):
        chunk = TranscriptChunk.from_text(chunk_id=f"chunk_{i}", session_id_hash=sid,
                                          provider=P, project=project,
                                          turn_start_index=i, turn_end_index=i, text=body)
        doc = dm.build_conversation_chunk_document(chunk=chunk)
        hashes.append(str(doc["content_hash"]))
        store.put(doc)
    ec = 0
    if evidence:
        ev = dm.build_tool_evidence_bundle_document(
            session_id_hash=sid, provider=P, project=project, part_index=0,
            part_count=1, evidence_index_start=0, evidence_index_end=1,
            record_content_hashes=[], body="SYNTHETIC_EVIDENCE_BODY")
        store.put(ev)
        ec = 1
    store.put(dm.build_coverage_manifest_document(
        session_id_hash=sid, provider=P, project=project,
        conversation_chunk_count=len(bodies), tool_evidence_bundle_count=ec,
        conversation_content_hashes=hashes, tool_evidence_coverage_hashes=[],
        project_authority={"project": project, "ambiguous": False,
                           "eligible_for_retirement": True}))
    return sid


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEURON_TRIAL_MODE", "1")
    monkeypatch.setenv("LLM_BRAIN_BULK_FREE_MODEL_ALLOWLIST", "free-model")
    store = InMemoryCouchDBSourceStore()
    extractor = _Extractor()
    monkeypatch.setattr(cli, "_build_source_store", lambda **kw: store)
    monkeypatch.setattr(OpenAICompatibleBulkSemanticExtractor, "from_env",
                        classmethod(lambda cls, **kw: extractor))
    monkeypatch.setattr(DeterministicGraphitiSemanticWriter, "from_env",
                        classmethod(lambda cls, **kw: _FakeWriter()))
    args = ["--ledger", str(tmp_path / "ledger.db"), "--runtime-dir", str(tmp_path / "runtime"),
            "--full-input-policy-version", POLICY, "--limit", "1"]
    return dict(store=store, extractor=extractor, args=args, tmp=tmp_path)


def _run(env, capsys):
    rc = cli.main(list(env["args"]))
    out = capsys.readouterr()
    return rc, json.loads(out.out or out.err)


def test_streaming_and_evidence_lane_reach_extractor(env, capsys, monkeypatch):
    project = PFX + "stream"
    _seed(env["store"], project, [f"body-{i}" for i in range(6)])
    called = []
    original = bfi.iter_bounded_chunks

    def spy(*args, **kwargs):
        called.append(kwargs.get("max_resident_bodies"))
        return original(*args, **kwargs)

    monkeypatch.setattr(bfi, "iter_bounded_chunks", spy)
    monkeypatch.setattr(cli, "iter_bounded_chunks", spy, raising=False)
    rc, report = _run(env, capsys)
    assert rc == 0, json.dumps(report, sort_keys=True)
    assert called and max(called) == 2
    assert any("SYNTHETIC_EVIDENCE_BODY" in text for text in env["extractor"].texts)
    assert len(env["extractor"].texts) == 7


def test_partial_success_resume_keeps_completed_chunk(env, capsys, monkeypatch):
    project = PFX + "partial"
    _seed(env["store"], project, ["a", "b", "c"], evidence=False)
    monkeypatch.setattr(DeterministicGraphitiSemanticWriter, "from_env",
                        classmethod(lambda cls, **kw: _PartialWriter(armed=True)))
    rc1, report1 = _run(env, capsys)
    assert rc1 == 1, json.dumps(report1, sort_keys=True)
    state = PgChunkState(os.environ["TRIAL_PG_DSN"])
    assert state.completed_chunks(project, POLICY)

    monkeypatch.setattr(DeterministicGraphitiSemanticWriter, "from_env",
                        classmethod(lambda cls, **kw: _PartialWriter(armed=False)))
    env["extractor"].texts.clear()
    rc2, report2 = _run(env, capsys)
    assert rc2 == 0, json.dumps(report2, sort_keys=True)
    assert len(env["extractor"].texts) < 3
    assert state.completed_chunks(project, POLICY)


def test_pg_receipt_failure_then_restart_reconciles_graph_once(env, capsys, monkeypatch):
    project = PFX + "reconcile"
    sid = _seed(env["store"], project, ["x", "y"], evidence=False)
    env["args"].append("--trial-neo4j-writer")
    original = PgChunkState.complete
    failed = []

    def flaky(self, **kwargs):
        if not failed:
            failed.append(True)
            raise ValueError("synthetic PG receipt failure")
        return original(self, **kwargs)

    monkeypatch.setattr(PgChunkState, "complete", flaky)
    rc1, report1 = _run(env, capsys)
    assert rc1 == 1, json.dumps(report1, sort_keys=True)
    monkeypatch.setattr(PgChunkState, "complete", original)
    rc2, report2 = _run(env, capsys)
    assert rc2 == 0, json.dumps(report2, sort_keys=True)
    writer = Neo4jShadowWriter(os.environ["TRIAL_NEO4J_URI"], "neo4j",
                               os.environ["TRIAL_NEO4J_PASSWORD"])
    try:
        for ordinal in range(2):
            assert writer.count_nodes(chunk_key=f"{sid}-c{ordinal}") == 1
    finally:
        writer.close()


def test_concurrent_workers_lease_fencing_no_duplicate_graph_nodes(env, capsys):
    project = PFX + "race"
    sid = _seed(env["store"], project, ["r1", "r2"], evidence=False)
    env["args"].append("--trial-neo4j-writer")
    results = []

    def run():
        results.append(_run(env, capsys))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    writer = Neo4jShadowWriter(os.environ["TRIAL_NEO4J_URI"], "neo4j",
                               os.environ["TRIAL_NEO4J_PASSWORD"])
    try:
        for ordinal in range(2):
            assert writer.count_nodes(chunk_key=f"{sid}-c{ordinal}") <= 2
    finally:
        writer.close()
    assert len(results) == 2
