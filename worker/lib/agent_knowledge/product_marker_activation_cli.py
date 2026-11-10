"""Operator-only PostgreSQL exact-marker activation CLI."""

from __future__ import annotations

import argparse
import hmac
import hashlib
import ipaddress
import json
import os
import re
import stat
import sys
import urllib.parse
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .postgres_exact_mutation_marker import (
    PostgresExactMutationMarkerContract,
    build_source_owned_postgres_exact_marker_contract,
)


_RESULT_SCHEMA = "product_marker_activation_result.v1"
_ZERO_HASH = "sha256:" + "0" * 64
_CONFIG_SCHEMA = "product_marker_activation_config.v1"
_CONFIG_NAME = "product-marker-activation.json"
_POSTGRES_PHASE = "postgres_schema_activation"
_POSTGRES_SCOPE = "postgres_exact_marker_activation"
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RAW_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COLLECTION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_APPROVAL_BYTES = 4 * 1024
_MAX_CREDENTIAL_BYTES = 32 * 1024
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
_APPROVAL_SCHEMA = "product_marker_activation_approval.v1"
_POSTGRES_SECRET_SCHEMA = "product_marker_activation_postgres_secret.v1"
_POSTGRES_SECRET_CONTRACT_SCHEMA = (
    "product_marker_activation_postgres_secret_contract.v1"
)


class ProductMarkerActivationCliError(RuntimeError):
    """Fixed-code, public-safe CLI failure."""

    def __init__(
        self,
        code: str,
        *,
        operation_count: int = 0,
        mutation_count: int = 0,
        mutation_performed: bool | str = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.operation_count = operation_count
        self.mutation_count = mutation_count
        self.mutation_performed = mutation_performed


@dataclass(frozen=True)
class ActivationDependencies:
    """Lazy connection factories; validation paths never call them."""

    postgres_connection_factory: Callable[[str], object]


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        _ = message
        raise ProductMarkerActivationCliError("invalid_arguments")


def _default_postgres_connection_factory(dsn: str) -> object:
    import psycopg

    return psycopg.connect(dsn, autocommit=True)


def _default_dependencies() -> ActivationDependencies:
    return ActivationDependencies(
        postgres_connection_factory=_default_postgres_connection_factory,
    )


def _result_hash(result: dict[str, object]) -> str:
    encoded = json.dumps(
        result,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _emit_result(
    *,
    phase: str,
    status: str,
    config_hash: str = _ZERO_HASH,
    operation_count: int = 0,
    mutation_count: int = 0,
    mutation_performed: bool | str = False,
) -> None:
    result: dict[str, object] = {
        "config_hash": config_hash,
        "live_proof": False,
        "mutation_count": mutation_count,
        "mutation_performed": mutation_performed,
        "operation_count": operation_count,
        "phase": phase,
        "schema_version": _RESULT_SCHEMA,
        "status": status,
    }
    result["result_hash"] = _result_hash(result)
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True))


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _canonical_json(raw: bytes, *, error_code: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
        if not isinstance(value, dict):
            raise ValueError
        canonical = (
            json.dumps(
                value,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise ProductMarkerActivationCliError(error_code) from None
    if canonical != raw:
        raise ProductMarkerActivationCliError(error_code)
    return value


def _fd_fingerprint(
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


def _read_bounded_fd(fd: int, *, maximum: int, error_code: str) -> bytes:
    try:
        metadata = os.fstat(fd)
    except OSError:
        raise ProductMarkerActivationCliError(error_code) from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size < 1
        or metadata.st_size > maximum
    ):
        raise ProductMarkerActivationCliError(error_code)
    before = _fd_fingerprint(metadata)
    chunks: list[bytes] = []
    remaining = metadata.st_size + 1
    while remaining > 0:
        try:
            chunk = os.read(fd, min(remaining, 16 * 1024))
        except OSError:
            raise ProductMarkerActivationCliError(error_code) from None
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    try:
        after = _fd_fingerprint(os.fstat(fd))
    except OSError:
        raise ProductMarkerActivationCliError(error_code) from None
    if len(raw) != metadata.st_size or after != before:
        raise ProductMarkerActivationCliError(error_code)
    return raw


def _open_absolute_root_directory(root: str) -> int:
    if (
        not isinstance(root, str)
        or not 1 < len(root) <= 4096
        or not root.startswith("/")
        or "\x00" in root
    ):
        raise ProductMarkerActivationCliError("config_invalid")
    components = root.split("/")[1:]
    if not components or any(component in {"", ".", ".."} for component in components):
        raise ProductMarkerActivationCliError("config_invalid")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise ProductMarkerActivationCliError("config_invalid")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | directory
        | nofollow
    )
    current_fd = -1
    try:
        current_fd = os.open("/", flags)
        for component in components:
            next_fd = os.open(component, flags, dir_fd=current_fd)
            if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                os.close(next_fd)
                raise ProductMarkerActivationCliError("config_invalid")
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except ProductMarkerActivationCliError:
        if current_fd >= 0:
            os.close(current_fd)
        raise
    except OSError:
        if current_fd >= 0:
            os.close(current_fd)
        raise ProductMarkerActivationCliError("config_invalid") from None


def _read_rooted_config(root: str, name: str) -> tuple[dict[str, Any], str]:
    if name != _CONFIG_NAME:
        raise ProductMarkerActivationCliError("config_invalid")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ProductMarkerActivationCliError("config_invalid")
    root_fd = -1
    config_fd = -1
    try:
        root_fd = _open_absolute_root_directory(root)
        config_fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow,
            dir_fd=root_fd,
        )
        raw = _read_bounded_fd(
            config_fd,
            maximum=_MAX_CONFIG_BYTES,
            error_code="config_invalid",
        )
    except ProductMarkerActivationCliError:
        raise
    except (OSError, ValueError):
        raise ProductMarkerActivationCliError("config_invalid") from None
    finally:
        if config_fd >= 0:
            os.close(config_fd)
        if root_fd >= 0:
            os.close(root_fd)
    config = _canonical_json(raw, error_code="config_invalid")
    _validate_public_config(config)
    return config, "sha256:" + hashlib.sha256(raw).hexdigest()


def _validate_hash(value: object) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ProductMarkerActivationCliError("config_invalid")


def _validate_raw_hash(value: object) -> None:
    if not isinstance(value, str) or not _RAW_SHA256.fullmatch(value):
        raise ProductMarkerActivationCliError("config_invalid")


def _validate_public_config(config: Mapping[str, object]) -> None:
    if set(config) != {
        "approval_token_hash",
        "contract",
        "operator_scope",
        "phase",
        "schema_version",
    }:
        raise ProductMarkerActivationCliError("config_invalid")
    if config.get("schema_version") != _CONFIG_SCHEMA:
        raise ProductMarkerActivationCliError("config_invalid")
    _validate_hash(config.get("approval_token_hash"))
    phase = config.get("phase")
    if phase != _POSTGRES_PHASE or config.get("operator_scope") != _POSTGRES_SCOPE:
        raise ProductMarkerActivationCliError("config_invalid")
    _validate_postgres_public_contract(config.get("contract"))


def _validate_postgres_public_contract(contract: object) -> None:
    if not isinstance(contract, Mapping) or set(contract) != {
        "activation_sql_hash",
        "advisory_lock_key",
        "expected_coverage_hash",
        "privileged_credential_inventory_anchor_hash",
        "schema_generation",
        "secret_contract_hash",
    }:
        raise ProductMarkerActivationCliError("config_invalid")
    for key in (
        "activation_sql_hash",
        "expected_coverage_hash",
        "privileged_credential_inventory_anchor_hash",
        "schema_generation",
        "secret_contract_hash",
    ):
        _validate_hash(contract.get(key))
    advisory_lock_key = contract.get("advisory_lock_key")
    if (
        isinstance(advisory_lock_key, bool)
        or not isinstance(advisory_lock_key, int)
        or not -(2**63) <= advisory_lock_key < 2**63
    ):
        raise ProductMarkerActivationCliError("config_invalid")






def _read_secret_json(fd: int, *, maximum: int, error_code: str) -> dict[str, Any]:
    if isinstance(fd, bool) or not isinstance(fd, int) or fd < 3:
        raise ProductMarkerActivationCliError(error_code)
    try:
        if os.lseek(fd, 0, os.SEEK_CUR) != 0:
            raise ProductMarkerActivationCliError(error_code)
        raw = _read_bounded_fd(fd, maximum=maximum, error_code=error_code)
    except ProductMarkerActivationCliError:
        raise
    except (OSError, ValueError):
        raise ProductMarkerActivationCliError(error_code) from None
    return _canonical_json(raw, error_code=error_code)


def _close_owned_fds(*fds: int) -> None:
    for fd in dict.fromkeys(fds):
        try:
            os.close(fd)
        except OSError:
            pass


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_approval(
    approval: Mapping[str, object],
    *,
    config: Mapping[str, object],
    config_hash: str,
) -> None:
    if set(approval) != {
        "config_hash",
        "operator_scope",
        "phase",
        "schema_version",
        "token",
    }:
        raise ProductMarkerActivationCliError("approval_invalid")
    token = approval.get("token")
    if (
        approval.get("schema_version") != _APPROVAL_SCHEMA
        or approval.get("phase") != config.get("phase")
        or approval.get("operator_scope") != config.get("operator_scope")
        or approval.get("config_hash") != config_hash
        or not isinstance(token, str)
        or not 32 <= len(token) <= 256
        or re.fullmatch(r"[A-Za-z0-9._~-]+", token) is None
    ):
        raise ProductMarkerActivationCliError("approval_invalid")
    expected_token_hash = config.get("approval_token_hash")
    if not isinstance(expected_token_hash, str) or not hmac.compare_digest(
        expected_token_hash,
        _sha256_text(token),
    ):
        raise ProductMarkerActivationCliError("approval_invalid")


def _validate_role_list(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) != len(set(value))
        or any(not isinstance(role, str) or not _IDENTIFIER.fullmatch(role) for role in value)
    ):
        raise ProductMarkerActivationCliError("credential_invalid")
    return tuple(value)


def _postgres_secret_contract_hash(secret: Mapping[str, object]) -> str:
    writers = _validate_role_list(secret.get("writer_roles"))
    privileged = _validate_role_list(secret.get("approved_privileged_roles"))
    marker_owner = secret.get("marker_owner_role")
    audit_reader = secret.get("audit_reader_role")
    anchor = secret.get("privileged_credential_inventory_anchor_hash")
    if (
        not isinstance(marker_owner, str)
        or not _IDENTIFIER.fullmatch(marker_owner)
        or not isinstance(audit_reader, str)
        or not _IDENTIFIER.fullmatch(audit_reader)
    ):
        raise ProductMarkerActivationCliError("credential_invalid")
    _validate_hash(anchor)
    projection = {
        "approved_privileged_role_hashes": sorted(
            _sha256_text(role) for role in privileged
        ),
        "audit_reader_role_hash": _sha256_text(audit_reader),
        "credential_inventory_anchor_hash": anchor,
        "marker_owner_role_hash": _sha256_text(marker_owner),
        "schema_version": _POSTGRES_SECRET_CONTRACT_SCHEMA,
        "writer_role_hashes": sorted(_sha256_text(role) for role in writers),
    }
    encoded = json.dumps(
        projection,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _build_postgres_contract(
    *,
    config: Mapping[str, object],
    secret: Mapping[str, object],
) -> tuple[PostgresExactMutationMarkerContract, str, str]:
    if set(secret) != {
        "approved_privileged_roles",
        "audit_reader_role",
        "dsn",
        "marker_owner_role",
        "privileged_credential_inventory_anchor_hash",
        "schema_version",
        "writer_roles",
    } or secret.get("schema_version") != _POSTGRES_SECRET_SCHEMA:
        raise ProductMarkerActivationCliError("credential_invalid")
    dsn = secret.get("dsn")
    if (
        not isinstance(dsn, str)
        or not 1 <= len(dsn) <= 4096
        or any(character < " " or character == "\x7f" for character in dsn)
    ):
        raise ProductMarkerActivationCliError("credential_invalid")
    contract_config = config.get("contract")
    if not isinstance(contract_config, Mapping):
        raise ProductMarkerActivationCliError("config_invalid")
    if not hmac.compare_digest(
        str(contract_config.get("secret_contract_hash") or ""),
        _postgres_secret_contract_hash(secret),
    ) or not hmac.compare_digest(
        str(contract_config.get("privileged_credential_inventory_anchor_hash") or ""),
        str(secret.get("privileged_credential_inventory_anchor_hash") or ""),
    ):
        raise ProductMarkerActivationCliError("credential_anchor_mismatch")
    try:
        contract = build_source_owned_postgres_exact_marker_contract(
            schema_generation=str(contract_config["schema_generation"]),
            writer_roles=_validate_role_list(secret.get("writer_roles")),
            marker_owner_role=str(secret["marker_owner_role"]),
            audit_reader_role=str(secret["audit_reader_role"]),
            advisory_lock_key=contract_config["advisory_lock_key"],  # type: ignore[arg-type]
            approved_privileged_roles=_validate_role_list(
                secret.get("approved_privileged_roles")
            ),
            privileged_credential_inventory_anchor_hash=str(
                secret["privileged_credential_inventory_anchor_hash"]
            ),
        )
    except (KeyError, TypeError, ValueError):
        raise ProductMarkerActivationCliError("postgres_contract_invalid") from None
    if not hmac.compare_digest(
        contract.expected_coverage_hash,
        str(contract_config.get("expected_coverage_hash") or ""),
    ):
        raise ProductMarkerActivationCliError("postgres_contract_invalid")
    activation_sql = contract.render_activation_sql()
    if not hmac.compare_digest(
        _sha256_text(activation_sql),
        str(contract_config.get("activation_sql_hash") or ""),
    ):
        raise ProductMarkerActivationCliError("postgres_contract_invalid")
    return contract, activation_sql, dsn


def _activate_postgres(
    *,
    config: Mapping[str, object],
    secret: Mapping[str, object],
    dependencies: ActivationDependencies,
) -> tuple[int, int, bool | str]:
    _contract, activation_sql, dsn = _build_postgres_contract(
        config=config,
        secret=secret,
    )
    try:
        connection = dependencies.postgres_connection_factory(dsn)
    except Exception:
        raise ProductMarkerActivationCliError("postgres_connection_failed") from None
    operation_count = 0
    try:
        execute = getattr(connection, "execute", None)
        if not callable(execute):
            raise ProductMarkerActivationCliError("postgres_connection_invalid")
        operation_count = 1
        execute(activation_sql)
    except ProductMarkerActivationCliError:
        raise
    except Exception:
        rollback = getattr(connection, "rollback", None)
        if not callable(rollback):
            raise ProductMarkerActivationCliError(
                "postgres_rollback_failed",
                operation_count=operation_count,
                mutation_count=1,
                mutation_performed="unknown",
            ) from None
        try:
            rollback()
        except Exception:
            raise ProductMarkerActivationCliError(
                "postgres_rollback_failed",
                operation_count=operation_count,
                mutation_count=1,
                mutation_performed="unknown",
            ) from None
        raise ProductMarkerActivationCliError(
            "postgres_activation_outcome_unknown",
            operation_count=operation_count,
            mutation_count=1,
            mutation_performed="unknown",
        ) from None
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                if operation_count:
                    raise ProductMarkerActivationCliError(
                        "postgres_close_failed",
                        operation_count=operation_count,
                        mutation_count=1,
                        mutation_performed=True,
                    ) from None
    return operation_count, 1, True


















def _build_parser() -> _SafeArgumentParser:
    parser = _SafeArgumentParser(prog="product-marker-activation")
    subparsers = parser.add_subparsers(dest="command")
    precheck = subparsers.add_parser("precheck")
    precheck.add_argument("--config-root", required=True)
    precheck.add_argument("--config-name", required=True)
    postgres = subparsers.add_parser("activate-postgres")
    postgres.add_argument("--config-root", required=True)
    postgres.add_argument("--config-name", required=True)
    postgres.add_argument("--approval-fd", required=True, type=int)
    postgres.add_argument("--credential-fd", required=True, type=int)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    dependencies: ActivationDependencies | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    selected_dependencies = dependencies or _default_dependencies()
    if not isinstance(selected_dependencies, ActivationDependencies):
        raise TypeError("ActivationDependencies required")
    if not args:
        _emit_result(phase="none", status="NOOP")
        return 0
    phase = "none"
    config_hash = _ZERO_HASH
    try:
        parsed = _build_parser().parse_args(args)
        if parsed.command == "precheck":
            phase = "precheck"
            _config, config_hash = _read_rooted_config(
                parsed.config_root,
                parsed.config_name,
            )
            _emit_result(
                phase=phase,
                status="PRECHECK_ONLY",
                config_hash=config_hash,
            )
            return 0
        if parsed.command == "activate-postgres":
            phase = _POSTGRES_PHASE
            try:
                if parsed.approval_fd == parsed.credential_fd:
                    raise ProductMarkerActivationCliError("invalid_arguments")
                config, config_hash = _read_rooted_config(
                    parsed.config_root,
                    parsed.config_name,
                )
                approval = _read_secret_json(
                    parsed.approval_fd,
                    maximum=_MAX_APPROVAL_BYTES,
                    error_code="approval_invalid",
                )
                _validate_approval(
                    approval,
                    config=config,
                    config_hash=config_hash,
                )
                secret = _read_secret_json(
                    parsed.credential_fd,
                    maximum=_MAX_CREDENTIAL_BYTES,
                    error_code="credential_invalid",
                )
                operation_count, mutation_count, mutation_performed = (
                    _activate_postgres(
                        config=config,
                        secret=secret,
                        dependencies=selected_dependencies,
                    )
                )
            finally:
                _close_owned_fds(parsed.approval_fd, parsed.credential_fd)
            _emit_result(
                phase=phase,
                status="PASS",
                config_hash=config_hash,
                operation_count=operation_count,
                mutation_count=mutation_count,
                mutation_performed=mutation_performed,
            )
            return 0
        raise ProductMarkerActivationCliError("invalid_arguments")
    except ProductMarkerActivationCliError as exc:
        _emit_result(
            phase=phase,
            status="FAIL",
            config_hash=config_hash,
            operation_count=exc.operation_count,
            mutation_count=exc.mutation_count,
            mutation_performed=exc.mutation_performed,
        )
        print(
            f"product_marker_activation_error:{exc.code}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
