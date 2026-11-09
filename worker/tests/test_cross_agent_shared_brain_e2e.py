"""공용 뇌 cross-agent E2E(in-memory).

에이전트 A가 세션에서 결정 → ingress(CouchDBDeliveryBackend) → session-memory build
(PgSessionMemoryProjector) → 에이전트 B가 MCP `brain.resolve`(기본 mode)로 받는다.
외부 I/O 없이 실제 ingress backend, materializer, projector, resolver, MCP dispatch를
그대로 통과시키고 PostgreSQL만 in-memory 대역으로 바꾼다.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from types import SimpleNamespace

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.session_memory_materializer import materialize_session_memory
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.knowledge_search_service import DisabledRetiredIndexBridgeClient, KnowledgeSearchService
from agent_knowledge.ledger import Ledger
from agent_knowledge.llm_brain_core.models import GraphMemoryResult, OntologyEpisode
from agent_knowledge.mcp_jsonrpc import handle_jsonrpc_message
from agent_knowledge.postgres_store.pgvector_store import SessionChunk
from agent_knowledge.rag_ingress.backfill_apply import apply_backfill_to_state_db
from agent_knowledge.rag_ingress.couchdb_delivery_backend import CouchDBDeliveryBackend
from agent_knowledge.rag_ingress.delivery_executor import DeliveryJobView
from agent_knowledge.rag_ingress.pg_backfill import PgSessionMemoryProjector
from agent_knowledge.rag_ingress.state_db import RAGIngressStateDB
from test_pg_backfill import SyntheticEmbedProvider

PROJECT = "shared-brain-e2e"
PROVIDER_A = "codex"
DECISION_TEXT = (
    "user: authority 저장소는 무엇으로 할까\n"
    "assistant: 결정: authority 저장소는 PostgreSQL로 확정한다. graph는 derived index로만 쓴다."
)


def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return 1.0 - dot / (na * nb)


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *_args, **_kwargs):
        return None


class _Conn:
    def cursor(self):
        return _Cursor()


class InMemorySharedBrainPgStore:
    """PgVectorStore 대역: session_memory_chunks + 승인 memory_cards 최소 구현."""

    def __init__(self) -> None:
        self.chunks: dict[str, SessionChunk] = {}
        self.cards: list[dict] = []
        self.projected: set[tuple[str, str]] = set()
        self.calls: list[str] = []

    # --- projector가 쓰는 쓰기 경로 ---
    @contextmanager
    def transaction(self):
        yield _Conn()

    def get_chunk(self, chunk_id, conn=None):
        return self.chunks.get(chunk_id)

    def find_ready_chunk_by_identity(self, *, session_id_hash, project, provider, content_hash,
                                     embedding_model, conn=None):
        for row in self.chunks.values():
            if (row.session_id_hash, row.project, row.provider, row.content_hash, row.embedding_model,
                    row.embedding_state) == (session_id_hash, project, provider, content_hash,
                                             embedding_model, "ready"):
                return row
        return None

    def insert_chunk(self, chunk, conn=None, *, insert_only=False):
        if insert_only and chunk.chunk_id in self.chunks:
            raise ValueError("duplicate chunk")
        self.chunks[chunk.chunk_id] = chunk
        return chunk.chunk_id

    # --- resolver가 쓰는 읽기 경로 ---
    def search_session_chunks(self, query_vector, project=None, limit=5, conn=None):
        self.calls.append("search_session_chunks")
        rows = [
            row for row in self.chunks.values()
            if row.embedding_state == "ready" and row.embedding is not None
            and (not project or row.project == project)
        ]
        ranked = sorted(rows, key=lambda r: (_cosine_distance(query_vector, r.embedding), r.chunk_id))
        return [
            {
                "chunk_id": r.chunk_id, "session_id_hash": r.session_id_hash, "project": r.project,
                "provider": r.provider, "chunk_index": r.chunk_index, "content_markdown": r.content_markdown,
                "token_count": r.token_count, "content_hash": r.content_hash, "created_at": r.created_at,
                "observed_at": r.observed_at, "distance": _cosine_distance(query_vector, r.embedding),
            }
            for r in ranked[:limit]
        ]

    def session_memory_freshness(self, project, *, conn=None):
        self.calls.append("session_memory_freshness")
        rows = [r for r in self.chunks.values() if r.project == project]
        pending = [r for r in rows if r.embedding_state != "ready"]
        return {
            "last_built_at": max((r.created_at for r in rows), default=None),
            "last_observed_at": max((r.observed_at for r in rows if r.observed_at is not None), default=None),
            "total_chunks": len(rows),
            "pending_chunks": len(pending),
            "pending_sessions": len({r.session_id_hash for r in pending}),
        }

    def graph_projection_health(self, project, *, as_of=None, steward_only=False):
        missing = [c for c in self.cards if c["project"] == project
                   and (c["memory_id"], c["content_hash"]) not in self.projected]
        return {"unprojected": bool(missing), "projection_lag_ms": None}

    def list_authorized_cards(self, *, project, memory_ids=None, as_of=None, limit=100,
                              after_memory_id=None, steward_only=False):
        self.calls.append("list_authorized_cards")
        rows = [dict(c) for c in self.cards if c["project"] == project
                and c.get("approval_state") == "approved"
                and (memory_ids is None or c["memory_id"] in memory_ids)]
        return sorted(rows, key=lambda c: c["memory_id"])[:limit]

    def hybrid_search(self, project="", query_vector=None, limit=5, text_query=None, as_of=None,
                      steward_only=False, **_kwargs):
        self.calls.append("hybrid_search")
        return [{**dict(c), "semantic_score": 0.9, "similarity_score": 0.9}
                for c in self.cards if c["project"] == project][:limit]

    def list_context_cards(self, *, project, query="", as_of=None, limit=5, steward_only=False):
        return []


def _ingress_payload(*, idempotency_key: str, session_id_hash: str, chunk_id: str, body: str) -> dict:
    return {
        "schemaVersion": "rag_ingress_enqueue.v1",
        "source": {"host": "agent-a-host", "producer": "dendrite", "provider": PROVIDER_A, "project": PROJECT},
        "payload": {
            "kind": "redacted_rag_ready_document",
            "redactionVersion": "redaction.v2",
            "document": {
                "filename": "session.md",
                "contentType": "text/markdown",
                "body": body,
                "metadata": {
                    "type": "conversation_chunk",
                    "session_id_hash": session_id_hash,
                    "chunk_id": chunk_id,
                    "provider": PROVIDER_A,
                    "project": PROJECT,
                    "turn_start_index": 0,
                    "turn_end_index": 1,
                    "part_index": 1,
                    "part_count": 1,
                    "char_start": 0,
                    "char_end": len(body),
                },
            },
        },
        "contentHash": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
        "targetProfile": "couchdb-transcript-source",
        "kind": "conversation_chunk",
        "idempotencyKey": idempotency_key,
    }


def agent_a_records_decision_via_ingress(tmp_path, couch: InMemoryCouchDBSourceStore, *, body=DECISION_TEXT) -> str:
    """에이전트 A의 세션 청크가 dendrite → ingress → CouchDB source plane에 들어간다."""
    private = tmp_path / "ingress-private"
    private.mkdir(mode=0o700)
    state_db = RAGIngressStateDB(private / "state.sqlite")
    session_id_hash = dm.sha256_hash(f"{PROVIDER_A}:agent-a-session")
    payload = _ingress_payload(idempotency_key="agent-a-turn-1", session_id_hash=session_id_hash,
                               chunk_id="agent-a-chunk-1", body=body)
    report = apply_backfill_to_state_db(state_db=state_db, payloads=[payload], dry_run=False)
    assert report["conflict_count"] == 0
    row = state_db.get_row("delivery_jobs", "idempotency_key", "agent-a-turn-1")
    evidence = CouchDBDeliveryBackend(state_db=state_db, store=couch).submit(DeliveryJobView.from_row(row))
    assert evidence.status == "succeeded"
    assert couch.get(dm.projection_state_doc_id(session_id_hash))["projection_status"] == dm.ProjectionStatus.PENDING
    return session_id_hash


def build_session_memory(couch, pg, embed, *, session_id_hash: str) -> str:
    document = materialize_session_memory(session_id_hash=session_id_hash, store=couch).to_projection_document()
    projector = PgSessionMemoryProjector(store=pg, embed_provider=embed)
    return projector.project(target_profile="session-memory", document=document)


def agent_b_service(tmp_path, pg, embed, *, graph_adapter=None) -> KnowledgeSearchService:
    private = tmp_path / "agent-b-private"
    private.mkdir(mode=0o700, parents=True)
    ledger = Ledger(private / "ledger.sqlite")
    ledger.list_llm_brain_memory_cards = lambda **_kw: pytest.fail("legacy ledger bypass forbidden")
    return KnowledgeSearchService(
        ledger=ledger, retired_index_bridge=DisabledRetiredIndexBridgeClient(), dataset_ids=[],
        pgvector_store=pg, graph_adapter=graph_adapter,
        semantic_ranker=SimpleNamespace(embed_query=embed.embed),
        mirror_search=lambda *_args: pytest.fail("Qdrant public read forbidden"),
    )


def agent_b_resolves(service, *, query: str, **extra) -> dict:
    response = handle_jsonrpc_message({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "brain.resolve", "arguments": {"project": PROJECT, "query": query, **extra}},
    }, service)
    assert "error" not in response, response
    return response["result"]["structuredContent"]


def _approved_card(pg, *, memory_id: str, summary: str) -> OntologyEpisode:
    content_hash = "sha256:" + hashlib.sha256(summary.encode()).hexdigest()
    pg.cards.append({
        "memory_id": memory_id, "project": PROJECT, "card_type": "decision", "title": "승인 card",
        "summary": summary, "typed_payload": {"decision": summary}, "currentness": "current",
        "content_hash": content_hash, "lifecycle_state": "human_accepted", "authorization_status": "active",
        "approval_state": "approved", "created_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
        "observed_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
    })
    pg.projected.add((memory_id, content_hash))
    return OntologyEpisode.from_payload(
        event_id=f"event:{memory_id}", entity_type="Decision", natural_id=memory_id,
        payload={"authority_memory_id": memory_id, "content_hash": content_hash,
                 "brain_id": f"/project/{PROJECT}", "summary": "그래프의 추론 문구", "provider": PROVIDER_A},
    )


def test_agent_b_receives_agent_a_session_decision_from_shared_brain_without_session_lookup(tmp_path):
    couch = InMemoryCouchDBSourceStore()
    pg = InMemorySharedBrainPgStore()
    embed = SyntheticEmbedProvider()

    # 빌드 전: 공용 뇌에는 아직 아무것도 없고 신선도도 비어 있다.
    before = agent_b_resolves(agent_b_service(tmp_path / "before", pg, embed), query="authority 저장소 결정")
    assert before["items"] == []
    assert before["metadata"]["freshness"]["session_memory_available"] is True
    assert before["metadata"]["freshness"]["session_memory_last_built_at"] is None

    session_id_hash = agent_a_records_decision_via_ingress(tmp_path, couch)
    chunk_id = build_session_memory(couch, pg, embed, session_id_hash=session_id_hash)
    assert pg.chunks[chunk_id].embedding_state == "ready"

    # 에이전트 B는 세션/transcript를 뒤지지 않고 brain.resolve(기본 mode)만 호출한다.
    service = agent_b_service(tmp_path / "after", pg, embed)
    after = agent_b_resolves(service, query="authority 저장소 결정")

    assert len(after["items"]) == 1
    item = after["items"][0]
    assert item["source"] == "session_memory"
    assert item["id"] == f"chunk:{chunk_id[:56]}"
    assert "PostgreSQL로 확정" in item["summary"]
    assert "session_memory_chunks" not in json.dumps(after, ensure_ascii=False)
    assert session_id_hash not in json.dumps(after, ensure_ascii=False)
    meta = after["metadata"]
    assert meta["retrieval_path"] == "pgvector_fallback"
    assert meta["session_memory_merged"] is True
    assert meta["sources"] == {"graph_card": 0, "session_memory": 1}
    freshness = meta["freshness"]
    assert freshness["session_memory_available"] is True
    assert freshness["session_memory_last_built_at"] is not None
    assert freshness["session_memory_last_observed_at"] is not None
    assert freshness["session_memory_pending_chunks"] == 0
    assert freshness["session_memory_pending_sessions"] == 0
    # 명시 mode=context 호출은 기존처럼 동작한다(세션 지식이 아닌 활성 컨텍스트 경로).
    legacy = agent_b_resolves(service, query="authority 저장소 결정", mode="context")
    assert "error_code" not in legacy


def test_shared_brain_merges_approved_graph_card_with_other_agents_session_memory(tmp_path):
    couch = InMemoryCouchDBSourceStore()
    pg = InMemorySharedBrainPgStore()
    embed = SyntheticEmbedProvider()
    episode = _approved_card(pg, memory_id="mem_authority_pg", summary="authority 저장소 결정: PostgreSQL 정본")
    graph_calls = []

    def search_context(**kwargs):
        graph_calls.append(kwargs["brain_id"])
        return GraphMemoryResult(status="available", episodes=(episode,))

    session_id_hash = agent_a_records_decision_via_ingress(tmp_path, couch)
    chunk_id = build_session_memory(couch, pg, embed, session_id_hash=session_id_hash)
    service = agent_b_service(tmp_path, pg, embed, graph_adapter=SimpleNamespace(search_context=search_context))

    payload = agent_b_resolves(service, query="authority 저장소 결정")

    assert graph_calls == [f"/project/{PROJECT}"]
    assert [item["source"] for item in payload["items"]] == ["graph_card", "session_memory"]
    assert [item["id"] for item in payload["items"]] == ["mem_authority_pg", f"chunk:{chunk_id[:56]}"]
    assert payload["items"][0]["summary"] == "authority 저장소 결정: PostgreSQL 정본"
    assert "그래프의 추론 문구" not in json.dumps(payload, ensure_ascii=False)
    meta = payload["metadata"]
    assert meta["retrieval_path"] == "graph_neo4j"
    assert meta["authority_join_status"] == "verified"
    assert meta["fallback_used"] is False
    assert meta["session_memory_merged"] is True
    assert meta["sources"] == {"graph_card": 1, "session_memory": 1}
    assert "search_session_chunks" in pg.calls


def test_shared_brain_reports_pending_session_memory_as_freshness_gap(tmp_path):
    couch = InMemoryCouchDBSourceStore()
    pg = InMemorySharedBrainPgStore()
    embed = SyntheticEmbedProvider()
    session_id_hash = agent_a_records_decision_via_ingress(tmp_path, couch)
    build_session_memory(couch, pg, embed, session_id_hash=session_id_hash)
    # 다른 에이전트의 두 번째 세션은 PG에 들어왔지만 embedding이 아직 준비되지 않았다.
    pending_text = "user: 두 번째 세션\nassistant: 아직 embedding 대기 중인 결정"
    pg.insert_chunk(SessionChunk(
        chunk_id="pending-chunk", session_id_hash=dm.sha256_hash("codex:agent-c-session"),
        project=PROJECT, provider="claude", content_markdown=pending_text,
        content_hash=dm.sha256_hash(pending_text), embedding_state="pending",
    ))

    payload = agent_b_resolves(agent_b_service(tmp_path, pg, embed), query="authority 저장소 결정")

    assert [item["source"] for item in payload["items"]] == ["session_memory"]
    freshness = payload["metadata"]["freshness"]
    assert freshness["session_memory_pending_chunks"] == 1
    assert freshness["session_memory_pending_sessions"] == 1
    assert "pending-chunk" not in json.dumps(payload["items"], ensure_ascii=False)


def test_shared_brain_shows_only_the_approved_card_when_session_chunk_has_same_content(tmp_path):
    couch = InMemoryCouchDBSourceStore()
    pg = InMemorySharedBrainPgStore()
    embed = SyntheticEmbedProvider()
    session_id_hash = agent_a_records_decision_via_ingress(tmp_path, couch)
    chunk_id = build_session_memory(couch, pg, embed, session_id_hash=session_id_hash)
    chunk = pg.chunks[chunk_id]
    # 에이전트 A의 세션 본문이 그대로 승인 card가 되었다(content_hash 동일).
    episode = _approved_card(pg, memory_id="mem_from_agent_a", summary="세션 본문에서 승인된 결정")
    pg.cards[-1]["content_hash"] = chunk.content_hash
    pg.projected.add(("mem_from_agent_a", chunk.content_hash))
    episode = OntologyEpisode.from_payload(
        event_id="event:mem_from_agent_a", entity_type="Decision", natural_id="mem_from_agent_a",
        payload={"authority_memory_id": "mem_from_agent_a", "content_hash": chunk.content_hash,
                 "brain_id": f"/project/{PROJECT}", "summary": "그래프의 추론 문구", "provider": PROVIDER_A},
    )
    graph = SimpleNamespace(search_context=lambda **kw: GraphMemoryResult(status="available", episodes=(episode,)))

    payload = agent_b_resolves(agent_b_service(tmp_path, pg, embed, graph_adapter=graph), query="authority 저장소 결정")

    assert [(item["id"], item["source"]) for item in payload["items"]] == [("mem_from_agent_a", "graph_card")]
    assert payload["metadata"]["sources"] == {"graph_card": 1, "session_memory": 0}
    assert payload["metadata"]["session_memory_merged"] is True
    assert payload["metadata"]["freshness"]["session_memory_pending_chunks"] == 0
    assert f"chunk:{chunk_id[:56]}" not in json.dumps(payload, ensure_ascii=False)
