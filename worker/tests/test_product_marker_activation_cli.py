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
    calls = {"postgres": 0}

    def postgres_factory(_dsn: str):
        calls["postgres"] += 1
        raise AssertionError("precheck must not open PostgreSQL")


    dependencies = activation_cli.ActivationDependencies(
        postgres_connection_factory=postgres_factory,
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
    assert calls == {"postgres": 0}


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
        ("phase", "unsupported_phase"),
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
