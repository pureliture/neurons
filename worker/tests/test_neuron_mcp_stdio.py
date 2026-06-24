from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from agent_knowledge.cli import main
from agent_knowledge.curation import CurationService
from agent_knowledge.ledger import Ledger
from agent_knowledge.mcp_server import (
    BRAIN_QUERY_TOOL_NAME,
    BRAIN_RESOLVE_TOOL_NAME,
    BRAIN_CONTEXT_RESOLVE_TOOL_NAME,
    BRAIN_DRIFT_EXPLAIN_TOOL_NAME,
    BRAIN_EVIDENCE_GET_TOOL_NAME,
    BRAIN_INCIDENT_SEARCH_TOOL_NAME,
    BRAIN_MEMORY_SEARCH_TOOL_NAME,
    BRAIN_PERSONA_CHECK_TOOL_NAME,
    BRAIN_PERSONA_GET_TOOL_NAME,
    TOOL_NAME,
    DisabledRagflowClient,
    KnowledgeSearchService,
    MemoryReadPipeline,
    MemorySearchQuery,
    MemorySearchResponse,
    _call_tool,
    dispatch_tool_call,
    handle_jsonrpc_message,
    list_tools,
)
from agent_knowledge.memory_card import build_memory_candidate
from agent_knowledge.memory_miner import build_memory_card_candidate_from_source_span
from agent_knowledge.llm_brain_core.ledger_adapter import LedgerSourceRefCatalog
from agent_knowledge.llm_brain_core.graph import FakeGraphMemoryAdapter
from agent_knowledge.llm_brain_core.models import CONTEXT_PACK_SCHEMA_VERSION, OntologyEpisode
from agent_knowledge.llm_brain_core.runtime import source_ref_from_catalog_event
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
        ragflow=DisabledRagflowClient(),
        dataset_ids=[],
        allow_private_results=True,
    )


def test_mcp_tool_list_exposes_neuron_owned_tools():
    names = [tool["name"] for tool in list_tools()]

    assert TOOL_NAME in names
    assert BRAIN_QUERY_TOOL_NAME in names
    assert BRAIN_RESOLVE_TOOL_NAME in names
    assert BRAIN_CONTEXT_RESOLVE_TOOL_NAME in names
    assert BRAIN_PERSONA_CHECK_TOOL_NAME in names
    assert BRAIN_EVIDENCE_GET_TOOL_NAME in names


def test_brain_memory_search_schema_matches_repository_project_derivation():
    tools = {tool["name"]: tool for tool in list_tools()}
    schema = tools[BRAIN_MEMORY_SEARCH_TOOL_NAME]["inputSchema"]

    assert "repository" in schema["properties"]
    assert "project" in schema["properties"]
    assert schema["required"] == ["query"]


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


def test_mcp_brain_context_resolve_roundtrip_uses_core_without_ragflow(tmp_path: Path):
    service = _service(tmp_path)
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": BRAIN_CONTEXT_RESOLVE_TOOL_NAME,
                "arguments": {
                    "repository": "/Users/example/Projects/workspace-ragflow-advisor",
                    "branch": "codex/llm-brain-core-design",
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
            branch="codex/m14",
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
        branch="codex/m14",
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
        ragflow=DisabledRagflowClient(),
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
                    "repository": "/Users/example/Projects/workspace-ragflow-advisor",
                    "branch": "codex/m14",
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
                    "repository": "/Users/example/Projects/workspace-ragflow-advisor",
                    "branch": "codex/llm-brain-core-design",
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
        ragflow=_FakeBridgeRagflow(),
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
                    "repository": "/Users/example/Projects/workspace-ragflow-advisor",
                    "current_request": "RAGFlow bridge citation",
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
                    "repository": "/Users/example/Projects/workspace-ragflow-advisor",
                    "query": "한국어 응답",
                },
            },
        },
        service,
    )

    result = response["result"]["structuredContent"]
    assert result["memory_status"]["count"] == 1
    assert result["results"][0]["summary"] == "한국어로 응답한다"


def test_mcp_knowledge_search_caps_limit_at_tool_layer(tmp_path: Path):
    pipeline = _RecordingReadPipeline()
    service = KnowledgeSearchService(
        ledger=_ledger(tmp_path),
        ragflow=DisabledRagflowClient(),
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
        ragflow=DisabledRagflowClient(),
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


def test_memory_read_pipeline_respects_query_limit_above_tool_cap():
    pipeline = MemoryReadPipeline(
        ledger=_AuthorizingLedger(),
        ragflow=_ManyDocsRagflow(count=12),
        dataset_ids=["ds_memory"],
    )

    response = pipeline.read(MemorySearchQuery(query="reuse pipeline", limit=12))

    assert len(response.results) == 12
    assert response.results[-1].knowledge_id == "kn_doc_11"


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


def test_mcp_stdio_cli_serves_tools_list_without_ragflow_token(tmp_path: Path, monkeypatch, capsys):
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


@pytest.mark.parametrize("agent_name", ["codex", "claude-code"])
def test_mcp_stdio_cli_serves_contextpack_for_codex_and_claude_code_agents(
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
                    "repository": "/Users/example/Projects/workspace-ragflow-advisor",
                    "branch": "codex/m14",
                    "current_request": f"{agent_name} Brain MCP ContextPack",
                    "current_files": [],
                    "project": PROJECT,
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


class _FakeBridgeRagflow:
    def retrieve(self, query, dataset_ids, filters=None, limit=10):
        assert "RAGFlow bridge" in query
        assert dataset_ids == ["ds_docs"]
        assert filters == {"project": PROJECT}
        return [
            {
                "title": "Bridge citation",
                "summary": "RAGFlow bridge remains read only.",
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


class _ManyDocsRagflow:
    def __init__(self, *, count: int):
        self.count = count

    def retrieve(self, query, dataset_ids, filters=None, limit=10):
        assert query == "reuse pipeline"
        assert dataset_ids == ["ds_memory"]
        assert limit == 12
        return [
            {
                "document_id": f"doc_{index}",
                "kb_id": "ds_memory",
                "score": 1.0 - (index / 100),
            }
            for index in range(self.count)
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
            "ragflow_dataset_id": "ds_memory",
            "ragflow_document_id": document_id,
        }
