"""Streamable HTTP transport for the neuron-knowledge MCP surface.

기존 stdio transport(`run_stdio_server`)는 그대로 두고, MCP 공식 SDK(mcp)의
Streamable HTTP transport를 추가한다. tool 선언/디스패치/안전 기본값은 MCP JSON-RPC
단일 seam(`list_tools` / `dispatch_tool_call`)을 재사용하고, 이 모듈은 transport만 담당한다.

설치-검증 게이트(mcp==1.28.0 실측):
- low-level `Server`에는 `streamable_http_app`/생성자 `on_call_tool` kwarg가 없다.
  → `StreamableHTTPSessionManager`로 transport를 배선하고 핸들러는 데코레이터
    (`@server.list_tools()` / `@server.call_tool()`)로 등록한다.
- `Tool` 필드명은 `inputSchema`(camelCase), `CallToolResult`는 `structuredContent`/
  `isError`(camelCase)이며 그대로 수용된다.
- `@server.call_tool(validate_input=False)`: 입력 검증을 SDK가 아닌 기존 dispatcher
  단일 validator에 맡겨 stdio 경로와 동작을 동일하게 유지한다(transport만 추가).

읽기 전용 recall 전용. YAGNI: 중앙/sync, bearer 인증, event_store(재전송)는 미구현.
"""

from __future__ import annotations

import ipaddress
import logging
import traceback
from typing import Any

from mcp import types as mcp_types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Route

from .knowledge_search_service import KnowledgeSearchService
from .mcp_jsonrpc import dispatch_tool_call
from .mcp_tools import list_tools

DEFAULT_PORT = 8765
_LOGGER = logging.getLogger(__name__)

# Tailscale tailnet 대역: IPv4 CGNAT 100.64.0.0/10, IPv6 ULA fd7a:115c:a1e0::/48.
# 신뢰 경계 = tailnet 전용이므로 비-loopback bind는 이 대역만 허용한다(v1 앱 token 없음 →
# 네트워크 계층이 유일 방어선이라 공개/사설 IP 오설정 노출을 코드레벨로 차단).
_TAILNET_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)


def _is_tailnet_address(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in _TAILNET_NETWORKS)


def _bracket(host: str) -> str:
    # IPv6 리터럴은 Host/Origin authority에서 [..]로 감싼다.
    try:
        if ipaddress.ip_address(host).version == 6:
            return f"[{host}]"
    except ValueError:
        pass
    return host


def _is_loopback_address(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _transport_security_settings(host: str, port: int) -> TransportSecuritySettings:
    authority = f"{_bracket(host)}:{port}"
    allowed_hosts = [_bracket(host), authority]
    allowed_origins = [f"http://{_bracket(host)}", f"http://{authority}"]
    if _is_loopback_address(host):
        loopback_hosts = [
            "localhost",
            f"localhost:{port}",
            "127.0.0.1",
            f"127.0.0.1:{port}",
            "[::1]",
            f"[::1]:{port}",
        ]
        allowed_hosts.extend(alias for alias in loopback_hosts if alias not in allowed_hosts)
        allowed_origins.extend(
            f"http://{alias}" for alias in loopback_hosts if f"http://{alias}" not in allowed_origins
        )
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def _redacted_traceback(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    parts = []
    for frame in frames[-6:]:
        filename = frame.filename.replace("\\", "/").rsplit("/", 1)[-1]
        parts.append(f"{filename}:{frame.name}:{frame.lineno}")
    return " > ".join(parts)


def _to_sdk_tools() -> list[mcp_types.Tool]:
    # list_tools() 리터럴의 inputSchema는 이미 MCP JSON Schema 형식이라 직매핑(변형 없음).
    return [
        mcp_types.Tool(
            name=tool["name"],
            description=tool["description"],
            inputSchema=tool["inputSchema"],
        )
        for tool in list_tools()
    ]


class _StreamableHTTPASGIApp:
    """`session_manager.handle_request`를 Starlette Route endpoint로 노출하는 얇은 ASGI
    래퍼. FastMCP 내부 동형 클래스를 private import하지 않기 위한 재현."""

    def __init__(self, session_manager: StreamableHTTPSessionManager) -> None:
        self._session_manager = session_manager

    async def __call__(self, scope, receive, send) -> None:
        await self._session_manager.handle_request(scope, receive, send)


async def _dispatch_call_tool(
    service: KnowledgeSearchService, name: str, arguments: dict[str, Any] | None
) -> mcp_types.CallToolResult:
    """tool 호출의 transport 측 단일 처리부. 동기 dispatcher를 워커 스레드로 위임하고
    (이벤트 루프 비블로킹) 결과/오류를 `CallToolResult`로 매핑한다."""
    args = {"name": name, "arguments": arguments or {}}
    try:
        # graph 경로가 최대 300s 블로킹(graphiti_adapter)해도 루프/healthz/동시 요청 무영향.
        # ledger 메서드는 per-call sqlite3 connection을 열어 스레드풀 실행이 안전하다.
        result = await run_in_threadpool(dispatch_tool_call, args, service)
    except (ValueError, TypeError) as exc:
        # raw 메시지는 caller-supplied 인자값/private context를 담을 수 있으므로 에코하지 않는다.
        # stdio handle_jsonrpc_message(-32602)와 동일하게 type name만 노출(redaction 대칭).
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=f"invalid params: {type(exc).__name__}")],
            isError=True,
        )
    except Exception as exc:
        # 예기치 못한 내부 예외(graph adapter stack 등)는 마스킹 = handle_jsonrpc_message의
        # -32603 정책을 HTTP 경로에 재현. private path/token/raw id/stack 비노출(CLAUDE.md).
        _LOGGER.error(
            "unexpected mcp-http tool execution error: %s stack=%s",
            type(exc).__name__,
            _redacted_traceback(exc),
        )
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="internal error")],
            isError=True,
        )
    content = [
        mcp_types.TextContent(type="text", text=block["text"]) for block in result["content"]
    ]
    return mcp_types.CallToolResult(
        content=content,
        structuredContent=result.get("structuredContent"),
        isError=False,
    )


async def _healthz(_request):
    # service/ledger/graph 미조회 정적 200 = plane 분리. HTTP listen 생존만 검증.
    return JSONResponse({"status": "ok"})


def build_app(
    service: KnowledgeSearchService,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    allow_non_loopback: bool = False,
    stateless_http: bool = True,
) -> Starlette:
    # bind 가드: 0.0.0.0은 무조건 거부(전 인터페이스 노출 차단). 비-loopback은
    # --allow-non-loopback + tailnet 대역일 때만 허용(공개/사설 IP 오설정 노출 차단).
    if host == "0.0.0.0":  # noqa: S104 - 명시적 거부 가드
        raise ValueError("mcp-http refuses 0.0.0.0 bind")
    is_loopback = _is_loopback_address(host)
    if not is_loopback:
        if not allow_non_loopback:
            raise ValueError("mcp-http must bind loopback unless --allow-non-loopback is set")
        if not _is_tailnet_address(host):
            raise ValueError(
                "mcp-http non-loopback bind must be a Tailscale tailnet address "
                "(100.64.0.0/10 or fd7a:115c:a1e0::/48)"
            )

    server: Server = Server("neurons")

    @server.list_tools()
    async def _handle_list_tools() -> list[mcp_types.Tool]:
        return _to_sdk_tools()

    @server.call_tool(validate_input=False)
    async def _handle_call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> mcp_types.CallToolResult:
        service.invalidate_brain_card_cache()
        return await _dispatch_call_tool(service, name, arguments)

    # DNS rebinding 보호는 loopback/tailnet 모두 활성화한다.
    security_settings = _transport_security_settings(host, port)

    session_manager = StreamableHTTPSessionManager(
        app=server,
        event_store=None,  # YAGNI: 재전송 불필요(read-only recall)
        json_response=True,  # 단일 JSON 응답(SSE 스트리밍 불필요)
        stateless=stateless_http,
        security_settings=security_settings,
    )

    routes = [
        Route("/healthz", endpoint=_healthz, methods=["GET"]),
        Route("/mcp", endpoint=_StreamableHTTPASGIApp(session_manager)),
    ]

    # session_manager.run()은 transport 수명 컨텍스트. Starlette lifespan으로 구동한다.
    return Starlette(routes=routes, lifespan=lambda _app: session_manager.run())


def serve(
    service: KnowledgeSearchService,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    allow_non_loopback: bool = False,
) -> None:
    import uvicorn

    app = build_app(
        service,
        host=host,
        port=port,
        allow_non_loopback=allow_non_loopback,
        stateless_http=True,
    )
    uvicorn.run(app, host=host, port=int(port), log_level="warning")
