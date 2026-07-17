from __future__ import annotations

import importlib
import hashlib
import json
import os
from pathlib import Path
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from agent_knowledge.postgres_exact_mutation_marker import (
    build_source_owned_postgres_exact_marker_contract,
)
from agent_knowledge.qdrant_write_gateway_runtime import (
    QDRANT_MARKER_REQUIRED_PAYLOAD_INDEX_FIELDS,
    QDRANT_SOURCE_REGISTRY,
    QdrantMutationSource,
    RenderedQdrantWriter,
    build_qdrant_coverage_activation_anchor,
    build_qdrant_coverage_manifest_from_activation_anchor,
    build_qdrant_pending_cutover_anchor,
)


def _sha(value: bytes | str) -> str:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _raw_sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _postgres_config(*, approval_token: str = "a" * 48) -> dict[str, object]:
    return {
        "approval_token_hash": _sha(approval_token),
        "contract": {
            "activation_sql_hash": _sha("activation-sql"),
            "advisory_lock_key": 730461829,
            "expected_coverage_hash": _sha("coverage"),
            "privileged_credential_inventory_anchor_hash": _sha(
                "credential-inventory"
            ),
            "schema_generation": _sha("schema-generation"),
            "secret_contract_hash": _sha("secret-contract"),
        },
        "operator_scope": "postgres_exact_marker_activation",
        "phase": "postgres_schema_activation",
        "schema_version": "product_marker_activation_config.v1",
    }


def _write_config(root: Path, config: dict[str, object]) -> tuple[str, str]:
    name = "product-marker-activation.json"
    payload = _canonical_bytes(config)
    (root / name).write_bytes(payload)
    return name, _sha(payload)


def _secret_contract_hash(secret: dict[str, object]) -> str:
    projection = {
        "approved_privileged_role_hashes": sorted(
            _sha(role) for role in secret["approved_privileged_roles"]
        ),
        "audit_reader_role_hash": _sha(secret["audit_reader_role"]),
        "credential_inventory_anchor_hash": secret[
            "privileged_credential_inventory_anchor_hash"
        ],
        "marker_owner_role_hash": _sha(secret["marker_owner_role"]),
        "schema_version": "product_marker_activation_postgres_secret_contract.v1",
        "writer_role_hashes": sorted(_sha(role) for role in secret["writer_roles"]),
    }
    return _sha(
        json.dumps(
            projection,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _postgres_activation_inputs(
    root: Path,
    *,
    config_mutation: Callable[[dict[str, object]], None] | None = None,
) -> tuple[str, str, int, int, str]:
    token = "approved-postgres-marker-activation-token-000001"
    secret: dict[str, object] = {
        "approved_privileged_roles": ["marker_admin"],
        "audit_reader_role": "marker_reader",
        "dsn": "postgresql://operator:credential@private.invalid/authority",
        "marker_owner_role": "marker_owner",
        "privileged_credential_inventory_anchor_hash": _sha(
            "credential-inventory"
        ),
        "schema_version": "product_marker_activation_postgres_secret.v1",
        "writer_roles": ["ledger_writer"],
    }
    contract = build_source_owned_postgres_exact_marker_contract(
        schema_generation=_sha("schema-generation"),
        writer_roles=tuple(secret["writer_roles"]),
        marker_owner_role=secret["marker_owner_role"],
        audit_reader_role=secret["audit_reader_role"],
        advisory_lock_key=730461829,
        approved_privileged_roles=tuple(secret["approved_privileged_roles"]),
        privileged_credential_inventory_anchor_hash=secret[
            "privileged_credential_inventory_anchor_hash"
        ],
    )
    config = _postgres_config(approval_token=token)
    config["contract"] = {
        "activation_sql_hash": _sha(contract.render_activation_sql()),
        "advisory_lock_key": contract.advisory_lock_key,
        "expected_coverage_hash": contract.expected_coverage_hash,
        "privileged_credential_inventory_anchor_hash": (
            contract.privileged_credential_inventory_anchor_hash
        ),
        "schema_generation": contract.schema_generation,
        "secret_contract_hash": _secret_contract_hash(secret),
    }
    if config_mutation is not None:
        config_mutation(config)
    name, config_hash = _write_config(root, config)
    approval = {
        "config_hash": config_hash,
        "operator_scope": config["operator_scope"],
        "phase": config["phase"],
        "schema_version": "product_marker_activation_approval.v1",
        "token": token,
    }
    approval_path = root / "approval.json"
    credential_path = root / "postgres-secret.json"
    approval_path.write_bytes(_canonical_bytes(approval))
    credential_path.write_bytes(_canonical_bytes(secret))
    approval_fd = os.open(approval_path, os.O_RDONLY)
    credential_fd = os.open(credential_path, os.O_RDONLY)
    return name, config_hash, approval_fd, credential_fd, secret["dsn"]


def _qdrant_secret_contract_hash(secret: dict[str, object]) -> str:
    projection = {
        "credential_inventory_anchor_hash": secret[
            "credential_inventory_anchor_hash"
        ],
        "endpoint_identity_hash": _sha(secret["url"]),
        "marker_collection_hash": _sha(secret["marker_collection"]),
        "metadata_point_id_hash": _sha(secret["metadata_point_id"]),
        "operator_subject_ref_hash": secret["operator_subject_ref_hash"],
        "product_collection_hashes": sorted(
            _sha(name) for name in secret["product_collections"]
        ),
        "schema_version": "product_marker_activation_qdrant_secret_contract.v1",
    }
    return _sha(
        json.dumps(
            projection,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _qdrant_pending_inputs(
    root: Path,
    *,
    secret_mutation: Callable[[dict[str, object]], None] | None = None,
    config_mutation: Callable[[dict[str, object]], None] | None = None,
) -> tuple[str, str, int, int, dict[str, object]]:
    token = "approved-qdrant-pending-initialization-token-000001"
    secret: dict[str, object] = {
        "api_key": "private-qdrant-api-key-material",
        "credential_inventory_anchor_hash": _sha("qdrant-credential-inventory"),
        "marker_collection": "private_mutation_marker",
        "metadata_point_id": "00000000-0000-4000-8000-000000000001",
        "operator_subject_ref_hash": _raw_sha("operator-subject"),
        "product_collections": ["private_product_index"],
        "schema_version": "product_marker_activation_qdrant_secret.v1",
        "url": "https://private-qdrant.invalid:6333",
    }
    if secret_mutation is not None:
        secret_mutation(secret)
    anchor = build_qdrant_pending_cutover_anchor(
        generation=7,
        marker_collection=secret["marker_collection"],
        previous_generation_hash=_raw_sha("qdrant-generation-6"),
    )
    config = {
        "approval_token_hash": _sha(token),
        "contract": {
            "activation_hash": anchor.activation_hash,
            "coverage_hash": anchor.coverage_hash,
            "credential_inventory_anchor_hash": secret[
                "credential_inventory_anchor_hash"
            ],
            "generation": anchor.generation,
            "previous_generation_hash": anchor.previous_generation_hash,
            "secret_contract_hash": _qdrant_secret_contract_hash(secret),
        },
        "operator_scope": "qdrant_marker_generation_initialization",
        "phase": "qdrant_marker_generation_initialization",
        "schema_version": "product_marker_activation_config.v1",
    }
    if config_mutation is not None:
        config_mutation(config)
    name, config_hash = _write_config(root, config)
    approval = {
        "config_hash": config_hash,
        "operator_scope": config["operator_scope"],
        "phase": config["phase"],
        "schema_version": "product_marker_activation_approval.v1",
        "token": token,
    }
    approval_path = root / "approval.json"
    credential_path = root / "qdrant-secret.json"
    approval_path.write_bytes(_canonical_bytes(approval))
    credential_path.write_bytes(_canonical_bytes(secret))
    return (
        name,
        config_hash,
        os.open(approval_path, os.O_RDONLY),
        os.open(credential_path, os.O_RDONLY),
        secret,
    )


def _rendered_qdrant_inventory() -> tuple[RenderedQdrantWriter, ...]:
    return tuple(
        RenderedQdrantWriter(
            source=source,
            route=binding.route,
            writer_ref_hash=binding.writer_ref_hash,
            active_caller=binding.active_caller,
            workload_ref_hash=(
                _raw_sha(f"workload:{source.value}")
                if binding.active_caller
                else None
            ),
            image_ref_hash=(
                _raw_sha(f"image:{source.value}")
                if binding.active_caller
                else None
            ),
            network_policy_ref_hash=(
                _raw_sha(f"network:{source.value}")
                if binding.active_caller
                else None
            ),
            route_set_hash=(
                _raw_sha(f"routes:{source.value}")
                if binding.active_caller
                else None
            ),
        )
        for source, binding in QDRANT_SOURCE_REGISTRY.items()
    )


def _qdrant_finalize_inputs(
    root: Path,
    *,
    secret: dict[str, object],
    config_mutation: Callable[[dict[str, object]], None] | None = None,
) -> tuple[str, str, int, int]:
    token = "approved-qdrant-coverage-finalization-token-000001"
    inventory = _rendered_qdrant_inventory()
    anchor = build_qdrant_coverage_activation_anchor(
        generation=7,
        marker_collection=secret["marker_collection"],
        rendered_inventory=inventory,
        previous_generation_hash=_raw_sha("qdrant-generation-6"),
        auth_boundary_status="validated",
        network_policy_status="validated",
        direct_write_credentials_zero=True,
        read_endpoint_write_denied_status="validated",
    )
    coverage = build_qdrant_coverage_manifest_from_activation_anchor(anchor)
    config = {
        "approval_token_hash": _sha(token),
        "contract": {
            "activation_hash": anchor.activation_hash,
            "auth_boundary_status": anchor.auth_boundary_status,
            "coverage_hash": coverage.coverage_hash,
            "credential_inventory_anchor_hash": secret[
                "credential_inventory_anchor_hash"
            ],
            "direct_write_credentials_zero": anchor.direct_write_credentials_zero,
            "generation": anchor.generation,
            "network_policy_status": anchor.network_policy_status,
            "previous_generation_hash": anchor.previous_generation_hash,
            "read_endpoint_write_denied_status": (
                anchor.read_endpoint_write_denied_status
            ),
            "rendered_inventory": [
                {
                    "active_caller": item.active_caller,
                    "image_ref_hash": item.image_ref_hash,
                    "network_policy_ref_hash": item.network_policy_ref_hash,
                    "route": item.route.value,
                    "route_set_hash": item.route_set_hash,
                    "source": item.source.value,
                    "workload_ref_hash": item.workload_ref_hash,
                    "writer_ref_hash": item.writer_ref_hash,
                }
                for item in inventory
            ],
            "secret_contract_hash": _qdrant_secret_contract_hash(secret),
        },
        "operator_scope": "qdrant_marker_coverage_finalization",
        "phase": "qdrant_marker_coverage_finalization",
        "schema_version": "product_marker_activation_config.v1",
    }
    if config_mutation is not None:
        config_mutation(config)
    name, config_hash = _write_config(root, config)
    approval = {
        "config_hash": config_hash,
        "operator_scope": config["operator_scope"],
        "phase": config["phase"],
        "schema_version": "product_marker_activation_approval.v1",
        "token": token,
    }
    approval_path = root / "approval.json"
    credential_path = root / "qdrant-secret.json"
    approval_path.write_bytes(_canonical_bytes(approval))
    credential_path.write_bytes(_canonical_bytes(secret))
    return (
        name,
        config_hash,
        os.open(approval_path, os.O_RDONLY),
        os.open(credential_path, os.O_RDONLY),
    )


class _QdrantActivationClient:
    def __init__(self) -> None:
        self.exists = False
        self.indexes: dict[str, object] = {}
        self.points: dict[str, object] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.operation_id = 0
        self.close_count = 0

    def collection_exists(self, collection_name: str) -> bool:
        self.calls.append(("collection_exists", {"collection_name": collection_name}))
        return self.exists

    def create_collection(self, **kwargs: object) -> None:
        self.calls.append(("create_collection", dict(kwargs)))
        self.exists = True

    def get_collection(self, collection_name: str) -> dict[str, object]:
        self.calls.append(("get_collection", {"collection_name": collection_name}))
        return {
            "config": {
                "params": {
                    "replication_factor": 1,
                    "shard_number": 1,
                    "vectors": {"distance": "cosine", "size": 1},
                    "write_consistency_factor": 1,
                }
            },
            "payload_schema": {
                field: {"data_type": field_schema}
                for field, field_schema in self.indexes.items()
            },
        }

    def create_payload_index(self, **kwargs: object) -> None:
        self.calls.append(("create_payload_index", dict(kwargs)))
        self.indexes[str(kwargs["field_name"])] = kwargs["field_schema"]

    def retrieve(
        self,
        *,
        collection_name: str,
        ids: list[str],
        **kwargs: object,
    ) -> list[object]:
        self.calls.append(
            (
                "retrieve",
                {"collection_name": collection_name, "ids": ids, **kwargs},
            )
        )
        return [self.points[point_id] for point_id in ids if point_id in self.points]

    def count(self, **kwargs: object) -> object:
        self.calls.append(("count", dict(kwargs)))
        return SimpleNamespace(count=len(self.points))

    def upsert(self, **kwargs: object) -> object:
        self.calls.append(("upsert", dict(kwargs)))
        self.operation_id += 1
        for point in kwargs["points"]:
            self.points[str(point.id)] = point
        return SimpleNamespace(status="completed", operation_id=self.operation_id)

    def close(self) -> None:
        self.close_count += 1


def test_default_qdrant_factory_disables_proxy_redirect_and_grpc(monkeypatch):
    activation_cli = importlib.import_module(
        "agent_knowledge.product_marker_activation_cli"
    )
    observed: dict[str, object] = {}
    sentinel = object()

    def fake_client(**kwargs: object) -> object:
        observed.update(kwargs)
        return sentinel

    qdrant_client_module = importlib.import_module("qdrant_client")
    monkeypatch.setattr(qdrant_client_module, "QdrantClient", fake_client)

    assert (
        activation_cli._default_qdrant_client_factory(
            "https://private-qdrant.invalid:6333",
            "private-api-key-material",
        )
        is sentinel
    )
    assert observed == {
        "api_key": "private-api-key-material",
        "check_compatibility": False,
        "follow_redirects": False,
        "https": True,
        "port": 6333,
        "prefer_grpc": False,
        "timeout": 5,
        "trust_env": False,
        "url": "https://private-qdrant.invalid:6333",
    }


def test_default_mode_is_a_public_safe_noop(capsys):
    activation_cli = importlib.import_module(
        "agent_knowledge.product_marker_activation_cli"
    )

    assert activation_cli.main([]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "config_hash": "sha256:" + "0" * 64,
        "live_proof": False,
        "mutation_count": 0,
        "mutation_performed": False,
        "operation_count": 0,
        "phase": "none",
        "result_hash": (
            "sha256:bac76a56b1b691a1a48d9f4c1701f1ca2749f21049a29115198b3b79a15cfc1e"
        ),
        "schema_version": "product_marker_activation_result.v1",
        "status": "NOOP",
    }


def test_precheck_reads_canonical_rooted_config_without_calling_connectors(
    tmp_path: Path,
    capsys,
):
    activation_cli = importlib.import_module(
        "agent_knowledge.product_marker_activation_cli"
    )
    name, config_hash = _write_config(tmp_path, _postgres_config())
    calls = {"postgres": 0, "qdrant": 0}

    def postgres_factory(_dsn: str):
        calls["postgres"] += 1
        raise AssertionError("precheck must not open PostgreSQL")

    def qdrant_factory(_url: str, _api_key: str):
        calls["qdrant"] += 1
        raise AssertionError("precheck must not open Qdrant")

    dependencies = activation_cli.ActivationDependencies(
        postgres_connection_factory=postgres_factory,
        qdrant_client_factory=qdrant_factory,
    )

    assert (
        activation_cli.main(
            [
                "precheck",
                "--config-root",
                str(tmp_path),
                "--config-name",
                name,
            ],
            dependencies=dependencies,
        )
        == 0
    )

    result = json.loads(capsys.readouterr().out)
    assert result["phase"] == "precheck"
    assert result["status"] == "PRECHECK_ONLY"
    assert result["config_hash"] == config_hash
    assert result["operation_count"] == 0
    assert result["mutation_count"] == 0
    assert result["mutation_performed"] is False
    assert result["live_proof"] is False
    assert calls == {"postgres": 0, "qdrant": 0}


def test_postgres_activation_executes_the_source_contract_once(
    tmp_path: Path,
    capsys,
):
    activation_cli = importlib.import_module(
        "agent_knowledge.product_marker_activation_cli"
    )
    name, config_hash, approval_fd, credential_fd, expected_dsn = (
        _postgres_activation_inputs(tmp_path)
    )

    class Connection:
        def __init__(self) -> None:
            self.executed: list[str] = []
            self.rollback_count = 0
            self.close_count = 0

        def execute(self, sql: str) -> None:
            self.executed.append(sql)

        def rollback(self) -> None:
            self.rollback_count += 1

        def close(self) -> None:
            self.close_count += 1

    connection = Connection()
    factory_calls = []

    def postgres_factory(dsn: str) -> Connection:
        factory_calls.append(dsn)
        return connection

    dependencies = activation_cli.ActivationDependencies(
        postgres_connection_factory=postgres_factory,
        qdrant_client_factory=lambda _url, _api_key: (_ for _ in ()).throw(
            AssertionError("PostgreSQL phase must not open Qdrant")
        ),
    )

    assert (
        activation_cli.main(
            [
                "activate-postgres",
                "--config-root",
                str(tmp_path),
                "--config-name",
                name,
                "--approval-fd",
                str(approval_fd),
                "--credential-fd",
                str(credential_fd),
            ],
            dependencies=dependencies,
        )
        == 0
    )

    result = json.loads(capsys.readouterr().out)
    assert result["phase"] == "postgres_schema_activation"
    assert result["status"] == "PASS"
    assert result["config_hash"] == config_hash
    assert result["operation_count"] == 1
    assert result["mutation_count"] == 1
    assert result["mutation_performed"] is True
    assert result["live_proof"] is False
    assert factory_calls == [expected_dsn]
    assert len(connection.executed) == 1
    assert connection.executed[0].startswith("BEGIN;\n")
    assert connection.executed[0].splitlines().count("COMMIT;") == 1
    assert connection.executed[0].endswith("COMMIT;\n")
    assert connection.rollback_count == 0
    assert connection.close_count == 1


def test_postgres_failure_rolls_back_without_partial_ddl_or_secret_output(
    tmp_path: Path,
    capsys,
):
    activation_cli = importlib.import_module(
        "agent_knowledge.product_marker_activation_cli"
    )
    name, config_hash, approval_fd, credential_fd, raw_dsn = (
        _postgres_activation_inputs(tmp_path)
    )

    class FailingConnection:
        def __init__(self) -> None:
            self.persistent_ddl: list[str] = []
            self.execute_count = 0
            self.rollback_count = 0
            self.close_count = 0

        def execute(self, _sql: str) -> None:
            self.execute_count += 1
            self.persistent_ddl.append("partial-marker-table")
            raise RuntimeError(f"database failure for {raw_dsn}")

        def rollback(self) -> None:
            self.rollback_count += 1
            self.persistent_ddl.clear()

        def close(self) -> None:
            self.close_count += 1

    connection = FailingConnection()
    dependencies = activation_cli.ActivationDependencies(
        postgres_connection_factory=lambda _dsn: connection,
        qdrant_client_factory=lambda _url, _api_key: (_ for _ in ()).throw(
            AssertionError("PostgreSQL phase must not open Qdrant")
        ),
    )

    assert (
        activation_cli.main(
            [
                "activate-postgres",
                "--config-root",
                str(tmp_path),
                "--config-name",
                name,
                "--approval-fd",
                str(approval_fd),
                "--credential-fd",
                str(credential_fd),
            ],
            dependencies=dependencies,
        )
        == 2
    )

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["phase"] == "postgres_schema_activation"
    assert result["status"] == "FAIL"
    assert result["config_hash"] == config_hash
    assert result["operation_count"] == 1
    assert result["mutation_count"] == 1
    assert result["mutation_performed"] == "unknown"
    assert captured.err == (
        "product_marker_activation_error:postgres_activation_outcome_unknown\n"
    )
    assert raw_dsn not in captured.out
    assert raw_dsn not in captured.err
    assert str(tmp_path) not in captured.out
    assert str(tmp_path) not in captured.err
    assert connection.execute_count == 1
    assert connection.rollback_count == 1
    assert connection.close_count == 1
    assert connection.persistent_ddl == []


def test_postgres_commit_response_loss_is_unknown_and_never_auto_retried(
    tmp_path: Path,
    capsys,
):
    activation_cli = importlib.import_module(
        "agent_knowledge.product_marker_activation_cli"
    )
    name, config_hash, approval_fd, credential_fd, raw_dsn = (
        _postgres_activation_inputs(tmp_path)
    )
    durable_state = {"activated": False}

    class AmbiguousConnection:
        def __init__(self) -> None:
            self.execute_count = 0
            self.rollback_count = 0
            self.close_count = 0

        def execute(self, _sql: str) -> None:
            self.execute_count += 1
            durable_state["activated"] = True
            raise TimeoutError("protected-commit-response-loss")

        def rollback(self) -> None:
            self.rollback_count += 1

        def close(self) -> None:
            self.close_count += 1

    connection = AmbiguousConnection()
    factory_count = 0

    def postgres_factory(_dsn: str) -> AmbiguousConnection:
        nonlocal factory_count
        factory_count += 1
        return connection

    dependencies = activation_cli.ActivationDependencies(
        postgres_connection_factory=postgres_factory,
        qdrant_client_factory=lambda _url, _api_key: (_ for _ in ()).throw(
            AssertionError("PostgreSQL phase must not open Qdrant")
        ),
    )

    assert (
        activation_cli.main(
            [
                "activate-postgres",
                "--config-root",
                str(tmp_path),
                "--config-name",
                name,
                "--approval-fd",
                str(approval_fd),
                "--credential-fd",
                str(credential_fd),
            ],
            dependencies=dependencies,
        )
        == 2
    )

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["status"] == "FAIL"
    assert result["config_hash"] == config_hash
    assert result["operation_count"] == 1
    assert result["mutation_count"] == 1
    assert result["mutation_performed"] == "unknown"
    assert captured.err == (
        "product_marker_activation_error:postgres_activation_outcome_unknown\n"
    )
    assert durable_state["activated"] is True
    assert factory_count == 1
    assert connection.execute_count == 1
    assert connection.rollback_count == 1
    assert connection.close_count == 1
    for protected in (raw_dsn, "protected-commit-response-loss", str(tmp_path)):
        assert protected not in captured.out
        assert protected not in captured.err


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (
        ("schema_version", "product_marker_activation_config.v999"),
        ("phase", "qdrant_marker_coverage_finalization"),
        ("operator_scope", "self_approved_scope"),
    ),
)
def test_postgres_activation_rejects_self_consistent_wrong_config_identity_before_connect(
    tmp_path: Path,
    capsys,
    field: str,
    invalid_value: str,
):
    activation_cli = importlib.import_module(
        "agent_knowledge.product_marker_activation_cli"
    )
    name, _config_hash, approval_fd, credential_fd, _dsn = (
        _postgres_activation_inputs(
            tmp_path,
            config_mutation=lambda config: config.__setitem__(field, invalid_value),
        )
    )
    calls = []
    dependencies = activation_cli.ActivationDependencies(
        postgres_connection_factory=lambda dsn: calls.append(dsn),
        qdrant_client_factory=lambda _url, _api_key: (_ for _ in ()).throw(
            AssertionError("invalid PostgreSQL config must not open Qdrant")
        ),
    )

    assert (
        activation_cli.main(
            [
                "activate-postgres",
                "--config-root",
                str(tmp_path),
                "--config-name",
                name,
                "--approval-fd",
                str(approval_fd),
                "--credential-fd",
                str(credential_fd),
            ],
            dependencies=dependencies,
        )
        == 2
    )

    captured = capsys.readouterr()
    assert json.loads(captured.out)["mutation_performed"] is False
    assert captured.err == "product_marker_activation_error:config_invalid\n"
    assert calls == []


def test_bad_approval_closes_both_fds_and_calls_no_connector_without_leaking_values(
    tmp_path: Path,
    capsys,
):
    activation_cli = importlib.import_module(
        "agent_knowledge.product_marker_activation_cli"
    )
    name, config_hash, approval_fd, credential_fd, raw_dsn = (
        _postgres_activation_inputs(tmp_path)
    )
    bad_token = "rejected-postgres-marker-activation-token-999999"
    approval = {
        "config_hash": config_hash,
        "operator_scope": "postgres_exact_marker_activation",
        "phase": "postgres_schema_activation",
        "schema_version": "product_marker_activation_approval.v1",
        "token": bad_token,
    }
    (tmp_path / "approval.json").write_bytes(_canonical_bytes(approval))
    calls = []
    dependencies = activation_cli.ActivationDependencies(
        postgres_connection_factory=lambda dsn: calls.append(dsn),
        qdrant_client_factory=lambda _url, _api_key: (_ for _ in ()).throw(
            AssertionError("invalid approval must not open Qdrant")
        ),
    )

    assert (
        activation_cli.main(
            [
                "activate-postgres",
                "--config-root",
                str(tmp_path),
                "--config-name",
                name,
                "--approval-fd",
                str(approval_fd),
                "--credential-fd",
                str(credential_fd),
            ],
            dependencies=dependencies,
        )
        == 2
    )

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["status"] == "FAIL"
    assert result["mutation_performed"] is False
    assert captured.err == "product_marker_activation_error:approval_invalid\n"
    assert calls == []
    protected = (bad_token, raw_dsn, str(tmp_path))
    assert all(value not in captured.out for value in protected)
    assert all(value not in captured.err for value in protected)
    for fd in (approval_fd, credential_fd):
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize(
    ("boundary", "expected_code"),
    (
        ("config", "config_invalid"),
        ("approval", "approval_invalid"),
        ("credential", "credential_invalid"),
    ),
)
@pytest.mark.parametrize("parser_stage", ("loads", "dumps"))
def test_deep_json_is_a_fixed_public_safe_failure_without_connector(
    tmp_path: Path,
    capsys,
    monkeypatch,
    boundary: str,
    expected_code: str,
    parser_stage: str,
):
    activation_cli = importlib.import_module(
        "agent_knowledge.product_marker_activation_cli"
    )
    depth_bomb = ("[" * 1_500 + "0" + "]" * 1_500 + "\n").encode("utf-8")
    calls: list[tuple[object, ...]] = []
    dependencies = activation_cli.ActivationDependencies(
        postgres_connection_factory=lambda dsn: calls.append(("postgres", dsn)),
        qdrant_client_factory=lambda url, key: calls.append(("qdrant", url, key)),
    )

    if boundary == "config":
        name = "product-marker-activation.json"
        (tmp_path / name).write_bytes(depth_bomb)
        arguments = [
            "precheck",
            "--config-root",
            str(tmp_path),
            "--config-name",
            name,
        ]
    else:
        name, _config_hash, approval_fd, credential_fd, _dsn = (
            _postgres_activation_inputs(tmp_path)
        )
        target = (
            tmp_path / "approval.json"
            if boundary == "approval"
            else tmp_path / "postgres-secret.json"
        )
        target.write_bytes(depth_bomb)
        arguments = [
            "activate-postgres",
            "--config-root",
            str(tmp_path),
            "--config-name",
            name,
            "--approval-fd",
            str(approval_fd),
            "--credential-fd",
            str(credential_fd),
        ]

    real_loads = activation_cli.json.loads
    real_dumps = activation_cli.json.dumps
    parsed_sentinel: dict[str, object] = {"nested": []}

    def guarded_loads(value, *args, **kwargs):
        if isinstance(value, str) and value.startswith("[[["):
            if parser_stage == "loads":
                raise RecursionError("protected-depth")
            return parsed_sentinel
        return real_loads(value, *args, **kwargs)

    def guarded_dumps(value, *args, **kwargs):
        if value is parsed_sentinel:
            raise RecursionError("protected-depth")
        return real_dumps(value, *args, **kwargs)

    monkeypatch.setattr(activation_cli.json, "loads", guarded_loads)
    monkeypatch.setattr(activation_cli.json, "dumps", guarded_dumps)

    assert activation_cli.main(arguments, dependencies=dependencies) == 2

    captured = capsys.readouterr()
    result = real_loads(captured.out)
    assert result["status"] == "FAIL"
    assert result["mutation_count"] == 0
    assert result["mutation_performed"] is False
    assert captured.err == f"product_marker_activation_error:{expected_code}\n"
    assert calls == []
    assert str(tmp_path) not in captured.out
    assert str(tmp_path) not in captured.err


def test_config_root_rejects_relative_parent_and_intermediate_symlink_before_connect(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    activation_cli = importlib.import_module(
        "agent_knowledge.product_marker_activation_cli"
    )
    legitimate_root = tmp_path / "legitimate" / "activation"
    legitimate_root.mkdir(parents=True)
    name, _config_hash = _write_config(legitimate_root, _postgres_config())
    intermediate_link = tmp_path / "redirect"
    intermediate_link.symlink_to(tmp_path / "legitimate", target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    rejected_roots = (
        str(Path("legitimate") / "activation"),
        str(tmp_path / "legitimate" / ".." / "legitimate" / "activation"),
        str(intermediate_link / "activation"),
    )
    calls = []
    dependencies = activation_cli.ActivationDependencies(
        postgres_connection_factory=lambda dsn: calls.append(("postgres", dsn)),
        qdrant_client_factory=lambda url, key: calls.append(("qdrant", url, key)),
    )

    for rejected_root in rejected_roots:
        assert (
            activation_cli.main(
                [
                    "precheck",
                    "--config-root",
                    rejected_root,
                    "--config-name",
                    name,
                ],
                dependencies=dependencies,
            )
            == 2
        )
        captured = capsys.readouterr()
        assert json.loads(captured.out)["mutation_performed"] is False
        assert captured.err == "product_marker_activation_error:config_invalid\n"
        assert rejected_root not in captured.out
        assert rejected_root not in captured.err

    assert calls == []


@pytest.mark.parametrize("corruption", ("noncanonical", "symlink", "hardlink"))
def test_precheck_rejects_noncanonical_or_aliased_config_file(
    tmp_path: Path,
    capsys,
    corruption: str,
):
    activation_cli = importlib.import_module(
        "agent_knowledge.product_marker_activation_cli"
    )
    name, _config_hash = _write_config(tmp_path, _postgres_config())
    config_path = tmp_path / name
    if corruption == "noncanonical":
        config_path.write_text(
            json.dumps(_postgres_config(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    elif corruption == "symlink":
        target = tmp_path / "target.json"
        target.write_bytes(config_path.read_bytes())
        config_path.unlink()
        config_path.symlink_to(target)
    else:
        os.link(config_path, tmp_path / "second-link.json")
    calls: list[tuple[object, ...]] = []
    dependencies = activation_cli.ActivationDependencies(
        postgres_connection_factory=lambda dsn: calls.append(("postgres", dsn)),
        qdrant_client_factory=lambda url, key: calls.append(("qdrant", url, key)),
    )

    assert (
        activation_cli.main(
            [
                "precheck",
                "--config-root",
                str(tmp_path),
                "--config-name",
                name,
            ],
            dependencies=dependencies,
        )
        == 2
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out)["mutation_count"] == 0
    assert captured.err == "product_marker_activation_error:config_invalid\n"
    assert calls == []


def test_bounded_one_open_read_rejects_same_inode_same_size_rewrite(
    tmp_path: Path,
    monkeypatch,
):
    activation_cli = importlib.import_module(
        "agent_knowledge.product_marker_activation_cli"
    )
    path = tmp_path / "bounded-input.json"
    original_payload = b"a" * (32 * 1024)
    replacement_payload = b"b" * len(original_payload)
    path.write_bytes(original_payload)
    before = path.stat()
    descriptor = os.open(path, os.O_RDONLY)
    original_read = activation_cli.os.read
    raced = False

    def racing_read(fd: int, amount: int) -> bytes:
        nonlocal raced
        chunk = original_read(fd, amount)
        if not raced:
            raced = True
            writer = os.open(path, os.O_WRONLY)
            try:
                os.pwrite(writer, replacement_payload, 0)
            finally:
                os.close(writer)
            current = path.stat()
            os.utime(
                path,
                ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000_000),
            )
        return chunk

    monkeypatch.setattr(activation_cli.os, "read", racing_read)
    try:
        with pytest.raises(activation_cli.ProductMarkerActivationCliError) as error:
            activation_cli._read_bounded_fd(
                descriptor,
                maximum=64 * 1024,
                error_code="config_invalid",
            )
    finally:
        os.close(descriptor)

    after = path.stat()
    assert error.value.code == "config_invalid"
    assert before.st_ino == after.st_ino
    assert before.st_size == after.st_size
    assert path.read_bytes() == replacement_payload


def test_qdrant_pending_initialization_uses_only_insert_only_typed_apis(
    tmp_path: Path,
    capsys,
):
    activation_cli = importlib.import_module(
        "agent_knowledge.product_marker_activation_cli"
    )
    name, config_hash, approval_fd, credential_fd, secret = (
        _qdrant_pending_inputs(tmp_path)
    )
    client = _QdrantActivationClient()
    factory_calls = []

    def qdrant_factory(url: str, api_key: str) -> _QdrantActivationClient:
        factory_calls.append((url, api_key))
        return client

    dependencies = activation_cli.ActivationDependencies(
        postgres_connection_factory=lambda _dsn: (_ for _ in ()).throw(
            AssertionError("Qdrant phase must not open PostgreSQL")
        ),
        qdrant_client_factory=qdrant_factory,
    )

    assert (
        activation_cli.main(
            [
                "activate-qdrant-pending",
                "--config-root",
                str(tmp_path),
                "--config-name",
                name,
                "--approval-fd",
                str(approval_fd),
                "--credential-fd",
                str(credential_fd),
            ],
            dependencies=dependencies,
        )
        == 0
    )

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert captured.err == ""
    assert result["phase"] == "qdrant_marker_generation_initialization"
    assert result["status"] == "PASS"
    assert result["config_hash"] == config_hash
    assert result["operation_count"] == 2
    assert result["mutation_count"] == len(
        QDRANT_MARKER_REQUIRED_PAYLOAD_INDEX_FIELDS
    ) + 2
    assert result["mutation_performed"] is True
    assert result["live_proof"] is False
    assert factory_calls == [(secret["url"], secret["api_key"])]
    assert sum(name == "create_collection" for name, _ in client.calls) == 1
    assert sum(name == "create_payload_index" for name, _ in client.calls) == len(
        QDRANT_MARKER_REQUIRED_PAYLOAD_INDEX_FIELDS
    )
    upserts = [kwargs for name, kwargs in client.calls if name == "upsert"]
    assert len(upserts) == 1
    assert str(upserts[0]["update_mode"]).lower().endswith("insert_only")
    point = next(iter(client.points.values()))
    assert point.payload["coverage_status"] == "pending_cutover"
    assert point.payload["bypass_count"] == 1
    assert client.close_count == 1
    protected_values = (
        secret["url"],
        secret["api_key"],
        secret["marker_collection"],
        secret["metadata_point_id"],
        str(tmp_path),
    )
    assert all(value not in captured.out for value in protected_values)
    assert all(value not in captured.err for value in protected_values)


def test_qdrant_pending_initialization_is_idempotent_without_a_second_write(
    tmp_path: Path,
    capsys,
):
    activation_cli = importlib.import_module(
        "agent_knowledge.product_marker_activation_cli"
    )
    client = _QdrantActivationClient()
    dependencies = activation_cli.ActivationDependencies(
        postgres_connection_factory=lambda _dsn: (_ for _ in ()).throw(
            AssertionError("Qdrant phase must not open PostgreSQL")
        ),
        qdrant_client_factory=lambda _url, _api_key: client,
    )

    for expected_mutation_count in (
        len(QDRANT_MARKER_REQUIRED_PAYLOAD_INDEX_FIELDS) + 2,
        0,
    ):
        name, config_hash, approval_fd, credential_fd, _secret = (
            _qdrant_pending_inputs(tmp_path)
        )
        assert (
            activation_cli.main(
                [
                    "activate-qdrant-pending",
                    "--config-root",
                    str(tmp_path),
                    "--config-name",
                    name,
                    "--approval-fd",
                    str(approval_fd),
                    "--credential-fd",
                    str(credential_fd),
                ],
                dependencies=dependencies,
            )
            == 0
        )
        result = json.loads(capsys.readouterr().out)
        assert result["config_hash"] == config_hash
        assert result["operation_count"] == 2
        assert result["mutation_count"] == expected_mutation_count
        assert result["mutation_performed"] is (expected_mutation_count > 0)

    assert sum(name == "create_collection" for name, _ in client.calls) == 1
    assert sum(name == "create_payload_index" for name, _ in client.calls) == len(
        QDRANT_MARKER_REQUIRED_PAYLOAD_INDEX_FIELDS
    )
    assert sum(name == "upsert" for name, _ in client.calls) == 1
    assert client.close_count == 2


@pytest.mark.parametrize(
    "malicious_url",
    (
        "http://private-qdrant.invalid:6333",
        "https://operator@private-qdrant.invalid:6333",
        "https://private-qdrant.invalid",
        "https://private-qdrant.invalid:6334",
        "https://private-qdrant.invalid:6333/admin",
        "https://private-qdrant.invalid:6333?target=other",
        "https://private-qdrant.invalid:6333#other",
        "https://PRIVATE-qdrant.invalid:6333",
        "https://private%2dqdrant.invalid:6333",
        "https://private-qdrant.invalid\\@other.invalid:6333",
        "https://127.1:6333",
        "https://0x7f000001:6333",
    ),
)
def test_qdrant_operator_url_rejects_credential_confusion_before_connect(
    tmp_path: Path,
    capsys,
    malicious_url: str,
):
    activation_cli = importlib.import_module(
        "agent_knowledge.product_marker_activation_cli"
    )
    name, _config_hash, approval_fd, credential_fd, secret = (
        _qdrant_pending_inputs(
            tmp_path,
            secret_mutation=lambda value: value.__setitem__("url", malicious_url),
        )
    )
    calls: list[tuple[str, str]] = []
    dependencies = activation_cli.ActivationDependencies(
        postgres_connection_factory=lambda _dsn: (_ for _ in ()).throw(
            AssertionError("Qdrant phase must not open PostgreSQL")
        ),
        qdrant_client_factory=lambda url, api_key: calls.append((url, api_key)),
    )

    assert (
        activation_cli.main(
            [
                "activate-qdrant-pending",
                "--config-root",
                str(tmp_path),
                "--config-name",
                name,
                "--approval-fd",
                str(approval_fd),
                "--credential-fd",
                str(credential_fd),
            ],
            dependencies=dependencies,
        )
        == 2
    )

    captured = capsys.readouterr()
    assert json.loads(captured.out)["mutation_count"] == 0
    assert captured.err == "product_marker_activation_error:credential_invalid\n"
    assert calls == []
    assert malicious_url not in captured.out
    assert malicious_url not in captured.err
    assert secret["api_key"] not in captured.out
    assert secret["api_key"] not in captured.err


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (
        ("generation", True),
        ("generation", 0),
        ("activation_hash", "sha256:" + "a" * 64),
        ("coverage_hash", "not-a-hash"),
        ("previous_generation_hash", "A" * 64),
        ("credential_inventory_anchor_hash", "0" * 64),
        ("secret_contract_hash", "sha256:short"),
    ),
)
def test_qdrant_pending_precheck_rejects_malformed_contract_without_connect(
    tmp_path: Path,
    capsys,
    field: str,
    invalid_value: object,
):
    activation_cli = importlib.import_module(
        "agent_knowledge.product_marker_activation_cli"
    )

    def mutate(config: dict[str, object]) -> None:
        contract = config["contract"]
        assert isinstance(contract, dict)
        contract[field] = invalid_value

    name, _config_hash, approval_fd, credential_fd, _secret = (
        _qdrant_pending_inputs(tmp_path, config_mutation=mutate)
    )
    os.close(approval_fd)
    os.close(credential_fd)
    calls: list[tuple[object, ...]] = []
    dependencies = activation_cli.ActivationDependencies(
        postgres_connection_factory=lambda dsn: calls.append(("postgres", dsn)),
        qdrant_client_factory=lambda url, api_key: calls.append(
            ("qdrant", url, api_key)
        ),
    )

    assert (
        activation_cli.main(
            [
                "precheck",
                "--config-root",
                str(tmp_path),
                "--config-name",
                name,
            ],
            dependencies=dependencies,
        )
        == 2
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out)["mutation_count"] == 0
    assert captured.err == "product_marker_activation_error:config_invalid\n"
    assert calls == []


@pytest.mark.parametrize("field", ("activation_hash", "coverage_hash"))
def test_qdrant_pending_semantic_hash_mismatch_calls_no_connector(
    tmp_path: Path,
    capsys,
    field: str,
):
    activation_cli = importlib.import_module(
        "agent_knowledge.product_marker_activation_cli"
    )

    def mutate(config: dict[str, object]) -> None:
        contract = config["contract"]
        assert isinstance(contract, dict)
        contract[field] = _raw_sha(f"wrong-{field}")

    name, _config_hash, approval_fd, credential_fd, secret = (
        _qdrant_pending_inputs(tmp_path, config_mutation=mutate)
    )
    calls: list[tuple[str, str]] = []
    dependencies = activation_cli.ActivationDependencies(
        postgres_connection_factory=lambda _dsn: (_ for _ in ()).throw(
            AssertionError("Qdrant phase must not open PostgreSQL")
        ),
        qdrant_client_factory=lambda url, api_key: calls.append((url, api_key)),
    )

    assert (
        activation_cli.main(
            [
                "activate-qdrant-pending",
                "--config-root",
                str(tmp_path),
                "--config-name",
                name,
                "--approval-fd",
                str(approval_fd),
                "--credential-fd",
                str(credential_fd),
            ],
            dependencies=dependencies,
        )
        == 2
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out)["mutation_count"] == 0
    assert captured.err == "product_marker_activation_error:qdrant_anchor_invalid\n"
    assert calls == []
    assert secret["api_key"] not in captured.out
    assert secret["url"] not in captured.out


def test_qdrant_finalize_only_updates_exact_pending_metadata(
    tmp_path: Path,
    capsys,
):
    activation_cli = importlib.import_module(
        "agent_knowledge.product_marker_activation_cli"
    )
    pending_name, _pending_hash, pending_approval_fd, pending_credential_fd, secret = (
        _qdrant_pending_inputs(tmp_path)
    )
    client = _QdrantActivationClient()
    dependencies = activation_cli.ActivationDependencies(
        postgres_connection_factory=lambda _dsn: (_ for _ in ()).throw(
            AssertionError("Qdrant phase must not open PostgreSQL")
        ),
        qdrant_client_factory=lambda _url, _api_key: client,
    )
    assert (
        activation_cli.main(
            [
                "activate-qdrant-pending",
                "--config-root",
                str(tmp_path),
                "--config-name",
                pending_name,
                "--approval-fd",
                str(pending_approval_fd),
                "--credential-fd",
                str(pending_credential_fd),
            ],
            dependencies=dependencies,
        )
        == 0
    )
    capsys.readouterr()
    initial_collection_creates = sum(
        name == "create_collection" for name, _ in client.calls
    )
    initial_index_creates = sum(
        name == "create_payload_index" for name, _ in client.calls
    )

    name, config_hash, approval_fd, credential_fd = _qdrant_finalize_inputs(
        tmp_path,
        secret=secret,
    )
    assert (
        activation_cli.main(
            [
                "finalize-qdrant-coverage",
                "--config-root",
                str(tmp_path),
                "--config-name",
                name,
                "--approval-fd",
                str(approval_fd),
                "--credential-fd",
                str(credential_fd),
            ],
            dependencies=dependencies,
        )
        == 0
    )

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert captured.err == ""
    assert result["phase"] == "qdrant_marker_coverage_finalization"
    assert result["status"] == "PASS"
    assert result["config_hash"] == config_hash
    assert result["operation_count"] == 1
    assert result["mutation_count"] == 1
    assert result["mutation_performed"] is True
    assert result["live_proof"] is False
    assert sum(name == "create_collection" for name, _ in client.calls) == (
        initial_collection_creates
    )
    assert sum(name == "create_payload_index" for name, _ in client.calls) == (
        initial_index_creates
    )
    upserts = [kwargs for name, kwargs in client.calls if name == "upsert"]
    assert len(upserts) == 2
    assert str(upserts[-1]["update_mode"]).lower().endswith("update_only")
    assert upserts[-1]["update_filter"] is not None
    assert len(upserts[-1]["update_filter"].must) == 7
    point = next(iter(client.points.values()))
    assert point.payload["coverage_status"] == "complete"
    assert point.payload["bypass_count"] == 0
    assert client.close_count == 2


def test_qdrant_finalize_is_idempotent_without_collection_or_metadata_write(
    tmp_path: Path,
    capsys,
):
    activation_cli = importlib.import_module(
        "agent_knowledge.product_marker_activation_cli"
    )
    pending_name, _pending_hash, approval_fd, credential_fd, secret = (
        _qdrant_pending_inputs(tmp_path)
    )
    client = _QdrantActivationClient()
    dependencies = activation_cli.ActivationDependencies(
        postgres_connection_factory=lambda _dsn: (_ for _ in ()).throw(
            AssertionError("Qdrant phase must not open PostgreSQL")
        ),
        qdrant_client_factory=lambda _url, _api_key: client,
    )
    assert (
        activation_cli.main(
            [
                "activate-qdrant-pending",
                "--config-root",
                str(tmp_path),
                "--config-name",
                pending_name,
                "--approval-fd",
                str(approval_fd),
                "--credential-fd",
                str(credential_fd),
            ],
            dependencies=dependencies,
        )
        == 0
    )
    capsys.readouterr()

    for expected_mutation_count in (1, 0):
        name, config_hash, approval_fd, credential_fd = _qdrant_finalize_inputs(
            tmp_path,
            secret=secret,
        )
        assert (
            activation_cli.main(
                [
                    "finalize-qdrant-coverage",
                    "--config-root",
                    str(tmp_path),
                    "--config-name",
                    name,
                    "--approval-fd",
                    str(approval_fd),
                    "--credential-fd",
                    str(credential_fd),
                ],
                dependencies=dependencies,
            )
            == 0
        )
        result = json.loads(capsys.readouterr().out)
        assert result["config_hash"] == config_hash
        assert result["operation_count"] == 1
        assert result["mutation_count"] == expected_mutation_count
        assert result["mutation_performed"] is (expected_mutation_count > 0)

    assert sum(name == "create_collection" for name, _ in client.calls) == 1
    assert sum(name == "create_payload_index" for name, _ in client.calls) == len(
        QDRANT_MARKER_REQUIRED_PAYLOAD_INDEX_FIELDS
    )
    assert sum(name == "upsert" for name, _ in client.calls) == 2
    assert client.close_count == 3


@pytest.mark.parametrize(
    ("case", "expected_code"),
    (
        ("activation_hash", "qdrant_anchor_invalid"),
        ("coverage_hash", "qdrant_anchor_invalid"),
        ("inventory_route", "qdrant_anchor_invalid"),
        ("credential_anchor", "credential_anchor_mismatch"),
    ),
)
def test_qdrant_finalize_mismatch_calls_no_connector_or_collection_api(
    tmp_path: Path,
    capsys,
    case: str,
    expected_code: str,
):
    activation_cli = importlib.import_module(
        "agent_knowledge.product_marker_activation_cli"
    )
    _name, _hash, pending_approval_fd, pending_credential_fd, secret = (
        _qdrant_pending_inputs(tmp_path)
    )
    os.close(pending_approval_fd)
    os.close(pending_credential_fd)

    def mutate(config: dict[str, object]) -> None:
        contract = config["contract"]
        assert isinstance(contract, dict)
        if case in {"activation_hash", "coverage_hash"}:
            contract[case] = _raw_sha(f"wrong-{case}")
        elif case == "inventory_route":
            inventory = contract["rendered_inventory"]
            assert isinstance(inventory, list)
            first = inventory[0]
            assert isinstance(first, dict)
            first["route"] = "repair"
        else:
            contract["credential_inventory_anchor_hash"] = _sha(
                "wrong-credential-anchor"
            )

    name, _config_hash, approval_fd, credential_fd = _qdrant_finalize_inputs(
        tmp_path,
        secret=secret,
        config_mutation=mutate,
    )
    calls: list[tuple[str, str]] = []
    dependencies = activation_cli.ActivationDependencies(
        postgres_connection_factory=lambda _dsn: (_ for _ in ()).throw(
            AssertionError("Qdrant phase must not open PostgreSQL")
        ),
        qdrant_client_factory=lambda url, api_key: calls.append((url, api_key)),
    )

    assert (
        activation_cli.main(
            [
                "finalize-qdrant-coverage",
                "--config-root",
                str(tmp_path),
                "--config-name",
                name,
                "--approval-fd",
                str(approval_fd),
                "--credential-fd",
                str(credential_fd),
            ],
            dependencies=dependencies,
        )
        == 2
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out)["mutation_count"] == 0
    assert captured.err == f"product_marker_activation_error:{expected_code}\n"
    assert calls == []
    assert secret["api_key"] not in captured.out
    assert secret["url"] not in captured.out
