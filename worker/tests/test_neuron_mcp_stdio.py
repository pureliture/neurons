from __future__ import annotations

import io
import json
import os
from pathlib import Path

from agent_knowledge.cli import main
from agent_knowledge.curation import CurationService
from agent_knowledge.ledger import Ledger
from agent_knowledge.mcp_server import (
    BRAIN_QUERY_TOOL_NAME,
    BRAIN_RESOLVE_TOOL_NAME,
    TOOL_NAME,
    DisabledRagflowClient,
    KnowledgeSearchService,
    handle_jsonrpc_message,
    list_tools,
)
from agent_knowledge.memory_card import build_memory_candidate
from agent_knowledge.memory_miner import build_memory_card_candidate_from_source_span
from agent_knowledge.session_memory.llm_brain_service import LLMBrainMemoryService

PROJECT = "workspace-ragflow-advisor"


def _ledger(tmp_path: Path) -> Ledger:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    return Ledger(private / "ledger.sqlite")


def _source_span(**overrides):
    span = {
        "source_ref": {"source_id": "src_neuron_mcp"},
        "span_ref": {"span_id": "span_neuron_mcp"},
        "content_hash": "sha256:neuron-mcp-card",
        "brain_id": f"/project/{PROJECT}",
        "card_type": "preference",
        "scope": "project",
        "project": PROJECT,
        "provider": "codex",
        "title": "Korean response preference",
        "redacted_summary": "한국어로 응답한다",
        "typed_payload": {
            "preference": "한국어로 응답한다",
            "explicitness": "explicit",
            "repeated_count": 1,
            "confirmation_status": "confirmed",
            "applies_to": "natural_language_response",
        },
        "confidence": 0.9,
        "confidence_basis": "human-approved preference",
    }
    span.update(overrides)
    return span


def _service(tmp_path: Path) -> KnowledgeSearchService:
    ledger = _ledger(tmp_path)
    curation = CurationService(ledger)
    candidate = curation.add_candidate(
        build_memory_candidate(
            candidate_type="user_preference",
            statement="한국어로 응답한다",
            project=PROJECT,
            provider="codex",
            evidence_refs=[{"knowledge_id": "kn", "content_hash": "sha256:c"}],
        )
    )
    curation.approve(candidate["candidate_id"], approved_by="ddalkak")
    llm_candidate = build_memory_card_candidate_from_source_span(
        _source_span(),
        refresh_watermark="test",
    )
    LLMBrainMemoryService(ledger).accept_human_approved_candidate(
        llm_candidate,
        approved_by="ddalkak",
        decision_id="decision_neuron_mcp",
    )
    return KnowledgeSearchService(
        ledger=ledger,
        ragflow=DisabledRagflowClient(),
        dataset_ids=[],
        allow_private_results=True,
    )


def test_mcp_tool_list_exposes_neuron_owned_tools():
    names = [tool["name"] for tool in list_tools()]

    assert names == [TOOL_NAME, BRAIN_QUERY_TOOL_NAME, BRAIN_RESOLVE_TOOL_NAME]


def test_mcp_brain_query_roundtrip(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": BRAIN_QUERY_TOOL_NAME,
                "arguments": {"brain_id": f"/project/{PROJECT}", "query": "언어 선호"},
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    assert result["audit"]["path"] == "ledger_precedence_v2"
    assert result["current"][0]["summary"] == "한국어로 응답한다"
    assert json.loads(response["result"]["content"][0]["text"]) == result


def test_mcp_brain_resolve_roundtrip(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": BRAIN_RESOLVE_TOOL_NAME, "arguments": {"query": "ragflow"}},
        },
        service,
    )

    candidates = response["result"]["structuredContent"]["candidates"]
    assert candidates[0]["brain_id"] == f"/project/{PROJECT}"


def test_mcp_stdio_cli_serves_tools_list_without_ragflow_token(tmp_path: Path, monkeypatch, capsys):
    ledger = _ledger(tmp_path)
    request = {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request) + "\n"))

    assert main(["mcp-stdio", "--ledger", str(ledger.path), "--dataset-id", "ds"]) == 0

    response = json.loads(capsys.readouterr().out)
    assert response["id"] == 3
    assert [tool["name"] for tool in response["result"]["tools"]] == [
        TOOL_NAME,
        BRAIN_QUERY_TOOL_NAME,
        BRAIN_RESOLVE_TOOL_NAME,
    ]
