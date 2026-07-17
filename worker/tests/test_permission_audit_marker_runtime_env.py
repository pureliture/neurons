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
    from agent_knowledge.qdrant_write_gateway_runtime import (
        QDRANT_SOURCE_REGISTRY,
        RenderedQdrantWriter,
        build_qdrant_coverage_activation_anchor,
        build_qdrant_coverage_manifest_from_activation_anchor,
        build_qdrant_exact_marker_hash,
    )

    inventory = tuple(
        RenderedQdrantWriter(
            source=source,
            route=binding.route,
            writer_ref_hash=binding.writer_ref_hash,
            active_caller=binding.active_caller,
            workload_ref_hash=(
                _raw_sha(format(index + 1, "x")) if binding.active_caller else None
            ),
            image_ref_hash=(
                _raw_sha(format(index + 9, "x")) if binding.active_caller else None
            ),
            network_policy_ref_hash=(
                _raw_sha("abcdef"[index]) if binding.active_caller else None
            ),
            route_set_hash=(
                _raw_sha(format(index + 7, "x")) if binding.active_caller else None
            ),
        )
        for index, (source, binding) in enumerate(QDRANT_SOURCE_REGISTRY.items())
    )
    activation = build_qdrant_coverage_activation_anchor(
        generation=7,
        marker_collection="fixed_markers",
        rendered_inventory=inventory,
        previous_generation_hash=_raw_sha("f"),
        auth_boundary_status="validated",
        network_policy_status="validated",
        direct_write_credentials_zero=True,
        read_endpoint_write_denied_status="validated",
    )
    coverage = build_qdrant_coverage_manifest_from_activation_anchor(activation)

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
            "index": {
                "credential_scope": "qdrant_marker_metadata_read_only",
                "url": "https://qdrant.invalid:6333",
                "marker_collection": "fixed_markers",
                "metadata_point_id": "00000000-0000-4000-8000-000000000001",
                "generation": 7,
                "rendered_inventory": [
                    {
                        "source": item.source.value,
                        "route": item.route.value,
                        "writer_ref_hash": item.writer_ref_hash,
                        "active_caller": item.active_caller,
                        "workload_ref_hash": item.workload_ref_hash,
                        "image_ref_hash": item.image_ref_hash,
                        "network_policy_ref_hash": item.network_policy_ref_hash,
                        "route_set_hash": item.route_set_hash,
                    }
                    for item in inventory
                ],
                "previous_generation_hash": activation.previous_generation_hash,
                "activation_hash": activation.activation_hash,
                "auth_boundary_status": "validated",
                "network_policy_status": "validated",
                "direct_write_credentials_zero": True,
                "read_endpoint_write_denied_status": "validated",
                "event_position_floor": 24,
                "marker_hash_at_floor": build_qdrant_exact_marker_hash(
                    generation=7,
                    event_position=24,
                    in_flight_count=0,
                    coverage_hash=coverage.coverage_hash,
                ),
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
        "NEURONS_PERMISSION_AUDIT_QDRANT_API_KEY": "private-qdrant",
    }


def test_valid_fixed_five_environment_builds_reader_without_storage_calls():
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

    class Qdrant:
        def read_marker_metadata(self):
            raise AssertionError("delegate fake must not read storage")

        def read_coverage_manifest(self):
            raise AssertionError("delegate fake must not read storage")

        def close(self):
            calls.append(("close", "qdrant"))

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
        qdrant_adapter=lambda scope, credential, expected: (
            calls.append(("qdrant", credential)) or Qdrant()
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
        "qdrant",
        "reader",
        "delegate",
        "close",
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
        qdrant_adapter=lambda *_args: (_ for _ in ()).throw(
            RuntimeError("protected-fixture-value")
        ),
        sqlite_connector=lambda: object(),
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


def test_qdrant_adapter_uses_exact_counts_and_one_fixed_metadata_read_only():
    from agent_knowledge.permission_audit_marker_runtime_env import (
        _QdrantMarkerMetadataAdapter,
        _qdrant_coverage,
        _qdrant_marker_hash,
    )

    scope = _config()["scopes"]["index"]
    expected = _qdrant_coverage(scope)
    calls = []
    snapshots = ((24, 0), (25, 1), (26, 0))

    class CountResult:
        def __init__(self, count):
            self.count = count

    class Point:
        id = scope["metadata_point_id"]
        payload = {
            "schema_version": "qdrant_exact_marker_metadata.v2",
            "generation": 7,
            "coverage_hash": expected.coverage_hash,
            "coverage_status": "complete",
            "bypass_count": 0,
            "activation_hash": scope["activation_hash"],
            "previous_generation_hash": scope["previous_generation_hash"],
        }

    class Client:
        snapshot_index = 0

        def get_collection(self, **kwargs):
            calls.append(("get_collection", kwargs))
            return {
                "config": {
                    "params": {
                        "shard_number": 1,
                        "replication_factor": 1,
                        "write_consistency_factor": 1,
                    }
                },
                "payload_schema": {
                    "record_kind": {"data_type": "keyword"},
                    "phase": {"data_type": "keyword"},
                    "unresolved": {"data_type": "bool"},
                    "generation": {"data_type": "integer"},
                    "route": {"data_type": "keyword"},
                    "writer_ref_hash": {"data_type": "keyword"},
                    "pod_ref_hash": {"data_type": "keyword"},
                    "workload_ref_hash": {"data_type": "keyword"},
                    "route_set_hash": {"data_type": "keyword"},
                    "bypass": {"data_type": "bool"},
                }
            }

        def count(self, **kwargs):
            calls.append(("count", kwargs))
            event_count, unresolved_count = snapshots[self.snapshot_index]
            filter_value = kwargs["count_filter"]
            if filter_value is None:
                return CountResult(event_count + 1)
            values = {
                condition.key: condition.match.value
                for condition in filter_value.must
            }
            if "route" in values:
                routes = ("normal_ingest", "projection", "backfill", "repair")
                route_index = routes.index(values["route"])
                quotient, remainder = divmod(event_count, len(routes))
                return CountResult(quotient + (route_index < remainder))
            if values.get("phase") == "start" and values.get("unresolved") is True:
                return CountResult(unresolved_count)
            start_count = (event_count + 1) // 2
            if values.get("phase") == "start":
                return CountResult(start_count)
            if values.get("phase") == "terminal":
                return CountResult(event_count - start_count)
            return CountResult(event_count)

        def retrieve(self, **kwargs):
            calls.append(("retrieve", kwargs))
            self.snapshot_index += 1
            return [Point()]

        def close(self):
            calls.append(("close", {}))

    adapter = _QdrantMarkerMetadataAdapter(
        scope,
        "fixture-api-key",
        expected,
        client=Client(),
    )

    observed_snapshots = [adapter.read_marker_metadata() for _ in range(3)]
    snapshot = observed_snapshots[0]

    assert snapshot == {
        "generation": 7,
        "event_position": 24,
        "marker_hash": _qdrant_marker_hash(
            generation=7,
            event_count=24,
            unresolved_count=0,
            coverage_hash=expected.coverage_hash,
        ),
        "in_flight_count": 0,
        "coverage_hash": expected.coverage_hash,
        "coverage_status": "complete",
        "count_status": "exact",
        "reset_count": 0,
        "bypass_count": 0,
    }
    assert adapter.read_coverage_manifest() == expected
    assert [item["event_position"] for item in observed_snapshots] == [24, 25, 26]
    assert [item["in_flight_count"] for item in observed_snapshots] == [0, 1, 0]
    assert len({item["marker_hash"] for item in observed_snapshots}) == 3
    assert [name for name, _ in calls].count("get_collection") == 3
    assert [name for name, _ in calls].count("count") == 36
    assert [name for name, _ in calls].count("retrieve") == 3
    assert all(call["exact"] is True for name, call in calls if name == "count")
    assert calls[-1][1]["ids"] == [scope["metadata_point_id"]]
    assert calls[-1][1]["with_payload"] is True
    assert calls[-1][1]["with_vectors"] is False
    assert str(calls[-1][1]["consistency"]).casefold().endswith("all")
    assert all(
        kwargs["ids"] == [scope["metadata_point_id"]]
        for name, kwargs in calls
        if name == "retrieve"
    )
    route_filters = [
        {
            condition.key: condition.match.value
            for condition in kwargs["count_filter"].must
        }
        for name, kwargs in calls
        if name == "count"
        and kwargs["count_filter"] is not None
        and any(
            condition.key == "route" for condition in kwargs["count_filter"].must
        )
    ]
    assert len(route_filters) == 12
    assert all(
        {
            "record_kind",
            "generation",
            "route",
            "writer_ref_hash",
            "workload_ref_hash",
            "route_set_hash",
            "bypass",
        }.issubset(values)
        for values in route_filters
    )
    pod_presence_filters = [
        kwargs["count_filter"]
        for name, kwargs in calls
        if name == "count"
        and kwargs["count_filter"] is not None
        and getattr(kwargs["count_filter"], "must_not", None)
    ]
    assert len(pod_presence_filters) == 3
    assert all(
        filter_value.must_not[0].is_empty.key == "pod_ref_hash"
        for filter_value in pod_presence_filters
    )
    adapter.close()
    assert calls[-1] == ("close", {})


def test_qdrant_client_disables_proxy_environment_and_redirects(monkeypatch):
    import qdrant_client
    from agent_knowledge.permission_audit_marker_runtime_env import (
        _QdrantMarkerMetadataAdapter,
        _qdrant_coverage,
    )

    scope = _config()["scopes"]["index"]
    captured = {}

    class Client:
        def get_collection(self, **_kwargs):
            return None

        def count(self, **_kwargs):
            return None

        def retrieve(self, **_kwargs):
            return None

    def client_factory(**kwargs):
        captured.update(kwargs)
        return Client()

    monkeypatch.setattr(qdrant_client, "QdrantClient", client_factory)

    _QdrantMarkerMetadataAdapter(
        scope,
        "fixture-api-key",
        _qdrant_coverage(scope),
    )

    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False
    assert captured["prefer_grpc"] is False
    assert captured["url"] == "https://qdrant.invalid:6333"
    assert captured["port"] == 6333
    assert captured["https"] is True


@pytest.mark.parametrize(
    "failure",
    ("approximate_count", "duplicate_metadata", "missing_index", "wrong_point"),
)
def test_qdrant_malformed_or_non_exact_metadata_fails_closed(failure):
    from agent_knowledge.permission_audit_marker_runtime_env import (
        PermissionAuditMarkerEnvironmentError,
        _QdrantMarkerMetadataAdapter,
        _qdrant_coverage,
    )

    scope = _config()["scopes"]["index"]
    expected = _qdrant_coverage(scope)

    class CountResult:
        count = 1.5 if failure == "approximate_count" else 2

    class Point:
        id = (
            "00000000-0000-4000-8000-000000000099"
            if failure == "wrong_point"
            else scope["metadata_point_id"]
        )
        payload = {
            "schema_version": "qdrant_exact_marker_metadata.v2",
            "generation": 7,
            "coverage_hash": expected.coverage_hash,
            "coverage_status": "complete",
            "bypass_count": 0,
            "activation_hash": scope["activation_hash"],
            "previous_generation_hash": scope["previous_generation_hash"],
        }

    class Client:
        def get_collection(self, **_kwargs):
            fields = {
                "record_kind": {"data_type": "keyword"},
                "phase": {"data_type": "keyword"},
                "unresolved": {"data_type": "bool"},
                "generation": {"data_type": "integer"},
                "route": {"data_type": "keyword"},
                "writer_ref_hash": {"data_type": "keyword"},
                "pod_ref_hash": {"data_type": "keyword"},
                "workload_ref_hash": {"data_type": "keyword"},
                "route_set_hash": {"data_type": "keyword"},
                "bypass": {"data_type": "bool"},
            }
            if failure == "missing_index":
                fields.pop("unresolved")
            return {
                "config": {
                    "params": {
                        "shard_number": 1,
                        "replication_factor": 1,
                        "write_consistency_factor": 1,
                    }
                },
                "payload_schema": fields,
            }

        def count(self, **_kwargs):
            return CountResult()

        def retrieve(self, **_kwargs):
            return [Point(), Point()] if failure == "duplicate_metadata" else [Point()]

    adapter = _QdrantMarkerMetadataAdapter(
        scope,
        "fixture-api-key",
        expected,
        client=Client(),
    )

    with pytest.raises(PermissionAuditMarkerEnvironmentError) as error:
        adapter.read_marker_metadata()

    assert str(error.value) == "Qdrant exact marker metadata read failed"


@pytest.mark.parametrize(
    "failure",
    (
        "unknown_record_kind",
        "stale_generation",
        "malformed_phase",
        "unknown_route",
        "unknown_writer",
        "missing_pod",
        "bypass_true",
    ),
)
def test_qdrant_exact_counts_reject_uncovered_event_dimensions(failure):
    from agent_knowledge.permission_audit_marker_runtime_env import (
        PermissionAuditMarkerEnvironmentError,
        _QdrantMarkerMetadataAdapter,
        _qdrant_coverage,
    )

    scope = _config()["scopes"]["index"]
    expected = _qdrant_coverage(scope)

    class CountResult:
        def __init__(self, count):
            self.count = count

    class Point:
        id = scope["metadata_point_id"]
        payload = {
            "schema_version": "qdrant_exact_marker_metadata.v2",
            "generation": 7,
            "coverage_hash": expected.coverage_hash,
            "coverage_status": "complete",
            "bypass_count": 0,
            "activation_hash": scope["activation_hash"],
            "previous_generation_hash": scope["previous_generation_hash"],
        }

    class Client:
        def get_collection(self, **_kwargs):
            return {
                "config": {
                    "params": {
                        "shard_number": 1,
                        "replication_factor": 1,
                        "write_consistency_factor": 1,
                    }
                },
                "payload_schema": {
                    "record_kind": {"data_type": "keyword"},
                    "phase": {"data_type": "keyword"},
                    "unresolved": {"data_type": "bool"},
                    "generation": {"data_type": "integer"},
                    "route": {"data_type": "keyword"},
                    "writer_ref_hash": {"data_type": "keyword"},
                    "pod_ref_hash": {"data_type": "keyword"},
                    "workload_ref_hash": {"data_type": "keyword"},
                    "route_set_hash": {"data_type": "keyword"},
                    "bypass": {"data_type": "bool"},
                },
            }

        def count(self, **kwargs):
            filter_value = kwargs["count_filter"]
            if filter_value is None:
                return CountResult(26 if failure == "unknown_record_kind" else 25)
            values = {
                condition.key: condition.match.value
                for condition in filter_value.must
            }
            keys = set(values)
            if failure == "missing_pod" and getattr(filter_value, "must_not", None):
                return CountResult(23)
            if failure == "stale_generation" and keys == {"record_kind", "generation"}:
                return CountResult(23)
            if failure == "malformed_phase" and values.get("phase") == "terminal":
                return CountResult(11)
            if "route" in values:
                routes = ("normal_ingest", "projection", "backfill", "repair")
                index = routes.index(values["route"])
                count = 6
                if failure in {"unknown_route", "unknown_writer"} and index == 0:
                    count -= 1
                return CountResult(count)
            if failure == "bypass_true" and keys == {"record_kind", "bypass"}:
                return CountResult(23)
            if values.get("phase") == "start" and values.get("unresolved") is True:
                return CountResult(0)
            if values.get("phase") in {"start", "terminal"}:
                return CountResult(12)
            return CountResult(24)

        def retrieve(self, **_kwargs):
            return [Point()]

    adapter = _QdrantMarkerMetadataAdapter(
        scope,
        "fixture-api-key",
        expected,
        client=Client(),
    )

    with pytest.raises(PermissionAuditMarkerEnvironmentError):
        adapter.read_marker_metadata()


def test_qdrant_external_continuity_anchor_rejects_cross_audit_reset():
    from agent_knowledge.permission_audit_marker_runtime_env import (
        PermissionAuditMarkerEnvironmentError,
        _QdrantMarkerMetadataAdapter,
        _qdrant_coverage,
    )

    scope = _config()["scopes"]["index"]
    expected = _qdrant_coverage(scope)

    class CountResult:
        def __init__(self, count):
            self.count = count

    class Client:
        def get_collection(self, **_kwargs):
            return {
                "config": {
                    "params": {
                        "shard_number": 1,
                        "replication_factor": 1,
                        "write_consistency_factor": 1,
                    }
                },
                "payload_schema": {
                    field: {"data_type": "integer" if field == "generation" else "bool" if field in {"unresolved", "bypass"} else "keyword"}
                    for field in (
                        "record_kind", "phase", "unresolved", "generation", "route",
                        "writer_ref_hash", "pod_ref_hash", "workload_ref_hash", "route_set_hash", "bypass",
                    )
                },
            }

        def count(self, **kwargs):
            filter_value = kwargs["count_filter"]
            if filter_value is None:
                return CountResult(24)
            values = {
                condition.key: condition.match.value
                for condition in filter_value.must
            }
            if "route" in values:
                return CountResult(23 // 4 + (values["route"] == "normal_ingest"))
            if values.get("phase") == "start" and values.get("unresolved") is True:
                return CountResult(0)
            if values.get("phase") == "start":
                return CountResult(12)
            if values.get("phase") == "terminal":
                return CountResult(11)
            return CountResult(23)

        def retrieve(self, **_kwargs):
            class Point:
                id = scope["metadata_point_id"]
                payload = {
                    "schema_version": "qdrant_exact_marker_metadata.v2",
                    "generation": 7,
                    "coverage_hash": expected.coverage_hash,
                    "coverage_status": "complete",
                    "bypass_count": 0,
                    "activation_hash": scope["activation_hash"],
                    "previous_generation_hash": scope["previous_generation_hash"],
                }

            return [Point()]

    adapter = _QdrantMarkerMetadataAdapter(
        scope,
        "fixture-api-key",
        expected,
        client=Client(),
    )

    with pytest.raises(PermissionAuditMarkerEnvironmentError):
        adapter.read_marker_metadata()


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
        "missing_storage_credential",
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
    else:
        environment.pop("NEURONS_PERMISSION_AUDIT_QDRANT_API_KEY")

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


@pytest.mark.parametrize(
    "malicious_url",
    (
        "http://qdrant.invalid:6333",
        "https://operator@qdrant.invalid:6333",
        "https://qdrant.invalid",
        "https://qdrant.invalid:6334",
        "https://qdrant.invalid:6333/",
        "https://qdrant.invalid:6333/admin",
        "https://qdrant.invalid:6333?target=other",
        "https://qdrant.invalid:6333#other",
        "https://QDRANT.invalid:6333",
        "https://qdrant%2einvalid:6333",
        "https://qdrant.invalid\\@other.invalid:6333",
        "https://127.1:6333",
        "https://0x7f000001:6333",
    ),
)
def test_environment_rejects_noncanonical_qdrant_authority_before_runtime(
    malicious_url,
):
    from agent_knowledge.permission_audit_marker_runtime_env import (
        PermissionAuditMarkerEnvironmentError,
        build_production_permission_audit_marker_reader,
    )

    environment = _environment()
    config = _config()
    config["scopes"]["index"]["url"] = malicious_url
    environment["NEURONS_PERMISSION_AUDIT_MARKER_CONFIG"] = json.dumps(config)

    with pytest.raises(PermissionAuditMarkerEnvironmentError) as error:
        build_production_permission_audit_marker_reader(environment)

    assert str(error.value) == "permission audit exact marker configuration invalid"
    assert malicious_url not in str(error.value)
