"""Real isolated CouchDB HTTP read-path RSS proof (synthetic data only)."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import urllib.request
import uuid
from agent_knowledge.couchdb_source.source_revision import activate_source_revision
from pathlib import Path

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession
from agent_knowledge.llm_brain_core.runtime import _strip_chunk_metadata_header

pytestmark = pytest.mark.skipif(
    not (os.environ.get("TRIAL_COUCH_URL") and os.environ.get("TRIAL_COUCH_PASSWORD")),
    reason="no isolated CouchDB",
)
P = "synthetic-provider"
PFX = uuid.uuid4().hex[:8] + "-"
RSS_PROBE = Path(__file__).with_name("bulk_couch_rss_probe_helper.py")


def _auth():
    return "Basic " + base64.b64encode(
        f"admin:{os.environ['TRIAL_COUCH_PASSWORD']}".encode()
    ).decode()


def _create_db(name: str) -> None:
    CouchDBHttpSourceStore(
        base_url=os.environ["TRIAL_COUCH_URL"], db=name, auth_header=_auth()
    ).ensure_database()


def _drop_db(name: str) -> None:
    url = os.environ["TRIAL_COUCH_URL"].rstrip("/") + "/" + name
    request = urllib.request.Request(url, method="DELETE", headers={"Authorization": _auth()})
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def _seed(store, project, count, body_bytes, active=False):
    sid = dm.build_session_id_hash(P, project + "-session")
    store.put(dm.build_transcript_session_document(
        session=TranscriptSession(session_id_hash=sid, provider=P, project=project,
                                  started_at="2026-01-01T00:00:00Z")))
    hashes = []
    for index in range(count):
        marker = ("[FIRST]" if index == 0 else
                  "[MIDDLE]" if index == count // 2 else
                  "[LAST]" if index == count - 1 else "")
        body = (marker + "한글" + "A" * body_bytes).encode("utf-8")[:body_bytes].decode()
        chunk = TranscriptChunk.from_text(
            chunk_id=f"chunk_{index:04d}", session_id_hash=sid, provider=P, project=project,
            turn_start_index=index, turn_end_index=index, text=body,
        )
        doc = dm.build_conversation_chunk_document(chunk=chunk)
        hashes.append(str(doc["content_hash"]))
        store.put(doc)
    store.put(dm.build_tool_evidence_bundle_document(
        session_id_hash=sid, provider=P, project=project, part_index=0, part_count=1,
        evidence_index_start=0, evidence_index_end=1, record_content_hashes=[],
        body="synthetic evidence body"))
    store.put(dm.build_coverage_manifest_document(
        session_id_hash=sid, provider=P, project=project,
        conversation_chunk_count=count, tool_evidence_bundle_count=1,
        conversation_content_hashes=hashes, tool_evidence_coverage_hashes=[],
        project_authority={"project": project, "ambiguous": False,
                           "eligible_for_retirement": True}))
    if active:
        activate_source_revision(store=store, session_id_hash=sid)
    return sid


def _run_probe(project, db):
    env = os.environ.copy()
    if env.get("TRIAL_PG_DSN"):
        from agent_knowledge.postgres_store.pgvector_store import PgVectorStore

        PgVectorStore(dsn=env["TRIAL_PG_DSN"]).execute_ddl()

    env["TRIAL_COUCH_DB"] = db
    proc = subprocess.run(
        [sys.executable, str(RSS_PROBE), project],
        env=env, capture_output=True, text=True, timeout=3600,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_real_couchdb_http_active_branch_coverage():
    peaks = {}
    cases = (("8m", 512, 16 * 1024), ("200m", 4000, 50 * 1024))
    selected = os.environ.get("BULK_RSS_CASE")
    if selected:
        cases = tuple(case for case in cases if case[0] == selected)
        assert cases, "unknown BULK_RSS_CASE"
    for label, count, body_bytes in cases:
        db = "db" + PFX + label
        _create_db(db)
        try:
            store = CouchDBHttpSourceStore(
                base_url=os.environ["TRIAL_COUCH_URL"], db=db, auth_header=_auth()
            )
            project = PFX + label
            _seed(store, project, count, body_bytes, active=True)
            expected_bytes = sum(
                len(_strip_chunk_metadata_header(str(doc.get("body") or "")).encode("utf-8"))
                for doc in store.find_by_session(
                    session_id_hash=dm.build_session_id_hash(P, project + "-session"),
                    doc_type=dm.SourceDocType.CONVERSATION_CHUNK,
                )
            )
            result = _run_probe(project, db)
            report = result["report"]
            assert report["status"] == "ok", json.dumps(report, sort_keys=True)
            assert report["processed"] > 0
            print("COUCH_EXPECTED_BYTES", report["expected_total_bytes"])
            print("COUCH_STREAM_TOTAL_BYTES", report["stream_total_bytes"])
            print("COUCH_EXPECTED_FULL_BYTES", report["expected_full_bytes"])
            print("COUCH_SEPARATOR_BYTES", report["expected_separator_bytes"])
            assert report["stream_total_bytes"] == report["expected_total_bytes"] > 0
            flags = result["model_flags"]
            assert flags["texts"] > 0
            assert flags["first"] and flags["middle"] and flags["last"]
            assert flags["evidence"] >= 1
            assert report["writer_receipt_chunks"] == count + 1
            received = set(report["writer_receipt_hashes"])
            expected = set(result["expected_hashes"])
            assert received == expected, (
                len(received), len(expected), len(received - expected), len(expected - received)
            )
            peaks[label] = result["peak_rss"]
            print("COUCH_ACTIVE_COUNTS", label, result["counts"], "elapsed_ms", report["elapsed_ms"])
        finally:
            _drop_db(db)
    print("COUCH_ACTIVE_PEAKS", peaks)
    assert all(label in peaks for label, _count, _body in cases)
