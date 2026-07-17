from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import re

import pytest

from agent_knowledge.native_exact_mutation_markers import (
    CouchDBExactMutationMarkerProvider,
    NatsJetStreamExactMutationMarkerProvider,
    NativeExactMutationMarkerContract,
    NativeExactMutationMarkerError,
    SQLiteExactMutationMarkerProvider,
)


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COUCH_CONTINUITY_ANCHOR = "sha256:" + "6" * 64
_NATS_PUBLISHER_AUTHORIZATION_ANCHOR = "sha256:" + "7" * 64
_RECORD_KEYS = {
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
}


def _sha(character: str) -> str:
    return "sha256:" + character * 64


def _contract(plane: str) -> NativeExactMutationMarkerContract:
    return NativeExactMutationMarkerContract(
        plane=plane,
        storage_identity=f"private-{plane}-storage",
        schema_generation=_sha("1"),
        config_generation=_sha("2"),
        reader_contract=f"{plane}-metadata-reader.v1",
        writer_registry=("writer-b", "writer-a"),
    )


def _assert_exact_record(record: dict[str, object], plane: str) -> None:
    assert set(record) == _RECORD_KEYS
    assert record["plane"] == plane
    for key in (
        "generation_hash",
        "event_position_hash",
        "marker_hash",
        "coverage_hash",
    ):
        assert _SHA256.fullmatch(str(record[key]))
    assert record["in_flight_count"] == 0
    assert record["in_flight_status"] == "atomic_commit_boundary"
    assert record["coverage_status"] == "validated"
    assert record["read_scope_status"] == "read_only"
    assert record["reset_or_decrease_count"] == 0
    assert record["read_call_count"] == 1


def test_source_contract_hashes_fixed_coverage_inputs_canonically() -> None:
    first = _contract("corpus")
    reordered = replace(first, writer_registry=("writer-a", "writer-b"))

    assert first.expected_coverage_hash == reordered.expected_coverage_hash
    assert first.generation_hash == reordered.generation_hash
    assert _SHA256.fullmatch(first.expected_coverage_hash)
    assert _SHA256.fullmatch(first.generation_hash)
    assert "private-corpus-storage" not in first.expected_coverage_hash


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("plane", "authority_ledger"),
        ("storage_identity", ""),
        ("schema_generation", "schema-1"),
        ("config_generation", "config-1"),
        ("reader_contract", "reader\ncontract"),
        ("writer_registry", ()),
        ("writer_registry", ("writer-a", "writer-a")),
    ),
)
def test_source_contract_rejects_ambiguous_or_malformed_coverage(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match="native exact marker contract"):
        replace(_contract("corpus"), **{field: value})


class _CouchAdapter:
    def __init__(self, info: dict[str, object]) -> None:
        self.info = info
        self.calls = 0

    def read_database_info(self) -> dict[str, object]:
        self.calls += 1
        return deepcopy(self.info)


def _couch_info(**overrides: object) -> dict[str, object]:
    info = {
        "continuity_anchor_hash": _COUCH_CONTINUITY_ANCHOR,
        "update_seq": "41-g1AAA-private-opaque-sequence",
        "purge_seq": 3,
        "doc_count": 80,
        "doc_del_count": 5,
    }
    info.update(overrides)
    return info


def test_couchdb_provider_hashes_opaque_sequences_and_returns_only_exact_metadata() -> None:
    adapter = _CouchAdapter(_couch_info())
    provider = CouchDBExactMutationMarkerProvider(
        _contract("corpus"),
        adapter,
        expected_continuity_anchor_hash=_COUCH_CONTINUITY_ANCHOR,
    )

    marker = provider.read_marker()

    _assert_exact_record(marker, "corpus")
    assert adapter.calls == 1
    assert "g1AAA" not in repr(marker)
    assert "doc_count" not in marker


def test_provider_pre_post_api_uses_the_same_instance_and_compares_monotonically() -> None:
    adapter = _CouchAdapter(_couch_info())
    provider = CouchDBExactMutationMarkerProvider(
        _contract("corpus"),
        adapter,
        expected_continuity_anchor_hash=_COUCH_CONTINUITY_ANCHOR,
    )

    def action() -> str:
        adapter.info["update_seq"] = "42-next"
        adapter.info["doc_count"] = 81
        return "done"

    before, result, after = provider.compare_pre_post(action)

    assert result == "done"
    assert adapter.calls == 2
    assert before["marker_hash"] != after["marker_hash"]
    _assert_exact_record(before, "corpus")
    _assert_exact_record(after, "corpus")


@pytest.mark.parametrize(
    "mutation",
    (
        lambda info: info.update({"document": {"secret": True}}),
        lambda info: info.update({"update_seq": "opaque-without-position"}),
        lambda info: info.update({"doc_count": True}),
        lambda info: info.pop("purge_seq"),
    ),
)
def test_couchdb_provider_rejects_non_fixed_or_malformed_database_info(mutation) -> None:
    info = _couch_info()
    mutation(info)
    provider = CouchDBExactMutationMarkerProvider(
        _contract("corpus"),
        _CouchAdapter(info),
        expected_continuity_anchor_hash=_COUCH_CONTINUITY_ANCHOR,
    )

    with pytest.raises(NativeExactMutationMarkerError, match="CouchDB"):
        provider.read_marker()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("update_seq", "40-next"),
        ("purge_seq", 2),
        ("doc_count", 79),
        ("doc_del_count", 4),
    ),
)
def test_same_couchdb_provider_fails_closed_on_reset_or_decrease(
    field: str,
    value: object,
) -> None:
    adapter = _CouchAdapter(_couch_info())
    provider = CouchDBExactMutationMarkerProvider(
        _contract("corpus"),
        adapter,
        expected_continuity_anchor_hash=_COUCH_CONTINUITY_ANCHOR,
    )
    provider.read_marker()
    adapter.info[field] = value

    with pytest.raises(NativeExactMutationMarkerError, match="decreased or recreated"):
        provider.read_marker()


@pytest.mark.parametrize("anchor", (None, "sha256:" + "9" * 64))
def test_couchdb_provider_requires_matching_external_continuity_anchor(
    anchor: str | None,
) -> None:
    info = _couch_info()
    if anchor is None:
        info.pop("continuity_anchor_hash")
    else:
        info["continuity_anchor_hash"] = anchor
    provider = CouchDBExactMutationMarkerProvider(
        _contract("corpus"),
        _CouchAdapter(info),
        expected_continuity_anchor_hash=_COUCH_CONTINUITY_ANCHOR,
    )

    with pytest.raises(NativeExactMutationMarkerError, match="continuity"):
        provider.read_marker()


class _NatsAdapter:
    def __init__(self, metadata: dict[str, object]) -> None:
        self.metadata = metadata
        self.calls = 0

    def read_stream_and_durable_metadata(self) -> dict[str, object]:
        self.calls += 1
        return deepcopy(self.metadata)


def _consumer(**overrides: object) -> dict[str, object]:
    value = {
        "generation_hash": _sha("4"),
        "delivered_stream_seq": 38,
        "ack_floor_stream_seq": 35,
        "num_ack_pending": 3,
        "num_redelivered": 1,
        "num_waiting": 2,
        "num_pending": 3,
    }
    value.update(overrides)
    return value


def _nats_metadata(contract: NativeExactMutationMarkerContract) -> dict[str, object]:
    return {
        "stream": {
            "generation_hash": contract.generation_hash,
            "messages": 8,
            "bytes": 2048,
            "first_seq": 34,
            "last_seq": 41,
            "num_deleted": 0,
            "consumer_count": 1,
        },
        "durable_consumers": [_consumer()],
        "unknown_durable_count": 0,
        "publisher_authorization_anchor_hash": (
            _NATS_PUBLISHER_AUTHORIZATION_ANCHOR
        ),
    }


def test_nats_provider_reads_only_fixed_stream_and_durable_counters() -> None:
    contract = _contract("queue")
    adapter = _NatsAdapter(_nats_metadata(contract))
    provider = NatsJetStreamExactMutationMarkerProvider(
        contract,
        adapter,
        expected_durable_count=1,
        expected_publisher_authorization_anchor_hash=(
            _NATS_PUBLISHER_AUTHORIZATION_ANCHOR
        ),
    )

    marker = provider.read_marker()

    _assert_exact_record(marker, "queue")
    assert adapter.calls == 1
    assert not ({"name", "subject", "message", "consumer"} & set(marker))


def test_nats_provider_rejects_unknown_durable_coverage() -> None:
    contract = _contract("queue")
    metadata = _nats_metadata(contract)
    metadata["unknown_durable_count"] = 1

    with pytest.raises(NativeExactMutationMarkerError, match="durable coverage"):
        NatsJetStreamExactMutationMarkerProvider(
            contract,
            _NatsAdapter(metadata),
            expected_durable_count=1,
            expected_publisher_authorization_anchor_hash=(
                _NATS_PUBLISHER_AUTHORIZATION_ANCHOR
            ),
        ).read_marker()


@pytest.mark.parametrize("anchor", (None, "sha256:" + "9" * 64))
def test_nats_provider_requires_matching_publisher_authorization_anchor(
    anchor: str | None,
) -> None:
    contract = _contract("queue")
    metadata = _nats_metadata(contract)
    if anchor is None:
        metadata.pop("publisher_authorization_anchor_hash")
    else:
        metadata["publisher_authorization_anchor_hash"] = anchor

    with pytest.raises(NativeExactMutationMarkerError, match="publisher coverage"):
        NatsJetStreamExactMutationMarkerProvider(
            contract,
            _NatsAdapter(metadata),
            expected_durable_count=1,
            expected_publisher_authorization_anchor_hash=(
                _NATS_PUBLISHER_AUTHORIZATION_ANCHOR
            ),
        ).read_marker()


@pytest.mark.parametrize(
    ("section", "field", "value"),
    (
        ("stream", "num_deleted", 1),
        ("consumer", "num_ack_pending", 2),
        ("consumer", "num_pending", 2),
    ),
)
def test_nats_provider_rejects_stream_or_consumer_counter_parity_drift(
    section: str,
    field: str,
    value: int,
) -> None:
    contract = _contract("queue")
    metadata = _nats_metadata(contract)
    target = (
        metadata["stream"]
        if section == "stream"
        else metadata["durable_consumers"][0]
    )
    target[field] = value  # type: ignore[index]

    with pytest.raises(NativeExactMutationMarkerError, match="metadata"):
        NatsJetStreamExactMutationMarkerProvider(
            contract,
            _NatsAdapter(metadata),
            expected_durable_count=1,
            expected_publisher_authorization_anchor_hash=(
                _NATS_PUBLISHER_AUTHORIZATION_ANCHOR
            ),
        ).read_marker()


def test_nats_provider_rejects_raw_names_subjects_messages_and_unknown_fields() -> None:
    contract = _contract("queue")
    metadata = _nats_metadata(contract)
    metadata["stream"]["subject"] = "private.events"  # type: ignore[index]

    with pytest.raises(NativeExactMutationMarkerError, match="NATS"):
        NatsJetStreamExactMutationMarkerProvider(
            contract,
            _NatsAdapter(metadata),
            expected_durable_count=1,
            expected_publisher_authorization_anchor_hash=(
                _NATS_PUBLISHER_AUTHORIZATION_ANCHOR
            ),
        ).read_marker()


def test_same_nats_provider_rejects_generation_recreation_or_position_decrease() -> None:
    contract = _contract("queue")
    adapter = _NatsAdapter(_nats_metadata(contract))
    provider = NatsJetStreamExactMutationMarkerProvider(
        contract,
        adapter,
        expected_durable_count=1,
        expected_publisher_authorization_anchor_hash=(
            _NATS_PUBLISHER_AUTHORIZATION_ANCHOR
        ),
    )
    provider.read_marker()
    adapter.metadata["stream"]["last_seq"] = 40  # type: ignore[index]
    adapter.metadata["stream"]["messages"] = 7  # type: ignore[index]
    adapter.metadata["durable_consumers"][0]["num_pending"] = 2  # type: ignore[index]

    with pytest.raises(NativeExactMutationMarkerError, match="decreased or recreated"):
        provider.read_marker()

    recreated = _nats_metadata(contract)
    recreated["stream"]["generation_hash"] = _sha("9")  # type: ignore[index]
    with pytest.raises(NativeExactMutationMarkerError, match="generation"):
        NatsJetStreamExactMutationMarkerProvider(
            contract,
            _NatsAdapter(recreated),
            expected_durable_count=1,
            expected_publisher_authorization_anchor_hash=(
                _NATS_PUBLISHER_AUTHORIZATION_ANCHOR
            ),
        ).read_marker()


class _SQLiteFileInspector:
    def __init__(self, metadata: dict[str, object]) -> None:
        self.metadata = metadata
        self.paths = []

    def inspect(self, path: object) -> dict[str, object]:
        self.paths.append(path)
        return deepcopy(self.metadata)


class _SQLiteConnection:
    def __init__(self, values: dict[str, int]) -> None:
        self.values = values
        self.pragmas = []
        self.connection_identity_hash = _sha("8")

    def read_pragma(self, name: str) -> int:
        self.pragmas.append(name)
        return self.values[name]


class _SQLiteConnector:
    def __init__(self, connection: _SQLiteConnection) -> None:
        self.connection = connection
        self.calls = []

    def open(self, path: object, *, mode: str, query_only: bool):
        self.calls.append((path, mode, query_only))
        return self.connection


def _file_metadata(**overrides: object) -> dict[str, object]:
    value = {
        "is_symlink": False,
        "is_regular": True,
        "directory_identity_hash": _sha("5"),
        "file_identity_hash": _sha("6"),
        "permission_hash": _sha("7"),
        "sidecar_identity_hash": _sha("9"),
    }
    value.update(overrides)
    return value


def _pragma_values(**overrides: int) -> dict[str, int]:
    value = {
        "query_only": 1,
        "data_version": 17,
        "page_count": 48,
        "schema_version": 5,
        "freelist_count": 2,
    }
    value.update(overrides)
    return value


def test_sqlite_provider_reuses_one_ro_query_only_connection_and_reads_only_pragmas() -> None:
    path = object()
    connection = _SQLiteConnection(_pragma_values())
    connector = _SQLiteConnector(connection)
    inspector = _SQLiteFileInspector(_file_metadata())
    provider = SQLiteExactMutationMarkerProvider(
        _contract("product_db"),
        path=path,
        connector=connector,
        file_inspector=inspector,
        expected_schema_version=5,
    )

    first = provider.read_marker()
    second = provider.read_marker()

    _assert_exact_record(first, "product_db")
    _assert_exact_record(second, "product_db")
    assert connector.calls == [(path, "ro", True)]
    assert connection.pragmas == [
        "query_only",
        "data_version",
        "page_count",
        "schema_version",
        "freelist_count",
    ] * 2
    assert path not in first.values()


@pytest.mark.parametrize(
    "metadata",
    (
        _file_metadata(is_symlink=True),
        _file_metadata(is_regular=False),
        {**_file_metadata(), "path": "/private/db.sqlite"},
    ),
)
def test_sqlite_provider_rejects_symlink_non_regular_or_raw_path_metadata(
    metadata: dict[str, object],
) -> None:
    provider = SQLiteExactMutationMarkerProvider(
        _contract("product_db"),
        path=object(),
        connector=_SQLiteConnector(_SQLiteConnection(_pragma_values())),
        file_inspector=_SQLiteFileInspector(metadata),
        expected_schema_version=5,
    )

    with pytest.raises(NativeExactMutationMarkerError, match="SQLite"):
        provider.read_marker()


@pytest.mark.parametrize(
    ("surface", "new_value", "message"),
    (
        ("permission_hash", _sha("9"), "permission"),
        ("directory_identity_hash", _sha("9"), "directory"),
        ("file_identity_hash", _sha("9"), "replaced"),
        ("sidecar_identity_hash", _sha("8"), "sidecar"),
        ("connection_identity_hash", _sha("9"), "connection replaced"),
    ),
)
def test_same_sqlite_provider_rejects_permission_file_or_connection_replacement(
    surface: str,
    new_value: str,
    message: str,
) -> None:
    connection = _SQLiteConnection(_pragma_values())
    inspector = _SQLiteFileInspector(_file_metadata())
    provider = SQLiteExactMutationMarkerProvider(
        _contract("product_db"),
        path=object(),
        connector=_SQLiteConnector(connection),
        file_inspector=inspector,
        expected_schema_version=5,
    )
    provider.read_marker()
    if surface == "connection_identity_hash":
        connection.connection_identity_hash = new_value
    else:
        inspector.metadata[surface] = new_value

    with pytest.raises(NativeExactMutationMarkerError, match=message):
        provider.read_marker()


@pytest.mark.parametrize(
    ("pragma", "value", "message"),
    (
        ("query_only", 0, "query_only"),
        ("schema_version", 6, "schema drift"),
        ("data_version", 16, "decreased or recreated"),
        ("page_count", 47, "decreased or recreated"),
        ("freelist_count", 1, "decreased or recreated"),
    ),
)
def test_same_sqlite_provider_fails_closed_on_scope_schema_or_counter_anomaly(
    pragma: str,
    value: int,
    message: str,
) -> None:
    connection = _SQLiteConnection(_pragma_values())
    provider = SQLiteExactMutationMarkerProvider(
        _contract("product_db"),
        path=object(),
        connector=_SQLiteConnector(connection),
        file_inspector=_SQLiteFileInspector(_file_metadata()),
        expected_schema_version=5,
    )
    provider.read_marker()
    connection.values[pragma] = value

    with pytest.raises(NativeExactMutationMarkerError, match=message):
        provider.read_marker()
