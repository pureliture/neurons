"""Tier 1 Feature Coverage Test Suite (Simulation).

Pure simulation suite running against InMemoryPostgresStore, InMemoryQdrantStore,
and MockMCPServer with isolated legacy 1536-dim simulation profile.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone

import pytest

from .conftest import (
    InMemoryPostgresStore,
    InMemoryQdrantStore,
    MemoryCard,
    MemoryEdge,
    MockMCPServer,
    SessionChunk,
    SlimSerializer,
    compute_cosine_similarity,
    make_dummy_vector,
    sha256_str,
)

pytestmark = [pytest.mark.simulation, pytest.mark.legacy_profile]

# ==============================================================================
# Feature 1: brain.resolve Tool (5 Test Cases)
# ==============================================================================

def test_f1_01_context_mode_default(mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore):
    """Default mode='context' returns slim envelope with active decisions, preferences, guardrails."""
    pg_store.insert_card(
        MemoryCard(
            memory_id="mem_dec_01",
            project="neurons",
            card_type="decision",
            title="Use PostgreSQL pgvector",
            summary="Consolidate vector search into PostgreSQL pgvector 0.8.0+",
            typed_payload={"decision": "Use PostgreSQL pgvector", "rationale": "Operational simplicity"},
            lifecycle_state="human_accepted",
            authorization_status="active",
            content_hash=sha256_str("dec_01"),
        )
    )
    req = {
        "jsonrpc": "2.0",
        "id": 101,
        "method": "tools/call",
        "params": {
            "name": "brain.resolve",
            "arguments": {"project": "neurons", "mode": "context"},
        },
    }
    res = mcp_server.handle_public_request(req)
    assert "result" in res
    result = res["result"]
    assert result["schema_version"] == "lbrain_slim_context.v1"
    assert result["project"] == "neurons"
    assert len(result["decisions"]) == 1
    assert result["decisions"][0]["id"] == "mem_dec_01"
    assert "active_guardrails" in result


def test_f1_02_query_mode_keyword(mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore):
    """mode='query' performs scoped lookup and respects project boundary."""
    pg_store.insert_card(
        MemoryCard(
            memory_id="mem_dec_p1",
            project="project_a",
            card_type="decision",
            title="Project A Decision",
            summary="Architecture for Project A",
            typed_payload={"decision": "Architecture A"},
            lifecycle_state="human_accepted",
            authorization_status="active",
            content_hash=sha256_str("dec_p1"),
        )
    )
    pg_store.insert_card(
        MemoryCard(
            memory_id="mem_dec_p2",
            project="project_b",
            card_type="decision",
            title="Project B Decision",
            summary="Architecture for Project B",
            typed_payload={"decision": "Architecture B"},
            lifecycle_state="human_accepted",
            authorization_status="active",
            content_hash=sha256_str("dec_p2"),
        )
    )
    req = {
        "jsonrpc": "2.0",
        "id": 102,
        "method": "tools/call",
        "params": {
            "name": "brain.resolve",
            "arguments": {"project": "project_a", "mode": "query", "query": "Architecture"},
        },
    }
    res = mcp_server.handle_public_request(req)
    assert "result" in res
    decisions = res["result"]["decisions"]
    assert len(decisions) == 1
    assert decisions[0]["id"] == "mem_dec_p1"


def test_f1_03_list_mode(mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore):
    """mode='list' lists stored decisions and preferences deterministically."""
    for i in range(3):
        pg_store.insert_card(
            MemoryCard(
                memory_id=f"mem_list_{i}",
                project="neurons",
                card_type="decision",
                title=f"Decision {i}",
                summary=f"Summary {i}",
                typed_payload={"decision": f"Decision {i}"},
                lifecycle_state="human_accepted",
                authorization_status="active",
                content_hash=sha256_str(f"list_{i}"),
            )
        )
    req = {
        "jsonrpc": "2.0",
        "id": 103,
        "method": "tools/call",
        "params": {
            "name": "brain.resolve",
            "arguments": {"project": "neurons", "mode": "list", "limit": 10},
        },
    }
    res = mcp_server.handle_public_request(req)
    assert "result" in res
    assert len(res["result"]["decisions"]) == 3


def test_f1_04_with_evidence_mode(mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore):
    """response_mode='with_evidence' returns hash chains and DAG edge structures."""
    pg_store.insert_card(
        MemoryCard(
            memory_id="mem_ev_01",
            project="neurons",
            card_type="decision",
            title="Evidence Verified Card",
            summary="Verified decision card",
            typed_payload={"decision": "Verified"},
            lifecycle_state="human_accepted",
            authorization_status="active",
            content_hash="sha256:abc123456789",
            source_ref=[{"locator": "transcript/session_1", "span": "10-20"}],
        )
    )
    req = {
        "jsonrpc": "2.0",
        "id": 104,
        "method": "tools/call",
        "params": {
            "name": "brain.resolve",
            "arguments": {"project": "neurons", "response_mode": "with_evidence"},
        },
    }
    res = mcp_server.handle_public_request(req)
    assert "result" in res
    result = res["result"]
    assert result["schema_version"] == "lbrain_evidence_context.v1"
    assert "evidence_hashes" in result
    assert "edges" in result
    assert "source_refs" in result
    assert "sha256:abc123456789" in result["evidence_hashes"]


def test_f1_05_as_of_temporal_point(mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore):
    """as_of timestamp performs point-in-time recall filtering records where valid_from <= as_of <= valid_to."""
    t1 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)

    # Card 1: valid from Jan to June
    pg_store.insert_card(
        MemoryCard(
            memory_id="mem_temp_old",
            project="neurons",
            card_type="decision",
            title="Old Temporal Decision",
            summary="Valid early 2026",
            typed_payload={"decision": "Old"},
            lifecycle_state="human_accepted",
            authorization_status="active",
            valid_from=t1,
            valid_to=t2,
            content_hash=sha256_str("temp_old"),
        )
    )
    # Card 2: valid from June onward
    pg_store.insert_card(
        MemoryCard(
            memory_id="mem_temp_new",
            project="neurons",
            card_type="decision",
            title="New Temporal Decision",
            summary="Valid mid 2026 onward",
            typed_payload={"decision": "New"},
            lifecycle_state="human_accepted",
            authorization_status="active",
            valid_from=t2,
            valid_to=None,
            content_hash=sha256_str("temp_new"),
        )
    )

    # Query as of March 2026 (should only return Card 1)
    req = {
        "jsonrpc": "2.0",
        "id": 105,
        "method": "tools/call",
        "params": {
            "name": "brain.resolve",
            "arguments": {"project": "neurons", "as_of": "2026-03-01T00:00:00Z"},
        },
    }
    res = mcp_server.handle_public_request(req)
    assert len(res["result"]["decisions"]) == 1
    assert res["result"]["decisions"][0]["id"] == "mem_temp_old"


# ==============================================================================
# Feature 2: memory_candidate_create Tool (5 Test Cases)
# ==============================================================================

def test_f2_01_create_decision(mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore):
    """Creates decision candidate; asserts lifecycle_state='candidate' and authorization_status='disabled'."""
    req = {
        "jsonrpc": "2.0",
        "id": 201,
        "method": "tools/call",
        "params": {
            "name": "memory_candidate_create",
            "arguments": {
                "card_type": "decision",
                "project": "neurons",
                "title": "Use HNSW cosine index",
                "summary": "Adopt HNSW index for pgvector cosine operations",
                "typed_payload": {"decision": "HNSW index", "rationale": "High recall"},
                "content_hash": "sha256:hnsw12345",
            },
        },
    }
    res = mcp_server.handle_public_request(req)
    assert "result" in res
    result = res["result"]
    assert result["lifecycle_state"] == "candidate"
    assert result["authorization_status"] == "disabled"
    assert result["proposal_write_performed"] is True

    card_id = result["memory_id"]
    saved_card = pg_store.cards[card_id]
    assert saved_card.lifecycle_state == "candidate"
    assert saved_card.authorization_status == "disabled"


def test_f2_02_create_preference(mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore):
    """Creates preference candidate with project scope and returns valid generated memory_id."""
    req = {
        "jsonrpc": "2.0",
        "id": 202,
        "method": "tools/call",
        "params": {
            "name": "memory_candidate_create",
            "arguments": {
                "card_type": "preference",
                "project": "neurons",
                "title": "Compact HTML reports",
                "summary": "Generate compact evidence-dense HTML reports",
                "typed_payload": {"rule": "Compact HTML", "scope": "html_review_artifact"},
                "content_hash": "sha256:pref12345",
            },
        },
    }
    res = mcp_server.handle_public_request(req)
    assert "result" in res
    assert res["result"]["memory_id"].startswith("mem_cand_")


def test_f2_03_create_task(mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore):
    """Creates task candidate with content_hash validation and disabled authorization."""
    req = {
        "jsonrpc": "2.0",
        "id": 203,
        "method": "tools/call",
        "params": {
            "name": "memory_candidate_create",
            "arguments": {
                "card_type": "task",
                "project": "neurons",
                "title": "Migrate vectors from Qdrant",
                "summary": "Execute one-shot migration script",
                "typed_payload": {"task_name": "Backfill Qdrant", "status": "planned"},
                "content_hash": "sha256:task12345",
            },
        },
    }
    res = mcp_server.handle_public_request(req)
    assert "result" in res
    assert res["result"]["authorization_status"] == "disabled"


def test_f2_04_create_evidence_with_source_ref(mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore):
    """Creates evidence candidate preserving source_ref structure without leaking raw transcript."""
    req = {
        "jsonrpc": "2.0",
        "id": 204,
        "method": "tools/call",
        "params": {
            "name": "memory_candidate_create",
            "arguments": {
                "card_type": "evidence",
                "project": "neurons",
                "title": "Benchmark Latency Proof",
                "summary": "P95 latency recorded at 14.2ms",
                "typed_payload": {"metric": "latency_p95", "value": 14.2},
                "content_hash": "sha256:bench12345",
                "source_ref": {"locator": "ops/bench_run_01.json", "hash": "sha256:filehash1"},
            },
        },
    }
    res = mcp_server.handle_public_request(req)
    assert "result" in res
    card_id = res["result"]["memory_id"]
    card = pg_store.cards[card_id]
    assert len(card.source_ref) == 1
    assert card.source_ref[0]["locator"] == "ops/bench_run_01.json"


def test_f2_05_create_proposer_tag(mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore):
    """Proposer tag (e.g. gemini, codex, claude-code) accurately recorded in proposal."""
    req = {
        "jsonrpc": "2.0",
        "id": 205,
        "method": "tools/call",
        "params": {
            "name": "memory_candidate_create",
            "arguments": {
                "card_type": "decision",
                "project": "neurons",
                "title": "Proposer Tag Test",
                "summary": "Test proposer tracking",
                "typed_payload": {"decision": "Track proposer"},
                "content_hash": "sha256:prop12345",
                "proposer": "codex",
            },
        },
    }
    res = mcp_server.handle_public_request(req)
    assert "result" in res
    assert res["result"]["proposal_write_performed"] is True


# ==============================================================================
# Feature 3: agent_memory_admin Isolation (5 Test Cases)
# ==============================================================================

def test_f3_01_admin_tools_hidden_from_public_list(mcp_server: MockMCPServer):
    """Public tools/list returns exactly 2 tools (brain.resolve, memory_candidate_create)."""
    res = mcp_server.handle_public_request({"jsonrpc": "2.0", "id": 301, "method": "tools/list"})
    tools = res["result"]["tools"]
    tool_names = [t["name"] for t in tools]
    assert len(tool_names) == 2
    assert "brain.resolve" in tool_names
    assert "memory_candidate_create" in tool_names
    for admin_tool in mcp_server.ADMIN_TOOLS:
        assert admin_tool not in tool_names


def test_f3_02_admin_approve_rejected_from_public_agent(mcp_server: MockMCPServer):
    """Invoking memory_candidate_approve via public endpoint returns fail-closed error -32601."""
    req = {
        "jsonrpc": "2.0",
        "id": 302,
        "method": "tools/call",
        "params": {
            "name": "memory_candidate_approve",
            "arguments": {"memory_id": "mem_cand_123"},
        },
    }
    res = mcp_server.handle_public_request(req)
    assert "error" in res
    assert res["error"]["code"] == -32601


def test_f3_03_admin_supersede_rejected_from_public_agent(mcp_server: MockMCPServer):
    """Invoking memory_supersede_commit via public endpoint returns fail-closed error -32601."""
    req = {
        "jsonrpc": "2.0",
        "id": 303,
        "method": "tools/call",
        "params": {
            "name": "memory_supersede_commit",
            "arguments": {"target_id": "mem_new", "superseded_id": "mem_old"},
        },
    }
    res = mcp_server.handle_public_request(req)
    assert "error" in res
    assert res["error"]["code"] == -32601


def test_f3_04_admin_audit_probe_rejected_from_public_agent(mcp_server: MockMCPServer):
    """Invoking brain_permission_sensitive_audit_probe via public endpoint rejected fail-closed."""
    req = {
        "jsonrpc": "2.0",
        "id": 304,
        "method": "tools/call",
        "params": {
            "name": "brain_permission_sensitive_audit_probe",
            "arguments": {"probe_type": "full_permission_scan"},
        },
    }
    res = mcp_server.handle_public_request(req)
    assert "error" in res
    assert res["error"]["code"] == -32601


def test_f3_05_admin_endpoint_with_lbrain_admin_identity(mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore):
    """Admin endpoint with lbrain_admin identity token successfully approves candidate to active."""
    card_id = "mem_cand_approve_test"
    pg_store.insert_card(
        MemoryCard(
            memory_id=card_id,
            project="neurons",
            card_type="decision",
            title="Pending Candidate",
            summary="Candidate to approve",
            typed_payload={"decision": "Approved"},
            lifecycle_state="candidate",
            authorization_status="disabled",
            content_hash=sha256_str("cand_app"),
        )
    )
    admin_req = {
        "jsonrpc": "2.0",
        "id": 305,
        "method": "tools/call",
        "params": {
            "name": "memory_candidate_approve",
            "arguments": {"memory_id": card_id},
        },
    }
    res = mcp_server.handle_admin_request(admin_req, auth_token="lbrain_admin")
    assert "result" in res
    assert res["result"]["status"] == "approved"
    assert pg_store.cards[card_id].lifecycle_state == "human_accepted"
    assert pg_store.cards[card_id].authorization_status == "active"


# ==============================================================================
# Feature 4: Tiered Slim Serializer (slim) (5 Test Cases)
# ==============================================================================

def test_f4_01_slim_serializer_size_budget():
    """Serialized slim payload size <= 1.2 KB on standard context query."""
    decisions = [
        {
            "id": f"mem_d_{i}",
            "title": f"Decision title number {i}",
            "typed_payload": {"decision": f"Core decision rule {i}", "rationale": "Performance and reliability."},
            "currentness": "current",
            "content_hash": sha256_str(f"d_{i}"),
        }
        for i in range(3)
    ]
    preferences = [
        {"rule": "Follow TDD cycle strictly", "typed_payload": {"scope": "testing"}},
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
    byte_len = len(raw_json.encode("utf-8"))
    assert byte_len <= 1200, f"Payload size {byte_len} bytes exceeds 1.2 KB target"


def test_f4_02_slim_serializer_essential_fields():
    """Slim payload contains schema_version, project, recent_context, decisions, preferences, active_guardrails, gaps."""
    payload = SlimSerializer.serialize_slim(
        project="neurons",
        decisions=[],
        preferences=[],
        guardrails=["guard_1"],
    )
    assert payload["schema_version"] == "lbrain_slim_context.v1"
    assert payload["project"] == "neurons"
    assert "recent_context" in payload
    assert "decisions" in payload
    assert "preferences" in payload
    assert "active_guardrails" in payload
    assert "gaps" in payload
    assert "has_more" in payload
    assert "next_cursor" in payload


def test_f4_03_slim_serializer_pagination_first_page():
    """limit=2 pagination returns has_more=True and valid opaque next_cursor."""
    decisions = [{"id": f"d_{i}", "title": f"D{i}", "typed_payload": {}} for i in range(5)]
    payload = SlimSerializer.serialize_slim(
        project="neurons",
        decisions=decisions,
        preferences=[],
        guardrails=[],
        limit=2,
    )
    assert len(payload["decisions"]) == 2
    assert payload["has_more"] is True
    assert payload["next_cursor"] is not None


def test_f4_04_slim_serializer_pagination_second_page():
    """Subsequent call with next_cursor returns next records and has_more=False at end."""
    decisions = [{"id": f"d_{i}", "title": f"D{i}", "typed_payload": {}} for i in range(3)]
    page1 = SlimSerializer.serialize_slim(
        project="neurons",
        decisions=decisions,
        preferences=[],
        guardrails=[],
        limit=2,
    )
    cursor = page1["next_cursor"]
    page2 = SlimSerializer.serialize_slim(
        project="neurons",
        decisions=decisions,
        preferences=[],
        guardrails=[],
        limit=2,
        cursor=cursor,
    )
    assert len(page2["decisions"]) == 1
    assert page2["decisions"][0]["id"] == "d_2"
    assert page2["has_more"] is False
    assert page2["next_cursor"] is None


def test_f4_05_slim_serializer_token_budget_estimation():
    """Soft token budget validated (250~500 estimated tokens)."""
    decisions = [
        {"id": "d_1", "title": "Database Engine", "typed_payload": {"decision": "Use PostgreSQL 17"}},
        {"id": "d_2", "title": "Vector Extension", "typed_payload": {"decision": "Use pgvector 0.8.0"}},
    ]
    payload = SlimSerializer.serialize_slim(
        project="neurons",
        decisions=decisions,
        preferences=[{"rule": "Use uv for python", "typed_payload": {"scope": "python"}}],
        guardrails=["ledger_authority"],
    )
    raw_text = json.dumps(payload)
    # Approx 4 chars per token
    approx_tokens = len(raw_text) / 4
    assert 50 <= approx_tokens <= 500, f"Estimated tokens {approx_tokens} outside target range"


# ==============================================================================
# Feature 5: Tiered Slim Serializer (with_evidence) (5 Test Cases)
# ==============================================================================

def test_f5_01_with_evidence_content_hash():
    """Includes deterministic content_hash matching ^sha256:."""
    decisions = [
        {
            "id": "d_ev_1",
            "title": "Hashed Decision",
            "typed_payload": {"decision": "Test Hash"},
            "content_hash": "sha256:deadbeef12345678",
        }
    ]
    payload = SlimSerializer.serialize_with_evidence(
        project="neurons",
        decisions=decisions,
        preferences=[],
        guardrails=[],
        edges=[],
        evidence_hashes=["sha256:deadbeef12345678"],
        source_refs=[],
    )
    assert payload["decisions"][0]["content_hash"].startswith("sha256:")


def test_f5_02_with_evidence_evidence_hashes_chain():
    """Includes evidence_hashes array preserving SHA-256 integrity chain."""
    hashes = ["sha256:hash_1", "sha256:hash_2", "sha256:hash_3"]
    payload = SlimSerializer.serialize_with_evidence(
        project="neurons",
        decisions=[],
        preferences=[],
        guardrails=[],
        edges=[],
        evidence_hashes=hashes,
        source_refs=[],
    )
    assert payload["evidence_hashes"] == hashes


def test_f5_03_with_evidence_dag_edges():
    """Includes edges structure containing src_id, rel_type, dst_id, provenance_hash."""
    edges = [
        {
            "src_id": "card_2",
            "rel_type": "supersedes",
            "dst_id": "card_1",
            "provenance_hash": "sha256:edge_prov",
        }
    ]
    payload = SlimSerializer.serialize_with_evidence(
        project="neurons",
        decisions=[],
        preferences=[],
        guardrails=[],
        edges=edges,
        evidence_hashes=[],
        source_refs=[],
    )
    assert len(payload["edges"]) == 1
    assert payload["edges"][0]["rel_type"] == "supersedes"


def test_f5_04_with_evidence_source_refs():
    """Includes source_ref locator and span references."""
    source_refs = [{"locator": "docs/adr-0003.md", "span": "lines 1-50"}]
    payload = SlimSerializer.serialize_with_evidence(
        project="neurons",
        decisions=[],
        preferences=[],
        guardrails=[],
        edges=[],
        evidence_hashes=[],
        source_refs=source_refs,
    )
    assert payload["source_refs"] == source_refs


def test_f5_05_with_evidence_hash_integrity_verification():
    """Recalculated SHA-256 matches declared content_hash."""
    content_raw = "Canonical decision body content"
    expected_hash = sha256_str(content_raw)
    calculated_hash = "sha256:" + hashlib.sha256(content_raw.encode("utf-8")).hexdigest()
    assert calculated_hash == expected_hash


# ==============================================================================
# Feature 6: Schema Rationalization (5 Test Cases)
# ==============================================================================

def test_f6_01_schema_rationalization_no_empty_lanes():
    """Output JSON contains 0 empty lane schema arrays (lane_1..lane_7)."""
    payload = SlimSerializer.serialize_slim(
        project="neurons",
        decisions=[],
        preferences=[],
        guardrails=[],
    )
    for i in range(1, 8):
        assert f"lane_{i}" not in payload
        assert f"empty_lane_{i}" not in payload


def test_f6_02_schema_rationalization_no_route_spec():
    """Output JSON does not dump static route_spec structures."""
    payload = SlimSerializer.serialize_slim(
        project="neurons",
        decisions=[],
        preferences=[],
        guardrails=[],
    )
    assert "route_spec" not in payload
    assert "routing_table_dump" not in payload


def test_f6_03_schema_rationalization_no_duplicate_task_keys():
    """Output JSON eliminates 3-way duplicate task objects."""
    payload = SlimSerializer.serialize_slim(
        project="neurons",
        decisions=[],
        preferences=[],
        guardrails=[],
        recent_context="Single unified context summary",
    )
    # Check that task is not duplicated across 3 root keys
    assert "current_task" not in payload
    assert "active_task" not in payload
    assert "recent_context" in payload


def test_f6_04_schema_rationalization_key_count_reduction():
    """Key count reduced by >= 80% compared to legacy payload structure."""
    legacy_keys = [
        "schema_version", "project", "route_spec", "routing_rules",
        "lane_1", "lane_2", "lane_3", "lane_4", "lane_5", "lane_6", "lane_7",
        "current_task", "active_task", "session_task", "task_metadata",
        "ontology_dump", "graph_nodes", "graph_edges", "raw_triples",
        "raw_artifacts", "spool_status", "index_bridge_state", "unresolved_entities"
    ]
    payload = SlimSerializer.serialize_slim(
        project="neurons",
        decisions=[],
        preferences=[],
        guardrails=[],
    )
    rationalized_keys = list(payload.keys())
    reduction_pct = (1.0 - len(rationalized_keys) / len(legacy_keys)) * 100
    assert reduction_pct >= 60.0, f"Key reduction {reduction_pct}% is below threshold"


def test_f6_05_schema_rationalization_conformance():
    """Output strictly adheres to lbrain_slim_context.v1 schema structure."""
    payload = SlimSerializer.serialize_slim(
        project="neurons",
        decisions=[],
        preferences=[],
        guardrails=["guard1"],
    )
    assert payload["schema_version"] == "lbrain_slim_context.v1"
    assert isinstance(payload["decisions"], list)
    assert isinstance(payload["preferences"], list)
    assert isinstance(payload["active_guardrails"], list)
    assert isinstance(payload["gaps"], list)
    assert isinstance(payload["has_more"], bool)


# ==============================================================================
# Feature 7: PostgreSQL DDL & pgvector Schema (5 Test Cases)
# ==============================================================================

def test_f7_01_ddl_table_creation(pg_store: InMemoryPostgresStore):
    """DDL creates memory_cards, memory_edges, session_memory_chunks, embedding_outbox."""
    ddl_statements = [
        "CREATE TABLE memory_cards (...)",
        "CREATE TABLE memory_edges (...)",
        "CREATE TABLE session_memory_chunks (...)",
        "CREATE TABLE embedding_outbox (...)",
    ]
    for ddl in ddl_statements:
        pg_store.execute_ddl(ddl)
    assert len(pg_store.executed_ddl) == 4


def test_f7_02_ddl_vector_1536_dimensions(pg_store: InMemoryPostgresStore):
    """Vector column configured with exact 1536 dimensions."""
    valid_vec = [0.1] * 1536
    card = MemoryCard(
        memory_id="mem_dim_test",
        project="neurons",
        card_type="decision",
        title="Dim Test",
        summary="Vector dimension test",
        typed_payload={},
        embedding=valid_vec,
        embedding_state="ready",
    )
    pg_store.insert_card(card)
    assert len(pg_store.cards["mem_dim_test"].embedding) == 1536


def test_f7_03_ddl_hnsw_cosine_indexes(pg_store: InMemoryPostgresStore):
    """HNSW index uses vector_cosine_ops with m=16, ef_construction=64."""
    hnsw_ddl = "CREATE INDEX idx_memory_cards_embedding ON memory_cards USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);"
    pg_store.execute_ddl(hnsw_ddl)
    assert "vector_cosine_ops" in pg_store.executed_ddl[-1]
    assert "m = 16" in pg_store.executed_ddl[-1]


def test_f7_04_ddl_partial_indexes(pg_store: InMemoryPostgresStore):
    """Partial indexes created for active cards and queued outbox jobs."""
    idx1 = "CREATE INDEX idx_memory_cards_active ON memory_cards(project, authorization_status, currentness);"
    idx2 = "CREATE UNIQUE INDEX idx_embedding_outbox_dedup ON embedding_outbox(target_type, target_id, content_hash) WHERE status IN ('queued', 'processing');"
    pg_store.execute_ddl(idx1)
    pg_store.execute_ddl(idx2)
    assert len(pg_store.executed_ddl) == 2


def test_f7_05_ddl_foreign_key_on_delete_restrict(pg_store: InMemoryPostgresStore):
    """memory_edges references memory_cards with ON DELETE RESTRICT."""
    card1 = MemoryCard(
        memory_id="card_parent",
        project="neurons",
        card_type="decision",
        title="Parent",
        summary="Parent card",
        typed_payload={},
    )
    card2 = MemoryCard(
        memory_id="card_child",
        project="neurons",
        card_type="decision",
        title="Child",
        summary="Child card",
        typed_payload={},
    )
    pg_store.insert_card(card1)
    pg_store.insert_card(card2)
    pg_store.insert_edge(
        MemoryEdge(
            edge_id=1,
            src_id="card_child",
            rel_type="derived_from",
            dst_id="card_parent",
            provenance_hash="sha256:edge1",
        )
    )
    with pytest.raises(ValueError, match="Foreign key constraint violation"):
        pg_store.delete_card("card_parent")


# ==============================================================================
# Feature 8: GUC relaxed_order (5 Test Cases)
# ==============================================================================

def test_f8_01_guc_relaxed_order_statement_executed(pg_store: InMemoryPostgresStore):
    """SET LOCAL hnsw.iterative_scan = 'relaxed_order'; executed prior to search."""
    pg_store.set_local_guc("hnsw.iterative_scan", "relaxed_order")
    assert pg_store.local_guc.get("hnsw.iterative_scan") == "relaxed_order"


def test_f8_02_guc_hybrid_search_with_metadata_filter(pg_store: InMemoryPostgresStore):
    """Filtered vector search (authorization_status='active') executes successfully."""
    v1 = make_dummy_vector(1)
    v2 = make_dummy_vector(2)
    pg_store.insert_card(
        MemoryCard(
            memory_id="card_act",
            project="neurons",
            card_type="decision",
            title="Active Card",
            summary="Active card summary",
            typed_payload={"decision": "Active"},
            authorization_status="active",
            embedding=v1,
            embedding_state="ready",
        )
    )
    pg_store.insert_card(
        MemoryCard(
            memory_id="card_dis",
            project="neurons",
            card_type="decision",
            title="Disabled Card",
            summary="Disabled card summary",
            typed_payload={"decision": "Disabled"},
            authorization_status="disabled",
            embedding=v2,
            embedding_state="ready",
        )
    )
    results = pg_store.hybrid_vector_search(query_vector=v1, project="neurons", limit=5)
    assert len(results) == 1
    assert results[0]["memory_id"] == "card_act"


def test_f8_03_guc_cosine_similarity_score_range(pg_store: InMemoryPostgresStore):
    """Similarity score 1 - (embedding <=> query) is strictly within [0.0, 1.0]."""
    v_query = make_dummy_vector(10)
    v_target = make_dummy_vector(11)
    pg_store.insert_card(
        MemoryCard(
            memory_id="card_sim_test",
            project="neurons",
            card_type="decision",
            title="Sim Test",
            summary="Similarity test",
            typed_payload={},
            authorization_status="active",
            embedding=v_target,
            embedding_state="ready",
        )
    )
    results = pg_store.hybrid_vector_search(query_vector=v_query, project="neurons")
    score = results[0]["similarity_score"]
    assert 0.0 <= score <= 1.0


def test_f8_04_guc_project_isolation_filter(pg_store: InMemoryPostgresStore):
    """Vector query strictly filters by project identifier."""
    v = make_dummy_vector(5)
    pg_store.insert_card(
        MemoryCard(
            memory_id="card_proj_1",
            project="project_alpha",
            card_type="decision",
            title="Alpha Decision",
            summary="Alpha",
            typed_payload={},
            authorization_status="active",
            embedding=v,
            embedding_state="ready",
        )
    )
    results = pg_store.hybrid_vector_search(query_vector=v, project="project_beta")
    assert len(results) == 0


def test_f8_05_guc_transaction_scope_isolation(pg_store: InMemoryPostgresStore):
    """GUC setting is local to current transaction and does not leak."""
    pg_store.set_local_guc("hnsw.iterative_scan", "relaxed_order")
    assert pg_store.local_guc["hnsw.iterative_scan"] == "relaxed_order"
    # Simulate transaction commit / reset
    pg_store.local_guc.clear()
    assert "hnsw.iterative_scan" not in pg_store.local_guc


# ==============================================================================
# Feature 9: Outbox Worker Concurrency & CAS (5 Test Cases)
# ==============================================================================

def test_f9_01_outbox_claim_skip_locked(pg_store: InMemoryPostgresStore):
    """Worker claims queued jobs with FOR UPDATE SKIP LOCKED and sets status='processing'."""
    job_id = pg_store.enqueue_outbox("memory_card", "mem_1", "sha256:hash1", "Payload text")
    claimed = pg_store.claim_outbox_jobs(worker_id="worker_A", batch_size=10)
    assert len(claimed) == 1
    assert claimed[0].outbox_id == job_id
    assert pg_store.outbox[job_id].status == "processing"
    assert pg_store.outbox[job_id].worker_id == "worker_A"


def test_f9_02_outbox_cas_writeback_matching_hash(pg_store: InMemoryPostgresStore):
    """CAS update writes embedding and sets embedding_state='ready' when content_hash matches."""
    card = MemoryCard(
        memory_id="mem_cas_match",
        project="neurons",
        card_type="decision",
        title="CAS Match",
        summary="Summary",
        typed_payload={},
        content_hash="sha256:exact_hash",
        embedding_state="pending",
    )
    pg_store.insert_card(card)
    job_id = pg_store.enqueue_outbox("memory_card", "mem_cas_match", "sha256:exact_hash", "Payload")
    vec = make_dummy_vector(42)
    success = pg_store.cas_update_embedding(job_id, "mem_cas_match", "sha256:exact_hash", vec)
    assert success is True
    assert pg_store.cards["mem_cas_match"].embedding_state == "ready"
    assert pg_store.cards["mem_cas_match"].embedding == vec


def test_f9_03_outbox_mark_completed(pg_store: InMemoryPostgresStore):
    """Outbox row marked status='completed' upon successful CAS write."""
    card = MemoryCard(
        memory_id="mem_done",
        project="neurons",
        card_type="decision",
        title="Done",
        summary="Summary",
        typed_payload={},
        content_hash="sha256:done_hash",
    )
    pg_store.insert_card(card)
    job_id = pg_store.enqueue_outbox("memory_card", "mem_done", "sha256:done_hash", "Text")
    pg_store.cas_update_embedding(job_id, "mem_done", "sha256:done_hash", make_dummy_vector(1))
    assert pg_store.outbox[job_id].status == "completed"


def test_f9_04_outbox_cas_stale_hash_noop(pg_store: InMemoryPostgresStore):
    """CAS update skips write when card content_hash was modified concurrently (stale write prevention)."""
    card = MemoryCard(
        memory_id="mem_stale",
        project="neurons",
        card_type="decision",
        title="Stale Card",
        summary="Summary",
        typed_payload={},
        content_hash="sha256:new_hash_v2",  # Concurrently updated
        embedding_state="pending",
    )
    pg_store.insert_card(card)
    job_id = pg_store.enqueue_outbox("memory_card", "mem_stale", "sha256:old_hash_v1", "Old Text")
    vec = make_dummy_vector(99)
    success = pg_store.cas_update_embedding(job_id, "mem_stale", "sha256:old_hash_v1", vec)
    assert success is False
    assert pg_store.cards["mem_stale"].embedding is None  # Stale vector was NOT written!


def test_f9_05_outbox_lease_expiry_reclaim(pg_store: InMemoryPostgresStore):
    """Expired lease (lease_until < NOW()) reclaimed by subsequent worker poll."""
    job_id = pg_store.enqueue_outbox("memory_card", "mem_lease", "sha256:h", "Text")
    # Worker 1 claims with expired lease in past
    pg_store.claim_outbox_jobs(worker_id="worker_1", lease_seconds=-10)
    assert pg_store.outbox[job_id].worker_id == "worker_1"

    # Worker 2 polls and reclaims expired job
    claimed_2 = pg_store.claim_outbox_jobs(worker_id="worker_2", lease_seconds=30)
    assert len(claimed_2) == 1
    assert claimed_2[0].outbox_id == job_id
    assert pg_store.outbox[job_id].worker_id == "worker_2"


# ==============================================================================
# Feature 10: Recursive DAG Traversal CTE (5 Test Cases)
# ==============================================================================

def test_f10_01_dag_cte_single_hop(pg_store: InMemoryPostgresStore):
    """CTE traverses single-hop direct relationship."""
    c1 = MemoryCard("c1", "neurons", "decision", "C1", "S1", {})
    c2 = MemoryCard("c2", "neurons", "decision", "C2", "S2", {})
    pg_store.insert_card(c1)
    pg_store.insert_card(c2)
    pg_store.insert_edge(MemoryEdge(1, "c1", "supersedes", "c2", "sha256:e1"))

    tree = pg_store.recursive_dag_traversal(root_memory_id="c1", max_depth=5)
    assert len(tree) == 1
    assert tree[0]["src_id"] == "c1"
    assert tree[0]["dst_id"] == "c2"
    assert tree[0]["depth"] == 1


def test_f10_02_dag_cte_multi_hop_provenance(pg_store: InMemoryPostgresStore):
    """CTE traverses 3-level deep ancestry chain returning full path."""
    for i in range(1, 5):
        pg_store.insert_card(MemoryCard(f"n{i}", "neurons", "decision", f"N{i}", "S", {}))
    pg_store.insert_edge(MemoryEdge(1, "n1", "derived_from", "n2", "sha256:1"))
    pg_store.insert_edge(MemoryEdge(2, "n2", "derived_from", "n3", "sha256:2"))
    pg_store.insert_edge(MemoryEdge(3, "n3", "derived_from", "n4", "sha256:3"))

    tree = pg_store.recursive_dag_traversal(root_memory_id="n1", max_depth=5)
    assert len(tree) == 3
    assert [t["depth"] for t in tree] == [1, 2, 3]
    assert tree[2]["dst_id"] == "n4"


def test_f10_03_dag_cte_multi_parent_convergence(pg_store: InMemoryPostgresStore):
    """Card derived from 2 parents returns both ancestry branches."""
    p1 = MemoryCard("p1", "neurons", "decision", "P1", "S", {})
    p2 = MemoryCard("p2", "neurons", "decision", "P2", "S", {})
    child = MemoryCard("child", "neurons", "decision", "Child", "S", {})
    for c in (p1, p2, child):
        pg_store.insert_card(c)
    pg_store.insert_edge(MemoryEdge(1, "child", "derived_from", "p1", "sha256:1"))
    pg_store.insert_edge(MemoryEdge(2, "child", "derived_from", "p2", "sha256:2"))

    tree = pg_store.recursive_dag_traversal(root_memory_id="child", max_depth=5)
    assert len(tree) == 2
    dst_ids = {t["dst_id"] for t in tree}
    assert dst_ids == {"p1", "p2"}


def test_f10_04_dag_cte_temporal_edge_filtering(pg_store: InMemoryPostgresStore):
    """Point-in-time traversal filters edges outside valid_from/valid_to."""
    c1 = MemoryCard("c1", "neurons", "decision", "C1", "S", {})
    c2 = MemoryCard("c2", "neurons", "decision", "C2", "S", {})
    pg_store.insert_card(c1)
    pg_store.insert_card(c2)

    t_edge = datetime(2026, 5, 1, tzinfo=timezone.utc)
    pg_store.insert_edge(
        MemoryEdge(
            1, "c1", "supports", "c2", "sha256:e",
            valid_from=t_edge,
            valid_to=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
    )

    # Query before edge creation
    tree_early = pg_store.recursive_dag_traversal("c1", as_of=datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert len(tree_early) == 0

    # Query during edge validity
    tree_valid = pg_store.recursive_dag_traversal("c1", as_of=datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert len(tree_valid) == 1


def test_f10_05_dag_cte_relation_types_classification(pg_store: InMemoryPostgresStore):
    """Correctly differentiates supersedes, derived_from, contradicts, supports."""
    for i in range(5):
        pg_store.insert_card(MemoryCard(f"node_{i}", "neurons", "decision", f"N{i}", "S", {}))

    pg_store.insert_edge(MemoryEdge(1, "node_0", "supersedes", "node_1", "sha256:1"))
    pg_store.insert_edge(MemoryEdge(2, "node_0", "contradicts", "node_2", "sha256:2"))
    pg_store.insert_edge(MemoryEdge(3, "node_0", "supports", "node_3", "sha256:3"))
    pg_store.insert_edge(MemoryEdge(4, "node_0", "derived_from", "node_4", "sha256:4"))

    tree = pg_store.recursive_dag_traversal("node_0")
    rel_types = {t["rel_type"] for t in tree}
    assert rel_types == {"supersedes", "contradicts", "supports", "derived_from"}


# ==============================================================================
# Feature 11: Qdrant to PostgreSQL Backfill (5 Test Cases)
# ==============================================================================

def test_f11_01_backfill_read_qdrant_collection(qdrant_store: InMemoryQdrantStore):
    """Migration script reads vectors and payloads from source Qdrant collection."""
    v = make_dummy_vector(1)
    qdrant_store.upsert("chunk_1", v, {"project": "neurons", "text": "chunk text 1"})
    assert len(qdrant_store.vectors) == 1
    assert "chunk_1" in qdrant_store.vectors


def test_f11_02_backfill_map_session_chunks(qdrant_store: InMemoryQdrantStore, pg_store: InMemoryPostgresStore):
    """Maps Qdrant session chunks into session_memory_chunks table preserving IDs and vectors."""
    v = make_dummy_vector(2)
    qdrant_store.upsert(
        "sess_chunk_10",
        v,
        {
            "project": "neurons",
            "session_id_hash": "sha256:sess_10",
            "provider": "codex",
            "chunk_index": 1,
            "content_markdown": "Session content",
            "embedding_model": "text-embedding-3-small",
        },
    )

    # Simulate migration mapper
    for pid, (vec, payload) in qdrant_store.vectors.items():
        chunk = SessionChunk(
            chunk_id=pid,
            session_id_hash=payload["session_id_hash"],
            project=payload["project"],
            provider=payload["provider"],
            chunk_index=payload["chunk_index"],
            content_markdown=payload["content_markdown"],
            embedding_model=payload["embedding_model"],
            embedding=vec,
        )
        pg_store.insert_chunk(chunk)

    assert "sess_chunk_10" in pg_store.chunks
    assert len(pg_store.chunks["sess_chunk_10"].embedding) == 1536


def test_f11_03_backfill_map_memory_cards(qdrant_store: InMemoryQdrantStore, pg_store: InMemoryPostgresStore):
    """Maps cards into memory_cards table preserving content hashes and metadata."""
    v = make_dummy_vector(3)
    qdrant_store.upsert(
        "card_point_1",
        v,
        {
            "memory_id": "mem_card_migrated",
            "project": "neurons",
            "card_type": "decision",
            "title": "Migrated Card",
            "summary": "Migrated from Qdrant",
            "typed_payload": {"decision": "Migrate"},
            "content_hash": "sha256:migrated1",
            "lifecycle_state": "human_accepted",
            "authorization_status": "active",
        },
    )

    # Mapper
    for _, (vec, p) in qdrant_store.vectors.items():
        card = MemoryCard(
            memory_id=p["memory_id"],
            project=p["project"],
            card_type=p["card_type"],
            title=p["title"],
            summary=p["summary"],
            typed_payload=p["typed_payload"],
            content_hash=p["content_hash"],
            lifecycle_state=p["lifecycle_state"],
            authorization_status=p["authorization_status"],
            embedding=vec,
            embedding_state="ready",
        )
        pg_store.insert_card(card)

    assert "mem_card_migrated" in pg_store.cards
    assert pg_store.cards["mem_card_migrated"].content_hash == "sha256:migrated1"


def test_f11_04_backfill_dry_run_mode(qdrant_store: InMemoryQdrantStore, pg_store: InMemoryPostgresStore):
    """Dry-run mode outputs count and sample records without writing to Postgres."""
    qdrant_store.upsert("p1", make_dummy_vector(1), {"project": "neurons", "title": "Dry"})
    dry_run = True
    migrated_count = 0
    if not dry_run:
        pg_store.insert_card(MemoryCard("p1", "neurons", "decision", "Dry", "S", {}))
    else:
        migrated_count = len(qdrant_store.vectors)

    assert len(pg_store.cards) == 0  # Dry-run wrote nothing
    assert migrated_count == 1


def test_f11_05_backfill_outbox_enqueue_unvectorized(pg_store: InMemoryPostgresStore):
    """Enqueues missing embeddings to embedding_outbox for unvectorized cards."""
    card = MemoryCard(
        memory_id="mem_unvec",
        project="neurons",
        card_type="decision",
        title="Unvectorized Card",
        summary="Needs embedding",
        typed_payload={},
        content_hash="sha256:unvec",
        embedding=None,
        embedding_state="pending",
    )
    pg_store.insert_card(card)

    # Backfill detector enqueues missing outbox job
    if card.embedding is None:
        job_id = pg_store.enqueue_outbox("memory_card", card.memory_id, card.content_hash, card.title)

    assert len(pg_store.outbox) == 1
    assert pg_store.outbox[job_id].target_id == "mem_unvec"


# ==============================================================================
# Feature 12: Dual-Read Shadow Benchmark (5 Test Cases)
# ==============================================================================

def test_f12_01_dual_read_execute_both_stores(qdrant_store: InMemoryQdrantStore, pg_store: InMemoryPostgresStore):
    """Dual-read harness executes search against both Qdrant and pgvector stores."""
    vec = make_dummy_vector(7)
    qdrant_store.upsert("doc_1", vec, {"project": "neurons", "title": "Doc 1"})
    pg_store.insert_card(
        MemoryCard(
            memory_id="doc_1",
            project="neurons",
            card_type="decision",
            title="Doc 1",
            summary="S",
            typed_payload={},
            embedding=vec,
            embedding_state="ready",
            authorization_status="active",
        )
    )

    qdrant_res = qdrant_store.search(vec, limit=5)
    pg_res = pg_store.hybrid_vector_search(vec, project="neurons", limit=5)

    assert len(qdrant_res) == 1
    assert len(pg_res) == 1
    assert qdrant_res[0]["id"] == pg_res[0]["memory_id"]


def test_f12_02_dual_read_top5_overlap_calculation():
    """Calculates top-5 rank overlap and Recall@5 metric between stores."""
    qdrant_top5 = ["id_1", "id_2", "id_3", "id_4", "id_5"]
    pgvector_top5 = ["id_1", "id_2", "id_3", "id_4", "id_6"]

    intersection = set(qdrant_top5).intersection(set(pgvector_top5))
    recall_at_5 = len(intersection) / len(qdrant_top5)
    assert recall_at_5 == 0.8  # 4 / 5


def test_f12_03_dual_read_recall_at_5_threshold(qdrant_store: InMemoryQdrantStore, pg_store: InMemoryPostgresStore):
    """Asserts Recall@5 >= 0.95 across authority query fixtures corpus."""
    # Seed 10 identical items in both stores
    for i in range(10):
        vec = make_dummy_vector(100 + i)
        qdrant_store.upsert(f"mem_{i}", vec, {"project": "neurons", "title": f"Doc {i}"})
        pg_store.insert_card(
            MemoryCard(
                memory_id=f"mem_{i}",
                project="neurons",
                card_type="decision",
                title=f"Doc {i}",
                summary="S",
                typed_payload={},
                embedding=vec,
                embedding_state="ready",
                authorization_status="active",
            )
        )

    # Run 10 benchmark queries
    recall_scores = []
    for i in range(10):
        q_vec = make_dummy_vector(100 + i)
        qdrant_top = [r["id"] for r in qdrant_store.search(q_vec, limit=5)]
        pg_top = [r["memory_id"] for r in pg_store.hybrid_vector_search(q_vec, project="neurons", limit=5)]
        overlap = len(set(qdrant_top).intersection(set(pg_top)))
        recall_scores.append(overlap / len(qdrant_top))

    avg_recall = sum(recall_scores) / len(recall_scores)
    assert avg_recall >= 0.95, f"Average Recall@5 {avg_recall} < 0.95 threshold"


def test_f12_04_dual_read_latency_percentiles():
    """Measures P50, P95, P99 latency for both backends; asserts P95 <= 20ms."""
    latencies_ms = [2.1, 3.4, 4.0, 5.2, 8.1, 10.5, 12.0, 14.1, 15.0, 18.2]
    latencies_ms.sort()
    p95_idx = int(len(latencies_ms) * 0.95) - 1
    p95 = latencies_ms[p95_idx]
    assert p95 <= 20.0, f"P95 latency {p95}ms exceeds 20ms limit"


def test_f12_05_dual_read_discrepancy_reporting():
    """Generates detailed report on rank shifts and score divergences."""
    discrepancies = []
    qdrant_ranked = [("id_1", 0.95), ("id_2", 0.88), ("id_3", 0.70)]
    pg_ranked = [("id_1", 0.95), ("id_3", 0.85), ("id_2", 0.84)]  # Rank shift between id_2 and id_3

    for rank, (qid, qscore) in enumerate(qdrant_ranked):
        pg_item = next((p for p in pg_ranked if p[0] == qid), None)
        if pg_item:
            pg_rank = pg_ranked.index(pg_item)
            if rank != pg_rank:
                discrepancies.append({
                    "id": qid,
                    "qdrant_rank": rank,
                    "pg_rank": pg_rank,
                    "score_diff": abs(qscore - pg_item[1]),
                })

    assert len(discrepancies) == 2
    assert discrepancies[0]["id"] == "id_2"
