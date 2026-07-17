from __future__ import annotations

import hashlib
from datetime import datetime

import pytest

from agent_knowledge.llm_brain_core.context import BrainReadService
from agent_knowledge.llm_brain_core.temporal import TemporalSelectorError, parse_temporal_selector
from agent_knowledge.llm_brain_core._util import hash_payload
from agent_knowledge.session_memory.brain_query import build_temporal_brain_query_response
from agent_knowledge.llm_brain_core import (
    FakeGraphMemoryAdapter,
    InMemorySessionMemoryArtifactStore,
    OntologyEpisode,
    SessionMemoryArtifact,
    episode_from_session_artifact,
)
from agent_knowledge.mcp_jsonrpc import handle_jsonrpc_message
from agent_knowledge.mcp_tools import (
    BRAIN_OBJECTS_QUERY_TOOL_NAME,
    BRAIN_QUERY_TOOL_NAME,
    list_tools,
)


class _RecordingObjectQueryService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def brain_objects_query(self, **kwargs) -> dict:
        self.calls.append(dict(kwargs))
        return {
            "schema_version": "brain_objects_query.v1",
            "route": "temporal_work_recall",
            "response_mode": "full",
            "object_pack": {"objects": [], "gaps": []},
        }


class _RecordingBrainQueryService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def brain_query(self, **kwargs) -> dict:
        self.calls.append(dict(kwargs))
        return {
            "schema_version": "brain.query.v1",
            "route": "temporal_work_recall",
            "results": [],
        }


def _mcp_object_query(service: object, **selector: str) -> dict:
    return handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECTS_QUERY_TOOL_NAME,
                "arguments": {
                    "repository": "neurons",
                    "branch": "main",
                    "query": "temporal work recall",
                    "route": "temporal_work_recall",
                    **selector,
                },
            },
        },
        service,
    )


def test_mcp_brain_objects_query_declares_temporal_selectors() -> None:
    tool = next(tool for tool in list_tools() if tool["name"] == BRAIN_OBJECTS_QUERY_TOOL_NAME)
    properties = tool["inputSchema"]["properties"]

    assert {"as_of", "date_from", "date_to"}.issubset(properties)
    assert properties["as_of"]["type"] == "string"
    assert properties["date_from"]["type"] == "string"
    assert properties["date_to"]["type"] == "string"
    assert "UTC" in properties["as_of"]["description"]


def test_bare_and_relative_dates_use_an_explicit_utc_calendar_contract() -> None:
    bare = parse_temporal_selector(as_of="2026-07-09")
    relative = parse_temporal_selector(
        query="오늘 작업",
        now=datetime.fromisoformat("2026-07-10T00:30:00+09:00"),
    )

    assert bare is not None
    assert bare.to_audit_dict() == {
        "start": "2026-07-09T00:00:00Z",
        "end": "2026-07-09T23:59:59.999999Z",
        "source": "as_of",
    }
    assert relative is not None
    assert relative.to_audit_dict()["start"] == "2026-07-09T00:00:00Z"
    assert relative.to_audit_dict()["end"] == "2026-07-09T23:59:59.999999Z"


@pytest.mark.parametrize(
    ("observed_at_start", "observed_at_end"),
    [
        ("2026-07-09T10:00:00Z", "not-a-time"),
        ("not-a-time", "2026-07-09T10:00:00Z"),
    ],
)
def test_selector_rejects_supplied_malformed_evidence_bound(
    observed_at_start: str, observed_at_end: str
) -> None:
    selector = parse_temporal_selector(as_of="2026-07-09T10:00:00Z")

    assert selector is not None
    assert selector.matches(
        observed_at_start=observed_at_start,
        observed_at_end=observed_at_end,
    ) is False


@pytest.mark.parametrize(
    "selector",
    [
        {"as_of": "2026-07-09T12:00:00Z"},
        {"date_from": "2026-07-09T00:00:00Z"},
        {"date_to": "2026-07-15T23:59:59Z"},
        {
            "date_from": "2026-07-09T00:00:00Z",
            "date_to": "2026-07-15T23:59:59Z",
        },
    ],
)
def test_mcp_brain_objects_query_forwards_temporal_selectors(selector: dict[str, str]) -> None:
    service = _RecordingObjectQueryService()

    response = _mcp_object_query(service, **selector)

    assert "error" not in response
    assert service.calls
    assert set(selector).issubset(service.calls[0])
    for field, value in selector.items():
        assert service.calls[0][field] == value


@pytest.mark.parametrize(
    "selector",
    [
        {"date_from": "2026-07-09T00:00:00Z"},
        {"date_to": "2026-07-15T23:59:59Z"},
    ],
)
def test_mcp_brain_query_forwards_open_ended_temporal_selector(
    selector: dict[str, str],
) -> None:
    service = _RecordingBrainQueryService()

    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": BRAIN_QUERY_TOOL_NAME,
                "arguments": {
                    "brain_id": "/project/neurons",
                    "query": "temporal migration",
                    **selector,
                },
            },
        },
        service,
    )

    assert "error" not in response
    assert service.calls
    for field, value in selector.items():
        assert service.calls[0][field] == value


@pytest.mark.parametrize(
    "selector",
    [
        {
            "as_of": "2026-07-09T10:00:00Z",
            "date_from": "2026-07-09T00:00:00Z",
        },
        {"as_of": "2026-07-09T10:00:00"},
        {"date_to": "2026-07-09T10:00:00"},
    ],
)
def test_mcp_brain_query_rejects_conflicting_or_offsetless_temporal_selector(
    selector: dict[str, str],
) -> None:
    service = _RecordingBrainQueryService()

    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": BRAIN_QUERY_TOOL_NAME,
                "arguments": {
                    "brain_id": "/project/neurons",
                    "query": "temporal migration",
                    **selector,
                },
            },
        },
        service,
    )

    assert response["error"]["code"] == -32602
    assert service.calls == []


@pytest.mark.parametrize(
    "selector",
    [
        {"as_of": "not-an-iso-date"},
        {
            "date_from": "2026-07-16T00:00:00Z",
            "date_to": "2026-07-15T23:59:59Z",
        },
    ],
)
def test_mcp_brain_objects_query_rejects_invalid_temporal_selectors(
    selector: dict[str, str],
) -> None:
    service = _RecordingObjectQueryService()

    response = _mcp_object_query(service, **selector)

    assert "error" in response
    assert response["error"]["code"] == -32602
    assert service.calls == []


def test_mcp_temporal_selector_rejects_a_conflicting_non_temporal_route() -> None:
    service = _RecordingObjectQueryService()

    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": BRAIN_OBJECTS_QUERY_TOOL_NAME,
                "arguments": {
                    "repository": "neurons",
                    "branch": "main",
                    "query": "deployment migration",
                    "route": "documentation_cleanup",
                    "as_of": "2026-07-15T10:30:00Z",
                },
            },
        },
        service,
    )

    assert response["error"]["code"] == -32602
    assert service.calls == []


def test_explicit_temporal_selector_routes_to_temporal_recall_when_route_is_omitted() -> None:
    service = BrainReadService(
        memory_cards=[
            _task_card(
                "mem_explicit_selector",
                "Deploy the temporal projection migration",
                observed_at_start="2026-07-15T10:00:00Z",
                observed_at_end="2026-07-15T11:00:00Z",
            )
        ]
    )

    result = service.brain_objects_query(
        repository="neurons",
        branch="main",
        query="deployment migration",
        current_files=[],
        as_of="2026-07-15T10:30:00Z",
    )

    assert result["route"] == "temporal_work_recall"
    assert _work_titles(result) == ["Deploy the temporal projection migration"]


def test_query_iso_date_routes_to_temporal_recall_when_route_is_omitted() -> None:
    service = BrainReadService(
        memory_cards=[
            _task_card(
                "mem_iso_date_a",
                "Deploy the date A temporal migration",
                observed_at_start="2026-07-09T10:00:00Z",
                observed_at_end="2026-07-09T11:00:00Z",
            ),
            _task_card(
                "mem_iso_date_b",
                "Deploy the date B temporal migration",
                observed_at_start="2026-07-15T10:00:00Z",
                observed_at_end="2026-07-15T11:00:00Z",
            ),
        ]
    )

    result = service.brain_objects_query(
        repository="neurons",
        branch="main",
        query="What did we deploy on 2026-07-09?",
        current_files=[],
    )

    assert result["route"] == "temporal_work_recall"
    assert _work_titles(result) == ["Deploy the date A temporal migration"]


def test_english_auxiliary_is_not_required_for_temporal_artifact_relevance() -> None:
    store = InMemorySessionMemoryArtifactStore(
        [
            _artifact(
                "english-auxiliary-artifact",
                "Deployment completed",
                observed_at_start="2026-07-15T00:00:00Z",
                observed_at_end="2026-07-15T23:59:59Z",
                materialized_at="2026-07-15T23:59:59Z",
                search_terms=("deploy",),
            )
        ]
    )

    result = BrainReadService(artifact_store=store).brain_objects_query(
        repository="neurons",
        branch="main",
        query="what was deployment on 2026-07-15",
        current_files=[],
    )

    assert result["route"] == "temporal_work_recall"
    assert _work_titles(result) == ["Deployment completed"]
    assert result["object_pack"]["gaps"] == []


def test_query_iso_date_rejects_an_explicit_non_temporal_route() -> None:
    service = BrainReadService(
        memory_cards=[
            _task_card(
                "mem_iso_date_explicit_route",
                "Deploy the explicit-route temporal migration",
                observed_at_start="2026-07-09T10:00:00Z",
                observed_at_end="2026-07-09T11:00:00Z",
            )
        ]
    )

    with pytest.raises(TemporalSelectorError, match="require route temporal_work_recall"):
        service.brain_objects_query(
            repository="neurons",
            branch="main",
            query="What did we deploy on 2026-07-09?",
            current_files=[],
            route="authority_archive_separation",
        )


def _task_card(
    memory_id: str,
    title: str,
    *,
    observed_at_start: str = "",
    observed_at_end: str = "",
) -> dict:
    materialized_at = observed_at_end or observed_at_start or "2026-07-16T00:00:00Z"
    return {
        "memory_id": memory_id,
        "card_type": "task",
        "project": "neurons",
        "provider": "codex",
        "title": title,
        "summary": title,
        "lifecycle_state": "accepted",
        "approval_state": "approved",
        "currentness": "current",
        "status": "accepted",
        "content_hash": "sha256:" + hashlib.sha256(memory_id.encode("utf-8")).hexdigest(),
        "observed_at_start": observed_at_start,
        "observed_at_end": observed_at_end,
        "materialized_at": materialized_at,
        "updated_at": materialized_at,
        "typed_payload": {
            "task_state": title,
            "next_action": f"Resume {title}",
            "blocker": "",
            "owner_hint": "neurons",
            "status": "open",
        },
    }


def _artifact(
    session_key: str,
    summary: str,
    *,
    observed_at_start: str,
    observed_at_end: str,
    materialized_at: str,
    search_terms: tuple[str, ...] = (),
    provider: str = "codex",
) -> SessionMemoryArtifact:
    session_hash = "sha256:" + hashlib.sha256(session_key.encode("utf-8")).hexdigest()
    source_revision = "sha256:" + hashlib.sha256(
        f"{session_key}:source".encode("utf-8")
    ).hexdigest()
    return SessionMemoryArtifact.from_summary(
        session_id_hash=session_hash,
        project="neurons",
        provider=provider,
        summary=summary,
        source_event_ids=[f"event-{session_key}"],
        source_revision=source_revision,
        observed_at_start=observed_at_start,
        observed_at_end=observed_at_end,
        revision_observed_at_start=observed_at_start,
        revision_observed_at_end=observed_at_end,
        revision_temporal_evidence="bounded",
        materialized_at=materialized_at,
        materialization_revision=1,
        created_at=materialized_at,
        search_term_hashes=[hash_payload(term.casefold()) for term in search_terms],
    )


def _temporal_query(service: BrainReadService, **selector: str) -> dict:
    return service.brain_objects_query(
        repository="neurons",
        branch="main",
        query="temporal work recall",
        current_files=[],
        route="temporal_work_recall",
        **selector,
    )


def _work_titles(result: dict) -> list[str]:
    return [
        obj["title"]
        for obj in result["object_pack"]["objects"]
        if obj["object_type"] == "WorkUnit"
    ]


def test_temporal_task_cards_preserve_current_and_historical_authority_lanes() -> None:
    current = _task_card(
        "mem_current",
        "Temporal migration current",
        observed_at_start="2026-07-15T10:00:00Z",
        observed_at_end="2026-07-15T11:00:00Z",
    )
    stale = _task_card(
        "mem_stale",
        "Temporal migration stale",
        observed_at_start="2026-07-15T10:00:00Z",
        observed_at_end="2026-07-15T11:00:00Z",
    )
    stale["currentness"] = "stale"
    completed = _task_card(
        "mem_completed",
        "Temporal migration completed",
        observed_at_start="2026-07-15T10:00:00Z",
        observed_at_end="2026-07-15T11:00:00Z",
    )
    completed["typed_payload"]["status"] = "completed"
    service = BrainReadService(memory_cards=[current, stale, completed])

    result = service.brain_objects_query(
        repository="neurons",
        branch="main",
        query="temporal migration",
        current_files=[],
        route="temporal_work_recall",
        as_of="2026-07-15T10:30:00Z",
    )
    pack = result["object_pack"]

    assert [obj["title"] for obj in pack["lanes"]["accepted_current"]] == [
        "Temporal migration current"
    ]
    assert {obj["title"] for obj in pack["lanes"]["accepted_non_current"]} == {
        "Temporal migration stale",
        "Temporal migration completed",
    }
    assert [action["action"] for action in pack["recommended_actions"]] == [
        "resume_work"
    ]


@pytest.mark.parametrize(
    ("currentness", "task_status"),
    [
        ("stale", "open"),
        ("superseded", "open"),
        ("conflicted", "open"),
        ("current", "closed"),
        ("current", "cancelled"),
    ],
)
def test_temporal_non_current_only_cards_are_historical_without_resume(
    currentness: str,
    task_status: str,
) -> None:
    card = _task_card(
        "mem_historical",
        "Temporal migration historical",
        observed_at_start="2026-07-15T10:00:00Z",
        observed_at_end="2026-07-15T11:00:00Z",
    )
    card["currentness"] = currentness
    card["typed_payload"]["status"] = task_status
    service = BrainReadService(memory_cards=[card])

    result = service.brain_objects_query(
        repository="neurons",
        branch="main",
        query="temporal migration historical",
        current_files=[],
        route="temporal_work_recall",
        as_of="2026-07-15T10:30:00Z",
    )
    pack = result["object_pack"]

    assert pack["lanes"]["accepted_current"] == []
    assert len(pack["lanes"]["accepted_non_current"]) == 1
    assert pack["recommended_actions"] == []
    assert "temporal_current_authority_missing" in pack["gaps"]
    assert pack["confidence"]["score"] < 0.9


@pytest.mark.parametrize(
    ("lifecycle", "approval", "currentness"),
    [
        ("candidate", "pending", "current"),
        ("rejected", "rejected", "current"),
        ("accepted", "approved", "unknown"),
    ],
)
def test_temporal_unaccepted_or_unknown_cards_fail_closed(
    lifecycle: str,
    approval: str,
    currentness: str,
) -> None:
    card = _task_card(
        "mem_untrusted",
        "Temporal migration untrusted",
        observed_at_start="2026-07-15T10:00:00Z",
        observed_at_end="2026-07-15T11:00:00Z",
    )
    card["lifecycle_state"] = lifecycle
    card["approval_state"] = approval
    card["currentness"] = currentness
    service = BrainReadService(memory_cards=[card])

    result = service.brain_objects_query(
        repository="neurons",
        branch="main",
        query="temporal migration untrusted",
        current_files=[],
        route="temporal_work_recall",
        as_of="2026-07-15T10:30:00Z",
    )

    assert result["object_pack"]["objects"] == []
    assert result["object_pack"]["confidence"]["score"] == 0.0
    assert result["object_pack"]["gaps"]


def test_temporal_recall_as_of_returns_only_the_work_observed_on_that_date() -> None:
    service = BrainReadService(
        memory_cards=[
            _task_card(
                "mem_date_a",
                "Work observed on date A",
                observed_at_start="2026-07-09T00:00:00Z",
                observed_at_end="2026-07-09T23:59:59Z",
            ),
            _task_card(
                "mem_date_b",
                "Work observed on date B",
                observed_at_start="2026-07-15T00:00:00Z",
                observed_at_end="2026-07-15T23:59:59Z",
            ),
        ]
    )

    date_a = _temporal_query(service, as_of="2026-07-09T12:00:00Z")
    date_b = _temporal_query(service, as_of="2026-07-15T12:00:00Z")

    assert _work_titles(date_a) == ["Work observed on date A"]
    assert _work_titles(date_b) == ["Work observed on date B"]


def test_temporal_recall_finds_matching_artifact_beyond_recent_one_hundred() -> None:
    store = InMemorySessionMemoryArtifactStore()
    store.upsert(
        _artifact(
            "historical-target",
            "Historic temporal migration",
            observed_at_start="2026-07-09T00:00:00Z",
            observed_at_end="2026-07-09T23:59:59Z",
            materialized_at="2026-07-09T23:59:59Z",
        )
    )
    for index in range(126):
        store.upsert(
            _artifact(
                f"newer-{index}",
                f"Newer unrelated artifact {index}",
                observed_at_start="2026-07-15T00:00:00Z",
                observed_at_end="2026-07-15T23:59:59Z",
                materialized_at="2026-07-15T23:59:59Z",
            )
        )

    result = BrainReadService(artifact_store=store).brain_objects_query(
        repository="neurons",
        branch="main",
        query="historic temporal migration",
        current_files=[],
        route="temporal_work_recall",
        as_of="2026-07-09T12:00:00Z",
    )

    assert _work_titles(result) == ["Historic temporal migration"]


def test_temporal_recall_matches_private_artifact_subject_by_term_fingerprint_only() -> None:
    store = InMemorySessionMemoryArtifactStore(
        [
            _artifact(
                "fingerprinted-target",
                "Session artifact for codex/neurons. conversation_chunks=3.",
                observed_at_start="2026-07-15T10:00:00Z",
                observed_at_end="2026-07-15T11:00:00Z",
                materialized_at="2026-07-15T11:01:00Z",
                search_terms=("deploy", "migration"),
            ),
            _artifact(
                "fingerprinted-unrelated",
                "Session artifact for codex/neurons. conversation_chunks=2.",
                observed_at_start="2026-07-15T10:00:00Z",
                observed_at_end="2026-07-15T11:00:00Z",
                materialized_at="2026-07-15T11:01:00Z",
                search_terms=("profile", "screen"),
            ),
            _artifact(
                "fingerprinted-partial-overlap",
                "Session artifact for codex/neurons. conversation_chunks=4.",
                observed_at_start="2026-07-15T10:00:00Z",
                observed_at_end="2026-07-15T11:00:00Z",
                materialized_at="2026-07-15T11:01:00Z",
                search_terms=("deploy", "profile"),
            ),
        ]
    )

    result = BrainReadService(artifact_store=store).brain_objects_query(
        repository="neurons",
        branch="main",
        query="deployment migration",
        current_files=[],
        route="temporal_work_recall",
        as_of="2026-07-15T10:30:00Z",
    )

    assert _work_titles(result) == [
        "Session artifact for codex/neurons. conversation_chunks=3."
    ]
    serialized = repr(result)
    assert "fingerprinted-target" not in serialized


def test_artifact_only_temporal_match_has_evidence_backed_confidence() -> None:
    store = InMemorySessionMemoryArtifactStore(
        [
            _artifact(
                "artifact-only-confidence",
                "Session artifact for codex/neurons. conversation_chunks=3.",
                observed_at_start="2026-07-15T10:00:00Z",
                observed_at_end="2026-07-15T11:00:00Z",
                materialized_at="2026-07-15T11:01:00Z",
                search_terms=("temporal", "migration"),
            )
        ]
    )

    result = BrainReadService(artifact_store=store).brain_objects_query(
        repository="neurons",
        branch="main",
        query="temporal migration",
        current_files=[],
        route="temporal_work_recall",
        as_of="2026-07-15T10:30:00Z",
    )

    pack = result["object_pack"]
    assert len(pack["objects"]) == 1
    assert pack["objects"][0]["confidence"]["score"] > 0.0
    assert pack["confidence"]["score"] > 0.0
    assert pack["gaps"] == []


def test_synthetic_canary_artifact_is_excluded_from_normal_context_and_temporal_recall() -> None:
    normal_summary = "Normal retention migration work"
    canary_summary = "Synthetic canary temporal migration"
    store = InMemorySessionMemoryArtifactStore(
        [
            _artifact(
                "normal-session",
                normal_summary,
                observed_at_start="2026-07-15T10:00:00Z",
                observed_at_end="2026-07-15T11:00:00Z",
                materialized_at="2026-07-15T11:01:00Z",
                search_terms=("retention", "migration"),
            ),
            _artifact(
                "canary-session",
                canary_summary,
                observed_at_start="2026-07-15T10:00:00Z",
                observed_at_end="2026-07-15T11:00:00Z",
                materialized_at="2026-07-15T11:02:00Z",
                search_terms=("synthetic", "canary", "temporal", "migration"),
                provider="lbrain-temporal-canary",
            ),
        ]
    )
    service = BrainReadService(artifact_store=store)

    context = service.brain_context_resolve(
        repository="neurons",
        branch="main",
        current_files=[],
        current_request="resume current work",
        project="neurons",
    ).to_dict()
    temporal = service.brain_objects_query(
        repository="neurons",
        branch="main",
        query="synthetic canary temporal migration",
        current_files=[],
        route="temporal_work_recall",
        as_of="2026-07-15T10:30:00Z",
    )

    assert context["current_task"] == normal_summary
    assert context["memory_status"]["artifact_count"] == 1
    assert canary_summary not in repr(context)
    assert _work_titles(temporal) == []
    assert temporal["object_pack"]["gaps"]
    assert canary_summary not in repr(temporal)


def test_synthetic_canary_graph_episode_is_excluded_from_memory_search() -> None:
    canary = _artifact(
        "canary-graph-session",
        "Synthetic canary graph projection",
        observed_at_start="2026-07-15T10:00:00Z",
        observed_at_end="2026-07-15T11:00:00Z",
        materialized_at="2026-07-15T11:02:00Z",
        search_terms=("synthetic", "canary", "graph"),
        provider="lbrain-temporal-canary",
    )
    canary_task = OntologyEpisode.from_payload(
        event_id="canary-graph-task-event",
        entity_type="Task",
        natural_id="canary-graph-task",
        payload={
            "brain_id": "/project/neurons",
            "project": "neurons",
            "provider": "lbrain-temporal-canary",
            "task_state": "Synthetic canary graph task",
        },
        observed_at="2026-07-15T11:02:00Z",
    )
    service = BrainReadService(
        graph_adapter=FakeGraphMemoryAdapter(
            [episode_from_session_artifact(canary), canary_task]
        )
    )

    result = service.brain_memory_search(
        project="neurons",
        query="synthetic canary graph",
    )

    assert result["graph_results"] == []

    context = service.brain_context_resolve(
        repository="neurons",
        branch="main",
        current_files=[],
        current_request="synthetic canary graph task",
        project="neurons",
    ).to_dict()

    assert context["current_task"] == ""
    assert context["authority"]["object_packs"]["current_work"]["objects"] == []


def test_temporal_brain_query_keeps_artifact_evidence_out_of_memory_card_lanes() -> None:
    object_id = "ko:WorkUnit:artifact-only-temporal-evidence"
    response = build_temporal_brain_query_response(
        brain_id="/project/neurons",
        temporal_response={
            "object_pack": {
                "objects": [
                    {
                        "object_id": object_id,
                        "title": "Artifact-only temporal evidence",
                        "summary": "Artifact-only temporal evidence",
                        "observed_at": "2026-07-15T10:00:00Z",
                        "authority_lane": "reference_only",
                        "lifecycle_status": "observed",
                        "confidence": {"score": 0.7},
                        "payload": {
                            "source_kind": "session_memory_artifact",
                            "source_object_type": "SessionMemoryArtifact",
                            "observed_at_start": "2026-07-15T10:00:00Z",
                            "observed_at_end": "2026-07-15T11:00:00Z",
                        },
                    }
                ],
                "gaps": [],
                "audit": {
                    "temporal_selector": {
                        "start": "2026-07-15T10:00:00Z",
                        "end": "2026-07-15T11:00:00Z",
                        "source": "as_of",
                    }
                },
            }
        },
    )

    assert response["current"] == []
    assert response["accepted"] == []
    assert response["results"] == [
        {
            "brain_id": "/project/neurons",
            "result_type": "temporal_artifact_evidence",
            "summary": "Artifact-only temporal evidence",
            "why_retrieved": "matching_observed_event_time",
            "source_ref": object_id,
            "object_id": object_id,
            "observed_at": "2026-07-15T10:00:00Z",
            "observed_at_start": "2026-07-15T10:00:00Z",
            "observed_at_end": "2026-07-15T11:00:00Z",
            "freshness": "event_time_matched",
            "approval_state": "unknown",
            "privacy": "redacted",
            "confidence": 0.7,
            "conflicts": [],
            "currentness": "unknown",
            "card_type": "",
            "score": 0.7,
            "source_kind": "session_memory_artifact",
        }
    ]


def test_temporal_recall_excludes_unrelated_work_observed_on_the_same_date() -> None:
    service = BrainReadService(
        memory_cards=[
            _task_card(
                "mem_deploy",
                "Deploy the temporal projection migration",
                observed_at_start="2026-07-15T10:00:00Z",
                observed_at_end="2026-07-15T11:00:00Z",
            ),
            _task_card(
                "mem_unrelated",
                "Polish the unrelated profile screen",
                observed_at_start="2026-07-15T10:00:00Z",
                observed_at_end="2026-07-15T11:00:00Z",
            ),
        ]
    )

    result = service.brain_objects_query(
        repository="neurons",
        branch="main",
        query="Which temporal projection migration did we deploy?",
        current_files=[],
        route="temporal_work_recall",
        as_of="2026-07-15T10:30:00Z",
    )

    assert _work_titles(result) == ["Deploy the temporal projection migration"]


def test_temporal_recall_fails_closed_when_same_date_evidence_is_query_irrelevant() -> None:
    service = BrainReadService(
        memory_cards=[
            _task_card(
                "mem_unrelated_same_date",
                "Deploy the temporal projection migration",
                observed_at_start="2026-07-15T10:00:00Z",
                observed_at_end="2026-07-15T11:00:00Z",
            )
        ]
    )

    result = service.brain_objects_query(
        repository="neurons",
        branch="main",
        query="quasar marmalade",
        current_files=[],
        route="temporal_work_recall",
        as_of="2026-07-15T10:30:00Z",
    )

    assert result["object_pack"]["objects"] == []
    assert result["object_pack"]["confidence"]["score"] == 0.0
    assert result["object_pack"]["gaps"]


def test_temporal_recall_matches_card_tokens_not_substrings() -> None:
    service = BrainReadService(
        memory_cards=[
            _task_card(
                "mem_substring_only",
                "Concatenate profile logs",
                observed_at_start="2026-07-15T10:00:00Z",
                observed_at_end="2026-07-15T11:00:00Z",
            )
        ]
    )

    result = service.brain_objects_query(
        repository="neurons",
        branch="main",
        query="cat",
        current_files=[],
        route="temporal_work_recall",
        as_of="2026-07-15T10:30:00Z",
    )

    pack = result["object_pack"]
    assert pack["objects"] == []
    assert pack["confidence"]["score"] == 0.0
    assert "temporal_evidence_no_relevant_match" in pack["gaps"]


def test_temporal_object_type_filter_fails_closed_when_it_removes_all_matches() -> None:
    service = BrainReadService(
        memory_cards=[
            _task_card(
                "mem_filtered_match",
                "Deploy the temporal projection migration",
                observed_at_start="2026-07-15T10:00:00Z",
                observed_at_end="2026-07-15T11:00:00Z",
            )
        ]
    )

    result = service.brain_objects_query(
        repository="neurons",
        branch="main",
        query="temporal projection migration",
        current_files=[],
        object_types=["Decision"],
        route="temporal_work_recall",
        as_of="2026-07-15T10:30:00Z",
    )

    pack = result["object_pack"]
    assert pack["objects"] == []
    assert pack["confidence"] == {
        "score": 0.0,
        "basis": "temporal_object_type_filter_no_match",
    }
    assert "temporal_object_type_filter_no_matching_evidence" in pack["gaps"]
    assert pack["route_trace"]["stop_reason"] == "missing_evidence_gap_returned"


@pytest.mark.parametrize(
    "boundary",
    ["2026-07-09T10:00:00Z", "2026-07-09T12:00:00Z"],
)
def test_temporal_recall_range_includes_observed_interval_boundaries(boundary: str) -> None:
    service = BrainReadService(
        memory_cards=[
            _task_card(
                "mem_boundary",
                "Work touching the requested boundary",
                observed_at_start="2026-07-09T10:00:00Z",
                observed_at_end="2026-07-09T12:00:00Z",
            )
        ]
    )

    result = _temporal_query(service, date_from=boundary, date_to=boundary)

    assert _work_titles(result) == ["Work touching the requested boundary"]


@pytest.mark.parametrize(
    ("card", "as_of"),
    [
        (_task_card("mem_missing_time", "Work without temporal evidence"), "2026-07-15T12:00:00Z"),
        (
            _task_card(
                "mem_wrong_date",
                "Unrelated work from another date",
                observed_at_start="2026-07-09T00:00:00Z",
                observed_at_end="2026-07-09T23:59:59Z",
            ),
            "2026-07-15T12:00:00Z",
        ),
    ],
)
def test_temporal_recall_fails_closed_without_matching_temporal_evidence(
    card: dict,
    as_of: str,
) -> None:
    service = BrainReadService(memory_cards=[card])

    result = _temporal_query(service, as_of=as_of)

    pack = result["object_pack"]
    assert pack["objects"] == []
    assert pack["confidence"]["score"] == 0.0
    assert pack["gaps"]
    assert any("temporal" in gap.casefold() for gap in pack["gaps"])
