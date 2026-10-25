"""실제 PostgreSQL과 공개 MCP dispatch 사이의 Graph-first 수직 검증."""

from datetime import datetime, timezone
import hashlib
import json
import os
from types import SimpleNamespace
import uuid

import pytest

from agent_knowledge.knowledge_search_service import KnowledgeSearchService, DisabledRetiredIndexBridgeClient
from agent_knowledge.ledger import Ledger
from agent_knowledge.llm_brain_core.models import GraphMemoryResult, OntologyEpisode
from agent_knowledge.llm_brain_core.graphiti_adapter import GraphitiNeo4jGraphMemoryAdapter, _AsyncLoopRunner
from agent_knowledge.mcp_jsonrpc import handle_jsonrpc_message
from agent_knowledge.mcp_payload import tool_result_bytes
import agent_knowledge.llm_brain_core.graph_first_resolver as resolver_module
from agent_knowledge.postgres_store.pgvector_store import MemoryCard, PgVectorStore, make_dummy_vector

PG_DSN = os.environ.get("LBRAIN_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not PG_DSN, reason="전용 PostgreSQL integration DSN 필요")

# Section 8 응답 계약(22개 temporal 필드)은 실제 전송 경계에서도 그대로 유지된다.
# 응답 내용을 3KB로 줄이던 상한은 이 필수 필드를 잘라냈으므로 폐기했고, 남은 상한은
# 전송 안전 상한뿐이다. 따라서 크기 대신 필수 필드의 완전성을 검사한다.
REQUIRED_TEMPORAL_FIELDS = {
    "observed_at", "recorded_at", "valid_from", "valid_to", "superseded_by", "as_of",
    "age", "ingestion_lag_seconds", "time_confidence", "currentness", "lifecycle_status",
    "freshness", "temporal_relation", "temporal_conflict", "projection_lag_seconds",
    "recommended_action", "summary",
}
REQUIRED_AGE_SUBFIELDS = {"seconds", "label", "clock", "confidence", "reason_codes"}
REQUIRED_FRESHNESS_SUBFIELDS = {"state", "checked_at", "next_check_after", "gaps"}


def _assert_complete_temporal_block(item: dict) -> None:
    temporal = item["temporal"]
    assert not (REQUIRED_TEMPORAL_FIELDS - set(temporal)), "temporal 필수 필드 누락"
    assert not (REQUIRED_AGE_SUBFIELDS - set(temporal["age"])), "temporal.age 하위 필드 누락"
    assert not (REQUIRED_FRESHNESS_SUBFIELDS - set(temporal["freshness"])), "temporal.freshness 하위 필드 누락"


@pytest.mark.parametrize("graph_path", ["direct", "relationship", "historical_relationship", "unavailable"])
def test_public_query_graph_authority_join_and_explicit_pg_fallback(tmp_path, graph_path):
    graph_available = graph_path != "unavailable"
    historical = graph_path == "historical_relationship"
    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()
    memory_id = "m3_public_" + uuid.uuid4().hex[:12]
    content_hash = "sha256:" + hashlib.sha256(memory_id.encode()).hexdigest()
    project = "m3-public-" + uuid.uuid4().hex[:8]
    vector = make_dummy_vector(42)
    card = MemoryCard(memory_id=memory_id, project=project, card_type="decision",
                      title="PG 권위", summary="정본에서 읽은 결정",
                      lifecycle_state="human_accepted", authorization_status="active",
                      embedding=vector, embedding_state="ready", content_hash=content_hash)
    if historical:
        card.currentness = "superseded"
        card.valid_from = datetime(2026, 1, 1, tzinfo=timezone.utc)
        card.valid_to = datetime(2026, 2, 1, tzinfo=timezone.utc)
    events = []

    def graph_search(**kwargs):
        events.append("graph")
        assert kwargs["brain_id"] == f"/project/{project}"
        return GraphMemoryResult(status="available" if graph_available else "unavailable",
                                 episodes=(episode,) if graph_available else ())

    def embed(query):
        events.append("embedding")
        return vector

    episode = OntologyEpisode.from_payload(
        event_id="event:m3", entity_type="Decision", natural_id=memory_id,
        payload={"authority_memory_id": memory_id, "content_hash": content_hash,
                 "brain_id": f"/project/{project}", "summary": "그래프의 추론 문구", "provider": "codex"},
    )
    runner = None
    graph_adapter = SimpleNamespace(search_context=graph_search)
    if graph_path in {"relationship", "historical_relationship"}:
        async def search(query, **_kwargs):
            events.append("graph")
            assert query == "결정"
            if historical:
                assert _kwargs["search_filter"].valid_at[0][0].date == datetime(2026, 1, 15, tzinfo=timezone.utc)
            return [SimpleNamespace(
                uuid="edge:m3", fact="관계로 찾은 결정", episodes=[episode.episode_id],
                valid_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                invalid_at=datetime(2026, 2, 1, tzinfo=timezone.utc) if historical else None,
            )]

        async def retrieve_episodes(**_kwargs):
            # 원문에는 질의어가 없다. 관계 검색의 원본 키만이 PG join으로 이어져야 한다.
            return [SimpleNamespace(content=json.dumps(episode.to_dict()))]

        runner = _AsyncLoopRunner()
        graph_adapter = GraphitiNeo4jGraphMemoryAdapter(
            SimpleNamespace(search=search, retrieve_episodes=retrieve_episodes),
            default_group_id=f"/project/{project}", runner=runner,
        )
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    ledger = Ledger(private / "ledger.sqlite")
    service = KnowledgeSearchService(
        ledger=ledger, retired_index_bridge=DisabledRetiredIndexBridgeClient(), dataset_ids=[],
        pgvector_store=store, graph_adapter=graph_adapter,
        semantic_ranker=SimpleNamespace(embed_query=embed),
        mirror_search=lambda *_args: pytest.fail("Qdrant public read forbidden"),
    )
    ledger.list_llm_brain_memory_cards = lambda **_kw: pytest.fail("legacy ledger bypass forbidden")
    try:
        store.insert_card(card)
        with store.transaction() as conn:
            conn.execute("UPDATE memory_cards SET steward_envelope = %s::jsonb WHERE memory_id = %s",
                         (json.dumps({"approval_state": "approved", "memory_id": memory_id}), memory_id))
            conn.execute(
                """INSERT INTO graph_projection_outbox
                       (source_type, source_id, source_revision, content_hash, episode_payload, status)
                       VALUES ('memory_card', %s, %s, %s, '{}'::jsonb, 'completed')""",
                (memory_id, content_hash, content_hash),
            )
        response = handle_jsonrpc_message({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "brain.resolve", "arguments": {
                "project": project, "query": "결정", "mode": "query",
                "response_mode": "slim",
                **({"as_of": "2026-01-15"} if historical else {}),
            }},
        }, service)
        assert "error" not in response
        payload = response["result"]["structuredContent"]
        assert payload["metadata"]["retrieval_path"] == ("graph_neo4j" if graph_available else "pgvector_fallback")
        assert payload["metadata"]["authority_join_status"] == "verified"
        assert payload["metadata"]["fallback_used"] is (not graph_available)
        assert payload["items"][0]["currentness"] == ("superseded" if historical else "current")
        assert events == (["graph"] if graph_available else ["graph", "embedding"])
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "정본에서 읽은 결정" in serialized
        assert "그래프의 추론 문구" not in serialized
        _assert_complete_temporal_block(payload["items"][0])
        assert tool_result_bytes(payload) <= resolver_module._MAX_BYTES
    finally:
        if runner:
            runner.shutdown()
        with store.transaction() as conn:
            conn.execute("DELETE FROM graph_projection_outbox WHERE source_id = %s", (memory_id,))
            conn.execute("DELETE FROM memory_cards WHERE memory_id = %s", (memory_id,))


def test_public_query_authority_outage_is_tool_error_without_legacy_fallback(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    service = KnowledgeSearchService(
        ledger=Ledger(private / "ledger.sqlite"),
        retired_index_bridge=DisabledRetiredIndexBridgeClient(), dataset_ids=[],
        pgvector_store=PgVectorStore(dsn="postgresql://postgres@127.0.0.1:1/postgres?connect_timeout=1"),
        mirror_search=lambda *_args: pytest.fail("Qdrant public read forbidden"),
    )
    service.ledger.list_llm_brain_memory_cards = lambda **_kw: pytest.fail("ledger bypass forbidden")
    response = handle_jsonrpc_message({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "brain.resolve", "arguments": {"project": "neurons", "query": "decision"}},
    }, service)
    assert response["result"]["isError"] is True
    payload = response["result"]["structuredContent"]
    assert payload["error_code"] == "authority_store_unavailable"
    assert payload["metadata"]["retrieval_path"] == "none"
    assert "127.0.0.1" not in json.dumps(response)


def test_public_list_keyset_crosses_100_rows_with_actual_mcp_wire_budget(tmp_path):
    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()
    project = "m3-pages-" + uuid.uuid4().hex[:8]
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    service = KnowledgeSearchService(
        ledger=Ledger(private / "ledger.sqlite"), pgvector_store=store,
        retired_index_bridge=DisabledRetiredIndexBridgeClient(), dataset_ids=[],
    )
    ids = [f"{project}_{i:03d}" for i in range(107)]
    try:
        with store.transaction() as conn:
            for memory_id in ids:
                store.insert_card(MemoryCard(
                    memory_id=memory_id, project=project, card_type="decision",
                    title="한글😀" * 40, summary="문맥 설명😀" * 80,
                    content_hash="sha256:" + hashlib.sha256(memory_id.encode()).hexdigest(),
                    lifecycle_state="human_accepted", authorization_status="active",
                ), conn=conn)
                conn.execute("UPDATE memory_cards SET steward_envelope = %s::jsonb WHERE memory_id = %s",
                             (json.dumps({"approval_state": "approved", "memory_id": memory_id}), memory_id))
        cursor = None
        observed = []
        for _ in range(len(ids) + 1):
            response = handle_jsonrpc_message({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "brain.resolve", "arguments": {
                    "project": project, "mode": "list", "limit": 20, "response_mode": "slim",
                    **({"cursor": cursor} if cursor else {}),
                }},
            }, service)
            result = response["result"]
            assert result["isError"] is False
            for item in result["structuredContent"]["items"]:
                _assert_complete_temporal_block(item)
            assert tool_result_bytes(result["structuredContent"]) <= resolver_module._MAX_BYTES
            payload = result["structuredContent"]
            observed.extend(item["id"] for item in payload["items"])
            cursor = payload["next_cursor"]
            assert payload["has_more"] is bool(cursor)
            if not cursor:
                break
        assert observed == ids
    finally:
        with store.transaction() as conn:
            conn.execute("DELETE FROM graph_projection_outbox WHERE source_id IN (SELECT memory_id FROM memory_cards WHERE project = %s)", (project,))
            conn.execute("DELETE FROM memory_cards WHERE project = %s", (project,))
