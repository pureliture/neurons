from __future__ import annotations
import os
from pathlib import Path

from agent_knowledge.ledger import Ledger
from agent_knowledge.mcp_server import (
    TOOL_NAME,
    DisabledRagflowClient,
    KnowledgeSearchService,
    handle_jsonrpc_message,
)


def _ledger(tmp_path: Path) -> Ledger:
    private = tmp_path / "private"
    private.mkdir(parents=True, exist_ok=True)
    os.chmod(private, 0o700)
    return Ledger(private / "ledger.sqlite")


def _service(tmp_path: Path) -> KnowledgeSearchService:
    ledger = _ledger(tmp_path)
    return KnowledgeSearchService(
        ledger=ledger,
        ragflow=DisabledRagflowClient(),
        dataset_ids=[],
        allow_private_results=True,
    )


def test_adversarial_query_none(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": TOOL_NAME,
                "arguments": {"query": None},
            },
        },
        service,
    )
    assert response["error"]["code"] == -32602
    assert "ValueError" in response["error"]["message"]


def test_adversarial_query_empty(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": TOOL_NAME,
                "arguments": {"query": "   "},
            },
        },
        service,
    )
    assert response["error"]["code"] == -32602
    assert "ValueError" in response["error"]["message"]


def test_adversarial_filters_invalid_type(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": TOOL_NAME,
                "arguments": {"query": "hello", "filters": "not-a-dict"},
            },
        },
        service,
    )
    assert response["error"]["code"] == -32602
    assert "ValueError" in response["error"]["message"]


def test_adversarial_limit_invalid_string(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": TOOL_NAME,
                "arguments": {"query": "hello", "limit": "abc"},
            },
        },
        service,
    )
    assert response["error"]["code"] == -32602
    assert "ValueError" in response["error"]["message"]


def test_adversarial_limit_none(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": TOOL_NAME,
                "arguments": {"query": "hello", "limit": None},
            },
        },
        service,
    )
    assert response["error"]["code"] == -32602
    assert "TypeError" in response["error"]["message"]


def test_adversarial_limit_negative(tmp_path: Path):
    # limit이 음수인 경우, ValueError/TypeError를 던지지 않고
    # 내부적으로 bounds 처리가 정상적으로 되어 결과 반환 성공(빈 리스트)해야 함.
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": TOOL_NAME,
                "arguments": {"query": "hello", "limit": -5},
            },
        },
        service,
    )
    assert "error" not in response
    assert response["result"]["structuredContent"]["results"] == []


def test_adversarial_limit_large(tmp_path: Path):
    # limit이 매우 큰 경우에도 에러 없이 정상적으로 10으로 바인딩되어 성공해야 함.
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": TOOL_NAME,
                "arguments": {"query": "hello", "limit": 100},
            },
        },
        service,
    )
    assert "error" not in response
    assert response["result"]["structuredContent"]["results"] == []
