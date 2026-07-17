"""Operator-only PostgreSQL and Qdrant exact-marker activation CLI."""

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
from .qdrant_write_gateway import QdrantGatewayContractError, QdrantMutationRoute
from .qdrant_write_gateway_runtime import (
    QDRANT_EXACT_MARKER_METADATA_KEYS,
    QDRANT_MARKER_REQUIRED_PAYLOAD_INDEX_FIELDS,
    AuthenticatedQdrantSubject,
    ExactRouteAuthorizer,
    QdrantCollectionPolicy,
    QdrantMarkerMetadataPhase,
    QdrantMutationSource,
    RenderedQdrantWriter,
    activate_qdrant_marker_collection,
    build_qdrant_coverage_activation_anchor,
    build_qdrant_coverage_manifest_from_activation_anchor,
    build_qdrant_pending_cutover_anchor,
    reconcile_qdrant_marker_metadata,
)


_RESULT_SCHEMA = "product_marker_activation_result.v1"
_ZERO_HASH = "sha256:" + "0" * 64
_CONFIG_SCHEMA = "product_marker_activation_config.v1"
_CONFIG_NAME = "product-marker-activation.json"
_POSTGRES_PHASE = "postgres_schema_activation"
_POSTGRES_SCOPE = "postgres_exact_marker_activation"
_QDRANT_PENDING_PHASE = "qdrant_marker_generation_initialization"
_QDRANT_PENDING_SCOPE = "qdrant_marker_generation_initialization"
_QDRANT_FINALIZE_PHASE = "qdrant_marker_coverage_finalization"
_QDRANT_FINALIZE_SCOPE = "qdrant_marker_coverage_finalization"
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
_QDRANT_SECRET_SCHEMA = "product_marker_activation_qdrant_secret.v1"
_QDRANT_SECRET_CONTRACT_SCHEMA = (
    "product_marker_activation_qdrant_secret_contract.v1"
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
    qdrant_client_factory: Callable[[str, str], object]


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        _ = message
        raise ProductMarkerActivationCliError("invalid_arguments")


def _default_postgres_connection_factory(dsn: str) -> object:
    import psycopg

    return psycopg.connect(dsn, autocommit=True)


def _default_qdrant_client_factory(url: str, api_key: str) -> object:
    from qdrant_client import QdrantClient

    return QdrantClient(
        url=url,
        port=6333,
        https=True,
        api_key=api_key,
        prefer_grpc=False,
        timeout=5,
        check_compatibility=False,
        trust_env=False,
        follow_redirects=False,
    )


def _default_dependencies() -> ActivationDependencies:
    return ActivationDependencies(
        postgres_connection_factory=_default_postgres_connection_factory,
        qdrant_client_factory=_default_qdrant_client_factory,
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
    if phase == _POSTGRES_PHASE:
        if config.get("operator_scope") != _POSTGRES_SCOPE:
            raise ProductMarkerActivationCliError("config_invalid")
        _validate_postgres_public_contract(config.get("contract"))
        return
    if phase == _QDRANT_PENDING_PHASE:
        if config.get("operator_scope") != _QDRANT_PENDING_SCOPE:
            raise ProductMarkerActivationCliError("config_invalid")
        _validate_qdrant_pending_public_contract(config.get("contract"))
        return
    if phase == _QDRANT_FINALIZE_PHASE:
        if config.get("operator_scope") != _QDRANT_FINALIZE_SCOPE:
            raise ProductMarkerActivationCliError("config_invalid")
        _validate_qdrant_finalize_public_contract(config.get("contract"))
        return
    raise ProductMarkerActivationCliError("config_invalid")


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


def _validate_qdrant_pending_public_contract(contract: object) -> None:
    if not isinstance(contract, Mapping) or set(contract) != {
        "activation_hash",
        "coverage_hash",
        "credential_inventory_anchor_hash",
        "generation",
        "previous_generation_hash",
        "secret_contract_hash",
    }:
        raise ProductMarkerActivationCliError("config_invalid")
    for key in ("activation_hash", "coverage_hash", "previous_generation_hash"):
        _validate_raw_hash(contract.get(key))
    for key in ("credential_inventory_anchor_hash", "secret_contract_hash"):
        _validate_hash(contract.get(key))
    generation = contract.get("generation")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or not 0 < generation < 2**63
    ):
        raise ProductMarkerActivationCliError("config_invalid")


def _validate_qdrant_finalize_public_contract(contract: object) -> None:
    if not isinstance(contract, Mapping) or set(contract) != {
        "activation_hash",
        "auth_boundary_status",
        "coverage_hash",
        "credential_inventory_anchor_hash",
        "direct_write_credentials_zero",
        "generation",
        "network_policy_status",
        "previous_generation_hash",
        "read_endpoint_write_denied_status",
        "rendered_inventory",
        "secret_contract_hash",
    }:
        raise ProductMarkerActivationCliError("config_invalid")
    for key in ("activation_hash", "coverage_hash", "previous_generation_hash"):
        _validate_raw_hash(contract.get(key))
    for key in ("credential_inventory_anchor_hash", "secret_contract_hash"):
        _validate_hash(contract.get(key))
    generation = contract.get("generation")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or not 0 < generation < 2**63
        or contract.get("auth_boundary_status") != "validated"
        or contract.get("network_policy_status") != "validated"
        or contract.get("read_endpoint_write_denied_status") != "validated"
        or contract.get("direct_write_credentials_zero") is not True
    ):
        raise ProductMarkerActivationCliError("config_invalid")
    inventory = contract.get("rendered_inventory")
    if not isinstance(inventory, list) or not inventory:
        raise ProductMarkerActivationCliError("config_invalid")
    expected_keys = {
        "active_caller",
        "image_ref_hash",
        "network_policy_ref_hash",
        "route",
        "route_set_hash",
        "source",
        "workload_ref_hash",
        "writer_ref_hash",
    }
    observed_sources: set[str] = set()
    for item in inventory:
        if not isinstance(item, Mapping) or set(item) != expected_keys:
            raise ProductMarkerActivationCliError("config_invalid")
        source = item.get("source")
        route = item.get("route")
        active_caller = item.get("active_caller")
        if (
            not isinstance(source, str)
            or source not in {value.value for value in QdrantMutationSource}
            or source in observed_sources
            or not isinstance(route, str)
            or route not in {value.value for value in QdrantMutationRoute}
            or type(active_caller) is not bool
        ):
            raise ProductMarkerActivationCliError("config_invalid")
        observed_sources.add(source)
        _validate_raw_hash(item.get("writer_ref_hash"))
        identity_fields = (
            "image_ref_hash",
            "network_policy_ref_hash",
            "route_set_hash",
            "workload_ref_hash",
        )
        if active_caller:
            for key in identity_fields:
                _validate_raw_hash(item.get(key))
        elif any(item.get(key) is not None for key in identity_fields):
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


def _qdrant_secret_contract_hash(secret: Mapping[str, object]) -> str:
    product_collections = secret.get("product_collections")
    marker_collection = secret.get("marker_collection")
    metadata_point_id = secret.get("metadata_point_id")
    operator_subject_ref_hash = secret.get("operator_subject_ref_hash")
    url = secret.get("url")
    anchor = secret.get("credential_inventory_anchor_hash")
    if (
        not isinstance(product_collections, list)
        or not product_collections
        or len(product_collections) != len(set(product_collections))
        or any(
            not isinstance(name, str) or not _COLLECTION_NAME.fullmatch(name)
            for name in product_collections
        )
        or not isinstance(marker_collection, str)
        or not _COLLECTION_NAME.fullmatch(marker_collection)
        or marker_collection in product_collections
        or not isinstance(metadata_point_id, str)
        or not isinstance(operator_subject_ref_hash, str)
        or not _RAW_SHA256.fullmatch(operator_subject_ref_hash)
        or not isinstance(url, str)
        or not isinstance(anchor, str)
        or not _SHA256.fullmatch(anchor)
    ):
        raise ProductMarkerActivationCliError("credential_invalid")
    try:
        parsed_point_id = uuid.UUID(metadata_point_id)
    except (ValueError, AttributeError):
        raise ProductMarkerActivationCliError("credential_invalid") from None
    if str(parsed_point_id) != metadata_point_id:
        raise ProductMarkerActivationCliError("credential_invalid")
    _validate_canonical_qdrant_operator_url(url)
    projection = {
        "credential_inventory_anchor_hash": anchor,
        "endpoint_identity_hash": _sha256_text(url),
        "marker_collection_hash": _sha256_text(marker_collection),
        "metadata_point_id_hash": _sha256_text(metadata_point_id),
        "operator_subject_ref_hash": operator_subject_ref_hash,
        "product_collection_hashes": sorted(
            _sha256_text(name) for name in product_collections
        ),
        "schema_version": _QDRANT_SECRET_CONTRACT_SCHEMA,
    }
    encoded = json.dumps(
        projection,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _validate_canonical_qdrant_operator_url(url: str) -> None:
    if (
        not 1 <= len(url) <= 2048
        or any(character < " " or character == "\x7f" for character in url)
        or "\\" in url
        or "%" in url
    ):
        raise ProductMarkerActivationCliError("credential_invalid")
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        raise ProductMarkerActivationCliError("credential_invalid") from None
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
        raise ProductMarkerActivationCliError("credential_invalid")
    if set(hostname) <= set("0123456789."):
        try:
            address = ipaddress.IPv4Address(hostname)
        except ipaddress.AddressValueError:
            raise ProductMarkerActivationCliError("credential_invalid") from None
        if str(address) != hostname:
            raise ProductMarkerActivationCliError("credential_invalid")
        return
    labels = hostname.split(".")
    if (
        len(hostname) > 253
        or any(not label or not _DNS_LABEL.fullmatch(label) for label in labels)
    ):
        raise ProductMarkerActivationCliError("credential_invalid")


def _validate_qdrant_secret(secret: Mapping[str, object]) -> tuple[str, str]:
    if set(secret) != {
        "api_key",
        "credential_inventory_anchor_hash",
        "marker_collection",
        "metadata_point_id",
        "operator_subject_ref_hash",
        "product_collections",
        "schema_version",
        "url",
    } or secret.get("schema_version") != _QDRANT_SECRET_SCHEMA:
        raise ProductMarkerActivationCliError("credential_invalid")
    api_key = secret.get("api_key")
    if (
        not isinstance(api_key, str)
        or not 16 <= len(api_key) <= 4096
        or any(character < " " or character == "\x7f" for character in api_key)
    ):
        raise ProductMarkerActivationCliError("credential_invalid")
    return api_key, _qdrant_secret_contract_hash(secret)


def _build_qdrant_pending_inputs(
    *,
    config: Mapping[str, object],
    secret: Mapping[str, object],
) -> tuple[
    object,
    QdrantCollectionPolicy,
    AuthenticatedQdrantSubject,
    ExactRouteAuthorizer,
    str,
    str,
    str,
]:
    api_key, secret_contract_hash = _validate_qdrant_secret(secret)
    contract_config = config.get("contract")
    if not isinstance(contract_config, Mapping):
        raise ProductMarkerActivationCliError("config_invalid")
    if not hmac.compare_digest(
        str(contract_config.get("secret_contract_hash") or ""),
        secret_contract_hash,
    ) or not hmac.compare_digest(
        str(contract_config.get("credential_inventory_anchor_hash") or ""),
        str(secret.get("credential_inventory_anchor_hash") or ""),
    ):
        raise ProductMarkerActivationCliError("credential_anchor_mismatch")
    marker_collection = str(secret["marker_collection"])
    try:
        anchor = build_qdrant_pending_cutover_anchor(
            generation=contract_config["generation"],  # type: ignore[arg-type]
            marker_collection=marker_collection,
            previous_generation_hash=str(
                contract_config["previous_generation_hash"]
            ),
            coverage_hash=str(contract_config["coverage_hash"]),
            activation_hash=str(contract_config["activation_hash"]),
        )
        policy = QdrantCollectionPolicy(
            product_collections=tuple(secret["product_collections"]),  # type: ignore[arg-type]
            marker_collection=marker_collection,
        )
        subject = AuthenticatedQdrantSubject(
            subject_ref_hash=str(secret["operator_subject_ref_hash"])
        )
        authorizer = ExactRouteAuthorizer(
            bindings=(
                (
                    subject.subject_ref_hash,
                    QdrantMutationSource.OPERATOR_MAINTENANCE,
                    marker_collection,
                ),
            )
        )
    except (KeyError, TypeError, ValueError, QdrantGatewayContractError):
        raise ProductMarkerActivationCliError("qdrant_anchor_invalid") from None
    return (
        anchor,
        policy,
        subject,
        authorizer,
        str(secret["metadata_point_id"]),
        str(secret["url"]),
        api_key,
    )


def _build_qdrant_finalize_inputs(
    *,
    config: Mapping[str, object],
    secret: Mapping[str, object],
) -> tuple[
    object,
    QdrantCollectionPolicy,
    AuthenticatedQdrantSubject,
    ExactRouteAuthorizer,
    str,
    str,
    str,
]:
    api_key, secret_contract_hash = _validate_qdrant_secret(secret)
    contract_config = config.get("contract")
    if not isinstance(contract_config, Mapping):
        raise ProductMarkerActivationCliError("config_invalid")
    if not hmac.compare_digest(
        str(contract_config.get("secret_contract_hash") or ""),
        secret_contract_hash,
    ) or not hmac.compare_digest(
        str(contract_config.get("credential_inventory_anchor_hash") or ""),
        str(secret.get("credential_inventory_anchor_hash") or ""),
    ):
        raise ProductMarkerActivationCliError("credential_anchor_mismatch")
    inventory_config = contract_config.get("rendered_inventory")
    if not isinstance(inventory_config, list):
        raise ProductMarkerActivationCliError("config_invalid")
    marker_collection = str(secret["marker_collection"])
    try:
        rendered_inventory = tuple(
            RenderedQdrantWriter(
                source=QdrantMutationSource(str(item["source"])),
                route=QdrantMutationRoute(str(item["route"])),
                writer_ref_hash=str(item["writer_ref_hash"]),
                active_caller=item["active_caller"],  # type: ignore[arg-type]
                workload_ref_hash=item["workload_ref_hash"],  # type: ignore[arg-type]
                image_ref_hash=item["image_ref_hash"],  # type: ignore[arg-type]
                network_policy_ref_hash=item["network_policy_ref_hash"],  # type: ignore[arg-type]
                route_set_hash=item["route_set_hash"],  # type: ignore[arg-type]
            )
            for item in inventory_config
        )
        anchor = build_qdrant_coverage_activation_anchor(
            generation=contract_config["generation"],  # type: ignore[arg-type]
            marker_collection=marker_collection,
            rendered_inventory=rendered_inventory,
            previous_generation_hash=str(
                contract_config["previous_generation_hash"]
            ),
            auth_boundary_status=str(contract_config["auth_boundary_status"]),
            network_policy_status=str(contract_config["network_policy_status"]),
            direct_write_credentials_zero=contract_config[
                "direct_write_credentials_zero"
            ],  # type: ignore[arg-type]
            read_endpoint_write_denied_status=str(
                contract_config["read_endpoint_write_denied_status"]
            ),
            activation_hash=str(contract_config["activation_hash"]),
        )
        coverage = build_qdrant_coverage_manifest_from_activation_anchor(anchor)
        if not hmac.compare_digest(
            coverage.coverage_hash,
            str(contract_config["coverage_hash"]),
        ):
            raise QdrantGatewayContractError("coverage_hash_mismatch")
        policy = QdrantCollectionPolicy(
            product_collections=tuple(secret["product_collections"]),  # type: ignore[arg-type]
            marker_collection=marker_collection,
        )
        subject = AuthenticatedQdrantSubject(
            subject_ref_hash=str(secret["operator_subject_ref_hash"])
        )
        authorizer = ExactRouteAuthorizer(
            bindings=(
                (
                    subject.subject_ref_hash,
                    QdrantMutationSource.OPERATOR_MAINTENANCE,
                    marker_collection,
                ),
            )
        )
    except (KeyError, TypeError, ValueError, QdrantGatewayContractError):
        raise ProductMarkerActivationCliError("qdrant_anchor_invalid") from None
    return (
        anchor,
        policy,
        subject,
        authorizer,
        str(secret["metadata_point_id"]),
        str(secret["url"]),
        api_key,
    )


class _QdrantActivationClientGuard:
    """Expose only marker activation operations to the typed Qdrant APIs."""

    def __init__(
        self,
        client: object,
        *,
        marker_collection: str,
        metadata_point_id: str,
        phase: QdrantMarkerMetadataPhase,
    ) -> None:
        self._client = client
        self._marker_collection = marker_collection
        self._metadata_point_id = metadata_point_id
        self._phase = phase
        self.mutation_count = 0

    def _method(self, name: str) -> Callable[..., object]:
        method = getattr(self._client, name, None)
        if not callable(method):
            raise QdrantGatewayContractError("qdrant_client_operation_unavailable")
        return method

    def _require_marker_collection(self, collection_name: object) -> None:
        if collection_name != self._marker_collection:
            raise QdrantGatewayContractError("qdrant_activation_scope_invalid")

    def collection_exists(self, collection_name: str) -> object:
        self._require_marker_collection(collection_name)
        return self._method("collection_exists")(collection_name)

    def create_collection(self, **kwargs: object) -> object:
        self._require_marker_collection(kwargs.get("collection_name"))
        if self._phase is not QdrantMarkerMetadataPhase.PENDING_CUTOVER:
            raise QdrantGatewayContractError("qdrant_activation_phase_invalid")
        self.mutation_count += 1
        return self._method("create_collection")(**kwargs)

    def get_collection(self, collection_name: str) -> object:
        self._require_marker_collection(collection_name)
        return self._method("get_collection")(collection_name)

    def create_payload_index(self, **kwargs: object) -> object:
        self._require_marker_collection(kwargs.get("collection_name"))
        if (
            self._phase is not QdrantMarkerMetadataPhase.PENDING_CUTOVER
            or kwargs.get("field_name")
            not in QDRANT_MARKER_REQUIRED_PAYLOAD_INDEX_FIELDS
        ):
            raise QdrantGatewayContractError("qdrant_activation_phase_invalid")
        self.mutation_count += 1
        return self._method("create_payload_index")(**kwargs)

    def retrieve(
        self,
        *,
        collection_name: str,
        ids: list[str],
        **kwargs: object,
    ) -> object:
        self._require_marker_collection(collection_name)
        if ids != [self._metadata_point_id]:
            raise QdrantGatewayContractError("qdrant_activation_point_invalid")
        return self._method("retrieve")(
            collection_name=collection_name,
            ids=ids,
            **kwargs,
        )

    def count(self, **kwargs: object) -> object:
        self._require_marker_collection(kwargs.get("collection_name"))
        if (
            self._phase is not QdrantMarkerMetadataPhase.PENDING_CUTOVER
            or kwargs.get("count_filter") is not None
            or kwargs.get("exact") is not True
        ):
            raise QdrantGatewayContractError("qdrant_activation_count_invalid")
        return self._method("count")(**kwargs)

    def upsert(self, **kwargs: object) -> object:
        self._require_marker_collection(kwargs.get("collection_name"))
        points = kwargs.get("points")
        if not isinstance(points, list) or len(points) != 1:
            raise QdrantGatewayContractError("qdrant_activation_point_invalid")
        point = points[0]
        point_id = getattr(point, "id", None)
        payload = getattr(point, "payload", None)
        mode = str(kwargs.get("update_mode") or "").lower()
        expected_mode = (
            "insert_only"
            if self._phase is QdrantMarkerMetadataPhase.PENDING_CUTOVER
            else "update_only"
        )
        if (
            str(point_id) != self._metadata_point_id
            or not isinstance(payload, Mapping)
            or set(payload) != QDRANT_EXACT_MARKER_METADATA_KEYS
            or not mode.endswith(expected_mode)
            or (
                self._phase is QdrantMarkerMetadataPhase.PENDING_CUTOVER
                and kwargs.get("update_filter") is not None
            )
            or (
                self._phase is QdrantMarkerMetadataPhase.POST_RECONCILE
                and kwargs.get("update_filter") is None
            )
        ):
            raise QdrantGatewayContractError("qdrant_activation_point_invalid")
        self.mutation_count += 1
        return self._method("upsert")(**kwargs)


def _activate_qdrant_pending(
    *,
    config: Mapping[str, object],
    secret: Mapping[str, object],
    dependencies: ActivationDependencies,
) -> tuple[int, int, bool]:
    (
        anchor,
        policy,
        subject,
        authorizer,
        metadata_point_id,
        url,
        api_key,
    ) = _build_qdrant_pending_inputs(config=config, secret=secret)
    try:
        client = dependencies.qdrant_client_factory(url, api_key)
    except Exception:
        raise ProductMarkerActivationCliError("qdrant_connection_failed") from None
    close = getattr(client, "close", None)
    if not callable(close):
        raise ProductMarkerActivationCliError("qdrant_client_invalid")
    guarded_client = _QdrantActivationClientGuard(
        client,
        marker_collection=policy.marker_collection,
        metadata_point_id=metadata_point_id,
        phase=QdrantMarkerMetadataPhase.PENDING_CUTOVER,
    )
    operation_count = 0
    try:
        operation_count = 1
        activate_qdrant_marker_collection(
            client=guarded_client,
            source=QdrantMutationSource.OPERATOR_MAINTENANCE,
            subject=subject,
            authorizer=authorizer,
            policy=policy,
        )
        operation_count = 2
        reconcile_qdrant_marker_metadata(
            client=guarded_client,
            metadata_point_id=metadata_point_id,
            activation_anchor=anchor,  # type: ignore[arg-type]
            phase=QdrantMarkerMetadataPhase.PENDING_CUTOVER,
            source=QdrantMutationSource.OPERATOR_MAINTENANCE,
            subject=subject,
            authorizer=authorizer,
            policy=policy,
        )
    except Exception:
        raise ProductMarkerActivationCliError(
            "qdrant_initialization_failed",
            operation_count=operation_count,
            mutation_count=guarded_client.mutation_count,
            mutation_performed=guarded_client.mutation_count > 0,
        ) from None
    finally:
        try:
            close()
        except Exception:
            raise ProductMarkerActivationCliError(
                "qdrant_close_failed",
                operation_count=operation_count,
                mutation_count=guarded_client.mutation_count,
                mutation_performed=guarded_client.mutation_count > 0,
            ) from None
    return (
        operation_count,
        guarded_client.mutation_count,
        guarded_client.mutation_count > 0,
    )


def _finalize_qdrant_coverage(
    *,
    config: Mapping[str, object],
    secret: Mapping[str, object],
    dependencies: ActivationDependencies,
) -> tuple[int, int, bool]:
    (
        anchor,
        policy,
        subject,
        authorizer,
        metadata_point_id,
        url,
        api_key,
    ) = _build_qdrant_finalize_inputs(config=config, secret=secret)
    try:
        client = dependencies.qdrant_client_factory(url, api_key)
    except Exception:
        raise ProductMarkerActivationCliError("qdrant_connection_failed") from None
    close = getattr(client, "close", None)
    if not callable(close):
        raise ProductMarkerActivationCliError("qdrant_client_invalid")
    guarded_client = _QdrantActivationClientGuard(
        client,
        marker_collection=policy.marker_collection,
        metadata_point_id=metadata_point_id,
        phase=QdrantMarkerMetadataPhase.POST_RECONCILE,
    )
    operation_count = 0
    try:
        operation_count = 1
        reconcile_qdrant_marker_metadata(
            client=guarded_client,
            metadata_point_id=metadata_point_id,
            activation_anchor=anchor,  # type: ignore[arg-type]
            phase=QdrantMarkerMetadataPhase.POST_RECONCILE,
            source=QdrantMutationSource.OPERATOR_MAINTENANCE,
            subject=subject,
            authorizer=authorizer,
            policy=policy,
        )
    except Exception:
        raise ProductMarkerActivationCliError(
            "qdrant_finalization_failed",
            operation_count=operation_count,
            mutation_count=guarded_client.mutation_count,
            mutation_performed=guarded_client.mutation_count > 0,
        ) from None
    finally:
        try:
            close()
        except Exception:
            raise ProductMarkerActivationCliError(
                "qdrant_close_failed",
                operation_count=operation_count,
                mutation_count=guarded_client.mutation_count,
                mutation_performed=guarded_client.mutation_count > 0,
            ) from None
    return (
        operation_count,
        guarded_client.mutation_count,
        guarded_client.mutation_count > 0,
    )


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
    qdrant_pending = subparsers.add_parser("activate-qdrant-pending")
    qdrant_pending.add_argument("--config-root", required=True)
    qdrant_pending.add_argument("--config-name", required=True)
    qdrant_pending.add_argument("--approval-fd", required=True, type=int)
    qdrant_pending.add_argument("--credential-fd", required=True, type=int)
    qdrant_finalize = subparsers.add_parser("finalize-qdrant-coverage")
    qdrant_finalize.add_argument("--config-root", required=True)
    qdrant_finalize.add_argument("--config-name", required=True)
    qdrant_finalize.add_argument("--approval-fd", required=True, type=int)
    qdrant_finalize.add_argument("--credential-fd", required=True, type=int)
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
        if parsed.command == "activate-qdrant-pending":
            phase = _QDRANT_PENDING_PHASE
            try:
                if parsed.approval_fd == parsed.credential_fd:
                    raise ProductMarkerActivationCliError("invalid_arguments")
                config, config_hash = _read_rooted_config(
                    parsed.config_root,
                    parsed.config_name,
                )
                if config.get("phase") != phase:
                    raise ProductMarkerActivationCliError("config_invalid")
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
                    _activate_qdrant_pending(
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
        if parsed.command == "finalize-qdrant-coverage":
            phase = _QDRANT_FINALIZE_PHASE
            try:
                if parsed.approval_fd == parsed.credential_fd:
                    raise ProductMarkerActivationCliError("invalid_arguments")
                config, config_hash = _read_rooted_config(
                    parsed.config_root,
                    parsed.config_name,
                )
                if config.get("phase") != phase:
                    raise ProductMarkerActivationCliError("config_invalid")
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
                    _finalize_qdrant_coverage(
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
