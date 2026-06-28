from __future__ import annotations

import asyncio
import socket
import threading
import time

import httpx
import pytest

# mcp(FastMCP)는 optional extra(mcp-http)다. extra 없이 도는 base `uv run pytest`에서는
# 이 transport 테스트를 건너뛴다(trim 철학: 기본 worker는 mcp를 요구하지 않는다).
pytest.importorskip("mcp")
pytest.importorskip("starlette")

from agent_knowledge import mcp_http_server as mh  # noqa: E402
from agent_knowledge.mcp_server import list_tools  # noqa: E402


@pytest.fixture(autouse=True)
def _default_kubernetes_pod_cidr(monkeypatch):
    monkeypatch.delenv("KUBERNETES_POD_CIDR", raising=False)


# --- 테스트용 stub service (transport 경로만 검증; 실 ledger/graph 불필요) ---


class _StubService:
    """`_call_tool`의 knowledge.search 경로가 호출하는 `.search`만 제공하는 stub."""

    def __init__(self, *, result=None, raises: BaseException | None = None, sleep: float = 0.0):
        self._result = result if result is not None else {"results": []}
        self._raises = raises
        self._sleep = sleep
        self.invalidations = 0
        self.last_kwargs: dict | None = None

    def invalidate_brain_card_cache(self):
        self.invalidations += 1

    def search(self, query, *, filters=None, limit=10, include_private=False):
        self.last_kwargs = {
            "query": query,
            "filters": filters,
            "limit": limit,
            "include_private": include_private,
        }
        if self._sleep:
            time.sleep(self._sleep)
        if self._raises is not None:
            raise self._raises
        return self._result


# --- _to_sdk_tools: 스키마 무변형 매핑 ---


def test_to_sdk_tools_maps_ten_without_mutation():
    sdk_tools = mh._to_sdk_tools()
    source = list_tools()
    assert len(sdk_tools) == 10
    assert {t.name for t in sdk_tools} == {t["name"] for t in source}
    by_name = {t["name"]: t for t in source}
    for tool in sdk_tools:
        # transport가 inputSchema를 변형하지 않음을 증명.
        assert tool.inputSchema == by_name[tool.name]["inputSchema"]


# --- bind 가드 ---


def test_build_app_rejects_zero_host():
    with pytest.raises(ValueError, match="0.0.0.0"):
        mh.build_app(_StubService(), host="0.0.0.0")


def test_build_app_rejects_non_loopback_without_flag():
    with pytest.raises(ValueError, match="loopback"):
        mh.build_app(_StubService(), host="100.64.0.1", allow_non_loopback=False)


def test_build_app_allows_tailnet_with_flag():
    app = mh.build_app(_StubService(), host="100.64.0.1", allow_non_loopback=True)
    paths = {r.path for r in app.routes}
    assert "/healthz" in paths and "/mcp" in paths


def test_build_app_rejects_kubernetes_pod_ip_without_specific_flag():
    with pytest.raises(ValueError, match="Kubernetes Pod CIDR"):
        mh.build_app(_StubService(), host="10.42.0.31", allow_non_loopback=True)


def test_build_app_allows_kubernetes_pod_ip_with_specific_flag():
    app = mh.build_app(
        _StubService(),
        host="10.42.0.31",
        allow_non_loopback=True,
        allow_kubernetes_pod_ip=True,
    )
    paths = {r.path for r in app.routes}
    assert "/healthz" in paths and "/mcp" in paths


def test_build_app_allows_configured_kubernetes_pod_cidr(monkeypatch):
    monkeypatch.setenv("KUBERNETES_POD_CIDR", "10.244.0.0/16, fd00:10:42::/64")

    app = mh.build_app(
        _StubService(),
        host="10.244.3.31",
        allow_non_loopback=True,
        allow_kubernetes_pod_ip=True,
    )

    paths = {r.path for r in app.routes}
    assert "/healthz" in paths and "/mcp" in paths
    assert mh._is_kubernetes_pod_address("fd00:10:42::31") is True
    assert mh._is_kubernetes_pod_address("10.42.0.31") is False


def test_build_app_rejects_invalid_configured_kubernetes_pod_cidr(monkeypatch):
    monkeypatch.setenv("KUBERNETES_POD_CIDR", "not-a-cidr")

    with pytest.raises(ValueError, match="invalid KUBERNETES_POD_CIDR"):
        mh.build_app(
            _StubService(),
            host="10.42.0.31",
            allow_non_loopback=True,
            allow_kubernetes_pod_ip=True,
        )


def test_build_app_allows_whole_loopback_subnet_without_flag():
    app = mh.build_app(_StubService(), host="127.0.0.2", allow_non_loopback=False)
    paths = {r.path for r in app.routes}
    assert "/healthz" in paths and "/mcp" in paths


def test_build_app_rejects_public_ip_even_with_flag():
    # tailnet 밖 공개 IP는 --allow-non-loopback이 있어도 거부(신뢰 경계 = tailnet 전용).
    with pytest.raises(ValueError, match="tailnet or Kubernetes Pod CIDR"):
        mh.build_app(_StubService(), host="8.8.8.8", allow_non_loopback=True)


def test_build_app_rejects_private_lan_ip_even_with_flag():
    with pytest.raises(ValueError, match="tailnet or Kubernetes Pod CIDR"):
        mh.build_app(_StubService(), host="192.168.1.10", allow_non_loopback=True)


def test_build_app_rejects_service_cidr_even_with_kubernetes_pod_flag():
    with pytest.raises(ValueError, match="tailnet or Kubernetes Pod CIDR"):
        mh.build_app(
            _StubService(),
            host="10.43.0.10",
            allow_non_loopback=True,
            allow_kubernetes_pod_ip=True,
        )


def test_is_tailnet_address():
    assert mh._is_tailnet_address("100.64.0.1") is True
    assert mh._is_tailnet_address("100.127.255.254") is True
    assert mh._is_tailnet_address("fd7a:115c:a1e0::1") is True
    assert mh._is_tailnet_address("8.8.8.8") is False
    assert mh._is_tailnet_address("192.168.1.1") is False
    assert mh._is_tailnet_address("example.com") is False


def test_is_kubernetes_pod_address():
    assert mh._is_kubernetes_pod_address("10.42.0.1") is True
    assert mh._is_kubernetes_pod_address("10.42.255.254") is True
    assert mh._is_kubernetes_pod_address("10.43.0.1") is False
    assert mh._is_kubernetes_pod_address("192.168.1.1") is False
    assert mh._is_kubernetes_pod_address("example.com") is False


def test_bracket_ipv6_and_ipv4():
    assert mh._bracket("fd7a:115c:a1e0::1") == "[fd7a:115c:a1e0::1]"
    assert mh._bracket("100.64.0.1") == "100.64.0.1"


def test_transport_security_settings_cover_loopback_aliases():
    settings = mh._transport_security_settings("127.0.0.2", 8765)

    assert settings.enable_dns_rebinding_protection is True
    assert "127.0.0.2" in settings.allowed_hosts
    assert "127.0.0.2:8765" in settings.allowed_hosts
    assert "localhost" in settings.allowed_hosts
    assert "127.0.0.1:8765" in settings.allowed_hosts
    assert "[::1]:8765" in settings.allowed_hosts
    assert "http://localhost:8765" in settings.allowed_origins
    assert "http://[::1]:8765" in settings.allowed_origins


def test_transport_security_settings_cover_tailnet_without_loopback_aliases():
    settings = mh._transport_security_settings("fd7a:115c:a1e0::1", 8765)

    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == ["[fd7a:115c:a1e0::1]", "[fd7a:115c:a1e0::1]:8765"]
    assert settings.allowed_origins == [
        "http://[fd7a:115c:a1e0::1]",
        "http://[fd7a:115c:a1e0::1]:8765",
    ]


def test_build_app_loopback_has_routes():
    app = mh.build_app(_StubService())
    paths = {r.path for r in app.routes}
    assert paths == {"/healthz", "/mcp"}


# --- _healthz: 정적 200, service 미조회 ---


def test_healthz_static_ok():
    resp = asyncio.run(mh._healthz(None))
    assert resp.status_code == 200
    assert resp.body == b'{"status":"ok"}'


# --- _dispatch_call_tool: 위임 + 결과 매핑 ---


def test_dispatch_delegates_and_maps_result():
    stub = _StubService(result={"results": [{"knowledge_id": "k1"}]})
    res = asyncio.run(mh._dispatch_call_tool(stub, "knowledge.search", {"query": "hello"}))
    assert res.isError is False
    # structuredContent는 _call_tool의 _tool_result 출구를 그대로 통과.
    assert res.structuredContent == {"results": [{"knowledge_id": "k1"}]}
    assert res.content[0].type == "text"
    assert "k1" in res.content[0].text


def test_dispatch_passes_include_private_through_to_service():
    # 안전 게이트는 service 안(allow_private_results)에 있고 transport는 인자를 그대로 전달.
    stub = _StubService()
    res = asyncio.run(
        mh._dispatch_call_tool(stub, "knowledge.search", {"query": "q", "include_private": True, "limit": 5})
    )
    assert res.isError is False
    assert stub.last_kwargs["include_private"] is True
    assert stub.last_kwargs["limit"] == 5


# --- _dispatch_call_tool: 오류 마스킹 ---


def test_dispatch_value_error_is_masked_to_type_name():
    # unknown tool -> _call_tool ValueError. raw 메시지(caller-supplied tool명 포함)는
    # 에코하지 않고 type name만 노출(stdio -32602 redaction과 대칭).
    res = asyncio.run(mh._dispatch_call_tool(_StubService(), "no.such.tool", {}))
    assert res.isError is True
    assert res.content[0].text == "invalid params: ValueError"
    # caller 입력(tool 이름)이 응답으로 새지 않음.
    assert "no.such.tool" not in res.content[0].text


def test_dispatch_unexpected_exception_is_masked():
    # 내부 RuntimeError(가짜 private path 포함)는 'internal error'로 마스킹.
    stub = _StubService(raises=RuntimeError("boom at /private/secret/path token=abc"))
    res = asyncio.run(mh._dispatch_call_tool(stub, "knowledge.search", {"query": "q"}))
    assert res.isError is True
    assert res.content[0].text == "internal error"
    assert "private" not in res.content[0].text
    assert "token" not in res.content[0].text


def test_dispatch_unexpected_exception_logs_redacted_stack(caplog):
    caplog.set_level("ERROR", logger="agent_knowledge.mcp_http_server")
    stub = _StubService(raises=RuntimeError("boom at /private/secret/path token=abc"))

    res = asyncio.run(mh._dispatch_call_tool(stub, "knowledge.search", {"query": "q"}))

    assert res.isError is True
    assert "RuntimeError" in caplog.text
    assert "stack=" in caplog.text
    assert "private" not in caplog.text
    assert "token" not in caplog.text


# --- 비블로킹: 동기 _call_tool이 이벤트 루프를 막지 않음 ---


def test_dispatch_does_not_block_event_loop():
    async def _run():
        stub = _StubService(sleep=0.4)
        flag = {"fast_done": False}

        async def racer():
            await asyncio.sleep(0.05)
            flag["fast_done"] = True

        dispatch = asyncio.create_task(
            mh._dispatch_call_tool(stub, "knowledge.search", {"query": "q"})
        )
        await asyncio.create_task(racer())
        # 느린(0.4s) 블로킹 호출이 스레드풀에서 도는 동안 racer(0.05s)가 먼저 끝나야 한다.
        assert flag["fast_done"] is True
        assert not dispatch.done()
        res = await dispatch
        assert res.isError is False

    asyncio.run(_run())


# --- 통합: 실 HTTP round-trip (uvicorn 스레드 + mcp 클라이언트) ---


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ServerThread:
    def __init__(self, app, port: int):
        import uvicorn

        self._config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._port = port

    def __enter__(self):
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


@pytest.fixture()
def http_base():
    port = _free_port()
    app = mh.build_app(_StubService(result={"results": [{"knowledge_id": "kx"}]}), port=port)
    with _ServerThread(app, port) as base:
        yield base


def test_healthz_over_real_http(http_base):
    resp = httpx.get(f"{http_base}/healthz", timeout=2)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_initialize_list_and_call_over_http(http_base):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def _roundtrip():
        async with streamablehttp_client(f"{http_base}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                called = await session.call_tool("knowledge.search", {"query": "hello"})
                return tools, called

    tools, called = asyncio.run(_roundtrip())
    assert len(tools.tools) == 10
    assert "knowledge.search" in {t.name for t in tools.tools}
    assert called.isError is False
    assert called.structuredContent == {"results": [{"knowledge_id": "kx"}]}


def test_http_call_refreshes_brain_card_cache_per_request():
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    stub = _StubService(result={"results": [{"knowledge_id": "fresh"}]})
    port = _free_port()
    app = mh.build_app(stub, port=port)

    async def _roundtrip(base):
        async with streamablehttp_client(f"{base}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.call_tool("knowledge.search", {"query": "first"})
                await session.call_tool("knowledge.search", {"query": "second"})

    with _ServerThread(app, port) as base:
        asyncio.run(_roundtrip(base))

    assert stub.invalidations == 2


def test_stateless_two_independent_sessions(http_base):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def _one():
        async with streamablehttp_client(f"{http_base}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return (await session.list_tools()).tools

    async def _both():
        first = await _one()
        second = await _one()
        return first, second

    first, second = asyncio.run(_both())
    assert len(first) == 10 and len(second) == 10
