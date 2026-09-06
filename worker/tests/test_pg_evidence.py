"""명시적 근거의 SQL 권위·시간·순환 경계를 일회용 PostgreSQL에서 검증한다."""

from datetime import datetime, timezone
import hashlib
import json
import os
import uuid

import pytest

from agent_knowledge.postgres_store.pgvector_store import MemoryCard, MemoryEdge, PgVectorStore, make_dummy_vector
from agent_knowledge.knowledge_search_service import KnowledgeSearchService, DisabledRetiredIndexBridgeClient
from agent_knowledge.ledger import Ledger
from agent_knowledge.mcp_jsonrpc import handle_jsonrpc_message


PG_DSN = os.environ.get("LBRAIN_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not PG_DSN, reason="전용 PostgreSQL integration DSN 필요")


def digest(text):
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def graph():
    store = PgVectorStore(dsn=PG_DSN)
    store.execute_ddl()
    project = "m4-" + uuid.uuid4().hex[:12]
    ids = {name: f"{project}_{name}" for name in ("a", "b", "c", "disabled", "candidate", "foreign", "future")}
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with store.transaction() as conn:
        for name, memory_id in ids.items():
            store.insert_card(MemoryCard(
                memory_id=memory_id, project=project if name != "foreign" else project + "-other",
                card_type="decision", title="근거", summary="명시적 근거",
                content_hash=digest(memory_id),
                lifecycle_state="candidate" if name == "candidate" else "human_accepted",
                authorization_status="disabled" if name == "disabled" else "active",
                valid_from=start if name != "future" else datetime(2099, 1, 1, tzinfo=timezone.utc),
            ), conn=conn)
    try:
        yield store, project, ids, start
    finally:
        with store.transaction() as conn:
            conn.execute("DELETE FROM memory_edges WHERE src_id = ANY(%s) OR dst_id = ANY(%s)", (list(ids.values()), list(ids.values())))
            conn.execute("DELETE FROM memory_cards WHERE memory_id = ANY(%s)", (list(ids.values()),))


def edge(store, ids, src, dst, start, end=None):
    return store.insert_edge(MemoryEdge(
        src_id=ids[src], dst_id=ids[dst], rel_type="derived_from",
        provenance_hash=digest(src + dst), valid_from=start, valid_to=end,
    ))


def test_sql_evidence_excludes_unauthorized_foreign_and_invalid_nodes(graph):
    store, project, ids, start = graph
    for dst in ("b", "disabled", "candidate", "foreign", "future"):
        edge(store, ids, "a", dst, start)
    edge(store, ids, "a", "c", start, datetime(2026, 2, 1, tzinfo=timezone.utc))
    result = store.read_authorized_evidence(project=project, root_memory_ids=[ids["a"]])
    assert [(row["src_id"], row["dst_id"]) for row in result["edges"]] == [(ids["a"], ids["b"])]
    assert result["edges"][0]["provenance_hash"] == digest("ab")
    assert result["edges"][0]["src_content_hash"] == digest(ids["a"])
    assert result["truncated"] is False
    assert store.read_authorized_evidence(project=project + "-other", root_memory_ids=[ids["a"]])["edges"] == []


def test_sql_evidence_cycle_guard_depth_limit_and_half_open_time(graph):
    store, project, ids, start = graph
    boundary = datetime(2026, 2, 1, tzinfo=timezone.utc)
    edge(store, ids, "a", "a", start)
    edge(store, ids, "a", "b", start)
    edge(store, ids, "b", "c", start, boundary)
    edge(store, ids, "c", "a", start)
    before = store.read_authorized_evidence(project=project, root_memory_ids=[ids["a"]], as_of="2026-01-15")
    assert [row["dst_id"] for row in before["edges"]] == [ids["b"], ids["c"]]
    assert all(len(row["visited_path"]) == len(set(row["visited_path"])) for row in before["edges"])
    at_boundary = store.read_authorized_evidence(project=project, root_memory_ids=[ids["a"]], as_of=boundary)
    assert [row["dst_id"] for row in at_boundary["edges"]] == [ids["b"]]
    limited = store.read_authorized_evidence(project=project, root_memory_ids=[ids["a"]], as_of="2026-01-15", max_depth=1)
    assert len(limited["edges"]) == 1
    assert limited["truncated"] is True
    assert len(store.traverse_provenance_dag(ids["a"], as_of="2026-01-15")) == 2
    with pytest.raises(ValueError):
        store.read_authorized_evidence(project=project, root_memory_ids=[ids["a"]], as_of="not-a-date")
    with pytest.raises(ValueError):
        store.read_authorized_evidence(project=project, root_memory_ids=[ids["a"]], max_depth=6)


def test_sql_evidence_batch_roots_row_limit_and_caller_guc_preserved(graph):
    store, project, ids, start = graph
    edge(store, ids, "a", "b", start)
    edge(store, ids, "b", "c", start)
    with store.transaction() as conn:
        conn.execute("SET LOCAL statement_timeout = '2s'")
        injected = PgVectorStore(connection=conn)
        result = injected.read_authorized_evidence(project=project, root_memory_ids=[ids["a"], ids["b"]], limit=1)
        assert len(result["edges"]) == 1
        assert result["truncated"] is True
        assert conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "2s"


def test_public_with_evidence_contains_real_sql_edge_within_wire_budget(graph, tmp_path):
    store, project, ids, start = graph
    edge(store, ids, "a", "b", start)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    service = KnowledgeSearchService(
        ledger=Ledger(private / "ledger.sqlite"), pgvector_store=store,
        retired_index_bridge=DisabledRetiredIndexBridgeClient(), dataset_ids=[],
    )
    response = handle_jsonrpc_message({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "brain.resolve", "arguments": {
            "project": project, "mode": "list", "limit": 1, "response_mode": "with_evidence",
        }},
    }, service)
    result = response["result"]
    assert result["isError"] is False
    payload = result["structuredContent"]
    assert payload["items"][0]["id"] == ids["a"]
    assert payload["decisions"][0]["decision"] == "명시적 근거"
    assert payload["has_more"] is True
    assert payload["next_cursor"]
    assert payload["evidence"]["explicit_edges"][0]["provenance_hash"] == digest("ab")
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= 3072


def test_explicit_as_of_recalls_superseded_with_current_authorization(graph):
    store, project, ids, start = graph
    edge(store, ids, "a", "b", start)
    vector = make_dummy_vector(9)
    with store.transaction() as conn:
        conn.execute("""UPDATE memory_cards SET currentness='superseded', valid_to='2026-02-01',
                            embedding=%s::halfvec, embedding_state='ready'
                         WHERE memory_id=ANY(%s)""", (json.dumps(vector), [ids["a"], ids["b"]]))
    assert ids["a"] not in {row["memory_id"] for row in store.list_authorized_cards(project=project)}
    historical = store.list_authorized_cards(project=project, as_of="2026-01-15")
    assert ids["a"] in {row["memory_id"] for row in historical}
    ranked = store.hybrid_search(project=project, query_vector=vector, as_of="2026-01-15")
    assert ids["a"] in {row["memory_id"] for row in ranked}
    evidence = store.read_authorized_evidence(project=project, root_memory_ids=[ids["a"]], as_of="2026-01-15")
    assert evidence["edges"][0]["dst_id"] == ids["b"]
    with store.transaction() as conn:
        conn.execute("UPDATE memory_cards SET authorization_status='disabled' WHERE memory_id=%s", (ids["b"],))
    assert store.read_authorized_evidence(project=project, root_memory_ids=[ids["a"]], as_of="2026-01-15")["edges"] == []
