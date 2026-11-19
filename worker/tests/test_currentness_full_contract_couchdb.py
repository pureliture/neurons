"""d45aa475 currentness contract against real CouchDB (synthetic data only)."""

from __future__ import annotations

import base64
import os
import uuid
from urllib.parse import quote

import httpx
import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
from agent_knowledge.couchdb_source.session_memory_materializer import materialize_session_memory
from agent_knowledge.couchdb_source.source_revision import (
    SourceRevisionCurrentnessLimitError,
    SourceRevisionResolutionError,
    activate_source_revision,
    resolve_active_source_revision,
    resolve_source_hash_for_currentness,
)
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

COUCH_URL = os.environ.get("TRIAL_COUCH_URL", "")
COUCH_USER = os.environ.get("TRIAL_COUCH_USER", "")
COUCH_PASSWORD = os.environ.get("TRIAL_COUCH_PASSWORD", "")
pytestmark = pytest.mark.skipif(not all((COUCH_URL, COUCH_USER, COUCH_PASSWORD)),
                                reason="isolated CouchDB required")


def _auth():
    return "Basic " + base64.b64encode(f"{COUCH_USER}:{COUCH_PASSWORD}".encode()).decode()


def _seed(*, active: bool):
    auth = _auth()
    db = "contract" + uuid.uuid4().hex[:10]
    httpx.put(f"{COUCH_URL}/{db}", headers={"Authorization": auth}, timeout=30).raise_for_status()
    store = CouchDBHttpSourceStore(base_url=COUCH_URL, db=db, auth_header=auth)
    project = "contract-" + uuid.uuid4().hex[:10]
    provider = "synthetic-provider"
    sid = dm.build_session_id_hash(provider, uuid.uuid4().hex)
    store.put(dm.build_transcript_session_document(session=TranscriptSession(
        session_id_hash=sid, provider=provider, project=project,
        started_at="2026-01-01T00:00:00Z",
    )))
    chunk = TranscriptChunk.from_text(
        chunk_id="chunk_0", session_id_hash=sid, provider=provider, project=project,
        turn_start_index=0, turn_end_index=0, text="synthetic contract body",
    )
    doc = dm.build_conversation_chunk_document(chunk=chunk)
    stored = store.put(doc)
    store.put(dm.build_coverage_manifest_document(
        session_id_hash=sid, provider=provider, project=project,
        conversation_chunk_count=1, tool_evidence_bundle_count=0,
        conversation_content_hashes=[str(doc["content_hash"])],
        tool_evidence_coverage_hashes=[],
        project_authority={"project": project, "ambiguous": False,
                           "eligible_for_retirement": True},
    ))
    if active:
        activate_source_revision(store=store, session_id_hash=sid)
        snapshot = next(
            doc for doc in store.find_by_session(session_id_hash=sid)
            if str(doc.get("doc_type") or "") == dm.SourceDocType.CONVERSATION_CHUNK
            and ":snapshot:" in str(doc.get("_id") or "")
        )
        return store, auth, sid, dict(snapshot), str(snapshot["_rev"])
    return store, auth, sid, dict(doc), stored.rev


def _raw_put(store, auth: str, doc: dict, rev: str, **changes):
    payload = {**doc, "_rev": rev, **changes}
    return httpx.put(
        f"{COUCH_URL}/{store.db}/{quote(str(doc['_id']), safe='')}",
        headers={"Authorization": auth}, json=payload, timeout=30,
    )


def _hash(store, sid: str) -> str:
    return resolve_source_hash_for_currentness(session_id_hash=sid, store=store)


@pytest.mark.parametrize("change", [
    {"body": "tampered without hash change"},
    {"redaction_version": "tampered-redaction", "parser_version": "p9", "chunker_version": "c9"},
])
def test_active_full_descriptor_detects_body_or_metadata_change(change):
    store, auth, sid, doc, rev = _seed(active=True)
    try:
        baseline = _hash(store, sid)
        assert _raw_put(store, auth, doc, rev, **change).status_code in (201, 202)
        with pytest.raises(SourceRevisionResolutionError):
            resolve_active_source_revision(session_id_hash=sid, store=store)
        with pytest.raises(SourceRevisionResolutionError):
            materialize_session_memory(session_id_hash=sid, store=store)
        assert baseline
    finally:
        httpx.delete(f"{COUCH_URL}/{store.db}", headers={"Authorization": auth}, timeout=30)


def test_legacy_materialization_detects_body_change_without_active_pointer():
    store, auth, sid, doc, rev = _seed(active=False)
    try:
        baseline = _hash(store, sid)
        assert _raw_put(store, auth, doc, rev, body="tampered").status_code in (201, 202)
        with pytest.raises(SourceRevisionResolutionError):
            materialize_session_memory(session_id_hash=sid, store=store)
        assert baseline
    finally:
        httpx.delete(f"{COUCH_URL}/{store.db}", headers={"Authorization": auth}, timeout=30)


def test_stale_revision_write_is_rejected_and_currentness_unchanged():
    store, auth, sid, doc, rev = _seed(active=True)
    try:
        baseline = _hash(store, sid)
        assert _raw_put(store, auth, doc, "1-stale-revision", body="tampered").status_code == 409
        assert _hash(store, sid) == baseline
    finally:
        httpx.delete(f"{COUCH_URL}/{store.db}", headers={"Authorization": auth}, timeout=30)


def test_canonical_stream_budget_overrun_defers_session(monkeypatch):
    store, auth, sid, _doc, _rev = _seed(active=True)
    try:
        monkeypatch.setenv("NEURON_SOURCE_CURRENTNESS_STREAM_SPOOL_BYTES", "1")
        with pytest.raises(SourceRevisionCurrentnessLimitError) as exc:
            _hash(store, sid)
        assert exc.value.reason_code == "source_revision_stream_budget_exceeded"
    finally:
        httpx.delete(f"{COUCH_URL}/{store.db}", headers={"Authorization": auth}, timeout=30)


def test_legacy_currentness_detects_body_tamper_without_active_pointer():
    store, auth, sid, doc, rev = _seed(active=False)
    try:
        baseline = _hash(store, sid)
        response = _raw_put(store, auth, doc, rev, body="tampered without hash change")
        response.raise_for_status()
        with pytest.raises(SourceRevisionResolutionError):
            _hash(store, sid)
        assert baseline
    finally:
        httpx.delete(f"{COUCH_URL}/{store.db}", headers={"Authorization": auth}, timeout=30)
