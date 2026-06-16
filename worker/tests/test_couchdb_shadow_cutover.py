from __future__ import annotations

import json

import pytest

from agent_knowledge.couchdb_source.historical_import import ImportStatus, SourceLocator
from agent_knowledge.couchdb_source.shadow_cutover import (
    CutoverNotReady,
    CutoverPhase,
    RecordingComparisonSink,
    ShadowCoordinator,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore


def _fixture(tmp_path, provider, *, session_id, project_text="work"):
    if provider == "claude":
        payload = {
            "provider": "claude",
            "schema_version": "provider_transcript_fixture.v1",
            "session_id": session_id,
            "started_at": "2026-06-17T01:00:00Z",
            "messages": [
                {"role": "user", "content": f"do {project_text}", "timestamp": "2026-06-17T01:00:01Z"},
                {"role": "assistant", "content": "ok done", "timestamp": "2026-06-17T01:00:02Z"},
            ],
        }
    else:
        payload = {
            "provider": provider,
            "schema_version": "provider_transcript_fixture.v1",
            "session_id": session_id,
            "started_at": "2026-06-17T01:00:00Z",
            "turns": [
                {"role": "user", "text": f"do {project_text}", "timestamp": "2026-06-17T01:00:01Z"},
                {"role": "assistant", "text": "ok done", "timestamp": "2026-06-17T01:00:02Z"},
            ],
        }
    path = tmp_path / f"{provider}-{session_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _loc(tmp_path, provider, session_id, project):
    return SourceLocator(
        provider=provider,
        source_path=_fixture(tmp_path, provider, session_id=session_id),
        capture_metadata_project=project,
    )


def test_shadow_writes_couch_and_records_comparison(tmp_path):
    store = InMemoryCouchDBSourceStore()
    sink = RecordingComparisonSink()
    coord = ShadowCoordinator(store=store, comparison_sink=sink)
    obs = coord.ingest_live_event(_loc(tmp_path, "codex", "s1", "neurons"))
    assert obs.status == ImportStatus.IMPORTED
    assert obs.couch_written is True
    assert obs.comparison_recorded is True
    assert len(sink.calls) == 1


def test_gemini_live_event_is_scope_violation(tmp_path):
    store = InMemoryCouchDBSourceStore()
    sink = RecordingComparisonSink()
    coord = ShadowCoordinator(store=store, comparison_sink=sink)
    obs = coord.ingest_live_event(_loc(tmp_path, "gemini", "g1", "neurons"))
    assert obs.status == ImportStatus.SCOPE_VIOLATION
    assert obs.couch_written is False
    assert sink.calls == []


def test_agy_live_event_imports_under_agy_label(tmp_path):
    store = InMemoryCouchDBSourceStore()
    coord = ShadowCoordinator(store=store, comparison_sink=RecordingComparisonSink())
    obs = coord.ingest_live_event(_loc(tmp_path, "agy", "a1", "neurons"))
    assert obs.status == ImportStatus.IMPORTED
    assert obs.provider == "agy"
    assert obs.couch_written is True


def test_switch_blocked_until_all_required_lanes_covered(tmp_path):
    store = InMemoryCouchDBSourceStore()
    coord = ShadowCoordinator(store=store, comparison_sink=RecordingComparisonSink())
    coord.ingest_live_event(_loc(tmp_path, "codex", "s1", "neurons"))
    # default required set includes agy -> never ready
    with pytest.raises(CutoverNotReady):
        coord.switch_to_couchdb_only()
    verdict = coord.stability_verdict()
    assert "agy" in verdict["uncovered_providers"]
    assert "claude" in verdict["uncovered_providers"]
    assert verdict["per_provider"]["codex"]["covered"] is True


def test_cutover_to_couchdb_only_stops_comparison_writes(tmp_path):
    store = InMemoryCouchDBSourceStore()
    sink = RecordingComparisonSink()
    coord = ShadowCoordinator(store=store, comparison_sink=sink)
    parseable = ("codex", "claude", "antigravity")
    for provider in parseable:
        coord.ingest_live_event(_loc(tmp_path, provider, "s1", "neurons"))
    comparison_calls_during_shadow = len(sink.calls)
    assert comparison_calls_during_shadow == 3

    verdict = coord.switch_to_couchdb_only(required_providers=parseable)
    assert verdict["ready"] is True
    assert coord.phase == CutoverPhase.COUCHDB_ONLY

    # after cutover, new live events write CouchDB only -> no new comparison write
    obs = coord.ingest_live_event(_loc(tmp_path, "codex", "s2", "neurons"))
    assert obs.couch_written is True
    assert obs.comparison_recorded is False
    assert len(sink.calls) == comparison_calls_during_shadow  # unchanged


def test_mixed_projects_required_for_readiness(tmp_path):
    store = InMemoryCouchDBSourceStore()
    coord = ShadowCoordinator(store=store, comparison_sink=RecordingComparisonSink())
    coord.ingest_live_event(_loc(tmp_path, "codex", "s1", "neurons"))
    # require 2 distinct projects -> not ready with a single project
    verdict = coord.stability_verdict(required_providers=("codex",), min_projects=2)
    assert verdict["ready"] is False
