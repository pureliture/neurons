from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock

import pytest

from agent_knowledge.llm_brain_core.slim_serializer import SlimSerializer
from agent_knowledge.mcp_jsonrpc import _dispatch_brain_resolve_tool


def test_slim_serializer_standard_size():
    decisions = [
        {
            "id": f"mem_d_{i}",
            "title": f"Decision {i} Title",
            "typed_payload": {"decision": f"Decision {i} details", "rationale": "Testing rationale"},
            "currentness": "current",
            "content_hash": f"sha256:{i:064x}",
        }
        for i in range(3)
    ]
    preferences = [
        {"rule": "Use uv for python", "typed_payload": {"scope": "build"}},
        {"rule": "Keep wire payloads slim", "typed_payload": {"scope": "network"}},
    ]
    guardrails = ["agents_use_brain_resolve", "ledger_is_single_authority"]

    payload = SlimSerializer.serialize_slim(
        project="neurons",
        decisions=decisions,
        preferences=preferences,
        guardrails=guardrails,
    )
    raw_json = json.dumps(payload)
    assert len(raw_json.encode("utf-8")) <= 1200
    assert payload["schema_version"] == "lbrain_slim_context.v1"
    assert payload["project"] == "neurons"
    assert len(payload["decisions"]) == 3
    assert len(payload["preferences"]) == 2
    assert payload["has_more"] is False
    assert payload["next_cursor"] is None


def test_slim_serializer_pagination():
    decisions = [{"id": f"d_{i}", "title": f"D{i}", "typed_payload": {}} for i in range(5)]
    page1 = SlimSerializer.serialize_slim(
        project="neurons",
        decisions=decisions,
        preferences=[],
        guardrails=[],
        limit=2,
    )
    assert len(page1["decisions"]) == 2
    assert page1["has_more"] is True
    cursor1 = page1["next_cursor"]
    assert cursor1 is not None

    page2 = SlimSerializer.serialize_slim(
        project="neurons",
        decisions=decisions,
        preferences=[],
        guardrails=[],
        limit=2,
        cursor=cursor1,
    )
    assert len(page2["decisions"]) == 2
    assert page2["decisions"][0]["id"] == "d_2"
    assert page2["has_more"] is True
    cursor2 = page2["next_cursor"]

    page3 = SlimSerializer.serialize_slim(
        project="neurons",
        decisions=decisions,
        preferences=[],
        guardrails=[],
        limit=2,
        cursor=cursor2,
    )
    assert len(page3["decisions"]) == 1
    assert page3["decisions"][0]["id"] == "d_4"
    assert page3["has_more"] is False
    assert page3["next_cursor"] is None


def test_slim_serializer_hard_limit_truncation():
    # Construct very large decisions
    huge_decisions = [
        {
            "id": f"mem_huge_{i}",
            "title": f"Huge Title {i}" * 20,
            "typed_payload": {
                "decision": "Very long decision payload string that consumes lots of space " * 30,
                "rationale": "Very long rationale string that consumes lots of space " * 20,
            },
            "content_hash": f"sha256:{i:064x}",
        }
        for i in range(10)
    ]
    payload = SlimSerializer.serialize_slim(
        project="neurons",
        decisions=huge_decisions,
        preferences=[],
        guardrails=[],
        limit=10,
    )
    raw_json = json.dumps(payload, ensure_ascii=False)
    assert len(raw_json.encode("utf-8")) <= 3072
    assert payload["has_more"] is True


def test_with_evidence_serialization():
    decisions = [
        {
            "id": "mem_d1",
            "title": "D1",
            "typed_payload": {"decision": "Rule 1"},
            "content_hash": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
        }
    ]
    edges = [
        {
            "src_id": "mem_d2",
            "rel_type": "supersedes",
            "dst_id": "mem_d1",
            "provenance_hash": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
        }
    ]
    evidence_hashes = [
        "sha256:1111111111111111111111111111111111111111111111111111111111111111",
        "sha256:3333333333333333333333333333333333333333333333333333333333333333",
    ]
    source_refs = [{"locator": "docs/design.md", "span": "Section 5"}]

    payload = SlimSerializer.serialize_with_evidence(
        project="neurons",
        decisions=decisions,
        preferences=[],
        guardrails=["guard1"],
        edges=edges,
        evidence_hashes=evidence_hashes,
        source_refs=source_refs,
    )

    assert payload["schema_version"] == "lbrain_evidence_context.v1"
    assert payload["edges"] == edges
    assert payload["evidence_hashes"] == evidence_hashes
    assert payload["source_refs"] == source_refs


def test_schema_rationalization_absence_of_legacy_keys():
    payload = SlimSerializer.serialize_slim(
        project="neurons",
        decisions=[],
        preferences=[],
        guardrails=[],
    )
    for i in range(1, 8):
        assert f"lane_{i}" not in payload
        assert f"empty_lane_{i}" not in payload
    assert "route_spec" not in payload
    assert "routing_table_dump" not in payload
    assert "current_task" not in payload
    assert "active_task" not in payload
    assert "session_task" not in payload


def test_dispatch_brain_resolve_tool_integration():
    mock_service = MagicMock()
    mock_service.ledger.list_llm_brain_memory_cards.return_value = [
        {
            "memory_id": "mem_test_1",
            "card_type": "decision",
            "title": "PostgreSQL Convergence",
            "summary": "Converge storage on Postgres",
            "typed_payload": {"decision": "Use PostgreSQL 17 + pgvector", "rationale": "Simplicity"},
            "content_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "currentness": "current",
        },
        {
            "memory_id": "mem_pref_1",
            "card_type": "preference",
            "title": "Use uv",
            "summary": "Prefer uv for python run",
            "typed_payload": {"rule": "uv run pytest", "scope": "testing"},
            "content_hash": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "currentness": "current",
        },
    ]

    res_slim = _dispatch_brain_resolve_tool(
        "brain.resolve",
        {"project": "neurons", "mode": "context", "response_mode": "slim"},
        mock_service,
    )
    assert res_slim["isError"] is False
    data = json.loads(res_slim["content"][0]["text"])
    assert data["schema_version"] == "lbrain_slim_context.v1"
    assert len(data["decisions"]) == 1
    assert data["decisions"][0]["id"] == "mem_test_1"
    assert len(data["preferences"]) == 1

    res_ev = _dispatch_brain_resolve_tool(
        "brain.resolve",
        {"project": "neurons", "mode": "context", "response_mode": "with_evidence"},
        mock_service,
    )
    assert res_ev["isError"] is False
    data_ev = json.loads(res_ev["content"][0]["text"])
    assert data_ev["schema_version"] == "lbrain_evidence_context.v1"
    assert "evidence_hashes" in data_ev
    assert "edges" in data_ev
    assert "source_refs" in data_ev


def test_slim_serializer_single_huge_decision_field_truncation():
    # A single decision whose text alone exceeds 3072 bytes
    single_huge_decision = [
        {
            "id": "mem_single_huge",
            "title": "A" * 500,
            "typed_payload": {
                "decision": "B" * 3000,
                "rationale": "C" * 2000,
            },
            "content_hash": "sha256:" + "f" * 64,
        }
    ]
    payload = SlimSerializer.serialize_slim(
        project="neurons",
        decisions=single_huge_decision,
        preferences=[],
        guardrails=[],
        limit=1,
    )
    raw_json = json.dumps(payload, ensure_ascii=False)
    assert len(raw_json.encode("utf-8")) <= 3072
    assert payload["schema_version"] == "lbrain_slim_context.v1"
    assert len(payload["decisions"]) == 1
    assert payload["decisions"][0]["id"] == "mem_single_huge"


def test_slim_serializer_preferences_fields_present():
    preferences = [
        {
            "title": "Use Python uv",
            "summary": "Always use uv for Python package execution",
            "typed_payload": {
                "rule": "Use uv for python",
                "scope": "tooling",
            },
        }
    ]
    payload = SlimSerializer.serialize_slim(
        project="neurons",
        decisions=[],
        preferences=preferences,
        guardrails=[],
    )
    assert len(payload["preferences"]) == 1
    pref = payload["preferences"][0]
    assert pref["title"] == "Use Python uv"
    assert pref["rule"] == "Use uv for python"
    assert pref["scope"] == "tooling"

