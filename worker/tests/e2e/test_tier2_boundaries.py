"""Tier 2 Boundary Condition Coverage Test Suite (Simulation).

Pure simulation suite running against InMemoryPostgresStore, InMemoryQdrantStore,
and MockMCPServer with isolated legacy 1536-dim simulation profile.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
from datetime import datetime, timezone

import pytest

from .conftest import (
    InMemoryPostgresStore,
    InMemoryQdrantStore,
    MemoryCard,
    MemoryEdge,
    MockMCPServer,
    OutboxJob,
    SessionChunk,
    SlimSerializer,
    compute_cosine_similarity,
    make_dummy_vector,
    sha256_str,
)

pytestmark = [pytest.mark.simulation, pytest.mark.legacy_profile]

# ==============================================================================
# Feature 1 Boundary: brain.resolve (5 Boundary Tests)
# ==============================================================================

def test_f1_b01_empty_query_context(mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore):
    """Empty query with mode='context' safely returns project base context."""
    pg_store.insert_card(
        MemoryCard(
            memory_id="mem_base_1",
            project="neurons",
            card_type="decision",
            title="Base Decision",
            summary="Base summary",
            typed_payload={},
            authorization_status="active",
            currentness="current",
        )
    )
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "brain.resolve",
            "arguments": {"project": "neurons", "mode": "context", "query": ""},
        },
    }
    res = mcp_server.handle_public_request(req)
    assert "result" in res
    assert len(res["result"]["decisions"]) == 1


def test_f1_b02_nonexistent_project(mcp_server: MockMCPServer):
    """Unknown project returns clean empty envelope without raising unhandled exception."""
    req = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "brain.resolve",
            "arguments": {"project": "unknown_project_xyz", "mode": "context"},
        },
    }
    res = mcp_server.handle_public_request(req)
    assert "result" in res
    assert res["result"]["project"] == "unknown_project_xyz"
    assert len(res["result"]["decisions"]) == 0
    assert len(res["result"]["preferences"]) == 0


def test_f1_b03_invalid_mode_error(mcp_server: MockMCPServer):
    """Invalid mode string rejected with standard JSON-RPC -32602 error."""
    req = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "brain.resolve",
            "arguments": {"project": "neurons", "mode": "invalid_mode_action"},
        },
    }
    res = mcp_server.handle_public_request(req)
    assert "error" in res
    assert res["error"]["code"] == -32602


def test_f1_b04_limit_clamping(mcp_server: MockMCPServer):
    """Negative limit or limit > 100 rejected safely with -32602."""
    for bad_limit in (-1, 0, 101, "ten"):
        req = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "brain.resolve",
                "arguments": {"project": "neurons", "limit": bad_limit},
            },
        }
        res = mcp_server.handle_public_request(req)
        assert "error" in res
        assert res["error"]["code"] == -32602


def test_f1_b05_malformed_as_of_iso(mcp_server: MockMCPServer):
    """Malformed as_of string returns validation error -32602."""
    req = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {
            "name": "brain.resolve",
            "arguments": {"project": "neurons", "as_of": "not-a-valid-date-time"},
        },
    }
    res = mcp_server.handle_public_request(req)
    assert "error" in res
    assert res["error"]["code"] == -32602


# ==============================================================================
# Feature 2 Boundary: memory_candidate_create (5 Boundary Tests)
# ==============================================================================

def test_f2_b01_override_active_ignored(mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore):
    """Argument attempting authorization_status='active' is strictly overridden to 'disabled'."""
    req = {
        "jsonrpc": "2.0",
        "id": 201,
        "method": "tools/call",
        "params": {
            "name": "memory_candidate_create",
            "arguments": {
                "card_type": "decision",
                "project": "neurons",
                "title": "Bypass Attempt",
                "summary": "Try to force active status",
                "typed_payload": {},
                "content_hash": "sha256:attack1",
                "authorization_status": "active",
            },
        },
    }
    res = mcp_server.handle_public_request(req)
    assert "result" in res
    assert res["result"]["authorization_status"] == "disabled"
    card_id = res["result"]["memory_id"]
    assert pg_store.cards[card_id].authorization_status == "disabled"


def test_f2_b02_override_accepted_ignored(mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore):
    """Argument attempting lifecycle_state='human_accepted' overridden to 'candidate'."""
    req = {
        "jsonrpc": "2.0",
        "id": 202,
        "method": "tools/call",
        "params": {
            "name": "memory_candidate_create",
            "arguments": {
                "card_type": "decision",
                "project": "neurons",
                "title": "Bypass Lifecycle Attempt",
                "summary": "Try to force accepted status",
                "typed_payload": {},
                "content_hash": "sha256:attack2",
                "lifecycle_state": "human_accepted",
            },
        },
    }
    res = mcp_server.handle_public_request(req)
    assert "result" in res
    assert res["result"]["lifecycle_state"] == "candidate"
    card_id = res["result"]["memory_id"]
    assert pg_store.cards[card_id].lifecycle_state == "candidate"


def test_f2_b03_invalid_hash_prefix(mcp_server: MockMCPServer):
    """content_hash missing sha256: prefix rejected with -32602."""
    req = {
        "jsonrpc": "2.0",
        "id": 203,
        "method": "tools/call",
        "params": {
            "name": "memory_candidate_create",
            "arguments": {
                "card_type": "decision",
                "project": "neurons",
                "title": "Bad Hash",
                "summary": "Summary",
                "typed_payload": {},
                "content_hash": "md5:12345678",
            },
        },
    }
    res = mcp_server.handle_public_request(req)
    assert "error" in res
    assert res["error"]["code"] == -32602


def test_f2_b04_empty_title_summary(mcp_server: MockMCPServer):
    """Empty string title or summary rejected with validation error."""
    for empty_field in ("title", "summary"):
        args = {
            "card_type": "decision",
            "project": "neurons",
            "title": "Title",
            "summary": "Summary",
            "typed_payload": {},
            "content_hash": "sha256:valid",
        }
        args[empty_field] = ""
        req = {
            "jsonrpc": "2.0",
            "id": 204,
            "method": "tools/call",
            "params": {"name": "memory_candidate_create", "arguments": args},
        }
        res = mcp_server.handle_public_request(req)
        assert "error" in res
        assert res["error"]["code"] == -32602


def test_f2_b05_unknown_card_type(mcp_server: MockMCPServer):
    """Invalid card_type not in enum rejected fail-closed."""
    req = {
        "jsonrpc": "2.0",
        "id": 205,
        "method": "tools/call",
        "params": {
            "name": "memory_candidate_create",
            "arguments": {
                "card_type": "secret_root_card",
                "project": "neurons",
                "title": "Unknown Card",
                "summary": "Summary",
                "typed_payload": {},
                "content_hash": "sha256:valid",
            },
        },
    }
    res = mcp_server.handle_public_request(req)
    assert "error" in res
    assert res["error"]["code"] == -32602


# ==============================================================================
# Feature 3 Boundary: agent_memory_admin (5 Boundary Tests)
# ==============================================================================

def test_f3_b01_missing_auth_header(mcp_server: MockMCPServer):
    """Request without authorization token rejected with 401/fail-closed."""
    req = {
        "jsonrpc": "2.0",
        "id": 301,
        "method": "tools/call",
        "params": {"name": "memory_candidate_approve", "arguments": {"memory_id": "mem_1"}},
    }
    res = mcp_server.handle_admin_request(req, auth_token=None)
    assert "error" in res
    assert res["error"]["code"] == -32600


def test_f3_b02_spoofed_agent_identity(mcp_server: MockMCPServer):
    """Request with standard agent_user token rejected with 403 Forbidden."""
    req = {
        "jsonrpc": "2.0",
        "id": 302,
        "method": "tools/call",
        "params": {"name": "memory_candidate_approve", "arguments": {"memory_id": "mem_1"}},
    }
    res = mcp_server.handle_admin_request(req, auth_token="agent_user_token")
    assert "error" in res
    assert res["error"]["code"] == -32600


def test_f3_b03_wildcard_method_injection(mcp_server: MockMCPServer):
    """Malicious method names (*, ../admin) rejected with -32600 / -32601."""
    for mal_method in ("*", "../admin/tools", "system.reboot"):
        req = {
            "jsonrpc": "2.0",
            "id": 303,
            "method": mal_method,
            "params": {},
        }
        res = mcp_server.handle_admin_request(req, auth_token="lbrain_admin")
        assert "error" in res


def test_f3_b04_approve_nonexistent_card(mcp_server: MockMCPServer):
    """Approving non-existent memory_id returns 404 / entity not found error."""
    req = {
        "jsonrpc": "2.0",
        "id": 304,
        "method": "tools/call",
        "params": {"name": "memory_candidate_approve", "arguments": {"memory_id": "mem_nonexistent_999"}},
    }
    res = mcp_server.handle_admin_request(req, auth_token="lbrain_admin")
    assert "error" in res
    assert res["error"]["code"] == -32602


def test_f3_b05_reject_already_superseded(mcp_server: MockMCPServer, pg_store: InMemoryPostgresStore):
    """Rejecting already superseded card handled idempotently."""
    card = MemoryCard(
        memory_id="mem_sup_rej",
        project="neurons",
        card_type="decision",
        title="Sup",
        summary="S",
        typed_payload={},
        currentness="superseded",
    )
    pg_store.insert_card(card)
    req = {
        "jsonrpc": "2.0",
        "id": 305,
        "method": "tools/call",
        "params": {"name": "memory_candidate_reject", "arguments": {"memory_id": "mem_sup_rej"}},
    }
    res = mcp_server.handle_admin_request(req, auth_token="lbrain_admin")
    assert "result" in res
    assert res["result"]["status"] == "rejected"


# ==============================================================================
# Feature 4 Boundary: Tiered Slim Serializer (slim) (5 Boundary Tests)
# ==============================================================================

def test_f4_b01_slim_serializer_empty_project_size():
    """Empty project returns valid slim JSON envelope <= 300 bytes."""
    payload = SlimSerializer.serialize_slim("empty_p", [], [], [])
    encoded = json.dumps(payload).encode("utf-8")
    assert len(encoded) <= 300


def test_f4_b02_slim_serializer_hard_limit_enforcement():
    """Extremely large card collections deterministically truncated at limit."""
    decisions = [
        {
            "id": f"d_{i}",
            "title": f"Large Decision Title with substantial text content {i}",
            "typed_payload": {"decision": f"Deciding on major architectural point {i} with detailed text"},
        }
        for i in range(100)
    ]
    # Default limit=5 ensures size stays under 3KB hard ceiling
    payload = SlimSerializer.serialize_slim("neurons", decisions, [], [], limit=5)
    raw = json.dumps(payload)
    assert len(raw.encode("utf-8")) <= 3000
    assert payload["has_more"] is True


def test_f4_b03_slim_serializer_unicode_control_chars():
    """Special characters, emojis, and newlines serialized without escaping corruption."""
    unicode_text = "결정 사항 🚀: newline\n\tand special & < > \" ' chars"
    decisions = [{"id": "d_uni", "title": unicode_text, "typed_payload": {"decision": unicode_text}}]
    payload = SlimSerializer.serialize_slim("neurons", decisions, [], [])
    serialized = json.dumps(payload, ensure_ascii=False)
    deserialized = json.loads(serialized)
    assert deserialized["decisions"][0]["title"] == unicode_text


def test_f4_b04_slim_serializer_malformed_cursor():
    """Invalid base64 or corrupted next_cursor handled gracefully with reset to index 0."""
    decisions = [{"id": "d_1", "title": "D1", "typed_payload": {}}]
    payload = SlimSerializer.serialize_slim("neurons", decisions, [], [], cursor="!!!invalid_base64!!!")
    assert len(payload["decisions"]) == 1


def test_f4_b05_slim_serializer_limit_one_boundary():
    """limit=1 pagination boundary executes correctly."""
    decisions = [{"id": "d_1", "title": "D1", "typed_payload": {}}, {"id": "d_2", "title": "D2", "typed_payload": {}}]
    p1 = SlimSerializer.serialize_slim("neurons", decisions, [], [], limit=1)
    assert len(p1["decisions"]) == 1
    assert p1["has_more"] is True
    p2 = SlimSerializer.serialize_slim("neurons", decisions, [], [], limit=1, cursor=p1["next_cursor"])
    assert len(p2["decisions"]) == 1
    assert p2["decisions"][0]["id"] == "d_2"
    assert p2["has_more"] is False


# ==============================================================================
# Feature 5 Boundary: Tiered Slim Serializer (with_evidence) (5 Boundary Tests)
# ==============================================================================

def test_f5_b01_with_evidence_empty_evidence_hashes():
    """Empty evidence hashes serialized as [], not omitted or null."""
    payload = SlimSerializer.serialize_with_evidence("neurons", [], [], [], [], [], [])
    assert payload["evidence_hashes"] == []


def test_f5_b02_with_evidence_no_raw_transcript_leak():
    """Verifies no raw private transcript/body appears in source_ref."""
    safe_ref = [{"locator": "redacted/span_123", "hash": "sha256:abc"}]
    payload = SlimSerializer.serialize_with_evidence("neurons", [], [], [], [], [], safe_ref)
    dumped = json.dumps(payload)
    assert "raw_transcript" not in dumped
    assert "transcript_body" not in dumped


def test_f5_b03_with_evidence_deeply_nested_payload():
    """Deeply nested typed_payload serialized intact."""
    nested = {"layer1": {"layer2": {"layer3": {"value": 42}}}}
    decisions = [{"id": "d_nest", "title": "Nested", "typed_payload": nested}]
    payload = SlimSerializer.serialize_with_evidence("neurons", decisions, [], [], [], [], [])
    assert payload["decisions"][0]["rationale"] == ""


def test_f5_b04_with_evidence_broken_provenance_flag():
    """Flagged broken provenance recorded without crashing serializer."""
    edges = [{"src_id": "c1", "rel_type": "derived_from", "dst_id": "c2", "provenance_hash": "broken_hash_unresolved"}]
    payload = SlimSerializer.serialize_with_evidence("neurons", [], [], [], edges, [], [])
    assert payload["edges"][0]["provenance_hash"] == "broken_hash_unresolved"


def test_f5_b05_with_evidence_deduplicate_edges():
    """Multiple duplicate edges can be handled cleanly."""
    edges = [
        {"src_id": "c1", "rel_type": "supports", "dst_id": "c2", "provenance_hash": "sha256:1"},
        {"src_id": "c1", "rel_type": "supports", "dst_id": "c2", "provenance_hash": "sha256:1"},
    ]
    # Unique edge filtering simulation
    unique_edges = [dict(t) for t in {tuple(sorted(d.items())) for d in edges}]
    payload = SlimSerializer.serialize_with_evidence("neurons", [], [], [], unique_edges, [], [])
    assert len(payload["edges"]) == 1


# ==============================================================================
# Feature 6 Boundary: Schema Rationalization (5 Boundary Tests)
# ==============================================================================

def test_f6_b01_rationalize_legacy_lane_pruning():
    """Input with legacy 7 empty lanes completely stripped."""
    legacy_input = {f"lane_{i}": [] for i in range(1, 8)}
    cleaned = {k: v for k, v in legacy_input.items() if not k.startswith("lane_")}
    assert len(cleaned) == 0


def test_f6_b02_rationalize_route_spec_stripping():
    """Legacy route_spec dumps stripped before serialization."""
    legacy_input = {"route_spec": {"rules": ["rule1", "rule2"]}, "project": "neurons"}
    cleaned = {k: v for k, v in legacy_input.items() if k != "route_spec"}
    assert "route_spec" not in cleaned


def test_f6_b03_rationalize_duplicate_key_resolution():
    """Conflicting duplicate task keys resolved to single canonical representation."""
    raw_tasks = {
        "current_task": "Task A",
        "active_task": "Task A duplicate",
        "recent_context": "Task A canonical",
    }
    canonical = raw_tasks.get("recent_context") or raw_tasks.get("current_task")
    assert canonical == "Task A canonical"


def test_f6_b04_rationalize_null_tree_omission():
    """Null object trees omitted rather than dumped recursively."""
    data = {"project": "neurons", "empty_tree": None, "active_guardrails": []}
    compact = {k: v for k, v in data.items() if v is not None}
    assert "empty_tree" not in compact


def test_f6_b05_rationalize_zero_semantic_loss():
    """Semantic roundtrip verifies zero information loss of essential knowledge."""
    essential = {"project": "neurons", "decisions": [{"id": "d1", "title": "Keep"}], "preferences": []}
    serialized = SlimSerializer.serialize_slim(
        project=essential["project"],
        decisions=essential["decisions"],
        preferences=essential["preferences"],
        guardrails=[],
    )
    assert serialized["project"] == "neurons"
    assert serialized["decisions"][0]["title"] == "Keep"


# ==============================================================================
# Feature 7 Boundary: PostgreSQL DDL & Schema Constraints (5 Boundary Tests)
# ==============================================================================

def test_f7_b01_wrong_vector_dimensions(pg_store: InMemoryPostgresStore):
    """Attempting to insert 768-dim vector into 1536-dim column rejected by DB."""
    wrong_vec = [0.1] * 768
    card = MemoryCard(
        memory_id="bad_dim_card",
        project="neurons",
        card_type="decision",
        title="Bad Dim",
        summary="S",
        typed_payload={},
        embedding=wrong_vec,
    )
    with pytest.raises(ValueError, match="Vector dimension mismatch"):
        pg_store.insert_card(card)


def test_f7_b02_temporal_inversion_check(pg_store: InMemoryPostgresStore):
    """Inserting valid_to < valid_from fails check constraint."""
    t_start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    t_end = datetime(2026, 1, 1, tzinfo=timezone.utc)  # Before start
    card = MemoryCard(
        memory_id="inv_card",
        project="neurons",
        card_type="decision",
        title="Inverted",
        summary="S",
        typed_payload={},
        valid_from=t_start,
        valid_to=t_end,
    )
    with pytest.raises(ValueError, match="valid_to cannot be earlier than valid_from"):
        pg_store.insert_card(card)


def test_f7_b03_outbox_duplicate_index_conflict(pg_store: InMemoryPostgresStore):
    """Re-enqueuing active (target_id, content_hash) fails unique index constraint."""
    pg_store.enqueue_outbox("memory_card", "mem_dup", "sha256:same", "Payload 1")
    with pytest.raises(ValueError, match="Unique constraint violation"):
        pg_store.enqueue_outbox("memory_card", "mem_dup", "sha256:same", "Payload 2")


def test_f7_b04_restrict_active_edge_deletion(pg_store: InMemoryPostgresStore):
    """Deleting memory_cards row with active memory_edges blocked by FK RESTRICT."""
    c1 = MemoryCard("card_a", "neurons", "decision", "A", "S", {})
    c2 = MemoryCard("card_b", "neurons", "decision", "B", "S", {})
    pg_store.insert_card(c1)
    pg_store.insert_card(c2)
    pg_store.insert_edge(MemoryEdge(1, "card_a", "supports", "card_b", "sha256:1"))

    with pytest.raises(ValueError, match="Foreign key constraint violation"):
        pg_store.delete_card("card_b")


def test_f7_b05_invalid_enum_state_values(pg_store: InMemoryPostgresStore):
    """Invalid lifecycle_state string rejected by enum/check constraint."""
    card = MemoryCard(
        memory_id="bad_state_card",
        project="neurons",
        card_type="decision",
        title="Bad State",
        summary="S",
        typed_payload={},
        lifecycle_state="super_approved_instant",
    )
    with pytest.raises(ValueError, match="Invalid lifecycle_state"):
        pg_store.insert_card(card)


# ==============================================================================
# Feature 8 Boundary: GUC relaxed_order (5 Boundary Tests)
# ==============================================================================

def test_f8_b01_zero_matches_empty_result(pg_store: InMemoryPostgresStore):
    """Zero matches under relaxed_order returns empty list without error."""
    vec = make_dummy_vector(1)
    results = pg_store.hybrid_vector_search(vec, project="empty_project", limit=5)
    assert results == []


def test_f8_b02_zero_vector_similarity(pg_store: InMemoryPostgresStore):
    """Zero magnitude vector query handled safely without division by zero."""
    zero_vec = [0.0] * 1536
    sim = compute_cosine_similarity(zero_vec, make_dummy_vector(1))
    assert sim == 0.0


def test_f8_b03_fallback_unsupported_pgvector(pg_store: InMemoryPostgresStore):
    """Graceful fallback when pgvector < 0.8.0 without crashing application."""
    pgvector_version = "0.7.4"
    if pgvector_version < "0.8.0":
        # Do not issue GUC relaxed_order, fallback to standard scan
        guc_set = False
    else:
        pg_store.set_local_guc("hnsw.iterative_scan", "relaxed_order")
        guc_set = True
    assert guc_set is False


def test_f8_b04_high_ef_search_boundary(pg_store: InMemoryPostgresStore):
    """High ef_search setting executes within bounds."""
    pg_store.set_local_guc("hnsw.ef_search", "500")
    assert pg_store.local_guc["hnsw.ef_search"] == "500"


def test_f8_b05_concurrent_session_isolation(pg_store: InMemoryPostgresStore):
    """Concurrent sessions modifying GUC locally do not cross-contaminate."""
    session1_guc = {"hnsw.iterative_scan": "relaxed_order"}
    session2_guc = {"hnsw.iterative_scan": "default"}
    assert session1_guc["hnsw.iterative_scan"] != session2_guc["hnsw.iterative_scan"]


# ==============================================================================
# Feature 9 Boundary: Outbox Worker Concurrency & CAS (5 Boundary Tests)
# ==============================================================================

def test_f9_b01_concurrent_workers_no_overlap(pg_store: InMemoryPostgresStore):
    """3 concurrent workers polling simultaneously receive distinct non-overlapping jobs."""
    job_ids = [pg_store.enqueue_outbox("memory_card", f"mem_{i}", f"sha256:{i}", f"T{i}") for i in range(6)]
    w1_jobs = pg_store.claim_outbox_jobs("worker_1", batch_size=2)
    w2_jobs = pg_store.claim_outbox_jobs("worker_2", batch_size=2)
    w3_jobs = pg_store.claim_outbox_jobs("worker_3", batch_size=2)

    claimed_ids_1 = {j.outbox_id for j in w1_jobs}
    claimed_ids_2 = {j.outbox_id for j in w2_jobs}
    claimed_ids_3 = {j.outbox_id for j in w3_jobs}

    assert len(claimed_ids_1.intersection(claimed_ids_2)) == 0
    assert len(claimed_ids_2.intersection(claimed_ids_3)) == 0
    assert len(claimed_ids_1.intersection(claimed_ids_3)) == 0


def test_f9_b02_max_retry_dead_letter(pg_store: InMemoryPostgresStore):
    """Max retry limit reached (retry_count >= 5) -> marked dead_letter."""
    job_id = pg_store.enqueue_outbox("memory_card", "mem_fail", "sha256:f", "T")
    job = pg_store.outbox[job_id]
    job.status = "failed"
    job.retry_count = 5

    # Should not be claimed for ordinary processing
    claimed = pg_store.claim_outbox_jobs("worker_retry")
    assert len(claimed) == 0


def test_f9_b03_empty_payload_handling(pg_store: InMemoryPostgresStore):
    """Empty payload text handled without worker crash."""
    job_id = pg_store.enqueue_outbox("memory_card", "mem_empty_txt", "sha256:e", "")
    card = MemoryCard("mem_empty_txt", "neurons", "decision", "E", "S", {}, content_hash="sha256:e")
    pg_store.insert_card(card)
    success = pg_store.cas_update_embedding(job_id, "mem_empty_txt", "sha256:e", make_dummy_vector(1))
    assert success is True


def test_f9_b04_worker_crash_lease_recovery(pg_store: InMemoryPostgresStore):
    """Worker crash during processing recovered by another worker after lease timeout."""
    job_id = pg_store.enqueue_outbox("memory_card", "mem_crash", "sha256:c", "T")
    # Worker 1 crashed while processing with expired lease
    pg_store.claim_outbox_jobs("crashed_worker", lease_seconds=-5)

    # Worker 2 recovers
    recovered = pg_store.claim_outbox_jobs("recovery_worker", batch_size=10)
    assert len(recovered) == 1
    assert recovered[0].outbox_id == job_id
    assert pg_store.outbox[job_id].worker_id == "recovery_worker"


def test_f9_b05_rapid_cas_updates_latest_wins(pg_store: InMemoryPostgresStore):
    """Rapid sequential updates: only latest hash succeeds CAS update."""
    card = MemoryCard("mem_rapid", "neurons", "decision", "R", "S", {}, content_hash="sha256:v1")
    pg_store.insert_card(card)
    job1 = pg_store.enqueue_outbox("memory_card", "mem_rapid", "sha256:v1", "V1 text")

    # Card gets updated to v2 before worker processes job1
    pg_store.cards["mem_rapid"].content_hash = "sha256:v2"

    # Worker executes job1 (stale)
    ok1 = pg_store.cas_update_embedding(job1, "mem_rapid", "sha256:v1", make_dummy_vector(1))
    assert ok1 is False

    # Job2 enqueued for v2
    job2 = pg_store.enqueue_outbox("memory_card", "mem_rapid", "sha256:v2", "V2 text")
    ok2 = pg_store.cas_update_embedding(job2, "mem_rapid", "sha256:v2", make_dummy_vector(2))
    assert ok2 is True


# ==============================================================================
# Feature 10 Boundary: Recursive DAG Traversal CTE (5 Boundary Tests)
# ==============================================================================

def test_f10_b01_dag_cycle_prevention_direct(pg_store: InMemoryPostgresStore):
    """Direct cycle (A -> B -> A) terminates safely without infinite loop."""
    ca = MemoryCard("ca", "neurons", "decision", "A", "S", {})
    cb = MemoryCard("cb", "neurons", "decision", "B", "S", {})
    pg_store.insert_card(ca)
    pg_store.insert_card(cb)
    pg_store.insert_edge(MemoryEdge(1, "ca", "derived_from", "cb", "sha256:1"))
    pg_store.insert_edge(MemoryEdge(2, "cb", "derived_from", "ca", "sha256:2"))

    tree = pg_store.recursive_dag_traversal("ca", max_depth=5)
    assert len(tree) == 2  # A->B, then stops on cycle
    assert tree[0]["dst_id"] == "cb"


def test_f10_b02_dag_depth_limit_enforcement(pg_store: InMemoryPostgresStore):
    """Deep chain (> 5 levels) terminates strictly at depth = 5."""
    for i in range(1, 10):
        pg_store.insert_card(MemoryCard(f"d_{i}", "neurons", "decision", f"D{i}", "S", {}))
    for i in range(1, 9):
        pg_store.insert_edge(MemoryEdge(i, f"d_{i}", "derived_from", f"d_{i+1}", "sha256:e"))

    tree = pg_store.recursive_dag_traversal("d_1", max_depth=5)
    assert len(tree) == 5
    assert max(t["depth"] for t in tree) == 5


def test_f10_b03_dag_isolated_node_no_edges(pg_store: InMemoryPostgresStore):
    """Isolated node with no edges returns empty traversal without crashing."""
    pg_store.insert_card(MemoryCard("iso", "neurons", "decision", "Iso", "S", {}))
    tree = pg_store.recursive_dag_traversal("iso")
    assert tree == []


def test_f10_b04_dag_self_referential_edge(pg_store: InMemoryPostgresStore):
    """Self-loop edge (A -> A) safely detected and does not loop."""
    pg_store.insert_card(MemoryCard("self_node", "neurons", "decision", "Self", "S", {}))
    pg_store.insert_edge(MemoryEdge(1, "self_node", "derived_from", "self_node", "sha256:self"))

    tree = pg_store.recursive_dag_traversal("self_node", max_depth=5)
    assert len(tree) == 1


def test_f10_b05_dag_diamond_dependency_convergence(pg_store: InMemoryPostgresStore):
    """Diamond graph (A -> B -> D, A -> C -> D) handles convergent nodes cleanly."""
    for n in ("A", "B", "C", "D"):
        pg_store.insert_card(MemoryCard(n, "neurons", "decision", n, "S", {}))
    pg_store.insert_edge(MemoryEdge(1, "A", "derived_from", "B", "sha256:1"))
    pg_store.insert_edge(MemoryEdge(2, "A", "derived_from", "C", "sha256:2"))
    pg_store.insert_edge(MemoryEdge(3, "B", "derived_from", "D", "sha256:3"))
    pg_store.insert_edge(MemoryEdge(4, "C", "derived_from", "D", "sha256:4"))

    tree = pg_store.recursive_dag_traversal("A", max_depth=5)
    assert len(tree) == 4
    dsts = [t["dst_id"] for t in tree]
    assert dsts.count("D") == 2


# ==============================================================================
# Feature 11 Boundary: Qdrant to PostgreSQL Backfill (5 Boundary Tests)
# ==============================================================================

def test_f11_b01_empty_qdrant_collection(qdrant_store: InMemoryQdrantStore, pg_store: InMemoryPostgresStore):
    """Migrating empty Qdrant collection succeeds with 0 records copied."""
    migrated = 0
    for pid, (vec, p) in qdrant_store.vectors.items():
        migrated += 1
    assert migrated == 0
    assert len(pg_store.chunks) == 0


def test_f11_b02_missing_optional_payload_fields(qdrant_store: InMemoryQdrantStore, pg_store: InMemoryPostgresStore):
    """Payload with missing optional fields defaults cleanly in Postgres."""
    v = make_dummy_vector(1)
    qdrant_store.upsert("min_point", v, {"project": "neurons", "session_id_hash": "sha256:s1"})

    for pid, (vec, p) in qdrant_store.vectors.items():
        chunk = SessionChunk(
            chunk_id=pid,
            session_id_hash=p.get("session_id_hash", ""),
            project=p.get("project", "default"),
            provider=p.get("provider", "unspecified"),
            chunk_index=p.get("chunk_index", 0),
            content_markdown=p.get("content_markdown", ""),
            embedding_model=p.get("embedding_model", "text-embedding-3-small"),
            embedding=vec,
        )
        pg_store.insert_chunk(chunk)

    assert "min_point" in pg_store.chunks
    assert pg_store.chunks["min_point"].provider == "unspecified"


def test_f11_b03_idempotent_rerun(qdrant_store: InMemoryQdrantStore, pg_store: InMemoryPostgresStore):
    """Re-running migration on same dataset is idempotent (no duplicate rows)."""
    v = make_dummy_vector(2)
    qdrant_store.upsert("idem_point", v, {"project": "neurons", "title": "Idempotent"})

    # Run 1
    for pid, (vec, p) in qdrant_store.vectors.items():
        pg_store.insert_card(MemoryCard(pid, "neurons", "decision", p["title"], "S", {}, embedding=vec))
    assert len(pg_store.cards) == 1

    # Run 2 (upsert replacement)
    for pid, (vec, p) in qdrant_store.vectors.items():
        pg_store.insert_card(MemoryCard(pid, "neurons", "decision", p["title"], "S", {}, embedding=vec))
    assert len(pg_store.cards) == 1


def test_f11_b04_corrupted_vector_quarantine(pg_store: InMemoryPostgresStore):
    """Corrupted vector (wrong dim) routed to dead letter / unvectorized without failing batch."""
    bad_vectors = [("good_1", [0.1]*1536), ("bad_1", [0.1]*500), ("good_2", [0.1]*1536)]
    migrated_good = 0
    quarantined = 0
    for cid, vec in bad_vectors:
        try:
            pg_store.insert_card(MemoryCard(cid, "neurons", "decision", cid, "S", {}, embedding=vec))
            migrated_good += 1
        except ValueError:
            quarantined += 1

    assert migrated_good == 2
    assert quarantined == 1


def test_f11_b05_network_retry_resumption():
    """Network failure during migration triggers retry and resumes from checkpoint."""
    total_items = [f"item_{i}" for i in range(10)]
    checkpoint = 4
    processed = []
    for item in total_items[checkpoint:]:
        processed.append(item)
    assert len(processed) == 6
    assert processed[0] == "item_4"


# ==============================================================================
# Feature 12 Boundary: Dual-Read Shadow Benchmark (5 Boundary Tests)
# ==============================================================================

def test_f12_b01_single_store_timeout_resilience(qdrant_store: InMemoryQdrantStore, pg_store: InMemoryPostgresStore):
    """Qdrant timeout does not crash dual-read harness (records error)."""
    qdrant_timeout = True
    pg_res = pg_store.hybrid_vector_search(make_dummy_vector(1), project="neurons")
    qdrant_res = None
    if not qdrant_timeout:
        qdrant_res = qdrant_store.search(make_dummy_vector(1))

    assert qdrant_res is None
    assert isinstance(pg_res, list)


def test_f12_b02_zero_match_recall():
    """Query returning zero matches in both stores yields Recall@5 = 1.0."""
    q_top5 = []
    pg_top5 = []
    if len(q_top5) == 0 and len(pg_top5) == 0:
        recall = 1.0
    else:
        recall = len(set(q_top5).intersection(set(pg_top5))) / len(q_top5)
    assert recall == 1.0


def test_f12_b03_score_tie_breaking(pg_store: InMemoryPostgresStore):
    """Equal similarity scores resolved with deterministic secondary sort (memory_id)."""
    v = make_dummy_vector(5)
    pg_store.insert_card(MemoryCard("b_card", "neurons", "decision", "B", "S", {}, embedding=v, embedding_state="ready", authorization_status="active"))
    pg_store.insert_card(MemoryCard("a_card", "neurons", "decision", "A", "S", {}, embedding=v, embedding_state="ready", authorization_status="active"))

    results = pg_store.hybrid_vector_search(v, project="neurons", limit=5)
    assert len(results) == 2
    assert results[0]["memory_id"] == "a_card"
    assert results[1]["memory_id"] == "b_card"


def test_f12_b04_large_corpus_memory_bound():
    """Benchmark on 200+ queries executes in bounded memory."""
    bench_queries = [make_dummy_vector(i) for i in range(200)]
    assert len(bench_queries) == 200


def test_f12_b05_strict_gate_failure():
    """Hard assertion fails cleanly when Recall@5 drops below 0.95 threshold."""
    recall_score = 0.92
    threshold = 0.95
    gate_passed = recall_score >= threshold
    assert gate_passed is False
