"""Hermes backfill admission tests: ro sqlite, manifest gate, strict writes.

All transcript text here is synthetic. No production CouchDB is used: every
store is in-memory and every CLI invocation is --dry-run.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from agent_knowledge.couchdb_source.hermes_admission import (
    HERMES_ADMISSION_SCHEMA,
    HERMES_CUTOFF,
    hermes_session_id_hash,
    load_hermes_admission_manifest,
    materialize_hermes_fixtures,
    plan_hermes_import,
)
from agent_knowledge.couchdb_source.historical_import import (
    ImportStatus,
    SourceLocator,
    import_historical_source,
)
from agent_knowledge.couchdb_source.migration_cli import main, run_migration
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore

_CUTOFF_EPOCH = datetime(2026, 11, 25, tzinfo=timezone.utc).timestamp()
_BEFORE = _CUTOFF_EPOCH - 86400


def _db(tmp_path, *, sessions):
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, "
        "started_at REAL NOT NULL, ended_at REAL, cwd TEXT, "
        "message_count INTEGER DEFAULT 0)"
    )
    connection.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT, "
        "timestamp REAL NOT NULL, active INTEGER DEFAULT 1, "
        "compacted INTEGER DEFAULT 0)"
    )
    for session_id, count in sessions:
        connection.execute(
            "INSERT INTO sessions (id, source, started_at, ended_at, cwd, message_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, "hermes", _BEFORE, _BEFORE, "/work/project", count),
        )
        for index in range(count):
            connection.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                (session_id, "user" if index % 2 == 0 else "assistant", f"synthetic-{index}", _BEFORE + index),
            )
    connection.commit()
    connection.close()
    return path


def _manifest(tmp_path, entries, *, name="manifest.json"):
    path = tmp_path / name
    path.write_text(
        json.dumps(
            {
                "schema_version": HERMES_ADMISSION_SCHEMA,
                "cutoff": HERMES_CUTOFF,
                "entries": [
                    {"session_id_hash": session_id_hash, "message_count": count}
                    for session_id_hash, count in entries
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _fixture_for(db, manifest, runtime_dir, session_id):
    plan = plan_hermes_import(db, load_hermes_admission_manifest(manifest))
    paths = materialize_hermes_fixtures(db, plan, runtime_dir)
    wanted = hermes_session_id_hash(session_id)
    return next(path for path in paths if wanted in path.name)


def test_plan_hermes_import_is_manifest_gated_and_read_only(tmp_path):
    db = _db(tmp_path, sessions=[("s1", 2), ("s2", 1), ("s3", 1)])
    manifest = _manifest(
        tmp_path,
        [(hermes_session_id_hash("s1"), 2), (hermes_session_id_hash("s3"), 99)],
    )
    loaded = load_hermes_admission_manifest(manifest)
    plan = plan_hermes_import(db, loaded)

    assert [session.session_id_hash for session in plan.sessions] == [hermes_session_id_hash("s1")]
    assert [reason for _, reason in plan.rejected] == ["message_count_mismatch"]
    assert plan.manifest_misses == 1
    assert not list(tmp_path.glob("state.db-*"))


def test_load_manifest_rejects_invalid_shape(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema_version": HERMES_ADMISSION_SCHEMA}), encoding="utf-8")
    try:
        load_hermes_admission_manifest(bad)
    except ValueError as exc:
        assert str(exc) == "hermes_admission_manifest_invalid"
    else:
        raise AssertionError("invalid manifest was admitted")


def test_run_migration_hermes_dry_run_imports_only_admitted_session(tmp_path):
    db = _db(tmp_path, sessions=[("s1", 2), ("s2", 1)])
    manifest = _manifest(tmp_path, [(hermes_session_id_hash("s1"), 2)])
    store = InMemoryCouchDBSourceStore()

    report = run_migration(
        store=store,
        roots={"hermes": db},
        providers=["hermes"],
        runtime_dir=tmp_path / "runtime",
        hermes_manifest=load_hermes_admission_manifest(manifest),
        strict_put_if_absent=True,
        dry_run=True,
    )

    assert report["imported"] == 1
    assert report["errors"] == 0
    assert report["by_provider"]["hermes"]["manifest_entries"] == 1
    sessions = store.find_by_type("transcript_session", fields=["session_id_hash"])
    assert [doc["session_id_hash"] for doc in sessions] == [hermes_session_id_hash("s1")]
    assert store.find_by_type("conversation_chunk")


def test_run_migration_hermes_count_mismatch_fails_closed(tmp_path):
    db = _db(tmp_path, sessions=[("s3", 1)])
    manifest = _manifest(tmp_path, [(hermes_session_id_hash("s3"), 99)])
    store = InMemoryCouchDBSourceStore()

    report = run_migration(
        store=store,
        roots={"hermes": db},
        providers=["hermes"],
        runtime_dir=tmp_path / "runtime",
        hermes_manifest=load_hermes_admission_manifest(manifest),
        strict_put_if_absent=True,
        dry_run=True,
    )

    assert report["imported"] == 0
    assert report["errors"] == 2  # count mismatch + unadmitted manifest entry
    assert not store.find_by_type("transcript_session")


def test_strict_put_if_absent_preserves_existing_documents(tmp_path):
    db = _db(tmp_path, sessions=[("s1", 2)])
    manifest = _manifest(tmp_path, [(hermes_session_id_hash("s1"), 2)])
    fixture = _fixture_for(db, manifest, tmp_path / "runtime", "s1")
    store = InMemoryCouchDBSourceStore()
    locator = SourceLocator(
        provider="hermes",
        source_path=str(fixture),
        capture_metadata_project="/work/project",
    )

    first = import_historical_source(locator=locator, store=store, strict_put_if_absent=True)
    assert first.status == ImportStatus.IMPORTED
    chunk_count = len(store.find_by_type("conversation_chunk"))

    second = import_historical_source(locator=locator, store=store, strict_put_if_absent=True)
    assert second.status == ImportStatus.SOURCE_STORE_CONFLICT
    assert "existing_source_document_preserved" in second.notes
    assert len(store.find_by_type("conversation_chunk")) == chunk_count


def test_main_dry_run_and_live_argv_gates(tmp_path, capsys):
    db = _db(tmp_path, sessions=[("s1", 2)])
    manifest = _manifest(tmp_path, [(hermes_session_id_hash("s1"), 2)])

    rc = main(
        [
            "--provider", "hermes",
            "--hermes-admission-manifest", str(manifest),
            "--source-root", f"hermes={db}",
            "--runtime-dir", str(tmp_path / "runtime"),
            "--dry-run",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["status"] == "ok"
    assert report["imported"] == 1

    rc = main(["--provider", "hermes", "--hermes-admission-manifest", str(manifest)])
    report = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert report["error_class"] == "hermes_requires_strict_put_if_absent"

    rc = main(["--provider", "hermes", "--dry-run"])
    report = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert report["error_class"] == "hermes_admission_manifest_required"


def test_main_migration_exit_code_reflects_partial_failure(monkeypatch, capsys):
    import agent_knowledge.couchdb_source.migration_cli as cli

    monkeypatch.setattr(
        cli,
        "run_migration",
        lambda **_: {"dry_run": True, "by_provider": {}, "imported": 0, "ambiguous": 0, "mismatch": 0, "errors": 1},
    )
    rc = cli.main(["--dry-run"])
    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "error"
