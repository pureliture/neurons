"""RED-first tests for durable lease rollback and per-batch claim/complete."""

from __future__ import annotations

import os
import uuid

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core.bulk_full_input_pg_state import (
    FencingRejected,
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


def _fence_token(key: str) -> int:
    import psycopg
    with psycopg.connect(os.environ["TRIAL_PG_DSN"], autocommit=True) as connection:
        row = connection.execute(
            "SELECT fence_token FROM llm_brain_full_input_chunk_trial WHERE chunk_key = %s",
            (key,),
        ).fetchone()
    return int(row[0]) if row else 0

pytestmark = pytest.mark.skipif(not os.environ.get("TRIAL_PG_DSN"), reason="no isolated PG")
P = "synthetic-provider"
PFX = uuid.uuid4().hex[:8] + "-"


def _state():
    return PgChunkState(os.environ["TRIAL_PG_DSN"])


def _register(state, keys):
    state.register_plan(run_id="r", project=PFX, session_hash=PFX,
                        policy_version="v", source_hash="s", map_digest="m",
                        chunk_keys=keys)


def test_complete_many_rejects_expired_lease():
    state = _state()
    keys = [PFX + "-exp-a", PFX + "-exp-b"]
    _register(state, keys)
    tokens = state.claim_many(keys, owner="w1", lease_seconds=300)
    state.force_expire(keys[0])
    entries = [{"key": key, "fence_token": tokens[key],
                "output_digest": "o", "write_receipt": "r"} for key in keys]
    with pytest.raises(LeaseExpired):
        state.complete_many(entries, owner="w1")
    assert state.is_complete_many(keys) == set()


def test_claim_many_all_or_nothing_on_partial_conflict():
    state = _state()
    keys = [PFX + "-rb-a", PFX + "-rb-b"]
    _register(state, keys)
    token_a = state.claim(key=keys[0], owner="other", lease_seconds=300)
    before_b = _fence_token(keys[1])
    with pytest.raises(LeaseConflict):
        state.claim_many(keys, owner="w2", lease_seconds=300)
    assert _fence_token(keys[0]) == token_a
    assert _fence_token(keys[1]) == before_b


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


class _Writer:
    def write_batch(self, inputs, extraction, *, allow_empty_sessions=False):
        return BulkSemanticWriteReport(
            projected=len(inputs), entities_written=len(inputs), relations_written=0,
            chunk_receipts={item.session_key: "receipt:" + item.session_key for item in inputs},
        )


def _seed(store, count):
    sid = dm.build_session_id_hash(P, PFX + "runner")
    store.put(dm.build_transcript_session_document(
        session=TranscriptSession(session_id_hash=sid, provider=P, project=PFX,
                                  started_at="2026-01-01T00:00:00Z")))
    hashes = []
    for i in range(count):
        chunk = TranscriptChunk.from_text(chunk_id=str(i), session_id_hash=sid,
                                          provider=P, project=PFX, turn_start_index=i,
                                          turn_end_index=i, text=f"body-{i}")
        doc = dm.build_conversation_chunk_document(chunk=chunk)
        hashes.append(str(doc["content_hash"]))
        store.put(doc)
    store.put(dm.build_coverage_manifest_document(
        session_id_hash=sid, provider=P, project=PFX,
        conversation_chunk_count=count, tool_evidence_bundle_count=0,
        conversation_content_hashes=hashes, tool_evidence_coverage_hashes=[],
        project_authority={"project": PFX, "ambiguous": False,
                           "eligible_for_retirement": True}))
    return sid


def test_runner_claims_and_completes_each_batch(tmp_path, monkeypatch):
    from agent_knowledge.llm_brain_core import bulk_stream_runner as runner
    store = InMemoryCouchDBSourceStore()
    _seed(store, 3)
    calls = {"claim": [], "complete": []}
    original_claim = PgChunkState.claim_many
    original_complete = PgChunkState.complete_many

    def claim_many(self, keys, **kwargs):
        calls["claim"].append(list(keys))
        return original_claim(self, keys, **kwargs)

    def complete_many(self, entries, **kwargs):
        calls["complete"].append([e["key"] for e in entries])
        return original_complete(self, entries, **kwargs)

    monkeypatch.setattr(PgChunkState, "claim_many", claim_many)
    monkeypatch.setattr(PgChunkState, "complete_many", complete_many)
    monkeypatch.setattr(runner, "_select_sessions_bounded", lambda *a, **k: type(
        "Sel", (), {"sessions": [{"session_id_hash":
                   dm.build_session_id_hash(P, PFX + "runner"), "project": PFX}]})())
    report = run_streamed_full_input(
        ledger_path=tmp_path / "ledger.db", source_store=store,
        extractor=_Extractor(), writer=_Writer(), full_input_state=_state(),
        policy_version=PFX + "v1", project=PFX, limit=1, batch_size=1,
    )
    assert report["status"] == "ok"
    assert len(calls["claim"]) == 3
    assert all(len(batch) == 1 for batch in calls["claim"])
    assert len(calls["complete"]) == 3
