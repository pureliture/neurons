"""Synthetic CouchDB oversized body -> PG authority -> Neo4j -> reader E2E."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import resource
import uuid

import httpx
import pytest
from neo4j import GraphDatabase

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
from agent_knowledge.couchdb_source.source_revision import activate_source_revision
from agent_knowledge.llm_brain_core import bulk_semantic_cli as cli
from agent_knowledge.llm_brain_core.bulk_full_input import shadow_group_ids
from agent_knowledge.llm_brain_core.bulk_full_input_reader import FullInputReader, PgAuthorityCardStore
from agent_knowledge.llm_brain_core.bulk_semantic import (
    BulkSemanticEntity,
    BulkSemanticExtractionResult,
    BulkSemanticSessionResult,
)
from agent_knowledge.couchdb_source.spool_reader import SpooledText
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

PG_DSN = os.environ.get("LBRAIN_TEST_PG_DSN", "")
NEO4J_URI = os.environ.get("TRIAL_NEO4J_URI", "")
NEO4J_PASSWORD = os.environ.get("TRIAL_NEO4J_PASSWORD", "")
COUCH_URL = os.environ.get("TRIAL_COUCH_URL", "")
COUCH_USER = os.environ.get("TRIAL_COUCH_USER", "")
COUCH_PASSWORD = os.environ.get("TRIAL_COUCH_PASSWORD", "")
pytestmark = pytest.mark.skipif(not all((PG_DSN, NEO4J_URI, NEO4J_PASSWORD, COUCH_URL,
                                         COUCH_USER, COUCH_PASSWORD)),
                                reason="isolated loopback DBs required")


class _Extractor:
    model = "free-model"
    provider = "synthetic-provider"
    endpoint_class = "approved_free"
    price_status = "free_confirmed"

    def __init__(self):
        self.coverage: dict[str, tuple[int, str]] = {}

    def extract(self, batch):
        results = []
        for item in batch:
            text = item.text
            if not isinstance(text, SpooledText):
                raise AssertionError("large source did not use spool")
            digest = hashlib.sha256()
            total = 0
            for page in text.iter_pages():
                data = page.encode("utf-8")
                digest.update(data)
                total += len(data)
            expected = str(item.episode.payload.get("canonical_chunk_hash") or "")
            actual = "sha256:" + digest.hexdigest()
            self.coverage[item.session_key] = (total, actual)
            if actual != expected:
                raise AssertionError("extractor coverage hash mismatch")
            results.append(BulkSemanticSessionResult(
                session_key=item.session_key,
                entities=(BulkSemanticEntity(name=f"Synthetic {item.session_key}",
                                             type="Concept", summary="synthetic"),),
                relations=(),
            ))
        return BulkSemanticExtractionResult(tuple(results))


def _insert_card(store, project: str, content_hash: str) -> str:
    memory_id = "mem_" + uuid.uuid4().hex[:16]
    envelope = {
        "memory_id": memory_id, "project": project, "card_type": "preference",
        "approval_state": "approved", "steward_proposal_kind": "", "lifecycle_state": "accepted",
        "authorization_status": "active", "currentness": "current", "content_hash": content_hash,
    }
    with store._scope(write=True) as db:
        db.execute(
            "INSERT INTO memory_cards (memory_id, project, card_type, title, summary, "
            "lifecycle_state, authorization_status, currentness, content_hash, valid_from, valid_to, steward_envelope) "
            "VALUES (%s, %s, 'preference', 'synthetic', 'synthetic', "
            "'accepted', 'active', 'current', %s, %s, NULL, %s::jsonb)",
            [memory_id, project, content_hash, "2020-01-01T00:00:00+00:00", json.dumps(envelope)],
        )
    PgAuthorityCardStore(store.dsn).upsert_card(
        memory_id=memory_id, project=project, content_hash=content_hash,
        provenance="shadow",
    )
    return memory_id


def _seed(store, project: str, sid: str, body: str) -> str:
    provider = "synthetic-provider"
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
    return str(stored.doc_id)


@pytest.mark.parametrize("body", [
    "가" * 300000,
    "😀" * 300000,
    'quote:" backslash:\\\n' + "한" * 300000,
])
def test_oversized_body_full_coverage_e2e(isolated_pg_store, monkeypatch, tmp_path, capsys, body):
    auth = "Basic " + base64.b64encode(f"{COUCH_USER}:{COUCH_PASSWORD}".encode()).decode()
    db = "spoolrun" + uuid.uuid4().hex[:10]
    assert httpx.put(f"{COUCH_URL}/{db}", headers={"Authorization": auth}, timeout=10).status_code in (201, 202, 412)
    store = CouchDBHttpSourceStore(base_url=COUCH_URL, db=db, auth_header=auth)
    project = "spoolrun-" + uuid.uuid4().hex[:10]
    provider = "synthetic-provider"
    sid = dm.build_session_id_hash(provider, "spoolrun-" + uuid.uuid4().hex)
    doc_id = _seed(store, project, sid, body)
    raw_doc = store.get(doc_id)
    content_hash = str(raw_doc["content_hash"])
    memory_id = _insert_card(isolated_pg_store, project, content_hash)
    extractor = _Extractor()
    monkeypatch.setattr(cli, "_build_source_store", lambda **kwargs: store)
    monkeypatch.setattr(cli.OpenAICompatibleBulkSemanticExtractor, "from_env",
                        classmethod(lambda cls, **kwargs: extractor))
    monkeypatch.setenv("NEURON_TRIAL_MODE", "1")
    monkeypatch.setenv("TRIAL_PG_DSN", isolated_pg_store.dsn)
    monkeypatch.setenv("TRIAL_NEO4J_URI", NEO4J_URI)
    monkeypatch.setenv("TRIAL_NEO4J_PASSWORD", NEO4J_PASSWORD)
    monkeypatch.setenv("LLM_BRAIN_BULK_FREE_MODEL_ALLOWLIST", "free-model")
    monkeypatch.setenv("NEURON_SOURCE_CURRENTNESS_MAX_PAGE_BYTES", str(8 * 1024 * 1024))
    monkeypatch.setenv("NEURON_SOURCE_CURRENTNESS_MAX_COMPACT_BYTES", str(8 * 1024 * 1024))
    policy = "full-input.v1"
    group = shadow_group_ids(f"/project/{project}", policy)[0]
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    try:
        rc = cli.main([
            "--ledger", str(tmp_path / "ledger.db"), "--runtime-dir", str(tmp_path / "runtime"),
            "--project", project, "--limit", "1", "--stream-mode",
            "--trial-neo4j-writer", "--full-input-policy-version", policy,
        ])
        captured = capsys.readouterr()
        report = json.loads(captured.out or "{}")
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        print("SPOOL_REPORT", json.dumps(report, sort_keys=True)[:4000])
        assert rc == 0 and report["status"] == "ok", report
        assert report["full_input_chunks"] == 1
        assert extractor.coverage
        assert all(total >= 600000 for total, _ in extractor.coverage.values())
        assert all(digest == content_hash for _, digest in extractor.coverage.values())
        reader = FullInputReader(isolated_pg_store.dsn)
        fact = __import__("types").SimpleNamespace(
            entity_type="GraphFact",
            payload={"shadow_group": group, "authority_sources": [
                {"authority_memory_id": memory_id, "content_hash": content_hash}
            ]},
        )
        result = reader.resolve(project, (fact,), eligible_shadow_groups=(group,))
        direct = isolated_pg_store.find_authorized_by_content_hashes(
            project=project, content_hashes=[content_hash],
            as_of=__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            + __import__("datetime").timedelta(seconds=60),
        )
        print("AUTH_DIRECT", direct["by_hash"].get(content_hash) is not None,
              direct["by_hash"].get(content_hash) == [memory_id], direct["ambiguous_hashes"] == set())
        exact = isolated_pg_store.list_authorized_cards(
            project=project, memory_ids=[memory_id], limit=1, steward_only=True
        )
        print("CARD_EXACT", bool(exact),
              bool(exact) and str(exact[0].get("content_hash") or "") == content_hash)
        assert memory_id in [card.get("id") or card.get("memory_id") for card in result["cards"]], result
        print("SPOOL_RSS", body[:1], before, peak, "delta", peak - before)
    finally:
        with GraphDatabase.driver(NEO4J_URI, auth=("neo4j", NEO4J_PASSWORD)) as driver:
            with driver.session() as session:
                session.run("MATCH (n:TrialShadowEntity) WHERE n.group_id = $g DETACH DELETE n", g=group)
        httpx.delete(f"{COUCH_URL}/{db}", headers={"Authorization": auth}, timeout=10)
