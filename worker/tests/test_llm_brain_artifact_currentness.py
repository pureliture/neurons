from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from agent_knowledge.ledger import Ledger
from agent_knowledge.llm_brain_core import (
    InMemorySessionMemoryArtifactStore,
    LedgerSessionMemoryArtifactStore,
    SessionMemoryArtifact,
)
from agent_knowledge.llm_brain_core.runtime import _next_materialization_revision


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _artifact(
    *,
    revision: int,
    created_at: str,
    materialized_at: str,
    session_key: str = "stable-session",
    observed_at_start: str = "2026-07-09T00:00:00Z",
    observed_at_end: str = "2026-07-09T01:00:00Z",
    revision_observed_at_start: str = "",
    revision_observed_at_end: str = "",
    revision_temporal_evidence: str = "bounded",
) -> SessionMemoryArtifact:
    if revision_temporal_evidence == "bounded":
        revision_observed_at_start = (
            revision_observed_at_start or observed_at_start
        )
        revision_observed_at_end = revision_observed_at_end or observed_at_end
    return SessionMemoryArtifact.from_summary(
        session_id_hash=_hash(session_key),
        project="neurons",
        provider="codex",
        summary=f"Materialized snapshot revision {revision}",
        source_event_ids=[f"source-event-{session_key}-{revision}"],
        source_revision=_hash(f"{session_key}:{revision}"),
        observed_at_start=observed_at_start,
        observed_at_end=observed_at_end,
        revision_observed_at_start=revision_observed_at_start,
        revision_observed_at_end=revision_observed_at_end,
        revision_temporal_evidence=revision_temporal_evidence,
        materialized_at=materialized_at,
        materialization_revision=revision,
        created_at=created_at,
    )


@pytest.mark.parametrize("store_kind", ["memory", "ledger"])
def test_same_session_latest_snapshot_uses_materialization_revision(tmp_path, store_kind):
    if store_kind == "memory":
        store = InMemorySessionMemoryArtifactStore()
    else:
        store = LedgerSessionMemoryArtifactStore(Ledger(tmp_path / "ledger.sqlite3"))

    revision_one = _artifact(
        revision=1,
        created_at="2026-07-16T00:00:00Z",
        materialized_at="2026-07-16T00:00:00Z",
    )
    revision_two = _artifact(
        revision=2,
        created_at="2026-07-15T00:00:00Z",
        materialized_at="2026-07-15T00:00:00Z",
    )
    store.upsert(revision_one)
    store.upsert(revision_two)

    latest = store.list_recent(project="neurons", limit=1)

    assert latest[0].materialization_revision == 2
    assert latest[0].source_revision == revision_two.source_revision


@pytest.mark.parametrize("store_kind", ["memory", "ledger"])
def test_restored_source_revision_creates_a_new_materialization_occurrence(
    tmp_path, store_kind
):
    if store_kind == "memory":
        store = InMemorySessionMemoryArtifactStore()
    else:
        store = LedgerSessionMemoryArtifactStore(Ledger(tmp_path / "ledger.sqlite3"))

    common = {
        "session_id_hash": _hash("restored-session"),
        "project": "neurons",
        "provider": "codex",
        "summary": "Restorable canonical source snapshot",
        "observed_at_start": "2026-07-09T00:00:00Z",
        "observed_at_end": "2026-07-09T01:00:00Z",
        "revision_observed_at_start": "2026-07-09T00:00:00Z",
        "revision_observed_at_end": "2026-07-09T01:00:00Z",
        "revision_temporal_evidence": "bounded",
    }
    source_a = _hash("source-a")
    source_b = _hash("source-b")
    first_a = SessionMemoryArtifact.from_summary(
        **common,
        source_event_ids=["event-a"],
        source_revision=source_a,
        materialized_at="2026-07-16T00:00:01Z",
        materialization_revision=1,
        created_at="2026-07-16T00:00:01Z",
    )
    changed_b = SessionMemoryArtifact.from_summary(
        **common,
        source_event_ids=["event-b"],
        source_revision=source_b,
        materialized_at="2026-07-16T00:00:02Z",
        materialization_revision=2,
        created_at="2026-07-16T00:00:02Z",
    )
    restored_a = SessionMemoryArtifact.from_summary(
        **common,
        source_event_ids=["event-a"],
        source_revision=source_a,
        materialized_at="2026-07-16T00:00:03Z",
        materialization_revision=3,
        created_at="2026-07-16T00:00:03Z",
    )

    assert store.upsert(first_a) == "inserted"
    assert store.upsert(changed_b) == "inserted"
    assert store.upsert(restored_a) == "inserted"

    latest = store.get_latest_for_session(
        project="neurons",
        session_id_hash=common["session_id_hash"],
    )
    assert latest is not None
    assert latest.materialization_revision == 3
    assert latest.source_revision == source_a
    assert latest.artifact_id == restored_a.artifact_id
    assert restored_a.artifact_id != first_a.artifact_id


def test_ledger_initialize_additively_migrates_legacy_artifact_currentness_columns(tmp_path):
    ledger_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(ledger_path)
    connection.executescript(
        """
        CREATE TABLE llm_brain_session_memory_artifacts (
            artifact_id TEXT PRIMARY KEY,
            session_id_hash TEXT NOT NULL,
            project TEXT NOT NULL,
            provider TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            artifact_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    connection.close()

    ledger = Ledger(ledger_path)
    with ledger._connect() as migrated:
        columns = {
            str(row["name"])
            for row in migrated.execute(
                "PRAGMA table_info(llm_brain_session_memory_artifacts)"
                ).fetchall()
        }
        index_names = {
            str(row["name"])
            for row in migrated.execute(
                "PRAGMA index_list(llm_brain_session_memory_artifacts)"
            ).fetchall()
        }

    assert {
        "source_revision",
        "observed_at_start",
        "observed_at_end",
        "materialized_at",
        "materialization_revision",
    } <= columns
    assert "idx_llm_brain_artifacts_observed_currentness" in index_names


def test_fresh_ledger_read_only_artifact_store_uses_currentness_schema(tmp_path):
    ledger_path = tmp_path / "ledger.sqlite3"
    Ledger(ledger_path)

    store = LedgerSessionMemoryArtifactStore(Ledger.open_read_only(ledger_path))

    assert store.list_recent(project="neurons", limit=1) == []


def test_legacy_unmigrated_read_only_artifact_store_falls_back_to_json_currentness(
    tmp_path,
):
    ledger_path = tmp_path / "legacy-read-only.sqlite3"
    artifact = _artifact(
        revision=2,
        created_at="2026-07-11T00:00:00Z",
        materialized_at="2026-07-11T00:00:00Z",
    )
    connection = sqlite3.connect(ledger_path)
    connection.executescript(
        """
        CREATE TABLE llm_brain_session_memory_artifacts (
            artifact_id TEXT PRIMARY KEY,
            session_id_hash TEXT NOT NULL,
            project TEXT NOT NULL,
            provider TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            artifact_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    connection.execute(
        """
        INSERT INTO llm_brain_session_memory_artifacts (
            artifact_id, session_id_hash, project, provider, content_hash,
            artifact_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artifact.artifact_id,
            artifact.session_id_hash,
            artifact.project,
            artifact.provider,
            artifact.content_hash,
            json.dumps(artifact.to_dict(), sort_keys=True),
            artifact.created_at,
            artifact.materialized_at,
        ),
    )
    connection.commit()
    connection.close()

    store = LedgerSessionMemoryArtifactStore(Ledger.open_read_only(ledger_path))

    assert store.list_recent(project="neurons", limit=1)[0].artifact_id == artifact.artifact_id


@pytest.mark.parametrize("store_kind", ["memory", "ledger"])
def test_observed_interval_finds_artifact_older_than_global_recent_hundred(
    tmp_path, store_kind
):
    if store_kind == "memory":
        store = InMemorySessionMemoryArtifactStore()
    else:
        store = LedgerSessionMemoryArtifactStore(Ledger(tmp_path / "ledger.sqlite3"))

    target = _artifact(
        revision=1,
        session_key="historical-target",
        created_at="2026-07-09T01:00:00Z",
        materialized_at="2026-07-09T01:00:00Z",
    )
    store.upsert(target)
    for index in range(126):
        store.upsert(
            _artifact(
                revision=1,
                session_key=f"recent-{index:03d}",
                observed_at_start="2026-07-15T00:00:00Z",
                observed_at_end="2026-07-15T01:00:00Z",
                created_at=f"2026-07-15T01:{index // 60:02d}:{index % 60:02d}Z",
                materialized_at=f"2026-07-15T01:{index // 60:02d}:{index % 60:02d}Z",
            )
        )

    results = store.list_observed_interval(
        project="neurons",
        observed_at_start="2026-07-09T00:00:00Z",
        observed_at_end="2026-07-09T23:59:59Z",
        limit=10,
    )

    assert [artifact.artifact_id for artifact in results] == [target.artifact_id]


@pytest.mark.parametrize("store_kind", ["memory", "ledger"])
def test_observed_interval_returns_latest_materialization_per_session(
    tmp_path, store_kind
):
    if store_kind == "memory":
        store = InMemorySessionMemoryArtifactStore()
    else:
        store = LedgerSessionMemoryArtifactStore(Ledger(tmp_path / "ledger.sqlite3"))

    store.upsert(
        _artifact(
            revision=1,
            created_at="2026-07-10T00:00:00Z",
            materialized_at="2026-07-10T00:00:00Z",
        )
    )
    latest = _artifact(
        revision=2,
        created_at="2026-07-11T00:00:00Z",
        materialized_at="2026-07-11T00:00:00Z",
    )
    store.upsert(latest)

    results = store.list_observed_interval(
        project="neurons",
        observed_at_start="2026-07-09T00:30:00Z",
        observed_at_end="2026-07-09T00:30:00Z",
        limit=10,
    )

    assert [artifact.materialization_revision for artifact in results] == [2]
    assert results[0].artifact_id == latest.artifact_id


@pytest.mark.parametrize("store_kind", ["memory", "ledger"])
def test_observed_interval_selects_the_session_revision_observed_on_each_date(
    tmp_path, store_kind
):
    if store_kind == "memory":
        store = InMemorySessionMemoryArtifactStore()
    else:
        store = LedgerSessionMemoryArtifactStore(Ledger(tmp_path / "ledger.sqlite3"))

    date_a = _artifact(
        revision=1,
        created_at="2026-07-09T11:00:00Z",
        materialized_at="2026-07-09T11:00:00Z",
        observed_at_start="2026-07-09T10:00:00Z",
        observed_at_end="2026-07-09T10:30:00Z",
        revision_observed_at_start="2026-07-09T10:00:00Z",
        revision_observed_at_end="2026-07-09T10:30:00Z",
    )
    date_b = _artifact(
        revision=2,
        created_at="2026-07-15T11:00:00Z",
        materialized_at="2026-07-15T11:00:00Z",
        observed_at_start="2026-07-09T10:00:00Z",
        observed_at_end="2026-07-15T10:30:00Z",
        revision_observed_at_start="2026-07-15T10:00:00Z",
        revision_observed_at_end="2026-07-15T10:30:00Z",
    )
    store.upsert(date_a)
    store.upsert(date_b)

    result_a = store.list_observed_interval(
        project="neurons",
        observed_at_start="2026-07-09T10:15:00Z",
        observed_at_end="2026-07-09T10:15:00Z",
    )
    result_b = store.list_observed_interval(
        project="neurons",
        observed_at_start="2026-07-15T10:15:00Z",
        observed_at_end="2026-07-15T10:15:00Z",
    )

    assert [artifact.source_revision for artifact in result_a] == [date_a.source_revision]
    assert [artifact.source_revision for artifact in result_b] == [date_b.source_revision]


@pytest.mark.parametrize("store_kind", ["memory", "ledger"])
def test_observed_interval_never_falls_back_to_legacy_cumulative_snapshot(
    tmp_path, store_kind
):
    if store_kind == "memory":
        store = InMemorySessionMemoryArtifactStore()
    else:
        store = LedgerSessionMemoryArtifactStore(Ledger(tmp_path / "ledger.sqlite3"))

    legacy = SessionMemoryArtifact.from_summary(
        session_id_hash=_hash("legacy-temporal-session"),
        project="neurons",
        provider="codex",
        summary="Legacy cumulative snapshot without revision event evidence",
        source_event_ids=["legacy-source-event"],
        source_revision=_hash("legacy-temporal-session:1"),
        observed_at_start="2026-07-09T10:00:00Z",
        observed_at_end="2026-07-15T10:30:00Z",
        revision_temporal_evidence="legacy",
        materialized_at="2026-07-09T11:00:00Z",
        materialization_revision=1,
        created_at="2026-07-09T11:00:00Z",
    )
    corrected = _artifact(
        revision=2,
        session_key="legacy-temporal-session",
        observed_at_start="2026-07-09T10:00:00Z",
        observed_at_end="2026-07-15T10:30:00Z",
        revision_observed_at_start="2026-07-15T10:00:00Z",
        revision_observed_at_end="2026-07-15T10:30:00Z",
        materialized_at="2026-07-15T11:00:00Z",
        created_at="2026-07-15T11:00:00Z",
    )
    store.upsert(legacy)
    store.upsert(corrected)

    result_a = store.list_observed_interval(
        project="neurons",
        observed_at_start="2026-07-09T10:15:00Z",
        observed_at_end="2026-07-09T10:15:00Z",
    )
    result_b = store.list_observed_interval(
        project="neurons",
        observed_at_start="2026-07-15T10:15:00Z",
        observed_at_end="2026-07-15T10:15:00Z",
    )

    assert result_a == []
    assert [artifact.source_revision for artifact in result_b] == [
        corrected.source_revision
    ]


@pytest.mark.parametrize("store_kind", ["memory", "ledger"])
def test_observed_interval_rejects_reversed_range_even_when_store_is_empty(
    tmp_path, store_kind
):
    if store_kind == "memory":
        store = InMemorySessionMemoryArtifactStore()
    else:
        store = LedgerSessionMemoryArtifactStore(Ledger(tmp_path / "ledger.sqlite3"))

    with pytest.raises(ValueError, match="start must not be after end"):
        store.list_observed_interval(
            project="neurons",
            observed_at_start="2026-07-10T00:00:00Z",
            observed_at_end="2026-07-09T00:00:00Z",
        )


@pytest.mark.parametrize("store_kind", ["memory", "ledger"])
def test_next_materialization_revision_finds_session_beyond_recent_hundred(
    tmp_path, store_kind
):
    if store_kind == "memory":
        store = InMemorySessionMemoryArtifactStore()
    else:
        store = LedgerSessionMemoryArtifactStore(Ledger(tmp_path / "ledger.sqlite3"))

    target = _artifact(
        revision=2,
        session_key="older-target-session",
        created_at="2026-07-09T00:00:00Z",
        materialized_at="2026-07-09T00:00:00Z",
    )
    store.upsert(target)
    for index in range(126):
        store.upsert(
            _artifact(
                revision=3,
                session_key=f"higher-revision-{index:03d}",
                created_at="2026-07-15T00:00:00Z",
                materialized_at="2026-07-15T00:00:00Z",
            )
        )

    assert _next_materialization_revision(
        artifact_store=store,
        project="neurons",
        session_id_hash=target.session_id_hash,
        source_revision=_hash("new-source-revision"),
    ) == 3
