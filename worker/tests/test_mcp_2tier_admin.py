from __future__ import annotations

import os
from pathlib import Path

import pytest
from agent_knowledge.ledger import Ledger
from agent_knowledge.mcp_jsonrpc import (
    ADMIN_AUTH_IDENTITY,
    handle_admin_jsonrpc_message,
    handle_jsonrpc_message,
)
from agent_knowledge.mcp_tools import (
    ADMIN_TOOL_NAMES,
    MEMORY_AUTHORITY_PACK_READ_TOOL_NAME,
    MEMORY_CANDIDATE_APPROVE_TOOL_NAME,
    MEMORY_CANDIDATE_REJECT_TOOL_NAME,
    MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME,
    MEMORY_STALE_MARK_TOOL_NAME,
    PUBLIC_AGENT_TOOL_NAMES,
    admin_tool_contract_registry,
    list_admin_tools,
    public_tool_contract_registry,
)
from agent_knowledge.knowledge_search_service import (
    DisabledRetiredIndexBridgeClient,
    KnowledgeSearchService,
)


def _ledger(tmp_path: Path) -> Ledger:
    private = tmp_path / "private"
    private.mkdir(parents=True, exist_ok=True)
    os.chmod(private, 0o700)
    return Ledger(private / "ledger.sqlite")


def _service(tmp_path: Path, allow_restricted: bool = True) -> KnowledgeSearchService:
    ledger = _ledger(tmp_path)
    return KnowledgeSearchService(
        ledger=ledger,
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        allow_private_results=True,
        allow_restricted_steward=allow_restricted,
    )


def test_contract_registries_partition_cleanly():
    public_contracts = public_tool_contract_registry()
    admin_contracts = admin_tool_contract_registry()

    assert len(public_contracts) == 2
    assert set(public_contracts) == PUBLIC_AGENT_TOOL_NAMES

    assert len(admin_contracts) == 32
    assert set(admin_contracts) == ADMIN_TOOL_NAMES

    # No overlap between public and admin tool sets
    assert len(set(public_contracts) & set(admin_contracts)) == 0


def test_admin_surface_tools_list_requires_auth(tmp_path: Path):
    service = _service(tmp_path)

    # 1. Without auth_token -> -32000 error
    resp_unauth = handle_admin_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        service,
        auth_token=None,
    )
    assert resp_unauth["error"]["code"] == -32000
    assert "lbrain_admin" in resp_unauth["error"]["message"]

    # 2. With invalid auth_token -> -32000 error
    resp_invalid = handle_admin_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        service,
        auth_token="invalid_token",
    )
    assert resp_invalid["error"]["code"] == -32000

    # 3. With valid lbrain_admin token -> returns 32 admin tools
    resp_auth = handle_admin_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        service,
        auth_token=ADMIN_AUTH_IDENTITY,
    )
    assert "error" not in resp_auth
    tools = resp_auth["result"]["tools"]
    assert len(tools) == 32
    assert {t["name"] for t in tools} == ADMIN_TOOL_NAMES


def test_admin_surface_call_requires_auth(tmp_path: Path):
    service = _service(tmp_path)

    # Call on admin surface without token fails
    resp = handle_admin_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME,
                "arguments": {"project": "proj-a"},
            },
        },
        service,
        auth_token=None,
    )
    assert resp["error"]["code"] == -32000
    assert "unauthorized" in resp["error"]["message"]


def test_admin_surface_authorized_call_execution(tmp_path: Path):
    service = _service(tmp_path, allow_restricted=True)
    steward = service.brain_steward()

    # 1. Create a candidate proposal via steward directly
    steward.candidate_create(
        source_span={
            "card_type": "decision",
            "project": "proj-admin",
            "title": "Admin Test Card",
            "summary": "Admin card summary",
            "content_hash": "sha256:" + "8" * 64,
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

    # 2. Call memory_review_queue_list with valid lbrain_admin auth
    resp_queue = handle_admin_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {
                "name": MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME,
                "arguments": {"project": "proj-admin"},
            },
        },
        service,
        auth_token=ADMIN_AUTH_IDENTITY,
    )
    assert "error" not in resp_queue
    items = resp_queue["result"]["structuredContent"]["items"]
    assert len(items) == 1
    cand_id = items[0]["memory_id"]

    # 3. Call memory_candidate_approve with valid lbrain_admin auth
    resp_approve = handle_admin_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 21,
            "method": "tools/call",
            "params": {
                "name": MEMORY_CANDIDATE_APPROVE_TOOL_NAME,
                "arguments": {
                    "candidate_memory_id": cand_id,
                    "approved_by": "admin_reviewer",
                    "decision_id": "decision_001",
                },
            },
        },
        service,
        auth_token=ADMIN_AUTH_IDENTITY,
    )
    assert "error" not in resp_approve
    approve_result = resp_approve["result"]["structuredContent"]
    assert approve_result["canonical_write_performed"] is True
    assert "accepted_card" in approve_result

    # 4. Verify card is now in authoritative pack
    resp_pack = handle_admin_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 22,
            "method": "tools/call",
            "params": {
                "name": MEMORY_AUTHORITY_PACK_READ_TOOL_NAME,
                "arguments": {"project": "proj-admin"},
            },
        },
        service,
        auth_token=ADMIN_AUTH_IDENTITY,
    )
    assert "error" not in resp_pack
    pack_items = resp_pack["result"]["structuredContent"]["items"]
    assert len(pack_items) == 1
    assert pack_items[0]["title"] == "Admin Test Card"
