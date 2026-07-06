from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from agent_knowledge.cli import main
from agent_knowledge.llm_brain_core.reference_corpus import reference_corpus_objects_from_manifest
from agent_knowledge.session_memory.curation import CurationService
from agent_knowledge.ledger import Ledger
from agent_knowledge.mcp_server import (
    BRAIN_QUERY_TOOL_NAME,
    BRAIN_RESOLVE_TOOL_NAME,
    BRAIN_CONTEXT_RESOLVE_TOOL_NAME,
    BRAIN_DRIFT_EXPLAIN_TOOL_NAME,
    BRAIN_EVIDENCE_GET_TOOL_NAME,
    BRAIN_INCIDENT_SEARCH_TOOL_NAME,
    BRAIN_MEMORY_SEARCH_TOOL_NAME,
    BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME,
    BRAIN_OBJECT_EXPLAIN_TOOL_NAME,
    BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME,
    BRAIN_OBJECTS_QUERY_TOOL_NAME,
    BRAIN_SOURCE_TO_CANDIDATE_GRAPH_TOOL_NAME,
    BRAIN_CANDIDATE_REVIEW_EDIT_TOOL_NAME,
    BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME,
    BRAIN_SOURCE_TO_CANDIDATE_RUNTIME_READINESS_TOOL_NAME,
    BRAIN_CORPUS_INGEST_PLAN_TOOL_NAME,
    BRAIN_CORPUS_STATUS_TOOL_NAME,
    BRAIN_REVIEW_PROPOSALS_TOOL_NAME,
    BRAIN_PERSONA_CHECK_TOOL_NAME,
    BRAIN_PERSONA_GET_TOOL_NAME,
    TOOL_NAME,
    DisabledRetiredIndexBridgeClient,
    KnowledgeSearchService,
    MemoryReadPipeline,
    MemoryProvenance,
    MemorySearchQuery,
    MemorySearchResponse,
    MemorySearchResultItem,
    _call_tool,
    dispatch_tool_call,
    handle_jsonrpc_message,
    list_tools,
)
from agent_knowledge import mcp_tools
from agent_knowledge.mcp_tools import tool_contract_registry, tool_registry, tool_names
from agent_knowledge.session_memory.memory_card import build_memory_candidate
from agent_knowledge.session_memory.memory_miner import build_memory_card_candidate_from_source_span
from agent_knowledge.llm_brain_core.context import BrainReadService
from agent_knowledge.llm_brain_core.context_builder import object_native_review_tool_hints
from agent_knowledge.llm_brain_core.ledger_adapter import LedgerSourceRefCatalog
from agent_knowledge.llm_brain_core.graph import FakeGraphMemoryAdapter
from agent_knowledge.llm_brain_core.knowledge_objects import EvidenceRef, KnowledgeEdge
from agent_knowledge.llm_brain_core.models import CONTEXT_PACK_SCHEMA_VERSION, OntologyEpisode
from agent_knowledge.llm_brain_core.runtime import source_ref_from_catalog_event
from agent_knowledge.session_memory.llm_brain_service import LLMBrainMemoryService

PROJECT = "workspace-index-advisor"
FIXTURE_REPOSITORY = PROJECT
FIXTURE_BRANCH = "fixture-branch"


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


def _accepted_task_card(memory_id: str, *, next_action: str, project: str = PROJECT) -> dict:
    summary = f"Resume fixture {memory_id}"
    return {
        "memory_id": memory_id,
        "brain_id": f"/project/{project}",
        "card_type": "task",
        "scope": "project",
        "project": project,
        "provider": "codex",
        "title": summary,
        "summary": summary,
        "render_text": summary,
        "lifecycle_state": "accepted",
        "judgment_state": "none",
        "status": "accepted",
        "approval_state": "approved",
        "governance_tier": "medium",
        "freshness": "current",
        "currentness": "current",
        "confidence": 0.9,
        "confidence_basis": "temporal work recall fixture",
        "source_refs": [{"source_ref_id": "src_neuron_mcp", "content_hash": _h("task-source")}],
        "evidence_refs": [],
        "evidence_hashes": [_h(memory_id)],
        "derived_from": [],
        "supersedes": [],
        "superseded_by": [],
        "conflicts": [],
        "active_until": "",
        "updated_at": "2026-07-06T00:00:00Z",
        "typed_payload": {
            "task_state": summary,
            "next_action": next_action,
            "blocker": "",
            "owner_hint": project,
            "status": "open",
        },
    }


def _accepted_preference_card(
    memory_id: str,
    *,
    preference: str,
    applies_to: str,
    project: str = PROJECT,
) -> dict:
    return {
        "memory_id": memory_id,
        "brain_id": f"/project/{project}",
        "card_type": "preference",
        "scope": "project",
        "project": project,
        "provider": "codex",
        "title": preference,
        "summary": preference,
        "render_text": preference,
        "lifecycle_state": "accepted",
        "judgment_state": "none",
        "status": "accepted",
        "approval_state": "approved",
        "governance_tier": "medium",
        "freshness": "current",
        "currentness": "current",
        "confidence": 0.93,
        "confidence_basis": "accepted preference fixture",
        "source_refs": [{"source_ref_id": "src_neuron_mcp", "content_hash": _h(memory_id)}],
        "evidence_refs": [],
        "evidence_hashes": [_h(memory_id)],
        "derived_from": [],
        "supersedes": [],
        "superseded_by": [],
        "conflicts": [],
        "active_until": "",
        "updated_at": "2026-07-06T00:00:00Z",
        "typed_payload": {
            "preference": preference,
            "applies_to": applies_to,
            "explicitness": "explicit",
            "repeated_count": 1,
            "confirmation_status": "confirmed",
        },
    }


def _reference_manifest() -> dict:
    return {
        "corpus_name": "palantir-ontology-mini",
        "sources": [
            {
                "source_id": "palantir-ontology-001",
                "title": "Ontology overview",
                "source_type": "WEB_PAGE",
                "source_url": "https://example.test/ontology",
                "normalized_path": "sources-normalized/palantir-ontology-001.md",
                "content_hash": "sha256:" + "1" * 64,
                "metadata_hash": "sha256:" + "2" * 64,
                "summary": "Objects, links, actions, functions.",
            },
            {
                "source_id": "palantir-ontology-002",
                "title": "Manual excerpt",
                "source_type": "TEXT",
                "normalized_path": "sources-normalized/palantir-ontology-002.md",
                "content_hash": "sha256:" + "3" * 64,
                "metadata_hash": "sha256:" + "4" * 64,
                "summary": "Manual source with missing URL.",
            },
        ],
    }


def _service(tmp_path: Path) -> KnowledgeSearchService:
    ledger = _ledger(tmp_path)
    LedgerSourceRefCatalog(ledger).register(
        source_ref_from_catalog_event(
            {
                "source_ref_id": "src_neuron_mcp",
                "device_id_hash": _h("device-a"),
                "root_id": "project-root",
                "relative_path_hash": _h("docs/design.md"),
                "content_hash": _h("mcp-source"),
                "mtime": "2026-06-19T00:00:00Z",
                "size": 100,
                "sync_policy": "derived_only",
                "derived_summary": "MCP SourceRef policy evidence is available.",
            }
        )
    )
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
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        allow_private_results=True,
        allow_local_test_object_authority_writes=True,
    )


def test_mcp_tool_list_exposes_neuron_owned_tools():
    tools = list_tools()
    names = [tool["name"] for tool in tools]

    assert TOOL_NAME in names
    assert BRAIN_QUERY_TOOL_NAME in names
    assert BRAIN_RESOLVE_TOOL_NAME in names
    assert BRAIN_CONTEXT_RESOLVE_TOOL_NAME in names
    assert BRAIN_PERSONA_CHECK_TOOL_NAME in names
    assert BRAIN_EVIDENCE_GET_TOOL_NAME in names
    legacy_search = next(tool for tool in tools if tool["name"] == TOOL_NAME)
    assert "legacy external index bridge is retired" in legacy_search["description"]
    assert "brain_context_resolve" in legacy_search["description"]


def test_mcp_tool_registry_matches_listed_tools_without_duplicate_names():
    tools = list_tools()
    names = [tool["name"] for tool in tools]
    registry = tool_registry()

    assert len(names) == len(set(names))
    assert set(registry) == set(names)
    assert tool_names() == frozenset(names)
    for tool in tools:
        registered = registry[tool["name"]]
        assert registered["description"] == tool["description"]
        assert registered["inputSchema"] == tool["inputSchema"]


def test_mcp_tool_registry_uses_lazy_internal_cache(monkeypatch):
    calls = 0
    original_list_tools = mcp_tools.list_tools
    monkeypatch.setattr(mcp_tools, "_TOOL_REGISTRY_CACHE", None)

    def _counting_list_tools():
        nonlocal calls
        calls += 1
        return original_list_tools()

    monkeypatch.setattr(mcp_tools, "list_tools", _counting_list_tools)

    first = mcp_tools.tool_registry()
    second = mcp_tools.tool_registry()

    assert calls == 1
    assert second == first


def test_mcp_tool_contract_registry_tracks_dispatch_ownership():
    registry = tool_registry()
    contracts = tool_contract_registry()

    assert set(contracts) == set(registry)
    for name, contract in contracts.items():
        assert contract.name == name
        assert contract.dispatch_owner
        assert contract.to_tool() == registry[name]


def test_mcp_public_tool_list_does_not_expose_dispatch_metadata():
    for tool in list_tools():
        assert "dispatch_owner" not in tool
        assert "handler" not in tool


def test_mcp_tool_contract_registry_fails_when_dispatch_owner_metadata_missing(monkeypatch):
    dispatch_owners = dict(mcp_tools._DISPATCH_OWNER_BY_TOOL_NAME)
    dispatch_owners.pop(TOOL_NAME)
    monkeypatch.setattr(mcp_tools, "_DISPATCH_OWNER_BY_TOOL_NAME", dispatch_owners)

    with pytest.raises(ValueError, match="missing dispatch owner"):
        mcp_tools.tool_contract_registry()


def test_mcp_tool_contract_registry_fails_when_dispatch_owner_metadata_stale(monkeypatch):
    dispatch_owners = dict(mcp_tools._DISPATCH_OWNER_BY_TOOL_NAME)
    dispatch_owners["stale.tool"] = "jsonrpc_brain"
    monkeypatch.setattr(mcp_tools, "_DISPATCH_OWNER_BY_TOOL_NAME", dispatch_owners)

    with pytest.raises(ValueError, match="stale"):
        mcp_tools.tool_contract_registry()


def test_brain_memory_search_schema_matches_repository_project_derivation():
    tools = {tool["name"]: tool for tool in list_tools()}
    schema = tools[BRAIN_MEMORY_SEARCH_TOOL_NAME]["inputSchema"]

    assert "repository" in schema["properties"]
    assert "project" in schema["properties"]
    assert schema["required"] == ["query"]
    assert schema["anyOf"] == [{"required": ["project"]}, {"required": ["repository"]}]


def test_brain_context_resolve_schema_exposes_response_mode():
    tools = {tool["name"]: tool for tool in list_tools()}
    schema = tools[BRAIN_CONTEXT_RESOLVE_TOOL_NAME]["inputSchema"]

    assert schema["properties"]["response_mode"] == {
        "type": "string",
        "enum": ["full", "compact", "degraded"],
        "default": "full",
    }
    assert schema["properties"]["consumer"]["enum"] == ["unspecified", "codex", "claude-code", "gemini", "hermes"]
    assert schema["properties"]["consumer"]["default"] == "unspecified"


def test_mcp_tool_list_exposes_object_substrate_tools():
    tools = {tool["name"]: tool for tool in list_tools()}

    for tool_name in [
        BRAIN_OBJECTS_QUERY_TOOL_NAME,
        BRAIN_OBJECT_EXPLAIN_TOOL_NAME,
        BRAIN_CORPUS_STATUS_TOOL_NAME,
        BRAIN_CORPUS_INGEST_PLAN_TOOL_NAME,
        BRAIN_SOURCE_TO_CANDIDATE_GRAPH_TOOL_NAME,
        BRAIN_CANDIDATE_REVIEW_EDIT_TOOL_NAME,
        BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME,
        BRAIN_SOURCE_TO_CANDIDATE_RUNTIME_READINESS_TOOL_NAME,
        BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME,
        BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME,
        BRAIN_REVIEW_PROPOSALS_TOOL_NAME,
    ]:
        assert tool_name in tools

    assert tools[BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME]["inputSchema"]["properties"]["ledger_scope"]["enum"] == [
        "local_test",
        "production",
    ]
    assert "project" in tools[BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME]["inputSchema"]["properties"]
    assert "production_gate" in tools[BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME]["inputSchema"]["properties"]
    assert "production_gate" in tools[BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME]["inputSchema"]["properties"]
    assert tools[BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME]["inputSchema"]["required"] == [
        "proposal_id",
        "decision_type",
        "target_object_id",
        "previous_authority_lane",
        "new_authority_lane",
        "approved_by",
        "decision_id",
    ]
    corpus_plan_properties = tools[BRAIN_CORPUS_INGEST_PLAN_TOOL_NAME]["inputSchema"]["properties"]
    assert "expected_source_count" in corpus_plan_properties
    assert "expected_source_url_count" in corpus_plan_properties
    assert "expected_manual_text_without_url_count" in corpus_plan_properties
    assert "expected_source_type_counts" in corpus_plan_properties
    assert tools[BRAIN_SOURCE_TO_CANDIDATE_GRAPH_TOOL_NAME]["inputSchema"]["properties"]["target"]["enum"] == [
        "local_test",
        "production",
    ]
    assert tools[BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME]["inputSchema"]["properties"]["target"]["enum"] == [
        "local_test",
        "production",
    ]
    readiness_schema = tools[BRAIN_SOURCE_TO_CANDIDATE_RUNTIME_READINESS_TOOL_NAME]["inputSchema"]
    assert readiness_schema["properties"]["live_evidence"]["type"] == "object"
    assert readiness_schema["properties"]["expected_commit"]["type"] == "string"
    assert readiness_schema["properties"]["evidence_collection_plan"]["type"] == "boolean"
    assert readiness_schema["properties"]["evidence_packet_template"]["type"] == "boolean"
    assert readiness_schema["properties"]["collect_shadow_evidence"]["type"] == "boolean"
    assert readiness_schema["properties"]["normalize_shadow_evidence"]["type"] == "object"
    assert readiness_schema["properties"]["shadow_evidence"]["type"] == "object"
    assert readiness_schema["properties"]["repository"]["type"] == "string"
    assert readiness_schema["properties"]["branch"]["type"] == "string"
    assert readiness_schema["properties"]["consumer"]["type"] == "string"


def test_mcp_source_to_candidate_runtime_readiness_evaluates_sanitized_evidence_without_mutation(tmp_path: Path):
    service = _service(tmp_path)
    tools = {tool["name"]: tool for tool in list_tools()}
    evidence = {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        "tool_names": [
            BRAIN_OBJECTS_QUERY_TOOL_NAME,
            BRAIN_SOURCE_TO_CANDIDATE_GRAPH_TOOL_NAME,
            BRAIN_CANDIDATE_REVIEW_EDIT_TOOL_NAME,
            BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME,
            BRAIN_SOURCE_TO_CANDIDATE_RUNTIME_READINESS_TOOL_NAME,
        ],
        "agent_context_product": {
            "schema_version": "agent_context_product_pack.v1",
            "consumer": "codex",
            "sections": {
                "style_preference": {"object_count": 1},
                "active_work": {"object_count": 1},
                "required_verification": {"object_count": 1},
            },
            "degraded_mode": {"active": True, "gaps": ["runtime_evidence_unverified"]},
            "missing_evidence_before_promotion": ["runtime_evidence_unverified"],
            "surface_policy": {"mutation_allowed": False},
            "tool_hints": object_native_review_tool_hints([]),
        },
        "brain_objects_query_smokes": [
            _brain_objects_query_smoke("authority_archive_separation"),
            _brain_objects_query_smoke("code_style_preference"),
            _brain_objects_query_smoke("temporal_work_recall"),
            _brain_objects_query_smoke("deployment_runtime_truth", gaps=["runtime_evidence_unverified"]),
        ],
        "source_to_candidate_review_loop": _source_to_candidate_review_loop_evidence(),
        "session_project_rollup_runtime": _session_project_rollup_runtime_evidence(),
        "preference_artifact_memory": _preference_artifact_memory_evidence(),
        "permission_sensitive_audit": _permission_sensitive_audit_evidence(),
        "agent_context_startup_runtime": _agent_context_startup_runtime_evidence(),
        "production_denials": {
            BRAIN_SOURCE_TO_CANDIDATE_GRAPH_TOOL_NAME: {
                "status": "denied",
                "production_mutation_performed": False,
                "mutation_performed": False,
            },
            BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME: {
                "permission": "denied",
                "production_mutation_performed": False,
                "authority_write_performed": False,
            },
            BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME: {
                "status": "denied",
                "production_mutation_performed": False,
                "proposal_write_performed": False,
                "authority_write_performed": False,
            },
            BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME: {
                "permission": "denied",
                "production_mutation_performed": False,
                "decision_write_performed": False,
                "authority_write_performed": False,
            },
        },
        "tool_schemas": {
            BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME: tools[BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME],
            BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME: tools[BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME],
        },
        "production_authority_gate": {
            "runtime_flag": "--allow-object-authority-production-writes",
            "default_enabled": False,
            "per_call_gate_required": True,
            "production_mutation_performed": False,
        },
        "deployed_identity": {
            "contains_expected_commit": True,
            "identity_source": "redacted_live_runtime_evidence",
        },
        "evidence_provenance": _runtime_evidence_provenance(
            collection_mode="post_deploy_read_only_smoke",
            mutation_scope="none",
            network_used=True,
        ),
    }

    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 120,
            "method": "tools/call",
            "params": {
                "name": BRAIN_SOURCE_TO_CANDIDATE_RUNTIME_READINESS_TOOL_NAME,
                "arguments": {
                    "live_evidence": evidence,
                    "expected_commit": "d38bcfa",
                },
            },
        },
        service,
    )

    report = response["result"]["structuredContent"]
    assert report["schema_version"] == "source_to_candidate_runtime_readiness.v1"
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["production_mutation_performed"] is False
    assert report["network_used"] is False
    assert report["evidence_collection_network_used"] is True
    assert "bounded_production_authority_execution_unverified" in report["gaps"]
    assert "live_session_project_rollup_unverified" not in report["gaps"]


def test_mcp_source_to_candidate_runtime_readiness_returns_evidence_collection_plan(tmp_path: Path):
    service = _service(tmp_path)

    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 120,
            "method": "tools/call",
            "params": {
                "name": BRAIN_SOURCE_TO_CANDIDATE_RUNTIME_READINESS_TOOL_NAME,
                "arguments": {
                    "evidence_collection_plan": True,
                    "expected_commit": "d38bcfa",
                    "repository": "pureliture/neurons",
                    "branch": "main",
                    "consumer": "codex",
                },
            },
        },
        service,
    )

    plan = response["result"]["structuredContent"]
    assert plan["schema_version"] == "source_to_candidate_runtime_evidence_collection_plan.v1"
    assert plan["expected_commit"] == "d38bcfa"
    assert plan["repository"] == "pureliture/neurons"
    assert plan["branch"] == "main"
    assert plan["consumer"] == "codex"
    assert plan["network_used"] is False
    assert plan["production_mutation_performed"] is False
    assert plan["mutation_allowed"] is False
    assert "probe_session_project_rollup_runtime" in plan["required_steps"]
    assert plan["gap_mapping"]["probe_session_project_rollup_runtime"] == "live_session_project_rollup_unverified"
    registration = plan["shadow_collection_registration"]
    assert registration["schema_version"] == "source_to_candidate_runtime_shadow_collection_registration.v1"
    assert registration["status"] == "registration_ready"
    assert registration["run_status"] == "not_run"
    assert registration["request_ids"] == ["shadow_brain_objects_query_route_smoke"]
    assert registration["readiness_claim"] == "registration_only_not_runtime_evidence"


def test_mcp_source_to_candidate_runtime_readiness_returns_evidence_packet_template(tmp_path: Path):
    service = _service(tmp_path)

    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 121,
            "method": "tools/call",
            "params": {
                "name": BRAIN_SOURCE_TO_CANDIDATE_RUNTIME_READINESS_TOOL_NAME,
                "arguments": {
                    "evidence_packet_template": True,
                    "expected_commit": "d38bcfa",
                    "repository": "pureliture/neurons",
                    "branch": "main",
                    "consumer": "codex",
                },
            },
        },
        service,
    )

    template = response["result"]["structuredContent"]
    assert template["schema_version"] == "source_to_candidate_runtime_evidence_packet_template.v1"
    assert template["status"] == "template_ready"
    assert template["output_schema"] == "source_to_candidate_runtime_evidence.v1"
    assert template["expected_commit"] == "d38bcfa"
    assert template["repository"] == "pureliture/neurons"
    assert template["branch"] == "main"
    assert template["consumer"] == "codex"
    assert template["network_used"] is False
    assert template["mutation_allowed"] is False
    assert template["production_mutation_performed"] is False
    assert template["readiness_claim"] == "template_only_not_runtime_evidence"
    assert template["packet_field_templates"]["schema_version"] == "source_to_candidate_runtime_evidence.v1"
    assert "session_project_rollup_runtime" in template["required_packet_fields"]
    assert (
        template["packet_field_templates"]["session_project_rollup_runtime"]["schema_version"]
        == "session_project_rollup_runtime_evidence.v1"
    )
    assert "preference_artifact_memory" in template["required_packet_fields"]
    assert (
        template["packet_field_templates"]["preference_artifact_memory"]["schema_version"]
        == "preference_artifact_memory_runtime_evidence.v1"
    )
    assert "permission_sensitive_audit" in template["required_packet_fields"]
    assert (
        template["packet_field_templates"]["permission_sensitive_audit"]["schema_version"]
        == "permission_sensitive_runtime_audit_evidence.v1"
    )
    assert "agent_context_startup_runtime" in template["required_packet_fields"]
    assert (
        template["packet_field_templates"]["agent_context_startup_runtime"]["schema_version"]
        == "agent_context_startup_runtime_evidence.v1"
    )
    assert len(template["packet_field_templates"]["brain_objects_query_smokes"]) == 4


def test_mcp_source_to_candidate_runtime_readiness_normalizes_shadow_evidence(tmp_path: Path):
    service = _service(tmp_path)

    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 122,
            "method": "tools/call",
            "params": {
                "name": BRAIN_SOURCE_TO_CANDIDATE_RUNTIME_READINESS_TOOL_NAME,
                "arguments": {
                    "normalize_shadow_evidence": _shadow_runtime_evidence_capture(),
                },
            },
        },
        service,
    )

    packet = response["result"]["structuredContent"]
    assert packet["schema_version"] == "source_to_candidate_runtime_evidence.v1"
    assert packet["production_mutation_performed"] is False
    assert (
        packet["evidence_provenance"]["schema_version"]
        == "source_to_candidate_runtime_evidence_provenance.v1"
    )
    assert packet["evidence_provenance"]["collection_mode"] == "post_deploy_read_only_smoke"
    assert packet["evidence_provenance"]["network_used"] is True
    assert len(packet["brain_objects_query_smokes"]) == 4


def test_mcp_source_to_candidate_runtime_readiness_evaluates_shadow_evidence(tmp_path: Path):
    service = _service(tmp_path)

    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 123,
            "method": "tools/call",
            "params": {
                "name": BRAIN_SOURCE_TO_CANDIDATE_RUNTIME_READINESS_TOOL_NAME,
                "arguments": {
                    "shadow_evidence": _shadow_runtime_evidence_capture(),
                    "expected_commit": "c264b46",
                },
            },
        },
        service,
    )

    report = response["result"]["structuredContent"]
    assert report["schema_version"] == "source_to_candidate_runtime_readiness.v1"
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["failed_claims"] == []
    assert report["live_evidence_provided"] is True
    assert report["production_mutation_performed"] is False
    assert report["network_used"] is False
    assert report["evidence_collection_network_used"] is True
    assert "shadow_route_smoke_not_implemented:deployment_runtime_truth" in report["gaps"]


def test_mcp_source_to_candidate_runtime_readiness_collects_shadow_evidence(tmp_path: Path):
    service = _service(tmp_path)

    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 124,
            "method": "tools/call",
            "params": {
                "name": BRAIN_SOURCE_TO_CANDIDATE_RUNTIME_READINESS_TOOL_NAME,
                "arguments": {
                    "collect_shadow_evidence": True,
                    "repository": "pureliture/neurons",
                    "branch": "codex/knowledge-object-review-flow-roadmap",
                    "consumer": "codex",
                },
            },
        },
        service,
    )

    packet = response["result"]["structuredContent"]
    assert packet["schema_version"] == "source_to_candidate_runtime_evidence.v1"
    assert packet["production_mutation_performed"] is False
    assert packet["collector"]["readiness_claim"] == "collector_packet_not_live_evidence"
    assert packet["evidence_provenance"]["collection_mode"] == "local_test_replay"
    assert packet["evidence_provenance"]["network_used"] is False
    assert packet["source_to_candidate_review_loop"]["schema_version"] == "source_to_candidate_review_loop_evidence.v1"
    assert packet["source_to_candidate_review_loop"]["source_to_candidate_graph"]["target_scope"] == "local_test"
    assert packet["source_to_candidate_review_loop"]["candidate_review_edit"]["mutation_mode"] == "no_mutation"
    assert (
        packet["source_to_candidate_review_loop"]["approval_board_decision"]["authority_write_scope"]
        == "local_test"
    )
    assert packet["session_project_rollup_runtime"]["schema_version"] == "session_project_rollup_runtime_evidence.v1"
    assert packet["session_project_rollup_runtime"]["rollup_preview"]["scope"] == "all_devices"
    assert packet["session_project_rollup_runtime"]["rollup_preview"]["device_count"] >= 2
    assert packet["preference_artifact_memory"]["schema_version"] == "preference_artifact_memory_runtime_evidence.v1"
    assert packet["preference_artifact_memory"]["preference_object_pack"]["accepted_preference_count"] >= 1
    assert packet["preference_artifact_memory"]["preference_object_pack"]["proposal_preference_count"] >= 1
    assert packet["preference_artifact_memory"]["html_visualization_route_smoke"]["route"] == "html_visualization_preference"
    assert packet["preference_artifact_memory"]["artifact_review_check"]["raw_artifact_body_returned"] is False
    assert len(packet["brain_objects_query_smokes"]) == 4
    assert all(
        "object_pack_route_not_implemented" not in smoke.get("object_pack", {}).get("gaps", [])
        for smoke in packet["brain_objects_query_smokes"]
    )


def _brain_objects_query_smoke(route: str, *, gaps: list[str] | None = None) -> dict:
    return {
        "schema_version": "brain_objects_query.v1",
        "route": route,
        "production_mutation_performed": False,
        "object_pack": {
            "schema_version": "object_pack.v1",
            "route": route,
            "objects": [{"object_id": f"ko:test:{route}", "object_type": "RuntimeTruth"}],
            "lanes": {"candidate": [{"object_id": f"ko:test:{route}", "object_type": "RuntimeTruth"}]},
            "recommended_actions": [{"object_id": f"ko:test:{route}", "action": "review"}],
            "gaps": list(gaps or []),
        },
    }


def _runtime_evidence_provenance(
    *,
    collection_mode: str,
    mutation_scope: str,
    network_used: bool,
) -> dict:
    return {
        "schema_version": "source_to_candidate_runtime_evidence_provenance.v1",
        "collection_mode": collection_mode,
        "collector": "redacted_operator_or_agent",
        "network_used": network_used,
        "mutation_scope": mutation_scope,
        "raw_private_evidence_returned": False,
        "secret_returned": False,
        "host_topology_returned": False,
        "raw_external_ids_returned": False,
    }


def _shadow_runtime_evidence_capture() -> dict:
    return {
        "tool_names": [BRAIN_CONTEXT_RESOLVE_TOOL_NAME, BRAIN_OBJECTS_QUERY_TOOL_NAME],
        "agent_context_product": {
            "schema_version": "agent_context_product_pack.v1",
            "consumer": "codex",
            "sections": {
                "style_preference": {"object_count": 0},
                "active_work": {"object_count": 0},
                "required_verification": {"object_count": 1},
            },
            "surface_policy": {"mutation_allowed": False},
            "degraded_mode": {"active": True, "gaps": ["runtime_evidence_unverified"]},
            "missing_evidence_before_promotion": ["runtime_evidence_unverified"],
            "tool_hints": [],
        },
        "brain_objects_query_smokes": [
            _brain_objects_query_smoke(
                "authority_archive_separation",
                gaps=["object_pack_route_not_implemented"],
            ),
            _brain_objects_query_smoke(
                "code_style_preference",
                gaps=["object_pack_route_not_implemented"],
            ),
            _brain_objects_query_smoke(
                "temporal_work_recall",
                gaps=["object_pack_route_not_implemented"],
            ),
            _brain_objects_query_smoke(
                "deployment_runtime_truth",
                gaps=["object_pack_route_not_implemented"],
            ),
        ],
        "deployed_identity": {
            "contains_expected_commit": False,
            "identity_source": "current_codex_session_configured_mcp_namespace",
        },
        "collection": {
            "collection_mode": "post_deploy_read_only_smoke",
            "network_used": True,
            "mutation_scope": "none",
        },
    }


def _runtime_readiness_complete_evidence(
    *,
    production_authority_execution: dict | None = None,
) -> dict:
    tools = {tool["name"]: tool for tool in list_tools()}
    evidence = {
        "schema_version": "source_to_candidate_runtime_evidence.v1",
        "tool_names": [
            BRAIN_OBJECTS_QUERY_TOOL_NAME,
            BRAIN_SOURCE_TO_CANDIDATE_GRAPH_TOOL_NAME,
            BRAIN_CANDIDATE_REVIEW_EDIT_TOOL_NAME,
            BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME,
            BRAIN_SOURCE_TO_CANDIDATE_RUNTIME_READINESS_TOOL_NAME,
        ],
        "agent_context_product": {
            "schema_version": "agent_context_product_pack.v1",
            "consumer": "codex",
            "sections": {
                "style_preference": {"object_count": 1},
                "active_work": {"object_count": 1},
                "required_verification": {"object_count": 1},
            },
            "degraded_mode": {"active": True, "gaps": ["runtime_evidence_unverified"]},
            "missing_evidence_before_promotion": ["runtime_evidence_unverified"],
            "surface_policy": {"mutation_allowed": False},
            "tool_hints": object_native_review_tool_hints([]),
        },
        "brain_objects_query_smokes": [
            _brain_objects_query_smoke("authority_archive_separation"),
            _brain_objects_query_smoke("code_style_preference"),
            _brain_objects_query_smoke("temporal_work_recall"),
            _brain_objects_query_smoke("deployment_runtime_truth", gaps=["runtime_evidence_unverified"]),
        ],
        "source_to_candidate_review_loop": _source_to_candidate_review_loop_evidence(),
        "session_project_rollup_runtime": _session_project_rollup_runtime_evidence(),
        "preference_artifact_memory": _preference_artifact_memory_evidence(),
        "permission_sensitive_audit": _permission_sensitive_audit_evidence(),
        "agent_context_startup_runtime": _agent_context_startup_runtime_evidence(),
        "production_denials": {
            BRAIN_SOURCE_TO_CANDIDATE_GRAPH_TOOL_NAME: {
                "status": "denied",
                "production_mutation_performed": False,
                "mutation_performed": False,
            },
            BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME: {
                "permission": "denied",
                "production_mutation_performed": False,
                "authority_write_performed": False,
            },
            BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME: {
                "status": "denied",
                "production_mutation_performed": False,
                "proposal_write_performed": False,
                "authority_write_performed": False,
            },
            BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME: {
                "permission": "denied",
                "production_mutation_performed": False,
                "decision_write_performed": False,
                "authority_write_performed": False,
            },
        },
        "tool_schemas": {
            BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME: tools[BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME],
            BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME: tools[BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME],
        },
        "production_authority_gate": {
            "runtime_flag": "--allow-object-authority-production-writes",
            "default_enabled": False,
            "per_call_gate_required": True,
            "production_mutation_performed": False,
        },
        "deployed_identity": {
            "contains_expected_commit": True,
            "identity_source": "redacted_live_runtime_evidence",
        },
        "evidence_provenance": _runtime_evidence_provenance(
            collection_mode="local_test_replay",
            mutation_scope="bounded_production_authority_execution"
            if production_authority_execution is not None
            else "none",
            network_used=False,
        ),
    }
    if production_authority_execution is not None:
        evidence["production_authority_execution"] = production_authority_execution
    return evidence


def _source_to_candidate_review_loop_evidence() -> dict:
    return {
        "schema_version": "source_to_candidate_review_loop_evidence.v1",
        "source_to_candidate_graph": {
            "schema_version": "source_to_candidate_graph_activation.v1",
            "status": "PASS_WITH_GAPS",
            "target_scope": "local_test",
            "pack_type": "candidate_graph_review",
            "candidate_count": 3,
            "accepted_count": 0,
            "quality_gate": {"source_to_candidate_graph": "PASS"},
            "production_mutation_performed": False,
            "mutation_performed": False,
        },
        "candidate_review_edit": {
            "schema_version": "candidate_review_edit_result.v1",
            "status": "PASS",
            "target_scope": "local_test",
            "mutation_mode": "no_mutation",
            "edited_candidate_count": 3,
            "rejected_edit_count": 0,
            "production_mutation_performed": False,
            "authority_write_performed": False,
        },
        "approval_board_decision": {
            "schema_version": "approval_board_decision_result.v1",
            "status": "PASS",
            "ledger_scope": "local_test",
            "authority_write_scope": "local_test",
            "decision_count": 1,
            "authority_write_performed": True,
            "production_mutation_performed": False,
        },
        "read_after_write": {
            "status": "validated",
            "object_pack_schema": "object_pack.v1",
            "route": "authority_archive_separation",
            "authority_lane": "accepted_current",
            "object_count": 1,
        },
        "postcheck": {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
    }


def _session_project_rollup_runtime_evidence() -> dict:
    return {
        "schema_version": "session_project_rollup_runtime_evidence.v1",
        "rollup_preview": {
            "schema_version": "object_extraction_session_project_rollup_preview.v1",
            "status": "pass",
            "scope": "all_devices",
            "object_type_counts": {
                "Device": 2,
                "Session": 2,
                "Repository": 1,
                "Branch": 1,
                "WorkUnit": 1,
            },
            "edge_types": [
                "repository_has_branch",
                "session_on_device",
                "device_has_session",
                "session_in_repository",
                "repository_has_session",
                "session_on_branch",
                "branch_has_session",
                "part_of_work_unit",
                "work_unit_has_session",
            ],
            "object_count": 7,
            "edge_count": 12,
            "visible_session_count": 2,
            "all_device_session_count": 2,
            "device_count": 2,
            "production_mutation_performed": False,
        },
        "handoff_pack": {
            "schema_version": "session_project_handoff_pack.v1",
            "raw_return_capability": "denied",
            "visible_session_count": 2,
            "object_ref_counts": {"Session": 2, "WorkUnit": 1},
            "resume_context": {
                "schema_version": "session_project_resume_context.v1",
                "latest_session_ref_present": True,
                "work_unit_ref_count": 1,
                "production_mutation_performed": False,
            },
        },
        "read_after_write": {
            "status": "validated",
            "route": "temporal_work_recall",
            "object_pack_schema": "object_pack.v1",
            "object_types": ["WorkUnit"],
            "object_count": 1,
        },
        "postcheck": {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
    }


def _preference_artifact_memory_evidence() -> dict:
    accepted_object = {
        "object_id": "ko:ArtifactPreference:html-review-density",
        "object_type": "ArtifactPreference",
        "authority_lane": "accepted_current",
    }
    proposal_object = {
        "object_id": "ko:ArtifactPreference:visualization-proposal",
        "object_type": "ArtifactPreference",
        "authority_lane": "proposal_only",
    }
    return {
        "schema_version": "preference_artifact_memory_runtime_evidence.v1",
        "preference_object_pack": {
            "schema_version": "object_pack.v1",
            "route": "code_style_preference",
            "accepted_preference_count": 1,
            "proposal_preference_count": 1,
            "objects": [accepted_object, proposal_object],
            "lanes": {
                "accepted_current": [accepted_object],
                "proposal_only": [proposal_object],
            },
            "recommended_actions": [
                {"object_id": accepted_object["object_id"], "action": "apply_preference"},
                {"object_id": proposal_object["object_id"], "action": "review_inferred_preference"},
            ],
            "gaps": [],
            "production_mutation_performed": False,
        },
        "html_visualization_route_smoke": {
            "schema_version": "brain_objects_query.v1",
            "route": "html_visualization_preference",
            "production_mutation_performed": False,
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": "html_visualization_preference",
                "objects": [accepted_object],
                "lanes": {"accepted_current": [accepted_object]},
                "recommended_actions": [
                    {"object_id": accepted_object["object_id"], "action": "apply_preference"}
                ],
                "gaps": [],
            },
        },
        "agent_context_preference_section": {
            "schema_version": "agent_context_product_pack.v1",
            "section": "style_preference",
            "object_count": 1,
            "accepted_preference_count": 1,
            "surface_policy": {"mutation_allowed": False},
        },
        "artifact_review_check": {
            "schema_version": "artifact_review_preference_check.v1",
            "status": "pass",
            "ui_required": False,
            "raw_artifact_body_returned": False,
            "assertions": ["accepted_html_preference_available"],
        },
        "postcheck": {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
    }


def _permission_sensitive_audit_evidence() -> dict:
    event_base = {
        "schema_version": "runtime_permission_audit_event.v1",
        "event_type": "permission_sensitive_runtime_action",
        "ledger_scope": "production",
        "permission": "denied",
        "authority_write_performed": False,
        "production_mutation_performed": False,
        "actor_ref_hash": "sha256:" + "c" * 24,
        "request_hash": "sha256:" + "d" * 24,
        "protected_values_returned": False,
        "raw_private_evidence_returned": False,
        "secret_returned": False,
        "host_topology_returned": False,
        "raw_external_ids_returned": False,
    }
    return {
        "schema_version": "permission_sensitive_runtime_audit_evidence.v1",
        "audit_events": [
            {**event_base, "action": BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME},
            {**event_base, "action": BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME},
        ],
        "audit_store": {
            "status": "recorded",
            "event_count": 2,
            "production_mutation_performed": False,
        },
        "postcheck": {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
    }


def _agent_context_startup_runtime_evidence() -> dict:
    return {
        "schema_version": "agent_context_startup_runtime_evidence.v1",
        "startup_context": {
            "schema_version": "agent_context_product_pack.v1",
            "consumer": "codex",
            "loaded_on_startup": True,
            "section_counts": {
                "style_preference": 1,
                "active_work": 1,
                "required_verification": 1,
            },
            "surface_policy": {"mutation_allowed": False},
            "degraded_gap_disclosure_present": True,
            "missing_evidence_before_promotion_present": True,
        },
        "read_path_smoke": {
            "tool": BRAIN_OBJECTS_QUERY_TOOL_NAME,
            "read_only": True,
            "routes_checked": [
                "authority_archive_separation",
                "code_style_preference",
                "temporal_work_recall",
                "deployment_runtime_truth",
            ],
            "production_mutation_performed": False,
        },
        "runtime_enforcement": {
            "direct_execution_allowed": False,
            "production_mutation_allowed": False,
            "raw_private_context_blocked": True,
            "approval_scope_blocker_enforced": True,
            "stale_or_degraded_disclosure_present": True,
        },
        "postcheck": {
            "status": "validated",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
    }


def _production_authority_execution_from_smoke(
    *,
    proposal: dict,
    decision: dict,
    state: dict,
    queued_item: dict,
) -> dict:
    target_object_id = proposal["target_object_id"]
    return {
        "schema_version": "object_authority_bounded_execution_evidence.v1",
        "approval": {
            "approved": True,
            "approval_ref_hash": proposal["production_gate_ref_hash"],
            "scope": "single_project_single_object",
            "project": proposal["project"],
            "max_objects": 1,
        },
        "proposal": {
            "proposal_write_performed": proposal["proposal_write_performed"],
            "proposal_write_target": proposal["proposal_write_target"],
            "authority_write_performed": proposal["authority_write_performed"],
            "production_mutation_performed": proposal["production_mutation_performed"],
            "ledger_scope": proposal["ledger_scope"],
            "target_object_id": target_object_id,
            "production_gate_ref_hash": proposal["production_gate_ref_hash"],
        },
        "decision": {
            "authority_write_performed": decision["authority_write_performed"],
            "authoritative_memory_changed": decision["authoritative_memory_changed"],
            "production_mutation_performed": decision["production_mutation_performed"],
            "authority_write_scope": decision["authority_write_scope"],
            "ledger_scope": decision["ledger_scope"],
            "target_object_id": decision["target_object_id"],
            "decision_id": decision["decision_id"],
            "production_gate_ref_hash": decision["production_gate_ref_hash"],
        },
        "read_after_write": {
            "status": "validated",
            "target_object_id": target_object_id,
            "authority_lane": state["authority_lane"],
            "decision_id": state["decision_id"],
        },
        "rollback_or_supersession": {
            "status": "planned",
            "path": [
                "write_new_authority_decision_preserving_audit_history",
                "demote_prior_object_to_accepted_non_current_or_archive_only",
                "verify_brain_objects_query_read_after_write",
            ],
        },
        "postcheck": {
            "status": "validated",
            "review_queue_status": queued_item["status"],
            "raw_private_evidence_returned": False,
        },
        "scope": {
            "project": proposal["project"],
            "object_ids": [target_object_id],
            "max_objects": 1,
            "allowed_object_classes": ["RepoDocument"],
        },
    }


def test_mcp_source_to_candidate_runtime_readiness_accepts_bounded_execution_evidence_from_local_production_gate_simulation(
    tmp_path: Path,
):
    service = _service(tmp_path)
    service.allow_production_object_authority_writes = True
    production_gate = {
        "approved": True,
        "approval_ref": "preapproved-user-gate-2026-07-06",
        "scope": "single_project_single_object",
        "project": PROJECT,
        "max_objects": 1,
        "configured_deployed_mcp_identity_matches_source": True,
        "read_after_write_smoke_plan": True,
        "rollback_or_supersession_plan": True,
        "no_raw_private_evidence": True,
    }
    target_object_id = "ko:RepoDocument:production-gate-runtime-readiness"
    proposal = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 121,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME,
                "arguments": {
                    "proposal_type": "propose_current",
                    "target_object_id": target_object_id,
                    "reason": "Bounded production proposal smoke for runtime readiness.",
                    "evidence_refs": ["github_pr:95", "git_commit:73d5f6a"],
                    "ledger_scope": "production",
                    "project": PROJECT,
                    "proposer": "codex",
                    "production_gate": production_gate,
                },
            },
        },
        service,
    )["result"]["structuredContent"]
    decision = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 122,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME,
                "arguments": {
                    "proposal_id": proposal["proposal_id"],
                    "decision_type": "reject_candidate",
                    "target_object_id": target_object_id,
                    "previous_authority_lane": "proposal_only",
                    "new_authority_lane": "rejected",
                    "approved_by": "preapproved-user-gate-2026-07-06",
                    "decision_id": "decision:production-runtime-readiness",
                    "decision_reason": "Bounded production decision smoke rejects the candidate.",
                    "ledger_scope": "production",
                    "project": PROJECT,
                    "production_gate": production_gate,
                },
            },
        },
        service,
    )["result"]["structuredContent"]
    state = service.ledger.get_object_authority_state(target_object_id)
    queued_item = service.object_review_proposals(project=PROJECT)["items"][0]
    evidence = _runtime_readiness_complete_evidence(
        production_authority_execution=_production_authority_execution_from_smoke(
            proposal=proposal,
            decision=decision,
            state=state,
            queued_item=queued_item,
        )
    )
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 123,
            "method": "tools/call",
            "params": {
                "name": BRAIN_SOURCE_TO_CANDIDATE_RUNTIME_READINESS_TOOL_NAME,
                "arguments": {
                    "live_evidence": evidence,
                    "expected_commit": "73d5f6a",
                },
            },
        },
        service,
    )

    report = response["result"]["structuredContent"]
    claims = {claim["claim_id"]: claim for claim in report["claims"]}
    assert report["status"] == "PASS"
    assert report["production_mutation_performed"] is True
    assert claims["live.production.object_authority_bounded_execution"]["status"] == "validated"
    assert claims["live.production.object_authority_bounded_execution"]["read_after_write_status"] == "validated"
    assert "bounded_production_authority_execution_unverified" not in report["gaps"]


def test_mcp_source_to_candidate_runtime_readiness_without_evidence_preserves_live_gaps(tmp_path: Path):
    service = _service(tmp_path)

    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 120,
            "method": "tools/call",
            "params": {
                "name": BRAIN_SOURCE_TO_CANDIDATE_RUNTIME_READINESS_TOOL_NAME,
                "arguments": {"expected_commit": "d38bcfa"},
            },
        },
        service,
    )

    report = response["result"]["structuredContent"]
    assert report["status"] == "PASS_WITH_GAPS"
    assert report["live_evidence_provided"] is False
    assert report["production_mutation_performed"] is False
    assert "live_mcp_review_tools_unverified" in report["gaps"]


def test_mcp_source_to_candidate_graph_and_review_approval_preview_roundtrip(tmp_path: Path):
    service = _service(tmp_path)
    bundle = reference_corpus_objects_from_manifest(
        _reference_manifest(),
        project=PROJECT,
        storage_mode="managed_snapshot",
    )
    ingest = service.ledger.upsert_reference_corpus_bundle(bundle, project=PROJECT)

    graph_response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 121,
            "method": "tools/call",
            "params": {
                "name": BRAIN_SOURCE_TO_CANDIDATE_GRAPH_TOOL_NAME,
                "arguments": {
                    "project": PROJECT,
                    "target": "local_test",
                    "corpus_id": ingest["corpus_id"],
                    "consumer": "codex",
                },
            },
        },
        service,
    )

    graph = graph_response["result"]["structuredContent"]
    assert graph["schema_version"] == "source_to_candidate_graph_activation.v1"
    assert graph["status"] == "PASS_WITH_GAPS"
    assert graph["production_mutation_performed"] is False
    assert graph["ledger_mutation_performed"] is False
    assert graph["candidate_graph_review_pack"]["route"] == "candidate_graph_review"
    assert graph["candidate_graph_review_pack"]["lanes"]["candidate"]
    candidate_id = graph["candidate_graph_review_pack"]["lanes"]["candidate"][0]["object_id"]
    original_edge_id = graph["candidate_graph_review_pack"]["edges"][0]["edge_id"]
    original_evidence_id = graph["candidate_graph_review_pack"]["evidence"][0]["evidence_id"]
    added_evidence = EvidenceRef.from_parts(
        evidence_type="source_hash",
        authority_lane="reference_only",
        verification_state="source_hash_verified",
        locator={"kind": "relative_repo_path", "value": "docs/mcp-review-evidence.md"},
        content_hash="sha256:" + "9" * 64,
        summary="Reviewer attached MCP transport evidence.",
    )
    added_edge = KnowledgeEdge.from_parts(
        edge_type="review_supports",
        from_object_id=candidate_id,
        to_object_id=candidate_id,
        evidence_refs=[added_evidence.evidence_id],
        lifecycle_status="proposed",
        authority_lane="candidate",
        verification_state="unverified",
    )

    edit_response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 122,
            "method": "tools/call",
            "params": {
                "name": BRAIN_CANDIDATE_REVIEW_EDIT_TOOL_NAME,
                "arguments": {
                    "pack": graph["candidate_graph_review_pack"],
                    "target": "production",
                    "mutation_mode": "no_mutation",
                    "edits": [
                        {
                            "action": "update_object",
                            "object_id": candidate_id,
                            "fields": {
                                "summary": "Reviewer clarified candidate from MCP preview.",
                                "recommended_action": "promote",
                            },
                        },
                        {
                            "action": "add_evidence",
                            "attach_to_object_id": candidate_id,
                            "fields": {
                                "evidence_type": "source_hash",
                                "locator": {"kind": "relative_repo_path", "value": "docs/mcp-review-evidence.md"},
                                "content_hash": "sha256:" + "9" * 64,
                                "summary": "Reviewer attached MCP transport evidence.",
                            },
                        },
                        {
                            "action": "add_edge",
                            "fields": {
                                "edge_type": "review_supports",
                                "from_object_id": candidate_id,
                                "to_object_id": candidate_id,
                                "evidence_refs": [added_evidence.evidence_id],
                            },
                        },
                        {"action": "remove_edge", "edge_id": original_edge_id},
                        {"action": "remove_evidence", "evidence_id": original_evidence_id},
                    ],
                    "reviewer_id": "reviewer-local",
                },
            },
        },
        service,
    )
    edit_result = edit_response["result"]["structuredContent"]
    assert edit_result["schema_version"] == "candidate_review_edit_result.v1"
    assert edit_result["permission"] == "allowed"
    assert edit_result["target_scope"] == "production"
    assert edit_result["mutation_mode"] == "no_mutation"
    assert edit_result["candidate_state_changed"] is True
    assert edit_result["authority_write_performed"] is False
    assert edit_result["production_mutation_performed"] is False
    assert edit_result["rejected_edits"] == []
    assert [item["action"] for item in edit_result["accepted_edits"]] == [
        "update_object",
        "add_evidence",
        "add_edge",
        "remove_edge",
        "remove_evidence",
    ]
    assert added_evidence.evidence_id in {
        item["evidence_id"] for item in edit_result["updated_pack"]["evidence"]
    }
    assert original_evidence_id not in {
        item["evidence_id"] for item in edit_result["updated_pack"]["evidence"]
    }
    assert added_edge.edge_id in {item["edge_id"] for item in edit_result["updated_pack"]["edges"]}
    assert original_edge_id not in {item["edge_id"] for item in edit_result["updated_pack"]["edges"]}

    decision_response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 123,
            "method": "tools/call",
            "params": {
                "name": BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME,
                "arguments": {
                    "target": "local_test",
                    "pack": edit_result["updated_pack"],
                    "decisions": [
                        {
                            "action": "promote",
                            "object_id": candidate_id,
                            "reason": "MCP local test approval preview.",
                            "approved_by": "reviewer-local",
                        }
                    ],
                    "reviewer_id": "reviewer-local",
                },
            },
        },
        service,
    )
    decision_result = decision_response["result"]["structuredContent"]
    assert decision_result["schema_version"] == "approval_board_decision_result.v1"
    assert decision_result["permission"] == "allowed"
    assert decision_result["authority_write_scope"] == "local_test"
    assert decision_result["production_mutation_performed"] is False
    assert decision_result["updated_pack"]["lanes"]["accepted_current"][0]["object_id"] == candidate_id


def test_mcp_approval_board_preview_denies_production_without_mutation(tmp_path: Path):
    service = _service(tmp_path)

    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 124,
            "method": "tools/call",
            "params": {
                "name": BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME,
                "arguments": {
                    "target": "production",
                    "pack": {
                        "schema_version": "object_pack.v1",
                        "route": "candidate_graph_review",
                        "candidate_graph_hash": "sha256:" + "6" * 64,
                        "objects": [],
                        "edges": [],
                        "evidence": [],
                    },
                    "decisions": [{"action": "promote", "object_id": "ko:ReferenceDocument:test"}],
                    "reviewer_id": "reviewer-local",
                },
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    assert result["schema_version"] == "approval_board_decision_result.v1"
    assert result["permission"] == "denied"
    assert result["production_mutation_performed"] is False
    assert result["authority_write_performed"] is False
    assert result["promotion_plan"]["production_mutation_performed"] is False


def test_project_deriving_brain_tool_schemas_allow_repository():
    tools = {tool["name"]: tool for tool in list_tools()}

    expected_required = {
        BRAIN_MEMORY_SEARCH_TOOL_NAME: ["query"],
        BRAIN_INCIDENT_SEARCH_TOOL_NAME: ["symptom"],
        BRAIN_DRIFT_EXPLAIN_TOOL_NAME: ["subject"],
        BRAIN_PERSONA_GET_TOOL_NAME: None,
        BRAIN_PERSONA_CHECK_TOOL_NAME: ["plan"],
    }
    for tool_name, required in expected_required.items():
        schema = tools[tool_name]["inputSchema"]
        assert "repository" in schema["properties"]
        assert "project" in schema["properties"]
        assert schema.get("required") == required


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


def test_mcp_brain_objects_query_roundtrip(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECTS_QUERY_TOOL_NAME,
                "arguments": {
                    "repository": FIXTURE_REPOSITORY,
                    "branch": FIXTURE_BRANCH,
                    "query": "이 repo 문서 최신화하려면 뭘 봐야 해?",
                    "current_files": ["README.md"],
                    "consumer": "gemini",
                    "limit": None,
                },
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    assert result["schema_version"] == "brain_objects_query.v1"
    assert result["route"] == "documentation_cleanup"
    assert result["object_pack"]["schema_version"] == "object_pack.v1"


def test_mcp_brain_objects_query_applies_object_type_filter_and_response_mode(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECTS_QUERY_TOOL_NAME,
                "arguments": {
                    "repository": FIXTURE_REPOSITORY,
                    "branch": FIXTURE_BRANCH,
                    "query": "이 repo 문서 최신화하려면 뭘 봐야 해?",
                    "current_files": ["README.md"],
                    "object_types": ["ReferenceDocument"],
                    "response_mode": "compact",
                },
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    assert result["response_mode"] == "compact"
    assert result["object_pack"]["audit"]["object_type_filter"] == ["ReferenceDocument"]
    assert result["object_pack"]["objects"] == []


def test_mcp_brain_objects_query_default_route_returns_agent_context_objects(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECTS_QUERY_TOOL_NAME,
                "arguments": {
                    "repository": FIXTURE_REPOSITORY,
                    "branch": FIXTURE_BRANCH,
                    "query": "LBrain source-to-candidate-graph product activation roadmap P5 P6 P7 P8 P9 current gaps",
                    "current_files": ["docs/specs/roadmap.md"],
                    "consumer": "codex",
                    "response_mode": "compact",
                },
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    pack = result["object_pack"]
    assert result["route"] == "authority_archive_separation"
    assert pack["objects"]
    assert "object_pack_route_not_implemented" not in pack["gaps"]
    assert pack["recommended_actions"]
    assert {obj["object_type"] for obj in pack["objects"]} >= {"ArtifactPreference", "Test", "ToolHandoffContext"}
    assert pack["audit"]["object_pack_route_source"] == "context_authority_object_packs"
    assert pack["route_trace"]["route"] == "authority_archive_separation"
    assert "reference_only" in pack["route_trace"]["selected_source_lanes"]
    assert pack["route_trace"]["route_source"] == "inferred"
    assert pack["route_trace"]["stop_reason"] == "returned_object_pack"
    assert isinstance(pack["route_trace"]["missing_evidence"], list)
    assert pack["response_mode"] == "compact"


def test_mcp_brain_objects_query_temporal_route_returns_current_work_objects(tmp_path: Path):
    service = _service(tmp_path)
    service.ledger.upsert_llm_brain_memory_card(
        _accepted_task_card(
            "mem_temporal_work_recall",
            next_action="Continue P6 temporal repo recall object query route",
        )
    )

    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECTS_QUERY_TOOL_NAME,
                "arguments": {
                    "repository": FIXTURE_REPOSITORY,
                    "branch": FIXTURE_BRANCH,
                    "query": "어제 이 repo에서 뭐 했어? 작업 재개하려면 뭐 봐야 해?",
                    "current_files": ["docs/specs/roadmap.md"],
                    "consumer": "codex",
                    "response_mode": "compact",
                },
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    pack = result["object_pack"]
    assert result["route"] == "temporal_work_recall"
    assert "object_pack_route_not_implemented" not in pack["gaps"]
    assert any(obj["object_type"] == "WorkUnit" for obj in pack["objects"])
    assert any("P6 temporal repo recall" in obj["title"] for obj in pack["objects"])
    assert pack["recommended_actions"]
    assert pack["audit"]["source_pack_names"] == ["current_work", "required_verification"]
    assert pack["response_mode"] == "compact"


def test_mcp_brain_objects_query_style_route_uses_preference_objects(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECTS_QUERY_TOOL_NAME,
                "arguments": {
                    "repository": FIXTURE_REPOSITORY,
                    "branch": FIXTURE_BRANCH,
                    "query": "내 code style과 preference를 보여줘",
                    "current_files": ["worker/tests/test_neuron_mcp_stdio.py"],
                    "response_mode": "compact",
                },
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    pack = result["object_pack"]
    assert result["route"] == "code_style_preference"
    assert "object_pack_route_not_implemented" not in pack["gaps"]
    assert any(obj["object_type"] == "ArtifactPreference" for obj in pack["objects"])


def test_mcp_brain_objects_query_html_visualization_route_uses_artifact_preferences(tmp_path: Path):
    service = _service(tmp_path)
    service.ledger.upsert_llm_brain_memory_card(
        _accepted_preference_card(
            "mem_html_review_preference",
            preference="HTML review artifacts should be information dense.",
            applies_to="html review artifact",
        )
    )

    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECTS_QUERY_TOOL_NAME,
                "arguments": {
                    "repository": FIXTURE_REPOSITORY,
                    "branch": FIXTURE_BRANCH,
                    "query": "내가 선호하는 HTML review artifact 기준으로 이 산출물을 평가해줘.",
                    "current_files": [],
                    "consumer": "codex",
                    "response_mode": "compact",
                },
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    pack = result["object_pack"]
    assert result["route"] == "html_visualization_preference"
    assert pack["route"] == "html_visualization_preference"
    assert any(obj["object_type"] == "ArtifactPreference" for obj in pack["objects"])
    assert any("HTML review artifacts should be information dense." in obj["title"] for obj in pack["objects"])
    assert "accepted_html_preference_missing" not in pack["gaps"]
    assert "object_pack_route_not_implemented" not in pack["gaps"]
    assert pack["route_trace"]["selected_source_lanes"] == ["reference_only"]
    assert pack["route_trace"]["stop_reason"] == "returned_object_pack"


def test_mcp_brain_objects_query_html_visualization_route_can_be_explicit(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECTS_QUERY_TOOL_NAME,
                "arguments": {
                    "repository": FIXTURE_REPOSITORY,
                    "branch": FIXTURE_BRANCH,
                    "route": "html_visualization_preference",
                    "query": "문서 정리 질문이어도 명시 route가 우선이어야 한다",
                    "current_files": [],
                    "consumer": "codex",
                    "response_mode": "compact",
                },
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    pack = result["object_pack"]
    assert result["route"] == "html_visualization_preference"
    assert pack["route_trace"]["route_source"] == "explicit"
    assert pack["route_trace"]["missing_evidence"] == [
        "accepted_html_preference_missing",
        "visualization_preference_missing",
    ]
    assert "object_pack_route_not_implemented" not in pack["gaps"]


def test_mcp_brain_objects_query_html_visualization_route_filters_unrelated_preferences(tmp_path: Path):
    service = _service(tmp_path)
    service.ledger.upsert_llm_brain_memory_card(
        _accepted_preference_card(
            "mem_html_review_preference",
            preference="HTML review artifacts should be information dense.",
            applies_to="html review artifact",
        )
    )
    service.ledger.upsert_llm_brain_memory_card(
        _accepted_preference_card(
            "mem_commit_preference",
            preference="Commit messages should be concise.",
            applies_to="commit message",
        )
    )

    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECTS_QUERY_TOOL_NAME,
                "arguments": {
                    "repository": FIXTURE_REPOSITORY,
                    "branch": FIXTURE_BRANCH,
                    "query": "내가 선호하는 HTML review artifact 기준으로 이 산출물을 평가해줘.",
                    "current_files": [],
                    "consumer": "codex",
                    "response_mode": "compact",
                },
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    titles = [obj["title"] for obj in result["object_pack"]["objects"]]
    assert result["route"] == "html_visualization_preference"
    assert any("HTML review artifacts should be information dense." in title for title in titles)
    assert all("Commit messages should be concise." not in title for title in titles)


def test_mcp_html_preference_memory_card_rejects_private_preference_text_before_route(tmp_path: Path):
    service = _service(tmp_path)

    with pytest.raises(ValueError, match="forbidden private/source content") as excinfo:
        service.ledger.upsert_llm_brain_memory_card(
            _accepted_preference_card(
                "mem_html_private_preference",
                preference="HTML artifact note at /Users/example/private with API_KEY=secret",
                applies_to="html review artifact",
            )
        )

    assert "/Users/example" not in str(excinfo.value)
    assert "API_KEY=secret" not in str(excinfo.value)


def test_brain_objects_query_html_visualization_route_rejects_private_pack_text():
    service = BrainReadService()
    raw_text = "HTML artifact note at /Users/example/private with API_KEY=secret"
    obj = {
        "object_id": "ko:test:html-private",
        "object_type": "ArtifactPreference",
        "title": raw_text,
        "summary": raw_text,
        "authority_lane": "reference_only",
        "payload": {"applies_to": "html review artifact"},
    }

    class _ResolvedContext:
        def to_dict(self) -> dict:
            return {
                "authority": {
                    "object_packs": {
                        "preferences": {
                            "schema_version": "object_pack.v1",
                            "route": "code_style_preference",
                            "objects": [obj],
                            "edges": [],
                            "evidence": [],
                            "lanes": {"reference_only": [obj]},
                            "verification": {},
                            "gaps": [],
                            "recommended_actions": [{"object_id": obj["object_id"], "action": "review"}],
                        },
                        "style": {
                            "schema_version": "object_pack.v1",
                            "route": "code_style_preference",
                            "objects": [],
                            "edges": [],
                            "evidence": [],
                            "lanes": {},
                            "verification": {},
                            "gaps": [],
                            "recommended_actions": [],
                        },
                    }
                },
                "audit": {"request_hash": "sha256:" + "1" * 64},
            }

    service.brain_context_resolve = lambda **_: _ResolvedContext()  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="private or raw") as excinfo:
        service.brain_objects_query(
            repository=FIXTURE_REPOSITORY,
            branch=FIXTURE_BRANCH,
            route="html_visualization_preference",
            query="내가 선호하는 HTML review artifact 기준으로 이 산출물을 평가해줘.",
            current_files=[],
            consumer="codex",
            response_mode="compact",
        )

    assert "/Users/example" not in str(excinfo.value)
    assert "API_KEY=secret" not in str(excinfo.value)


def test_mcp_brain_objects_query_deploy_route_returns_runtime_gap_pack(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECTS_QUERY_TOOL_NAME,
                "arguments": {
                    "repository": FIXTURE_REPOSITORY,
                    "branch": FIXTURE_BRANCH,
                    "query": "이 PR merge됐어? 배포도 됐어?",
                    "current_files": [],
                    "response_mode": "compact",
                },
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    pack = result["object_pack"]
    assert result["route"] == "deployment_runtime_truth"
    assert any(obj["object_type"] == "PullRequest" for obj in pack["objects"])
    assert "runtime_evidence_unverified" in pack["gaps"]
    assert "object_pack_route_not_implemented" not in pack["gaps"]


def test_mcp_brain_objects_query_code_change_impact_route_returns_impact_pack(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECTS_QUERY_TOOL_NAME,
                "arguments": {
                    "repository": FIXTURE_REPOSITORY,
                    "branch": FIXTURE_BRANCH,
                    "query": "이 파일 바꾸면 어떤 테스트/런타임 영향 있어?",
                    "current_files": [
                        "worker/lib/agent_knowledge/llm_brain_core/objects/runtime_readiness.py"
                    ],
                    "consumer": "codex",
                    "response_mode": "compact",
                },
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    pack = result["object_pack"]
    object_types = {obj["object_type"] for obj in pack["objects"]}
    assert result["route"] == "code_change_impact"
    assert {"RepoFile", "VerificationCommand", "RuntimeSurface"} <= object_types
    assert any(edge["edge_type"] == "validated_by" for edge in pack["edges"])
    assert any(edge["edge_type"] == "requires_live_evidence" for edge in pack["edges"])
    assert "live_runtime_impact_unverified" in pack["gaps"]
    assert "object_pack_route_not_implemented" not in pack["gaps"]
    assert pack["route_trace"] == {
        "schema_version": "object_query_route_trace.v1",
        "route": "code_change_impact",
        "route_source": "inferred",
        "selected_source_lanes": ["candidate", "reference_only"],
        "confidence": pack["confidence"],
        "stop_reason": "missing_evidence_gap_returned",
        "missing_evidence": [
            "live_runtime_impact_unverified",
            "source_freshness_unverified",
        ],
    }
    assert pack["response_mode"] == "compact"


def test_mcp_object_proposal_create_local_test_and_production_denial(tmp_path: Path):
    service = _service(tmp_path)
    local = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 102,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME,
                "arguments": {
                    "proposal_type": "propose_stale",
                    "target_object_id": "ko:RepoDocument:old",
                    "reason": "Old doc needs review.",
                    "evidence_refs": ["ev:source_hash:old"],
                    "ledger_scope": "local_test",
                    "proposer": "codex",
                },
            },
        },
        service,
    )["result"]["structuredContent"]
    denied = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 103,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME,
                "arguments": {
                    "proposal_type": "propose_stale",
                    "target_object_id": "ko:RepoDocument:old",
                    "reason": "Old doc needs review.",
                    "ledger_scope": "production",
                    "proposer": "codex",
                },
            },
        },
        service,
    )["result"]["structuredContent"]

    assert local["proposal_write_performed"] is True
    assert local["proposal_write_target"] == "local_test_ledger"
    assert local["authority_write_performed"] is False
    assert local["authoritative_memory_changed"] is False
    ledger_items = service.ledger.list_object_review_proposals()
    assert ledger_items[0]["proposal_id"] == local["proposal_id"]
    assert service.object_review_proposals(limit=None)["count"] == 1
    queued = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 105,
            "method": "tools/call",
            "params": {"name": BRAIN_REVIEW_PROPOSALS_TOOL_NAME, "arguments": {}},
        },
        service,
    )["result"]["structuredContent"]
    assert queued["count"] == 1
    assert queued["items"][0]["proposal_id"] == local["proposal_id"]
    assert denied["permission"] == "denied"
    assert denied["reason"] == "proposal_write_requires_local_test_ledger_or_later_production_gate"
    assert denied["proposal_write_performed"] is False
    assert denied["authoritative_memory_changed"] is False


def test_mcp_object_authority_production_gate_writes_single_object_with_postcheck(tmp_path: Path):
    service = _service(tmp_path)
    service.allow_production_object_authority_writes = True
    production_gate = {
        "approved": True,
        "approval_ref": "preapproved-user-gate-2026-07-06",
        "scope": "single_project_single_object",
        "project": PROJECT,
        "max_objects": 1,
        "configured_deployed_mcp_identity_matches_source": True,
        "read_after_write_smoke_plan": True,
        "rollback_or_supersession_plan": True,
        "no_raw_private_evidence": True,
    }
    target_object_id = "ko:RepoDocument:production-gate-smoke"
    missing_gate = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 105,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME,
                "arguments": {
                    "proposal_type": "propose_current",
                    "target_object_id": target_object_id,
                    "reason": "Production write still needs per-call gate.",
                    "ledger_scope": "production",
                    "project": PROJECT,
                    "proposer": "codex",
                },
            },
        },
        service,
    )["result"]["structuredContent"]

    assert missing_gate["permission"] == "denied"
    assert missing_gate["reason"] == "proposal_write_requires_local_test_ledger_or_later_production_gate"
    assert missing_gate["proposal_write_performed"] is False
    assert service.object_review_proposals(project=PROJECT)["count"] == 0

    proposal = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 106,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME,
                "arguments": {
                    "proposal_type": "propose_current",
                    "target_object_id": target_object_id,
                    "reason": "Bounded production proposal smoke.",
                    "evidence_refs": ["github_pr:95", "git_commit:f8bbb42"],
                    "ledger_scope": "production",
                    "project": PROJECT,
                    "proposer": "codex",
                    "production_gate": production_gate,
                },
            },
        },
        service,
    )["result"]["structuredContent"]

    assert proposal["ledger_scope"] == "production"
    assert proposal["proposal_write_performed"] is True
    assert proposal["proposal_write_target"] == "production_ledger"
    assert proposal["production_mutation_performed"] is True
    assert proposal["authority_write_performed"] is False
    assert service.object_review_proposals(project=PROJECT)["count"] == 1

    decision = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 107,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME,
                "arguments": {
                    "proposal_id": proposal["proposal_id"],
                    "decision_type": "reject_candidate",
                    "target_object_id": target_object_id,
                    "previous_authority_lane": "proposal_only",
                    "new_authority_lane": "rejected",
                    "approved_by": "preapproved-user-gate-2026-07-06",
                    "decision_id": "decision:production-gate-smoke",
                    "decision_reason": "Bounded production decision smoke rejects the candidate.",
                    "ledger_scope": "production",
                    "project": PROJECT,
                    "production_gate": production_gate,
                },
            },
        },
        service,
    )["result"]["structuredContent"]

    assert decision["ledger_scope"] == "production"
    assert decision["authority_write_scope"] == "production_ledger"
    assert decision["authority_write_performed"] is True
    assert decision["authoritative_memory_changed"] is True
    assert decision["production_mutation_performed"] is True
    state = service.ledger.get_object_authority_state(target_object_id)
    assert state["authority_lane"] == "rejected"
    assert state["decision_id"] == "decision:production-gate-smoke"
    queued = service.object_review_proposals(project=PROJECT)
    assert queued["items"][0]["status"] == "rejected"
    assert queued["items"][0]["decision_id"] == "decision:production-gate-smoke"


def test_mcp_corpus_ingest_plan_reports_manifest_ref_gap(tmp_path: Path):
    service = _service(tmp_path)
    result = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 106,
            "method": "tools/call",
            "params": {
                "name": BRAIN_CORPUS_INGEST_PLAN_TOOL_NAME,
                "arguments": {
                    "manifest_ref": "refs/palantir.json",
                    "storage_mode": "metadata_only",
                    "project": "neurons",
                },
            },
        },
        service,
    )["result"]["structuredContent"]

    assert "manifest_ref_not_loaded" in result["gaps"]
    assert result["manifest_ref"] == "refs/palantir.json"


def test_mcp_corpus_ingest_plan_expected_count_gate(tmp_path: Path):
    service = _service(tmp_path)
    result = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 109,
            "method": "tools/call",
            "params": {
                "name": BRAIN_CORPUS_INGEST_PLAN_TOOL_NAME,
                "arguments": {
                    "manifest": _reference_manifest(),
                    "storage_mode": "metadata_only",
                    "project": "neurons",
                    "expected_source_count": 2,
                    "expected_source_url_count": 1,
                    "expected_manual_text_without_url_count": 1,
                    "expected_source_type_counts": {"WEB_PAGE": 1, "TEXT": 1},
                },
            },
        },
        service,
    )["result"]["structuredContent"]

    assert result["count_gate_status"] == "pass"
    assert result["count_gate_gaps"] == []
    assert result["writes_planned"] is False


def test_mcp_corpus_status_reports_policy_fields(tmp_path: Path):
    service = _service(tmp_path)
    result = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 107,
            "method": "tools/call",
            "params": {
                "name": BRAIN_CORPUS_STATUS_TOOL_NAME,
                "arguments": {
                    "project": "neurons",
                },
            },
        },
        service,
    )["result"]["structuredContent"]

    assert result["raw_body_policy"]["return_capability"] == "denied_without_explicit_approval"
    assert result["raw_body_policy"]["retention_class"] == "user_managed_reference"
    assert result["raw_body_policy"]["redaction_profile"] == "public_safe_summary"
    assert result["raw_body_policy"]["deletion_policy"] == "delete_snapshot_keep_metadata"
    assert result["raw_body_policy"]["license_source_rights"] == "operator_attested"
    assert result["source_rights_policy"] == "operator_attested_reference_use"
    assert "managed_snapshot" in result["supported_storage_modes"]


def test_mcp_corpus_status_reads_local_test_ledger_store(tmp_path: Path):
    service = _service(tmp_path)
    bundle = reference_corpus_objects_from_manifest(
        _reference_manifest(),
        project=PROJECT,
        storage_mode="managed_snapshot",
    )
    service.ledger.upsert_reference_corpus_bundle(bundle, project=PROJECT)

    result = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 108,
            "method": "tools/call",
            "params": {
                "name": BRAIN_CORPUS_STATUS_TOOL_NAME,
                "arguments": {
                    "project": PROJECT,
                    "corpus_id": bundle["corpus"]["corpus_id"],
                },
            },
        },
        service,
    )["result"]["structuredContent"]

    assert result["source_count"] == 2
    assert result["storage_modes"] == {"managed_snapshot": 2}
    assert result["reference_object_count"] == 2
    assert result["document_source_count"] == 2
    assert result["version_count"] == 2
    assert result["snapshot_count"] == 2
    assert result["chunk_count"] == 2
    assert result["freshness_check_count"] == 2
    assert result["extraction_run_count"] == 1
    assert result["first_class_store_counts"]["document_sources"] == 2
    assert result["first_class_store_counts"]["document_snapshots"] == 2
    assert result["first_class_store_counts"]["document_chunks"] == 2
    assert result["first_class_store_counts"]["freshness_checks"] == 2
    assert result["first_class_store_counts"]["extraction_runs"] == 1
    assert result["document_sources"][0]["schema_version"] == "document_source.v1"
    assert result["document_versions"][0]["schema_version"] == "document_version.v1"
    assert result["document_snapshots"][0]["schema_version"] == "document_snapshot.v1"
    assert result["document_chunks"][0]["schema_version"] == "document_chunk.v1"
    assert result["freshness_checks"][0]["schema_version"] == "freshness_check.v1"
    assert result["extraction_runs"][0]["status"] == "completed"
    assert result["freshness_gaps"][0]["source_url_status"] == "missing_manual_text"
    assert result["gaps"] == []


def test_mcp_object_decision_commit_is_restricted_denied_by_default(tmp_path: Path):
    service = _service(tmp_path)
    result = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 104,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME,
                "arguments": {
                    "proposal_id": "proposal:old",
                    "decision_type": "commit_stale",
                    "approved_by": "human",
                    "decision_id": "decision:old",
                },
            },
        },
        service,
    )["result"]["structuredContent"]

    assert result["permission"] == "denied"
    assert result["reason"] == "restricted_tool_requires_human_gate"
    assert result["authority_write_performed"] is False
    assert result["authoritative_memory_changed"] is False
    plan = result["production_promotion_plan"]
    assert plan["schema_version"] == "object_authority_promotion_plan.v1"
    assert plan["production_write_state"] == "closed_without_human_gate"
    assert plan["mutation_allowed"] is False
    assert plan["allowed_object_classes"] == ["RepoDocument"]
    assert "commit_stale" in plan["allowed_decision_types"]
    assert plan["reviewer_role"] == "human_object_authority_reviewer"
    assert plan["blast_radius"]["max_objects_per_decision"] == 1
    assert plan["no_mutation_report"] == {
        "proposal_write_performed": False,
        "authority_write_performed": False,
        "authoritative_memory_changed": False,
    }


def test_mcp_object_authority_local_test_write_requires_test_service_gate(tmp_path: Path):
    service = KnowledgeSearchService(
        ledger=_ledger(tmp_path),
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        allow_private_results=True,
    )

    proposal = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 104,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME,
                "arguments": {
                    "proposal_type": "propose_current",
                    "target_object_id": "ko:RepoDocument:gate",
                    "reason": "Should not write without local-test service gate.",
                    "ledger_scope": "local_test",
                    "project": PROJECT,
                },
            },
        },
        service,
    )["result"]["structuredContent"]

    assert proposal["permission"] == "denied"
    assert proposal["reason"] == "local_test_object_authority_write_requires_test_service_gate"
    assert proposal["proposal_write_performed"] is False
    assert service.object_review_proposals(project=PROJECT)["items"] == []

    decision = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 105,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME,
                "arguments": {
                    "proposal_id": "proposal:gate",
                    "decision_type": "accept_current",
                    "target_object_id": "ko:RepoDocument:gate",
                    "previous_authority_lane": "candidate",
                    "new_authority_lane": "accepted_current",
                    "approved_by": "human-reviewer",
                    "decision_id": "decision:gate",
                    "ledger_scope": "local_test",
                    "project": PROJECT,
                },
            },
        },
        service,
    )["result"]["structuredContent"]

    assert decision["permission"] == "denied"
    assert decision["reason"] == "local_test_object_authority_write_requires_test_service_gate"
    assert decision["authority_write_performed"] is False
    assert service.ledger.get_object_authority_state("ko:RepoDocument:gate") == {}


def test_mcp_object_decision_commit_local_test_updates_authority_state_with_audit(tmp_path: Path):
    service = _service(tmp_path)
    proposal = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 104,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME,
                "arguments": {
                    "proposal_type": "propose_current",
                    "target_object_id": "ko:RepoDocument:current",
                    "reason": "Promote reviewed docs SoT.",
                    "evidence_refs": ["ev:source_hash:current"],
                    "ledger_scope": "local_test",
                    "project": PROJECT,
                    "proposer": "codex",
                },
            },
        },
        service,
    )["result"]["structuredContent"]

    decision = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 105,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME,
                "arguments": {
                    "proposal_id": proposal["proposal_id"],
                    "target_object_id": proposal["target_object_id"],
                    "decision_type": "accept_current",
                    "previous_authority_lane": "candidate",
                    "new_authority_lane": "accepted_current",
                    "evidence_refs": ["ev:source_hash:current"],
                    "decision_reason": "Reviewed local fixture evidence.",
                    "approved_by": "human-reviewer",
                    "decision_id": "decision:local-current",
                    "ledger_scope": "local_test",
                    "project": PROJECT,
                },
            },
        },
        service,
    )["result"]["structuredContent"]

    assert decision["schema_version"] == "authority_decision.v1"
    assert decision["proposal_id"] == proposal["proposal_id"]
    assert decision["target_object_id"] == proposal["target_object_id"]
    assert decision["previous_authority_lane"] == "candidate"
    assert decision["new_authority_lane"] == "accepted_current"
    assert decision["authority_write_performed"] is True
    assert decision["authoritative_memory_changed"] is True
    assert decision["cache_invalidated"] is True
    assert decision["approved_by_hash"].startswith("sha256:")
    assert decision["approved_by"] == "redacted"
    assert decision["decision_reason"] == "Reviewed local fixture evidence."

    state = service.ledger.get_object_authority_state(proposal["target_object_id"])
    assert state["authority_lane"] == "accepted_current"
    assert state["decision_id"] == "decision:local-current"
    assert state["proposal_id"] == proposal["proposal_id"]
    assert state["decision_reason"] == "Reviewed local fixture evidence."
    decisions = service.ledger.list_object_authority_decisions(target_object_id=proposal["target_object_id"])
    assert decisions[0]["decision_id"] == "decision:local-current"
    assert decisions[0]["approved_by_hash"] == decision["approved_by_hash"]
    queued = service.object_review_proposals(project=PROJECT)
    assert queued["items"][0]["status"] == "accepted"


def test_mcp_object_decision_commit_requires_matching_review_proposal(tmp_path: Path):
    service = _service(tmp_path)
    proposal = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 106,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME,
                "arguments": {
                    "proposal_type": "propose_current",
                    "target_object_id": "ko:RepoDocument:proposal-a",
                    "reason": "Promote reviewed docs SoT.",
                    "ledger_scope": "local_test",
                    "project": PROJECT,
                },
            },
        },
        service,
    )["result"]["structuredContent"]

    missing = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 107,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME,
                "arguments": {
                    "proposal_id": "proposal:missing",
                    "target_object_id": "ko:RepoDocument:proposal-a",
                    "decision_type": "accept_current",
                    "previous_authority_lane": "candidate",
                    "new_authority_lane": "accepted_current",
                    "approved_by": "human-reviewer",
                    "decision_id": "decision:missing",
                    "ledger_scope": "local_test",
                    "project": PROJECT,
                },
            },
        },
        service,
    )

    mismatch = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 108,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME,
                "arguments": {
                    "proposal_id": proposal["proposal_id"],
                    "target_object_id": "ko:RepoDocument:proposal-b",
                    "decision_type": "accept_current",
                    "previous_authority_lane": "candidate",
                    "new_authority_lane": "accepted_current",
                    "approved_by": "human-reviewer",
                    "decision_id": "decision:mismatch",
                    "ledger_scope": "local_test",
                    "project": PROJECT,
                },
            },
        },
        service,
    )

    assert missing["error"]["code"] == -32602
    assert mismatch["error"]["code"] == -32602
    assert service.ledger.get_object_authority_state("ko:RepoDocument:proposal-a") == {}
    assert service.ledger.get_object_authority_state("ko:RepoDocument:proposal-b") == {}


def test_mcp_brain_object_explain_includes_local_authority_decision_history(tmp_path: Path):
    service = _service(tmp_path)
    target_object_id = "ko:RepoDocument:explain"
    proposal = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 106,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME,
                "arguments": {
                    "proposal_type": "propose_current",
                    "target_object_id": target_object_id,
                    "reason": "Explain current authority state.",
                    "evidence_refs": ["ev:source_hash:explain"],
                    "ledger_scope": "local_test",
                    "project": PROJECT,
                    "proposer": "codex",
                },
            },
        },
        service,
    )["result"]["structuredContent"]
    handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 107,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME,
                "arguments": {
                    "proposal_id": proposal["proposal_id"],
                    "target_object_id": target_object_id,
                    "decision_type": "accept_current",
                    "previous_authority_lane": "candidate",
                    "new_authority_lane": "accepted_current",
                    "evidence_refs": ["ev:source_hash:explain"],
                    "decision_reason": "Reviewed local fixture evidence.",
                    "approved_by": "human-reviewer",
                    "decision_id": "decision:local-explain-current",
                    "ledger_scope": "local_test",
                    "project": PROJECT,
                },
            },
        },
        service,
    )

    result = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 108,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECT_EXPLAIN_TOOL_NAME,
                "arguments": {"object_id": target_object_id},
            },
        },
        service,
    )["result"]["structuredContent"]

    assert result["schema_version"] == "brain_object_explain.v1"
    assert result["object_id"] == target_object_id
    assert result["object"]["authority_lane"] == "accepted_current"
    assert result["object"]["lifecycle_status"] == "current"
    assert result["authority_state"]["decision_id"] == "decision:local-explain-current"
    assert result["authority_state"]["proposal_id"] == proposal["proposal_id"]
    assert result["decision_history"][0]["decision_id"] == "decision:local-explain-current"
    assert result["decision_history"][0]["approved_by"] == "redacted"
    assert result["decision_history"][0]["approved_by_hash"].startswith("sha256:")
    assert "authority_state_from_ledger_only" in result["gaps"]


@pytest.mark.parametrize(
    ("decision_type", "new_authority_lane", "expected_lifecycle", "expected_review", "expected_action"),
    [
        ("commit_stale", "accepted_non_current", "stale", "accepted", "archive"),
        ("commit_supersession", "accepted_non_current", "superseded", "accepted", "supersede"),
        ("retire", "accepted_non_current", "retired", "accepted", "retire"),
        ("archive_only", "archive_only", "archived", "accepted", "archive"),
        ("reject_candidate", "rejected", "rejected", "rejected", "retire"),
    ],
)
def test_mcp_brain_objects_query_overlays_local_authority_state(
    tmp_path: Path,
    decision_type: str,
    new_authority_lane: str,
    expected_lifecycle: str,
    expected_review: str,
    expected_action: str,
):
    service = _service(tmp_path)
    query_args = {
        "repository": FIXTURE_REPOSITORY,
        "branch": FIXTURE_BRANCH,
        "query": "이 repo 문서 최신화하려면 뭘 봐야 해?",
        "current_files": ["README.md"],
        "consumer": "codex",
        "project": PROJECT,
    }
    before = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 106,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECTS_QUERY_TOOL_NAME,
                "arguments": query_args,
            },
        },
        service,
    )["result"]["structuredContent"]
    target = next(obj for obj in before["object_pack"]["objects"] if obj["title"] == "README.md")

    proposal = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 107,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME,
                "arguments": {
                    "proposal_type": "propose_stale",
                    "target_object_id": target["object_id"],
                    "reason": "README is no longer the current roadmap source.",
                    "evidence_refs": ["ev:source_hash:readme"],
                    "ledger_scope": "local_test",
                    "project": PROJECT,
                    "proposer": "codex",
                },
            },
        },
        service,
    )["result"]["structuredContent"]
    handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 108,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME,
                "arguments": {
                    "proposal_id": proposal["proposal_id"],
                    "target_object_id": target["object_id"],
                    "decision_type": decision_type,
                    "previous_authority_lane": target["authority_lane"],
                    "new_authority_lane": new_authority_lane,
                    "evidence_refs": ["ev:source_hash:readme"],
                    "decision_reason": "Reviewed local fixture evidence.",
                    "approved_by": "human-reviewer",
                    "decision_id": f"decision:local-{expected_lifecycle}-readme",
                    "ledger_scope": "local_test",
                    "project": PROJECT,
                },
            },
        },
        service,
    )

    after = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 109,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECTS_QUERY_TOOL_NAME,
                "arguments": query_args,
            },
        },
        service,
    )["result"]["structuredContent"]

    updated = next(obj for obj in after["object_pack"]["objects"] if obj["object_id"] == target["object_id"])
    assert updated["authority_lane"] == new_authority_lane
    assert updated["lifecycle_status"] == expected_lifecycle
    assert updated["review_state"] == expected_review
    assert updated["recommended_action"] == expected_action
    assert updated["authority_state"]["decision_id"] == f"decision:local-{expected_lifecycle}-readme"
    assert updated["authority_state"]["proposal_id"] == proposal["proposal_id"]
    assert updated["authority_state"]["decision_type"] == decision_type
    lane_ids = {obj["object_id"] for obj in after["object_pack"]["lanes"][new_authority_lane]}
    assert target["object_id"] in lane_ids
    assert all(
        obj["object_id"] != target["object_id"]
        for lane, objects in after["object_pack"]["lanes"].items()
        if lane != new_authority_lane
        for obj in objects
    )


def test_brain_query_semantic_recall_type_error_is_audited(tmp_path: Path, monkeypatch):
    service = _service(tmp_path)
    service.native_memory_id = "mem_native"

    def broken_semantic_recall(**kwargs):
        def recall(query: str, brain_id: str):
            raise TypeError("malformed native-memory hit")

        return recall

    monkeypatch.setattr(
        "agent_knowledge.knowledge_search_service.build_semantic_recall",
        broken_semantic_recall,
    )

    result = service.brain_query(brain_id=f"/project/{PROJECT}", query="언어 선호")

    assert result["current"][0]["summary"] == "한국어로 응답한다"
    assert result["audit"]["native_memory_bound"] is True
    assert result["audit"]["native_memory_hits"] == 0
    assert result["audit"]["native_memory_error_type"] == "TypeError"


def test_mcp_brain_resolve_roundtrip(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
                "params": {"name": BRAIN_RESOLVE_TOOL_NAME, "arguments": {"query": "index-advisor"}},
        },
        service,
    )

    candidates = response["result"]["structuredContent"]["candidates"]
    assert candidates[0]["brain_id"] == f"/project/{PROJECT}"


def test_mcp_brain_resolve_works_with_open_read_only_ledger(tmp_path: Path):
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
    service = KnowledgeSearchService(
        ledger=Ledger.open_read_only(ledger.path),
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        allow_private_results=True,
    )

    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": BRAIN_RESOLVE_TOOL_NAME, "arguments": {"query": PROJECT}},
        },
        service,
    )

    assert "error" not in response
    candidates = response["result"]["structuredContent"]["candidates"]
    assert candidates == [{"brain_id": f"/project/{PROJECT}", "kind": "project", "card_count": 1, "hint": ""}]


def test_mcp_brain_context_resolve_roundtrip_uses_core_without_retired_index_bridge(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": BRAIN_CONTEXT_RESOLVE_TOOL_NAME,
                "arguments": {
                    "repository": FIXTURE_REPOSITORY,
                    "branch": FIXTURE_BRANCH,
                    "current_request": "언어 선호 확인",
                    "current_files": ["docs/design.md"],
                    "project": PROJECT,
                },
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    assert result["memory_status"]["authority"] == "canonical_artifact_and_card"
    assert result["bridge_status"]["status"] == "disabled"
    assert result["graph_status"]["authority"] == "derived_index"
    # The MCP surface returns ContextPack.to_dict() directly, so the pack must
    # carry the same schema_version the CLI wraps it under: CLI/MCP versioning
    # symmetry.
    assert result["schema_version"] == CONTEXT_PACK_SCHEMA_VERSION
    assert json.loads(response["result"]["content"][0]["text"]) == result
    assert "/Users/" not in json.dumps(result, sort_keys=True)


def test_mcp_brain_context_resolve_carries_context_authority_block(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {
                "name": BRAIN_CONTEXT_RESOLVE_TOOL_NAME,
                "arguments": {
                    "repository": FIXTURE_REPOSITORY,
                    "branch": FIXTURE_BRANCH,
                    "current_request": "Context Authority Pack",
                    "current_files": ["specs/context-authority-roadmap/design.md"],
                    "project": PROJECT,
                },
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    authority = result["authority"]
    assert authority["schema_version"] == "context_authority_pack.v1"
    assert authority["preferences"][0]["rule"] == "한국어로 응답한다"
    assert authority["projection"]["neo4j"]["authority"] == "derived_authority_graph"
    assert authority["search_mirror"]["qdrant_docling"]["status"] == "unverified"
    assert "agents_use_brain_context_resolve" in authority["boundary_guardrails"]


def test_mcp_brain_context_resolve_reports_configured_unverified_search_mirror(tmp_path: Path):
    service = KnowledgeSearchService(
        ledger=_ledger(tmp_path),
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        mirror_search=lambda query, brain_id: [],
    )
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 15,
            "method": "tools/call",
            "params": {
                "name": BRAIN_CONTEXT_RESOLVE_TOOL_NAME,
                "arguments": {
                    "repository": FIXTURE_REPOSITORY,
                    "branch": FIXTURE_BRANCH,
                    "current_request": "Context Authority Pack",
                    "current_files": [],
                    "project": PROJECT,
                },
            },
        },
        service,
    )

    mirror = response["result"]["structuredContent"]["authority"]["search_mirror"]["qdrant_docling"]
    assert mirror["status"] == "configured_unverified"
    assert mirror["evidence_ref"] == "service:mirror_search_configured"
    assert mirror["requires_document_authority_join"] is True


def test_mcp_brain_context_resolve_can_emit_compact_response(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": BRAIN_CONTEXT_RESOLVE_TOOL_NAME,
                "arguments": {
                    "repository": FIXTURE_REPOSITORY,
                    "branch": FIXTURE_BRANCH,
                    "current_request": "compact context for agent",
                    "current_files": [],
                    "project": PROJECT,
                    "response_mode": "compact",
                },
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    assert result["response_mode"] == "compact"
    assert result["schema_version"] == CONTEXT_PACK_SCHEMA_VERSION
    assert "graph_status" in result
    assert "bridge_status" in result
    assert "authority" in result
    assert "relevant_decisions" not in result


def test_session_card_cache_collapses_repeated_accepted_card_reads(tmp_path: Path):
    # Repeated brain tool calls in one session must reuse a single accepted-card
    # snapshot per (project, limit) instead of reloading the ledger every call.
    service = _service(tmp_path)
    real_read_model = service._brain_card_cache._read_model
    counter = {"calls": 0}

    class _CountingReadModel:
        def list_accepted_cards(self, *, project, limit):
            counter["calls"] += 1
            return real_read_model.list_accepted_cards(project=project, limit=limit)

        def __getattr__(self, name):
            return getattr(real_read_model, name)

    service._brain_card_cache._read_model = _CountingReadModel()

    for _ in range(3):
        service.core_brain(project=PROJECT).brain_context_resolve(
            repository=PROJECT,
            branch=FIXTURE_BRANCH,
            current_files=[],
            current_request="언어 선호 확인",
            project=PROJECT,
        )

    # Three tool calls, one underlying accepted-card read for the shared snapshot.
    assert counter["calls"] == 1

    # The explicit refresh seam re-reads on the next call.
    service.invalidate_brain_card_cache()
    service.core_brain(project=PROJECT).brain_context_resolve(
        repository=PROJECT,
        branch=FIXTURE_BRANCH,
        current_files=[],
        current_request="언어 선호 확인",
        project=PROJECT,
    )
    assert counter["calls"] == 2


def test_session_card_cache_returns_independent_copies(tmp_path: Path):
    # The cache must hand out copies so a caller mutating its card list cannot
    # corrupt the shared session snapshot.
    service = _service(tmp_path)
    first = service._brain_card_cache.list_accepted_cards(project=PROJECT, limit=8)
    if first:
        first[0]["summary"] = "MUTATED"
    second = service._brain_card_cache.list_accepted_cards(project=PROJECT, limit=8)
    if second:
        assert second[0]["summary"] != "MUTATED"


def test_session_card_cache_isolates_nested_mutation_via_deepcopy(tmp_path: Path):
    # A shallow `dict(card)` copy still shares nested dict/list objects, so a
    # consumer mutating card["evidence_refs"][0] or card["meta"]["x"] would
    # corrupt the shared snapshot. deepcopy must isolate the nested structures.
    service = _service(tmp_path)

    snapshot_card = {
        "memory_id": "mem_nested",
        "card_type": "preference",
        "summary": "nested-card",
        "meta": {"nested": {"flag": "original"}},
        "evidence_refs": [{"knowledge_id": "kn", "content_hash": "sha256:c"}],
        "tags": ["original-tag"],
    }

    class _NestedReadModel:
        def list_accepted_cards(self, *, project, limit):
            # Return the SAME backing objects every call to model a real cache
            # snapshot; deepcopy on hand-out is what must protect them.
            return [snapshot_card]

        def __getattr__(self, name):
            return getattr(service._brain_card_cache._read_model, name)

    service._brain_card_cache._read_model = _NestedReadModel()
    service.invalidate_brain_card_cache()

    handed_out = service._brain_card_cache.list_accepted_cards(project=PROJECT, limit=8)
    assert handed_out, "expected the nested card to be handed out"

    # Mutate every nested level of the returned card.
    handed_out[0]["meta"]["nested"]["flag"] = "MUTATED"
    handed_out[0]["evidence_refs"][0]["content_hash"] = "sha256:MUTATED"
    handed_out[0]["evidence_refs"].append({"knowledge_id": "injected"})
    handed_out[0]["tags"].append("injected-tag")

    # The cached snapshot's backing object stays clean: deepcopy isolated it.
    assert snapshot_card["meta"]["nested"]["flag"] == "original"
    assert snapshot_card["evidence_refs"][0]["content_hash"] == "sha256:c"
    assert len(snapshot_card["evidence_refs"]) == 1
    assert snapshot_card["tags"] == ["original-tag"]

    # A second hand-out is also pristine (independent of the mutated copy).
    again = service._brain_card_cache.list_accepted_cards(project=PROJECT, limit=8)
    assert again[0]["meta"]["nested"]["flag"] == "original"
    assert again[0]["evidence_refs"] == [{"knowledge_id": "kn", "content_hash": "sha256:c"}]
    assert again[0]["tags"] == ["original-tag"]


def test_mcp_brain_context_resolve_reads_configured_graph_adapter(tmp_path: Path):
    graph = FakeGraphMemoryAdapter(
        [
            _episode(
                "Task",
                "task:graph-agent",
                {
                    "brain_id": f"/project/{PROJECT}",
                    "task_state": "Serve ContextPack through Brain MCP graph adapter",
                    "next_action": "Run Codex and Claude Code MCP E2E",
                },
            )
        ]
    )
    service = KnowledgeSearchService(
        ledger=_ledger(tmp_path),
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        graph_adapter=graph,
    )
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": BRAIN_CONTEXT_RESOLVE_TOOL_NAME,
                "arguments": {
                    "repository": FIXTURE_REPOSITORY,
                    "branch": FIXTURE_BRANCH,
                    "current_request": "Brain MCP graph adapter",
                    "current_files": ["worker/lib/agent_knowledge/mcp_server.py"],
                    "project": PROJECT,
                },
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    assert result["current_task"] == "Serve ContextPack through Brain MCP graph adapter"
    assert result["last_stopped_at"] == "Run Codex and Claude Code MCP E2E"
    assert result["graph_status"]["status"] == "available"
    assert "graph_unavailable" not in result["gaps"]


def test_mcp_brain_context_resolve_derives_project_when_omitted(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": BRAIN_CONTEXT_RESOLVE_TOOL_NAME,
                "arguments": {
                    "repository": FIXTURE_REPOSITORY,
                    "branch": FIXTURE_BRANCH,
                    "current_request": "언어 선호 확인",
                    "current_files": ["docs/design.md"],
                },
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    assert result["brain_id"] == f"/project/{PROJECT}"
    assert result["persona_constraints"][0]["preference"] == "한국어로 응답한다"


def test_mcp_brain_context_resolve_includes_configured_read_only_bridge(tmp_path: Path):
    service = KnowledgeSearchService(
        ledger=_ledger(tmp_path),
        retired_index_bridge=_FakeBridgeRetiredIndexBridge(),
        dataset_ids=["ds_docs"],
        allow_private_results=True,
    )
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": BRAIN_CONTEXT_RESOLVE_TOOL_NAME,
                "arguments": {
                    "repository": FIXTURE_REPOSITORY,
                    "branch": FIXTURE_BRANCH,
                    "current_request": "RetiredIndexBridge bridge citation",
                    "current_files": [],
                },
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    assert result["bridge_status"]["status"] == "available"
    assert result["bridge_evidence"][0]["authority"] == "external_document_bridge"
    assert result["bridge_evidence"][0]["title"] == "Bridge citation"


def test_mcp_brain_memory_search_derives_project_from_repository(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": BRAIN_MEMORY_SEARCH_TOOL_NAME,
                "arguments": {
                    "repository": FIXTURE_REPOSITORY,
                    "query": "한국어 응답",
                },
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    assert result["memory_status"]["count"] == 1
    assert result["results"][0]["summary"] == "한국어로 응답한다"


def test_mcp_brain_memory_search_normalizes_git_repository_project(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {
                "name": BRAIN_MEMORY_SEARCH_TOOL_NAME,
                "arguments": {
                    "repository": "https://github.com/pureliture/workspace-index-advisor.git",
                    "query": "한국어 응답",
                },
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    assert result["memory_status"]["count"] == 1
    assert result["results"][0]["summary"] == "한국어로 응답한다"


def test_brain_read_paths_do_not_leak_steward_proposals_to_hermes(tmp_path: Path):
    # Hermes 가 남긴 steward proposal(candidate)은 authoritative read 경로로 새지 않는다.
    from agent_knowledge.session_memory.brain_steward import BrainStewardService

    service = _service(tmp_path)
    BrainStewardService(service.ledger).candidate_create(
        source_span=_source_span(content_hash="sha256:steward-leak-probe"),
        proposer="hermes",
    )

    search = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 91,
            "method": "tools/call",
            "params": {
                "name": BRAIN_MEMORY_SEARCH_TOOL_NAME,
                "arguments": {"repository": FIXTURE_REPOSITORY, "query": "한국어 응답"},
            },
        },
        service,
    )["result"]["structuredContent"]
    # accepted card 1개만 보이고 proposal 은 결과/직렬화에 등장하지 않는다.
    assert search["memory_status"]["count"] == 1
    assert "mem_steward_" not in json.dumps(search, ensure_ascii=False)

    context = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 92,
            "method": "tools/call",
            "params": {
                "name": BRAIN_CONTEXT_RESOLVE_TOOL_NAME,
                "arguments": {
                    "repository": FIXTURE_REPOSITORY,
                    "branch": "main",
                    "current_request": "한국어 응답 규칙",
                    "consumer": "hermes",
                },
            },
        },
        service,
    )["result"]["structuredContent"]
    assert "mem_steward_" not in json.dumps(context, ensure_ascii=False)


def test_mcp_knowledge_search_caps_limit_at_tool_layer(tmp_path: Path):
    pipeline = _RecordingReadPipeline()
    service = KnowledgeSearchService(
        ledger=_ledger(tmp_path),
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        read_pipeline=pipeline,
    )

    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": TOOL_NAME,
                "arguments": {"query": "hello", "limit": 100},
            },
        },
        service,
    )

    assert "error" not in response
    assert pipeline.seen_limits == [10]


def test_service_search_caps_public_limit_before_authorized_reader(tmp_path: Path):
    pipeline = _RecordingReadPipeline()
    service = KnowledgeSearchService(
        ledger=_ledger(tmp_path),
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        authorized_reader=pipeline,
    )

    result = service.search("hello", limit=100)

    assert result == {"results": []}
    assert pipeline.seen_limits == [10]


def test_private_call_tool_alias_stays_compatible(tmp_path: Path):
    service = _service(tmp_path)
    params = {"name": TOOL_NAME, "arguments": {"query": "hello"}}

    assert _call_tool(params, service) == dispatch_tool_call(params, service)


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (BRAIN_QUERY_TOOL_NAME, {"query": "언어 선호"}),
        (
            BRAIN_CONTEXT_RESOLVE_TOOL_NAME,
            {"repository": PROJECT, "branch": FIXTURE_BRANCH},
        ),
        (BRAIN_MEMORY_SEARCH_TOOL_NAME, {"query": "언어 선호"}),
        (BRAIN_MEMORY_SEARCH_TOOL_NAME, {"repository": PROJECT}),
        (BRAIN_INCIDENT_SEARCH_TOOL_NAME, {"repository": PROJECT}),
        (BRAIN_DRIFT_EXPLAIN_TOOL_NAME, {"repository": PROJECT}),
        (BRAIN_PERSONA_CHECK_TOOL_NAME, {"repository": PROJECT}),
        (BRAIN_EVIDENCE_GET_TOOL_NAME, {"source_ref_id": "src_neuron_mcp"}),
    ],
)
def test_mcp_dispatch_rejects_missing_required_inputs(
    tmp_path: Path,
    tool_name: str,
    arguments: dict,
):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
        service,
    )

    assert response["error"]["code"] == -32602
    assert response["error"]["message"] == "invalid params: ValueError"


def test_service_search_returns_public_safe_provenance(tmp_path: Path):
    pipeline = _StaticReadPipeline(
        [
            MemorySearchResultItem(
                knowledge_id="kn_public",
                result_type="project_memory",
                title="Public memory",
                domain="memory",
                project=PROJECT,
                provider="codex",
                summary="safe summary",
                score=0.95,
                currentness="server_authorized",
                provenance=MemoryProvenance(
                    dataset="raw_dataset_id",
                    index_document_id="raw_doc_id",
                ),
            )
        ]
    )
    service = KnowledgeSearchService(
        ledger=_ledger(tmp_path),
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        authorized_reader=pipeline,
    )

    result = service.search("hello")

    assert result["results"][0]["provenance"] == {
        "authority": "ledger_authorized",
        "citation_ref": "kn_public",
    }
    serialized = json.dumps(result, sort_keys=True)
    assert "raw_dataset_id" not in serialized
    assert "raw_doc_id" not in serialized


def test_brain_query_index_fallback_omits_raw_document_ids_and_content(tmp_path: Path):
    service = KnowledgeSearchService(
        ledger=_ledger(tmp_path),
        retired_index_bridge=_RawFallbackRetiredIndexBridge(),
        dataset_ids=["ds_memory"],
    )

    result = service._brain_query_index_search("fallback", f"/project/{PROJECT}")

    assert result[0]["memory_id"] == ""
    assert result[0]["summary"] == ""
    serialized = json.dumps(result, sort_keys=True)
    assert "raw_doc_id" not in serialized
    assert "private raw content" not in serialized


def test_memory_read_pipeline_respects_query_limit_above_tool_cap():
    pipeline = MemoryReadPipeline(
        ledger=_AuthorizingLedger(),
        retired_index_bridge=_ManyDocsRetiredIndexBridge(count=12),
        dataset_ids=["ds_memory"],
    )

    response = pipeline.read(MemorySearchQuery(query="reuse pipeline", limit=12))

    assert len(response.results) == 12
    assert response.results[-1].knowledge_id == "kn_doc_11"


def test_memory_read_pipeline_normalizes_limit_before_retrieve():
    pipeline = MemoryReadPipeline(
        ledger=_AuthorizingLedger(),
        retired_index_bridge=_ManyDocsRetiredIndexBridge(count=3, expected_limit=1),
        dataset_ids=["ds_memory"],
    )

    response = pipeline.read(MemorySearchQuery(query="reuse pipeline", limit=0))

    assert len(response.results) == 1


def test_mcp_brain_persona_check_roundtrip(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": BRAIN_PERSONA_CHECK_TOOL_NAME,
                "arguments": {"plan": "한국어 응답 정책을 유지한다", "project": PROJECT},
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    assert result["status"] == "aligned"
    assert result["facts"][0]["preference"] == "한국어로 응답한다"


def test_mcp_brain_persona_check_uses_all_cards_when_project_omitted(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": BRAIN_PERSONA_CHECK_TOOL_NAME,
                "arguments": {"plan": "한국어 응답 정책을 유지한다"},
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    assert result["status"] == "aligned"
    assert result["facts"][0]["preference"] == "한국어로 응답한다"


def test_mcp_brain_evidence_get_roundtrip_respects_source_ref_policy(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": BRAIN_EVIDENCE_GET_TOOL_NAME,
                "arguments": {
                    "source_ref_id": "src_neuron_mcp",
                    "requesting_device_id_hash": _h("device-a"),
                },
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    assert result["resolution_state"] == "derived_only"
    assert result["reason_code"] == "policy_derived_only"
    assert result["content"] == "MCP SourceRef policy evidence is available."


def test_mcp_stdio_cli_serves_tools_list_without_index_token(tmp_path: Path, monkeypatch, capsys):
    ledger = _ledger(tmp_path)
    request = {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request) + "\n"))

    assert main(["mcp-stdio", "--ledger", str(ledger.path), "--dataset-id", "ds"]) == 0

    response = json.loads(capsys.readouterr().out)
    assert response["id"] == 3
    names = [tool["name"] for tool in response["result"]["tools"]]
    assert TOOL_NAME in names
    assert BRAIN_QUERY_TOOL_NAME in names
    assert BRAIN_RESOLVE_TOOL_NAME in names
    assert BRAIN_CONTEXT_RESOLVE_TOOL_NAME in names


def test_mcp_stdio_cli_accepts_proposal_only_steward_write_flag(monkeypatch):
    from agent_knowledge import cli as cli_mod

    captured = {}

    def _build_service(args):
        captured["allow_steward_proposals"] = args.allow_steward_proposals
        return object()

    def _run_stdio_server(_service):
        captured["served"] = True

    monkeypatch.setattr(cli_mod, "_build_recall_service", _build_service)
    monkeypatch.setattr(cli_mod, "run_stdio_server", _run_stdio_server)

    rc = cli_mod._mcp_stdio_main(
        [
            "--ledger",
            "/tmp/placeholder.sqlite",
            "--allow-steward-proposals",
        ]
    )

    assert rc == 0
    assert captured == {"allow_steward_proposals": True, "served": True}


def test_mcp_stdio_graph_initialization_error_does_not_print_raw_details(tmp_path: Path, monkeypatch, capsys):
    ledger = _ledger(tmp_path)
    monkeypatch.setattr(
        "agent_knowledge.cli.build_graph_adapter_from_env",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("/Users/example/private TOKEN=secret")),
    )

    rc = main(["mcp-stdio", "--ledger", str(ledger.path), "--enable-graph", "--graph-required"])
    output = capsys.readouterr()

    assert rc == 1
    assert "graph adapter unavailable: RuntimeError" in output.err
    assert "/Users/" not in output.err
    assert "TOKEN" not in output.err


def test_jsonrpc_value_error_does_not_leak_raw_exception_message(tmp_path: Path, monkeypatch):
    # A handler-level ValueError/TypeError must surface only a static message
    # plus the exception type name, never the raw str(exc) which can carry a
    # private path or token. Symmetric with the PR#11 brain_context_resolve fix.
    service = _service(tmp_path)
    leaky = "/Users/example/private/ledger.sqlite TOKEN=secret-bearer"
    monkeypatch.setattr(
        service,
        "brain_resolve",
        lambda **kwargs: (_ for _ in ()).throw(ValueError(leaky)),
    )

    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": BRAIN_RESOLVE_TOOL_NAME, "arguments": {"query": "x"}},
        },
        service,
    )

    error = response["error"]
    assert error["code"] == -32602
    assert error["message"] == "invalid params: ValueError"
    serialized = json.dumps(response, sort_keys=True)
    assert "/Users/" not in serialized
    assert "TOKEN" not in serialized
    assert "secret-bearer" not in serialized


def test_mcp_stdio_ledger_open_error_does_not_leak_raw_path(tmp_path: Path, monkeypatch, capsys):
    # Ledger.open_read_only raises ValueError embedding the ledger path; the CLI
    # must print only a static message + exception type, not the raw path.
    leaky = "/Users/example/private/missing-ledger.sqlite does not exist"
    monkeypatch.setattr(
        "agent_knowledge.cli.Ledger.open_read_only",
        classmethod(lambda cls, path: (_ for _ in ()).throw(ValueError(leaky))),
    )

    rc = main(["mcp-stdio", "--ledger", "/Users/example/private/missing-ledger.sqlite"])
    output = capsys.readouterr()

    assert rc == 2
    assert "ledger open failed: ValueError" in output.err
    assert "/Users/" not in output.err
    assert "missing-ledger" not in output.err


@pytest.mark.parametrize("agent_name", ["codex", "claude-code", "hermes"])
def test_mcp_stdio_cli_serves_contextpack_for_codex_claude_code_and_hermes_agents(
    tmp_path: Path,
    monkeypatch,
    capsys,
    agent_name: str,
):
    ledger = _ledger(tmp_path)
    graph = FakeGraphMemoryAdapter(
        [
            _episode(
                "Task",
                f"task:{agent_name}",
                {
                    "brain_id": f"/project/{PROJECT}",
                    "task_state": f"{agent_name} agent reads Brain MCP ContextPack",
                    "next_action": "Use stdio command in agent MCP config",
                },
            )
        ]
    )
    request_lines = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": agent_name}},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": BRAIN_CONTEXT_RESOLVE_TOOL_NAME,
                "arguments": {
                    "repository": FIXTURE_REPOSITORY,
                    "branch": FIXTURE_BRANCH,
                    "current_request": f"{agent_name} Brain MCP ContextPack",
                    "current_files": [],
                    "project": PROJECT,
                    "consumer": agent_name,
                },
            },
        },
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(json.dumps(item) for item in request_lines) + "\n"))
    monkeypatch.setattr("agent_knowledge.cli.build_graph_adapter_from_env", lambda **kwargs: graph)

    assert main(["mcp-stdio", "--ledger", str(ledger.path), "--enable-graph"]) == 0

    responses = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert responses[0]["result"]["serverInfo"]["name"] == "neurons"
    result = responses[1]["result"]["structuredContent"]
    assert result["current_task"] == f"{agent_name} agent reads Brain MCP ContextPack"
    assert result["graph_status"]["status"] == "available"
    assert result["authority"]["consumer_contract"] == {
        "consumer": agent_name,
        "read_only": True,
        "mutation_allowed": False,
        "default_agent_api": "brain_context_resolve",
    }


def _h(value):
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _episode(entity_type: str, natural_id: str, payload: dict) -> OntologyEpisode:
    return OntologyEpisode.from_payload(
        event_id=f"evt_{natural_id.replace(':', '_')}",
        entity_type=entity_type,
        natural_id=natural_id,
        payload=payload,
        observed_at="2026-06-19T00:00:00+00:00",
        reference_time="2026-06-19T00:00:00+00:00",
    )


class _FakeBridgeRetiredIndexBridge:
    def retrieve(self, query, dataset_ids, filters=None, limit=10):
        assert "RetiredIndexBridge bridge" in query
        assert dataset_ids == ["ds_docs"]
        assert filters == {"project": PROJECT}
        return [
            {
                "title": "Bridge citation",
                "summary": "RetiredIndexBridge bridge remains read only.",
                "score": 0.91,
                "source_ref_id": "src_bridge_citation",
            }
        ][:limit]


class _RecordingReadPipeline:
    def __init__(self):
        self.seen_limits = []

    def read(self, query: MemorySearchQuery) -> MemorySearchResponse:
        self.seen_limits.append(query.limit)
        return MemorySearchResponse(results=[])


class _StaticReadPipeline:
    def __init__(self, results: list[MemorySearchResultItem]):
        self.results = results

    def read(self, query: MemorySearchQuery) -> MemorySearchResponse:
        return MemorySearchResponse(results=self.results[: query.limit])


class _ManyDocsRetiredIndexBridge:
    def __init__(self, *, count: int, expected_limit: int = 12):
        self.count = count
        self.expected_limit = expected_limit

    def retrieve(self, query, dataset_ids, filters=None, limit=10):
        assert query == "reuse pipeline"
        assert dataset_ids == ["ds_memory"]
        assert limit == self.expected_limit
        return [
            {
                "document_id": f"doc_{index}",
                "kb_id": "ds_memory",
                "score": 1.0 - (index / 100),
            }
            for index in range(self.count)
        ]


class _RawFallbackRetiredIndexBridge:
    def retrieve(self, query, dataset_ids, filters=None, limit=10):
        assert query == "fallback"
        assert dataset_ids == ["ds_memory"]
        assert filters == {"project": PROJECT}
        return [
            {
                "document_id": "raw_doc_id",
                "doc_id": "raw_doc_alias",
                "content": "private raw content",
                "score": 0.5,
            }
        ]


class _AuthorizingLedger:
    def authorize_document(self, document_id, *, filters, include_private):
        assert filters == {}
        assert include_private is False
        return {
            "knowledge_id": f"kn_{document_id}",
            "type": "project_memory",
            "title": f"Memory {document_id}",
            "domain": "memory",
            "project": PROJECT,
            "provider": "codex",
            "summary": f"Summary {document_id}",
            "index_target_id": "ds_memory",
            "index_document_id": document_id,
        }
