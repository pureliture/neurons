"""Large-hash E2E: 101 source chunks -> PG authority -> HTTP MCP."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import socket
import threading
import time
import uuid

import httpx
import pytest
from neo4j import GraphDatabase

pytest.importorskip("mcp", reason="isolated mcp SDK target required")

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from agent_knowledge.couchdb_source import document_model as dm  # noqa: E402
from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBHttpSourceStore  # noqa: E402
from agent_knowledge.couchdb_source.project_authority import ProjectResolution  # noqa: E402
from agent_knowledge.couchdb_source.source_revision import activate_source_revision  # noqa: E402
from agent_knowledge.knowledge_search_service import (  # noqa: E402
    DisabledRetiredIndexBridgeClient, KnowledgeSearchService,
)
from agent_knowledge.llm_brain_core import bulk_semantic_cli as cli  # noqa: E402
from agent_knowledge.llm_brain_core.bulk_full_input import shadow_group_ids  # noqa: E402
from agent_knowledge.llm_brain_core.bulk_semantic import (  # noqa: E402
    BulkSemanticEntity, BulkSemanticExtractionResult, BulkSemanticSessionResult,
)
from agent_knowledge.llm_brain_core.trial_shadow_adapter import TrialShadowGraphAdapter  # noqa: E402
from agent_knowledge.mcp_http_server import build_app  # noqa: E402
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession  # noqa: E402

PG_DSN = os.environ.get("LBRAIN_TEST_PG_DSN", "")
NEO4J_URI = os.environ.get("TRIAL_NEO4J_URI", "")
NEO4J_PASSWORD = os.environ.get("TRIAL_NEO4J_PASSWORD", "")
COUCH_URL = os.environ.get("TRIAL_COUCH_URL", "")
COUCH_USER = os.environ.get("TRIAL_COUCH_USER", "")
COUCH_PASSWORD = os.environ.get("TRIAL_COUCH_PASSWORD", "")
pytestmark = pytest.mark.skipif(not all((PG_DSN, NEO4J_URI, NEO4J_PASSWORD,
                                         COUCH_URL, COUCH_USER, COUCH_PASSWORD)),
                                reason="isolated loopback DBs required")


class _Extractor:
    model = "free-model"
    provider = "synthetic-provider"
    endpoint_class = "approved_free"
    price_status = "free_confirmed"

    def extract(self, batch):  # type: ignore[no-untyped-def]
        return BulkSemanticExtractionResult(tuple(
            BulkSemanticSessionResult(
                session_key=item.session_key,
                entities=(BulkSemanticEntity(name=f"Synthetic {item.session_key}",
                                             type="Concept", summary="synthetic"),),
                relations=(),
            ) for item in batch
        ))


def _h(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


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
            "lifecycle_state, authorization_status, currentness, content_hash, steward_envelope) "
            "VALUES (%s, %s, 'preference', 'synthetic', 'synthetic', "
            "'accepted', 'active', 'current', %s, %s::jsonb)",
            [memory_id, project, content_hash, json.dumps(envelope)],
        )
    return memory_id


def _seed(store, project: str, sid: str, bodies: list[str]) -> list[str]:
    provider = "synthetic-provider"
    store.put(dm.build_transcript_session_document(session=TranscriptSession(
        session_id_hash=sid, provider=provider, project=project,
        started_at="2026-06-21T00:00:00Z",
    )))
    hashes: list[str] = []
    for index, body in enumerate(bodies):
        chunk = TranscriptChunk.from_text(
            chunk_id=f"chunk_{index}", session_id_hash=sid, provider=provider, project=project,
            turn_start_index=index, turn_end_index=index, text=body,
        )
        doc = dm.build_conversation_chunk_document(chunk=chunk)
        hashes.append(str(doc["content_hash"]))
        store.put(doc)
    authority = ProjectResolution(
        project=project, source="capture_metadata", ambiguous=False,
        candidates=(), index_mismatch=False,
    ).to_authority_block()
    assert "authority_sources" not in authority
    store.put(dm.build_coverage_manifest_document(
        session_id_hash=sid, provider=provider, project=project,
        conversation_chunk_count=len(bodies), tool_evidence_bundle_count=0,
        conversation_content_hashes=hashes, tool_evidence_coverage_hashes=[],
        project_authority=authority,
    ))
    activate_source_revision(store=store, session_id_hash=sid)
    return hashes


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Server:
    def __init__(self, app, port: int):
        import uvicorn
        self._config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self):
        self._thread.start()
        base = f"http://127.0.0.1:{self._config.port}"
        for _ in range(100):
            try:
                if httpx.get(f"{base}/healthz", timeout=0.5).status_code == 200:
                    return base
            except Exception:
                time.sleep(0.05)
        raise RuntimeError("server not ready")

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(timeout=10)


def _resolve(store, adapter, project: str) -> dict:
    service = KnowledgeSearchService(
        ledger=object(), retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[], pgvector_store=store, graph_adapter=adapter,
    )
    port = _port()
    app = build_app(service, host="127.0.0.1", port=port)

    async def run():
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await s.call_tool("brain.resolve", {
                    "project": project, "query": "synthetic", "mode": "query",
                    "response_mode": "minimal", "limit": 20,
                })

    with _Server(app, port):
        result = asyncio.run(run())
    return result.structuredContent


def test_101_chunks_crossing_100_hash_boundary_reach_mcp(isolated_pg_store, monkeypatch,
                                                          tmp_path, capsys):
    db = "bulkhash" + uuid.uuid4().hex[:10]
    auth = "Basic " + base64.b64encode(f"{COUCH_USER}:{COUCH_PASSWORD}".encode()).decode()
    assert httpx.put(f"{COUCH_URL}/{db}", headers={"Authorization": auth}, timeout=10).status_code in (201, 202, 412)
    store = CouchDBHttpSourceStore(base_url=COUCH_URL, db=db, auth_header=auth)
    project = "bulkhash-" + uuid.uuid4().hex[:10]
    policy = "full-input.v1"
    provider = "synthetic-provider"
    sid = dm.build_session_id_hash(provider, "bulkhash-" + uuid.uuid4().hex)
    bodies = [f"BODY{i}-" + "x" * 32 for i in range(101)]
    hashes = _seed(store, project, sid, bodies)
    target = _insert_card(isolated_pg_store, project, hashes[100])
    ambiguous_hash = hashes[99]
    amb_a = _insert_card(isolated_pg_store, project, ambiguous_hash)
    amb_b = _insert_card(isolated_pg_store, project, ambiguous_hash)

    monkeypatch.setattr(cli, "_build_source_store", lambda **kwargs: store)
    monkeypatch.setattr(cli.OpenAICompatibleBulkSemanticExtractor, "from_env",
                        classmethod(lambda cls, **kwargs: _Extractor()))
    monkeypatch.setenv("NEURON_TRIAL_MODE", "1")
    monkeypatch.setenv("TRIAL_PG_DSN", isolated_pg_store.dsn)
    monkeypatch.setenv("TRIAL_NEO4J_URI", NEO4J_URI)
    monkeypatch.setenv("TRIAL_NEO4J_PASSWORD", NEO4J_PASSWORD)
    monkeypatch.setenv("LLM_BRAIN_BULK_FREE_MODEL_ALLOWLIST", "free-model")
    group = shadow_group_ids(f"/project/{project}", policy)[0]
    try:
        rc = cli.main([
            "--ledger", str(tmp_path / "ledger.db"), "--runtime-dir", str(tmp_path / "runtime"),
            "--project", project, "--limit", "1", "--stream-mode", "--trial-neo4j-writer",
            "--full-input-policy-version", policy,
        ])
        report = json.loads(capsys.readouterr().out or "{}")
        assert rc == 0 and report["full_input_chunks"] == 101, report
        adapter = TrialShadowGraphAdapter(NEO4J_URI, "neo4j", NEO4J_PASSWORD,
                                          eligible_shadow_groups=(group,))
        result = _resolve(isolated_pg_store, adapter, project)
        ids = [item.get("id") for item in result.get("items", [])
               if item.get("source") == "graph_card"]
        assert target in ids, result
        assert amb_a not in ids and amb_b not in ids, result
    finally:
        with GraphDatabase.driver(NEO4J_URI, auth=("neo4j", NEO4J_PASSWORD)) as driver:
            with driver.session() as session:
                session.run("MATCH (n:TrialShadowEntity) WHERE n.group_id = $g DETACH DELETE n", g=group)
        httpx.delete(f"{COUCH_URL}/{db}", headers={"Authorization": auth}, timeout=10)
