from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from agent_knowledge.cli import main
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
from agent_knowledge.llm_brain_core.ledger_adapter import LedgerSourceRefCatalog
from agent_knowledge.llm_brain_core.graph import FakeGraphMemoryAdapter
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
        BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME,
        BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME,
        BRAIN_REVIEW_PROPOSALS_TOOL_NAME,
    ]:
        assert tool_name in tools

    assert tools[BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME]["inputSchema"]["properties"]["ledger_scope"]["enum"] == [
        "local_test",
        "production",
    ]


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
    assert denied["proposal_write_performed"] is False
    assert denied["authoritative_memory_changed"] is False


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
    assert result["authority_write_performed"] is False
    assert result["authoritative_memory_changed"] is False


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
