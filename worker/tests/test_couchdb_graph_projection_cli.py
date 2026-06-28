from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.ledger import Ledger
from agent_knowledge.llm_brain_core.couchdb_projection_cli import run_couchdb_projection
from agent_knowledge.llm_brain_core.graph import FakeGraphMemoryAdapter
from agent_knowledge.llm_brain_core.graph_projection_status_cli import build_graph_projection_status
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
    assert second["projection"]["attempted"] == 105
    assert second["projection"]["projected"] == 0
    assert second["projection"]["skipped_resumed"] == 105
    assert second["projection"]["failed"] == 0


def test_resume_uses_session_natural_id_when_episode_hash_changes(tmp_path):
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
        graph_adapter=_ExplodingEntityGraph(),
        limit=1,
    )

    assert second["projection"]["projected"] == 0
    assert second["projection"]["skipped_resumed"] == 1
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
    assert second["canonical_counts"]["selected_sessions"] == 5
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

    assert second["projection"]["attempted"] == 2
    assert second["projection"]["projected"] == 1
    assert second["projection"]["skipped_resumed"] == 1
    assert second["projection"]["failed"] == 0


def test_reextract_does_not_deprioritize_already_projected_sessions(tmp_path):
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
    # reextract must re-extract the already-projected backlog under canonical order, not
    # jump to the still-unprojected tail session and starve the projected sessions.
    assert second["projection"]["attempted"] == 2
    assert last_unprojected not in touched


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
    assert report["projection_state"]["entity_session_projected"] == 2
    assert report["projection_state"]["entity_session_backlog"] == 1
    assert 0.66 < report["projection_state"]["entity_coverage_ratio"] < 0.67
    assert report["lag"]["oldest_unprojected_started_at"] == "2026-06-21T00:00:00Z"
    assert report["raw_paths_printed"] is False


def test_status_excludes_source_invalid_sessions_from_valid_backlog(tmp_path):
    store = InMemoryCouchDBSourceStore()
    projected_sid = _seed_session(store, raw_id="valid-projected")
    invalid_sid = _seed_session(store, raw_id="invalid-source")
    backlog_sid = _seed_session(store, raw_id="valid-backlog")
    _ = (projected_sid, backlog_sid)
    projected_natural_id = projected_sid.replace(":", "_")
    invalid_natural_id = invalid_sid.replace(":", "_")
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
            (f"projected:{projected_natural_id}", PROJECT, projected_natural_id),
        )
        connection.execute(
            """
            INSERT INTO llm_brain_graph_projection_state (
                episode_id, extraction_level, project, entity_type, natural_id,
                group_id, brain_id, content_hash, ontology_version,
                extractor_version, upsert_result, projected_at, updated_at
            ) VALUES (?, 'entity', ?, 'Session', ?, '', '', '', '', '', 'source_invalid',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (f"source-invalid:{invalid_natural_id}", PROJECT, invalid_natural_id),
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
    progress = tmp_path / "progress.jsonl"
    progress.write_text(
        "\n".join(
            [
                json.dumps({"event": "start", "selected": 1}),
                json.dumps(
                    {
                        "event": "progress",
                        "index": 1,
                        "selected": 1,
                        "project": PROJECT,
                        "projected": 0,
                        "skipped_resumed": 0,
                        "failed": 1,
                        "elapsed_ms": 42,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    dead_letter = tmp_path / "dead-letter.jsonl"
    dead_letter.write_text(
        json.dumps({"project": PROJECT, "reason_code": "ValueError"}) + "\n",
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

    assert report["progress"]["event_counts"] == {"progress": 1, "start": 1}
    assert report["progress"]["last_index"] == 1
    assert report["progress"]["failed"] == 1
    assert report["progress"]["avg_checkpoint_elapsed_ms"] == 42
    assert report["dead_letter"] == {"count": 1, "failure_reasons": {"ValueError": 1}}
    assert PROJECT not in json.dumps(report, sort_keys=True)
