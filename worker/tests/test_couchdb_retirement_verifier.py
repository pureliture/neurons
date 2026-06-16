from __future__ import annotations

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.retirement_verifier import (
    SessionExpectation,
    verify_retirement,
    verify_session_retirement,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.couchdb_source.tool_evidence_bundler import store_tool_evidence_bundles
from agent_knowledge.couchdb_source.session_memory_materializer import (
    update_coverage_with_tool_evidence,
)
from agent_knowledge.session_memory.transcript_model import (
    ToolEvidenceSummaryRecord,
    TranscriptChunk,
    TranscriptSession,
)


def _sid(seed="sess-1") -> str:
    return dm.build_session_id_hash("codex", seed)


def _record(index, sid) -> ToolEvidenceSummaryRecord:
    return ToolEvidenceSummaryRecord(
        session_id_hash=sid,
        provider="codex",
        project="neurons",
        category="test_result",
        outcome="pass",
        tool_name="bash",
        command_summary="uv run pytest -q",
        redacted_summary="12 passed",
        evidence_index=index,
    )


def _seed_full_session(store, *, seed="sess-1", eligible=True):
    sid = _sid(seed)
    store.put(
        dm.build_transcript_session_document(
            session=TranscriptSession(
                session_id_hash=sid, provider="codex", project="neurons", started_at="2026-06-17T01:00:00Z"
            )
        )
    )
    conv_hashes = []
    for i, text in enumerate(("user asked", "assistant replied")):
        chunk = TranscriptChunk.from_text(
            chunk_id=f"chunk_{seed}_{i:02d}",
            session_id_hash=sid,
            provider="codex",
            project="neurons",
            turn_start_index=i,
            turn_end_index=i,
            text=text,
        )
        doc = dm.build_conversation_chunk_document(chunk=chunk)
        store.put(doc)
        conv_hashes.append(doc["content_hash"])
    store.put(
        dm.build_coverage_manifest_document(
            session_id_hash=sid,
            provider="codex",
            project="neurons",
            conversation_chunk_count=2,
            tool_evidence_bundle_count=0,
            conversation_content_hashes=conv_hashes,
            tool_evidence_coverage_hashes=[],
            project_authority={
                "project": "neurons",
                "ambiguous": not eligible,
                "eligible_for_retirement": eligible,
            },
        )
    )
    store_tool_evidence_bundles([_record(0, sid)], store=store)
    update_coverage_with_tool_evidence(session_id_hash=sid, store=store)
    return sid


def test_all_three_gates_pass_is_ready():
    store = InMemoryCouchDBSourceStore()
    sid = _seed_full_session(store)
    verdict = verify_session_retirement(
        expectation=SessionExpectation(
            session_id_hash=sid,
            expected_conversation_chunks=2,
            expected_tool_evidence_bundles=1,
            recall_smoke_passed=True,
        ),
        store=store,
    )
    assert verdict.coverage_pass and verdict.rebuild_pass and verdict.recall_pass
    assert verdict.eligible and verdict.ready


def test_recall_not_run_blocks_even_if_coverage_and_rebuild_pass():
    store = InMemoryCouchDBSourceStore()
    sid = _seed_full_session(store)
    verdict = verify_session_retirement(
        expectation=SessionExpectation(
            session_id_hash=sid,
            expected_conversation_chunks=2,
            expected_tool_evidence_bundles=1,
            recall_smoke_passed=None,  # not run
        ),
        store=store,
    )
    assert verdict.coverage_pass and verdict.rebuild_pass
    assert verdict.recall_pass is False
    assert verdict.ready is False
    assert "recall_smoke_not_run" in verdict.notes


def test_coverage_shortfall_fails_coverage_gate():
    store = InMemoryCouchDBSourceStore()
    sid = _seed_full_session(store)
    verdict = verify_session_retirement(
        expectation=SessionExpectation(
            session_id_hash=sid,
            expected_conversation_chunks=5,  # expects more than stored
            expected_tool_evidence_bundles=1,
            recall_smoke_passed=True,
        ),
        store=store,
    )
    assert verdict.coverage_pass is False
    assert verdict.ready is False
    assert "conversation_chunk_shortfall" in verdict.notes


def test_ambiguous_session_is_excluded():
    store = InMemoryCouchDBSourceStore()
    sid = _seed_full_session(store, eligible=False)
    verdict = verify_session_retirement(
        expectation=SessionExpectation(
            session_id_hash=sid,
            expected_conversation_chunks=2,
            expected_tool_evidence_bundles=1,
            recall_smoke_passed=True,
        ),
        store=store,
    )
    assert verdict.eligible is False
    assert verdict.ready is False


def test_aggregate_ready_requires_every_eligible_session():
    store = InMemoryCouchDBSourceStore()
    a = _seed_full_session(store, seed="a")
    b = _seed_full_session(store, seed="b")
    expectations = [
        SessionExpectation(session_id_hash=a, expected_conversation_chunks=2, expected_tool_evidence_bundles=1, recall_smoke_passed=True),
        SessionExpectation(session_id_hash=b, expected_conversation_chunks=2, expected_tool_evidence_bundles=1, recall_smoke_passed=False),
    ]
    report = verify_retirement(expectations=expectations, store=store)
    assert report.ready is False  # b's recall failed
    assert report.recall_pass is False
    assert report.coverage_pass is True


def test_aggregate_ready_when_all_eligible_pass():
    store = InMemoryCouchDBSourceStore()
    a = _seed_full_session(store, seed="a")
    b = _seed_full_session(store, seed="b")
    expectations = [
        SessionExpectation(session_id_hash=a, expected_conversation_chunks=2, expected_tool_evidence_bundles=1, recall_smoke_passed=True),
        SessionExpectation(session_id_hash=b, expected_conversation_chunks=2, expected_tool_evidence_bundles=1, recall_smoke_passed=True),
    ]
    report = verify_retirement(expectations=expectations, store=store)
    assert report.ready is True
    assert "human-gated" in report.live_action_required


def test_empty_eligible_set_is_not_ready():
    store = InMemoryCouchDBSourceStore()
    sid = _seed_full_session(store, eligible=False)
    report = verify_retirement(
        expectations=[
            SessionExpectation(session_id_hash=sid, expected_conversation_chunks=2, expected_tool_evidence_bundles=1, recall_smoke_passed=True)
        ],
        store=store,
    )
    assert report.ready is False
    assert sid in report.excluded_sessions
    assert "no_eligible_sessions" in report.notes
