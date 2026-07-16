from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.ledger import Ledger
from agent_knowledge.llm_brain_core.couchdb_projection_cli import run_couchdb_projection
from agent_knowledge.llm_brain_core.graph import FakeGraphMemoryAdapter
from agent_knowledge.llm_brain_core.graph_projection_status_cli import build_graph_projection_status
from agent_knowledge.llm_brain_core.runtime import (
    session_source_revision_from_couchdb_source,
)
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession


PROVIDER = "codex"
PROJECT = "neurons"


class _EntityFlagFakeGraph(FakeGraphMemoryAdapter):
    _extract_entities = True


class _FailingEntityGraph(_EntityFlagFakeGraph):
    def upsert_episode(self, episode):  # type: ignore[no-untyped-def]
        if episode.payload.get("session_id_hash") == self._failing_sid:
            raise ValueError("synthetic projection failure")
        return super().upsert_episode(episode)

    def __init__(self, failing_sid: str) -> None:
        super().__init__()
        self._failing_sid = failing_sid


class _FailingSetEntityGraph(_EntityFlagFakeGraph):
    def __init__(self, failing_sids: set[str]) -> None:
        super().__init__()
        self._failing_sids = set(failing_sids)

    def upsert_episode(self, episode):  # type: ignore[no-untyped-def]
        if episode.payload.get("session_id_hash") in self._failing_sids:
            raise ValueError("synthetic persistent projection failure")
        return super().upsert_episode(episode)


class _ExplodingEntityGraph(_EntityFlagFakeGraph):
    def upsert_episode(self, episode):  # type: ignore[no-untyped-def]
        raise AssertionError("graph adapter should not be called while locked")


class _DisabledEntityGraph(_EntityFlagFakeGraph):
    def upsert_episode(self, episode):  # type: ignore[no-untyped-def]
        return "skipped_disabled"


def _seed_session(
    store: InMemoryCouchDBSourceStore,
    *,
    raw_id: str,
    project: str = PROJECT,
    provider: str = PROVIDER,
    body: str = "user asked about ontology projection; assistant answered with a plan",
) -> str:
    sid = dm.build_session_id_hash(provider, raw_id)
    session = TranscriptSession(
        session_id_hash=sid,
        provider=provider,
        project=project,
        started_at="2026-06-21T00:00:00Z",
    )
    store.put(dm.build_transcript_session_document(session=session))
    chunk = TranscriptChunk.from_text(
        chunk_id=f"chunk_{raw_id}",
        session_id_hash=sid,
        provider=provider,
        project=project,
        turn_start_index=0,
        turn_end_index=0,
        text=body,
    )
    chunk_doc = dm.build_conversation_chunk_document(chunk=chunk)
    store.put(chunk_doc)
    store.put(
        dm.build_coverage_manifest_document(
            session_id_hash=sid,
            provider=provider,
            project=project,
            conversation_chunk_count=1,
            tool_evidence_bundle_count=0,
            conversation_content_hashes=[chunk_doc["content_hash"]],
            tool_evidence_coverage_hashes=[],
            project_authority={
                "project": project,
                "ambiguous": False,
                "eligible_for_retirement": True,
            },
        )
    )
    return sid


def _project(
    *,
    tmp_path: Path,
    store: InMemoryCouchDBSourceStore,
    graph_adapter: Any,
    limit: int,
    dead_letter_jsonl: Path | None = None,
    progress_jsonl: Path | None = None,
    runtime_dir: Path | None = None,
    max_projects: int = 0,
) -> dict[str, Any]:
    return run_couchdb_projection(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        limit=limit,
        project=PROJECT,
        provider=PROVIDER,
        enable_graph=True,
        graph_required=False,
        extract_entities=True,
        reextract_entities=False,
        resume=True,
        dead_letter_jsonl=dead_letter_jsonl,
        progress_jsonl=progress_jsonl,
        report_every=100,
        max_projects=max_projects,
        graph_adapter=graph_adapter,
        runtime_dir=runtime_dir,
    )


def test_projects_more_than_one_hundred_sessions_and_resumes_entity_level(tmp_path):
    store = InMemoryCouchDBSourceStore()
    for index in range(105):
        _seed_session(store, raw_id=f"session-{index:03d}")

    first = _project(
        tmp_path=tmp_path,
        store=store,
        graph_adapter=_EntityFlagFakeGraph(),
        limit=105,
    )

    assert first["status"] == "ok"
    assert first["canonical_counts"]["selected_sessions"] == 105
    assert first["truncated"] is False
    assert "project" not in first["filters"]
    assert first["filters"]["project_set"] is True
    assert first["filters"]["project_ref"].startswith("sha256:")
    assert first["target_extraction_level"] == "entity"
    assert first["projection"]["attempted"] == 105
    assert first["projection"]["projected"] == 105
    assert first["projection"]["skipped_resumed"] == 0
    assert first["projection"]["failed"] == 0

    second = _project(
        tmp_path=tmp_path,
        store=store,
        graph_adapter=_EntityFlagFakeGraph(),
        limit=105,
    )

    assert second["status"] == "ok"
    assert second["canonical_counts"]["selected_sessions"] == 0
    assert second["projection"]["attempted"] == 0
    assert second["projection"]["projected"] == 0
    assert second["projection"]["skipped_resumed"] == 0
    assert second["projection"]["failed"] == 0


def test_source_hash_change_invalidates_graph_projection_resume(tmp_path):
    store = InMemoryCouchDBSourceStore()
    raw_id = "stable-natural-id"
    _seed_session(store, raw_id=raw_id, body="first body mentions Graphiti and Neo4j")

    first = _project(
        tmp_path=tmp_path,
        store=store,
        graph_adapter=_EntityFlagFakeGraph(),
        limit=1,
    )

    assert first["projection"]["projected"] == 1

    _seed_session(store, raw_id=raw_id, body="changed body mentions DeepSeek and Gemini")
    second = _project(
        tmp_path=tmp_path,
        store=store,
        graph_adapter=_EntityFlagFakeGraph(),
        limit=1,
    )

    assert second["projection"]["projected"] == 1
    assert second["projection"]["skipped_resumed"] == 0
    assert second["projection"]["failed"] == 0


def test_max_projects_stops_after_bounded_non_resumed_upserts(tmp_path):
    store = InMemoryCouchDBSourceStore()
    for index in range(5):
        _seed_session(store, raw_id=f"bounded-{index}")

    first = _project(
        tmp_path=tmp_path,
        store=store,
        graph_adapter=_EntityFlagFakeGraph(),
        limit=2,
    )
    assert first["projection"]["projected"] == 2

    second = _project(
        tmp_path=tmp_path,
        store=store,
        graph_adapter=_EntityFlagFakeGraph(),
        limit=5,
        max_projects=2,
    )

    assert second["status"] == "ok"
    assert second["canonical_counts"]["selected_sessions"] == 3
    assert second["projection"]["attempted"] == 2
    assert second["projection"]["skipped_resumed"] == 0
    assert second["projection"]["projected"] == 2
    assert second["projection"]["failed"] == 0
    assert second["projection"]["stopped_after_max_projects"] is True
    assert second["truncated"] is True


def test_limited_resume_prioritizes_unprojected_sessions(tmp_path):
    store = InMemoryCouchDBSourceStore()
    for index in range(3):
        _seed_session(store, raw_id=f"new-session-priority-{index}")

    first = _project(
        tmp_path=tmp_path,
        store=store,
        graph_adapter=_EntityFlagFakeGraph(),
        limit=2,
    )
    assert first["projection"]["projected"] == 2

    second = _project(
        tmp_path=tmp_path,
        store=store,
        graph_adapter=_EntityFlagFakeGraph(),
        limit=2,
    )

    assert second["projection"]["attempted"] == 1
    assert second["projection"]["projected"] == 1
    assert second["projection"]["skipped_resumed"] == 0
    assert second["projection"]["failed"] == 0


def test_reextract_advances_to_unprojected_tail_before_reprocessing_current_sessions(tmp_path):
    store = InMemoryCouchDBSourceStore()
    sids = [_seed_session(store, raw_id=f"reextract-{index}") for index in range(3)]
    # canonical selection order is (project, provider, session_id_hash); same project/
    # provider here, so the canonically-last sid stays unprojected after a limit=2 pass.
    last_unprojected = sorted(sids)[-1]

    first = _project(
        tmp_path=tmp_path,
        store=store,
        graph_adapter=_EntityFlagFakeGraph(),
        limit=2,
    )
    assert first["projection"]["projected"] == 2

    reextract_graph = _EntityFlagFakeGraph()
    second = run_couchdb_projection(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        limit=2,
        project=PROJECT,
        provider=PROVIDER,
        enable_graph=True,
        graph_required=False,
        extract_entities=True,
        reextract_entities=True,
        resume=True,
        report_every=100,
        max_projects=0,
        graph_adapter=reextract_graph,
    )

    touched = {
        str(episode.payload.get("session_id_hash") or "")
        for episode in reextract_graph._episodes.values()
    }
    # A bounded reextract pass must first close the never-projected tail instead of
    # repeatedly spending the global limit on already-current rows.
    assert second["projection"]["attempted"] == 2
    assert last_unprojected in touched


def test_unscoped_limit_round_robins_across_projects_instead_of_starving_later_project(tmp_path):
    store = InMemoryCouchDBSourceStore()
    _seed_session(store, raw_id="alpha-1", project="alpha")
    _seed_session(store, raw_id="alpha-2", project="alpha")
    _seed_session(store, raw_id="beta-1", project="beta")

    first_graph = _EntityFlagFakeGraph()
    first = run_couchdb_projection(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        limit=1,
        project="",
        provider=PROVIDER,
        enable_graph=True,
        graph_required=False,
        extract_entities=True,
        resume=True,
        graph_adapter=first_graph,
    )
    second_graph = _EntityFlagFakeGraph()
    second = run_couchdb_projection(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        limit=1,
        project="",
        provider=PROVIDER,
        enable_graph=True,
        graph_required=False,
        extract_entities=True,
        resume=True,
        graph_adapter=second_graph,
    )

    first_projects = {
        str(episode.payload.get("project") or "")
        for episode in first_graph._episodes.values()
    }
    second_projects = {
        str(episode.payload.get("project") or "")
        for episode in second_graph._episodes.values()
    }
    assert first["projection"]["projected"] == 1
    assert second["projection"]["projected"] == 1
    assert first_projects == {"alpha"}
    assert second_projects == {"beta"}


def test_persistent_cursor_advances_past_repeated_failures_to_pending_tail(tmp_path):
    store = InMemoryCouchDBSourceStore()
    session_ids = [
        _seed_session(store, raw_id=f"persistent-failure-{index}")
        for index in range(4)
    ]
    failing = set(sorted(session_ids)[:2])
    runtime_dir = tmp_path / "runtime"

    first = _project(
        tmp_path=tmp_path,
        store=store,
        graph_adapter=_FailingSetEntityGraph(failing),
        limit=2,
        runtime_dir=runtime_dir,
    )
    second_graph = _FailingSetEntityGraph(failing)
    second = _project(
        tmp_path=tmp_path,
        store=store,
        graph_adapter=second_graph,
        limit=2,
        runtime_dir=runtime_dir,
    )

    touched_second = {
        str(episode.payload.get("session_id_hash") or "")
        for episode in second_graph._episodes.values()
    }
    assert first["status"] == "failed"
    assert first["projection"]["failed"] == 2
    assert first["scheduler"]["persistent_cursor_enabled"] is True
    assert first["scheduler"]["cursor_advance"] == 2
    assert second["status"] == "ok"
    assert second["projection"]["projected"] == 2
    assert touched_second == set(session_ids) - failing
    assert second["scheduler"]["cursor_offset"] == 2


def test_partial_projection_continues_and_writes_dead_letter(tmp_path):
    store = InMemoryCouchDBSourceStore()
    _seed_session(store, raw_id="good")
    failing_sid = _seed_session(store, raw_id="bad")
    dead_letter = tmp_path / "dead-letter.jsonl"

    report = _project(
        tmp_path=tmp_path,
        store=store,
        graph_adapter=_FailingEntityGraph(failing_sid),
        limit=2,
        dead_letter_jsonl=dead_letter,
    )

    assert report["status"] == "partial"
    assert report["projection"]["attempted"] == 2
    assert report["projection"]["projected"] == 1
    assert report["projection"]["failed"] == 1
    assert report["projection"]["failure_reasons"] == {"ValueError": 1}

    lines = [json.loads(line) for line in dead_letter.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    assert lines[0]["session_id_hash"] == failing_sid
    assert "project" not in lines[0]
    assert lines[0]["project_ref"].startswith("sha256:")
    assert lines[0]["reason_code"] == "ValueError"
    assert lines[0]["stage"] == "project"


def test_graph_disabled_reports_skipped_disabled_not_duplicate(tmp_path):
    store = InMemoryCouchDBSourceStore()
    _seed_session(store, raw_id="disabled")
    progress = tmp_path / "progress.jsonl"

    report = _project(
        tmp_path=tmp_path,
        store=store,
        graph_adapter=_DisabledEntityGraph(),
        limit=1,
        progress_jsonl=progress,
    )

    assert report["status"] == "ok"
    assert report["projection"]["attempted"] == 1
    assert report["projection"]["skipped_disabled"] == 1
    assert report["projection"]["projected"] == 0
    assert report["projection"]["duplicates"] == 0
    assert report["projection"]["failed"] == 0

    events = [json.loads(line) for line in progress.read_text(encoding="utf-8").splitlines()]
    progress_events = [event for event in events if event["event"] == "progress"]
    assert len(progress_events) == 1
    assert progress_events[0]["status"] == "skipped_disabled"
    assert progress_events[0]["skipped_disabled"] == 1


def test_progress_jsonl_uses_project_ref_not_raw_project(tmp_path):
    store = InMemoryCouchDBSourceStore()
    _seed_session(store, raw_id="progress")
    progress = tmp_path / "progress.jsonl"

    _project(
        tmp_path=tmp_path,
        store=store,
        graph_adapter=_EntityFlagFakeGraph(),
        limit=1,
        progress_jsonl=progress,
    )

    events = [json.loads(line) for line in progress.read_text(encoding="utf-8").splitlines()]
    progress_events = [event for event in events if event["event"] == "progress"]
    assert len(progress_events) == 1
    assert "project" not in progress_events[0]
    assert progress_events[0]["project_ref"].startswith("sha256:")
    assert {event["run_id"] for event in events} == {events[0]["run_id"]}
    assert events[0]["project_set"] is True
    assert events[0]["project_ref"].startswith("sha256:")
    assert events[0]["provider"] == PROVIDER
    assert events[0]["target_extraction_level"] == "entity"
    assert events[0]["started_at"].endswith("Z")
    assert events[-1]["event"] == "complete"
    assert events[-1]["completed_at"].endswith("Z")
    assert events[-1]["project_ref"] == events[0]["project_ref"]


def test_runtime_lock_skips_without_calling_graph_adapter(tmp_path):
    import fcntl

    store = InMemoryCouchDBSourceStore()
    _seed_session(store, raw_id="locked")
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    holder = (runtime / "graph-project.lock").open("a+", encoding="utf-8")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        report = _project(
            tmp_path=tmp_path,
            store=store,
            graph_adapter=_ExplodingEntityGraph(),
            limit=1,
            runtime_dir=runtime,
        )
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()

    assert report["status"] == "already_running"
    assert report["runtime_lock"] == {"enabled": True, "acquired": False}
    assert report["mutation_performed"] is False
    assert report["raw_paths_printed"] is False


def test_status_reports_entity_coverage_backlog_and_lag(tmp_path):
    store = InMemoryCouchDBSourceStore()
    for raw_id in ("status-a", "status-b", "status-c"):
        _seed_session(store, raw_id=raw_id)
    _project(
        tmp_path=tmp_path,
        store=store,
        graph_adapter=_EntityFlagFakeGraph(),
        limit=2,
    )

    report = build_graph_projection_status(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        project=PROJECT,
        provider=PROVIDER,
    )

    assert report["status"] == "ok"
    assert "project" not in report["filters"]
    assert report["filters"]["project_ref"].startswith("sha256:")
    assert report["source"]["session_count"] == 3
    assert report["projection_state"]["episodic_session_projected"] == 0
    assert report["projection_state"]["episodic_session_noncurrent"] == 3
    assert report["projection_state"]["entity_session_projected"] == 2
    assert report["projection_state"]["entity_session_backlog"] == 1
    assert report["projection_state"]["source_hash_mismatch_count"] == 0
    assert report["projection_state"]["stale_projected_session_count"] == 0
    assert 0.66 < report["projection_state"]["entity_coverage_ratio"] < 0.67
    assert report["artifact_age"]["artifact_session_count"] == 2
    assert report["artifact_age"]["artifact_missing_session_count"] == 1
    assert report["artifact_age"]["artifact_age_unknown_count"] == 0
    assert report["artifact_age"]["artifact_source_hash_mismatch_count"] == 0
    assert report["lag"]["oldest_unprojected_started_at"] == "2026-06-21T00:00:00Z"
    assert report["raw_paths_printed"] is False


def test_status_counts_source_hash_mismatch_as_stale_backlog(tmp_path):
    store = InMemoryCouchDBSourceStore()
    raw_id = "status-source-change"
    _seed_session(store, raw_id=raw_id, body="Initial graph source.")
    _project(
        tmp_path=tmp_path,
        store=store,
        graph_adapter=_EntityFlagFakeGraph(),
        limit=1,
    )
    _seed_session(store, raw_id=raw_id, body="Changed graph source.")

    report = build_graph_projection_status(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        project=PROJECT,
        provider=PROVIDER,
    )

    assert report["projection_state"]["source_hash_mismatch_count"] == 1
    assert report["projection_state"]["stale_projected_session_count"] == 1
    assert report["projection_state"]["entity_session_projected"] == 0
    assert report["projection_state"]["entity_session_backlog"] == 1
    assert report["artifact_age"]["artifact_source_hash_mismatch_count"] == 1


def test_status_fails_closed_for_legacy_projection_state_without_source_hash(tmp_path):
    store = InMemoryCouchDBSourceStore()
    session_id_hash = _seed_session(store, raw_id="legacy-state-without-source-hash")
    natural_id = session_id_hash.replace(":", "_")
    ledger_path = tmp_path / "ledger.sqlite3"
    Ledger(ledger_path)

    # Model a pre-currentness table.  The status command opens its ledger
    # read-only, so it must not rely on a write-time migration to add the
    # source_hash column.
    with Ledger(ledger_path)._connect() as connection:
        connection.execute("DROP TABLE llm_brain_graph_projection_state")
        connection.executescript(
            """
            CREATE TABLE llm_brain_graph_projection_state (
                episode_id TEXT NOT NULL,
                extraction_level TEXT NOT NULL DEFAULT 'episodic',
                project TEXT NOT NULL DEFAULT '',
                entity_type TEXT NOT NULL DEFAULT '',
                natural_id TEXT NOT NULL DEFAULT '',
                upsert_result TEXT NOT NULL DEFAULT '',
                projected_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(episode_id, extraction_level)
            );
            """
        )
        connection.execute(
            """
            INSERT INTO llm_brain_graph_projection_state (
                episode_id, extraction_level, project, entity_type, natural_id,
                upsert_result, projected_at, updated_at
            ) VALUES (?, 'entity', ?, 'Session', ?, 'inserted',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            ("legacy:source-hash", PROJECT, natural_id),
        )

    report = build_graph_projection_status(
        ledger_path=ledger_path,
        source_store=store,
        project=PROJECT,
        provider=PROVIDER,
    )

    state = report["projection_state"]
    assert report["status"] == "ok"
    assert state["entity_session_projected"] == 0
    assert state["entity_session_backlog"] == 1
    assert state["source_hash_mismatch_count"] == 1
    assert state["stale_projected_session_count"] == 1


def test_status_digest_changes_for_same_session_distinct_source_revision(tmp_path):
    store = InMemoryCouchDBSourceStore()
    raw_id = "status-digest-source-change"
    _seed_session(store, raw_id=raw_id, body="Initial graph source.")
    _project(
        tmp_path=tmp_path,
        store=store,
        graph_adapter=_EntityFlagFakeGraph(),
        limit=1,
    )
    before = build_graph_projection_status(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        project=PROJECT,
        provider=PROVIDER,
    )

    _seed_session(store, raw_id=raw_id, body="Distinct graph source revision.")
    after = build_graph_projection_status(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        project=PROJECT,
        provider=PROVIDER,
    )

    assert before["source"]["session_count"] == after["source"]["session_count"] == 1
    before_state = before["projection_state"]
    after_state = after["projection_state"]
    for field in (
        "source_state_digest",
        "graph_projection_state_digest",
        "session_memory_projection_state_digest",
        "source_projection_state_digest",
    ):
        assert before_state[field].startswith("sha256:")
        assert after_state[field].startswith("sha256:")
    assert before_state["source_state_digest"] != after_state["source_state_digest"]
    assert (
        before_state["source_projection_state_digest"]
        != after_state["source_projection_state_digest"]
    )
    assert (
        before_state["graph_projection_state_digest"]
        == after_state["graph_projection_state_digest"]
    )


def test_status_digest_is_stable_for_same_session_exact_duplicate(tmp_path):
    store = InMemoryCouchDBSourceStore()
    raw_id = "status-digest-exact-duplicate"
    body = "Stable graph source."
    _seed_session(store, raw_id=raw_id, body=body)
    _project(
        tmp_path=tmp_path,
        store=store,
        graph_adapter=_EntityFlagFakeGraph(),
        limit=1,
    )
    before = build_graph_projection_status(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        project=PROJECT,
        provider=PROVIDER,
    )

    _seed_session(store, raw_id=raw_id, body=body)
    after = build_graph_projection_status(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        project=PROJECT,
        provider=PROVIDER,
    )

    assert (
        before["projection_state"]["source_projection_state_digest"]
        == after["projection_state"]["source_projection_state_digest"]
    )


def test_status_includes_stale_canonical_session_memory_projection_currentness(tmp_path):
    store = InMemoryCouchDBSourceStore()
    session_id_hash = _seed_session(
        store,
        raw_id="canonical-session-memory-stale",
        body="Current canonical source revision.",
    )
    stale_source_hash = dm.sha256_hash("prior canonical source revision")
    store.put(
        dm.build_projection_state_document(
            session_id_hash=session_id_hash,
            provider=PROVIDER,
            project=PROJECT,
            projection_status=dm.ProjectionStatus.PROJECTED,
            active_content_hash=dm.sha256_hash("projected session memory"),
            source_hash=stale_source_hash,
            projected_source_hash=stale_source_hash,
            materialized_at="2026-06-21T00:01:00Z",
        )
    )
    Ledger(tmp_path / "ledger.sqlite3")

    report = build_graph_projection_status(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        project=PROJECT,
        provider=PROVIDER,
    )

    state = report["projection_state"]
    assert state["session_memory_projection_current_count"] == 0
    assert state["session_memory_projection_noncurrent_count"] == 1
    assert state["session_memory_source_hash_mismatch_count"] == 1
    assert state["session_memory_stale_projected_session_count"] == 1
    assert state["source_hash_mismatch_count"] == 1
    assert state["stale_projected_session_count"] == 1


def test_status_reports_one_stale_lane_even_when_other_lane_is_current(tmp_path):
    store = InMemoryCouchDBSourceStore()
    session_id_hash = _seed_session(store, raw_id="mixed-lane-currentness")
    natural_id = session_id_hash.replace(":", "_")
    current_source_hash = session_source_revision_from_couchdb_source(
        session_id_hash=session_id_hash,
        source_store=store,
    )
    stale_source_hash = dm.sha256_hash("stale episodic source")

    with Ledger(tmp_path / "ledger.sqlite3")._connect() as connection:
        for level, source_hash in (
            ("episodic", stale_source_hash),
            ("entity", current_source_hash),
        ):
            connection.execute(
                """
                INSERT INTO llm_brain_graph_projection_state (
                    episode_id, extraction_level, project, entity_type, natural_id,
                    group_id, brain_id, content_hash, source_hash, ontology_version,
                    extractor_version, upsert_result, projected_at, updated_at
                ) VALUES (?, ?, ?, 'Session', ?, '', '', '', ?, '', '', 'inserted',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (f"mixed:{level}", level, PROJECT, natural_id, source_hash),
            )

    report = build_graph_projection_status(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        project=PROJECT,
        provider=PROVIDER,
    )

    assert report["projection_state"]["entity_session_projected"] == 1
    assert report["projection_state"]["source_hash_mismatch_count"] == 1
    assert report["projection_state"]["stale_projected_session_count"] == 1


def test_status_excludes_source_invalid_sessions_from_valid_backlog(tmp_path):
    store = InMemoryCouchDBSourceStore()
    projected_sid = _seed_session(store, raw_id="valid-projected")
    invalid_sid = _seed_session(store, raw_id="invalid-source")
    backlog_sid = _seed_session(store, raw_id="valid-backlog")
    _ = (projected_sid, backlog_sid)
    projected_natural_id = projected_sid.replace(":", "_")
    invalid_natural_id = invalid_sid.replace(":", "_")
    projected_source_hash = session_source_revision_from_couchdb_source(
        session_id_hash=projected_sid,
        source_store=store,
    )
    invalid_source_hash = session_source_revision_from_couchdb_source(
        session_id_hash=invalid_sid,
        source_store=store,
    )
    with Ledger(tmp_path / "ledger.sqlite3")._connect() as connection:
        connection.execute(
            """
            INSERT INTO llm_brain_graph_projection_state (
                episode_id, extraction_level, project, entity_type, natural_id,
                group_id, brain_id, content_hash, source_hash, ontology_version,
                extractor_version, upsert_result, projected_at, updated_at
            ) VALUES (?, 'entity', ?, 'Session', ?, '', '', '', ?, '', '', 'inserted',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                f"projected:{projected_natural_id}",
                PROJECT,
                projected_natural_id,
                projected_source_hash,
            ),
        )
        connection.execute(
            """
            INSERT INTO llm_brain_graph_projection_state (
                episode_id, extraction_level, project, entity_type, natural_id,
                group_id, brain_id, content_hash, source_hash, ontology_version,
                extractor_version, upsert_result, projected_at, updated_at
            ) VALUES (?, 'entity', ?, 'Session', ?, '', '', '', ?, '', '', 'source_invalid',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                f"source-invalid:{invalid_natural_id}",
                PROJECT,
                invalid_natural_id,
                invalid_source_hash,
            ),
        )

    report = build_graph_projection_status(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        project=PROJECT,
        provider=PROVIDER,
    )

    assert report["source"]["session_count"] == 3
    assert report["projection_state"]["entity_valid_source_sessions"] == 2
    assert report["projection_state"]["entity_source_invalid"] == 1
    assert report["projection_state"]["entity_session_projected"] == 1
    assert report["projection_state"]["entity_session_backlog"] == 1
    assert report["projection_state"]["entity_coverage_ratio"] == 0.5


def test_status_scopes_recency_metrics_to_selected_provider(tmp_path):
    store = InMemoryCouchDBSourceStore()
    _seed_session(store, raw_id="codex-projected")
    _project(
        tmp_path=tmp_path,
        store=store,
        graph_adapter=_EntityFlagFakeGraph(),
        limit=1,
    )

    # Same project, but the natural_id belongs to a session outside the codex
    # provider scope. The projection_state table has no provider column, so this
    # row must be excluded via the selected source-set intersection.
    foreign_natural_id = "claude_session_outside_scope"
    with Ledger(tmp_path / "ledger.sqlite3")._connect() as connection:
        connection.execute(
            """
            INSERT INTO llm_brain_graph_projection_state (
                episode_id, extraction_level, project, entity_type, natural_id,
                group_id, brain_id, content_hash, ontology_version,
                extractor_version, upsert_result, projected_at, updated_at
            ) VALUES (?, 'entity', ?, 'Session', ?, '', '', '', '', '', 'inserted',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (f"foreign:{foreign_natural_id}", PROJECT, foreign_natural_id),
        )

    report = build_graph_projection_status(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        project=PROJECT,
        provider=PROVIDER,
    )

    assert report["source"]["session_count"] == 1
    assert report["projection_state"]["entity_session_projected"] == 1
    # The foreign-provider row must not inflate provider-scoped recency metrics.
    assert report["projection_state"]["entity_projected_last_24h"] == 1
    assert report["projection_state"]["entity_projected_last_1h"] == 1


def test_status_summarizes_progress_and_dead_letters_without_raw_project(tmp_path):
    store = InMemoryCouchDBSourceStore()
    _seed_session(store, raw_id="status-progress")
    Ledger(tmp_path / "ledger.sqlite3")
    progress = tmp_path / "progress.jsonl"
    progress.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "start",
                        "run_id": "run:current",
                        "selected": 1,
                        "project_set": True,
                        "project_ref": "sha256:scope",
                        "provider": PROVIDER,
                        "target_extraction_level": "entity",
                        "started_at": "2026-07-16T00:00:00Z",
                    }
                ),
                json.dumps(
                    {
                        "event": "progress",
                        "run_id": "run:current",
                        "index": 1,
                        "selected": 1,
                        "project": PROJECT,
                        "projected": 0,
                        "skipped_resumed": 0,
                        "failed": 1,
                        "elapsed_ms": 42,
                    }
                ),
                json.dumps(
                    {
                        "event": "complete",
                        "run_id": "run:current",
                        "status": "partial",
                        "selected": 1,
                        "projected": 0,
                        "skipped_resumed": 0,
                        "failed": 1,
                        "project_set": True,
                        "project_ref": "sha256:scope",
                        "provider": PROVIDER,
                        "target_extraction_level": "entity",
                        "completed_at": "2026-07-16T00:00:42Z",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    dead_letter = tmp_path / "dead-letter.jsonl"
    dead_letter.write_text(
        json.dumps(
            {
                "run_id": "run:current",
                "project": PROJECT,
                "reason_code": "ValueError",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = build_graph_projection_status(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        project=PROJECT,
        provider=PROVIDER,
        progress_jsonl=[progress],
        dead_letter_jsonl=[dead_letter],
    )

    assert report["progress"]["event_counts"] == {
        "complete": 1,
        "progress": 1,
        "start": 1,
    }
    assert report["progress"]["last_index"] == 1
    assert report["progress"]["failed"] == 1
    assert report["progress"]["avg_checkpoint_elapsed_ms"] == 42
    assert report["progress"]["latest_run_completed"] is True
    assert report["progress"]["latest_run_status"] == "partial"
    assert report["progress"]["latest_run_ref"].startswith("sha256:")
    assert report["progress"]["latest_run_project_set"] is True
    assert report["progress"]["latest_run_project_ref"] == "sha256:scope"
    assert report["progress"]["latest_run_provider"] == PROVIDER
    assert report["progress"]["latest_run_target_extraction_level"] == "entity"
    assert report["progress"]["latest_run_scope_consistent"] is True
    assert report["progress"]["latest_run_started_at"] == "2026-07-16T00:00:00Z"
    assert report["progress"]["latest_run_completed_at"] == "2026-07-16T00:00:42Z"
    assert report["dead_letter"] == {
        "count": 1,
        "total_count": 1,
        "failure_reasons": {"ValueError": 1},
    }
    assert PROJECT not in json.dumps(report, sort_keys=True)


def test_status_marks_latest_started_run_incomplete_and_scopes_old_dead_letters(tmp_path):
    store = InMemoryCouchDBSourceStore()
    _seed_session(store, raw_id="status-incomplete")
    Ledger(tmp_path / "ledger.sqlite3")
    progress = tmp_path / "progress.jsonl"
    progress.write_text(
        "\n".join(
            [
                json.dumps({"event": "start", "run_id": "run:old", "selected": 1}),
                json.dumps(
                    {
                        "event": "complete",
                        "run_id": "run:old",
                        "status": "ok",
                        "selected": 1,
                        "projected": 1,
                        "failed": 0,
                    }
                ),
                json.dumps({"event": "start", "run_id": "run:current", "selected": 1}),
            ]
        ),
        encoding="utf-8",
    )
    dead_letter = tmp_path / "dead-letter.jsonl"
    dead_letter.write_text(
        json.dumps({"run_id": "run:old", "reason_code": "OldFailure"}) + "\n",
        encoding="utf-8",
    )

    report = build_graph_projection_status(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        project=PROJECT,
        provider=PROVIDER,
        progress_jsonl=[progress],
        dead_letter_jsonl=[dead_letter],
    )

    assert report["progress"]["event_counts"] == {"start": 1}
    assert report["progress"]["latest_run_completed"] is False
    assert report["progress"]["latest_run_status"] == ""
    assert report["dead_letter"] == {
        "count": 0,
        "total_count": 1,
        "failure_reasons": {},
    }


def test_status_preserves_scoped_entity_attempt_across_newer_episodic_runs(tmp_path):
    store = InMemoryCouchDBSourceStore()
    _seed_session(store, raw_id="status-run-kind")
    Ledger(tmp_path / "ledger.sqlite3")
    project_ref = "sha256:" + hashlib.sha256(PROJECT.encode()).hexdigest()[:12]
    progress = tmp_path / "progress.jsonl"

    def event(run_id: str, event_name: str, level: str, **extra: Any) -> dict[str, Any]:
        return {
            "event": event_name,
            "run_id": run_id,
            "project_set": True,
            "project_ref": project_ref,
            "provider": "",
            "target_extraction_level": level,
            **extra,
        }

    payloads = [
        event(
            "run:entity",
            "start",
            "entity",
            selected=1,
            started_at="2026-07-16T00:00:00Z",
        ),
        event(
            "run:entity",
            "complete",
            "entity",
            status="ok",
            selected=1,
            projected=1,
            failed=0,
            completed_at="2026-07-16T00:00:10Z",
        ),
        event(
            "run:episodic",
            "start",
            "episodic",
            selected=1,
            started_at="2026-07-16T00:01:00Z",
        ),
        event(
            "run:episodic",
            "complete",
            "episodic",
            status="ok",
            selected=1,
            projected=1,
            failed=0,
            completed_at="2026-07-16T00:01:10Z",
        ),
    ]
    progress.write_text(
        "\n".join(json.dumps(payload) for payload in payloads) + "\n",
        encoding="utf-8",
    )

    report = build_graph_projection_status(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        project=PROJECT,
        progress_jsonl=[progress],
    )

    assert report["progress"]["latest_run_target_extraction_level"] == "episodic"
    entity = report["progress"]["latest_entity_run"]
    assert entity["completed"] is True
    assert entity["status"] == "ok"
    assert entity["target_extraction_level"] == "entity"
    assert entity["dead_letter_count"] == 0
    assert "run_id" not in json.dumps(report, sort_keys=True)

    payloads.append(
        event(
            "run:entity-new",
            "start",
            "entity",
            selected=1,
            started_at="2026-07-16T00:02:00Z",
        )
    )
    progress.write_text(
        "\n".join(json.dumps(payload) for payload in payloads) + "\n",
        encoding="utf-8",
    )
    incomplete = build_graph_projection_status(
        ledger_path=tmp_path / "ledger.sqlite3",
        source_store=store,
        project=PROJECT,
        progress_jsonl=[progress],
    )
    assert incomplete["progress"]["latest_entity_run"]["completed"] is False
