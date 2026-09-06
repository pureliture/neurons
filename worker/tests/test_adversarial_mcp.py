from __future__ import annotations

import os
from pathlib import Path

import pytest
from agent_knowledge.ledger import Ledger
from agent_knowledge.mcp_jsonrpc import (
    handle_admin_jsonrpc_message,
    handle_jsonrpc_message,
)
from agent_knowledge.mcp_tools import (
    BRAIN_RESOLVE_TOOL_NAME,
    MEMORY_CANDIDATE_APPROVE_TOOL_NAME,
    MEMORY_CANDIDATE_AUTO_ACCEPT_TOOL_NAME,
    MEMORY_CANDIDATE_CREATE_TOOL_NAME,
    MEMORY_SUPERSEDE_COMMIT_TOOL_NAME,
    TOOL_NAME,
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


def _service(tmp_path: Path) -> KnowledgeSearchService:
    ledger = _ledger(tmp_path)
    return KnowledgeSearchService(
        ledger=ledger,
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        allow_private_results=True,
    )


def test_adversarial_public_surface_blocks_admin_and_steward_tools(tmp_path: Path):
    service = _service(tmp_path)
    blocked_tools = [
        TOOL_NAME,
        MEMORY_CANDIDATE_APPROVE_TOOL_NAME,
        MEMORY_CANDIDATE_AUTO_ACCEPT_TOOL_NAME,
        MEMORY_SUPERSEDE_COMMIT_TOOL_NAME,
        "brain_context_resolve",
        "brain_drift_explain",
    ]
    for idx, tool in enumerate(blocked_tools, start=1):
        response = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": idx,
                "method": "tools/call",
                "params": {
                    "name": tool,
                    "arguments": {"project": "demo", "query": "probe"},
                },
            },
            service,
            surface="agent",
        )
        assert response["error"]["code"] == -32601
        assert f"unknown tool: {tool}" in response["error"]["message"]


def test_adversarial_candidate_create_injection_prevention(tmp_path: Path):
    service = _service(tmp_path)
    # Attacker tries to inject accepted lifecycle and active authorization status
    malicious_args = {
        "card_type": "decision",
        "project": "project-omega",
        "title": "Malicious Privilege Escalation",
        "summary": "Trying to bypass approval pipeline",
        "lifecycle_state": "accepted",
        "authorization_status": "active",
        "status": "accepted",
        "approval_state": "approved",
        "content_hash": "sha256:" + "a" * 64,
        "typed_payload": {
            "decision": "escalate privilege",
            "rationale": "malicious reason",
            "alternatives": ["none"],
            "consequence": "none",
            "authority_ref": "fake_auth",
        },
        "source_ref": {"source_id": "malicious_actor"},
        "span_ref": {"span_id": "malicious_span"},
    }
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 100,
            "method": "tools/call",
            "params": {
                "name": MEMORY_CANDIDATE_CREATE_TOOL_NAME,
                "arguments": malicious_args,
            },
        },
        service,
    )
    assert "error" not in response
    result = response["result"]["structuredContent"]
    # Verify strict proposal invariants cannot be overridden by input parameters
    assert result["lifecycle_state"] == "candidate"
    assert result["authorization_status"] == "disabled"
    assert result["approval_state"] == "suggested"
    assert result["proposal_write_performed"] is True
    assert result["authoritative_memory_changed"] is False
    assert result["accepted"] is False


def test_adversarial_candidate_create_invalid_hash(tmp_path: Path):
    service = _service(tmp_path)
    invalid_hashes = [
        "not-a-hash",
        "md5:123456",
        "sha256:invalid_hex_$$$",
        "",
        "sha256:" + "g" * 64,
        "sha256:" + "A" * 64,
        "sha256:12345",
        "sha256:" + "a" * 63,
        "sha256:" + "a" * 65,
        "sha256: " + "a" * 64,
    ]
    valid_payload = {
        "decision": "Use PostgreSQL pgvector",
        "rationale": "Unified storage architecture",
        "alternatives": ["Qdrant", "SQLite"],
        "consequence": "None",
        "authority_ref": "ADR-0007",
    }
    for idx, bad_hash in enumerate(invalid_hashes, start=200):
        response = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": idx,
                "method": "tools/call",
                "params": {
                    "name": MEMORY_CANDIDATE_CREATE_TOOL_NAME,
                    "arguments": {
                        "card_type": "decision",
                        "project": "proj-x",
                        "title": "Bad Hash",
                        "summary": "Bad hash test",
                        "content_hash": bad_hash,
                        "typed_payload": valid_payload,
                    },
                },
            },
            service,
        )
        assert "error" in response, f"Expected error for bad_hash={bad_hash}, got {response}"
        assert response["error"]["code"] == -32602


def test_adversarial_candidate_create_missing_required_fields(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 300,
            "method": "tools/call",
            "params": {
                "name": MEMORY_CANDIDATE_CREATE_TOOL_NAME,
                "arguments": {
                    "project": "proj-y",
                    "title": "Missing Card Type",
                    # card_type is missing
                },
            },
        },
        service,
    )
    assert response["error"]["code"] == -32602


def test_adversarial_brain_resolve_modes_and_limits(tmp_path: Path):
    service = _service(tmp_path)

    # 1. Missing project
    resp1 = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 401,
            "method": "tools/call",
            "params": {
                "name": BRAIN_RESOLVE_TOOL_NAME,
                "arguments": {"mode": "context"},
            },
        },
        service,
    )
    assert resp1["error"]["code"] == -32602

    # 2. mode='query' with empty query string
    resp2 = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 402,
            "method": "tools/call",
            "params": {
                "name": BRAIN_RESOLVE_TOOL_NAME,
                "arguments": {"project": "proj-a", "mode": "query", "query": "   "},
            },
        },
        service,
    )
    assert resp2["error"]["code"] == -32602

    # 3. Invalid mode
    resp3 = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 403,
            "method": "tools/call",
            "params": {
                "name": BRAIN_RESOLVE_TOOL_NAME,
                "arguments": {"project": "proj-a", "mode": "unsupported_mode"},
            },
        },
        service,
    )
    assert resp3["error"]["code"] == -32602

    # 4. Extreme limit bounding (negative and huge)
    resp4 = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 404,
            "method": "tools/call",
            "params": {
                "name": BRAIN_RESOLVE_TOOL_NAME,
                "arguments": {"project": "proj-a", "mode": "context", "limit": 9999},
            },
        },
        service,
    )
    assert "error" not in resp4
    assert resp4["result"]["structuredContent"]["schema_version"] == "lbrain_slim_context.v1"


def test_adversarial_admin_surface_unauthorized_probe(tmp_path: Path):
    service = _service(tmp_path)

    # 1. tools/list without auth token on admin surface
    resp1 = handle_admin_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 501, "method": "tools/list"},
        service,
        auth_token=None,
    )
    assert resp1["error"]["code"] == -32000
    assert "unauthorized" in resp1["error"]["message"]

    # 2. tools/call with wrong auth token on admin surface
    resp2 = handle_admin_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 502,
            "method": "tools/call",
            "params": {
                "name": MEMORY_CANDIDATE_APPROVE_TOOL_NAME,
                "arguments": {
                    "candidate_memory_id": "cand-1",
                    "approved_by": "hacker",
                    "decision_id": "dec-1",
                },
            },
        },
        service,
        auth_token="wrong_token",
    )
    assert resp2["error"]["code"] == -32000
    assert "unauthorized" in resp2["error"]["message"]
