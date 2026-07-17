"""Production environment boundary for exact-five permission-audit markers.

The process entrypoint parses one fixed, non-secret activation manifest.  Storage
credentials stay in fixed environment keys and are never copied into errors.  SDK
objects remain lazy so merely enabling the process configuration performs no
storage or network operation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from typing import Any
import urllib.parse
import urllib.request
import uuid

from .permission_audit import IndependentProductMutationMarkerReader
from .native_exact_mutation_markers import (
    CouchDBExactMutationMarkerProvider,
    NatsJetStreamExactMutationMarkerProvider,
    NativeExactMutationMarkerContract,
    SQLiteExactMutationMarkerProvider,
)
from .permission_audit_marker_runtime import build_permission_audit_marker_reader
from .postgres_exact_mutation_marker import (
    PostgresExactMutationMarkerFence,
    PostgresExactMutationMarkerReader,
    build_source_owned_postgres_exact_marker_contract,
)
from .qdrant_write_gateway import QdrantMutationRoute
from .qdrant_write_gateway_runtime import (
    ACTIVE_QDRANT_MUTATION_SOURCES,
    QDRANT_EXACT_MARKER_METADATA_KEYS,
    QDRANT_EXACT_MARKER_METADATA_SCHEMA,
    QDRANT_MARKER_EVENT_RECORD_KIND,
    QDRANT_MARKER_PHASE_START,
    QDRANT_MARKER_PHASE_TERMINAL,
    QDRANT_MARKER_REQUIRED_PAYLOAD_INDEX_FIELDS,
    QdrantMutationSource,
    RenderedQdrantWriter,
    build_qdrant_coverage_activation_anchor,
    build_qdrant_coverage_manifest_from_activation_anchor,
    build_qdrant_exact_marker_hash,
)


_CONFIG_ENV = "NEURONS_PERMISSION_AUDIT_MARKER_CONFIG"
_SECRET_ENV_KEYS = (
    "NEURONS_PERMISSION_AUDIT_PG_DSN",
    "NEURONS_PERMISSION_AUDIT_COUCHDB_AUTH_HEADER",
    "NEURONS_PERMISSION_AUDIT_NATS_TOKEN",
    "NEURONS_PERMISSION_AUDIT_QDRANT_API_KEY",
)
_PLANES = (
    "authority_ledger",
    "corpus",
    "queue",
    "index",
    "product_db",
)
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RAW_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}\Z")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")

_PG_KEYS = frozenset(
    {
        "credential_scope",
        "schema_generation",
        "writer_roles",
        "marker_owner_role",
        "audit_reader_role",
        "advisory_lock_key",
        "approved_privileged_roles",
        "privileged_credential_inventory_anchor_hash",
    }
)
_NATIVE_COMMON_KEYS = frozenset(
    {
        "credential_scope",
        "storage_identity",
        "schema_generation",
        "config_generation",
        "reader_contract",
        "writer_registry",
    }
)
_CORPUS_KEYS = _NATIVE_COMMON_KEYS | {
    "base_url",
    "database",
    "continuity_anchor_hash",
}
_QUEUE_KEYS = _NATIVE_COMMON_KEYS | {
    "server_url",
    "stream",
    "durables",
    "stream_generation_hash",
    "durable_generation_hashes",
    "publisher_authorization_anchor_hash",
}
_INDEX_KEYS = frozenset(
    {
        "credential_scope",
        "url",
        "marker_collection",
        "metadata_point_id",
        "generation",
        "rendered_inventory",
        "previous_generation_hash",
        "activation_hash",
        "auth_boundary_status",
        "network_policy_status",
        "direct_write_credentials_zero",
        "read_endpoint_write_denied_status",
        "event_position_floor",
        "marker_hash_at_floor",
    }
)
_SQLITE_KEYS = _NATIVE_COMMON_KEYS | {"path", "expected_schema_version"}
_EXPECTED_CREDENTIAL_SCOPES = {
    "authority_ledger": "postgres_exact_marker_read_only",
    "corpus": "couchdb_db_info_read_only",
    "queue": "nats_stream_consumer_metadata_read_only",
    "index": "qdrant_marker_metadata_read_only",
    "product_db": "sqlite_mode_ro_query_only",
}


class PermissionAuditMarkerEnvironmentError(RuntimeError):
    """Public-safe, fail-closed production marker configuration failure."""


@dataclass(frozen=True)
class ProductionMarkerAdapterFactories:
    """Narrow SDK factory boundary; every factory is storage-plane specific."""

    postgres_connection: Callable[..., object]
    couchdb_adapter: Callable[[Mapping[str, Any], str], object]
    nats_adapter: Callable[[Mapping[str, Any], str, str], object]
    qdrant_adapter: Callable[[Mapping[str, Any], str, object], object]
    sqlite_connector: Callable[[], object]
    sqlite_file_inspector: Callable[[], object]


@dataclass
class _RuntimeBundle:
    reader: IndependentProductMutationMarkerReader
    resources: tuple[object, ...]

    def close(self) -> None:
        _close_resources(self.resources)


def _close_resources(resources: tuple[object, ...]) -> None:
    first_error: BaseException | None = None
    for resource in reversed(resources):
        close = getattr(resource, "close", None)
        if not callable(close):
            continue
        try:
            close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise PermissionAuditMarkerEnvironmentError(
            "permission audit exact marker resource close failed"
        ) from first_error


class _ProductionPermissionAuditMarkerReader(IndependentProductMutationMarkerReader):
    """Lazy production reader; the runtime bundle is built per bounded audit."""

    def __init__(self, runtime_factory: Callable[[], Any]) -> None:
        self._runtime_factory = runtime_factory

    def run_audit_window(self, action):  # type: ignore[override]
        runtime = self._runtime_factory()
        try:
            return runtime.reader.run_audit_window(action)
        finally:
            runtime.close()


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def build_production_permission_audit_marker_reader(
    environ: Mapping[str, str],
    *,
    factories: ProductionMarkerAdapterFactories | None = None,
) -> IndependentProductMutationMarkerReader:
    """Build the production marker reader without exposing raw configuration."""

    config = _parse_environment(environ)
    selected_factories = factories or _default_factories()
    if not isinstance(selected_factories, ProductionMarkerAdapterFactories):
        raise PermissionAuditMarkerEnvironmentError(
            "permission audit exact marker configuration invalid"
        )
    return _ProductionPermissionAuditMarkerReader(
        lambda: _build_runtime(
            config=config,
            environ=environ,
            factories=selected_factories,
        )
    )


def _parse_environment(environ: Mapping[str, str]) -> dict[str, Any]:
    raw = environ.get(_CONFIG_ENV)
    if not isinstance(raw, str) or not raw:
        raise PermissionAuditMarkerEnvironmentError(
            "permission audit exact marker reader unavailable"
        )
    try:
        if len(raw) > 131_072:
            raise ValueError
        config = json.loads(raw, object_pairs_hook=_strict_json_object)
        if not isinstance(config, dict) or set(config) != {
            "schema_version",
            "scopes",
        }:
            raise ValueError
        if config["schema_version"] != "permission_audit_marker_runtime.v1":
            raise ValueError
        scopes = config["scopes"]
        if not isinstance(scopes, dict) or tuple(scopes) != _PLANES:
            raise ValueError
        _validate_scope(scopes["authority_ledger"], _PG_KEYS, "authority_ledger")
        _validate_scope(scopes["corpus"], _CORPUS_KEYS, "corpus")
        _validate_scope(scopes["queue"], _QUEUE_KEYS, "queue")
        _validate_scope(scopes["index"], _INDEX_KEYS, "index")
        _validate_scope(scopes["product_db"], _SQLITE_KEYS, "product_db")
        _validate_postgres_scope(scopes["authority_ledger"])
        _validate_native_scope(scopes["corpus"])
        _validate_native_scope(scopes["queue"])
        _validate_native_scope(scopes["product_db"])
        _validate_identifier(scopes["corpus"]["database"])
        _validate_identifier(scopes["queue"]["stream"])
        _validate_string_list(scopes["queue"]["durables"])
        _validate_hash(scopes["corpus"]["continuity_anchor_hash"])
        _validate_hash(scopes["queue"]["publisher_authorization_anchor_hash"])
        _validate_hash(scopes["queue"]["stream_generation_hash"])
        durable_hashes = scopes["queue"]["durable_generation_hashes"]
        if (
            not isinstance(durable_hashes, list)
            or len(durable_hashes) != len(scopes["queue"]["durables"])
        ):
            raise ValueError
        for value in durable_hashes:
            _validate_hash(value)
        _validate_identifier(scopes["index"]["marker_collection"])
        _validated_base_url(scopes["corpus"]["base_url"], schemes={"https"})
        _validated_base_url(
            scopes["queue"]["server_url"], schemes={"nats", "tls"}
        )
        _validated_qdrant_url(scopes["index"]["url"])
        _validate_qdrant_scope(scopes["index"])
        _validate_bounded_text(scopes["product_db"]["path"])
        expected_schema = scopes["product_db"]["expected_schema_version"]
        if type(expected_schema) is not int or not 0 <= expected_schema < 2**63:
            raise ValueError
        for key in _SECRET_ENV_KEYS:
            value = environ.get(key)
            if not isinstance(value, str) or not value or len(value) > 16_384:
                raise ValueError
        return config
    except Exception as exc:
        raise PermissionAuditMarkerEnvironmentError(
            "permission audit exact marker configuration invalid"
        ) from exc


def _validate_scope(value: object, keys: frozenset[str], plane: str) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError
    if value.get("credential_scope") != _EXPECTED_CREDENTIAL_SCOPES[plane]:
        raise ValueError


def _validate_postgres_scope(value: dict[str, Any]) -> None:
    _validate_hash(value["schema_generation"])
    _validate_string_list(value["writer_roles"])
    _validate_string_list(value["approved_privileged_roles"])
    _validate_hash(value["privileged_credential_inventory_anchor_hash"])
    _validate_identifier(value["marker_owner_role"])
    _validate_identifier(value["audit_reader_role"])
    lock_key = value["advisory_lock_key"]
    if type(lock_key) is not int or not -(2**63) <= lock_key < 2**63:
        raise ValueError


def _validate_native_scope(value: dict[str, Any]) -> None:
    for key in ("storage_identity", "reader_contract"):
        _validate_bounded_text(value[key])
    _validate_hash(value["schema_generation"])
    _validate_hash(value["config_generation"])
    _validate_string_list(value["writer_registry"])


def _validate_qdrant_scope(value: dict[str, Any]) -> None:
    anchor = _qdrant_activation_anchor(value)
    coverage = build_qdrant_coverage_manifest_from_activation_anchor(anchor)
    event_position_floor = value["event_position_floor"]
    if (
        type(event_position_floor) is not int
        or not 0 <= event_position_floor < 2**63
    ):
        raise ValueError
    marker_hash_at_floor = value["marker_hash_at_floor"]
    if (
        not isinstance(marker_hash_at_floor, str)
        or not _RAW_SHA256.fullmatch(marker_hash_at_floor)
        or marker_hash_at_floor
        != build_qdrant_exact_marker_hash(
            generation=anchor.generation,
            event_position=event_position_floor,
            in_flight_count=0,
            coverage_hash=coverage.coverage_hash,
        )
    ):
        raise ValueError
    try:
        point_id = uuid.UUID(str(value["metadata_point_id"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError from exc
    if str(point_id) != value["metadata_point_id"]:
        raise ValueError


def _validate_hash(value: object) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError


def _validate_bounded_text(value: object) -> None:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= 512
        or any(character < " " or character == "\x7f" for character in value)
    ):
        raise ValueError


def _validate_identifier(value: object) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError


def _validate_string_list(value: object) -> None:
    if not isinstance(value, list) or not value or len(value) != len(set(value)):
        raise ValueError
    for item in value:
        _validate_bounded_text(item)


def _build_runtime(
    *,
    config: dict[str, Any],
    environ: Mapping[str, str],
    factories: ProductionMarkerAdapterFactories,
) -> _RuntimeBundle:
    scopes = config["scopes"]
    pg_scope = scopes["authority_ledger"]
    corpus_scope = scopes["corpus"]
    queue_scope = scopes["queue"]
    index_scope = scopes["index"]
    sqlite_scope = scopes["product_db"]
    resources: list[object] = []
    try:
        postgres_contract = build_source_owned_postgres_exact_marker_contract(
            schema_generation=pg_scope["schema_generation"],
            writer_roles=tuple(pg_scope["writer_roles"]),
            marker_owner_role=pg_scope["marker_owner_role"],
            audit_reader_role=pg_scope["audit_reader_role"],
            advisory_lock_key=pg_scope["advisory_lock_key"],
            approved_privileged_roles=tuple(
                pg_scope["approved_privileged_roles"]
            ),
            privileged_credential_inventory_anchor_hash=pg_scope[
                "privileged_credential_inventory_anchor_hash"
            ],
        )
        couch_contract = _native_contract("corpus", corpus_scope)
        queue_contract = _native_contract("queue", queue_scope)
        sqlite_contract = _native_contract("product_db", sqlite_scope)
        expected_coverage = _qdrant_coverage(index_scope)

        couch_adapter = factories.couchdb_adapter(
            corpus_scope,
            environ["NEURONS_PERMISSION_AUDIT_COUCHDB_AUTH_HEADER"],
        )
        nats_adapter = factories.nats_adapter(
            queue_scope,
            environ["NEURONS_PERMISSION_AUDIT_NATS_TOKEN"],
            queue_contract.generation_hash,
        )
        resources.append(nats_adapter)
        qdrant_adapter = factories.qdrant_adapter(
            index_scope,
            environ["NEURONS_PERMISSION_AUDIT_QDRANT_API_KEY"],
            expected_coverage,
        )
        resources.append(qdrant_adapter)
        sqlite_connector = factories.sqlite_connector()
        resources.append(sqlite_connector)
        sqlite_inspector = factories.sqlite_file_inspector()

        couch_provider = CouchDBExactMutationMarkerProvider(
            couch_contract,
            couch_adapter,  # type: ignore[arg-type]
            expected_continuity_anchor_hash=corpus_scope[
                "continuity_anchor_hash"
            ],
        )
        nats_provider = NatsJetStreamExactMutationMarkerProvider(
            queue_contract,
            nats_adapter,  # type: ignore[arg-type]
            expected_durable_count=len(queue_scope["durables"]),
            expected_publisher_authorization_anchor_hash=queue_scope[
                "publisher_authorization_anchor_hash"
            ],
        )
        sqlite_provider = SQLiteExactMutationMarkerProvider(
            sqlite_contract,
            path=sqlite_scope["path"],
            connector=sqlite_connector,  # type: ignore[arg-type]
            file_inspector=sqlite_inspector,  # type: ignore[arg-type]
            expected_schema_version=sqlite_scope["expected_schema_version"],
        )
        reader = build_permission_audit_marker_reader(
            postgres_contract=postgres_contract,
            postgres_connection_factory=lambda: factories.postgres_connection(
                environ["NEURONS_PERMISSION_AUDIT_PG_DSN"],
                allowed_statements=_postgres_exact_read_statements(
                    postgres_contract
                ),
                expected_privileged_credential_inventory_anchor_hash=(
                    postgres_contract.privileged_credential_inventory_anchor_hash
                ),
            ),
            couchdb_provider=couch_provider,
            nats_provider=nats_provider,
            sqlite_provider=sqlite_provider,
            qdrant_metadata_reader=qdrant_adapter.read_marker_metadata,
            qdrant_coverage_reader=qdrant_adapter.read_coverage_manifest,
            qdrant_expected_coverage=expected_coverage,
        )
        return _RuntimeBundle(
            reader=reader,
            resources=tuple(resources),
        )
    except Exception as exc:
        try:
            _close_resources(tuple(resources))
        except PermissionAuditMarkerEnvironmentError:
            pass
        raise PermissionAuditMarkerEnvironmentError(
            "permission audit exact marker runtime unavailable"
        ) from exc


def _native_contract(
    plane: str,
    scope: Mapping[str, Any],
) -> NativeExactMutationMarkerContract:
    return NativeExactMutationMarkerContract(
        plane=plane,
        storage_identity=scope["storage_identity"],
        schema_generation=scope["schema_generation"],
        config_generation=scope["config_generation"],
        reader_contract=scope["reader_contract"],
        writer_registry=tuple(scope["writer_registry"]),
    )


def _qdrant_coverage(scope: Mapping[str, Any]):
    return build_qdrant_coverage_manifest_from_activation_anchor(
        _qdrant_activation_anchor(scope)
    )


def _qdrant_activation_anchor(scope: Mapping[str, Any]):
    inventory = scope["rendered_inventory"]
    if not isinstance(inventory, list):
        raise ValueError
    rendered: list[RenderedQdrantWriter] = []
    expected_keys = {
        "source",
        "route",
        "writer_ref_hash",
        "active_caller",
        "workload_ref_hash",
        "image_ref_hash",
        "network_policy_ref_hash",
        "route_set_hash",
    }
    for item in inventory:
        if not isinstance(item, dict) or set(item) != expected_keys:
            raise ValueError
        rendered.append(
            RenderedQdrantWriter(
                source=QdrantMutationSource(item["source"]),
                route=QdrantMutationRoute(item["route"]),
                writer_ref_hash=item["writer_ref_hash"],
                active_caller=item["active_caller"],
                workload_ref_hash=item["workload_ref_hash"],
                image_ref_hash=item["image_ref_hash"],
                network_policy_ref_hash=item["network_policy_ref_hash"],
                route_set_hash=item["route_set_hash"],
            )
        )
    return build_qdrant_coverage_activation_anchor(
        generation=scope["generation"],
        marker_collection=scope["marker_collection"],
        rendered_inventory=tuple(rendered),
        previous_generation_hash=scope["previous_generation_hash"],
        auth_boundary_status=scope["auth_boundary_status"],
        network_policy_status=scope["network_policy_status"],
        direct_write_credentials_zero=scope["direct_write_credentials_zero"],
        read_endpoint_write_denied_status=scope[
            "read_endpoint_write_denied_status"
        ],
        activation_hash=scope["activation_hash"],
    )


def _default_factories() -> ProductionMarkerAdapterFactories:
    sqlite_pin = _SQLitePinnedFile()
    return ProductionMarkerAdapterFactories(
        postgres_connection=_open_postgres_read_only,
        couchdb_adapter=_build_couchdb_adapter,
        nats_adapter=_build_nats_adapter,
        qdrant_adapter=_build_qdrant_adapter,
        sqlite_connector=lambda: _SQLiteReadOnlyConnector(sqlite_pin),
        sqlite_file_inspector=lambda: _SQLiteFileInspector(sqlite_pin),
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object):
        return None


def _couchdb_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    *,
    timeout_seconds: int,
) -> Mapping[str, object]:
    request = urllib.request.Request(url, headers=dict(headers), method=method)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    )
    with opener.open(request, timeout=timeout_seconds) as response:
        body = response.read(65_537)
    if len(body) > 65_536:
        raise PermissionAuditMarkerEnvironmentError(
            "CouchDB exact marker metadata read failed"
        )
    value = json.loads(body.decode("utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError
    return value


class _CouchDBInfoAdapter:
    """One fixed DB-info GET plus a separate durable continuity anchor.

    CouchDB's documented ``GET /{db}`` response exposes opaque update/purge
    sequences but no database-generation UUID:
    https://docs.couchdb.org/en/stable/api/database/common.html#get--db
    """

    def __init__(
        self,
        scope: Mapping[str, Any],
        credential: str,
        *,
        transport: Callable[..., Mapping[str, object]] = _couchdb_transport,
        continuity_anchor_reader: Callable[[], object] | None = None,
    ) -> None:
        self._base_url = _validated_base_url(scope["base_url"], schemes={"https"})
        database = scope["database"]
        _validate_identifier(database)
        self._database = str(database)
        self._expected_continuity_anchor_hash = str(
            scope["continuity_anchor_hash"]
        )
        _validate_hash(self._expected_continuity_anchor_hash)
        if not isinstance(credential, str) or not credential:
            raise ValueError
        self._credential = credential
        self._transport = transport
        self._continuity_anchor_reader = continuity_anchor_reader

    def read_database_info(self) -> Mapping[str, object]:
        try:
            if self._continuity_anchor_reader is None:
                raise ValueError
            continuity_anchor_hash = self._continuity_anchor_reader()
            _validate_hash(continuity_anchor_hash)
            if continuity_anchor_hash != self._expected_continuity_anchor_hash:
                raise ValueError
            value = self._transport(
                "GET",
                f"{self._base_url}/{urllib.parse.quote(self._database, safe='')}",
                {
                    "Accept": "application/json",
                    "Authorization": self._credential,
                },
                timeout_seconds=5,
            )
            expected = {"update_seq", "purge_seq", "doc_count", "doc_del_count"}
            if not isinstance(value, Mapping) or not expected.issubset(value):
                raise ValueError
            for key in ("update_seq", "purge_seq"):
                if isinstance(value[key], bool) or not isinstance(value[key], (int, str)):
                    raise ValueError
            for key in ("doc_count", "doc_del_count"):
                if type(value[key]) is not int or value[key] < 0:
                    raise ValueError
            return {
                "continuity_anchor_hash": continuity_anchor_hash,
                "update_seq": value["update_seq"],
                "purge_seq": value["purge_seq"],
                "doc_count": value["doc_count"],
                "doc_del_count": value["doc_del_count"],
            }
        except Exception as exc:
            raise PermissionAuditMarkerEnvironmentError(
                "CouchDB exact marker metadata read failed"
            ) from exc


def _build_couchdb_adapter(
    scope: Mapping[str, Any],
    credential: str,
) -> _CouchDBInfoAdapter:
    return _CouchDBInfoAdapter(scope, credential)


def _validated_base_url(value: object, *, schemes: set[str]) -> str:
    if not isinstance(value, str) or len(value) > 2_048:
        raise ValueError
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError
    return value.rstrip("/")


def _validated_qdrant_url(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2_048
        or any(character < " " or character == "\x7f" for character in value)
        or "\\" in value
        or "%" in value
    ):
        raise ValueError
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError from None
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or port != 6333
        or parsed.netloc != f"{hostname}:6333"
        or hostname == "localhost"
        or hostname.startswith("0x")
    ):
        raise ValueError
    if set(hostname) <= set("0123456789."):
        try:
            address = ipaddress.IPv4Address(hostname)
        except ipaddress.AddressValueError:
            raise ValueError from None
        if str(address) != hostname:
            raise ValueError
        return value
    labels = hostname.split(".")
    if (
        len(hostname) > 253
        or any(not label or not _DNS_LABEL.fullmatch(label) for label in labels)
    ):
        raise ValueError
    return value


class _SQLitePragmaConnection:
    _ALLOWED = frozenset(
        {
            "query_only",
            "data_version",
            "page_count",
            "schema_version",
            "freelist_count",
        }
    )

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        connection_identity_hash: str,
        verify_pinned_identity: Callable[[], object],
        refresh_pinned_snapshot: Callable[[], object],
    ) -> None:
        self._connection = connection
        self._closed = False
        self._marker_cycle_started = False
        _validate_hash(connection_identity_hash)
        self.connection_identity_hash = connection_identity_hash
        self._verify_pinned_identity = verify_pinned_identity
        self._refresh_pinned_snapshot = refresh_pinned_snapshot

    def read_pragma(self, name: str) -> object:
        if self._closed or name not in self._ALLOWED:
            raise PermissionAuditMarkerEnvironmentError(
                "SQLite exact marker operation is not allowed"
        )
        try:
            self._verify_pinned_identity()
            if name == "query_only":
                if self._marker_cycle_started:
                    self._refresh_pinned_snapshot()
                else:
                    self._marker_cycle_started = True
            row = self._connection.execute(f"PRAGMA {name}").fetchone()
            if row is None or len(row) != 1:
                raise ValueError
            self._verify_pinned_identity()
            return row[0]
        except PermissionAuditMarkerEnvironmentError:
            raise
        except Exception as exc:
            raise PermissionAuditMarkerEnvironmentError(
                "SQLite exact marker metadata read failed"
            ) from exc

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._connection.close()


class _SQLitePinnedFile:
    """Pin source inodes and maintain one private, ephemeral read snapshot.

    The snapshot is a process-local read aid, never a retained evidence artifact
    or product-state write.  Its bytes come only from already-pinned descriptors;
    SQLite never reopens the configured product pathname.
    """

    _SIDECAR_SUFFIXES = ("-wal", "-shm")
    _SNAPSHOT_BASENAME = "marker.sqlite3"
    _COPY_CHUNK_BYTES = 8 * 1024 * 1024

    def __init__(self) -> None:
        self._directory_fd: int | None = None
        self._file_fd: int | None = None
        self._sidecar_fds: dict[str, int] = {}
        self._path: str | None = None
        self._directory_path: str | None = None
        self._basename: str | None = None
        self._directory_fingerprint: tuple[int, int, int, int, int, int] | None = (
            None
        )
        self._file_fingerprint: tuple[int, int, int, int] | None = None
        self._sidecar_fingerprints: dict[
            str, tuple[int, int, int, int] | None
        ] = {}
        self._metadata: os.stat_result | None = None
        self._snapshot_directory: str | None = None
        self._snapshot_directory_fd: int | None = None
        self._snapshot_directory_fingerprint: (
            tuple[int, int, int, int, int, int] | None
        ) = None
        self._snapshot_fds: dict[str, int] = {}
        self._snapshot_fingerprints: dict[str, tuple[int, int, int, int]] = {}
        self._snapshot_generation_hash: str | None = None

    def inspect(self, path: object) -> Mapping[str, object]:
        metadata = self._ensure(path)
        if self._directory_fingerprint is None:
            raise PermissionAuditMarkerEnvironmentError(
                "SQLite exact marker directory is not pinned"
            )
        return {
            "is_symlink": False,
            "is_regular": True,
            "directory_identity_hash": _canonical_hash(
                {"fingerprint": self._directory_fingerprint}
            ),
            "file_identity_hash": self.identity_hash,
            "permission_hash": _canonical_hash(
                {
                    "gid": metadata.st_gid,
                    "mode": metadata.st_mode,
                    "uid": metadata.st_uid,
                }
            ),
            "sidecar_identity_hash": _canonical_hash(
                {
                    suffix: self._sidecar_fingerprints[suffix]
                    for suffix in self._SIDECAR_SUFFIXES
                }
            ),
        }

    @property
    def identity_hash(self) -> str:
        if self._file_fingerprint is None:
            raise PermissionAuditMarkerEnvironmentError(
                "SQLite exact marker file is not pinned"
            )
        return _canonical_hash({"fingerprint": self._file_fingerprint})

    def connection_uri(self, path: object) -> str:
        self._ensure(path)
        if self._snapshot_directory is None:
            self._create_snapshot()
        else:
            self._refresh_snapshot()
        if self._snapshot_directory is None:
            raise PermissionAuditMarkerEnvironmentError(
                "SQLite exact marker private snapshot is unavailable"
            )
        snapshot_path = os.path.join(
            self._snapshot_directory,
            self._SNAPSHOT_BASENAME,
        )
        encoded_path = urllib.parse.quote(snapshot_path, safe="/")
        return f"file:{encoded_path}?mode=ro"

    def refresh_snapshot(self, path: object) -> None:
        self._ensure(path)
        if self._snapshot_directory is None:
            raise PermissionAuditMarkerEnvironmentError(
                "SQLite exact marker private snapshot is unavailable"
            )
        try:
            self._refresh_snapshot()
            self._verify_pinned_state()
        except PermissionAuditMarkerEnvironmentError:
            raise
        except Exception as exc:
            raise PermissionAuditMarkerEnvironmentError(
                "SQLite exact marker private snapshot unavailable"
            ) from exc

    def verify(self, path: object) -> None:
        self._ensure(path)

    def _ensure(self, path: object) -> os.stat_result:
        normalized = os.path.abspath(os.fspath(path))
        try:
            if self._file_fd is None:
                self._pin(normalized)
            if normalized != self._path:
                raise OSError
            return self._verify_pinned_state()
        except PermissionAuditMarkerEnvironmentError:
            raise
        except Exception as exc:
            raise PermissionAuditMarkerEnvironmentError(
                "SQLite exact marker pinned file unavailable"
            ) from exc

    def _pin(self, normalized: str) -> None:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        cloexec = getattr(os, "O_CLOEXEC", None)
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if (
            type(nofollow) is not int
            or type(cloexec) is not int
            or type(directory_flag) is not int
            or os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd
            or os.stat not in os.supports_follow_symlinks
        ):
            raise OSError
        directory_path = os.path.dirname(normalized)
        basename = os.path.basename(normalized)
        directory_fd: int | None = None
        file_fd: int | None = None
        sidecar_fds: dict[str, int] = {}
        try:
            directory_fd = os.open(
                directory_path,
                os.O_RDONLY | directory_flag | nofollow | cloexec,
            )
            directory_metadata = os.fstat(directory_fd)
            directory_path_metadata = os.lstat(directory_path)
            directory_fingerprint = _sqlite_directory_fingerprint(
                directory_metadata
            )
            if (
                not stat.S_ISDIR(directory_metadata.st_mode)
                or stat.S_ISLNK(directory_path_metadata.st_mode)
                or _sqlite_directory_fingerprint(directory_path_metadata)
                != directory_fingerprint
            ):
                raise OSError
            file_fd = os.open(
                basename,
                os.O_RDONLY | nofollow | cloexec,
                dir_fd=directory_fd,
            )
            file_metadata = os.fstat(file_fd)
            file_path_metadata = os.stat(
                basename,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            file_fingerprint = _sqlite_file_fingerprint(file_metadata)
            if (
                not stat.S_ISREG(file_metadata.st_mode)
                or file_metadata.st_nlink != 1
                or _sqlite_file_fingerprint(file_path_metadata) != file_fingerprint
            ):
                raise OSError
            sidecar_fingerprints: dict[
                str, tuple[int, int, int, int] | None
            ] = {}
            for suffix in self._SIDECAR_SUFFIXES:
                sidecar_name = basename + suffix
                try:
                    sidecar_fd = os.open(
                        sidecar_name,
                        os.O_RDONLY | nofollow | cloexec,
                        dir_fd=directory_fd,
                    )
                except FileNotFoundError:
                    sidecar_fingerprints[suffix] = None
                    continue
                sidecar_fds[suffix] = sidecar_fd
                sidecar_metadata = os.fstat(sidecar_fd)
                sidecar_path_metadata = os.stat(
                    sidecar_name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                sidecar_fingerprint = _sqlite_file_fingerprint(sidecar_metadata)
                if (
                    not stat.S_ISREG(sidecar_metadata.st_mode)
                    or sidecar_metadata.st_nlink != 1
                    or sidecar_metadata.st_dev != directory_metadata.st_dev
                    or _sqlite_file_fingerprint(sidecar_path_metadata)
                    != sidecar_fingerprint
                ):
                    raise OSError
                sidecar_fingerprints[suffix] = sidecar_fingerprint
            self._directory_fd = directory_fd
            self._file_fd = file_fd
            self._sidecar_fds = sidecar_fds
            self._path = normalized
            self._directory_path = directory_path
            self._basename = basename
            self._directory_fingerprint = directory_fingerprint
            self._file_fingerprint = file_fingerprint
            self._sidecar_fingerprints = sidecar_fingerprints
            self._metadata = file_metadata
        except BaseException:
            for fd in sidecar_fds.values():
                os.close(fd)
            if file_fd is not None:
                os.close(file_fd)
            if directory_fd is not None:
                os.close(directory_fd)
            raise

    def _source_fds(self) -> dict[str, int]:
        if self._file_fd is None:
            raise OSError
        source_fds = {"": self._file_fd}
        for suffix in self._SIDECAR_SUFFIXES:
            expected = self._sidecar_fingerprints.get(suffix)
            sidecar_fd = self._sidecar_fds.get(suffix)
            if expected is None:
                if sidecar_fd is not None:
                    raise OSError
                continue
            if sidecar_fd is None:
                raise OSError
            source_fds[suffix] = sidecar_fd
        return source_fds

    def _create_snapshot(self) -> None:
        self._verify_pinned_state()
        nofollow = getattr(os, "O_NOFOLLOW", None)
        cloexec = getattr(os, "O_CLOEXEC", None)
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if (
            type(nofollow) is not int
            or type(cloexec) is not int
            or type(directory_flag) is not int
            or os.stat not in os.supports_dir_fd
            or os.stat not in os.supports_follow_symlinks
        ):
            raise OSError
        snapshot_directory = tempfile.mkdtemp(
            prefix="neurons-sqlite-marker-ephemeral-"
        )
        snapshot_directory_fd: int | None = None
        snapshot_fds: dict[str, int] = {}
        try:
            os.chmod(snapshot_directory, 0o700)
            snapshot_directory_fd = os.open(
                snapshot_directory,
                os.O_RDONLY | directory_flag | nofollow | cloexec,
            )
            for suffix in self._source_fds():
                snapshot_fds[suffix] = os.open(
                    self._SNAPSHOT_BASENAME + suffix,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow | cloexec,
                    0o600,
                    dir_fd=snapshot_directory_fd,
                )
            directory_metadata = os.fstat(snapshot_directory_fd)
            directory_path_metadata = os.lstat(snapshot_directory)
            directory_fingerprint = _sqlite_directory_fingerprint(
                directory_metadata
            )
            if (
                not stat.S_ISDIR(directory_metadata.st_mode)
                or stat.S_IMODE(directory_metadata.st_mode) != 0o700
                or stat.S_ISLNK(directory_path_metadata.st_mode)
                or _sqlite_directory_fingerprint(directory_path_metadata)
                != directory_fingerprint
            ):
                raise OSError
            snapshot_fingerprints = {
                suffix: _sqlite_file_fingerprint(os.fstat(fd))
                for suffix, fd in snapshot_fds.items()
            }
            for fingerprint, fd in zip(
                snapshot_fingerprints.values(),
                snapshot_fds.values(),
                strict=True,
            ):
                metadata = os.fstat(fd)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or _sqlite_file_fingerprint(metadata) != fingerprint
                ):
                    raise OSError
            generation_hash = _sqlite_committed_generation_hash(
                self._source_fds()
            )
            _copy_pinned_sqlite_bundle(
                self._source_fds(),
                snapshot_fds,
            )
            self._verify_pinned_state()
            if generation_hash != _sqlite_committed_generation_hash(
                self._source_fds()
            ):
                raise OSError
            self._snapshot_directory = snapshot_directory
            self._snapshot_directory_fd = snapshot_directory_fd
            self._snapshot_directory_fingerprint = directory_fingerprint
            self._snapshot_fds = snapshot_fds
            self._snapshot_fingerprints = snapshot_fingerprints
            self._snapshot_generation_hash = generation_hash
        except BaseException:
            for fd in snapshot_fds.values():
                os.close(fd)
            if snapshot_directory_fd is not None:
                os.close(snapshot_directory_fd)
            shutil.rmtree(snapshot_directory, ignore_errors=True)
            raise

    def _refresh_snapshot(self) -> None:
        self._verify_pinned_state()
        self._verify_snapshot_state()
        generation_hash = _sqlite_committed_generation_hash(self._source_fds())
        if generation_hash == self._snapshot_generation_hash:
            self._verify_pinned_state()
            self._verify_snapshot_state()
            return
        _copy_pinned_sqlite_bundle(
            self._source_fds(),
            self._snapshot_fds,
        )
        self._verify_pinned_state()
        self._verify_snapshot_state()
        if generation_hash != _sqlite_committed_generation_hash(
            self._source_fds()
        ):
            raise OSError
        self._snapshot_generation_hash = generation_hash

    def _verify_snapshot_state(self) -> None:
        if (
            self._snapshot_directory is None
            or self._snapshot_directory_fd is None
            or self._snapshot_directory_fingerprint is None
            or not self._snapshot_fds
            or set(self._snapshot_fds) != set(self._snapshot_fingerprints)
        ):
            raise OSError
        if (
            _sqlite_directory_fingerprint(
                os.fstat(self._snapshot_directory_fd)
            )
            != self._snapshot_directory_fingerprint
            or _sqlite_directory_fingerprint(
                os.lstat(self._snapshot_directory)
            )
            != self._snapshot_directory_fingerprint
        ):
            raise OSError
        for suffix, fd in self._snapshot_fds.items():
            expected = self._snapshot_fingerprints[suffix]
            metadata = os.fstat(fd)
            path_metadata = os.stat(
                self._SNAPSHOT_BASENAME + suffix,
                dir_fd=self._snapshot_directory_fd,
                follow_symlinks=False,
            )
            if (
                _sqlite_file_fingerprint(metadata) != expected
                or _sqlite_file_fingerprint(path_metadata) != expected
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise OSError

    def _verify_pinned_state(self) -> os.stat_result:
        if (
            self._directory_fd is None
            or self._file_fd is None
            or self._directory_path is None
            or self._basename is None
            or self._directory_fingerprint is None
            or self._file_fingerprint is None
        ):
            raise OSError
        if (
            _sqlite_directory_fingerprint(os.fstat(self._directory_fd))
            != self._directory_fingerprint
            or _sqlite_directory_fingerprint(os.lstat(self._directory_path))
            != self._directory_fingerprint
        ):
            raise OSError
        file_metadata = os.fstat(self._file_fd)
        file_path_metadata = os.stat(
            self._basename,
            dir_fd=self._directory_fd,
            follow_symlinks=False,
        )
        if (
            _sqlite_file_fingerprint(file_metadata) != self._file_fingerprint
            or _sqlite_file_fingerprint(file_path_metadata)
            != self._file_fingerprint
            or not stat.S_ISREG(file_metadata.st_mode)
            or file_metadata.st_nlink != 1
        ):
            raise OSError
        for suffix in self._SIDECAR_SUFFIXES:
            expected = self._sidecar_fingerprints[suffix]
            sidecar_name = self._basename + suffix
            if expected is None:
                try:
                    os.stat(
                        sidecar_name,
                        dir_fd=self._directory_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                raise OSError
            sidecar_fd = self._sidecar_fds.get(suffix)
            if sidecar_fd is None:
                raise OSError
            sidecar_metadata = os.fstat(sidecar_fd)
            sidecar_path_metadata = os.stat(
                sidecar_name,
                dir_fd=self._directory_fd,
                follow_symlinks=False,
            )
            if (
                _sqlite_file_fingerprint(sidecar_metadata) != expected
                or _sqlite_file_fingerprint(sidecar_path_metadata) != expected
                or not stat.S_ISREG(sidecar_metadata.st_mode)
                or sidecar_metadata.st_nlink != 1
                or sidecar_metadata.st_dev
                != self._directory_fingerprint[0]
            ):
                raise OSError
        self._metadata = file_metadata
        return file_metadata

    def close(self) -> None:
        first_error: BaseException | None = None
        snapshot_directory = self._snapshot_directory
        for fd in self._snapshot_fds.values():
            try:
                os.close(fd)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        self._snapshot_fds.clear()
        self._snapshot_fingerprints.clear()
        self._snapshot_generation_hash = None
        if self._snapshot_directory_fd is not None:
            try:
                os.close(self._snapshot_directory_fd)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            self._snapshot_directory_fd = None
        self._snapshot_directory = None
        self._snapshot_directory_fingerprint = None
        if snapshot_directory is not None:
            try:
                shutil.rmtree(snapshot_directory)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        for fd in self._sidecar_fds.values():
            try:
                os.close(fd)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        self._sidecar_fds.clear()
        if self._file_fd is not None:
            try:
                os.close(self._file_fd)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            self._file_fd = None
        if self._directory_fd is not None:
            try:
                os.close(self._directory_fd)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            self._directory_fd = None
        if first_error is not None:
            raise first_error


def _sqlite_file_fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_nlink,
        metadata.st_mode,
    )


def _sqlite_directory_fingerprint(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_nlink,
        metadata.st_mode,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _sqlite_content_fingerprint(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_nlink,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _kernel_copy_fd_exact(
    source_fd: int,
    destination_fd: int,
    *,
    length: int,
    chunk_bytes: int,
) -> None:
    if length < 0 or chunk_bytes < 1:
        raise OSError

    def copy_with_copy_file_range() -> None:
        copied = 0
        while copied < length:
            count = os.copy_file_range(
                source_fd,
                destination_fd,
                min(chunk_bytes, length - copied),
                offset_src=copied,
                offset_dst=copied,
            )
            if type(count) is not int or count < 1:
                raise OSError
            copied += count

    def copy_with_sendfile() -> None:
        os.lseek(destination_fd, 0, os.SEEK_SET)
        copied = 0
        while copied < length:
            count = os.sendfile(
                destination_fd,
                source_fd,
                copied,
                min(chunk_bytes, length - copied),
            )
            if type(count) is not int or count < 1:
                raise OSError
            copied += count

    def copy_with_fcopyfile() -> None:
        import posix

        fcopyfile = getattr(posix, "_fcopyfile", None)
        data_flag = getattr(posix, "_COPYFILE_DATA", None)
        if not callable(fcopyfile) or type(data_flag) is not int:
            raise OSError
        os.lseek(source_fd, 0, os.SEEK_SET)
        os.lseek(destination_fd, 0, os.SEEK_SET)
        fcopyfile(source_fd, destination_fd, data_flag)

    os.ftruncate(destination_fd, 0)
    copy_file_range = getattr(os, "copy_file_range", None)
    sendfile = getattr(os, "sendfile", None)
    if callable(copy_file_range):
        try:
            copy_with_copy_file_range()
        except OSError:
            os.ftruncate(destination_fd, 0)
            try:
                copy_with_fcopyfile()
            except (ImportError, OSError):
                if not callable(sendfile):
                    raise
                os.ftruncate(destination_fd, 0)
                copy_with_sendfile()
    else:
        try:
            copy_with_fcopyfile()
        except (ImportError, OSError):
            if not callable(sendfile):
                raise
            os.ftruncate(destination_fd, 0)
            copy_with_sendfile()
    os.ftruncate(destination_fd, length)


def _fd_sha256(fd: int) -> str:
    file_digest = getattr(hashlib, "file_digest", None)
    if not callable(file_digest):
        raise OSError
    saved_offset = os.lseek(fd, 0, os.SEEK_CUR)
    duplicate = os.dup(fd)
    try:
        os.lseek(duplicate, 0, os.SEEK_SET)
        with os.fdopen(duplicate, "rb", closefd=True) as stream:
            duplicate = -1
            return file_digest(stream, "sha256").hexdigest()
    finally:
        if duplicate >= 0:
            os.close(duplicate)
        os.lseek(fd, saved_offset, os.SEEK_SET)


def _fd_sha256_prefix(fd: int, *, length: int) -> str:
    if length < 0:
        raise OSError
    saved_offset = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        with tempfile.TemporaryFile(
            mode="w+b",
            prefix="neurons-sqlite-marker-hash-ephemeral-",
        ) as temporary:
            temporary_fd = temporary.fileno()
            os.fchmod(temporary_fd, 0o600)
            metadata = os.fstat(temporary_fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise OSError
            _kernel_copy_fd_exact(
                fd,
                temporary_fd,
                length=length,
                chunk_bytes=_SQLitePinnedFile._COPY_CHUNK_BYTES,
            )
            return _fd_sha256(temporary_fd)
    finally:
        os.lseek(fd, saved_offset, os.SEEK_SET)


def _wal_committed_length(fd: int, *, total_length: int) -> int:
    if total_length == 0:
        return 0
    if total_length < 32:
        raise OSError
    saved_offset = os.lseek(fd, 0, os.SEEK_CUR)
    duplicate = os.dup(fd)
    try:
        os.lseek(duplicate, 0, os.SEEK_SET)
        with os.fdopen(duplicate, "rb", closefd=True) as stream:
            duplicate = -1
            header = stream.read(32)
            if len(header) != 32:
                raise OSError
            magic = int.from_bytes(header[0:4], "big")
            if magic not in (0x377F0682, 0x377F0683):
                raise OSError
            page_size = int.from_bytes(header[8:12], "big")
            if page_size == 1:
                page_size = 65536
            if (
                page_size < 512
                or page_size > 65536
                or page_size & (page_size - 1)
            ):
                raise OSError
            frame_size = 24 + page_size
            if (total_length - 32) % frame_size:
                raise OSError
            salts = header[16:24]
            offset = 32
            committed = 32
            while offset + frame_size <= total_length:
                frame_header = stream.read(24)
                if len(frame_header) != 24:
                    raise OSError
                if (
                    int.from_bytes(frame_header[0:4], "big") == 0
                    or frame_header[8:16] != salts
                ):
                    raise OSError
                commit_page_count = int.from_bytes(frame_header[4:8], "big")
                next_offset = offset + frame_size
                if commit_page_count:
                    committed = next_offset
                skipped = stream.seek(page_size, os.SEEK_CUR)
                if skipped != next_offset:
                    raise OSError
                offset = next_offset
            if offset != total_length:
                raise OSError
            return committed
    finally:
        if duplicate >= 0:
            os.close(duplicate)
        os.lseek(fd, saved_offset, os.SEEK_SET)


def _sqlite_committed_generation_hash(source_fds: Mapping[str, int]) -> str:
    if not source_fds or "" not in source_fds:
        raise OSError
    has_wal = "-wal" in source_fds
    has_shm = "-shm" in source_fds
    if has_wal != has_shm:
        raise OSError
    before = {
        suffix: _sqlite_content_fingerprint(os.fstat(fd))
        for suffix, fd in source_fds.items()
    }
    members: list[dict[str, object]] = []
    for suffix in sorted(source_fds):
        if suffix == "-shm":
            continue
        fd = source_fds[suffix]
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size < 0
        ):
            raise OSError
        length = metadata.st_size
        if suffix == "-wal":
            length = _wal_committed_length(fd, total_length=length)
        members.append(
            {
                "hash": "sha256:" + _fd_sha256_prefix(fd, length=length),
                "length": length,
                "suffix": suffix,
            }
        )
    after = {
        suffix: _sqlite_content_fingerprint(os.fstat(fd))
        for suffix, fd in source_fds.items()
    }
    if before != after:
        raise OSError
    return _canonical_hash({"members": members})


def _connect_sqlite_uri(database_uri: str) -> sqlite3.Connection:
    return sqlite3.connect(database_uri, uri=True, timeout=5)


def _copy_pinned_sqlite_bundle(
    source_fds: Mapping[str, int],
    destination_fds: Mapping[str, int],
) -> str:
    if (
        not source_fds
        or set(source_fds) != set(destination_fds)
        or "" not in source_fds
    ):
        raise OSError
    before = {
        suffix: _sqlite_content_fingerprint(os.fstat(fd))
        for suffix, fd in source_fds.items()
    }
    for suffix, fingerprint in before.items():
        if (
            not stat.S_ISREG(fingerprint[3])
            or fingerprint[2] != 1
            or fingerprint[4] < 0
        ):
            raise OSError
        _kernel_copy_fd_exact(
            source_fds[suffix],
            destination_fds[suffix],
            length=fingerprint[4],
            chunk_bytes=_SQLitePinnedFile._COPY_CHUNK_BYTES,
        )
    source_hashes = {
        suffix: _fd_sha256(fd) for suffix, fd in source_fds.items()
    }
    destination_hashes = {
        suffix: _fd_sha256(fd) for suffix, fd in destination_fds.items()
    }
    after = {
        suffix: _sqlite_content_fingerprint(os.fstat(fd))
        for suffix, fd in source_fds.items()
    }
    destination_lengths = {
        suffix: os.fstat(fd).st_size for suffix, fd in destination_fds.items()
    }
    if (
        before != after
        or source_hashes != destination_hashes
        or any(
            destination_lengths[suffix] != fingerprint[4]
            for suffix, fingerprint in before.items()
        )
    ):
        raise OSError
    return _sqlite_committed_generation_hash(source_fds)


class _SQLiteReadOnlyConnector:
    """Open only SQLite URI ``mode=ro`` and expose a PRAGMA-only wrapper."""

    def __init__(self, pinned_file: _SQLitePinnedFile | None = None) -> None:
        self._pinned_file = pinned_file or _SQLitePinnedFile()
        self._connections: list[_SQLitePragmaConnection] = []

    def open(
        self,
        path: object,
        *,
        mode: str,
        query_only: bool,
    ) -> _SQLitePragmaConnection:
        if mode != "ro" or query_only is not True:
            raise PermissionAuditMarkerEnvironmentError(
                "SQLite exact marker read-only scope is invalid"
            )
        raw: sqlite3.Connection | None = None
        try:
            self._pinned_file.verify(path)
            raw = _connect_sqlite_uri(self._pinned_file.connection_uri(path))
            self._pinned_file.verify(path)
            raw.execute("PRAGMA query_only=ON")
            self._pinned_file.verify(path)
            connection = _SQLitePragmaConnection(
                raw,
                connection_identity_hash=self._pinned_file.identity_hash,
                verify_pinned_identity=lambda: self._pinned_file.verify(path),
                refresh_pinned_snapshot=lambda: self._pinned_file.refresh_snapshot(
                    path
                ),
            )
            self._connections.append(connection)
            return connection
        except PermissionAuditMarkerEnvironmentError:
            if raw is not None:
                raw.close()
            raise
        except Exception as exc:
            if raw is not None:
                raw.close()
            raise PermissionAuditMarkerEnvironmentError(
                "SQLite exact marker connection unavailable"
            ) from exc

    def close(self) -> None:
        first_error: BaseException | None = None
        for connection in reversed(self._connections):
            try:
                connection.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        self._connections.clear()
        try:
            self._pinned_file.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise PermissionAuditMarkerEnvironmentError(
                "SQLite exact marker connection close failed"
            ) from first_error


class _SQLiteFileInspector:
    def __init__(self, pinned_file: _SQLitePinnedFile | None = None) -> None:
        self._pinned_file = pinned_file or _SQLitePinnedFile()

    def inspect(self, path: object) -> Mapping[str, object]:
        return self._pinned_file.inspect(path)


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _postgres_exact_read_statements(contract: object) -> frozenset[str]:
    """Precompute the exact static and contract-rendered marker SQL set."""

    if not hasattr(contract, "audit_reader_role"):
        raise PermissionAuditMarkerEnvironmentError(
            "PostgreSQL exact marker contract unavailable"
        )
    fence = PostgresExactMutationMarkerFence(contract, object())  # type: ignore[arg-type]
    statements = frozenset(
        {
            PostgresExactMutationMarkerReader._ACQUIRE_SQL,
            PostgresExactMutationMarkerFence._RELEASE_SQL,
            PostgresExactMutationMarkerFence._SESSION_SQL.format(
                audit_role=contract.audit_reader_role  # type: ignore[attr-defined]
            ),
            PostgresExactMutationMarkerFence._PARAMETER_ACL_SQL,
            PostgresExactMutationMarkerFence._PROJECTION_SQL,
            PostgresExactMutationMarkerFence._TRIGGER_SQL,
            PostgresExactMutationMarkerFence._FUNCTION_SQL,
            fence._render_role_sql(),
            fence._render_privileged_role_sql(),
            fence._render_unregistered_writer_sql(),
        }
    )
    if len(statements) != 10:
        raise PermissionAuditMarkerEnvironmentError(
            "PostgreSQL exact marker statement contract invalid"
        )
    return statements


class _PostgresReadOnlyConnection:
    dialect = "postgres"

    def __init__(
        self,
        connection: object,
        *,
        allowed_statements: frozenset[str],
        privileged_credential_inventory_anchor_hash: str,
    ) -> None:
        if not callable(getattr(connection, "execute", None)) or not callable(
            getattr(connection, "close", None)
        ):
            raise PermissionAuditMarkerEnvironmentError(
                "PostgreSQL exact marker connection unavailable"
            )
        self._connection = connection
        _validate_hash(privileged_credential_inventory_anchor_hash)
        self.privileged_credential_inventory_anchor_hash = (
            privileged_credential_inventory_anchor_hash
        )
        if (
            not isinstance(allowed_statements, frozenset)
            or len(allowed_statements) != 10
            or any(not isinstance(value, str) for value in allowed_statements)
        ):
            raise PermissionAuditMarkerEnvironmentError(
                "PostgreSQL exact marker statement contract invalid"
            )
        self._allowed_statements = allowed_statements

    def execute(self, statement: str):
        if statement not in self._allowed_statements:
            raise PermissionAuditMarkerEnvironmentError(
                "PostgreSQL exact marker operation is not allowed"
            )
        try:
            return self._connection.execute(statement)  # type: ignore[attr-defined]
        except Exception as exc:
            raise PermissionAuditMarkerEnvironmentError(
                "PostgreSQL exact marker read failed"
            ) from exc

    def close(self) -> None:
        try:
            self._connection.close()  # type: ignore[attr-defined]
        except Exception as exc:
            raise PermissionAuditMarkerEnvironmentError(
                "PostgreSQL exact marker connection close failed"
            ) from exc


def _open_postgres_read_only(
    dsn: str,
    *,
    connect: Callable[..., object] | None = None,
    allowed_statements: frozenset[str],
    expected_privileged_credential_inventory_anchor_hash: str,
    privileged_credential_inventory_anchor_reader: Callable[[], object]
    | None = None,
) -> _PostgresReadOnlyConnection:
    if not isinstance(dsn, str) or not dsn or len(dsn) > 16_384:
        raise PermissionAuditMarkerEnvironmentError(
            "PostgreSQL exact marker connection unavailable"
        )
    try:
        _validate_hash(expected_privileged_credential_inventory_anchor_hash)
        if privileged_credential_inventory_anchor_reader is None:
            raise ValueError
        observed_anchor = privileged_credential_inventory_anchor_reader()
        _validate_hash(observed_anchor)
        if observed_anchor != expected_privileged_credential_inventory_anchor_hash:
            raise ValueError
        if connect is None:
            import psycopg

            connect = psycopg.connect
        raw = connect(
            dsn,
            autocommit=True,
            connect_timeout=5,
            options=(
                "-c default_transaction_read_only=on "
                "-c statement_timeout=5000 -c lock_timeout=1000"
            ),
        )
        return _PostgresReadOnlyConnection(
            raw,
            allowed_statements=allowed_statements,
            privileged_credential_inventory_anchor_hash=observed_anchor,
        )
    except PermissionAuditMarkerEnvironmentError:
        raise
    except Exception as exc:
        raise PermissionAuditMarkerEnvironmentError(
            "PostgreSQL exact marker connection unavailable"
        ) from exc


class _QdrantMarkerMetadataAdapter:
    """Exact count/filter plus one fixed marker-metadata point read."""

    _METADATA_KEYS = QDRANT_EXACT_MARKER_METADATA_KEYS

    def __init__(
        self,
        scope: Mapping[str, Any],
        credential: str,
        expected_coverage: object,
        *,
        client: object | None = None,
    ) -> None:
        self._url = _validated_qdrant_url(scope["url"])
        self._collection = str(scope["marker_collection"])
        _validate_identifier(self._collection)
        self._metadata_point_id = str(scope["metadata_point_id"])
        uuid.UUID(self._metadata_point_id)
        self._activation_anchor = _qdrant_activation_anchor(scope)
        self._expected_coverage = expected_coverage
        if (
            build_qdrant_coverage_manifest_from_activation_anchor(
                self._activation_anchor
            )
            != expected_coverage
        ):
            raise ValueError
        self._event_position_floor = scope["event_position_floor"]
        self._marker_hash_at_floor = scope["marker_hash_at_floor"]
        self._previous_event_position: int | None = None
        self._previous_marker_hash: str | None = None
        self._last_coverage = None
        if not isinstance(credential, str) or not credential:
            raise ValueError
        if client is None:
            from qdrant_client import QdrantClient

            client = QdrantClient(
                url=self._url,
                port=6333,
                https=True,
                api_key=credential,
                timeout=5,
                prefer_grpc=False,
                trust_env=False,
                follow_redirects=False,
            )
        if (
            not callable(getattr(client, "get_collection", None))
            or not callable(getattr(client, "count", None))
            or not callable(getattr(client, "retrieve", None))
        ):
            raise ValueError
        self._client = client

    def read_marker_metadata(self) -> Mapping[str, object]:
        try:
            from qdrant_client import models

            def exact_filter(**values: object):
                return models.Filter(
                    must=[
                        models.FieldCondition(
                            key=key,
                            match=models.MatchValue(value=value),
                        )
                        for key, value in values.items()
                    ]
                )

            event_filter = exact_filter(
                record_kind=QDRANT_MARKER_EVENT_RECORD_KIND,
            )
            generation_filter = exact_filter(
                record_kind=QDRANT_MARKER_EVENT_RECORD_KIND,
                generation=self._activation_anchor.generation,
            )
            start_filter = exact_filter(
                record_kind=QDRANT_MARKER_EVENT_RECORD_KIND,
                generation=self._activation_anchor.generation,
                phase=QDRANT_MARKER_PHASE_START,
            )
            terminal_filter = exact_filter(
                record_kind=QDRANT_MARKER_EVENT_RECORD_KIND,
                generation=self._activation_anchor.generation,
                phase=QDRANT_MARKER_PHASE_TERMINAL,
            )
            bypass_clear_filter = exact_filter(
                record_kind=QDRANT_MARKER_EVENT_RECORD_KIND,
                bypass=False,
            )
            pod_present_filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key="record_kind",
                        match=models.MatchValue(
                            value=QDRANT_MARKER_EVENT_RECORD_KIND
                        ),
                    )
                ],
                must_not=[
                    models.IsEmptyCondition(
                        is_empty=models.PayloadField(key="pod_ref_hash")
                    )
                ],
            )
            unresolved_filter = exact_filter(
                record_kind=QDRANT_MARKER_EVENT_RECORD_KIND,
                generation=self._activation_anchor.generation,
                phase=QDRANT_MARKER_PHASE_START,
                unresolved=True,
                bypass=False,
            )
            source_filters = tuple(
                exact_filter(
                    record_kind=QDRANT_MARKER_EVENT_RECORD_KIND,
                    generation=self._activation_anchor.generation,
                    route=item.route.value,
                    writer_ref_hash=item.writer_ref_hash,
                    workload_ref_hash=item.workload_ref_hash,
                    route_set_hash=item.route_set_hash,
                    bypass=False,
                )
                for item in self._activation_anchor.rendered_inventory
                if item.source in ACTIVE_QDRANT_MUTATION_SOURCES
            )
            self._validate_payload_indexes()
            point_count = self._strict_count(None)
            event_count = self._strict_count(event_filter)
            generation_count = self._strict_count(generation_filter)
            start_count = self._strict_count(start_filter)
            terminal_count = self._strict_count(terminal_filter)
            bypass_clear_count = self._strict_count(bypass_clear_filter)
            pod_present_count = self._strict_count(pod_present_filter)
            source_count = sum(
                self._strict_count(source_filter)
                for source_filter in source_filters
            )
            unresolved_count = self._strict_count(unresolved_filter)
            if (
                point_count != event_count + 1
                or generation_count != event_count
                or start_count + terminal_count != event_count
                or bypass_clear_count != event_count
                or pod_present_count != event_count
                or source_count != event_count
                or unresolved_count > start_count
            ):
                raise ValueError
            points = self._client.retrieve(  # type: ignore[attr-defined]
                collection_name=self._collection,
                ids=[self._metadata_point_id],
                with_payload=True,
                with_vectors=False,
                consistency=models.ReadConsistencyType.ALL,
            )
            if not isinstance(points, list) or len(points) != 1:
                raise ValueError
            if str(getattr(points[0], "id", "")) != self._metadata_point_id:
                raise ValueError
            payload = getattr(points[0], "payload", None)
            if not isinstance(payload, Mapping) or set(payload) != self._METADATA_KEYS:
                raise ValueError
            if payload["schema_version"] != QDRANT_EXACT_MARKER_METADATA_SCHEMA:
                raise ValueError
            if (
                payload["generation"] != self._activation_anchor.generation
                or payload["coverage_hash"]
                != self._expected_coverage.coverage_hash
                or payload["coverage_status"] != "complete"
                or payload["bypass_count"] != 0
                or payload["activation_hash"]
                != self._activation_anchor.activation_hash
                or payload["previous_generation_hash"]
                != self._activation_anchor.previous_generation_hash
            ):
                raise ValueError
            marker_hash = _qdrant_marker_hash(
                generation=payload["generation"],
                event_count=event_count,
                unresolved_count=unresolved_count,
                coverage_hash=payload["coverage_hash"],
            )
            if self._previous_event_position is None:
                if (
                    event_count != self._event_position_floor
                    or marker_hash != self._marker_hash_at_floor
                ):
                    raise ValueError
            elif event_count < self._previous_event_position or (
                event_count == self._previous_event_position
                and marker_hash != self._previous_marker_hash
            ):
                raise ValueError
            self._previous_event_position = event_count
            self._previous_marker_hash = marker_hash
            self._last_coverage = self._expected_coverage
            return {
                "generation": payload["generation"],
                "event_position": event_count,
                "marker_hash": marker_hash,
                "in_flight_count": unresolved_count,
                "coverage_hash": payload["coverage_hash"],
                "coverage_status": "complete",
                "count_status": "exact",
                "reset_count": 0,
                "bypass_count": 0,
            }
        except Exception as exc:
            raise PermissionAuditMarkerEnvironmentError(
                "Qdrant exact marker metadata read failed"
            ) from exc

    def _validate_payload_indexes(self) -> None:
        info = self._client.get_collection(  # type: ignore[attr-defined]
            collection_name=self._collection
        )
        config = _member(info, "config")
        params = _member(config, "params")
        for field in (
            "shard_number",
            "replication_factor",
            "write_consistency_factor",
        ):
            if _member(params, field) != 1:
                raise ValueError
        schema = _member(info, "payload_schema")
        if not isinstance(schema, Mapping):
            raise ValueError
        expected_types = {
            "record_kind": "keyword",
            "phase": "keyword",
            "unresolved": "bool",
            "generation": "integer",
            "route": "keyword",
            "writer_ref_hash": "keyword",
            "pod_ref_hash": "keyword",
            "workload_ref_hash": "keyword",
            "route_set_hash": "keyword",
            "bypass": "bool",
        }
        if tuple(expected_types) != QDRANT_MARKER_REQUIRED_PAYLOAD_INDEX_FIELDS:
            raise ValueError
        for field, expected_type in expected_types.items():
            entry = _member(schema, field)
            data_type = _member(entry, "data_type")
            normalized = getattr(data_type, "value", data_type)
            if str(normalized).casefold().rsplit(".", 1)[-1] != expected_type:
                raise ValueError

    def _strict_count(self, filter_value: object) -> int:
        result = self._client.count(  # type: ignore[attr-defined]
            collection_name=self._collection,
            count_filter=filter_value,
            exact=True,
        )
        count = getattr(result, "count", None)
        if type(count) is not int or not 0 <= count < 2**63:
            raise ValueError
        return count

    def read_coverage_manifest(self):
        if self._last_coverage is None:
            raise PermissionAuditMarkerEnvironmentError(
                "Qdrant exact marker coverage unavailable"
            )
        return self._last_coverage

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                raise PermissionAuditMarkerEnvironmentError(
                    "Qdrant exact marker connection close failed"
                ) from exc


def _qdrant_marker_hash(
    *,
    generation: int,
    event_count: int,
    unresolved_count: int,
    coverage_hash: str,
) -> str:
    try:
        return build_qdrant_exact_marker_hash(
            generation=generation,
            event_position=event_count,
            in_flight_count=unresolved_count,
            coverage_hash=coverage_hash,
        )
    except ValueError as exc:
        raise PermissionAuditMarkerEnvironmentError(
            "Qdrant exact marker metadata is malformed"
        ) from exc


def _build_qdrant_adapter(
    scope: Mapping[str, Any],
    credential: str,
    expected_coverage: object,
) -> _QdrantMarkerMetadataAdapter:
    return _QdrantMarkerMetadataAdapter(scope, credential, expected_coverage)


class _NatsStreamMetadataAdapter:
    """Bounded async bridge for fixed JetStream metadata calls only."""

    def __init__(
        self,
        scope: Mapping[str, Any],
        credential: str,
        generation_hash: str,
        *,
        connect: Callable[..., Any] | None = None,
        publisher_authorization_anchor_reader: Callable[[], object] | None = None,
    ) -> None:
        self._server_url = _validated_base_url(
            scope["server_url"], schemes={"nats", "tls"}
        )
        self._stream = str(scope["stream"])
        _validate_identifier(self._stream)
        self._durables = tuple(str(value) for value in scope["durables"])
        for durable in self._durables:
            _validate_identifier(durable)
        self._expected_publisher_authorization_anchor_hash = str(
            scope["publisher_authorization_anchor_hash"]
        )
        _validate_hash(self._expected_publisher_authorization_anchor_hash)
        self._expected_stream_generation_hash = str(
            scope["stream_generation_hash"]
        )
        _validate_hash(self._expected_stream_generation_hash)
        self._expected_durable_generation_hashes = tuple(
            str(value) for value in scope["durable_generation_hashes"]
        )
        if len(self._expected_durable_generation_hashes) != len(self._durables):
            raise ValueError
        for value in self._expected_durable_generation_hashes:
            _validate_hash(value)
        if not isinstance(credential, str) or not credential:
            raise ValueError
        _validate_hash(generation_hash)
        self._credential = credential
        self._generation_hash = generation_hash
        if connect is None:
            import nats

            connect = nats.connect
        self._connect = connect
        self._publisher_authorization_anchor_reader = (
            publisher_authorization_anchor_reader
        )
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="permission-audit-nats-marker",
        )
        self._closed = False

    def read_stream_and_durable_metadata(self) -> Mapping[str, object]:
        if self._closed:
            raise PermissionAuditMarkerEnvironmentError(
                "NATS exact marker adapter is closed"
            )
        try:
            if self._publisher_authorization_anchor_reader is None:
                raise ValueError
            publisher_authorization_anchor_hash = (
                self._publisher_authorization_anchor_reader()
            )
            _validate_hash(publisher_authorization_anchor_hash)
            if (
                publisher_authorization_anchor_hash
                != self._expected_publisher_authorization_anchor_hash
            ):
                raise ValueError
            future = self._executor.submit(
                lambda: asyncio.run(
                    self._read_once(publisher_authorization_anchor_hash)
                )
            )
            return future.result(timeout=15)
        except Exception as exc:
            raise PermissionAuditMarkerEnvironmentError(
                "NATS exact marker metadata read failed"
            ) from exc

    async def _read_once(
        self,
        publisher_authorization_anchor_hash: str,
    ) -> Mapping[str, object]:
        connection = None
        try:
            connection = await self._connect(
                servers=[self._server_url],
                token=self._credential,
                allow_reconnect=False,
                max_reconnect_attempts=0,
                connect_timeout=5,
            )
            jetstream = connection.jetstream()
            stream_info = await jetstream.stream_info(self._stream)
            stream_state = _member(stream_info, "state")
            stream_config = _member(stream_info, "config")
            subjects = tuple(_member(stream_config, "subjects"))
            observed_generation = _nats_stream_generation_hash(
                created=str(_member(stream_info, "created")),
                stream=self._stream,
                subjects=subjects,
            )
            if observed_generation != self._expected_stream_generation_hash:
                raise ValueError
            stream = {
                "generation_hash": self._generation_hash,
                "messages": _member(stream_state, "messages"),
                "bytes": _member(stream_state, "bytes"),
                "first_seq": _member(stream_state, "first_seq"),
                "last_seq": _member(stream_state, "last_seq"),
                "num_deleted": _member(stream_state, "num_deleted"),
                "consumer_count": _member(stream_state, "consumer_count"),
            }
            consumer_count = stream["consumer_count"]
            if type(consumer_count) is not int or consumer_count < len(self._durables):
                raise ValueError
            consumers = []
            for durable, expected_generation in zip(
                self._durables,
                self._expected_durable_generation_hashes,
                strict=True,
            ):
                info = await jetstream.consumer_info(self._stream, durable)
                delivered = _member(info, "delivered")
                ack_floor = _member(info, "ack_floor")
                consumer_generation = _nats_consumer_generation_hash(
                    created=str(_member(info, "created")),
                    stream=self._stream,
                    durable=durable,
                )
                if consumer_generation != expected_generation:
                    raise ValueError
                consumers.append(
                    {
                        "generation_hash": consumer_generation,
                        "delivered_stream_seq": _member(delivered, "stream_seq"),
                        "ack_floor_stream_seq": _member(ack_floor, "stream_seq"),
                        "num_ack_pending": _member(info, "num_ack_pending"),
                        "num_redelivered": _member(info, "num_redelivered"),
                        "num_waiting": _member(info, "num_waiting"),
                        "num_pending": _member(info, "num_pending"),
                    }
                )
            return {
                "stream": stream,
                "durable_consumers": consumers,
                "unknown_durable_count": consumer_count - len(self._durables),
                "publisher_authorization_anchor_hash": (
                    publisher_authorization_anchor_hash
                ),
            }
        finally:
            if connection is not None:
                await connection.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._executor.shutdown(wait=True, cancel_futures=True)


def _member(value: object, key: str) -> Any:
    if isinstance(value, Mapping):
        if key not in value:
            raise ValueError
        return value[key]
    result = getattr(value, key, None)
    if result is None:
        raise ValueError
    return result


def _nats_stream_generation_hash(
    *,
    created: str,
    stream: str,
    subjects: tuple[str, ...],
) -> str:
    _validate_bounded_text(created)
    _validate_identifier(stream)
    if (
        not isinstance(subjects, tuple)
        or not subjects
        or len(subjects) != len(set(subjects))
    ):
        raise ValueError
    for subject in subjects:
        _validate_bounded_text(subject)
    return _canonical_hash(
        {
            "created": created,
            "schema_version": "nats_stream_generation.v1",
            "stream": stream,
            "subjects": sorted(subjects),
        }
    )


def _nats_consumer_generation_hash(
    *,
    created: str,
    stream: str,
    durable: str,
) -> str:
    _validate_bounded_text(created)
    _validate_identifier(stream)
    _validate_identifier(durable)
    return _canonical_hash(
        {
            "created": created,
            "durable": durable,
            "schema_version": "nats_consumer_generation.v1",
            "stream": stream,
        }
    )


def _build_nats_adapter(
    scope: Mapping[str, Any],
    credential: str,
    generation_hash: str,
) -> _NatsStreamMetadataAdapter:
    return _NatsStreamMetadataAdapter(scope, credential, generation_hash)


__all__ = [
    "PermissionAuditMarkerEnvironmentError",
    "ProductionMarkerAdapterFactories",
    "build_production_permission_audit_marker_reader",
]
