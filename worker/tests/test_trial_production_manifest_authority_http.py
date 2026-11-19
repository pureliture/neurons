"""Production-manifest fixture: source hash -> PG authority -> HTTP MCP, fail-closed.

No authority_sources are injected into the CouchDB manifest. The coverage
manifest is built by the production builder. Provenance comes only from
already-approved current PG cards matching the exact source chunk hash.
"""

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
import psycopg
import pytest
from neo4j import GraphDatabase

pytest.importorskip("mcp", reason="isolated mcp SDK target required for real HTTP")

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from agent_knowledge.couchdb_source import document_model as dm  # noqa: E402
from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBHttpSourceStore  # noqa: E402
from agent_knowledge.couchdb_source.project_authority import ProjectResolution  # noqa: E402
from agent_knowledge.couchdb_source.source_revision import (  # noqa: E402
    activate_source_revision,
    resolve_active_source_revision,
)
from agent_knowledge.knowledge_search_service import (  # noqa: E402
    DisabledRetiredIndexBridgeClient,
    KnowledgeSearchService,
)
from agent_knowledge.llm_brain_core import bulk_semantic_cli as cli  # noqa: E402
from agent_knowledge.llm_brain_core.bulk_full_input import shadow_group_ids  # noqa: E402
from agent_knowledge.llm_brain_core.bulk_full_input_reader import PgAuthorityCardStore  # noqa: E402
from agent_knowledge.llm_brain_core.bulk_semantic import (  # noqa: E402
    BulkSemanticEntity,
    BulkSemanticExtractionResult,
    BulkSemanticRelation,
    BulkSemanticSessionResult,
)
from agent_knowledge.llm_brain_core.trial_shadow_adapter import TrialShadowGraphAdapter  # noqa: E402
from agent_knowledge.mcp_http_server import build_app  # noqa: E402
from agent_knowledge.session_memory.brain_steward import BrainStewardService  # noqa: E402
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession  # noqa: E402

PG_DSN = os.environ.get("LBRAIN_TEST_PG_DSN", "")
NEO4J_URI = os.environ.get("TRIAL_NEO4J_URI", "")
NEO4J_PASSWORD = os.environ.get("TRIAL_NEO4J_PASSWORD", "")
COUCH_URL = os.environ.get("TRIAL_COUCH_URL", "")
COUCH_USER = os.environ.get("TRIAL_COUCH_USER", "")
COUCH_PASSWORD = os.environ.get("TRIAL_COUCH_PASSWORD", "")
pytestmark = pytest.mark.skipif(
    not (PG_DSN and NEO4J_URI and NEO4J_PASSWORD and COUCH_URL and COUCH_USER and COUCH_PASSWORD),
    reason="isolated loopback PG/Neo4j/CouchDB required",
)


class _NoCardLedger:
    read_only = False

    def __getattr__(self, name):
        if name.startswith(("get_llm_brain", "list_llm_brain", "upsert_llm_brain", "_transaction")):
            raise AssertionError("SQLite card lane must not be accessed")
        raise AttributeError(name)


def _card(store, project: str, content_hash: str, *, approve: bool) -> str:
    span = {
        "card_type": "preference", "project": project, "provider": "hermes", "scope": "project",
        "title": "synthetic authority", "redacted_summary": "synthetic authority",
        "typed_payload": {
            "preference": "synthetic value", "explicitness": "explicit", "repeated_count": 1,
            "confirmation_status": "confirmed", "applies_to": "authority_contract",
        },
        "source_ref": {"source_id": "synthetic-" + uuid.uuid4().hex},
        "span_ref": {"span_id": "synthetic-" + uuid.uuid4().hex},
        "content_hash": content_hash, "confidence": 0.9, "confidence_basis": "synthetic test",
    }
    steward = BrainStewardService(_NoCardLedger(), pgvector_store=store, allow_restricted=True)
    memory_id = steward.candidate_create(source_span=span)["memory_id"]
    if approve:
        steward.candidate_approve(
            candidate_memory_id=memory_id, approved_by="test",
            decision_id="authority-" + uuid.uuid4().hex,
        )
    return memory_id


def _expire(dsn: str, memory_id: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE memory_cards SET valid_from = %s, valid_to = %s WHERE memory_id = %s",
            ("2019-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00", memory_id),
        )


class _FreeExtractor:
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
                relations=(BulkSemanticRelation(source=f"Synthetic {item.session_key}",
                                                target="SyntheticTarget", type="supports",
                                                fact="synthetic CLI relation"),),
            ) for item in batch
        ))


def _chunk_hash(provider: str, sid: str, index: int, body: str) -> str:
    chunk = TranscriptChunk.from_text(
        chunk_id=f"chunk_{index}", session_id_hash=sid, provider=provider,
        project="unused", turn_start_index=index, turn_end_index=index, text=body,
    )
    return str(dm.build_conversation_chunk_document(chunk=chunk)["content_hash"])


def _seed(store, project: str, sid: str, bodies: list[str]) -> list[str]:
    provider = "synthetic-provider"
    store.put(dm.build_transcript_session_document(session=TranscriptSession(
        session_id_hash=sid, provider=provider, project=project,
        started_at="2026-06-21T00:00:00Z",
    )))
    hashes: list[str] = []
    for index, body in enumerate(bodies):
        chunk = TranscriptChunk.from_text(
            chunk_id=f"chunk_{index}", session_id_hash=sid, provider=provider,
            project=project, turn_start_index=index, turn_end_index=index, text=body,
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
    current = resolve_active_source_revision(store=store, session_id_hash=sid)
    assert current.session_id_hash == sid
    return hashes


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _ServerThread:
    def __init__(self, app, port: int):
        import uvicorn
        self._config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> str:
        self._thread.start()
        base = f"http://127.0.0.1:{self._config.port}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{base}/healthz", timeout=0.5).status_code == 200:
                    return base
            except Exception:
                time.sleep(0.05)
        raise RuntimeError("mcp-http server did not start")

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(timeout=10)


def _resolve_http(store, adapter, *, project: str) -> dict:
    service = KnowledgeSearchService(
        ledger=object(), retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[], pgvector_store=store, graph_adapter=adapter,
    )
    port = _free_port()
    app = build_app(service, host="127.0.0.1", port=port)

    async def roundtrip():
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.list_tools()
                return await session.call_tool("brain.resolve", {
                    "project": project, "query": "synthetic authority",
                    "mode": "query", "response_mode": "minimal", "limit": 20,
                })

    with _ServerThread(app, port):
        called = asyncio.run(roundtrip())
    result = called.structuredContent
    assert result.get("error_code") in (None, "embedding_unavailable"), result
    return result


def test_production_manifest_and_pg_authority_http_contract(isolated_pg_store, monkeypatch,
                                                            tmp_path, capsys):
    db = "trialauth" + uuid.uuid4().hex[:10]
    auth = "Basic " + base64.b64encode(f"{COUCH_USER}:{COUCH_PASSWORD}".encode()).decode()
    assert httpx.put(f"{COUCH_URL}/{db}", headers={"Authorization": auth}, timeout=10).status_code in (201, 202, 412)
    store = CouchDBHttpSourceStore(base_url=COUCH_URL, db=db, auth_header=auth)
    project = "auth-" + uuid.uuid4().hex[:10]
    other_project = "other-" + uuid.uuid4().hex[:10]
    policy = "full-input.v1"
    provider = "synthetic-provider"
    sid = dm.build_session_id_hash(provider, "authority-" + uuid.uuid4().hex)
    bodies = [f"BODY{i}-" + chr(ord("A") + i) * 120 for i in range(6)]
    hashes = [_chunk_hash(provider, sid, i, body) for i, body in enumerate(bodies)]

    approved = _card(isolated_pg_store, project, hashes[0], approve=True)
    unapproved = _card(isolated_pg_store, project, hashes[1], approve=False)
    expired = _card(isolated_pg_store, project, hashes[2], approve=True)
    _expire(isolated_pg_store.dsn, expired)
    invalid_hash = _card(isolated_pg_store, project, _digest("not-a-chunk"), approve=True)
    ambiguous_a = _card(isolated_pg_store, project, hashes[4], approve=True)
    ambiguous_b = _card(isolated_pg_store, project, hashes[4], approve=True)
    other_project_card = _card(isolated_pg_store, other_project, hashes[0], approve=True)
    _seed(store, project, sid, bodies)

    monkeypatch.setattr(cli, "_build_source_store", lambda **kwargs: store)
    monkeypatch.setattr(cli.OpenAICompatibleBulkSemanticExtractor, "from_env",
                        classmethod(lambda cls, **kwargs: _FreeExtractor()))
    monkeypatch.setenv("NEURON_TRIAL_MODE", "1")
    monkeypatch.setenv("TRIAL_PG_DSN", isolated_pg_store.dsn)
    monkeypatch.setenv("TRIAL_NEO4J_URI", NEO4J_URI)
    monkeypatch.setenv("TRIAL_NEO4J_PASSWORD", NEO4J_PASSWORD)
    monkeypatch.setenv("LLM_BRAIN_BULK_FREE_MODEL_ALLOWLIST", "free-model")
    group = shadow_group_ids(f"/project/{project}", policy)[0]
    try:
        rc = cli.main([
            "--ledger", str(tmp_path / "ledger.db"), "--runtime-dir", str(tmp_path / "runtime"),
            "--project", project, "--limit", "1", "--stream-mode",
            "--trial-neo4j-writer", "--full-input-policy-version", policy,
        ])
        captured = capsys.readouterr()
        report = json.loads(captured.out or captured.err)
        assert rc == 0 and report["status"] == "ok", report
        assert report["full_input_chunks"] == len(bodies), report

        adapter = TrialShadowGraphAdapter(NEO4J_URI, "neo4j", NEO4J_PASSWORD,
                                          eligible_shadow_groups=(group,))
        result = _resolve_http(isolated_pg_store, adapter, project=project)
        ids = [item.get("id") for item in result.get("items", [])
               if item.get("source") == "graph_card"]
        assert approved in ids, result
        for hidden in (unapproved, expired, invalid_hash, ambiguous_a, ambiguous_b,
                       other_project_card):
            assert hidden not in ids, (hidden, result)
        assert result["metadata"]["retrieval_path"] == "graph_neo4j", result["metadata"]

        episodes = adapter.search_context(brain_id=f"/project/{project}",
                                          query="synthetic", limit=20).episodes
        assert any(ep.payload.get("authority_memory_id") == approved
                   and ep.payload.get("content_hash") == hashes[0]
                   and ep.payload.get("chunk_key") for ep in episodes)
        relations = [r for r in adapter.read_relation_facts()
                     if r["group_id"] == group and r["fact"] == "synthetic CLI relation"]
        assert relations and relations[0]["relation_type"] == "supports", relations
    finally:
        with GraphDatabase.driver(NEO4J_URI, auth=("neo4j", NEO4J_PASSWORD)) as driver:
            with driver.session() as session:
                session.run("MATCH (n:TrialShadowEntity) WHERE n.group_id = $g DETACH DELETE n",
                            g=group)
        httpx.delete(f"{COUCH_URL}/{db}", headers={"Authorization": auth}, timeout=10)


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
