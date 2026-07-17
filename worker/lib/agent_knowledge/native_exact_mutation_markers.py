"""Strict storage-native exact mutation marker providers.

The providers intentionally depend on narrow metadata-only adapters instead of live
storage SDKs.  Raw documents, messages, rows, names, subjects, paths, and topology
never cross the provider return boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Protocol


_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_OPAQUE_SEQUENCE_PATTERN = re.compile(r"([0-9]+)-[A-Za-z0-9_+/=-]+\Z")
_MAX_COUNTER = 2**63 - 1
_CONTRACT_VERSION = "storage_native_exact_mutation_marker.v1"
_PLANES = frozenset({"corpus", "queue", "product_db"})
_RECORD_KEYS = (
    "plane",
    "generation_hash",
    "event_position_hash",
    "marker_hash",
    "in_flight_count",
    "in_flight_status",
    "coverage_hash",
    "coverage_status",
    "read_scope_status",
    "reset_or_decrease_count",
    "read_call_count",
)


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class NativeExactMutationMarkerError(RuntimeError):
    """Fail-closed storage-native marker contract failure."""


@dataclass(frozen=True)
class NativeExactMutationMarkerContract:
    """Canonical source coverage expected by one native marker provider."""

    plane: str
    storage_identity: str
    schema_generation: str
    config_generation: str
    reader_contract: str
    writer_registry: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.plane not in _PLANES:
            _invalid_contract()
        for value in (self.storage_identity, self.reader_contract):
            if not _is_bounded_text(value):
                _invalid_contract()
        for value in (self.schema_generation, self.config_generation):
            if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
                _invalid_contract()
        if not isinstance(self.writer_registry, tuple) or not self.writer_registry:
            _invalid_contract()
        if any(not _is_bounded_text(value) for value in self.writer_registry):
            _invalid_contract()
        if len(set(self.writer_registry)) != len(self.writer_registry):
            _invalid_contract()
        object.__setattr__(self, "writer_registry", tuple(sorted(self.writer_registry)))

    @property
    def generation_hash(self) -> str:
        return _canonical_hash(
            {
                "config_generation": self.config_generation,
                "schema_generation": self.schema_generation,
                "storage_identity": self.storage_identity,
            }
        )

    @property
    def expected_coverage_hash(self) -> str:
        return _canonical_hash(
            {
                "config_generation": self.config_generation,
                "contract_version": _CONTRACT_VERSION,
                "plane": self.plane,
                "reader_contract": self.reader_contract,
                "schema_generation": self.schema_generation,
                "storage_identity": self.storage_identity,
                "writer_registry": list(self.writer_registry),
            }
        )


class CouchDBMetadataAdapter(Protocol):
    def read_database_info(self) -> Mapping[str, object]: ...


class NatsJetStreamMetadataAdapter(Protocol):
    def read_stream_and_durable_metadata(self) -> Mapping[str, object]: ...


class SQLiteFileInspector(Protocol):
    def inspect(self, path: object) -> Mapping[str, object]: ...


class SQLitePragmaConnection(Protocol):
    connection_identity_hash: str

    def read_pragma(self, name: str) -> object: ...


class SQLiteReadOnlyConnector(Protocol):
    def open(
        self,
        path: object,
        *,
        mode: str,
        query_only: bool,
    ) -> SQLitePragmaConnection: ...


class _PrePostProvider:
    """Stateful same-instance pre/post capture discipline."""

    _capture_open: bool

    def capture_before(self) -> dict[str, object]:
        if self._capture_open:
            raise NativeExactMutationMarkerError(
                "native exact marker pre/post window is already open"
            )
        marker = self.read_marker()  # type: ignore[attr-defined]
        self._capture_open = True
        return marker

    def capture_after(self) -> dict[str, object]:
        if not self._capture_open:
            raise NativeExactMutationMarkerError(
                "native exact marker pre/post window is not open"
            )
        try:
            return self.read_marker()  # type: ignore[attr-defined]
        finally:
            self._capture_open = False

    def compare_pre_post(
        self,
        action: Callable[[], Any],
    ) -> tuple[dict[str, object], Any, dict[str, object]]:
        """Capture around one caller-owned action using this provider instance."""

        before = self.capture_before()
        try:
            action_result = action()
        except BaseException:
            self._capture_open = False
            raise
        after = self.capture_after()
        return before, action_result, after


class CouchDBExactMutationMarkerProvider(_PrePostProvider):
    """Hash fixed CouchDB DB-info metadata and reject lifetime resets."""

    _INFO_KEYS = frozenset(
        {
            "continuity_anchor_hash",
            "update_seq",
            "purge_seq",
            "doc_count",
            "doc_del_count",
        }
    )

    def __init__(
        self,
        contract: NativeExactMutationMarkerContract,
        adapter: CouchDBMetadataAdapter,
        *,
        expected_continuity_anchor_hash: str,
    ) -> None:
        _require_plane(contract, "corpus")
        if not callable(getattr(adapter, "read_database_info", None)):
            raise TypeError("CouchDB metadata adapter is required")
        self._contract = contract
        self._adapter = adapter
        self._expected_continuity_anchor_hash = _strict_hash(
            expected_continuity_anchor_hash,
            "CouchDB",
        )
        self._previous: tuple[int, int] | None = None
        self._capture_open = False

    def __call__(self) -> dict[str, object]:
        return self.read_marker()

    def read_marker(self) -> dict[str, object]:
        try:
            info = self._adapter.read_database_info()
            if not isinstance(info, Mapping):
                raise NativeExactMutationMarkerError(
                    "CouchDB exact marker metadata is malformed"
                )
            if "continuity_anchor_hash" not in info:
                raise NativeExactMutationMarkerError(
                    "CouchDB exact marker continuity anchor is unavailable"
                )
            if set(info) != self._INFO_KEYS:
                raise NativeExactMutationMarkerError(
                    "CouchDB exact marker metadata is malformed"
                )
            continuity_anchor_hash = _strict_hash(
                info["continuity_anchor_hash"],
                "CouchDB",
            )
            if continuity_anchor_hash != self._expected_continuity_anchor_hash:
                raise NativeExactMutationMarkerError(
                    "CouchDB exact marker continuity anchor is invalid"
                )
            update_raw, update_position = _strict_sequence(info["update_seq"])
            purge_raw, purge_position = _strict_sequence(info["purge_seq"])
            doc_count = _strict_counter(info["doc_count"], source="CouchDB")
            doc_del_count = _strict_counter(
                info["doc_del_count"], source="CouchDB"
            )
            current = (update_position, purge_position)
            if self._previous is not None and any(
                after < before
                for before, after in zip(self._previous, current, strict=True)
            ):
                raise NativeExactMutationMarkerError(
                    "CouchDB exact marker decreased or recreated"
                )
            event_payload = {
                "purge_seq": purge_raw,
                "update_seq": update_raw,
            }
            marker_payload = {
                **event_payload,
                "continuity_anchor_hash": continuity_anchor_hash,
                "doc_count": doc_count,
                "doc_del_count": doc_del_count,
            }
            marker = _record(
                contract=self._contract,
                event_position_hash=_canonical_hash(event_payload),
                marker_hash=_canonical_hash(marker_payload),
            )
            self._previous = current
            return marker
        except NativeExactMutationMarkerError:
            raise
        except Exception as exc:
            raise NativeExactMutationMarkerError(
                "CouchDB exact marker read failed"
            ) from exc


class NatsJetStreamExactMutationMarkerProvider(_PrePostProvider):
    """Validate fixed JetStream stream and durable-consumer counters."""

    _TOP_KEYS = frozenset(
        {
            "stream",
            "durable_consumers",
            "unknown_durable_count",
            "publisher_authorization_anchor_hash",
        }
    )
    _STREAM_KEYS = frozenset(
        {
            "generation_hash",
            "messages",
            "bytes",
            "first_seq",
            "last_seq",
            "num_deleted",
            "consumer_count",
        }
    )
    _CONSUMER_KEYS = frozenset(
        {
            "generation_hash",
            "delivered_stream_seq",
            "ack_floor_stream_seq",
            "num_ack_pending",
            "num_redelivered",
            "num_waiting",
            "num_pending",
        }
    )

    def __init__(
        self,
        contract: NativeExactMutationMarkerContract,
        adapter: NatsJetStreamMetadataAdapter,
        *,
        expected_durable_count: int,
        expected_publisher_authorization_anchor_hash: str,
    ) -> None:
        _require_plane(contract, "queue")
        if not callable(
            getattr(adapter, "read_stream_and_durable_metadata", None)
        ):
            raise TypeError("NATS JetStream metadata adapter is required")
        if (
            isinstance(expected_durable_count, bool)
            or not isinstance(expected_durable_count, int)
            or not 0 < expected_durable_count <= _MAX_COUNTER
        ):
            raise ValueError("NATS exact marker expected durable count is invalid")
        self._contract = contract
        self._adapter = adapter
        self._expected_durable_count = expected_durable_count
        self._expected_publisher_authorization_anchor_hash = _strict_hash(
            expected_publisher_authorization_anchor_hash,
            "NATS",
        )
        self._previous: tuple[object, ...] | None = None
        self._capture_open = False

    def __call__(self) -> dict[str, object]:
        return self.read_marker()

    def read_marker(self) -> dict[str, object]:
        try:
            metadata = self._adapter.read_stream_and_durable_metadata()
            if not isinstance(metadata, Mapping):
                raise NativeExactMutationMarkerError(
                    "NATS exact marker metadata is malformed"
                )
            if "publisher_authorization_anchor_hash" not in metadata:
                raise NativeExactMutationMarkerError(
                    "NATS exact marker publisher coverage is unavailable"
                )
            if set(metadata) != self._TOP_KEYS:
                raise NativeExactMutationMarkerError(
                    "NATS exact marker metadata is malformed"
                )
            if (
                _strict_counter(
                    metadata["unknown_durable_count"], source="NATS"
                )
                != 0
            ):
                raise NativeExactMutationMarkerError(
                    "NATS exact marker durable coverage is invalid"
                )
            publisher_authorization_anchor_hash = _strict_hash(
                metadata["publisher_authorization_anchor_hash"],
                "NATS",
            )
            if (
                publisher_authorization_anchor_hash
                != self._expected_publisher_authorization_anchor_hash
            ):
                raise NativeExactMutationMarkerError(
                    "NATS exact marker publisher coverage is invalid"
                )
            stream = _strict_mapping(metadata["stream"], self._STREAM_KEYS, "NATS")
            stream_generation = _strict_hash(stream["generation_hash"], "NATS")
            if stream_generation != self._contract.generation_hash:
                raise NativeExactMutationMarkerError(
                    "NATS exact marker generation is unexpected"
                )
            stream_counters = {
                key: _strict_counter(stream[key], source="NATS")
                for key in self._STREAM_KEYS
                if key != "generation_hash"
            }
            consumers = metadata["durable_consumers"]
            if (
                not isinstance(consumers, Sequence)
                or isinstance(consumers, (str, bytes, bytearray))
                or len(consumers) != self._expected_durable_count
                or stream_counters["consumer_count"] != self._expected_durable_count
            ):
                raise NativeExactMutationMarkerError(
                    "NATS exact marker durable coverage is invalid"
                )
            parsed_consumers: list[dict[str, object]] = []
            generation_hashes: list[str] = []
            monotonic_positions: list[int] = [stream_counters["last_seq"]]
            for item in consumers:
                consumer = _strict_mapping(item, self._CONSUMER_KEYS, "NATS")
                generation_hash = _strict_hash(
                    consumer["generation_hash"], "NATS"
                )
                generation_hashes.append(generation_hash)
                parsed = {
                    key: _strict_counter(consumer[key], source="NATS")
                    for key in self._CONSUMER_KEYS
                    if key != "generation_hash"
                }
                if (
                    parsed["ack_floor_stream_seq"] > parsed["delivered_stream_seq"]
                    or parsed["delivered_stream_seq"] > stream_counters["last_seq"]
                ):
                    raise NativeExactMutationMarkerError(
                        "NATS exact marker metadata is malformed"
                    )
                parsed_consumers.append(
                    {"generation_hash": generation_hash, **parsed}
                )
                monotonic_positions.extend(
                    (
                        parsed["delivered_stream_seq"],
                        parsed["ack_floor_stream_seq"],
                    )
                )
            if (
                stream_counters["messages"] > 0
                and (
                    stream_counters["first_seq"] > stream_counters["last_seq"]
                    or stream_counters["last_seq"]
                    - stream_counters["first_seq"]
                    + 1
                    != stream_counters["messages"] + stream_counters["num_deleted"]
                )
            ):
                raise NativeExactMutationMarkerError(
                    "NATS exact marker metadata is malformed"
                )
            current: tuple[object, ...] = (
                stream_generation,
                tuple(generation_hashes),
                tuple(monotonic_positions),
            )
            if self._previous is not None:
                previous_generation, previous_consumers, previous_positions = self._previous
                if (
                    current[0] != previous_generation
                    or current[1] != previous_consumers
                    or any(
                        after < before
                        for before, after in zip(
                            previous_positions,  # type: ignore[arg-type]
                            current[2],  # type: ignore[arg-type]
                            strict=True,
                        )
                    )
                ):
                    raise NativeExactMutationMarkerError(
                        "NATS exact marker decreased or recreated"
                    )
            payload = {
                "publisher_authorization_anchor_hash": (
                    publisher_authorization_anchor_hash
                ),
                "stream": {"generation_hash": stream_generation, **stream_counters},
                "durable_consumers": parsed_consumers,
            }
            event_payload = {
                "last_seq": stream_counters["last_seq"],
                "consumer_positions": [
                    {
                        "ack_floor_stream_seq": item["ack_floor_stream_seq"],
                        "delivered_stream_seq": item["delivered_stream_seq"],
                    }
                    for item in parsed_consumers
                ],
            }
            marker = _record(
                contract=self._contract,
                event_position_hash=_canonical_hash(event_payload),
                marker_hash=_canonical_hash(payload),
            )
            self._previous = current
            return marker
        except NativeExactMutationMarkerError:
            raise
        except Exception as exc:
            raise NativeExactMutationMarkerError(
                "NATS exact marker read failed"
            ) from exc


NATSJetStreamExactMutationMarkerProvider = NatsJetStreamExactMutationMarkerProvider


class SQLiteExactMutationMarkerProvider(_PrePostProvider):
    """Read fixed SQLite PRAGMAs through one mode=ro/query_only connection."""

    _FILE_KEYS = frozenset(
        {
            "is_symlink",
            "is_regular",
            "directory_identity_hash",
            "file_identity_hash",
            "permission_hash",
            "sidecar_identity_hash",
        }
    )
    _PRAGMAS = (
        "query_only",
        "data_version",
        "page_count",
        "schema_version",
        "freelist_count",
    )

    def __init__(
        self,
        contract: NativeExactMutationMarkerContract,
        *,
        path: object,
        connector: SQLiteReadOnlyConnector,
        file_inspector: SQLiteFileInspector,
        expected_schema_version: int,
    ) -> None:
        _require_plane(contract, "product_db")
        if not callable(getattr(connector, "open", None)):
            raise TypeError("SQLite read-only connector is required")
        if not callable(getattr(file_inspector, "inspect", None)):
            raise TypeError("SQLite file inspector is required")
        if (
            isinstance(expected_schema_version, bool)
            or not isinstance(expected_schema_version, int)
            or not 0 <= expected_schema_version <= _MAX_COUNTER
        ):
            raise ValueError("SQLite expected schema version is invalid")
        self._expected_schema_version = expected_schema_version
        self._contract = contract
        self._path = path
        self._connector = connector
        self._file_inspector = file_inspector
        self._connection: SQLitePragmaConnection | None = None
        self._connection_object_id: int | None = None
        self._connection_identity_hash: str | None = None
        self._directory_identity_hash: str | None = None
        self._file_identity_hash: str | None = None
        self._permission_hash: str | None = None
        self._sidecar_identity_hash: str | None = None
        self._capture_open = False

    def __call__(self) -> dict[str, object]:
        return self.read_marker()

    def read_marker(self) -> dict[str, object]:
        try:
            file_metadata = _strict_mapping(
                self._file_inspector.inspect(self._path),
                self._FILE_KEYS,
                "SQLite",
            )
            if file_metadata["is_symlink"] is not False:
                raise NativeExactMutationMarkerError(
                    "SQLite exact marker path is a symlink"
                )
            if file_metadata["is_regular"] is not True:
                raise NativeExactMutationMarkerError(
                    "SQLite exact marker path is not a regular file"
                )
            file_identity_hash = _strict_hash(
                file_metadata["file_identity_hash"], "SQLite"
            )
            directory_identity_hash = _strict_hash(
                file_metadata["directory_identity_hash"], "SQLite"
            )
            permission_hash = _strict_hash(
                file_metadata["permission_hash"], "SQLite"
            )
            sidecar_identity_hash = _strict_hash(
                file_metadata["sidecar_identity_hash"], "SQLite"
            )
            if self._directory_identity_hash not in (
                None,
                directory_identity_hash,
            ):
                raise NativeExactMutationMarkerError(
                    "SQLite exact marker directory was replaced"
                )
            if self._file_identity_hash not in (None, file_identity_hash):
                raise NativeExactMutationMarkerError(
                    "SQLite exact marker file was replaced"
                )
            if self._permission_hash not in (None, permission_hash):
                raise NativeExactMutationMarkerError(
                    "SQLite exact marker permission changed"
                )
            if self._sidecar_identity_hash not in (None, sidecar_identity_hash):
                raise NativeExactMutationMarkerError(
                    "SQLite exact marker sidecar was replaced"
                )
            connection = self._ensure_connection()
            connection_identity_hash = _strict_hash(
                getattr(connection, "connection_identity_hash", None),
                "SQLite",
            )
            if (
                id(connection) != self._connection_object_id
                or self._connection_identity_hash not in (
                    None,
                    connection_identity_hash,
                )
            ):
                raise NativeExactMutationMarkerError(
                    "SQLite exact marker connection replaced"
                )
            values = {
                name: _strict_counter(
                    connection.read_pragma(name), source="SQLite"
                )
                for name in self._PRAGMAS
            }
            if values["query_only"] != 1:
                raise NativeExactMutationMarkerError(
                    "SQLite exact marker query_only scope is invalid"
                )
            if values["schema_version"] != self._expected_schema_version:
                raise NativeExactMutationMarkerError(
                    "SQLite exact marker schema drift detected"
                )
            event_payload = {
                "data_version": values["data_version"],
                "schema_version": values["schema_version"],
            }
            marker_payload = {
                **values,
                "connection_identity_hash": connection_identity_hash,
                "directory_identity_hash": directory_identity_hash,
                "file_identity_hash": file_identity_hash,
                "permission_hash": permission_hash,
                "sidecar_identity_hash": sidecar_identity_hash,
            }
            marker = _record(
                contract=self._contract,
                event_position_hash=_canonical_hash(event_payload),
                marker_hash=_canonical_hash(marker_payload),
            )
            self._connection_identity_hash = connection_identity_hash
            self._directory_identity_hash = directory_identity_hash
            self._file_identity_hash = file_identity_hash
            self._permission_hash = permission_hash
            self._sidecar_identity_hash = sidecar_identity_hash
            return marker
        except NativeExactMutationMarkerError:
            raise
        except Exception as exc:
            raise NativeExactMutationMarkerError(
                "SQLite exact marker read failed"
            ) from exc

    def _ensure_connection(self) -> SQLitePragmaConnection:
        if self._connection is None:
            connection = self._connector.open(
                self._path,
                mode="ro",
                query_only=True,
            )
            if not callable(getattr(connection, "read_pragma", None)):
                raise NativeExactMutationMarkerError(
                    "SQLite exact marker connection is malformed"
                )
            self._connection = connection
            self._connection_object_id = id(connection)
        return self._connection


def _record(
    *,
    contract: NativeExactMutationMarkerContract,
    event_position_hash: str,
    marker_hash: str,
) -> dict[str, object]:
    record: dict[str, object] = {
        "plane": contract.plane,
        "generation_hash": contract.generation_hash,
        "event_position_hash": _strict_hash(event_position_hash, "native"),
        "marker_hash": _strict_hash(marker_hash, "native"),
        "in_flight_count": 0,
        "in_flight_status": "atomic_commit_boundary",
        "coverage_hash": contract.expected_coverage_hash,
        "coverage_status": "validated",
        "read_scope_status": "read_only",
        "reset_or_decrease_count": 0,
        "read_call_count": 1,
    }
    if tuple(record) != _RECORD_KEYS:
        raise AssertionError("native exact marker record order drifted")
    return record


def _strict_sequence(value: object) -> tuple[object, int]:
    if isinstance(value, bool):
        raise NativeExactMutationMarkerError(
            "CouchDB exact marker sequence is malformed"
        )
    if isinstance(value, int):
        return value, _strict_counter(value, source="CouchDB")
    if isinstance(value, str) and len(value) <= 4096:
        match = _OPAQUE_SEQUENCE_PATTERN.fullmatch(value)
        if match is not None:
            return value, _strict_counter(
                int(match.group(1)), source="CouchDB"
            )
    raise NativeExactMutationMarkerError(
        "CouchDB exact marker sequence is malformed"
    )


def _strict_counter(value: object, *, source: str = "native") -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_COUNTER
    ):
        raise NativeExactMutationMarkerError(
            f"{source} exact marker counter is malformed"
        )
    return value


def _strict_hash(value: object, source: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise NativeExactMutationMarkerError(
            f"{source} exact marker hash is malformed"
        )
    return value


def _strict_mapping(
    value: object,
    keys: frozenset[str],
    source: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise NativeExactMutationMarkerError(
            f"{source} exact marker metadata is malformed"
        )
    return value


def _is_bounded_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 512
        and all(character >= " " and character != "\x7f" for character in value)
    )


def _require_plane(
    contract: NativeExactMutationMarkerContract,
    plane: str,
) -> None:
    if not isinstance(contract, NativeExactMutationMarkerContract):
        raise TypeError("native exact marker contract is required")
    if contract.plane != plane:
        raise ValueError(f"native exact marker provider requires {plane} plane")


def _invalid_contract() -> None:
    raise ValueError("native exact marker contract is malformed")


__all__ = [
    "CouchDBExactMutationMarkerProvider",
    "CouchDBMetadataAdapter",
    "NATSJetStreamExactMutationMarkerProvider",
    "NatsJetStreamExactMutationMarkerProvider",
    "NatsJetStreamMetadataAdapter",
    "NativeExactMutationMarkerContract",
    "NativeExactMutationMarkerError",
    "SQLiteExactMutationMarkerProvider",
    "SQLiteFileInspector",
    "SQLitePragmaConnection",
    "SQLiteReadOnlyConnector",
]
