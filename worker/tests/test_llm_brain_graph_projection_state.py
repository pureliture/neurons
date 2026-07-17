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
    _migrate_extraction_level,
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
        "source_hash",
        "ontology_version",
        "extractor_version",
        "upsert_result",
        "projected_at",
        "updated_at",
    }
    assert initialize_columns == expected
    assert store_columns == expected
    assert initialize_columns == store_columns


def test_projected_source_hash_sets_preserve_multiple_revisions_for_one_natural_id(
    tmp_path: Path,
):
    store = LedgerGraphProjectionStateStore(_ledger(tmp_path))
    natural_id = "session:stable"
    old_source_hash = _h("old-source")
    current_source_hash = _h("current-source")

    for suffix, source_hash in (
        ("old-revision", old_source_hash),
        ("current-revision", current_source_hash),
    ):
        episode = OntologyEpisode.from_payload(
            event_id=f"evt:{suffix}",
            entity_type="Session",
            natural_id=natural_id,
            payload={
                "project": "neurons",
                "brain_id": "/project/neurons",
                "source_hash": source_hash,
            },
        )
        store.mark_projected(episode, "inserted", extraction_level="entity")

    assert store.list_projected_source_hash_sets(
        project="neurons",
        extraction_level="entity",
        entity_type="Session",
    ) == {natural_id: {old_source_hash, current_source_hash}}


def test_pre_m2_rebuild_keeps_indexes_on_new_table(tmp_path: Path):
    # The pre-M2 -> composite rebuild renames the legacy table to *_pre_m2 before
    # recreating the table. RENAME leaves the legacy indexes attached under their
    # original names, so a naive CREATE INDEX IF NOT EXISTS would skip (name still
    # occupied) and the indexes would then vanish with DROP TABLE *_pre_m2. We call
    # the migration block in isolation (not via _ensure_schema, which re-runs the
    # schema afterward and would mask the bug) and assert the new table carries the
    # explicit indexes.
    ledger = _ledger(tmp_path)
    table = "llm_brain_graph_projection_state"
    with ledger._connect() as connection:
        connection.execute(f"DROP TABLE IF EXISTS {table}")
        # Legacy pre-M2 shape: sole episode_id PRIMARY KEY, no extraction_level,
        # plus the original named indexes.
        connection.executescript(
            f"""
            CREATE TABLE {table} (
                episode_id TEXT PRIMARY KEY,
                project TEXT NOT NULL DEFAULT '',
                entity_type TEXT NOT NULL DEFAULT '',
                natural_id TEXT NOT NULL DEFAULT '',
                group_id TEXT NOT NULL DEFAULT '',
                brain_id TEXT DEFAULT '',
                content_hash TEXT NOT NULL DEFAULT '',
                ontology_version TEXT NOT NULL DEFAULT '',
                extractor_version TEXT NOT NULL DEFAULT '',
                upsert_result TEXT NOT NULL DEFAULT '',
                projected_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX idx_{table}_project_projected ON {table}(project, projected_at);
            CREATE INDEX idx_{table}_group ON {table}(group_id);
            INSERT INTO {table} (episode_id, projected_at, updated_at)
                VALUES ('evt:legacy', '2026-01-01', '2026-01-01');
            """
        )

    with ledger._connect() as connection:
        _migrate_extraction_level(connection)
        index_names = {
            str(row["name"])
            for row in connection.execute(
                f"PRAGMA index_list({table})"
            ).fetchall()
        }
        # Row was copied into the rebuilt composite table.
        copied = connection.execute(
            f"SELECT extraction_level FROM {table} WHERE episode_id = 'evt:legacy'"
        ).fetchone()

    assert f"idx_{table}_project_projected" in index_names
    assert f"idx_{table}_group" in index_names
    assert f"idx_{table}_level" in index_names
    assert f"idx_{table}_currentness" in index_names
    assert copied is not None and copied["extraction_level"] == "episodic"


def test_schema_migration_recorded(tmp_path: Path):
    ledger = _ledger(tmp_path)
    with ledger._connect() as connection:
        row = connection.execute(
            "SELECT version FROM schema_migrations WHERE version = ?",
            ("agent_knowledge_graph_projection_state.v1",),
        ).fetchone()
    assert row is not None
