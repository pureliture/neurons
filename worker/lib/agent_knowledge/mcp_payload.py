"""MCP structured/text 표현과 실제 tool-result 바이트 계산의 공통 경계."""

import json
from typing import Any


def tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": bool(payload.get("error_code")),
    }


def tool_result_bytes(payload: dict[str, Any]) -> int:
    return len(json.dumps(tool_result(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
