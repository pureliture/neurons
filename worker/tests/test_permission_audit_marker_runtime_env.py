from __future__ import annotations

import json
from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
import stat
import urllib.parse

import pytest

from agent_knowledge.permission_audit import IndependentProductMutationMarkerReader


def _sha(character: str) -> str:
    return "sha256:" + character * 64


def _raw_sha(character: str) -> str:
    return character * 64


def _config() -> dict[str, object]:
    from agent_knowledge.permission_audit_marker_runtime_env import (
        _nats_consumer_generation_hash,
        _nats_stream_generation_hash,
    )

    native_common = {
        "storage_identity": "fixed-production-scope",
        "schema_generation": _sha("a"),
        "config_generation": _sha("b"),
        "writer_registry": ["fixed-writer"],
    }
    return {
        "schema_version": "permission_audit_marker_runtime.v1",
        "scopes": {
            "authority_ledger": {
                "credential_scope": "postgres_exact_marker_read_only",
                "schema_generation": _sha("c"),
                "writer_roles": ["neurons_writer"],
                "marker_owner_role": "neurons_marker_owner",
                "audit_reader_role": "neurons_marker_reader",
                "advisory_lock_key": 7211740091,
                "approved_privileged_roles": ["postgres_bootstrap"],
                "privileged_credential_inventory_anchor_hash": _sha("8"),
            },
            "corpus": {
                **native_common,
                "credential_scope": "couchdb_db_info_read_only",
                "reader_contract": "couchdb_db_info_only.v1",
                "base_url": "https://couchdb.invalid",
                "database": "fixed_corpus",
                "continuity_anchor_hash": _sha("6"),
            },
            "queue": {
                **native_common,
                "credential_scope": "nats_stream_consumer_metadata_read_only",
                "reader_contract": "nats_stream_consumer_metadata_only.v1",
                "server_url": "tls://nats.invalid:4222",
                "stream": "FIXED_STREAM",
                "durables": ["FIXED_DURABLE"],
                "publisher_authorization_anchor_hash": _sha("7"),
                "stream_generation_hash": _nats_stream_generation_hash(
                    created="fixture-stream-generation",
                    stream="FIXED_STREAM",
                    subjects=("fixed-writer",),
                ),
                "durable_generation_hashes": [
                    _nats_consumer_generation_hash(
                        created="fixture-consumer-generation",
                        stream="FIXED_STREAM",
                        durable="FIXED_DURABLE",
                    )
                ],
            },
            "product_db": {
                **native_common,
                "credential_scope": "sqlite_mode_ro_query_only",
                "reader_contract": "sqlite_pragma_metadata_only.v1",
                "path": "/fixed/product-state.sqlite3",
                "expected_schema_version": 12,
            },
        },
    }


def _environment() -> dict[str, str]:
    return {
        "NEURONS_PERMISSION_AUDIT_MARKER_CONFIG": json.dumps(_config()),
        "NEURONS_PERMISSION_AUDIT_PG_DSN": "postgresql://private.invalid/db",
        "NEURONS_PERMISSION_AUDIT_COUCHDB_AUTH_HEADER": "Bearer private-couch",
        "NEURONS_PERMISSION_AUDIT_NATS_TOKEN": "private-nats",
    }


def test_valid_fixed_four_environment_builds_reader_without_storage_calls():
    from agent_knowledge.permission_audit_marker_runtime_env import (
        build_production_permission_audit_marker_reader,
    )

    reader = build_production_permission_audit_marker_reader(_environment())

    assert isinstance(reader, IndependentProductMutationMarkerReader)


def test_runtime_assembles_fixed_adapters_lazily_and_closes_resources(
    monkeypatch,
):
    import agent_knowledge.permission_audit_marker_runtime_env as runtime_env

    calls: list[tuple[str, object]] = []

    class Couch:
        def read_database_info(self):
            raise AssertionError("delegate fake must not read storage")

    class Nats:
        def read_stream_and_durable_metadata(self):
            raise AssertionError("delegate fake must not read storage")

        def close(self):
            calls.append(("close", "nats"))

    class SQLiteConnector:
        def open(self, *_args, **_kwargs):
            raise AssertionError("delegate fake must not read storage")

        def close(self):
            calls.append(("close", "sqlite"))

    class SQLiteInspector:
        def inspect(self, _path):
            raise AssertionError("delegate fake must not read storage")

    @dataclass
    class Delegate:
        def run_audit_window(self, action):
            calls.append(("delegate", "run"))
            return {"before": True}, action(), {"after": True}

    def fake_reader_builder(**kwargs):
        calls.append(("reader", tuple(sorted(kwargs))))
        return Delegate()

    monkeypatch.setattr(
        runtime_env,
        "build_permission_audit_marker_reader",
        fake_reader_builder,
        raising=False,
    )
    factories = runtime_env.ProductionMarkerAdapterFactories(
        postgres_connection=lambda dsn: calls.append(("postgres", dsn)),
        couchdb_adapter=lambda scope, credential: (
            calls.append(("couchdb", credential)) or Couch()
        ),
        nats_adapter=lambda scope, credential, generation_hash: (
            calls.append(("nats", credential)) or Nats()
        ),
        sqlite_connector=lambda: SQLiteConnector(),
        sqlite_file_inspector=lambda: SQLiteInspector(),
    )

    reader = runtime_env.build_production_permission_audit_marker_reader(
        _environment(),
        factories=factories,
    )
    assert calls == []

    result = reader.run_audit_window(lambda: "stored")

    assert result == ({"before": True}, "stored", {"after": True})
    assert [name for name, _ in calls] == [
        "couchdb",
        "nats",
        "reader",
        "delegate",
        "close",
        "close",
    ]
    assert all("private" not in str(value) for name, value in calls if name == "close")


def test_partial_runtime_assembly_failure_closes_already_created_resources():
    import agent_knowledge.permission_audit_marker_runtime_env as runtime_env

    calls = []

    class Couch:
        def read_database_info(self):
            return {}

    class Nats:
        def read_stream_and_durable_metadata(self):
            return {}

        def close(self):
            calls.append("nats_closed")

    factories = runtime_env.ProductionMarkerAdapterFactories(
        postgres_connection=lambda *_args, **_kwargs: object(),
        couchdb_adapter=lambda *_args: Couch(),
        nats_adapter=lambda *_args: Nats(),
        sqlite_connector=lambda: (_ for _ in ()).throw(
            RuntimeError("protected-fixture-value")
        ),
        sqlite_file_inspector=lambda: object(),
    )
    reader = runtime_env.build_production_permission_audit_marker_reader(
        _environment(),
        factories=factories,
    )

    with pytest.raises(runtime_env.PermissionAuditMarkerEnvironmentError) as error:
        reader.run_audit_window(lambda: None)

    assert str(error.value) == "permission audit exact marker runtime unavailable"
    assert "protected-fixture-value" not in str(error.value)
    assert calls == ["nats_closed"]


def test_couchdb_adapter_reads_only_fixed_database_info():
    from agent_knowledge.permission_audit_marker_runtime_env import (
        _CouchDBInfoAdapter,
    )

    calls = []

    def transport(method, url, headers, *, timeout_seconds):
        calls.append((method, url, set(headers), timeout_seconds))
        return {
            "db_name": "fixed_corpus",
            "update_seq": "12-fixture",
            "purge_seq": 0,
            "doc_count": 7,
            "doc_del_count": 2,
            "sizes": {"file": 4096, "external": 2048, "active": 2048},
            "compact_running": False,
            "disk_format_version": 8,
        }

    adapter = _CouchDBInfoAdapter(
        {
            "base_url": "https://couchdb.invalid",
            "database": "fixed_corpus",
            "continuity_anchor_hash": _sha("6"),
        },
        "Bearer fixture-value",
        transport=transport,
        continuity_anchor_reader=lambda: _sha("6"),
    )

    assert adapter.read_database_info() == {
        "continuity_anchor_hash": _sha("6"),
        "update_seq": "12-fixture",
        "purge_seq": 0,
        "doc_count": 7,
        "doc_del_count": 2,
    }
    assert calls == [
        (
            "GET",
            "https://couchdb.invalid/fixed_corpus",
            {"Accept", "Authorization"},
            5,
        )
    ]


def test_couchdb_adapter_requires_external_continuity_anchor_before_network():
    from agent_knowledge.permission_audit_marker_runtime_env import (
        PermissionAuditMarkerEnvironmentError,
        _CouchDBInfoAdapter,
    )

    calls = []
    adapter = _CouchDBInfoAdapter(
        {
            "base_url": "https://couchdb.invalid",
            "database": "fixed_corpus",
            "continuity_anchor_hash": _sha("6"),
        },
        "Bearer fixture-value",
        transport=lambda *_args, **_kwargs: calls.append("network") or {},
    )

    with pytest.raises(PermissionAuditMarkerEnvironmentError):
        adapter.read_database_info()

    assert calls == []


def test_sqlite_connector_is_mode_ro_query_only_and_pragma_allowlisted(tmp_path):
    from agent_knowledge.permission_audit_marker_runtime_env import (
        PermissionAuditMarkerEnvironmentError,
        _SQLiteFileInspector,
        _SQLitePinnedFile,
        _SQLiteReadOnlyConnector,
    )

    database = tmp_path / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE fixture (value TEXT)")

    pinned = _SQLitePinnedFile()
    inspector = _SQLiteFileInspector(pinned)
    connector = _SQLiteReadOnlyConnector(pinned)
    inspected = inspector.inspect(database)
    uri = pinned.connection_uri(database)
    connection = connector.open(database, mode="ro", query_only=True)

    assert str(database) not in uri
    assert uri.startswith("file:") and uri.endswith("?mode=ro")
    assert "neurons-sqlite-marker-ephemeral-" in uri
    assert connection.connection_identity_hash == inspected["file_identity_hash"]
    assert set(inspected) == {
        "is_symlink",
        "is_regular",
        "directory_identity_hash",
        "file_identity_hash",
        "permission_hash",
        "sidecar_identity_hash",
    }
    assert connection.read_pragma("query_only") == 1
    assert all(
        isinstance(connection.read_pragma(name), int)
        for name in ("data_version", "page_count", "schema_version", "freelist_count")
    )
    with pytest.raises(PermissionAuditMarkerEnvironmentError):
        connection.read_pragma("table_list")

    snapshot_path = Path(urllib.parse.unquote(urllib.parse.urlsplit(uri).path))
    assert snapshot_path.is_file()
    assert stat.S_IMODE(snapshot_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(snapshot_path.stat().st_mode) == 0o600
    connector.close()
    assert not snapshot_path.parent.exists()
    with pytest.raises(PermissionAuditMarkerEnvironmentError):
        connection.read_pragma("data_version")


@pytest.mark.parametrize("failure", ("symlink", "hardlink", "path_swap"))
def test_sqlite_pinned_file_rejects_alias_or_replacement(tmp_path, failure):
    from agent_knowledge.permission_audit_marker_runtime_env import (
        PermissionAuditMarkerEnvironmentError,
        _SQLiteFileInspector,
        _SQLitePinnedFile,
        _SQLiteReadOnlyConnector,
    )

    original = tmp_path / "original.sqlite3"
    with sqlite3.connect(original) as connection:
        connection.execute("CREATE TABLE fixture (value TEXT)")
    candidate = tmp_path / "candidate.sqlite3"
    if failure == "symlink":
        candidate.symlink_to(original)
    elif failure == "hardlink":
        candidate.hardlink_to(original)
    else:
        candidate = original

    pinned = _SQLitePinnedFile()
    inspector = _SQLiteFileInspector(pinned)
    connector = _SQLiteReadOnlyConnector(pinned)
    if failure == "path_swap":
        inspector.inspect(candidate)
        moved = tmp_path / "moved.sqlite3"
        candidate.rename(moved)
        with sqlite3.connect(candidate) as connection:
            connection.execute("CREATE TABLE replacement (value TEXT)")

    with pytest.raises(PermissionAuditMarkerEnvironmentError):
        connector.open(candidate, mode="ro", query_only=True)


def test_sqlite_connect_rejects_swap_and_restore_race_without_path_reopen(
    tmp_path,
    monkeypatch,
):
    import agent_knowledge.permission_audit_marker_runtime_env as runtime_env

    database = tmp_path / "state.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    for path, table in ((database, "expected"), (replacement, "replacement")):
        with sqlite3.connect(path) as connection:
            connection.execute(f"CREATE TABLE {table} (value TEXT)")

    pinned = runtime_env._SQLitePinnedFile()
    inspector = runtime_env._SQLiteFileInspector(pinned)
    connector = runtime_env._SQLiteReadOnlyConnector(pinned)
    inspector.inspect(database)
    real_connect = runtime_env.sqlite3.connect
    observed_uris: list[str] = []

    def racing_connect(database_uri, **kwargs):
        observed_uris.append(str(database_uri))
        parked = tmp_path / "parked.sqlite3"
        database.rename(parked)
        replacement.rename(database)
        try:
            return real_connect(database_uri, **kwargs)
        finally:
            database.rename(replacement)
            parked.rename(database)

    monkeypatch.setattr(runtime_env.sqlite3, "connect", racing_connect)
    try:
        with pytest.raises(runtime_env.PermissionAuditMarkerEnvironmentError):
            connector.open(database, mode="ro", query_only=True)
    finally:
        connector.close()

    assert len(observed_uris) == 1
    assert str(database) not in observed_uris[0]
    assert observed_uris[0].startswith("file:")
    assert "neurons-sqlite-marker-ephemeral-" in observed_uris[0]


def test_sqlite_wal_uncheckpointed_external_commit_is_seen_without_sleep(tmp_path):
    from agent_knowledge.native_exact_mutation_markers import (
        NativeExactMutationMarkerContract,
        SQLiteExactMutationMarkerProvider,
    )
    from agent_knowledge.permission_audit_marker_runtime_env import (
        _SQLiteFileInspector,
        _SQLitePinnedFile,
        _SQLiteReadOnlyConnector,
    )

    database = tmp_path / "wal-state.sqlite3"
    writer = sqlite3.connect(database)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE fixture (value TEXT)")
    writer.commit()
    assert database.with_name(database.name + "-wal").is_file()
    assert database.with_name(database.name + "-shm").is_file()
    schema_version = writer.execute("PRAGMA schema_version").fetchone()[0]

    pinned = _SQLitePinnedFile()
    connector = _SQLiteReadOnlyConnector(pinned)
    provider = SQLiteExactMutationMarkerProvider(
        NativeExactMutationMarkerContract(
            plane="product_db",
            storage_identity="fixed-production-scope",
            schema_generation=_sha("a"),
            config_generation=_sha("b"),
            reader_contract="sqlite_mode_ro_query_only.v1",
            writer_registry=("fixed-writer",),
        ),
        path=database,
        connector=connector,
        file_inspector=_SQLiteFileInspector(pinned),
        expected_schema_version=schema_version,
    )

    before = provider.read_marker()
    writer.execute("INSERT INTO fixture(value) VALUES ('bounded')")
    writer.commit()
    after = provider.read_marker()

    assert before["event_position_hash"] != after["event_position_hash"]
    assert before["marker_hash"] != after["marker_hash"]
    connector.close()
    writer.close()


def test_sqlite_private_snapshot_refresh_preserves_inode_and_commit_boundary(
    tmp_path,
    monkeypatch,
):
    import agent_knowledge.permission_audit_marker_runtime_env as runtime_env

    database = tmp_path / "wal-state.sqlite3"
    writer = sqlite3.connect(database)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE fixture (value TEXT)")
    writer.commit()

    pinned = runtime_env._SQLitePinnedFile()
    runtime_env._SQLiteFileInspector(pinned).inspect(database)
    connector = runtime_env._SQLiteReadOnlyConnector(pinned)
    real_open = runtime_env.os.open
    forbidden_source_reopens: list[object] = []

    def guarded_open(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(database) or (
            os.fspath(path) == database.name
            and kwargs.get("dir_fd") == pinned._directory_fd
        ):
            forbidden_source_reopens.append(path)
        return real_open(path, *args, **kwargs)

    def reject_python_raw_read(*_args, **_kwargs):
        raise AssertionError("private snapshot must use kernel copy")

    monkeypatch.setattr(runtime_env.os, "open", guarded_open)
    monkeypatch.setattr(runtime_env.os, "read", reject_python_raw_read)
    monkeypatch.setattr(runtime_env.os, "pread", reject_python_raw_read)

    connection = connector.open(database, mode="ro", query_only=True)
    uri = pinned.connection_uri(database)
    snapshot_path = Path(urllib.parse.unquote(urllib.parse.urlsplit(uri).path))
    snapshot_inode = snapshot_path.stat().st_ino
    assert connection.read_pragma("query_only") == 1
    before = connection.read_pragma("data_version")

    writer.execute("INSERT INTO fixture(value) VALUES ('uncommitted')")
    assert connection.read_pragma("query_only") == 1
    during = connection.read_pragma("data_version")
    assert during == before

    writer.commit()
    assert connection.read_pragma("query_only") == 1
    after = connection.read_pragma("data_version")

    assert after > during
    assert snapshot_path.stat().st_ino == snapshot_inode
    assert forbidden_source_reopens == []
    connector.close()
    writer.close()


def test_sqlite_wal_checksum_corruption_fails_via_private_snapshot_pragma(
    tmp_path,
    monkeypatch,
    capsys,
):
    from agent_knowledge.native_exact_mutation_markers import (
        NativeExactMutationMarkerContract,
        NativeExactMutationMarkerError,
        SQLiteExactMutationMarkerProvider,
    )
    import agent_knowledge.permission_audit_marker_runtime_env as runtime_env

    database = tmp_path / "wal-checksum.sqlite3"
    writer = sqlite3.connect(database)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("CREATE TABLE fixture (value TEXT)")
    writer.execute("INSERT INTO fixture(value) VALUES ('committed')")
    writer.commit()
    schema_version = writer.execute("PRAGMA schema_version").fetchone()[0]

    pinned = runtime_env._SQLitePinnedFile()
    runtime_env._SQLiteFileInspector(pinned).inspect(database)
    wal_fd = pinned._sidecar_fds["-wal"]
    header = os.pread(wal_fd, 32, 0)
    page_size = int.from_bytes(header[8:12], "big")
    if page_size == 1:
        page_size = 65_536
    frame_size = 24 + page_size
    wal_length = os.fstat(wal_fd).st_size
    frame_count = (wal_length - 32) // frame_size
    assert frame_count > 0
    checksum_offset = 32 + (frame_count - 1) * frame_size + 16
    checksum_byte = os.pread(wal_fd, 1, checksum_offset)
    assert len(checksum_byte) == 1
    writable_wal = os.open(str(database) + "-wal", os.O_WRONLY)
    try:
        os.pwrite(
            writable_wal,
            bytes((checksum_byte[0] ^ 0x01,)),
            checksum_offset,
        )
    finally:
        os.close(writable_wal)

    original_connect = runtime_env._connect_sqlite_uri
    connection_attempts: list[str] = []

    def recording_connect(database_uri: str):
        connection_attempts.append(database_uri)
        return original_connect(database_uri)

    monkeypatch.setattr(
        runtime_env,
        "_connect_sqlite_uri",
        recording_connect,
    )
    connector = runtime_env._SQLiteReadOnlyConnector(pinned)
    provider = SQLiteExactMutationMarkerProvider(
        NativeExactMutationMarkerContract(
            plane="product_db",
            storage_identity="fixed-production-scope",
            schema_generation=_sha("a"),
            config_generation=_sha("b"),
            reader_contract="sqlite_mode_ro_query_only.v1",
            writer_registry=("fixed-writer",),
        ),
        path=database,
        connector=connector,
        file_inspector=runtime_env._SQLiteFileInspector(pinned),
        expected_schema_version=schema_version,
    )

    with pytest.raises(NativeExactMutationMarkerError) as error:
        provider.read_marker()

    assert str(error.value) == "SQLite exact marker schema drift detected"
    assert len(connection_attempts) == 1
    assert str(database) not in connection_attempts[0]
    assert "neurons-sqlite-marker-ephemeral-" in connection_attempts[0]
    assert str(database) not in str(error.value)
    # A single byte's bare hex text can naturally occur in ordinary words
    # (for example, ``ed`` in ``detected``). Check representations that would
    # actually disclose the private byte instead of a substring coincidence.
    assert repr(checksum_byte) not in str(error.value)
    assert f"0x{checksum_byte.hex()}" not in str(error.value)
    assert capsys.readouterr() == ("", "")
    connector.close()
    writer.close()


def test_sqlite_ephemeral_snapshot_is_not_provider_public_projection(
    tmp_path,
    capsys,
):
    from agent_knowledge.native_exact_mutation_markers import (
        NativeExactMutationMarkerContract,
        SQLiteExactMutationMarkerProvider,
    )
    from agent_knowledge.permission_audit_marker_runtime_env import (
        _SQLiteFileInspector,
        _SQLitePinnedFile,
        _SQLiteReadOnlyConnector,
    )

    protected_row_value = "protected-row-value-must-never-be-projected"
    database = tmp_path / "wal-private-snapshot.sqlite3"
    writer = sqlite3.connect(database)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE fixture (value TEXT)")
    writer.execute(
        "INSERT INTO fixture(value) VALUES (?)",
        (protected_row_value,),
    )
    writer.commit()
    schema_version = writer.execute("PRAGMA schema_version").fetchone()[0]
    source_entries = {item.name for item in tmp_path.iterdir()}

    pinned = _SQLitePinnedFile()
    connector = _SQLiteReadOnlyConnector(pinned)
    provider = SQLiteExactMutationMarkerProvider(
        NativeExactMutationMarkerContract(
            plane="product_db",
            storage_identity="fixed-production-scope",
            schema_generation=_sha("a"),
            config_generation=_sha("b"),
            reader_contract="sqlite_mode_ro_query_only.v1",
            writer_registry=("fixed-writer",),
        ),
        path=database,
        connector=connector,
        file_inspector=_SQLiteFileInspector(pinned),
        expected_schema_version=schema_version,
    )

    marker = provider.read_marker()
    snapshot_directory = Path(str(pinned._snapshot_directory))
    snapshot_generation_hash = str(pinned._snapshot_generation_hash)
    public_projection = json.dumps(marker, sort_keys=True)

    # This source-layer contract owns the provider projection and verifies that
    # no source-adjacent output file appears. Archive and public-console
    # enforcement remain the ops collector's cross-repository responsibility.
    assert snapshot_directory.is_dir()
    assert "neurons-sqlite-marker-ephemeral-" in snapshot_directory.name
    for protected in (
        protected_row_value,
        str(database),
        str(snapshot_directory),
        snapshot_generation_hash,
        "marker.sqlite3-wal",
        "marker.sqlite3-shm",
    ):
        assert protected not in public_projection
    assert capsys.readouterr() == ("", "")

    connector.close()
    assert not snapshot_directory.exists()
    assert {item.name for item in tmp_path.iterdir()} == source_entries
    writer.close()


@pytest.mark.parametrize("suffix", ("-wal", "-shm"))
def test_sqlite_pinned_file_rejects_wal_or_shm_replacement(tmp_path, suffix):
    from agent_knowledge.permission_audit_marker_runtime_env import (
        PermissionAuditMarkerEnvironmentError,
        _SQLiteFileInspector,
        _SQLitePinnedFile,
    )

    database = tmp_path / "wal-state.sqlite3"
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE fixture (value TEXT)")
    writer.commit()
    sidecar = database.with_name(database.name + suffix)
    assert sidecar.is_file()
    pinned = _SQLitePinnedFile()
    inspector = _SQLiteFileInspector(pinned)
    inspector.inspect(database)

    moved = database.with_name(database.name + suffix + ".moved")
    sidecar.rename(moved)
    sidecar.write_bytes(moved.read_bytes())

    with pytest.raises(PermissionAuditMarkerEnvironmentError):
        inspector.inspect(database)

    pinned.close()
    writer.close()


def test_sqlite_pinning_fails_when_safe_dirfd_primitives_are_unsupported(
    monkeypatch,
    tmp_path,
):
    import agent_knowledge.permission_audit_marker_runtime_env as runtime_env

    database = tmp_path / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE fixture (value TEXT)")
    monkeypatch.setattr(runtime_env.os, "O_DIRECTORY", None)

    with pytest.raises(runtime_env.PermissionAuditMarkerEnvironmentError):
        runtime_env._SQLitePinnedFile().inspect(database)


def test_postgres_connection_is_read_only_and_exact_sql_allowlisted():
    from agent_knowledge.permission_audit_marker_runtime_env import (
        PermissionAuditMarkerEnvironmentError,
        _open_postgres_read_only,
        _postgres_exact_read_statements,
    )
    from agent_knowledge.postgres_exact_mutation_marker import (
        PostgresExactMutationMarkerReader,
        build_source_owned_postgres_exact_marker_contract,
    )

    calls = []

    class RawConnection:
        def execute(self, statement):
            calls.append(("execute", statement))
            return object()

        def close(self):
            calls.append(("close", None))

    raw = RawConnection()

    def connect(dsn, **kwargs):
        calls.append(("connect", set(kwargs)))
        assert dsn == "postgresql://fixture.invalid/db"
        assert kwargs["autocommit"] is True
        assert "default_transaction_read_only=on" in kwargs["options"]
        return raw

    pg_scope = _config()["scopes"]["authority_ledger"]
    contract = build_source_owned_postgres_exact_marker_contract(
        schema_generation=pg_scope["schema_generation"],
        writer_roles=tuple(pg_scope["writer_roles"]),
        marker_owner_role=pg_scope["marker_owner_role"],
        audit_reader_role=pg_scope["audit_reader_role"],
        advisory_lock_key=pg_scope["advisory_lock_key"],
        approved_privileged_roles=tuple(pg_scope["approved_privileged_roles"]),
        privileged_credential_inventory_anchor_hash=pg_scope[
            "privileged_credential_inventory_anchor_hash"
        ],
    )
    allowed = _postgres_exact_read_statements(contract)
    connection = _open_postgres_read_only(
        "postgresql://fixture.invalid/db",
        connect=connect,
        allowed_statements=allowed,
        expected_privileged_credential_inventory_anchor_hash=_sha("8"),
        privileged_credential_inventory_anchor_reader=lambda: _sha("8"),
    )

    assert connection.dialect == "postgres"
    connection.execute(PostgresExactMutationMarkerReader._ACQUIRE_SQL)
    dynamic_statements = [
        statement
        for statement in allowed
        if "postgres_exact_marker:roles" in statement
        or "postgres_exact_marker:privileged_roles" in statement
        or "postgres_exact_marker:unregistered_writers" in statement
    ]
    assert len(dynamic_statements) == 3
    for statement in dynamic_statements:
        connection.execute(statement)
    with pytest.raises(PermissionAuditMarkerEnvironmentError):
        connection.execute("SELECT * FROM protected_product_table")
    with pytest.raises(PermissionAuditMarkerEnvironmentError):
        connection.execute("UPDATE protected_product_table SET value = 1")
    connection.close()
    assert [name for name, _ in calls] == [
        "connect",
        "execute",
        "execute",
        "execute",
        "execute",
        "close",
    ]


def test_postgres_connection_requires_external_credential_inventory_before_network():
    from agent_knowledge.permission_audit_marker_runtime_env import (
        PermissionAuditMarkerEnvironmentError,
        _open_postgres_read_only,
    )

    calls = []
    with pytest.raises(PermissionAuditMarkerEnvironmentError):
        _open_postgres_read_only(
            "postgresql://fixture.invalid/db",
            connect=lambda *_args, **_kwargs: calls.append("network") or object(),
            allowed_statements=frozenset(),
            expected_privileged_credential_inventory_anchor_hash=_sha("8"),
        )

    assert calls == []
def test_nats_adapter_reads_fixed_stream_and_durable_metadata_without_discovery():
    from agent_knowledge.permission_audit_marker_runtime_env import (
        _NatsStreamMetadataAdapter,
        _nats_stream_generation_hash,
    )

    scope = _config()["scopes"]["queue"]
    scope["stream_generation_hash"] = _nats_stream_generation_hash(
        created="fixture-stream-generation",
        stream="FIXED_STREAM",
        subjects=("events.subject",),
    )
    calls = []

    class JetStream:
        async def stream_info(self, stream):
            calls.append(("stream_info", stream))
            return {
                "created": "fixture-stream-generation",
                "config": {"subjects": ["events.subject"]},
                "state": {
                    "messages": 11,
                    "bytes": 2048,
                    "first_seq": 1,
                    "last_seq": 14,
                    "num_deleted": 3,
                    "consumer_count": 1,
                },
            }

        async def consumer_info(self, stream, durable):
            calls.append(("consumer_info", (stream, durable)))
            return {
                "created": "fixture-consumer-generation",
                "delivered": {"stream_seq": 12},
                "ack_floor": {"stream_seq": 10},
                "num_ack_pending": 2,
                "num_redelivered": 1,
                "num_waiting": 0,
                "num_pending": 2,
            }

    class Connection:
        def jetstream(self):
            calls.append(("jetstream", None))
            return JetStream()

        async def close(self):
            calls.append(("close", None))

    async def connect(**kwargs):
        calls.append(("connect", set(kwargs)))
        assert kwargs["allow_reconnect"] is False
        assert kwargs["max_reconnect_attempts"] == 0
        return Connection()

    adapter = _NatsStreamMetadataAdapter(
        scope,
        "fixture-token",
        _sha("e"),
        connect=connect,
        publisher_authorization_anchor_reader=lambda: _sha("7"),
    )

    metadata = adapter.read_stream_and_durable_metadata()

    assert metadata["unknown_durable_count"] == 0
    assert metadata["publisher_authorization_anchor_hash"] == _sha("7")
    assert metadata["stream"] == {
        "generation_hash": _sha("e"),
        "messages": 11,
        "bytes": 2048,
        "first_seq": 1,
        "last_seq": 14,
        "num_deleted": 3,
        "consumer_count": 1,
    }
    assert len(metadata["durable_consumers"]) == 1
    assert [name for name, _ in calls] == [
        "connect",
        "jetstream",
        "stream_info",
        "consumer_info",
        "close",
    ]
    adapter.close()


def test_nats_adapter_requires_external_publisher_anchor_before_network():
    from agent_knowledge.permission_audit_marker_runtime_env import (
        PermissionAuditMarkerEnvironmentError,
        _NatsStreamMetadataAdapter,
    )

    calls = []

    async def connect(**_kwargs):
        calls.append("network")
        raise AssertionError("publisher anchor must be checked before network")

    adapter = _NatsStreamMetadataAdapter(
        _config()["scopes"]["queue"],
        "fixture-token",
        _sha("e"),
        connect=connect,
    )

    with pytest.raises(PermissionAuditMarkerEnvironmentError):
        adapter.read_stream_and_durable_metadata()

    assert calls == []
    adapter.close()


def test_nats_stream_recreation_fails_even_when_counters_are_unchanged():
    from agent_knowledge.permission_audit_marker_runtime_env import (
        PermissionAuditMarkerEnvironmentError,
        _NatsStreamMetadataAdapter,
    )

    scope = _config()["scopes"]["queue"]

    class JetStream:
        async def stream_info(self, _stream):
            return {
                "created": "different-live-generation",
                "config": {"subjects": ["fixed-writer"]},
                "state": {
                    "messages": 11,
                    "bytes": 2048,
                    "first_seq": 1,
                    "last_seq": 14,
                    "num_deleted": 3,
                    "consumer_count": 1,
                },
            }

        async def consumer_info(self, _stream, _durable):
            raise AssertionError("consumer metadata must not be read after generation mismatch")

    class Connection:
        def jetstream(self):
            return JetStream()

        async def close(self):
            return None

    async def connect(**_kwargs):
        return Connection()

    adapter = _NatsStreamMetadataAdapter(
        scope,
        "fixture-token",
        _sha("e"),
        connect=connect,
        publisher_authorization_anchor_reader=lambda: _sha("7"),
    )

    with pytest.raises(PermissionAuditMarkerEnvironmentError) as error:
        adapter.read_stream_and_durable_metadata()

    assert str(error.value) == "NATS exact marker metadata read failed"
    assert "different-live-generation" not in str(error.value)
    adapter.close()


def test_nats_durable_recreation_fails_even_when_counters_are_unchanged():
    from agent_knowledge.permission_audit_marker_runtime_env import (
        PermissionAuditMarkerEnvironmentError,
        _NatsStreamMetadataAdapter,
    )

    scope = _config()["scopes"]["queue"]

    class JetStream:
        async def stream_info(self, _stream):
            return {
                "created": "fixture-stream-generation",
                "config": {"subjects": ["fixed-writer"]},
                "state": {
                    "messages": 11,
                    "bytes": 2048,
                    "first_seq": 1,
                    "last_seq": 14,
                    "num_deleted": 3,
                    "consumer_count": 1,
                },
            }

        async def consumer_info(self, _stream, _durable):
            return {
                "created": "different-consumer-generation",
                "delivered": {"stream_seq": 12},
                "ack_floor": {"stream_seq": 10},
                "num_ack_pending": 2,
                "num_redelivered": 1,
                "num_waiting": 0,
                "num_pending": 2,
            }

    class Connection:
        def jetstream(self):
            return JetStream()

        async def close(self):
            return None

    async def connect(**_kwargs):
        return Connection()

    adapter = _NatsStreamMetadataAdapter(
        scope,
        "fixture-token",
        _sha("e"),
        connect=connect,
        publisher_authorization_anchor_reader=lambda: _sha("7"),
    )

    with pytest.raises(PermissionAuditMarkerEnvironmentError) as error:
        adapter.read_stream_and_durable_metadata()

    assert str(error.value) == "NATS exact marker metadata read failed"
    assert "different-consumer-generation" not in str(error.value)
    adapter.close()


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_config",
        "malformed_config",
        "extra_scope",
        "wrong_credential_scope",
        "missing_couch_continuity_anchor",
        "missing_nats_publisher_anchor",
        "credential_in_endpoint",
    ),
)
def test_environment_configuration_failures_are_public_safe_and_closed(mutation):
    from agent_knowledge.permission_audit_marker_runtime_env import (
        PermissionAuditMarkerEnvironmentError,
        build_production_permission_audit_marker_reader,
    )

    environment = _environment()
    config = _config()
    protected_value = "protected-fixture-value"
    if mutation == "missing_config":
        environment.pop("NEURONS_PERMISSION_AUDIT_MARKER_CONFIG")
    elif mutation == "malformed_config":
        environment["NEURONS_PERMISSION_AUDIT_MARKER_CONFIG"] = protected_value
    elif mutation == "extra_scope":
        config["scopes"]["cache"] = {"value": protected_value}
        environment["NEURONS_PERMISSION_AUDIT_MARKER_CONFIG"] = json.dumps(config)
    elif mutation == "wrong_credential_scope":
        config["scopes"]["queue"]["credential_scope"] = protected_value
        environment["NEURONS_PERMISSION_AUDIT_MARKER_CONFIG"] = json.dumps(config)
    elif mutation == "missing_couch_continuity_anchor":
        config["scopes"]["corpus"].pop("continuity_anchor_hash")
        environment["NEURONS_PERMISSION_AUDIT_MARKER_CONFIG"] = json.dumps(config)
    elif mutation == "missing_nats_publisher_anchor":
        config["scopes"]["queue"].pop("publisher_authorization_anchor_hash")
        environment["NEURONS_PERMISSION_AUDIT_MARKER_CONFIG"] = json.dumps(config)
    elif mutation == "credential_in_endpoint":
        config["scopes"]["corpus"]["base_url"] = (
            f"https://user:{protected_value}@couchdb.invalid"
        )
        environment["NEURONS_PERMISSION_AUDIT_MARKER_CONFIG"] = json.dumps(config)
    with pytest.raises(PermissionAuditMarkerEnvironmentError) as error:
        build_production_permission_audit_marker_reader(environment)

    expected_message = (
        "permission audit exact marker reader unavailable"
        if mutation == "missing_config"
        else "permission audit exact marker configuration invalid"
    )
    assert str(error.value) == expected_message
    assert protected_value not in str(error.value)


@pytest.mark.parametrize("duplicate_depth", ("top", "scopes", "nested_scope"))
def test_environment_rejects_duplicate_keys_at_every_object_depth(duplicate_depth):
    from agent_knowledge.permission_audit_marker_runtime_env import (
        PermissionAuditMarkerEnvironmentError,
        build_production_permission_audit_marker_reader,
    )

    environment = _environment()
    raw = json.dumps(_config(), separators=(",", ":"))
    protected_value = "protected-duplicate-value"
    if duplicate_depth == "top":
        raw = raw.replace(
            '{"schema_version":',
            f'{{"schema_version":"{protected_value}","schema_version":',
            1,
        )
    elif duplicate_depth == "scopes":
        raw = raw.replace(
            '"scopes":{"authority_ledger":',
            '"scopes":{"authority_ledger":{},"authority_ledger":',
            1,
        )
    else:
        raw = raw.replace(
            '"authority_ledger":{"credential_scope":',
            (
                '"authority_ledger":{"credential_scope":'
                f'"{protected_value}","credential_scope":'
            ),
            1,
        )
    environment["NEURONS_PERMISSION_AUDIT_MARKER_CONFIG"] = raw

    with pytest.raises(PermissionAuditMarkerEnvironmentError) as error:
        build_production_permission_audit_marker_reader(environment)

    assert str(error.value) == "permission audit exact marker configuration invalid"
    assert protected_value not in str(error.value)
