from __future__ import annotations

import os
from pathlib import Path

from agent_knowledge.ledger import Ledger
from agent_knowledge.mcp_jsonrpc import (
    REVIEW_READER_AUTH_IDENTITY,
    dispatch_tool_call,
    handle_jsonrpc_message,
)
from agent_knowledge.mcp_tools import (
    MEMORY_CANDIDATE_APPROVE_TOOL_NAME,
    MEMORY_CANDIDATE_REJECT_TOOL_NAME,
    MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME,
    REVIEW_READER_TOOL_NAMES,
    list_tools,
)
from agent_knowledge.knowledge_search_service import (
    DisabledRetiredIndexBridgeClient,
    KnowledgeSearchService,
)


def _service(tmp_path: Path) -> KnowledgeSearchService:
    private = tmp_path / "private"
    private.mkdir(parents=True, exist_ok=True)
    os.chmod(private, 0o700)
    return KnowledgeSearchService(
        ledger=Ledger(private / "ledger.sqlite"),
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        allow_private_results=True,
    )


def test_review_reader_tools_list_serves_only_queue_list():
    tools = list_tools(surface="review-reader")
    assert {t["name"] for t in tools} == {MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME}
    assert {t["name"] for t in tools} == REVIEW_READER_TOOL_NAMES


def test_review_reader_rejects_unauthenticated_requests(tmp_path: Path):
    service = _service(tmp_path)

    for method, params in (
        ("tools/list", None),
        (
            "tools/call",
            {"name": MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME, "arguments": {"project": "proj-a"}},
        ),
    ):
        message = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            message["params"] = params
        resp = handle_jsonrpc_message(message, service, surface="review-reader", auth_token=None)
        assert resp["error"]["code"] == -32000
        assert "unauthorized" in resp["error"]["message"]

        resp_bad = handle_jsonrpc_message(
            message, service, surface="review-reader", auth_token="wrong-token"
        )
        assert resp_bad["error"]["code"] == -32000

    # dispatch_tool_call direct path also fails closed without the reader identity
    try:
        dispatch_tool_call(
            {"name": MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME, "arguments": {"project": "proj-a"}},
            service,
            surface="review-reader",
            auth_token=None,
        )
    except PermissionError:
        pass
    else:
        raise AssertionError("dispatch_tool_call must raise PermissionError without auth")


def test_review_reader_blocks_approve_and_reject_even_with_reader_identity(tmp_path: Path):
    service = _service(tmp_path)

    # Via handle_jsonrpc_message direct JSON-RPC
    for tool_name, args in (
        (
            MEMORY_CANDIDATE_APPROVE_TOOL_NAME,
            {"candidate_memory_id": "x", "approved_by": "r", "decision_id": "d"},
        ),
        (
            MEMORY_CANDIDATE_REJECT_TOOL_NAME,
            {"candidate_memory_id": "x", "rejected_by": "r", "decision_id": "d", "reason": "no"},
        ),
    ):
        resp = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": args},
            },
            service,
            surface="review-reader",
            auth_token=REVIEW_READER_AUTH_IDENTITY,
        )
        assert resp["error"]["code"] == -32601
        assert f"unknown tool: {tool_name}" in resp["error"]["message"]

    # Via dispatch_tool_call direct JSON-RPC-equivalent path
    for tool_name in (MEMORY_CANDIDATE_APPROVE_TOOL_NAME, MEMORY_CANDIDATE_REJECT_TOOL_NAME):
        try:
            dispatch_tool_call(
                {"name": tool_name, "arguments": {}},
                service,
                surface="review-reader",
                auth_token=REVIEW_READER_AUTH_IDENTITY,
            )
        except ValueError as exc:
            assert f"unknown tool: {tool_name}" in str(exc)
        else:
            raise AssertionError(f"{tool_name} must be rejected on review-reader")


def test_review_reader_authenticated_queue_list_succeeds(tmp_path: Path, isolated_pg_store):
    service = _service(tmp_path)
    service.pgvector_store = isolated_pg_store
    steward = service.brain_steward()
    steward.candidate_create(
        source_span={
            "card_type": "decision",
            "project": "proj-reader",
            "title": "Reader Test Card",
            "summary": "Reader card summary",
            "content_hash": "sha256:" + "9" * 64,
            "typed_payload": {
                "decision": "test",
                "rationale": "test",
                "alternatives": [],
                "consequence": "none",
                "authority_ref": "ref",
            },
        },
        proposer="codex",
    )

    resp = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME,
                "arguments": {"project": "proj-reader"},
            },
        },
        service,
        surface="review-reader",
        auth_token=REVIEW_READER_AUTH_IDENTITY,
    )
    assert "error" not in resp
    items = resp["result"]["structuredContent"]["items"]
    assert len(items) == 1
    assert items[0]["title"] == "Reader Test Card"


def test_admin_dispatch_requires_auth_for_every_tool(tmp_path: Path):
    """Unauthenticated direct admin dispatch is denied for every admin tool."""
    from agent_knowledge.mcp_tools import ADMIN_TOOL_NAMES

    service = _service(tmp_path)
    for tool_name in sorted(ADMIN_TOOL_NAMES):
        try:
            dispatch_tool_call(
                {"name": tool_name, "arguments": {}},
                service,
                surface="admin",
                auth_token=None,
            )
        except PermissionError as exc:
            assert "lbrain_admin" in str(exc)
        else:
            raise AssertionError(f"admin dispatch without auth must deny {tool_name}")

        try:
            dispatch_tool_call(
                {"name": tool_name, "arguments": {}},
                service,
                surface="admin",
                auth_token="wrong-token",
            )
        except PermissionError:
            pass
        else:
            raise AssertionError(f"admin dispatch with wrong auth must deny {tool_name}")


# --- Real ASGI/HTTP round-trips (fake isolated service, no PG) ---

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")

from agent_knowledge import mcp_http_server as _mh  # noqa: E402

class _FakeSteward:
    def review_queue_list(self, *, project="", limit=20):
        return {
            "schema_version": "brain_steward_review_queue.v1",
            "project": project,
            "count": 1,
            "items": [{"memory_id": "x", "title": "Reader Test Card"}],
        }


class _FakeService:
    def __init__(self):
        self.invalidations = 0

    def invalidate_brain_card_cache(self):
        self.invalidations += 1

    def brain_steward(self):
        return _FakeSteward()


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ServerThread:
    def __init__(self, app, port: int):
        import uvicorn

        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        self._thread = __import__("threading").Thread(target=self._server.run, daemon=True)
        self._port = port

    def __enter__(self):
        import time

        import httpx

        self._thread.start()
        base = f"http://127.0.0.1:{self._port}"
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


def _post_jsonrpc(base: str, token: str | None, *, method="tools/call", tool=MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME):
    import httpx

    if method == "tools/list":
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    else:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {"project": "reader-test", "limit": 5}},
        }
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return httpx.post(f"{base}/mcp", headers=headers, json=payload, timeout=5)


def test_review_reader_http_requires_bearer_and_serves_only_queue_list():
    token = "reader-token-with-at-least-thirty-two-chars"
    port = _free_port()
    app = _mh.build_app(_FakeService(), port=port, surface="review-reader", admin_token=token)
    with _ServerThread(app, port) as base:
        unauth = _post_jsonrpc(base, None)
        wrong = _post_jsonrpc(base, "wrong")
        ok_list = _post_jsonrpc(base, token, method="tools/list")
        ok_call = _post_jsonrpc(base, token)
        approve = _post_jsonrpc(base, token, tool=MEMORY_CANDIDATE_APPROVE_TOOL_NAME)
        reject = _post_jsonrpc(base, token, tool=MEMORY_CANDIDATE_REJECT_TOOL_NAME)
    assert unauth.status_code == 401
    assert wrong.status_code == 401
    assert ok_list.status_code == 200
    assert {t["name"] for t in ok_list.json()["result"]["tools"]} == {MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME}
    assert ok_call.status_code == 200
    assert ok_call.json()["result"]["structuredContent"]["items"][0]["title"] == "Reader Test Card"
    for blocked in (approve, reject):
        assert blocked.status_code == 200
        assert blocked.json()["result"]["isError"] is True


def test_admin_http_requires_bearer_and_rejects_wrong_token():
    token = "admin-token-with-at-least-thirty-two-characters"
    port = _free_port()
    app = _mh.build_app(_FakeService(), port=port, surface="admin", admin_token=token)
    with _ServerThread(app, port) as base:
        unauth = _post_jsonrpc(base, None)
        wrong = _post_jsonrpc(base, "wrong")
        ok = _post_jsonrpc(base, token)
    assert unauth.status_code == 401
    assert wrong.status_code == 401
    assert ok.status_code == 200
    assert ok.json()["result"]["structuredContent"]["items"][0]["title"] == "Reader Test Card"
