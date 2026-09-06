from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_knowledge.ledger import Ledger
from agent_knowledge.mcp_jsonrpc import (
    handle_jsonrpc_message,
)
from agent_knowledge.mcp_tools import (
    ADMIN_TOOL_NAMES,
    BRAIN_RESOLVE_TOOL_NAME,
    MEMORY_CANDIDATE_APPROVE_TOOL_NAME,
    MEMORY_CANDIDATE_AUTO_ACCEPT_TOOL_NAME,
    MEMORY_CANDIDATE_CREATE_TOOL_NAME,
    MEMORY_SUPERSEDE_COMMIT_TOOL_NAME,
    PUBLIC_AGENT_TOOL_NAMES,
    TOOL_NAME,
    list_public_agent_tools,
)
from agent_knowledge.knowledge_search_service import (
    DisabledRetiredIndexBridgeClient,
    KnowledgeSearchService,
)


def _make_card(
    *,
    memory_id: str,
    project: str,
    card_type: str,
    title: str,
    summary: str,
    typed_payload: dict,
    content_hash: str,
    source_refs: list | None = None,
    evidence_hashes: list | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
) -> dict:
    return {
        "memory_id": memory_id,
        "brain_id": f"/project/{project}",
        "project": project,
        "scope": "project",
        "provider": "manual",
        "card_type": card_type,
        "title": title,
        "summary": summary,
        "render_text": summary,
        "lifecycle_state": "accepted",
        "authorization_status": "active",
        "status": "accepted",
        "judgment_state": "none",
        "approval_state": "approved",
        "governance_tier": "high",
        "freshness": "current",
        "currentness": "current",
        "confidence": 0.95,
        "confidence_basis": "verified test",
        "content_hash": content_hash,
        "source_refs": source_refs or [],
        "evidence_refs": [],
        "evidence_hashes": evidence_hashes or [],
        "derived_from": [],
        "supersedes": [],
        "superseded_by": [],
        "conflicts": [],
        "active_until": None,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "typed_payload": typed_payload,
    }


class _LedgerStoreAdapter:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self._cards_by_id = {}

    def get_card(self, memory_id: str):
        c = self._cards_by_id.get(memory_id)
        if c is None:
            return None
        return SimpleNamespace(
            memory_id=getattr(c, "memory_id", ""),
            lifecycle_state=getattr(c, "lifecycle_state", "candidate"),
            content_hash=getattr(c, "content_hash", ""),
        )

    def upsert_card(self, card):
        self._cards_by_id[str(getattr(card, "memory_id", ""))] = card
        card_dict = _make_card(
            memory_id=getattr(card, "memory_id", ""),
            project=getattr(card, "project", ""),
            card_type=getattr(card, "card_type", ""),
            title=getattr(card, "title", ""),
            summary=getattr(card, "summary", ""),
            typed_payload=getattr(card, "typed_payload", {}),
            content_hash=getattr(card, "content_hash", ""),
        )
        card_dict["lifecycle_state"] = getattr(card, "lifecycle_state", "candidate")
        card_dict["authorization_status"] = getattr(card, "authorization_status", "disabled")
        card_dict["approval_state"] = getattr(card, "approval_state", "suggested")
        card_dict["governance_tier"] = "low"
        self.ledger.upsert_llm_brain_memory_card(card_dict)

    def graph_projection_health(self, project: str, *, as_of: str | None = None) -> dict:
        return {"unprojected": False, "projection_lag_ms": None}

    def list_authorized_cards(
        self,
        project: str,
        *,
        memory_ids: list[str] | None = None,
        as_of: str | None = None,
        limit: int = 100,
        after_memory_id: str | None = None,
    ) -> list[dict]:
        cards = self.ledger.list_llm_brain_memory_cards(project=project)
        result = []
        for c in cards:
            if memory_ids is not None and c.get("memory_id") not in memory_ids:
                continue
            if as_of:
                try:
                    dt = datetime.fromisoformat(as_of)
                    vf = c.get("valid_from")
                    vt = c.get("valid_to")
                    if vf and datetime.fromisoformat(vf) > dt:
                        continue
                    if vt and datetime.fromisoformat(vt) < dt:
                        continue
                except Exception:
                    pass
            result.append(c)
        if after_memory_id:
            result = [c for c in result if c.get("memory_id", "") > after_memory_id]
        return result[:limit]

    def hybrid_search(
        self,
        project: str,
        *,
        query_vector=None,
        text_query=None,
        limit: int = 100,
        as_of: str | None = None,
        **kwargs,
    ) -> list[dict]:
        return self.list_authorized_cards(project=project, limit=limit, as_of=as_of)

    def read_authorized_evidence(
        self,
        project: str,
        *,
        root_memory_ids: list[str],
        max_depth: int = 5,
        **kwargs,
    ) -> dict:
        cards = self.list_authorized_cards(project=project)
        return {
            "root_hashes": {
                str(c["memory_id"]): c.get("content_hash")
                for c in cards
                if str(c["memory_id"]) in root_memory_ids
            },
            "edges": [],
            "truncated": False,
        }


def _ledger(tmp_path: Path) -> Ledger:
    private = tmp_path / "private"
    private.mkdir(parents=True, exist_ok=True)
    os.chmod(private, 0o700)
    return Ledger(private / "ledger.sqlite")


def _service(tmp_path: Path) -> KnowledgeSearchService:
    ledger = _ledger(tmp_path)
    service = KnowledgeSearchService(
        ledger=ledger,
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        allow_private_results=True,
    )
    service.pgvector_store = _LedgerStoreAdapter(ledger)
    service._semantic_ranker = SimpleNamespace(embed_query=lambda _text: [0.1] * 3072)
    return service


def test_public_tools_list_contains_exactly_two_tools(tmp_path: Path):
    service = _service(tmp_path)
    # 1. Via list_public_agent_tools()
    public_tools = list_public_agent_tools()
    assert len(public_tools) == 2
    tool_names = {t["name"] for t in public_tools}
    assert tool_names == {BRAIN_RESOLVE_TOOL_NAME, MEMORY_CANDIDATE_CREATE_TOOL_NAME}

    # 2. Via MCP tools/list on public/agent surface
    response = handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        service,
        surface="agent",
    )
    assert "error" not in response
    tools = response["result"]["tools"]
    assert len(tools) == 2
    assert {t["name"] for t in tools} == PUBLIC_AGENT_TOOL_NAMES


def test_public_surface_blocks_all_admin_and_steward_tools(tmp_path: Path):
    service = _service(tmp_path)
    # Every tool in ADMIN_TOOL_NAMES must fail-closed with -32601 on public surface
    sample_admin_tools = [
        TOOL_NAME,
        "brain.query",
        "brain_context_resolve",
        "brain_memory_search",
        "brain_drift_explain",
        "brain_objects_query",
        "brain_object_decision_commit",
        "brain_review_proposals",
        "memory_authority_pack_read",
        "memory_review_queue_list",
        "memory_stale_mark",
        "memory_supersede_propose",
        MEMORY_CANDIDATE_APPROVE_TOOL_NAME,
        MEMORY_CANDIDATE_AUTO_ACCEPT_TOOL_NAME,
        MEMORY_SUPERSEDE_COMMIT_TOOL_NAME,
        "memory_stale_commit",
    ]
    for idx, tool_name in enumerate(sample_admin_tools, start=10):
        response = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": idx,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": {"project": "test-project", "query": "hello"},
                },
            },
            service,
            surface="agent",
        )
        assert response["error"]["code"] == -32601
        assert f"unknown tool: {tool_name}" in response["error"]["message"]


def test_brain_resolve_context_mode_slim(tmp_path: Path):
    service = _service(tmp_path)
    ledger = service.ledger

    # Seed an accepted decision and preference
    ledger.upsert_llm_brain_memory_card(_make_card(
        memory_id="mem_dec_1",
        project="proj-x",
        card_type="decision",
        title="Adopt FastPath Ingress",
        summary="FastPath ingress adopted",
        content_hash="sha256:" + "1" * 64,
        typed_payload={
            "decision": "Adopt FastPath",
            "rationale": "lower latency",
            "alternatives": ["batch"],
            "consequence": "fast",
            "authority_ref": "arch_doc_1",
        },
    ))
    ledger.upsert_llm_brain_memory_card(_make_card(
        memory_id="mem_pref_1",
        project="proj-x",
        card_type="preference",
        title="Use Python uv",
        summary="Always use uv",
        content_hash="sha256:" + "2" * 64,
        typed_payload={
            "preference": "Use uv for python",
            "explicitness": "explicit",
            "repeated_count": 5,
            "confirmation_status": "confirmed",
            "applies_to": "python",
        },
    ))

    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 100,
            "method": "tools/call",
            "params": {
                "name": BRAIN_RESOLVE_TOOL_NAME,
                "arguments": {
                    "project": "proj-x",
                    "mode": "context",
                    "response_mode": "slim",
                    "limit": 5,
                },
            },
        },
        service,
    )
    assert "error" not in response
    payload = response["result"]["structuredContent"]
    assert payload["schema_version"] == "lbrain_slim_context.v1"
    assert payload["project"] == "proj-x"
    assert len(payload["decisions"]) == 1
    assert payload["decisions"][0]["title"] == "Adopt FastPath Ingress"
    assert len(payload["preferences"]) == 1
    assert payload["preferences"][0]["title"] == "Use Python uv"
    assert "active_guardrails" in payload
    assert "has_more" in payload


def test_brain_resolve_context_mode_with_evidence(tmp_path: Path):
    service = _service(tmp_path)
    ledger = service.ledger

    ledger.upsert_llm_brain_memory_card(_make_card(
        memory_id="mem_dec_ev",
        project="proj-ev",
        card_type="decision",
        title="Evidence Grounded Decision",
        summary="Decision with source ref evidence",
        content_hash="sha256:" + "3" * 64,
        source_refs=[{"source_id": "src_1", "location": "doc.md"}],
        evidence_hashes=["sha256:" + "3" * 64],
        typed_payload={
            "decision": "Test Decision",
            "rationale": "test",
            "alternatives": [],
            "consequence": "none",
            "authority_ref": "ref_1",
        },
    ))

    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/call",
            "params": {
                "name": BRAIN_RESOLVE_TOOL_NAME,
                "arguments": {
                    "project": "proj-ev",
                    "mode": "context",
                    "response_mode": "with_evidence",
                },
            },
        },
        service,
    )
    assert "error" not in response
    payload = response["result"]["structuredContent"]
    assert payload["schema_version"] == "lbrain_slim_context.v1"
    assert len(payload["evidence_hashes"]) >= 1
    assert "evidence" in payload


def test_brain_resolve_query_and_list_modes(tmp_path: Path):
    service = _service(tmp_path)
    ledger = service.ledger

    ledger.upsert_llm_brain_memory_card(_make_card(
        memory_id="mem_q_1",
        project="proj-q",
        card_type="decision",
        title="Quantum Routing Decision",
        summary="Quantum routing is chosen for high speed packet delivery",
        content_hash="sha256:" + "4" * 64,
        typed_payload={
            "decision": "Quantum routing",
            "rationale": "speed",
            "alternatives": [],
            "consequence": "fast",
            "authority_ref": "ref",
        },
    ))

    # Query mode: search matching term
    resp_query = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 102,
            "method": "tools/call",
            "params": {
                "name": BRAIN_RESOLVE_TOOL_NAME,
                "arguments": {
                    "project": "proj-q",
                    "mode": "query",
                    "query": "Quantum",
                },
            },
        },
        service,
    )
    assert "error" not in resp_query
    q_payload = resp_query["result"]["structuredContent"]
    assert len(q_payload["decisions"]) == 1
    assert q_payload["decisions"][0]["title"] == "Quantum Routing Decision"

    # List mode
    resp_list = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 103,
            "method": "tools/call",
            "params": {
                "name": BRAIN_RESOLVE_TOOL_NAME,
                "arguments": {
                    "project": "proj-q",
                    "mode": "list",
                },
            },
        },
        service,
    )
    assert "error" not in resp_list
    list_payload = resp_list["result"]["structuredContent"]
    assert len(list_payload["decisions"]) == 1


def test_memory_candidate_create_strict_security_invariants(tmp_path: Path):
    service = _service(tmp_path)

    hash_val = "sha256:" + "5" * 64
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 200,
            "method": "tools/call",
            "params": {
                "name": MEMORY_CANDIDATE_CREATE_TOOL_NAME,
                "arguments": {
                    "card_type": "decision",
                    "project": "proj-cand",
                    "title": "Proposed Async Queue",
                    "summary": "Proposing async queue for background workers",
                    "typed_payload": {
                        "decision": "Use Celery or RQ",
                        "rationale": "background tasks",
                        "alternatives": ["threading"],
                        "consequence": "worker separation",
                        "authority_ref": "arch_doc",
                    },
                    "content_hash": hash_val,
                    "source_ref": {"source_id": "test_agent_ref"},
                    "span_ref": {"span_id": "test_span_ref"},
                    "proposer": "codex",
                },
            },
        },
        service,
    )
    assert "error" not in response
    res = response["result"]["structuredContent"]

    # Invariant checks
    assert res["lifecycle_state"] == "candidate"
    assert res["authorization_status"] == "disabled"
    assert res["approval_state"] == "suggested"
    assert res["proposal_write_performed"] is True
    assert res["authoritative_memory_changed"] is False
    assert res["accepted"] is False
    assert res["memory_id"] is not None

    # Verify candidate is NOT returned in authoritative memory pack
    authority_pack = service.brain_steward().authority_pack_read(project="proj-cand")
    assert len(authority_pack["items"]) == 0

    # Verify candidate IS visible in review queue for admin steward
    queue = service.brain_steward().review_queue_list(project="proj-cand")
    assert len(queue["items"]) == 1
    assert queue["items"][0]["memory_id"] == res["memory_id"]
    assert queue["items"][0]["lifecycle_state"] == "candidate"
