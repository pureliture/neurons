"""인증된 실제 MCP HTTP → 실제 isolated PG → 새 공개 HTTP 세션 수용 검사.

공식 MCP ClientSession/Streamable HTTP SDK와 loopback uvicorn만 사용한다.
fixture DSN 밖의 DB·모델·그래프·런타임 서비스에 연결하지 않는다.
이 모듈의 실제 실행은 부모의 보호된 PG runner에 맡긴다.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
import json
import secrets
import time

import httpx
import pytest

# 기존 transport 검사와 같은 optional-extra 경계. DB opt-in은 conftest가 소유한다.
pytest.importorskip("mcp")
pytest.importorskip("starlette")
pytest.importorskip("uvicorn")

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from agent_knowledge import mcp_http_server  # noqa: E402
from agent_knowledge.knowledge_search_service import (  # noqa: E402
    DisabledRetiredIndexBridgeClient,
    KnowledgeSearchService,
)
from agent_knowledge.postgres_store.pgvector_store import PgVectorStore  # noqa: E402
from test_neuron_mcp_http import _ServerThread, _free_port  # noqa: E402
from test_pg_steward_authority import NoCardLedger  # noqa: E402
from test_review_bundle_pg_acceptance import (  # noqa: E402
    PROJECT,
    TEST_ACTOR,
    _assert_decided_rows,
    _assert_pending,
    _candidate,
    _db_snapshot,
    _read_arguments,
    _request,
    _service,
)


READ_TOOL = "memory_review_bundle_read"
COMMIT_TOOL = "memory_review_bundle_commit"
PUBLIC_TOOLS = {"brain.resolve", "memory_candidate_create"}


@pytest.fixture(autouse=True)
def _clean_transport_environment(monkeypatch):
    monkeypatch.delenv("KUBERNETES_POD_CIDR", raising=False)
    monkeypatch.delenv("MCP_HTTP_ALLOWED_HOSTS", raising=False)


def _knowledge_service(store, *, allowed=False, read_only=False):
    # 실제 KnowledgeSearchService/PG read model을 연결한다. 서비스 seam 대역 없음.
    # NoCardLedger는 transport permission만 제공하고 SQLite 카드 접근을 실패시킨다.
    ledger = NoCardLedger()
    ledger.read_only = read_only
    return KnowledgeSearchService(
        ledger=ledger,
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        pgvector_store=store,
        graph_adapter=None,
        allow_restricted_steward=allowed,
        allow_steward_auto_accept=False,
        allow_local_test_object_authority_writes=False,
        allow_production_object_authority_writes=False,
    )


@contextmanager
def _http_server(service, *, surface="agent", bearer=None):
    port = _free_port()
    app = mcp_http_server.build_app(
        service, host="127.0.0.1", port=port, surface=surface,
        admin_token=bearer, stateless_http=True,
    )
    server = _ServerThread(app, port)
    try:
        with server as base:
            yield base
    finally:
        # 기존 helper의 start timeout 경로도 소유 thread를 남기지 않는다.
        server._server.should_exit = True
        if server._thread.ident is not None:
            server._thread.join(timeout=10)
        assert not server._thread.is_alive(), "test-owned MCP HTTP server did not stop"


@asynccontextmanager
async def _mcp_session(base, *, bearer=None):
    headers = {} if bearer is None else {"Authorization": f"Bearer {bearer}"}
    # ambient proxy/자격을 사용하지 않고 loopback 요청만 보낸다.
    async with httpx.AsyncClient(headers=headers, trust_env=False, timeout=httpx.Timeout(10.0)) as client:
        async with streamable_http_client(f"{base}/mcp", http_client=client) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session:
                await session.initialize()
                yield session


def _payload(result):
    # snapshot/bearer가 pytest assertion 출력에 섞이지 않게 raw 결과를 에코하지 않는다.
    if result.isError:
        raise AssertionError("MCP tool returned an error")
    payload = result.structuredContent
    if not isinstance(payload, dict):
        raise AssertionError("MCP tool did not return structured content")
    return payload


def _assert_invalid_params(result):
    assert result.isError is True
    assert result.structuredContent == {"error_code": -32602, "error_type": "ValueError"}
    assert [block.text for block in result.content if block.type == "text"] == ["invalid params: ValueError"]


def _assert_http_auth_denied(base):
    with httpx.Client(trust_env=False, timeout=3.0) as client:
        for method in ("GET", "POST", "DELETE"):
            response = client.request(method, f"{base}/mcp")
            assert response.status_code == 401
            assert response.json() == {"error": "unauthorized"}
        response = client.post(f"{base}/mcp", headers={"Authorization": f"Bearer {secrets.token_hex(32)}"})
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}
        assert client.get(f"{base}/healthz").status_code == 200


async def _collect_context(session, *, expected_ids, timeout=15.0):
    """모든 cursor page를 읽고 PG/호스트 시계 차이는 정해진 시간 안에서만 기다린다."""
    expected_ids = set(expected_ids)
    deadline = time.monotonic() + timeout
    while True:
        items = []
        cursor = None
        cursors = set()
        for _ in range(10):
            arguments = {"project": PROJECT, "mode": "context", "query": "", "limit": 1}
            if cursor:
                arguments["cursor"] = cursor
            result = await session.call_tool("brain.resolve", arguments)
            payload = _payload(result)
            assert payload["metadata"]["authority_join_status"] == "verified"
            # 공개 wire budget에는 JSON text와 structuredContent가 함께 포함된다.
            wire = {
                "content": [block.model_dump(exclude_none=True) for block in result.content],
                "structuredContent": payload,
                "isError": result.isError,
            }
            assert len(json.dumps(wire, ensure_ascii=False, separators=(",", ":")).encode()) <= 3072
            page_items = payload["items"]
            assert not {item["id"] for item in page_items}.intersection(item["id"] for item in items)
            items.extend(page_items)
            cursor = payload.get("next_cursor")
            assert payload["has_more"] is bool(cursor)
            if not cursor:
                break
            assert cursor not in cursors, "context cursor repeated"
            cursors.add(cursor)
        else:
            raise AssertionError("context cursor did not finish within the page bound")
        seen = [item["id"] for item in items]
        assert len(seen) == len(set(seen)), "context pages contain duplicate authority"
        if set(seen) == expected_ids:
            return items
        # 예기치 않은 권위나 부분 결과를 성공으로 취급하지 않는다.
        if set(seen) - expected_ids or time.monotonic() >= deadline:
            raise AssertionError("fresh public context did not match the committed member set")
        await asyncio.sleep(0.1)


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_authenticated_http_bulk_decision_is_durable_and_new_public_session_reads_pg(isolated_pg_store, action):
    store = isolated_pg_store
    admin_service = _knowledge_service(store, allowed=True)
    author = admin_service.brain_steward()
    ids = [_candidate(author, "http-first"), _candidate(author, "http-second")]
    before = _db_snapshot(store)
    # 실행마다 임시 인증값을 만들며 파일·출력·snapshot에 넣지 않는다.
    bearer = secrets.token_hex(32)

    async def admin_roundtrip(base):
        async with _mcp_session(base, bearer=bearer) as session:
            tools = await session.list_tools()
            assert {READ_TOOL, COMMIT_TOOL}.issubset({tool.name for tool in tools.tools})
            bundle = _payload(await session.call_tool(READ_TOOL, _read_arguments(list(reversed(ids)))))
            assert bundle["schema_version"] == "brain_steward_review_bundle.v1"
            assert bundle["count"] == 2 and bundle["member_ids"] == sorted(ids)
            assert bundle["write_performed"] is False and bundle["context_complete"] is True
            assert _db_snapshot(PgVectorStore(dsn=store.dsn)) == before
            arguments = {
                **_read_arguments(bundle["member_ids"]), "snapshot_token": bundle["snapshot_token"],
                "action": action, "actor": TEST_ACTOR, "decision_id": f"http-bulk-{action}",
                "reason": "Synthetic human explicitly reviewed both members",
            }
            # Bearer는 admin transport gate, actor는 합성 advisory label이다.
            result = _payload(await session.call_tool(COMMIT_TOOL, arguments))
            assert result["schema_version"] == "brain_steward_review_bundle_decision.v1"
            assert result["actor"] == TEST_ACTOR and result["action"] == action
            assert result["count"] == 2 and result["canonical_write_performed"] is True
            assert _payload(await session.call_tool(COMMIT_TOOL, arguments)) == result
            return result

    with _http_server(admin_service, surface="admin", bearer=bearer) as base:
        _assert_http_auth_denied(base)
        assert _db_snapshot(store) == before
        result = asyncio.run(admin_roundtrip(base))
    fresh_store = PgVectorStore(dsn=store.dsn)
    committed = _assert_decided_rows(fresh_store, ids, result, action=action)
    # 서비스·PG 연결·SDK 세션·HTTP server를 모두 새로 만든 공개 읽기 경로.
    public_service = _knowledge_service(fresh_store)

    async def public_roundtrip(base):
        async with _mcp_session(base) as session:
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == PUBLIC_TOOLS
            return await _collect_context(session, expected_ids=ids if action == "approve" else [])

    with _http_server(public_service) as base:
        recalled = asyncio.run(public_roundtrip(base))
    assert {item["id"] for item in recalled} == (set(ids) if action == "approve" else set())
    assert _db_snapshot(fresh_store) == committed


@pytest.mark.parametrize("boundary", ["default-denied", "read-only", "wrong-project", "wrong-brain-id", "wrong-scope"])
def test_authenticated_admin_cannot_bypass_manual_gate_writability_or_exact_scope(isolated_pg_store, boundary):
    store = isolated_pg_store
    author = _service(store)
    ids = [_candidate(author, "http-denied-first"), _candidate(author, "http-denied-second")]
    service = _knowledge_service(store, allowed=boundary != "default-denied", read_only=boundary == "read-only")
    before = _db_snapshot(store)
    bearer = secrets.token_hex(32)

    async def denied_roundtrip(base):
        async with _mcp_session(base, bearer=bearer) as session:
            bundle = _payload(await session.call_tool(READ_TOOL, _read_arguments(ids)))
            assert bundle["count"] == 2 and bundle["write_performed"] is False
            arguments = {**_read_arguments(ids), "snapshot_token": bundle["snapshot_token"],
                         "action": "approve", "actor": TEST_ACTOR, "decision_id": "http-denied", "reason": ""}
            if boundary == "wrong-project":
                arguments["project"] = "other-project"
            elif boundary == "wrong-brain-id":
                arguments["brain_id"] = "/project/other-brain"
            elif boundary == "wrong-scope":
                arguments["scope"] = "workspace"
            result = await session.call_tool(COMMIT_TOOL, arguments)
            if boundary == "default-denied":
                assert _payload(result) == {
                    "schema_version": "brain_steward_restricted_denied.v1", "tool": COMMIT_TOOL,
                    "permission": "denied", "reason": "restricted_tool_requires_human_gate",
                    "write_performed": False, "authoritative_memory_changed": False,
                }
            else:
                _assert_invalid_params(result)
            reread = _payload(await session.call_tool(READ_TOOL, _read_arguments(ids)))
            assert reread["count"] == 2
            assert all(item["lifecycle_state"] == "candidate" for item in reread["items"])

    with _http_server(service, surface="admin", bearer=bearer) as base:
        asyncio.run(denied_roundtrip(base))
    assert _db_snapshot(PgVectorStore(dsn=store.dsn)) == before
    _assert_pending(store, ids)


@pytest.mark.parametrize("tool_name", [READ_TOOL, COMMIT_TOOL])
def test_public_http_has_exact_two_tools_and_cannot_call_review_bundle(isolated_pg_store, tool_name):
    store = isolated_pg_store
    author = _service(store)
    ids = [_candidate(author, "public-denial-first"), _candidate(author, "public-denial-second")]
    # 유효한 실제 PG token과 writable service capability도 public registry를 우회하지 못한다.
    arguments = _read_arguments(ids) if tool_name == READ_TOOL else _request(author, ids, decision_id="public-must-not-commit")
    public_service = _knowledge_service(store, allowed=True)
    before = _db_snapshot(store)

    async def public_roundtrip(base):
        async with _mcp_session(base) as session:
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == PUBLIC_TOOLS
            assert all(tool.name not in {READ_TOOL, COMMIT_TOOL} for tool in tools.tools)
            _assert_invalid_params(await session.call_tool(tool_name, arguments))
            assert await _collect_context(session, expected_ids=[]) == []

    with _http_server(public_service) as base:
        asyncio.run(public_roundtrip(base))
    assert _db_snapshot(PgVectorStore(dsn=store.dsn)) == before
    _assert_pending(store, ids)


def test_authenticated_http_rejects_snapshot_after_separate_human_changes_current_context(isolated_pg_store):
    store = isolated_pg_store
    service = _knowledge_service(store, allowed=True)
    author = service.brain_steward()
    ids = [_candidate(author, "http-context-first"), _candidate(author, "http-context-second")]
    bearer = secrets.token_hex(32)

    async def changed_context_roundtrip(base):
        async with _mcp_session(base, bearer=bearer) as session:
            bundle = _payload(await session.call_tool(READ_TOOL, _read_arguments(ids)))
            extra = _candidate(author, "http-separate-current")
            author.candidate_approve(candidate_memory_id=extra, approved_by=TEST_ACTOR, decision_id="http-separate-human")
            after_separate_change = _db_snapshot(PgVectorStore(dsn=store.dsn))
            arguments = {**_read_arguments(ids), "snapshot_token": bundle["snapshot_token"],
                         "action": "approve", "actor": TEST_ACTOR, "decision_id": "http-stale-context", "reason": ""}
            _assert_invalid_params(await session.call_tool(COMMIT_TOOL, arguments))
            assert _db_snapshot(PgVectorStore(dsn=store.dsn)) == after_separate_change
            return extra

    with _http_server(service, surface="admin", bearer=bearer) as base:
        extra = asyncio.run(changed_context_roundtrip(base))
    _assert_pending(store, ids)
    state = _db_snapshot(PgVectorStore(dsn=store.dsn))
    assert len(state["steward_card_decisions"]) == 1 and not state["steward_bundle_decisions"]
    assert state["steward_card_decisions"][0]["memory_id"] == extra
    assert len(state["graph_projection_outbox"]) == len(state["embedding_outbox"]) == 1
