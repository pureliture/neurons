"""Active revision mutation/missing/multi-session counterexamples."""

from __future__ import annotations

import os
import uuid

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_revision import activate_source_revision
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core.bulk_full_input import SourceInvalid
from agent_knowledge.llm_brain_core.runtime import session_source_revision_from_couchdb_source
from agent_knowledge.llm_brain_core.bulk_source_validation import plan_validated_source
from agent_knowledge.llm_brain_core.runtime import session_source_revision_from_couchdb_source
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

pytestmark = pytest.mark.skipif(not os.environ.get("TRIAL_PG_DSN"), reason="no isolated PG")
P = "synthetic-provider"
PFX = uuid.uuid4().hex[:8] + "-"


def _seed(store, name, bodies, active):
    sid = dm.build_session_id_hash(P, PFX + name)
    store.put(dm.build_transcript_session_document(
        session=TranscriptSession(session_id_hash=sid, provider=P, project=PFX,
                                  started_at="2026-01-01T00:00:00Z")))
    hashes = []
    for index, body in enumerate(bodies):
        chunk = TranscriptChunk.from_text(chunk_id=f"{name}_{index}", session_id_hash=sid,
                                          provider=P, project=PFX, turn_start_index=index,
                                          turn_end_index=index, text=body)
        doc = dm.build_conversation_chunk_document(chunk=chunk)
        hashes.append(str(doc["content_hash"]))
        store.put(doc)
    store.put(dm.build_coverage_manifest_document(
        session_id_hash=sid, provider=P, project=PFX,
        conversation_chunk_count=len(bodies), tool_evidence_bundle_count=0,
        conversation_content_hashes=hashes, tool_evidence_coverage_hashes=[],
        project_authority={"project": PFX, "ambiguous": False,
                           "eligible_for_retirement": True}))
    if active:
        activate_source_revision(store=store, session_id_hash=sid)
    return sid


def test_active_missing_member_fails_closed():
    store = InMemoryCouchDBSourceStore()
    sid = _seed(store, "missing", ["a", "b"], True)
    current = session_source_revision_from_couchdb_source(session_id_hash=sid, source_store=store)
    members = store.find_by_session(session_id_hash=sid,
                                    doc_type=dm.SourceDocType.SOURCE_REVISION_MEMBER)
    store._docs.pop(members[0]["_id"], None)
    with pytest.raises(Exception):
        plan_validated_source(store, session_id_hash=sid, expected_source_hash=current)


def test_active_mutated_member_body_fails_closed():
    store = InMemoryCouchDBSourceStore()
    sid = _seed(store, "mutated", ["a", "b"], True)
    current = session_source_revision_from_couchdb_source(session_id_hash=sid, source_store=store)
    members = store.find_by_session(session_id_hash=sid,
                                    doc_type=dm.SourceDocType.SOURCE_REVISION_MEMBER)
    doc_id = members[0]["_id"]
    from agent_knowledge.llm_brain_core.bulk_streaming import iter_planned_chunks
    plan = plan_validated_source(store, session_id_hash=sid, expected_source_hash=current)
    body_doc_id = plan.members[0].doc_id
    store._docs[body_doc_id]["body"] = store._docs[body_doc_id].get("body", "") + " MUTATED"
    with pytest.raises(SourceInvalid):
        list(iter_planned_chunks(store, plan))


def test_active_and_legacy_members_are_distinct_sets():
    store = InMemoryCouchDBSourceStore()
    active_sid = _seed(store, "active", ["a"], True)
    _seed(store, "legacy", ["b"], False)
    current = session_source_revision_from_couchdb_source(
        session_id_hash=active_sid, source_store=store
    )
    active_plan = plan_validated_source(
        store, session_id_hash=active_sid, expected_source_hash=current
    )
    assert len(active_plan.members) >= 1
    assert all(member.lane in {"conversation", "evidence"} for member in active_plan.members)
