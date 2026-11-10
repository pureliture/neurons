from __future__ import annotations


import pytest

from agent_knowledge.permission_audit import IndependentProductMutationMarkerReader
from agent_knowledge.postgres_exact_mutation_marker import (
    build_source_owned_postgres_exact_marker_contract,
)


def _sha(character: str) -> str:
    return "sha256:" + character * 64


def _raw_sha(character: str) -> str:
    return character * 64


def _record(
    plane: str,
    *,
    call_count: int = 1,
    in_flight_count: int = 0,
) -> dict[str, object]:
    status = "clear" if plane == "authority_ledger" else "atomic_commit_boundary"
    return {
        "plane": plane,
        "generation_hash": _sha("1"),
        "event_position_hash": _sha("2"),
        "marker_hash": _sha("3"),
        "in_flight_count": in_flight_count,
        "in_flight_status": status,
        "coverage_hash": _sha("4"),
        "coverage_status": "validated",
        "read_scope_status": "read_only",
        "reset_or_decrease_count": 0,
        "read_call_count": call_count,
    }


class _NativeProvider:
    def __init__(self, plane: str, *, in_flight_count: int = 0) -> None:
        self.plane = plane
        self.calls = 0
        self.in_flight_count = in_flight_count

    def __call__(self) -> dict[str, object]:
        self.calls += 1
        return _record(self.plane, in_flight_count=self.in_flight_count)


class _Connection:
    dialect = "postgres"

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Fence:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.reads = 0
        self.releases = 0

    def read_marker(self) -> dict[str, object]:
        assert self.connection.closed is False
        self.reads += 1
        return _record("authority_ledger")

    def release(self) -> None:
        assert self.connection.closed is False
        self.releases += 1


class _PostgresReader:
    instances: list["_PostgresReader"] = []

    def __init__(self, contract: object) -> None:
        self.contract = contract
        self.acquired_connections: list[_Connection] = []
        self.fences: list[_Fence] = []
        self.instances.append(self)

    def acquire_audit_fence(self, connection: _Connection) -> _Fence:
        self.acquired_connections.append(connection)
        fence = _Fence(connection)
        self.fences.append(fence)
        return fence






def _postgres_contract():
    return build_source_owned_postgres_exact_marker_contract(
        schema_generation=_sha("a"),
        writer_roles=("neurons_writer",),
        marker_owner_role="neurons_marker_owner",
        audit_reader_role="neurons_marker_reader",
        advisory_lock_key=7_211_740_091,
        approved_privileged_roles=("postgres_bootstrap",),
        privileged_credential_inventory_anchor_hash=_sha("8"),
    )


def _build_reader(monkeypatch: pytest.MonkeyPatch, **overrides: object):
    import agent_knowledge.permission_audit_marker_runtime as runtime

    _PostgresReader.instances.clear()
    monkeypatch.setattr(runtime, "PostgresExactMutationMarkerReader", _PostgresReader)
    connection = _Connection()
    corpus = _NativeProvider("corpus")
    queue = _NativeProvider("queue")
    product_db = _NativeProvider("product_db")
    arguments: dict[str, object] = {
        "postgres_contract": _postgres_contract(),
        "postgres_connection_factory": lambda: connection,
        "couchdb_provider": corpus,
        "nats_provider": queue,
        "sqlite_provider": product_db,
    }
    arguments.update(overrides)
    return runtime.build_permission_audit_marker_reader(**arguments), {
        "connection": connection,
        "native": (corpus, queue, product_db),
    }


def test_builder_returns_one_source_owned_reader_and_reuses_provider_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, state = _build_reader(monkeypatch)
    actions: list[str] = []

    before, result, after = reader.run_audit_window(
        lambda: actions.append("stored") or "result"
    )

    assert isinstance(reader, IndependentProductMutationMarkerReader)
    assert result == "result"
    assert actions == ["stored"]
    assert [item["plane"] for item in before["markers"]] == [
        "authority_ledger",
        "corpus",
        "queue",
        "product_db",
    ]
    assert [item["plane"] for item in after["markers"]] == [
        "authority_ledger",
        "corpus",
        "queue",
        "product_db",
    ]
    assert all(
        set(item) == set(_record(str(item["plane"])))
        for item in before["markers"]
    )
    assert [provider.calls for provider in state["native"]] == [2, 2, 2]
    pg_reader = _PostgresReader.instances[0]
    assert pg_reader.acquired_connections == [state["connection"]]
    assert pg_reader.fences[0].reads == 2
    assert pg_reader.fences[0].releases == 1
    assert state["connection"].closed is True


@pytest.mark.parametrize("failure", ["missing", "alias", "malformed"])
def test_builder_rejects_missing_aliased_or_malformed_sources(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    corpus = _NativeProvider("corpus")
    overrides: dict[str, object]
    if failure == "missing":
        overrides = {"couchdb_provider": None}
    elif failure == "alias":
        overrides = {"couchdb_provider": corpus, "nats_provider": corpus}
    else:
        overrides = {"postgres_connection_factory": None}

    with pytest.raises((TypeError, ValueError)):
        _build_reader(monkeypatch, **overrides)


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("acquire", "acquisition failed"),
        ("replacement", "connection was replaced"),
        ("release", "fence release failed"),
        ("close", "connection close failed"),
    ],
)
def test_postgres_fence_and_connection_failures_close_before_store_action(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    message: str,
) -> None:
    import agent_knowledge.permission_audit_marker_runtime as runtime

    class Connection(_Connection):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            self.closed = True
            if failure == "close":
                raise OSError("private/runtime/path")

    class Fence(_Fence):
        def __init__(self, connection: Connection) -> None:
            super().__init__(connection)
            self._connection = object() if failure == "replacement" else connection

        def release(self) -> None:
            self.releases += 1
            if failure == "release":
                raise OSError("private/runtime/path")

    class PostgresReader:
        def __init__(self, _contract: object) -> None:
            pass

        def acquire_audit_fence(self, connection: Connection) -> Fence:
            if failure == "acquire":
                raise OSError("private/runtime/path")
            return Fence(connection)

    monkeypatch.setattr(runtime, "PostgresExactMutationMarkerReader", PostgresReader)
    connection = Connection()

    reader = runtime.build_permission_audit_marker_reader(
        postgres_contract=_postgres_contract(),
        postgres_connection_factory=lambda: connection,
        couchdb_provider=_NativeProvider("corpus"),
        nats_provider=_NativeProvider("queue"),
        sqlite_provider=_NativeProvider(
            "product_db",
            in_flight_count=int(failure in {"release", "close"}),
        ),
    )
    actions: list[str] = []

    with pytest.raises(Exception, match=message) as error:
        reader.run_audit_window(lambda: actions.append("stored"))

    assert actions == []
    assert connection.close_calls == 1
    assert "private/runtime/path" not in str(error.value)
