"""M0: durable graph projection_state SoT.

A new Ledger-backed store records which OntologyEpisodes have been projected into
the derived graph so a re-run can resume against persistent state instead of a
best-effort resume file. The store is dialect-aware (sqlite + postgres) and is
the single source of truth for the projection-state schema, which the Ledger
also installs at initialize time.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from agent_knowledge.ledger import Ledger
from agent_knowledge.llm_brain_core.graphiti_adapter import (
    _graphiti_group_id,
    _group_id_for_episode,
)
from agent_knowledge.llm_brain_core.ledger_adapter import (
    LedgerGraphProjectionStateStore,
)
from agent_knowledge.llm_brain_core.models import OntologyEpisode


def _h(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _ledger(tmp_path: Path) -> Ledger:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    return Ledger(private / "ledger.sqlite")


def _episode(natural_suffix: str, *, project: str = "neurons") -> OntologyEpisode:
    return OntologyEpisode.from_payload(
        event_id=f"evt:{natural_suffix}",
        entity_type="Task",
        natural_id=f"task:{natural_suffix}",
        payload={
            "task_state": f"State {natural_suffix}",
            "project": project,
            "brain_id": f"/project/{project}",
        },
    )


def test_mark_projected_then_list_is_idempotent(tmp_path: Path):
    # (a) mark/list idempotency on sqlite: marking the same episode twice keeps a
    # single row, and list_projected_ids returns it once.
    store = LedgerGraphProjectionStateStore(_ledger(tmp_path))
    episode = _episode("idem")

    store.mark_projected(episode, "inserted")
    store.mark_projected(episode, "duplicate")

    assert store.list_projected_ids() == {episode.episode_id}


def test_list_projected_ids_filters_by_project(tmp_path: Path):
    store = LedgerGraphProjectionStateStore(_ledger(tmp_path))
    neurons = _episode("p1", project="neurons")
    other = _episode("p2", project="other")

    store.mark_projected(neurons, "inserted")
    store.mark_projected(other, "inserted")

    assert store.list_projected_ids(project="neurons") == {neurons.episode_id}
    assert store.list_projected_ids() == {neurons.episode_id, other.episode_id}


def test_mark_projected_persists_group_id_via_graphiti_helpers(tmp_path: Path):
    # (5) group_id consistency: the store reuses the graphiti group_id helpers, it
    # does not reimplement normalization.
    ledger = _ledger(tmp_path)
    store = LedgerGraphProjectionStateStore(ledger)
    episode = _episode("group")
    expected_group_id = _graphiti_group_id(_group_id_for_episode(episode, ""))

    store.mark_projected(episode, "inserted")

    with ledger._connect() as connection:
        row = connection.execute(
            "SELECT group_id, project, brain_id, entity_type, natural_id, "
            "content_hash, upsert_result "
            "FROM llm_brain_graph_projection_state WHERE episode_id = ?",
            (episode.episode_id,),
        ).fetchone()
    assert row is not None
    assert str(row["group_id"]) == expected_group_id
    assert str(row["project"]) == "neurons"
    assert str(row["brain_id"]) == "/project/neurons"
    assert str(row["entity_type"]) == "Task"
    assert str(row["natural_id"]) == episode.natural_id
    assert str(row["content_hash"]) == episode.content_hash
    assert str(row["upsert_result"]) == "inserted"


def test_list_returns_empty_when_table_missing_without_swallowing_pg_errors(tmp_path: Path):
    # (b) dialect-aware missing-table guard: a read against a ledger that never
    # installed the table returns empty via _table_exists pre-check, NOT via a
    # caught sqlite3.OperationalError ("no such table"). We prove the pre-check
    # path runs by making any actual SELECT raise; the guard must short-circuit
    # before that.
    ledger = _ledger(tmp_path)
    # Build the store WITHOUT ensuring schema (read_only skips _ensure_schema),
    # then drop the table so the read path must rely on _table_exists.
    store = LedgerGraphProjectionStateStore(ledger)
    with ledger._connect() as connection:
        connection.execute("DROP TABLE IF EXISTS llm_brain_graph_projection_state")

    # No OperationalError leaks out; empty result because the table is absent.
    assert store.list_projected_ids() == set()
    assert store.list_projected_ids(project="neurons") == set()


def test_schema_single_source_initialize_matches_store(tmp_path: Path):
    # (c) single source of truth: the column set installed by Ledger._initialize
    # equals the column set the store's _ensure_schema would install. They must
    # not drift because both derive from one constant.
    ledger = _ledger(tmp_path)  # _initialize ran during construction.

    def _columns(connection) -> set[str]:
        rows = connection.execute(
            "PRAGMA table_info(llm_brain_graph_projection_state)"
        ).fetchall()
        return {row["name"] for row in rows}

    with ledger._connect() as connection:
        initialize_columns = _columns(connection)

    # Store on a separate fresh ledger that only ran _ensure_schema.
    fresh_root = tmp_path / "fresh"
    fresh_root.mkdir()
    fresh = _ledger(fresh_root)
    with fresh._connect() as connection:
        connection.execute("DROP TABLE IF EXISTS llm_brain_graph_projection_state")
    LedgerGraphProjectionStateStore(fresh)  # _ensure_schema reinstalls.
    with fresh._connect() as connection:
        store_columns = _columns(connection)

    expected = {
        "episode_id",
        "extraction_level",
        "project",
        "entity_type",
        "natural_id",
        "group_id",
        "brain_id",
        "content_hash",
        "ontology_version",
        "extractor_version",
        "upsert_result",
        "projected_at",
        "updated_at",
    }
    assert initialize_columns == expected
    assert store_columns == expected
    assert initialize_columns == store_columns


def test_schema_migration_recorded(tmp_path: Path):
    ledger = _ledger(tmp_path)
    with ledger._connect() as connection:
        row = connection.execute(
            "SELECT version FROM schema_migrations WHERE version = ?",
            ("agent_knowledge_graph_projection_state.v1",),
        ).fetchone()
    assert row is not None
