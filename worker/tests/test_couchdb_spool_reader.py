"""Private spool reader: real CouchDB, bounded bytes, full UTF-8/hash coverage."""

from __future__ import annotations

import base64
import hashlib
import os
import uuid

import httpx
import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
from agent_knowledge.couchdb_source.spool_reader import SpooledText
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

COUCH_URL = os.environ.get("TRIAL_COUCH_URL", "")
COUCH_USER = os.environ.get("TRIAL_COUCH_USER", "")
COUCH_PASSWORD = os.environ.get("TRIAL_COUCH_PASSWORD", "")
pytestmark = pytest.mark.skipif(not all((COUCH_URL, COUCH_USER, COUCH_PASSWORD)),
                                reason="isolated CouchDB required")

BODIES = [
    "가" * 300000,
    "😀" * 300000,
    'quote:" backslash:\\\n' + "한" * 300000,
]


@pytest.mark.parametrize("body", BODIES)
def test_spool_full_coverage(body):
    auth = "Basic " + base64.b64encode(f"{COUCH_USER}:{COUCH_PASSWORD}".encode()).decode()
    db = "spool" + uuid.uuid4().hex[:10]
    assert httpx.put(f"{COUCH_URL}/{db}", headers={"Authorization": auth}, timeout=10).status_code in (201, 202, 412)
    store = CouchDBHttpSourceStore(base_url=COUCH_URL, db=db, auth_header=auth)
    provider = "synthetic-provider"
    sid = dm.build_session_id_hash(provider, "spool-" + uuid.uuid4().hex)
    store.put(dm.build_transcript_session_document(session=TranscriptSession(
        session_id_hash=sid, provider=provider, project="spool",
        started_at="2026-01-01T00:00:00Z",
    )))
    chunk = TranscriptChunk.from_text(
        chunk_id="chunk_0", session_id_hash=sid, provider=provider, project="spool",
        turn_start_index=0, turn_end_index=0, text=body,
    )
    doc = dm.build_conversation_chunk_document(chunk=chunk)
    stored = store.put(doc)
    expected = str(doc["content_hash"])
    spooled = store.open_document_spool(stored.doc_id, expected_hash=expected)
    try:
        pages = list(SpooledText(spooled).iter_pages())
        joined = "".join(pages).encode("utf-8")
        assert "".join(pages) == body
        assert hashlib.sha256(joined).hexdigest() == expected.removeprefix("sha256:")
        assert spooled.byte_length == len(joined)
        assert spooled.metadata["_id"] == stored.doc_id
    finally:
        spooled.close()
        assert not spooled.path.exists()
