"""Bounded synthetic contract for independent full-input expected-byte accounting."""

from __future__ import annotations

import uuid

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_revision import SourceRevisionCurrentnessLimitError
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core import bulk_stream_runner as runner_mod
from agent_knowledge.llm_brain_core.bulk_semantic import (
    BulkSemanticEntity,
    BulkSemanticExtractionResult,
    BulkSemanticSessionResult,
    BulkSemanticWriteReport,
)
from agent_knowledge.llm_brain_core.bulk_source_validation import plan_validated_source
from agent_knowledge.llm_brain_core.bulk_stream_runner import run_streamed_full_input
from agent_knowledge.llm_brain_core.bulk_streaming import planned_chunk_key
from agent_knowledge.llm_brain_core.runtime import session_source_revision_from_couchdb_source
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

P = "synthetic-provider"
PFX = uuid.uuid4().hex[:8] + "-"


class _Extractor:
    model = "free-model"
    provider = P
    endpoint_class = "approved_free"
    price_status = "free_confirmed"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def extract(self, batch):
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic extraction failure")
        return BulkSemanticExtractionResult(tuple(
            BulkSemanticSessionResult(
                session_key=item.session_key,
                entities=(BulkSemanticEntity(name=PFX + "E", type="Concept", summary="s"),),
            )
            for item in batch
        ))


class _Writer:
    def __init__(self, *, omit=()) -> None:
        self.omit = set(omit)

    def write_batch(self, inputs, extraction, *, allow_empty_sessions=False):
        receipts = {
            item.session_key: "receipt:" + item.session_key
            for item in inputs if item.session_key not in self.omit
        }
        return BulkSemanticWriteReport(
            projected=len(inputs), entities_written=len(inputs), relations_written=0,
            chunk_receipts=receipts,
        )


class _State:
    def __init__(self, *, complete=()) -> None:
        self.complete = set(complete)

    def register_plan(self, **kwargs):
        return None
    def is_complete(self, key):
        return key in self.complete

    def is_complete_many(self, keys):
        return {key for key in keys if key in self.complete}

    def is_session_complete(self, *, chunk_keys, **kwargs):
        return bool(chunk_keys) and all(key in self.complete for key in chunk_keys)

    def claim_many(self, keys, owner):
        return {key: 1 for key in keys}

    def complete_many(self, entries, owner):
        for entry in entries:
            self.complete.add(entry["key"])

    def supersede_keys(self, keys):
        return None


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


def _plan_keys(store, sid):
    source_hash = session_source_revision_from_couchdb_source(
        session_id_hash=sid, source_store=store)
    plan = plan_validated_source(store, session_id_hash=sid,
                                 expected_source_hash=source_hash)
    return [planned_chunk_key(plan, member) for member in plan.members]


def _run(tmp_path, monkeypatch, store, project, sid, *, writer=None, extractor=None, state=None):
    monkeypatch.setattr(runner_mod, "_pg_authority_by_hash", lambda *a, **k: {})
    return run_streamed_full_input(
        ledger_path=tmp_path / "ledger.db", source_store=store,
        extractor=extractor or _Extractor(), writer=writer or _Writer(),
        full_input_state=state or _State(), policy_version=PFX + "v1", project=project,
        limit=1, batch_size=16,
    )


@pytest.mark.parametrize("omit_index", [0, 1, 2, 3], ids=["front", "middle", "end", "evidence"])
def test_omitted_planned_member_is_detected_with_independent_expected_bytes(
    tmp_path, monkeypatch, omit_index,
):
    store = InMemoryCouchDBSourceStore()
    project = PFX + f"omit{omit_index}"
    sid = _seed(store, project, ["FRONT", "MIDDLE", "END"], evidence=1)
    keys = _plan_keys(store, sid)
    report = _run(tmp_path, monkeypatch, store, project, sid,
                  writer=_Writer(omit={keys[omit_index]}))

    assert report["expected_total_bytes"] > 0
    assert report["expected_full_bytes"] == (
        report["expected_total_bytes"] + report["expected_separator_bytes"]
    )
    assert len(report["expected_ordered_hashes"]) == 4
    assert report["model_received_ordered_hashes"] != report["expected_ordered_hashes"]
    assert report["hash_order_match"] is False
    assert report["stream_coverage_complete"] is False
    assert report["failed_chunks"] == 1
    assert report["failed_sessions"] == 0


def test_source_independent_expected_bytes_do_not_shrink_after_partial_failure(tmp_path, monkeypatch):
    store = InMemoryCouchDBSourceStore()
    project = PFX + "partial"
    sid = _seed(store, project, ["FRONT", "MIDDLE", "END"], evidence=1)
    baseline = _run(tmp_path, monkeypatch, store, project, sid)
    failed = _run(tmp_path, monkeypatch, store, project, sid, extractor=_Extractor(fail=True))

    assert failed["expected_total_bytes"] == baseline["expected_total_bytes"]
    assert failed["expected_ordered_hashes"] == baseline["expected_ordered_hashes"]
    assert failed["failed_sessions"] == 1
    assert failed["stream_coverage_complete"] is False


def test_deferred_session_is_separate_from_failure(tmp_path, monkeypatch):
    store = InMemoryCouchDBSourceStore()
    project = PFX + "defer"
    sid = _seed(store, project, ["FRONT"])

    def _defer(**kwargs):
        raise SourceRevisionCurrentnessLimitError("synthetic cap")

    monkeypatch.setattr(runner_mod, "session_source_revision_from_couchdb_source", _defer)
    report = _run(tmp_path, monkeypatch, store, project, sid)

    assert report["deferred_sessions"] == 1
    assert report["failed_sessions"] == 0
    assert report["failed_chunks"] == 0
    assert report["status"] == "partial"


def test_resumed_chunks_count_separately_and_remain_coverage_complete(tmp_path, monkeypatch):
    store = InMemoryCouchDBSourceStore()
    project = PFX + "resume"
    sid = _seed(store, project, ["FRONT", "END"], evidence=1)
    keys = _plan_keys(store, sid)
    report = _run(tmp_path, monkeypatch, store, project, sid, state=_State(complete={keys[0]}))

    assert report["resumed_chunks"] == 1
    assert report["skipped_resumed"] == 0
    assert report["verified_ordered_hashes"] == report["expected_ordered_hashes"]
    assert report["stream_coverage_complete"] is True


def test_empty_source_has_zero_expected_bytes_and_is_complete(tmp_path, monkeypatch):
    store = InMemoryCouchDBSourceStore()
    project = PFX + "empty"
    sid = _seed(store, project, [], evidence=0)
    report = _run(tmp_path, monkeypatch, store, project, sid)

    assert report["expected_total_bytes"] == 0
    assert report["expected_ordered_hashes"] == []
    assert report["hash_order_match"] is True
    assert report["stream_coverage_complete"] is True


def test_zero_byte_members_are_excluded_from_hash_order_contract(tmp_path, monkeypatch):
    from agent_knowledge.llm_brain_core.bulk_streaming import expected_source_bytes

    store = InMemoryCouchDBSourceStore()
    project = PFX + "zero"
    sid = _seed(store, project, ["", "END"], evidence=0)
    source_hash = session_source_revision_from_couchdb_source(
        session_id_hash=sid, source_store=store)
    plan = plan_validated_source(store, session_id_hash=sid, expected_source_hash=source_hash)
    expectation = expected_source_bytes(store, plan)
    report = _run(tmp_path, monkeypatch, store, project, sid)

    assert expectation["zero_byte_members"] == 1
    assert len(expectation["ordered"]) == 2
    assert len(expectation["ordered"]) - len(report["expected_ordered_hashes"]) == 1
    assert report["zero_byte_members"] == 1
    assert report["hash_order_match"] is True
    assert report["stream_coverage_complete"] is True
