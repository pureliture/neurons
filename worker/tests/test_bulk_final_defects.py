"""RED-first tests for coverage, lease/fencing, and evidence membership defects."""

from __future__ import annotations

import os
import uuid

import json
import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core.bulk_full_input_pg_state import (
    LeaseConflict,
    PgChunkState,
)
from agent_knowledge.llm_brain_core.bulk_full_input_neo4j_writer import Neo4jShadowWriter
from agent_knowledge.llm_brain_core.bulk_semantic import (
    BulkSemanticEntity,
    BulkSemanticExtractionResult,
    BulkSemanticSessionResult,
    BulkSemanticWriteReport,
)
from agent_knowledge.llm_brain_core.bulk_stream_runner import run_streamed_full_input
from agent_knowledge.llm_brain_core.bulk_source_validation import plan_validated_source
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

pytestmark = pytest.mark.skipif(not os.environ.get("TRIAL_PG_DSN"), reason="no isolated PG")
P = "synthetic-provider"
PFX = uuid.uuid4().hex[:8] + "-"


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


class _LastBatchFailureWriter:
    def __init__(self, total):
        self.total = total
        self.calls = 0

    def write_batch(self, inputs, extraction, *, allow_empty_sessions=False):
        self.calls += 1
        if self.calls == self.total:
            return BulkSemanticWriteReport(projected=0, entities_written=0, relations_written=0)
        return BulkSemanticWriteReport(
            projected=len(inputs), entities_written=len(inputs), relations_written=0,
            chunk_receipts={item.session_key: "receipt:" + item.session_key for item in inputs},
        )


def _seed(store, project, bodies, evidence=0):
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
    ev_hashes = []
    for i in range(evidence):
        doc = dm.build_tool_evidence_bundle_document(
            session_id_hash=sid, provider=P, project=project, part_index=i,
            part_count=evidence, evidence_index_start=i, evidence_index_end=i,
            record_content_hashes=[], body=f"evidence-{i}")
        store.put(doc)
        ev_hashes.append(str(doc["coverage_hash"]))
    store.put(dm.build_coverage_manifest_document(
        session_id_hash=sid, provider=P, project=project,
        conversation_chunk_count=len(bodies), tool_evidence_bundle_count=evidence,
        conversation_content_hashes=hashes, tool_evidence_coverage_hashes=ev_hashes,
        project_authority={"project": project, "ambiguous": False,
                           "eligible_for_retirement": True}))
    return sid


def test_last_batch_failure_marks_coverage_incomplete(tmp_path):
    store = InMemoryCouchDBSourceStore()
    project = PFX + "coverage"
    _seed(store, project, ["a", "b", "c"], evidence=1)
    state = PgChunkState(os.environ["TRIAL_PG_DSN"])
    report = run_streamed_full_input(
        ledger_path=tmp_path / "ledger.db", source_store=store,
        extractor=_Extractor(), writer=_LastBatchFailureWriter(4),
        full_input_state=state, policy_version=PFX + "v1", project=project,
        limit=1, batch_size=1,
    )
    assert len(report["session_coverage"]) == 1
    coverage = next(iter(report["session_coverage"].values()))
    assert coverage["coverage_complete"] is False
    assert coverage["verified_chunks"] < coverage["planned_chunks"]


def test_unexpired_owner_claim_from_other_worker_is_rejected():
    state = PgChunkState(os.environ["TRIAL_PG_DSN"])
    key = PFX + "lease"
    state.register_plan(run_id="r", project=PFX, session_hash=PFX,
                        policy_version="v", source_hash="s", map_digest="m",
                        chunk_keys=[key])
    state.claim(key=key, owner="worker-A", lease_seconds=300)
    with pytest.raises(LeaseConflict):
        state.claim(key=key, owner="worker-B", lease_seconds=300)


def test_expired_owner_completion_is_rejected():
    state = PgChunkState(os.environ["TRIAL_PG_DSN"])
    key = PFX + "expired"
    state.register_plan(run_id="r", project=PFX, session_hash=PFX,
                        policy_version="v", source_hash="s", map_digest="m",
                        chunk_keys=[key])
    token = state.claim(key=key, owner="worker-A", lease_seconds=300)
    state.force_expire(key)
    new_token = state.claim(key=key, owner="worker-B", lease_seconds=300)
    assert new_token > token
    with pytest.raises(Exception):
        state.complete(key=key, fence_token=token, output_digest="o",
                       write_receipt="r", owner="worker-A")


@pytest.mark.skipif(not os.environ.get("TRIAL_NEO4J_URI"), reason="no isolated Neo4j")
def test_graph_fence_rejects_stale_writer_token():
    writer = Neo4jShadowWriter(os.environ["TRIAL_NEO4J_URI"], "neo4j",
                               os.environ["TRIAL_NEO4J_PASSWORD"])
    from agent_knowledge.llm_brain_core.bulk_full_input import build_session_grounding_fact
    from types import SimpleNamespace
    try:
        group = "shadow_" + PFX
        key = PFX + "fence"
        writer.write_batch(
            [SimpleNamespace(session_key=key,
                             episode=SimpleNamespace(natural_id=PFX + "n", payload={
                                 "graph_group_id": group,
                                 "canonical_chunk_hash": "sha256:" + "a" * 64,
                                 "fence_token": 9}), text="s")],
            _Extractor().extract([SimpleNamespace(
                session_key=key,
                episode=SimpleNamespace(natural_id=PFX + "n", payload={}), text="s")]),
        )
        with pytest.raises(Exception):
            writer.write_batch(
                [SimpleNamespace(session_key=key,
                                 episode=SimpleNamespace(natural_id=PFX + "n", payload={
                                     "graph_group_id": group,
                                     "canonical_chunk_hash": "sha256:" + "a" * 64,
                                     "fence_token": 2}), text="s")],
                _Extractor().extract([SimpleNamespace(
                    session_key=key,
                    episode=SimpleNamespace(natural_id=PFX + "n", payload={}), text="s")]),
            )
    finally:
        writer.delete_synthetic(chunk_key=key)
        writer.close()


def test_evidence_coverage_hash_mismatch_is_rejected():
    store = InMemoryCouchDBSourceStore()
    project = PFX + "evidence"
    sid = _seed(store, project, ["a"], evidence=2)
    docs = store.find_by_session(session_id_hash=sid)
    manifest = next(d for d in docs if d.get("doc_type") == dm.SourceDocType.COVERAGE_MANIFEST)
    manifest["tool_evidence_coverage_hashes"] = ["sha256:" + "0" * 64, "sha256:" + "1" * 64]
    store.put(manifest)
    with pytest.raises(Exception):
        plan_validated_source(store, session_id_hash=sid, expected_source_hash="any")
