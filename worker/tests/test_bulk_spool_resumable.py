"""Resumable spool: explicit budget, owner isolation, and completion guards."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from neo4j import GraphDatabase

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
from agent_knowledge.couchdb_source.source_revision import activate_source_revision
from agent_knowledge.couchdb_source.spool_reader import (
    SpoolBusy,
    acquire_spool,
    cleanup_stale_spools,
    spool_dir_for,
    spool_base,
)
from agent_knowledge.llm_brain_core.bulk_streaming import safe_reason
from agent_knowledge.llm_brain_core import bulk_authority, bulk_semantic_cli as cli
from agent_knowledge.llm_brain_core import couchdb_projection_cli
from agent_knowledge.llm_brain_core.bulk_full_input import shadow_group_ids
from agent_knowledge.llm_brain_core.bulk_semantic import (
    BulkSemanticEntity,
    BulkSemanticExtractionResult,
    BulkSemanticSessionResult,
)
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

COUCH_URL = os.environ.get("TRIAL_COUCH_URL", "")
COUCH_USER = os.environ.get("TRIAL_COUCH_USER", "")
COUCH_PASSWORD = os.environ.get("TRIAL_COUCH_PASSWORD", "")
PG_DSN = os.environ.get("TRIAL_PG_DSN", "")
NEO4J_URI = os.environ.get("TRIAL_NEO4J_URI", "")
NEO4J_PASSWORD = os.environ.get("TRIAL_NEO4J_PASSWORD", "")
pytestmark = pytest.mark.skipif(not all((COUCH_URL, COUCH_USER, COUCH_PASSWORD)),
                                reason="isolated CouchDB required")

_UNIQUE = "PRIVATE_MARKER_" + uuid.uuid4().hex
_LARGE_BODY = _UNIQUE + "😀한A" * 2_200_000


class _Extractor:
    model = "free-model"
    provider = "synthetic-provider"
    endpoint_class = "approved_free"
    price_status = "free_confirmed"

    def __init__(self):
        self.coverage = {}

    def extract(self, batch):
        results = []
        for item in batch:
            text = item.text
            digest = hashlib.sha256()
            total = 0
            pages = text.iter_pages() if hasattr(text, "iter_pages") else [text]
            for page in pages:
                raw = page.encode("utf-8")
                digest.update(raw)
                total += len(raw)
            actual = "sha256:" + digest.hexdigest()
            self.coverage[item.session_key] = (total, actual)
            if actual != str(item.episode.payload.get("canonical_chunk_hash") or ""):
                raise AssertionError("coverage hash mismatch")
            results.append(BulkSemanticSessionResult(
                session_key=item.session_key,
                entities=(BulkSemanticEntity(name=f"Synthetic {item.session_key}",
                                             type="Concept", summary="synthetic"),),
                relations=(),
            ))
        return BulkSemanticExtractionResult(tuple(results))


def _auth():
    return "Basic " + base64.b64encode(f"{COUCH_USER}:{COUCH_PASSWORD}".encode()).decode()


def _seed(store, project: str, label: str, body: str) -> tuple[str, str]:
    provider = "synthetic-provider"
    sid = dm.build_session_id_hash(provider, label)
    store.put(dm.build_transcript_session_document(session=TranscriptSession(
        session_id_hash=sid, provider=provider, project=project,
        started_at="2026-01-01T00:00:00Z",
    )))
    chunk = TranscriptChunk.from_text(
        chunk_id="chunk_0", session_id_hash=sid, provider=provider, project=project,
        turn_start_index=0, turn_end_index=0, text=body,
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
    activate_source_revision(store=store, session_id_hash=sid)
    return str(stored.doc_id), str(doc["content_hash"])


def _new_store(db: str):
    return CouchDBHttpSourceStore(base_url=COUCH_URL, db=db, auth_header=_auth())


def test_large_document_budget_full_coverage_and_privacy(monkeypatch, tmp_path, capsys):
    auth = _auth()
    db = "bigspool" + uuid.uuid4().hex[:10]
    assert httpx.put(f"{COUCH_URL}/{db}", headers={"Authorization": auth}, timeout=10).status_code in (201, 202, 412)
    store = _new_store(db)
    project = "bigspool-" + uuid.uuid4().hex[:10]
    doc_id, content_hash = _seed(store, project, uuid.uuid4().hex, _LARGE_BODY)
    extractor = _Extractor()
    monkeypatch.setattr(cli, "_build_source_store", lambda **kwargs: store)
    monkeypatch.setattr(cli.OpenAICompatibleBulkSemanticExtractor, "from_env",
                        classmethod(lambda cls, **kwargs: extractor))
    monkeypatch.setenv("NEURON_TRIAL_MODE", "1")
    monkeypatch.setenv("TRIAL_PG_DSN", PG_DSN)
    monkeypatch.setenv("TRIAL_NEO4J_URI", NEO4J_URI)
    monkeypatch.setenv("TRIAL_NEO4J_PASSWORD", NEO4J_PASSWORD)
    monkeypatch.setenv("LLM_BRAIN_BULK_FREE_MODEL_ALLOWLIST", "free-model")
    monkeypatch.setenv("NEURON_SOURCE_CURRENTNESS_MAX_PAGE_BYTES", str(128 * 1024 * 1024))
    monkeypatch.setenv("NEURON_SOURCE_CURRENTNESS_MAX_COMPACT_BYTES", str(128 * 1024 * 1024))
    try:
        rc = cli.main([
            "--ledger", str(tmp_path / "ledger.db"), "--runtime-dir", str(tmp_path / "runtime"),
            "--project", project, "--limit", "1", "--stream-mode", "--trial-neo4j-writer",
            "--full-input-policy-version", "full-input.v1",
        ])
        output = capsys.readouterr()
        report = json.loads(output.out or "{}")
        assert rc == 0 and report["status"] == "ok", report
        assert report["full_input_chunks"] == 1
        assert extractor.coverage
        assert all(total > 16 * 1024 * 1024 and digest == content_hash
                   for total, digest in extractor.coverage.values())
        assert _UNIQUE not in json.dumps(report)
    finally:
        httpx.delete(f"{COUCH_URL}/{db}", headers={"Authorization": auth}, timeout=10)


def test_resume_reuses_complete_spool_without_http():
    auth = _auth()
    db = "resumespool" + uuid.uuid4().hex[:10]
    httpx.put(f"{COUCH_URL}/{db}", headers={"Authorization": auth}, timeout=10)
    store = _new_store(db)
    doc_id, content_hash = _seed(store, "resume-" + uuid.uuid4().hex[:10],
                                 uuid.uuid4().hex, "😀한" * 300000)
    worker = "resume-worker"
    first = store.open_document_spool(doc_id, expected_hash=content_hash, worker_id=worker)
    first_path = first.path
    first.close(retain=True)
    try:
        with patch("urllib.request.urlopen", side_effect=AssertionError("unexpected GET")):
            second = store.open_document_spool(doc_id, expected_hash=content_hash, worker_id=worker)
        assert second.path == first_path
        assert hashlib.sha256(b"".join(second.iter_bytes())).hexdigest() == content_hash.removeprefix("sha256:")
        second.close()
    finally:
        httpx.delete(f"{COUCH_URL}/{db}", headers={"Authorization": auth}, timeout=10)


def test_locked_spool_rejects_other_worker():
    doc_id = "doc-" + uuid.uuid4().hex
    expected = "sha256:" + hashlib.sha256(doc_id.encode()).hexdigest()
    directory, lease = acquire_spool(doc_id, expected, worker_id="owner")
    try:
        with pytest.raises(SpoolBusy):
            acquire_spool(doc_id, expected, worker_id="other")
    finally:
        lease.close()
        cleanup_stale_spools(current_worker_id="owner")


def test_fresh_active_worker_spool_is_not_cleaned():
    doc_id = "doc-" + uuid.uuid4().hex
    expected = "sha256:" + hashlib.sha256(doc_id.encode()).hexdigest()
    directory, lease = acquire_spool(doc_id, expected, worker_id="live")
    (directory / "manifest.json").write_text(json.dumps({
        "state": "partial", "pid": os.getpid(), "worker_id": "live",
    }))
    try:
        cleanup_stale_spools(current_worker_id="other")
        assert directory.exists()
    finally:
        lease.close()
        cleanup_stale_spools(current_worker_id="live")


def test_authority_fixed_timestamp_and_revalidation():
    seen = []

    def fake(self, *, project, batches, as_of, max_hashes=100):
        seen.append(([len(batch) for batch in batches], as_of))
        return {"by_hash": {}, "ambiguous_hashes": set()}

    with patch.object(bulk_authority.PgVectorStore, "find_authorized_by_content_hashes_snapshot", fake):
        result = bulk_authority.authorized_sources_by_hash(
            "dsn", "project", [f"sha256:{i:064x}" for i in range(101)]
        )
    assert result == {}
    assert seen[0][0] == [100, 1]
    assert seen[0][1] == seen[-1][1]


def test_nonstream_body_change_fails_before_model(monkeypatch, capsys):
    from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore

    store = InMemoryCouchDBSourceStore()
    project = "bodyguard-" + uuid.uuid4().hex[:10]
    provider = "synthetic-provider"
    sid = dm.build_session_id_hash(provider, project + "-session")
    store.put(dm.build_transcript_session_document(session=TranscriptSession(
        session_id_hash=sid, provider=provider, project=project,
        started_at="2026-01-01T00:00:00Z",
    )))
    chunk = TranscriptChunk.from_text(
        chunk_id="chunk_0", session_id_hash=sid, provider=provider, project=project,
        turn_start_index=0, turn_end_index=0, text="original body",
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
    store.put({**doc, "_rev": stored.doc_id, "body": "TAMPERED BODY"})
    extractor = _Extractor()
    monkeypatch.setattr(cli, "_build_source_store", lambda **kwargs: store)
    monkeypatch.setattr(cli.OpenAICompatibleBulkSemanticExtractor, "from_env",
                        classmethod(lambda cls, **kwargs: extractor))
    monkeypatch.setenv("NEURON_TRIAL_MODE", "1")
    rc = cli.main(["--ledger", ":memory:", "--project", project,
                   "--full-input-policy-version", "full-input.v1"])
    capsys.readouterr()
    assert rc != 0
    assert extractor.coverage == {}


def _approved_card(dsn: str, project: str, content_hash: str) -> str:
    memory_id = "mem_" + uuid.uuid4().hex[:16]
    envelope = {
        "memory_id": memory_id, "project": project, "card_type": "preference",
        "approval_state": "approved", "steward_proposal_kind": "",
        "lifecycle_state": "accepted", "authorization_status": "active",
        "currentness": "current", "content_hash": content_hash,
    }
    from agent_knowledge.postgres_store.pgvector_store import PgVectorStore
    store = PgVectorStore(dsn=dsn)
    with store._scope(write=True) as db:
        db.execute(
            "INSERT INTO memory_cards (memory_id, project, card_type, title, summary, "
            "lifecycle_state, authorization_status, currentness, content_hash, valid_from, valid_to, steward_envelope) "
            "VALUES (%s, %s, 'preference', 'synthetic', 'synthetic', "
            "'accepted', 'active', 'current', %s, %s, NULL, %s::jsonb)",
            [memory_id, project, content_hash, "2020-01-01T00:00:00+00:00", json.dumps(envelope)],
        )
    return memory_id


def test_revalidation_rejects_revoked_authority(isolated_pg_store):
    project = "revoke-" + uuid.uuid4().hex[:10]
    content_hash = "sha256:" + hashlib.sha256(project.encode()).hexdigest()
    memory_id = _approved_card(isolated_pg_store.dsn, project, content_hash)
    sources = [{"authority_memory_id": memory_id, "content_hash": content_hash}]
    assert bulk_authority.revalidate_authority_sources(isolated_pg_store.dsn, project, sources)
    with isolated_pg_store._scope(write=True) as db:
        db.execute("UPDATE memory_cards SET valid_to = %s WHERE memory_id = %s",
                   ["2020-01-02T00:00:00+00:00", memory_id])
    assert not bulk_authority.revalidate_authority_sources(isolated_pg_store.dsn, project, sources)


def test_spool_budget_exceeded_is_deferred(monkeypatch):
    auth = _auth()
    db = "budget" + uuid.uuid4().hex[:10]
    httpx.put(f"{COUCH_URL}/{db}", headers={"Authorization": auth}, timeout=10)
    store = _new_store(db)
    doc_id, content_hash = _seed(store, "budget-" + uuid.uuid4().hex[:10],
                                 uuid.uuid4().hex, "한" * 100000)
    monkeypatch.setenv("NEURON_BULK_SPOOL_BODY_BUDGET_BYTES", "1024")
    try:
        with pytest.raises(Exception) as exc:
            store.open_document_spool(doc_id, expected_hash=content_hash)
        assert safe_reason(exc.value) == "defer"
    finally:
        httpx.delete(f"{COUCH_URL}/{db}", headers={"Authorization": auth}, timeout=10)


def test_oversized_active_probe_does_not_drop_session(monkeypatch, tmp_path, capsys):
    """The drift probe must not dead-letter an over-budget active session."""
    auth = _auth()
    db = "probe" + uuid.uuid4().hex[:10]
    httpx.put(f"{COUCH_URL}/{db}", headers={"Authorization": auth}, timeout=10)
    store = _new_store(db)
    project = "probe-" + uuid.uuid4().hex[:10]
    large_label = uuid.uuid4().hex
    large_sid = dm.build_session_id_hash("synthetic-provider", large_label)
    _seed(store, project, large_label, "가" * 400000)
    _seed(store, project, uuid.uuid4().hex, "small session")
    original_active = couchdb_projection_cli.iter_active_source_hashes

    def _large_first(*args, **kwargs):
        hashes, invalid = original_active(*args, **kwargs)
        if large_sid not in hashes:
            return hashes, invalid
        ordered = {large_sid: hashes[large_sid]}
        ordered.update({key: value for key, value in hashes.items() if key != large_sid})
        return ordered, invalid

    extractor = _Extractor()
    monkeypatch.setattr(couchdb_projection_cli, "iter_active_source_hashes", _large_first)
    monkeypatch.setattr(cli, "_build_source_store", lambda **kwargs: store)
    monkeypatch.setattr(cli.OpenAICompatibleBulkSemanticExtractor, "from_env",
                        classmethod(lambda cls, **kwargs: extractor))
    monkeypatch.setenv("NEURON_TRIAL_MODE", "1")
    monkeypatch.setenv("TRIAL_PG_DSN", PG_DSN)
    monkeypatch.setenv("TRIAL_NEO4J_URI", NEO4J_URI)
    monkeypatch.setenv("TRIAL_NEO4J_PASSWORD", NEO4J_PASSWORD)
    monkeypatch.setenv("LLM_BRAIN_BULK_FREE_MODEL_ALLOWLIST", "free-model")
    monkeypatch.setenv("NEURON_BULK_SPOOL_BODY_BUDGET_BYTES", "1024")
    try:
        rc = cli.main([
            "--ledger", str(tmp_path / "ledger.db"), "--runtime-dir", str(tmp_path / "runtime"),
            "--project", project, "--limit", "2", "--stream-mode", "--trial-neo4j-writer",
            "--full-input-policy-version", "full-input.v1",
        ])
        captured = capsys.readouterr()
        report = json.loads(captured.out or "{}")
        assert rc == 1, json.dumps(report, sort_keys=True)
        assert report["status"] == "partial"
        assert report["processed"] == 2, json.dumps(report, sort_keys=True)
        assert report["failed"] == 0
        assert report["deferred"] == 1
        assert report["full_input_chunks"] == 1
    finally:
        httpx.delete(f"{COUCH_URL}/{db}", headers={"Authorization": auth}, timeout=10)


def test_oversized_session_does_not_block_other_session(monkeypatch, tmp_path, capsys):
    auth = _auth()
    db = "progress" + uuid.uuid4().hex[:10]
    httpx.put(f"{COUCH_URL}/{db}", headers={"Authorization": auth}, timeout=10)
    store = _new_store(db)
    project = "progress-" + uuid.uuid4().hex[:10]
    _seed(store, project, uuid.uuid4().hex, "가" * 400000)
    _seed(store, project, uuid.uuid4().hex, "small session")
    extractor = _Extractor()
    monkeypatch.setattr(cli, "_build_source_store", lambda **kwargs: store)
    monkeypatch.setattr(cli.OpenAICompatibleBulkSemanticExtractor, "from_env",
                        classmethod(lambda cls, **kwargs: extractor))
    monkeypatch.setenv("NEURON_TRIAL_MODE", "1")
    monkeypatch.setenv("TRIAL_PG_DSN", PG_DSN)
    monkeypatch.setenv("TRIAL_NEO4J_URI", NEO4J_URI)
    monkeypatch.setenv("TRIAL_NEO4J_PASSWORD", NEO4J_PASSWORD)
    monkeypatch.setenv("LLM_BRAIN_BULK_FREE_MODEL_ALLOWLIST", "free-model")
    monkeypatch.setenv("NEURON_BULK_SPOOL_BODY_BUDGET_BYTES", "1024")
    try:
        rc = cli.main([
            "--ledger", str(tmp_path / "ledger.db"), "--runtime-dir", str(tmp_path / "runtime"),
            "--project", project, "--limit", "2", "--stream-mode", "--trial-neo4j-writer",
            "--full-input-policy-version", "full-input.v1",
        ])
        report = json.loads(capsys.readouterr().out or "{}")
        assert report["processed"] == 2, json.dumps(report, sort_keys=True)
        assert report["full_input_chunks"] == 1
        assert extractor.coverage
    finally:
        httpx.delete(f"{COUCH_URL}/{db}", headers={"Authorization": auth}, timeout=10)
