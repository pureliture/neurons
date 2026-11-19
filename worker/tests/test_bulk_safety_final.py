"""Final safety tests: worker identity, per-session coverage, stale completion."""

from __future__ import annotations

import multiprocessing as mp
import os
import uuid

from types import SimpleNamespace
import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core.bulk_full_input_pg_state import (
    LeaseConflict,
    LeaseExpired,
    PgChunkState,
)
from agent_knowledge.llm_brain_core.bulk_semantic import (
    BulkSemanticEntity,
    BulkSemanticExtractionResult,
    BulkSemanticSessionResult,
    BulkSemanticWriteReport,
)
from agent_knowledge.llm_brain_core.bulk_stream_runner import run_streamed_full_input
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


class _FailingWriter:
    def __init__(self, fail_call):
        self.fail_call = fail_call
        self.calls = 0

    def write_batch(self, inputs, extraction, *, allow_empty_sessions=False):
        self.calls += 1
        receipts = {} if self.calls == self.fail_call else {
            item.session_key: "receipt:" + item.session_key for item in inputs
        }
        return BulkSemanticWriteReport(
            projected=len(receipts), entities_written=len(receipts), relations_written=0,
            chunk_receipts=receipts,
        )


def _seed_session(store, name, bodies):
    sid = dm.build_session_id_hash(P, PFX + name)
    store.put(dm.build_transcript_session_document(
        session=TranscriptSession(session_id_hash=sid, provider=P, project=PFX,
                                  started_at="2026-01-01T00:00:00Z")))
    hashes = []
    for index, body in enumerate(bodies):
        chunk = TranscriptChunk.from_text(
            chunk_id=f"{name}_{index}", session_id_hash=sid, provider=P, project=PFX,
            turn_start_index=index, turn_end_index=index, text=body)
        doc = dm.build_conversation_chunk_document(chunk=chunk)
        hashes.append(str(doc["content_hash"]))
        store.put(doc)
    store.put(dm.build_coverage_manifest_document(
        session_id_hash=sid, provider=P, project=PFX,
        conversation_chunk_count=len(bodies), tool_evidence_bundle_count=0,
        conversation_content_hashes=hashes, tool_evidence_coverage_hashes=[],
        project_authority={"project": PFX, "ambiguous": False,
                           "eligible_for_retirement": True}))
    return sid


def _claim_worker(dsn, key, owner, queue):
    try:
        state = PgChunkState(dsn)
        queue.put(("ok", state.claim(key=key, owner=owner)))
    except Exception as exc:
        queue.put(("error", type(exc).__name__))


def test_two_processes_unique_owners_cannot_both_claim_live_lease():
    key = PFX + "crossproc"
    state = PgChunkState(os.environ["TRIAL_PG_DSN"])
    state.register_plan(run_id="r", project=PFX, session_hash=PFX,
                        policy_version="v", source_hash="s", map_digest="m",
                        chunk_keys=[key])
    queue = mp.Queue()
    processes = [
        mp.Process(target=_claim_worker,
                   args=(os.environ["TRIAL_PG_DSN"], key, f"worker-{uuid.uuid4().hex}", queue))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
    results = [queue.get(timeout=5) for _ in range(2)]
    successes = [item for item in results if item[0] == "ok"]
    conflicts = [item for item in results if item[0] == "error" and item[1] == "LeaseConflict"]
    assert len(successes) == 1
    assert len(conflicts) == 1


def test_mixed_session_outcomes_keep_separate_coverage(tmp_path, monkeypatch):
    from agent_knowledge.llm_brain_core import bulk_stream_runner as runner
    store = InMemoryCouchDBSourceStore()
    good_sid = _seed_session(store, "good", ["a"])
    bad_sid = _seed_session(store, "bad", ["b"])
    monkeypatch.setattr(runner, "_select_sessions_bounded",
                        lambda *args, **kwargs: SimpleNamespace(
                            sessions=[{"session_id_hash": good_sid, "project": PFX},
                                      {"session_id_hash": bad_sid, "project": PFX}]
                        ))
    report = run_streamed_full_input(
        ledger_path=tmp_path / "ledger.db", source_store=store,
        extractor=_Extractor(), writer=_FailingWriter(2),
        full_input_state=PgChunkState(os.environ["TRIAL_PG_DSN"]),
        policy_version=PFX + "v1", project=PFX, limit=2, batch_size=1,
    )
    assert len(report["session_coverage"]) == 2
    values = list(report["session_coverage"].values())
    assert any(item["coverage_complete"] for item in values)
    assert any(not item["coverage_complete"] for item in values)
    assert report["stream_coverage_complete"] is False


def test_stale_completion_after_expired_lease_is_rejected():
    state = PgChunkState(os.environ["TRIAL_PG_DSN"])
    key = PFX + "expired-complete"
    state.register_plan(run_id="r", project=PFX, session_hash=PFX,
                        policy_version="v", source_hash="s", map_digest="m",
                        chunk_keys=[key])
    token = state.claim(key=key, owner="worker-A", lease_seconds=300)
    state.force_expire(key)
    with pytest.raises(LeaseExpired):
        state.complete(key=key, fence_token=token, output_digest="o",
                       write_receipt="r", owner="worker-A")


def _neo_writer_process(uri, password, token, delay, queue, key):
    import time
    from types import SimpleNamespace
    from agent_knowledge.llm_brain_core.bulk_full_input_neo4j_writer import Neo4jShadowWriter
    from agent_knowledge.llm_brain_core.bulk_semantic import (
        BulkSemanticExtractionResult, BulkSemanticSessionResult, BulkSemanticEntity,
    )
    time.sleep(delay)
    try:
        writer = Neo4jShadowWriter(uri, "neo4j", password)
        item = SimpleNamespace(
            session_key=key,
            episode=SimpleNamespace(natural_id=key + "-nat", payload={
                "graph_group_id": "shadow_" + key,
                "canonical_chunk_hash": "sha256:" + "a" * 64,
                "fence_token": token,
            }),
            text="s",
        )
        extraction = BulkSemanticExtractionResult((BulkSemanticSessionResult(
            session_key=key,
            entities=(BulkSemanticEntity(name="E", type="Concept", summary="s"),),
        ),))
        writer.write_batch([item], extraction)
        writer.close()
        queue.put(("ok", token))
    except Exception as exc:
        queue.put(("error", type(exc).__name__))


@pytest.mark.skipif(not os.environ.get("TRIAL_NEO4J_URI"), reason="no isolated Neo4j")
def test_two_process_neo4j_fence_interleaving():
    key = PFX + "neo-interleave"
    queue = mp.Queue()
    args = (os.environ["TRIAL_NEO4J_URI"], os.environ["TRIAL_NEO4J_PASSWORD"])
    stale = mp.Process(target=_neo_writer_process, args=(*args, 1, 1.0, queue, key))
    fresh = mp.Process(target=_neo_writer_process, args=(*args, 9, 0.0, queue, key))
    fresh.start()
    stale.start()
    fresh.join(timeout=60)
    stale.join(timeout=60)
    results = [queue.get(timeout=5) for _ in range(2)]
    assert ("ok", 9) in results
    assert any(item[0] == "error" for item in results)
