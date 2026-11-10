"""공용 뇌 cross-agent E2E(live-gated): 실제 PostgreSQL + 실제 CouchDB.

에이전트 A의 세션 청크가 ingress backend로 CouchDB에 들어가고, 실제 build CLI가
PgSessionMemoryProjector로 PostgreSQL에 session-memory를 만든 뒤, 에이전트 B가
MCP `brain.resolve`(기본 mode)로 그 결정을 받는다. 합성 데이터와 합성 embedding만 쓴다.

gate: `LBRAIN_TEST_PG_DSN`, `LBRAIN_TEST_COUCHDB_URL`(loopback 전용),
선택: `LBRAIN_TEST_COUCHDB_USER`, `LBRAIN_TEST_COUCHDB_PASSWORD`, `LBRAIN_TEST_COUCHDB_DB`.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from urllib.parse import urlsplit
import uuid

import pytest

from agent_knowledge.couchdb_source import build_cli, document_model as dm
from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBHttpSourceStore, basic_auth_header
from agent_knowledge.knowledge_search_service import DisabledRetiredIndexBridgeClient, KnowledgeSearchService
from agent_knowledge.ledger import Ledger
from agent_knowledge.mcp_jsonrpc import handle_jsonrpc_message
from agent_knowledge.postgres_store.pgvector_store import PgVectorStore
from agent_knowledge.rag_ingress.backfill_apply import apply_backfill_to_state_db
from agent_knowledge.rag_ingress.couchdb_delivery_backend import CouchDBDeliveryBackend
from agent_knowledge.rag_ingress.delivery_executor import DeliveryJobView
from agent_knowledge.rag_ingress.state_db import RAGIngressStateDB
from test_couchdb_build_cli import _write_approval
from test_cross_agent_shared_brain_e2e import DECISION_TEXT, PROVIDER_A, _ingress_payload
import test_cross_agent_shared_brain_e2e as e2e
from test_pg_backfill import SyntheticEmbedProvider

PG_DSN = os.environ.get("LBRAIN_TEST_PG_DSN", "")
COUCHDB_URL = os.environ.get("LBRAIN_TEST_COUCHDB_URL", "")
pytestmark = pytest.mark.skipif(
    not (PG_DSN and COUCHDB_URL), reason="LBRAIN_TEST_PG_DSN + LBRAIN_TEST_COUCHDB_URL (전용 live integration gate) 필요"
)


def _couchdb_store(db: str) -> CouchDBHttpSourceStore:
    host = urlsplit(COUCHDB_URL).hostname or ""
    assert host in {"127.0.0.1", "localhost", "::1"}, "live CouchDB gate는 loopback 전용"
    user = os.environ.get("LBRAIN_TEST_COUCHDB_USER", "")
    auth = basic_auth_header(user=user, password=os.environ.get("LBRAIN_TEST_COUCHDB_PASSWORD", "")) if user else ""
    store = CouchDBHttpSourceStore(base_url=COUCHDB_URL, db=db, auth_header=auth)
    store.ensure_database()
    return store


@pytest.fixture
def lane(monkeypatch, tmp_path, capsys):
    project = "shared-brain-live-" + uuid.uuid4().hex[:8]
    monkeypatch.setattr(e2e, "PROJECT", project)
    db = os.environ.get("LBRAIN_TEST_COUCHDB_DB", "") or ("neurons_test_" + uuid.uuid4().hex[:8])
    couch = _couchdb_store(db)
    sql = PgVectorStore(dsn=PG_DSN)
    sql.execute_ddl()
    embeds: list[SyntheticEmbedProvider] = []

    def embedding_factory(*, environ):
        provider = SyntheticEmbedProvider()
        embeds.append(provider)
        return provider

    monkeypatch.setattr("agent_knowledge.rag_ingress.embedding_provider.build_openai_embedding_provider", embedding_factory)
    monkeypatch.setenv("SESSION_MEMORY_PROJECTION_BACKEND", "postgres_pgvector")
    monkeypatch.setenv("NEURON_LBRAIN_PGVECTOR_DSN", PG_DSN)
    monkeypatch.setenv("COUCHDB_URL", COUCHDB_URL)
    monkeypatch.setenv("COUCHDB_DB", db)
    monkeypatch.setenv("COUCHDB_USER", os.environ.get("LBRAIN_TEST_COUCHDB_USER", ""))
    monkeypatch.setenv("COUCHDB_PASSWORD", os.environ.get("LBRAIN_TEST_COUCHDB_PASSWORD", ""))
    monkeypatch.setenv("PG_EMBEDDING_CHUNK_CACHE", str(tmp_path / "pg-embedding-chunks.sqlite3"))
    argv = ["--project", project, "--approval", str(tmp_path / "approval.json")]
    _write_approval(tmp_path, argv=argv)

    def build():
        rc = build_cli.main(argv)
        return rc, json.loads(capsys.readouterr().out)

    yield dict(project=project, couch=couch, sql=sql, build=build, embeds=embeds)
    with sql.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM embedding_outbox WHERE target_id IN (SELECT chunk_id FROM session_memory_chunks WHERE project = %s)", (project,))
            cur.execute("DELETE FROM session_local_embeddings WHERE chunk_id IN (SELECT chunk_id FROM session_memory_chunks WHERE project = %s)", (project,))
            cur.execute("DELETE FROM session_memory_chunks WHERE project = %s", (project,))
    if not os.environ.get("LBRAIN_TEST_COUCHDB_DB", ""):
        status, _ = couch._request("DELETE", f"/{db}")
        assert status in (200, 202)


def _agent_b_payload(tmp_path, sql, embed, *, project, query="authority 저장소 결정", **extra) -> dict:
    private = tmp_path / ("agent-b-" + uuid.uuid4().hex[:6])
    private.mkdir(mode=0o700)
    ledger = Ledger(private / "ledger.sqlite")
    ledger.list_llm_brain_memory_cards = lambda **_kw: pytest.fail("legacy ledger bypass forbidden")
    service = KnowledgeSearchService(
        ledger=ledger, retired_index_bridge=DisabledRetiredIndexBridgeClient(), dataset_ids=[],
        pgvector_store=sql, semantic_ranker=SimpleNamespace(embed_query=embed.embed),
        pg_search=lambda *_args: pytest.fail("unexpected PostgreSQL callback"),
    )
    response = handle_jsonrpc_message({
        "jsonrpc": "2.0", "id": 11, "method": "tools/call",
        "params": {"name": "brain.resolve", "arguments": {"project": project, "query": query, **extra}},
    }, service)
    assert "error" not in response, response
    return response["result"]["structuredContent"]


def test_agent_a_ingress_to_agent_b_resolve_over_live_pg_and_couchdb(tmp_path, lane):
    project, couch, sql = lane["project"], lane["couch"], lane["sql"]
    embed = SyntheticEmbedProvider()

    before = _agent_b_payload(tmp_path, sql, embed, project=project)
    assert before["items"] == []
    assert before["metadata"]["freshness"]["session_memory_available"] is True
    assert before["metadata"]["freshness"]["session_memory_last_built_at"] is None
    assert before["metadata"]["freshness"]["session_memory_pending_sessions"] == 0

    # 에이전트 A: dendrite payload → ingress state DB → CouchDBDeliveryBackend → 실제 CouchDB
    private = tmp_path / "ingress-private"
    private.mkdir(mode=0o700)
    state_db = RAGIngressStateDB(private / "state.sqlite")
    session_id_hash = dm.sha256_hash(f"{PROVIDER_A}:agent-a-live-session:{project}")
    payload = _ingress_payload(idempotency_key="agent-a-turn-1", session_id_hash=session_id_hash,
                               chunk_id="agent-a-chunk-1", body=DECISION_TEXT)
    payload["source"]["project"] = project
    payload["payload"]["document"]["metadata"]["project"] = project
    assert apply_backfill_to_state_db(state_db=state_db, payloads=[payload], dry_run=False)["conflict_count"] == 0
    row = state_db.get_row("delivery_jobs", "idempotency_key", "agent-a-turn-1")
    evidence = CouchDBDeliveryBackend(state_db=state_db, store=couch).submit(DeliveryJobView.from_row(row))
    assert evidence.status == "succeeded"
    assert couch.get(dm.projection_state_doc_id(session_id_hash))["projection_status"] == dm.ProjectionStatus.PENDING

    # session-memory build: 실제 build CLI → PgSessionMemoryProjector → PostgreSQL
    rc, report = lane["build"]()
    assert rc == 0 and report["projected"] == 1, report
    receipt = couch.get(dm.projection_state_doc_id(session_id_hash))["backend_receipts"]["postgres_pgvector"]
    chunk_id = receipt["session_memory_knowledge_id"]
    assert sql.get_chunk(chunk_id).embedding_state == "ready"

    # 에이전트 B: brain.resolve(기본 mode)만으로 에이전트 A의 결정을 받는다.
    after = _agent_b_payload(tmp_path, sql, embed, project=project)
    assert [item["source"] for item in after["items"]] == ["session_memory"]
    assert after["items"][0]["id"] == f"chunk:{chunk_id[:56]}"
    assert "PostgreSQL로 확정" in after["items"][0]["summary"]
    serialized = json.dumps(after, ensure_ascii=False)
    assert session_id_hash not in serialized and "127.0.0.1" not in serialized
    meta = after["metadata"]
    assert meta["retrieval_path"] == "pgvector_fallback"
    assert meta["session_memory_merged"] is True
    assert meta["sources"] == {"graph_card": 0, "session_memory": 1}
    freshness = meta["freshness"]
    assert freshness["session_memory_last_built_at"] is not None
    assert freshness["session_memory_last_observed_at"] is not None
    assert freshness["session_memory_pending_chunks"] == 0
    assert freshness["session_memory_pending_sessions"] == 0
    assert sql.session_memory_freshness(project)["total_chunks"] == 1

    # 명시 mode=context 호환 호출은 그대로 동작한다.
    legacy = _agent_b_payload(tmp_path, sql, embed, project=project, mode="context")
    assert "error_code" not in legacy
