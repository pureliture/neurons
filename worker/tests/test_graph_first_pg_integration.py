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
